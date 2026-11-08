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
         "'yeah'). If it IS a request for the assistant, route it as usual. ALSO add \"screen\": true to the "
         "JSON when the command is about something on the user's screen right now — 'look at my screen', "
         "'what's this error', 'check this page', 'fix this button', 'what am I looking at' (a screenshot "
         "will be attached); otherwise \"screen\": false.")


def _build_prompt(text: str, focused_title: str | None, candidate_titles: list[str], live: bool = False,
                  asked: str | None = None) -> str:
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(candidate_titles)) or "(none)"
    extra = ""
    if asked:   # hands-free: the agent ended its last answer with a question — "yes" is a reply, not filler
        extra = (f"\n\nThe CURRENT conversation's assistant just asked the user: {asked[:300]!r}. A reply to "
                 "that question — even a short one ('yes', 'no', 'sure', 'go ahead', 'the first one') — is a "
                 "FOLLOW_UP of the current conversation, not filler. Keep the reply as the task.")
    return (f"Current conversation: {focused_title or '(none)'}\n"
            f"Other conversations:\n{listing}\n\nSpoken command: {text!r}" + (_LIVE if live else "") + extra)


def _parse(raw: str, fallback_task: str, allow_ignore: bool = False) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    data = json.loads(raw)
    intent = str(data.get("intent", "NEW")).upper()
    screen = bool(data.get("screen")) if allow_ignore else False    # (hands-free only)
    if intent == "IGNORE" and allow_ignore:
        return {"intent": "IGNORE", "index": None, "task": fallback_task, "screen": False}
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
                 asked: str | None = None) -> dict:
        """Return {"intent": NEW|FOLLOW_UP|RESUME (|IGNORE when live), "index": int|None, "task": str}.
        Raises on not-ready / timeout / parse failure — the caller falls back safely."""
        import time
        if not self._ready.wait(8.0) or self._loop is None or self._failed:
            raise RuntimeError("intent router not ready")
        prompt = _build_prompt(text, focused_title, candidate_titles, live, asked)
        t0 = time.time()
        with self._call_lock:                          # serialize (single warm conversation)
            fut = asyncio.run_coroutine_threadsafe(self._classify_async(prompt), self._loop)
            raw = fut.result(timeout=timeout)
        d = _parse(raw, text, allow_ignore=live)
        logger.info("[intent-router] %.2fs | heard=%r focused=%r cands=%s -> %s idx=%s task=%r",
                    time.time() - t0, text, focused_title, candidate_titles,
                    d["intent"], d["index"], d["task"])
        return d


def classify_with_codex(text: str, focused_title: str | None, candidate_titles: list[str],
                        live: bool = False, asked: str | None = None) -> dict:
    """The same decision made by Codex — when Claude Code isn't available. A cold `codex exec` per call
    (slower than the warm Claude client, several seconds). Raises on any failure, like classify()."""
    import time
    from app.core.codex_runner import complete
    t0 = time.time()
    d = _parse(complete(_SYS, _build_prompt(text, focused_title, candidate_titles, live, asked)), text,
               allow_ignore=live)
    logger.info("[intent-router] codex %.2fs | heard=%r -> %s idx=%s task=%r",
                time.time() - t0, text, d["intent"], d["index"], d["task"])
    return d


def warm_up():
    """Start the warm client early (e.g. at app launch) so the first voice command isn't cold."""
    try:
        IntentRouter.get()
    except Exception:
        pass
