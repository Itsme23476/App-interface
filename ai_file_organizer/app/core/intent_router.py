"""Voice intent router — classifies a spoken command as NEW / FOLLOW_UP / RESUME, on the user's
Claude subscription (the agent SDK uses the Claude Code login, not an API key).

Runs a WARM, long-lived ClaudeSDKClient on its own asyncio thread so each classification is a message,
not a cold `claude` subprocess spawn — measured ~2.5s warm vs ~4s cold (the spawn, not the model, is
the cost, so the model choice is latency-neutral; we default to Opus). The client is recycled every
~20 calls to bound conversation growth, and the system prompt tells it to classify EACH message
independently, so reusing one conversation doesn't bleed context (verified). ANY failure raises so the
caller can fall back safely (NEW / continue-the-focused).

Windows: ported as-is (the warm client runs the user's Claude Code without a console window — see
agent_runner.hide_cli_windows, applied by use_login_path). When Claude Code can't run, Codex decides (classify_with_codex).

Decided with the user: Opus (configurable via settings.assistant_router_model). Not nano (their
backend cost + silent auth-fail), not a word-gate (can't catch a topic shift that never says "new").
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import threading

logger = logging.getLogger("ai_file_organizer")

_RECYCLE_EVERY = 20          # ponytail: recycle the warm client every N calls to bound context growth

_SYS = (
    "You route a short spoken command for a local coding assistant that can run SEVERAL conversations "
    "at once. Pick ONE intent: NEW (a brand-new task), FOLLOW_UP (continue the CURRENT conversation), "
    "or RESUME (switch back to a specific earlier conversation the user names). Default to FOLLOW_UP "
    "when there is a current conversation and the command reads like more work on it or a tweak. Pick "
    "NEW when it's clearly a different task, or they say 'new task', 'start over', 'separately'. Pick "
    "RESUME only when they explicitly point to a specific earlier conversation by name/topic, and set "
    "index to its number. Treat EACH message independently — classify only the command in THIS message, "
    "ignore earlier ones. "
    'Respond with ONLY compact JSON: {"intent":"NEW|FOLLOW_UP|RESUME","index":<number or null>,'
    '"task":"<the instruction to carry out, with any routing phrasing removed>"}.'
)


# Hands-free (Filect Assistant): an always-on mic hears things that aren't meant for the assistant at
# all, so for those messages ONLY a 4th intent is allowed. (Per message, so the warm conversation's
# system prompt stays the same for fn + ⌃, where IGNORE is never accepted.)
_LIVE = ("\n\nHANDS-FREE: this was picked up by an always-on microphone, so it may not be meant for "
         "the assistant at all. For THIS message only, a 4th intent is allowed: IGNORE — use it when the "
         "speech is NOT an instruction or question for the assistant: talking to another person, a phone "
         "call, thinking out loud, reading something aloud, a TV / video / music, filler ('okay', 'hmm', "
         "'yeah'), or words addressed to a DIFFERENT voice assistant or a named person ('Hey Siri, …', "
         "'Alexa, …', 'OK Google, …', 'Mark, can you …'). If it IS a request for the assistant, route it as "
         "usual. ALSO add \"screen\": true to the JSON when the command is about something on the user's "
         "screen right now — 'look at my screen', 'watch my screen', 'look at this', 'what's this error', "
         "'check this page', 'fix this button', 'what am I looking at' (a screenshot will be attached); "
         "otherwise \"screen\": false. A 5th intent is also allowed for THIS message only: "
         "ORGANIZE — use it ONLY when the user wants the FILES in a folder on this computer sorted into folders: "
         "'organize my Downloads', 'clean up my desktop', 'sort the screenshots folder by type', 'sort the Taxes "
         "folder by year', 'tidy up my Documents folder'.NOT for code or text ('organize the imports', 'clean up this function', 'sort this "
         "list', 'clean up the repo'), NOT for a question about organizing, NOT for emails, notes or tasks. For "
         "ORGANIZE, task is ONLY how they want it sorted ('by type', 'put the invoices in a folder called Money'), "
         "without which folder; use \"\" if they didn't say how. Two more intents for THIS message only. ORGANIZE_AUTO: they "
         "want a folder kept organized automatically from now on, so files keep getting sorted without asking: 'keep my "
         "Downloads tidy', 'auto organize my desktop', 'always sort whatever lands in my Downloads'. task is any sorting "
         "rules they gave, without which folder, or \"\". Add \"mode\" only if they said what to do with what's there: "
         "\"everything\" (sort what's there now too), \"existing\" (only into the folders already there), or \"new\" (only new "
         "files from now on). ORGANIZE_AUTO_OFF: they want automatic organizing stopped for a folder ('stop auto organizing "
         "my Downloads', 'you can stop keeping my desktop tidy now'); task is ONLY the folder's name as "
         "they said it, or \"\" if they didn't name one ('that folder' is not a name). A one-time 'organize my Downloads' is "
         "ORGANIZE, and a question about the feature is NEW.")


# Hands-free, while Filect is organizing a folder: what the decider may also answer. Offered only when it fits.
_ORGANIZE_INTENTS = {
    "ORGANIZE_UNDO": "they want that taken back, in any words ('undo those changes', 'I changed my mind, put them back', "
                     "'revert that', and 'forget it' or 'never mind' said right after it finished).",
    "ORGANIZE_CHOICE": "they picked one of the ways they were asked about, in any words ('only new files', 'just use the "
                       "folders that are there', 'the first one' = everything, 'the second one' = existing, 'the third "
                       "one' = new, as they were listed). Set task to exactly everything, existing, or new.",
    "ORGANIZE_GO": "they agree to what's being asked ('looks good', 'go ahead', 'yeah, do it').",
    "ORGANIZE_CANCEL": "they don't want it any more, in any words ('forget it', 'never mind', 'no, don't', 'I changed my "
                       "mind, don't move anything', 'stop', 'leave them all as they are').",
    "ORGANIZE_CHANGE": "a change to the plan on screen ('put the screenshots in their own folder', 'call it Pictures "
                       "instead', 'leave the PDFs where they are'), or their answer to which folder ('the one on my "
                       "desktop', 'the downloads one'), or the instructions they were asked for; put it in task.",
    "ORGANIZE_NO_RULES": "they have no instructions and leave it to Filect ('no', 'nope', 'no instructions', 'you decide', "
                         "'whatever you think is best', 'not really').",
    "ORGANIZE": "they want a different folder organized instead.",
}


def _organize_block(organize: dict) -> str:
    offered = " ".join(f"{k}: {_ORGANIZE_INTENTS[k]}" for k in organize["allowed"])
    return ("\n\nFILECT IS ORGANIZING FILES (separate from the conversations above): " + organize["text"] +
            " For THIS message these intents are also allowed, and they win over FOLLOW_UP, NEW, RESUME and IGNORE "
            "when the words are about that organizing rather than the current conversation's own work. A short reply "
            "to what Filect just asked ('the first one', 'no, don't', 'the one on my desktop', 'never mind') is always "
            "about it: never IGNORE, and never RESUME of a conversation. " + offered +
            " If the current conversation changed files more recently than the organize and the words could mean either, "
            "it is about the current conversation.")


def _build_prompt(text: str, focused_title: str | None, candidate_titles: list[str], live: bool = False,
                  asked: str | None = None, organize: dict | None = None) -> str:
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(candidate_titles)) or "(none)"
    extra = ""
    if asked:   # hands-free: the agent ended its last answer with a question — "yes" is a reply, not filler
        extra = (f"\n\nThe CURRENT conversation's assistant just asked the user: {asked[:300]!r}. A reply to "
                 "that question — even a short one ('yes', 'no', 'sure', 'go ahead', 'the first one') — is a "
                 "FOLLOW_UP of the current conversation, not filler. Keep the reply as the task.")
    return (f"Current conversation: {focused_title or '(none)'}\n"
            f"Other conversations:\n{listing}\n\nSpoken command: {text!r}" + (_LIVE if live else "") + extra +
            (_organize_block(organize) if organize and live else ""))


def _parse(raw: str, fallback_task: str, allow_ignore: bool = False, organize: dict | None = None) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    data = json.loads(raw)
    intent = str(data.get("intent", "NEW")).upper()
    screen = bool(data.get("screen")) if allow_ignore else False    # (hands-free only)
    if intent == "IGNORE" and allow_ignore:
        return {"intent": "IGNORE", "index": None, "task": fallback_task, "screen": False}
    # (Windows: all of these only when hands-free. Shift+Z is never offered them and its answers stay exactly as
    # before — an unknown intent there is NEW, as it always was.)
    if intent.startswith("ORGANIZE_") and intent not in ("ORGANIZE_AUTO", "ORGANIZE_AUTO_OFF") and allow_ignore:
        if intent in ((organize or {}).get("allowed") or ()):
            return {"intent": intent, "index": None, "task": str(data.get("task") or fallback_task).strip(), "screen": False}
        return {"intent": "IGNORE", "index": None, "task": fallback_task, "screen": False}   # (never a stray new chat)
    if intent in ("ORGANIZE_AUTO", "ORGANIZE_AUTO_OFF") and allow_ignore:
        mode = data.get("mode") if data.get("mode") in ("everything", "existing", "new") else None
        return {"intent": intent, "index": None, "task": str(data.get("task") or "").strip(), "screen": False, "mode": mode}
    if intent == "ORGANIZE" and allow_ignore:       # (task: how to sort it; often nothing. The folder comes from `heard`.)
        return {"intent": "ORGANIZE", "index": None, "task": str(data.get("task") or "").strip(), "screen": False}
    if intent not in ("NEW", "FOLLOW_UP", "RESUME"):
        intent = "NEW"
    idx = data.get("index")
    idx = idx if isinstance(idx, int) else None
    task = (data.get("task") or fallback_task).strip() or fallback_task
    return {"intent": intent, "index": idx, "task": task, "screen": screen}


class IntentRouter:
    """Singleton warm classifier. `classify()` is synchronous (call it off the UI thread)."""
    _instance: "IntentRouter | None" = None
    _init_lock = threading.Lock()

    @classmethod
    def get(cls) -> "IntentRouter":
        with cls._init_lock:
            if cls._instance is None or cls._instance._failed:   # e.g. Claude Code installed since
                model = "opus"
                try:
                    from app.core.settings import settings as _s
                    model = getattr(_s, "assistant_router_model", "") or "opus"
                except Exception:
                    pass
                cls._instance = cls(model)
            return cls._instance

    def __init__(self, model: str):
        self._model = model
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._calls = 0
        self._failed = False                   # the warm client couldn't start (no Claude Code)
        self._ready = threading.Event()
        self._call_lock = threading.Lock()     # one warm conversation → serialize classifications
        self._thread = threading.Thread(target=self._run, daemon=True, name="filect-intent-router")
        self._thread.start()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._open())
            self._ready.set()
            self._loop.run_forever()
        except Exception as e:                 # a dead loop just means callers fall back (Codex / NEW)
            logger.warning("[intent-router] loop failed to start: %s", e)
            self._failed = True
            self._ready.set()                  # don't make classify() wait 8s to find out

    async def _open(self):
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
        from app.core.agent_runner import use_login_path
        use_login_path()
        from app.core.agent_runner import claude_cli_override
        # Its own folder (Filect's app data): Claude Code saves every classification as a chat in the cwd —
        # left to the process cwd they piled up as "Current conversation: …" chats in whatever project Filect
        # was started from (for a developer: the source folder's Claude Code history).
        cwd = None
        try:
            from app.core.settings import settings as _s
            cwd = _s.get_app_data_dir() / "agent-router"
            cwd.mkdir(parents=True, exist_ok=True)
        except Exception:
            cwd = None
        opts = ClaudeAgentOptions(model=self._model, setting_sources=[], max_turns=1, cwd=cwd,
                                  system_prompt=_SYS, allowed_tools=[],
                                  **({"cli_path": claude_cli_override()} if claude_cli_override() else {}))
        self._client = ClaudeSDKClient(options=opts)
        await self._client.__aenter__()
        logger.info("[intent-router] warm client ready (model=%s)", self._model)

    async def _reopen(self):
        try:
            await self._client.__aexit__(None, None, None)
        except Exception:
            pass
        await self._open()

    async def _classify_async(self, prompt: str) -> str:
        from claude_agent_sdk import AssistantMessage, TextBlock
        self._calls += 1
        if self._calls % _RECYCLE_EVERY == 0:          # bound conversation growth
            await self._reopen()
        await self._client.query(prompt)
        out = []
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        out.append(b.text)
        return "".join(out)

    def classify(self, text: str, focused_title: str | None,
                 candidate_titles: list[str], timeout: float = 12.0, live: bool = False,
                 asked: str | None = None, organize: dict | None = None) -> dict:
        """Return {"intent": NEW|FOLLOW_UP|RESUME (|IGNORE|ORGANIZE… when live), "index": int|None, "task": str}.
        `organize` (hands-free only): what Filect's organizing is doing, {"text", "allowed"}.
        Raises on not-ready / timeout / parse failure — the caller falls back safely."""
        import time
        if not self._ready.wait(8.0) or self._loop is None or self._failed:
            raise RuntimeError("intent router not ready")
        prompt = _build_prompt(text, focused_title, candidate_titles, live, asked, organize)
        t0 = time.time()
        with self._call_lock:                          # serialize (single warm conversation)
            fut = asyncio.run_coroutine_threadsafe(self._classify_async(prompt), self._loop)
            raw = fut.result(timeout=timeout)
        d = _parse(raw, text, allow_ignore=live, organize=organize)
        logger.info("[intent-router] %.2fs | heard=%r focused=%r cands=%s -> %s idx=%s task=%r",
                    time.time() - t0, text, focused_title, candidate_titles,
                    d["intent"], d["index"], d["task"])
        return d


def classify_with_codex(text: str, focused_title: str | None, candidate_titles: list[str],
                        live: bool = False, asked: str | None = None, organize: dict | None = None) -> dict:
    """The same decision made by Codex — when Claude Code isn't available. A cold `codex exec` per call
    (slower than the warm Claude client, several seconds). Raises on any failure, like classify()."""
    import time
    from app.core.codex_runner import complete
    t0 = time.time()
    prompt = _build_prompt(text, focused_title, candidate_titles, live, asked, organize)
    if live and organize and set(organize.get("allowed", ())) & {"ORGANIZE_GO", "ORGANIZE_CHOICE", "ORGANIZE_CANCEL",
                                                                 "ORGANIZE_NO_RULES"}:
        # (Measured: a one-shot `codex exec` leans on the system prompt's NEW / FOLLOW_UP / RESUME list and read
        # replies to Filect's question — 'go ahead', 'nope', 'the last one', 'desktop' — as IGNORE / RESUME / NEW,
        # 60/76. The warm Claude client doesn't need this: 87/87 without it.)
        prompt += ("\n\nFINAL CHECK: Filect is waiting for the user's answer about organizing (see above). A reply to it — "
                   "even one or two words ('go ahead', 'yeah that one', 'correct', 'nope', 'the last one', 'desktop', "
                   "'scrap that') — is one of " + ", ".join(organize["allowed"]) + "; it is NEVER IGNORE, FOLLOW_UP "
                   "or RESUME. Use NEW only for an unrelated request, IGNORE only for talk that isn't to the assistant.")
    d = _parse(complete(_SYS, prompt), text, allow_ignore=live, organize=organize)
    logger.info("[intent-router] codex %.2fs | heard=%r -> %s idx=%s task=%r",
                time.time() - t0, text, d["intent"], d["index"], d["task"])
    return d


# ------------------------------------------------------------------ organizing by voice: plain answers + a guard
# Measured 2026-11-09 (decider_eval, 87 sentences): Claude 87/87, Codex 73/87. Codex's misses were answers said
# plainly ("undo", "scrap that", "nah just do whatever makes sense", "yeah that one", "the documents one") read as
# IGNORE / FOLLOW_UP / NEW / RESUME. organize_quick decides those without any AI (both deciders, and instantly);
# organize_guard turns a decision that can't be right while Filect waits for an answer into the answer it is.
_WAITING = {"preview", "ask_read", "ask_folder", "ask_guess", "ask_mode", "ask_rules", "confirm_auto", "ask_stop"}
_LEAD = {"uh", "um", "umm", "uhm", "erm", "oh", "ah", "hmm", "hm", "well", "so", "okay", "ok", "actually", "wait",
         "hey", "like", "just", "please", "alright", "filect"}
_CANCEL_LEAD = _LEAD | {"no", "nah", "nope", "hold", "on"}
_CANCEL_CORE = re.compile(
    r"(?:never ?mind|forget (?:it|about it|that|this)|scrap (?:that|it|this)|cancel(?: (?:that|it|this|everything))?"
    r"|stop(?: (?:that|it|this|stop|stop stop))?|(?:i )?(?:don't|do not) want (?:this|that|it|to)(?: anymore)?"
    r"|(?:i )?changed my mind|leave it(?: (?:off|alone|as it is|for now))?|leave (?:them|everything) "
    r"(?:on|alone|as (?:they are|it is)|where (?:they are|it is))|don't (?:bother|do (?:it|that|anything))"
    r"|(?:don't|do not) (?:move|change|touch) anything|not now|no thanks|skip (?:it|that)"
    r")")
_GO_CORE = (r"(?:that(?:'s| is) (?:the one|it|right|correct|the right one)|that one|this one|go ahead|go for it|do it"
            r"|sounds good|looks good|please do|let's do it|let's go|correct|exactly|absolutely|perfect)")
_GO = re.compile(r"(?:(?:yes|yeah|yep|yup|sure|ok|okay|alright|great|perfect|correct|right|exactly|absolutely) )*"
                 + _GO_CORE + r"(?: (?:" + _GO_CORE + r"|please|thanks|thank you|then|now))*")
_NO_RULES = re.compile(
    r"(?:you (?:decide|choose|pick|can decide|figure it out)|(?:it's )?up to you|your call|surprise me"
    r"|whatever(?: you (?:think|want|like|decide|prefer))?(?: (?:makes sense|is best|works|is fine))?"
    r"|do (?:whatever|what you (?:think|want))(?: you (?:think|want))?(?: (?:makes sense|is best|works))?"
    r"|no (?:instructions|rules|preferences?)|none|not really|(?:i )?(?:don't|do not) (?:have any|mind|care)"
    r"|anything(?: is fine)?|however you (?:want|like|think))")
_ABOUT_TO_SAY = re.compile(r"(?:yes|yeah|yep|yup|sure|i do|yes i do|yeah i do|i have some|i've got some|yeah i have some"
                           r"|let me think|hold on|wait|one sec(?:ond)?|give me a sec(?:ond)?)")
_UNDO = re.compile(
    r"(?:(?:can you|could you) )?(?:undo(?: (?:that|it|this|those|everything|all of it|the last one|what you (?:just )?did))?"
    r"|put (?:them|it|those|everything|the files|my files|all of it|it all) back(?: where (?:they|it) (?:was|were))?"
    r"|revert(?: (?:that|it|this))?|take (?:that|it) back|move (?:them|it|everything|the files) back|reverse (?:that|it))"
    r"(?: (?:please|now|then))*")
_QUESTION = re.compile(r"^(?:how|what|why|when|where|which|who|whose|is it|is there|are you|are there|does|did you"
                       r"|should i|can i|could i|would it|will it)\b")
_PLAN_WORDS = re.compile(r"\b(?:put|move|stick|place|keep|leave|group|split|merge|combine|separate|rename|call|name|sort"
                         r"|file|drop|throw|send|instead|folders?|together|own|by (?:year|date|type|month|topic|name"
                         r"|client|project|size)|don't move|do not move)\b")
_AUTO_OFF = re.compile(
    r"\b(?:(?:turn|switch|shut)(?:ing)? off|stop|disable|pause|deactivate)\b.{0,30}?\b(?:auto[\s-]?organi[sz]\w*"
    r"|automatic(?:ally)? (?:organi[sz]\w*|sort\w*|tidy\w*)|keeping\b.{1,40}\b(?:tidy|organi[sz]ed|neat|sorted|clean))"
    r"|\bauto[\s-]?organi[sz]\w*\b.{0,30}\b(?:off|stop)\b"
    r"|\b(?:don't|do not|no longer)\b.{0,15}\b(?:sort|organi[sz]e|tidy|clean)\w*\b.{0,40}\b(?:automatically|anymore)\b")
# A request (not a remark: "my sister keeps her downloads organized automatically"): it starts with what to do.
_REQUEST = re.compile(r"^(?:(?:please|okay|ok|so|hey|filect|um|uh|and|now|also|just|actually|alright|you can|can you"
                      r"|could you|would you|will you|i want you to|i'd like you to|i need you to|go ahead and) )*"
                      r"(?:keep|set|turn|switch|shut|start|stop|disable|pause|deactivate|auto|automatically|organi[sz]e"
                      r"|sort|tidy|clean|from now on|whenever|every time|each time|make sure|watch|don't|do not)\b")
_AUTO_ON = re.compile(
    r"\bauto[\s-]?organi[sz]\w*|\bautomatic(?:ally)?\s+(?:organi[sz]\w*|sort\w*|tidy\w*|clean\w*)"
    r"|\b(?:organi[sz]\w*|sort\w*|tidy\w*|clean\w*)\b.{0,40}\bautomatically\b"
    r"|\bkeep(?:ing)?\b.{1,40}\b(?:tidy|organi[sz]ed|neat|sorted|clean)\b"
    r"|\b(?:whenever|every time|each time)\b.{0,40}\b(?:lands|arrives|comes in|download)")
_AUTO_OFF_NOISE = set("""turn turning off switch switching shut shutting stop stopping disable pause deactivate auto
organize organise organizing organising organized organised organizer organiser automatically automatic auto-organize
keep keeping tidy neat sorted sorting sort clean the my for on in of folder folders too now please can you could would
just also and it that this anymore any more all from then so ok okay hey filect i want to don't need longer no be being
thing feature there one ones""".split())


def _plain(text: str) -> str:
    t = (text or "").lower().replace("’", "'").replace("dont ", "don't ")
    return " ".join(re.sub(r"[^a-z0-9' ]", " ", t).split())


def _strip_lead(words: list, lead: set) -> list:
    while words and words[0] in lead:
        words = words[1:]
    if words[:2] == ["can", "you"] or words[:2] == ["could", "you"]:
        words = words[2:]
    return words


def _segments(text: str) -> list:
    return [s for s in (_plain(x) for x in re.split(r"[,.;!?]+|\band\b", text or "")) if s]


def _decision(intent: str, task: str = "", mode=None) -> dict:
    return {"intent": intent, "index": None, "task": task, "mode": mode, "screen": False}


def organize_quick(text: str, organize: dict | None):
    """Answers to what Filect's organize panel is asking, said plainly enough to need no AI — or None (the decider
    reads it). `organize` is the controller's decider_state() (with "stage"). Conservative: only whole replies made
    of these phrases; anything with more in it ("yes but put the PDFs in Docs") goes to the decider."""
    if not organize or not text:
        return None
    stage, allowed = organize.get("stage"), set(organize.get("allowed") or ())
    words = _plain(text).split()
    if not words:
        return None
    if stage in _WAITING or stage in ("scanning", "reading", "planning", "finding"):
        # "never mind" / "hold on, scrap that" / "actually stop, I don't want this" (every part a way to stop)
        parts = [" ".join(_strip_lead(s.split(), _CANCEL_LEAD)) for s in _segments(text)]
        parts = [p for p in parts if p]
        if "ORGANIZE_CANCEL" in allowed and parts and all(_CANCEL_CORE.fullmatch(p) for p in parts):
            return _decision("ORGANIZE_CANCEL", text)
    if stage == "ask_rules":
        rest = _strip_lead(words, _LEAD | {"no", "nah", "nope", "honestly"})
        said_no = words[0] in ("no", "nah", "nope", "none")
        if "ORGANIZE_NO_RULES" in allowed and ((not rest and said_no) or _NO_RULES.fullmatch(" ".join(rest))):
            return _decision("ORGANIZE_NO_RULES", "")
        if _ABOUT_TO_SAY.fullmatch(" ".join(_strip_lead(words, _LEAD - {"wait"}))):
            return _decision("IGNORE", text)          # ("yeah I do" — the instructions come next)
    if stage == "ask_mode" and "ORGANIZE_CHOICE" in allowed:
        # "number two please" (Codex: "reopen the 2nd chat") / "the last one" / "just the new stuff"
        t = " ".join(_strip_lead(words, _LEAD))
        pick = re.fullmatch(r"(?:the )?(first|second|third|last|middle|top|bottom)(?: one| option)?(?: please)?"
                            r"|(?:number|option) (one|two|three|1|2|3)(?: please)?", t)
        if pick:
            which = pick.group(1) or pick.group(2)
            mode = {"first": "everything", "top": "everything", "one": "everything", "1": "everything",
                    "second": "existing", "middle": "existing", "two": "existing", "2": "existing",
                    "third": "new", "last": "new", "bottom": "new", "three": "new", "3": "new"}[which]
            return _decision("ORGANIZE_CHOICE", mode)
        whole = " ".join(words)
        if len(words) <= 10 and _auto_mode(whole):
            return _decision("ORGANIZE_CHOICE", _auto_mode(whole))
    if stage in ("ask_folder", "ask_stop") and "ORGANIZE_CHANGE" in allowed:
        # "desktop" / "the downloads one" / "the one in documents" (Codex: IGNORE, RESUME)
        t = " ".join(_strip_lead(words, _LEAD))
        if re.fullmatch(r"(?:the )?(?:one )?(?:(?:on|in) )?(?:my |the )?(?:desktop|desk top|downloads?|documents?)"
                        r"(?: one| folder)?(?: please)?", t):
            return _decision("ORGANIZE_CHANGE", text)
    if "ORGANIZE_GO" in allowed and stage in ("preview", "ask_read", "ask_guess", "confirm_auto"):
        t = " ".join(_strip_lead(words, _LEAD - {"okay", "ok", "alright"}))
        if _GO.fullmatch(t):
            return _decision("ORGANIZE_GO", text)
    if "ORGANIZE_UNDO" in allowed and stage == "after" and "more recently" not in (organize.get("text") or ""):
        t = " ".join(_strip_lead(words, _LEAD | {"no"}))
        if _UNDO.fullmatch(t):
            return _decision("ORGANIZE_UNDO", text)
    return None


def _auto_mode(t: str):
    if re.search(r"already (?:there|in (?:it|there))|existing folders|folders (?:that are )?(?:already )?(?:in it|there)"
                 r"|only (?:into|use) the folders|the folders (?:it|that) (?:has|already)", t):
        return "existing"
    if re.search(r"\bonly (?:for )?(?:the )?new|new (?:files|ones|stuff|downloads) only|just (?:the )?new|only what (?:comes"
                 r"|arrives|lands)|from now on only", t):
        return "new"
    if re.search(r"\beverything\b|\ball of it\b|what's (?:in )?there now|that's in (?:it|there)|(?:old|existing) "
                 r"(?:stuff|files) too|right now too|in there now", t):
        return "everything"
    return None


def _auto_how(text: str) -> str:
    """What the sentence says about how to sort ("…, put the bills in a folder called Money"), or ""."""
    m = re.search(r"(?:^|[,;]|\band\b)\s*(?:(?:like|then|also|just|please|maybe)\s+)*((?:put|move|send|file|bills"
                  r"|invoices|photos|pictures|screenshots|sort them|group|by (?:year|date|type|month|topic))\b.*)$",
                  text or "", re.I)
    return m.group(1).strip(" .") if m else ""


def organize_guard(decision: dict, text: str, organize: dict | None, folders=None) -> dict:
    """The decider's answer, unless it can't be right: while Filect waits for an answer, a reply read as a new chat /
    follow-up / old chat; with nothing on screen, 'turn off auto organize' read as a new chat, or "organize my <a
    folder that exists by exactly that name>" read as a new chat. `folders`: the folders organize could mean
    (a list, or a callable giving one — only called when needed). Returns the (possibly corrected) decision."""
    intent = decision.get("intent")
    t = _plain(text)
    words = t.split()
    if not words or _QUESTION.match(t):
        return decision
    stage = (organize or {}).get("stage")
    allowed = set((organize or {}).get("allowed") or ())
    chatty = intent in ("NEW", "FOLLOW_UP", "RESUME")
    edits = re.match(r"^(?:(?:hmm|um|uh|oh|okay|ok|so|actually|and|also|wait|no|now|then|next) )*"
                     r"(?:(?:can|could|would) you )?(?:please )?(?:rename|call|name|put|move|stick|merge|split|combine"
                     r"|group|leave|keep|don't move|do not move|place|drop)\b", t)
    if stage == "after" and "ORGANIZE" in allowed and intent in ("NEW", "FOLLOW_UP", "RESUME") and edits and \
            "more recently" not in ((organize or {}).get("text") or "") and re.search(r"\b(?:folder|in|into|to)\b", t):
        # (Codex, measured live: "now put the recipe in a folder called Kitchen" right after organizing came back as
        # NEW — a new agent chat went looking for recipes. Right after, moving files naming no other folder is
        # ORGANIZE of the same folder; the decider is told so, but Codex didn't follow it.)
        return _decision("ORGANIZE", text)
    if intent == "ORGANIZE" and stage in ("preview", "ask_rules") and edits and "ORGANIZE_CHANGE" in allowed:
        # (Codex, measured: "can you put the invoices and receipts together in one folder called Money" and "merge
        # the docs and the PDFs into one folder" with the plan up came back as "organize a folder")
        return _decision("ORGANIZE_CHANGE", text)
    if intent == "ORGANIZE" and stage not in _WAITING and not (decision.get("task") or "").strip():
        how = _auto_how(text)                          # (Codex dropped "…and like put all the PDFs together")
        if how:
            return {**decision, "task": how}
    if stage in _WAITING:
        if not chatty:
            return decision
        mode = _auto_mode(t)
        if mode and "ORGANIZE_CHOICE" in allowed and stage in ("ask_mode", "ask_rules", "confirm_auto", "preview"):
            return _decision("ORGANIZE_CHOICE", mode)  # ("make it sort everything that's there now too")
        if "ORGANIZE_CHANGE" not in allowed:
            return decision
        if stage in ("ask_folder", "ask_guess", "ask_stop") and len(words) <= 8:
            return _decision("ORGANIZE_CHANGE", text)   # ("the documents one" — an answer, not an old chat)
        if stage in ("preview", "ask_rules") and _PLAN_WORDS.search(t):
            from app.core.ai_organizer import sentence_names_a_folder
            # (naming a folder is fine when it's what to do with the plan — "rename the photos folder to Snapshots"
            # — not when it's which folder to do — "do my Documents folder instead")
            if stage == "ask_rules" or edits or not sentence_names_a_folder(text):
                return _decision("ORGANIZE_CHANGE", text)   # ("stick the screenshots somewhere separate")
        return decision
    if stage == "after" and "ORGANIZE_UNDO" in allowed and intent in ("IGNORE", "NEW", "FOLLOW_UP") and \
            "more recently" not in (organize.get("text") or "") and re.fullmatch(
                r"(?:(?:actually|oh|no|wait|hmm|um|uh) )*(?:forget (?:it|that)|(?:i )?changed my mind"
                r"(?: undo (?:it|that|those changes))?)", t):
        return _decision("ORGANIZE_UNDO", text)        # (right after organizing, "forget it" takes it back)
    if intent not in ("NEW", "FOLLOW_UP") or not _REQUEST.match(t):
        return decision
    if re.search(r"\b(?:don't|do not|never|not)\s+(?:stop|turn|switch|disable|pause)", t):
        return decision
    if _AUTO_OFF.search(t):
        name = " ".join(w for w in words if w not in _AUTO_OFF_NOISE)
        return _decision("ORGANIZE_AUTO_OFF", name)
    if _AUTO_ON.search(t) and not re.search(r"\b(?:turn|switch|shut)(?:ing)? off\b|\bstop\b|\bdisable\b", t):
        return _decision("ORGANIZE_AUTO", _auto_how(text), _auto_mode(t))
    if folders is not None and stage not in _WAITING and re.match(
            r"^(?:(?:please|okay|ok|so|hey|filect|um|uh|now|also|just|actually|can you|could you|would you) )*"
            r"(?:organi[sz]e|sort(?: out)?|tidy(?: up)?|clean up|arrange)\b", t):
        # (Codex, measured live: "Organize my Demo Game Code folder" came back as NEW — a coding task, by the word
        # "Code". Organizing a folder that exists by exactly that name is organizing it: Filect then decides —
        # a code project is refused there, untouched.)
        try:
            cands = folders() if callable(folders) else folders
            from app.core.ai_organizer import _folder_named_in, _word_guess_folder
            if cands and _folder_named_in(text, list(cands)):
                return _decision("ORGANIZE", _auto_how(text))
            # "organize my messy stuff" (Codex, live: NEW) — about files, and its words fit exactly one folder.
            # (A guess: the controller asks "did you mean…?" before anything is read or moved.)
            if cands and len(words) <= 10 and re.search(
                    r"\b(?:stuff|files?|things|folders?|mess|docs|documents|downloads|photos|pictures|screenshots"
                    r"|pile|junk)\b", t) and _word_guess_folder(text, list(cands)):
                return _decision("ORGANIZE", _auto_how(text))
        except Exception as e:
            logger.info("[intent-router] organize guard couldn't list folders: %s", e)
    return decision


def warm_up():
    """Start the warm client early (e.g. at app launch) so the first voice command isn't cold."""
    try:
        IntentRouter.get()
    except Exception:
        pass
