"""Browse local Claude Code sessions so the Agents page can list and RESUME existing conversations —
across ALL projects, not just its own workspace. (Ported from the Mac app.)

These are the same JSONL files Claude Code and the Claude app keep under
%USERPROFILE%\\.claude\\projects\\<encoded-cwd>\\<session-id>.jsonl. Read-only: we never write there.
Resuming one is just AgentRunner(cwd=<session.cwd>).run(prompt, resume=<session.id>) — Claude
continues that chat with its full prior context, and the new prompt is the next turn.

Windows: the folder encoding is the Mac one unchanged (every non-alphanumeric → '-', so
"C:\\Users\\x\\Filect Assistant" → "C--Users-x-Filect-Assistant"), but folder names and the `cwd`
recorded inside a chat can differ in drive-letter case ("c:\\…" from VS Code, "C:\\…" from Filect), so
paths are compared with normcase.

Headless self-check:
    .venv\\Scripts\\python.exe app\\core\\agent_sessions.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)
IS_WIN = sys.platform.startswith("win")


def _projects_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path(os.path.expanduser("~")) / ".claude")
    return Path(base) / "projects"


def _norm(p: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(p or ""))
    except Exception:
        return p or ""


def encode_cwd(cwd: str) -> str:
    """Claude Code's folder name for a cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(cwd))


@dataclass
class SessionInfo:
    id: str          # session id (jsonl filename stem) -> pass as resume=
    cwd: str         # the folder the conversation ran in -> pass as cwd=
    title: str       # Claude Code's generated summary if present, else the first user line
    preview: str     # first user message, trimmed
    mtime: float     # last-modified epoch (recency / sort key)
    project: str     # basename of cwd, for grouping/display
    agent: str = "claude"   # "claude" (~/.claude/projects) or "codex" (~/.codex/sessions)

    @property
    def when(self) -> str:
        return time.strftime("%b %d, %H:%M", time.localtime(self.mtime))


def _extract_text(message) -> str:
    """Plain text from a user message whose content is a str OR a list of blocks."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(p for p in parts if p)
    return ""


def _user_words(message) -> str:
    """What you typed in a user message: its text blocks minus the ones the editor injects
    ("<ide_opened_file>…", "<command-name>…") and tool results. A chat started from VS Code has those
    first — without this its title was the injected tag."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    blocks = [content] if isinstance(content, str) else [
        b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else (b if isinstance(b, str) else "")
        for b in (content or [])]
    words = [b.strip() for b in blocks if b and b.strip() and not b.lstrip().startswith("<")
             and not b.startswith("This session is being continued")]
    return " ".join(words)


def _decode_cwd(dirname: str) -> str:
    # Fallback only: the folder name encodes the cwd with separators collapsed to '-', which is
    # lossy (can't tell a space from a slash). We prefer the explicit `cwd` field inside the file.
    if IS_WIN and re.match(r"^[A-Za-z]--", dirname):
        return dirname[0].upper() + ":\\" + dirname[3:].replace("-", "\\")
    return "/" + dirname.strip("-").replace("-", "/")


def strip_speak_note(text: str) -> str:
    """The hands-free read-aloud note Filect appends to a message isn't your words (Mac speech_out)."""
    try:
        from app.core.speech_out import strip_speak_note as _s     # (hands-free Assistant, when ported)
        return _s(text)
    except Exception:
        return text


def _custom_title(jsonl_path: Path) -> Optional[str]:
    """Claude Code keeps the sidebar title the user sees in a sibling file:
    <proj>/<session-id>/custom-title.json -> {"customTitle": "..."}."""
    tf = jsonl_path.parent / jsonl_path.stem / "custom-title.json"
    try:
        if tf.is_file():
            t = (json.loads(tf.read_text(encoding="utf-8")).get("customTitle") or "").strip()
            return t or None
    except Exception:
        return None
    return None


def _parse_session(path: Path) -> Optional[SessionInfo]:
    summary: Optional[str] = None
    first_user: Optional[str] = None
    cwd: Optional[str] = None
    had_content = False
    replied = api_error = False          # a real reply / only an API error ("Not logged in", out of usage)
    read_all = True
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if t == "summary" and not summary:
                    summary = (obj.get("summary") or "").strip()
                if cwd is None and obj.get("cwd"):
                    cwd = obj.get("cwd")
                if t in ("user", "assistant"):
                    had_content = True
                    if t == "user" and first_user is None and not obj.get("isMeta"):
                        txt = _user_words(obj.get("message"))
                        if txt:
                            first_user = txt
                    if t == "assistant":
                        api_error |= bool(obj.get("isApiErrorMessage"))
                        replied |= not obj.get("isApiErrorMessage")
                # Bounded read: a listing row needs a title source + cwd. Once we have a first user line +
                # cwd and have scanned a bit, stop — don't read megabytes of a long transcript per row.
                if (first_user and cwd) and (summary or lineno > 60):
                    read_all = False
                    break
    except Exception:
        return None
    if not had_content and not summary:
        return None
    if read_all and api_error and not replied:
        # Claude Code refused to run (signed out / out of usage) — this attempt isn't a conversation.
        return None
    preview = strip_speak_note(first_user or "").strip().replace("\n", " ")
    ct = _custom_title(path)
    title = (ct or summary or (preview[:60] if preview else path.stem[:8])).strip()
    if not cwd:
        cwd = _decode_cwd(path.parent.name)
    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = 0.0
    return SessionInfo(id=path.stem, cwd=cwd, title=title[:90],
                       preview=preview[:220], mtime=mtime, project=(Path(cwd).name or cwd))


def is_junk(s) -> bool:
    """A saved "session" that isn't a real conversation: the voice decider's own classification queries (older
    versions saved them), resumed-context stubs, and empty titles. Never offered, never counted."""
    tl = (getattr(s, "title", "") or "").strip().lower()
    return (not tl or tl.startswith("current conversation:") or "spoken command:" in tl
            or tl.startswith("this session is being continued"))


def _decider_folder(proj: Path) -> bool:
    """Claude Code's folder for the voice decider's own working folder (Filect's app data \\agent-router): every chat
    in it is a classification, never a conversation — skipped without even opening its files."""
    return proj.name.lower().endswith("-agent-router")


def list_sessions(limit: int = 100, cwd: Optional[str] = None, real_only: bool = False) -> List[SessionInfo]:
    """All local Claude Code sessions, newest first. Pass `cwd` to scope to one project folder. `real_only`: junk
    (is_junk, and the decider's own folder) is left out BEFORE the `limit` — so it never uses up the places real
    chats need (the Mac: 286 of the newest 500 were junk, which put 177 real chats out of voice's reach)."""
    root = _projects_root()
    if not root.exists():
        return []
    want = _norm(cwd) if cwd else None
    out: List[SessionInfo] = []
    try:
        projects = list(root.iterdir())
    except Exception:
        return []
    if want:
        # Fast path: read just that cwd's folder instead of parsing every session on disk (hundreds).
        # The cwd check below still applies (the encoding is lossy). Windows' file system is
        # case-insensitive, so "C--Users-…" finds a "c--Users-…" folder too.
        enc = root / encode_cwd(cwd)
        if enc.is_dir():
            projects = [enc]          # else a full scan: Claude Code shortens very long folder names
    for proj in projects:
        if not proj.is_dir() or (real_only and _decider_folder(proj)):
            continue
        try:
            files = list(proj.glob("*.jsonl"))
        except Exception:
            continue
        for f in files:
            info = _parse_session(f)
            if info is None:
                continue
            if want and _norm(info.cwd) != want:
                continue
            if real_only and is_junk(info):
                continue
            out.append(info)
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out[:limit]


# ---- chats the Assistant continued OUTSIDE its workspace ------------------------------------------
# New tasks run in the workspace folder, which the page lists. A chat CONTINUED elsewhere (a resumed
# project chat) lives in that project's folder, so after a restart nothing pointed to it and it
# dropped off the list. Remember those ids here (app data dir, never ~/.claude).
_TOUCHED_MAX = 200


def _touched_path() -> Path:
    from app.core.settings import settings
    return settings.get_app_data_dir() / "assistant_sessions.json"


def _read_touched() -> Optional[list]:
    """The remembered [{id, cwd}], or None if the file was never written (→ run the backfill)."""
    try:
        data = json.loads(_touched_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return None
    except Exception:
        return []


def _write_touched(items: list) -> None:
    try:
        _touched_path().write_text(json.dumps(items[:_TOUCHED_MAX]), encoding="utf-8")
    except Exception:
        pass


def remember_session(session_id: str, cwd: str) -> None:
    """Keep a chat continued outside the workspace on the list after restarts. Never raises."""
    if not session_id:
        return
    items = [e for e in (_read_touched() or []) if isinstance(e, dict) and e.get("id") != session_id]
    _write_touched([{"id": session_id, "cwd": cwd}] + items)


def _backfill(skip_dir: str) -> list:
    """One-time catch-up for chats continued before remember_session existed: outside the workspace,
    written to by the Python SDK (Filect) AND by Claude Code/the Claude app — so a real continued chat,
    not an intent-router or test leftover. Reads every transcript once (~1s per GB), off the UI thread."""
    out = []
    sdk_re = re.compile(rb'"entrypoint":\s*"sdk-py"')
    other_re = re.compile(rb'"entrypoint":\s*"(?:cli|claude-desktop|claude-vscode)"')
    for f in _projects_root().glob("*/*.jsonl"):
        if f.parent.name.lower() == skip_dir.lower():
            continue
        sdk = other = False
        try:
            with f.open("rb") as fh:
                for line in fh:
                    if b'"entrypoint"' not in line:     # cheap pre-check; most lines skip the regex
                        continue
                    if sdk_re.search(line):
                        sdk = True
                    elif other_re.search(line):
                        other = True
                    if sdk and other:
                        info = _parse_session(f)
                        if info:
                            out.append({"id": info.id, "cwd": info.cwd})
                        break
        except Exception:
            continue
    return out


def remembered_sessions(workspace: str) -> List[SessionInfo]:
    """The chats continued outside `workspace` that still exist on disk, newest first."""
    items = _read_touched()
    if items is None:                    # first run with this feature: find the ones from before it
        items = _backfill(encode_cwd(workspace))
        _write_touched(items)
    out = []
    for e in items:
        sid = e.get("id") if isinstance(e, dict) else None
        f = _session_file(sid, e.get("cwd")) if sid else None
        info = _parse_session(f) if f else None
        if info:
            out.append(info)
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def tool_result_text(content) -> str:
    """Plain text of a tool result's content (a str, or a list of text/image blocks)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif b.get("type") == "image":
                parts.append("[image]")
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(p for p in parts if p)


def _parse_ts(s) -> Optional[float]:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _session_file(session_id: str, cwd: Optional[str]) -> Optional[Path]:
    root = _projects_root()
    if not session_id or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):    # an id, never a path
        return None
    if cwd:                                      # same folder encoding as list_sessions' fast path
        p = root / encode_cwd(cwd) / f"{session_id}.jsonl"
        if p.exists():
            return p
    return next(iter(root.glob(f"*/{session_id}.jsonl")), None)


def last_message_id(session_id: str, cwd: Optional[str] = None) -> Optional[str]:
    """The uuid of the chat's last real message (user / assistant), read from the end of its file. A Claude Code
    kept running for a follow-up is only handed the request if this hasn't changed since it started — otherwise
    it would answer without what was said elsewhere (Mac, measured 2026-11-08: it did — "I don't know your favorite
    color"). Trailing bookkeeping lines (cost, last prompt…) don't count."""
    import json
    p = _session_file(session_id, cwd)
    if p is None:
        return None
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            for start in (max(0, size - 512 * 1024), 0):    # the tail first; the whole file if that's not enough
                f.seek(start)
                for line in reversed(f.read().splitlines()):
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") in ("user", "assistant") and d.get("uuid"):
                        return d["uuid"]
                if start == 0:
                    break
    except OSError:
        return None
    return None


def load_transcript(session_id: str, cwd: Optional[str] = None) -> Optional[dict]:
    """What happened in a saved conversation, for showing it: `steps` in order —
    ("user", text) what you said, ("text", text) Claude's replies, ("tool", name, input, id) its
    actions, ("result", tool_id, text, is_error) what each action returned — plus `duration`, the LAST
    turn's length in seconds (None if unknown). Thinking and injected stubs are skipped. Reads the
    whole file, so call it off the UI thread."""
    path = _session_file(session_id, cwd)
    if path is None:
        return None
    steps, turn_start, last_ts = [], None, None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if t not in ("user", "assistant") or obj.get("isSidechain"):   # sidechain = a subagent's own chat
                    continue
                ts = _parse_ts(obj.get("timestamp"))
                meta = bool(obj.get("isMeta"))       # injected notes ("[Image: original 390x3000…]")
                content = (obj.get("message") or {}).get("content")
                blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    text = (b.get("text") or "").strip() if bt == "text" else ""
                    if t == "user" and text and not meta and not text.startswith("<") \
                            and not text.startswith("This session is being continued"):
                        steps.append(("user", text))
                        turn_start = ts                  # duration = the last turn
                    elif t == "assistant" and text:
                        steps.append(("text", text))
                    elif t == "assistant" and bt == "tool_use":
                        steps.append(("tool", b.get("name") or "", b.get("input") or {}, b.get("id") or ""))
                    elif t == "user" and bt == "tool_result":
                        steps.append(("result", b.get("tool_use_id") or "",
                                      tool_result_text(b.get("content")), bool(b.get("is_error"))))
                if ts:
                    last_ts = ts
    except Exception:
        return None
    dur = last_ts - turn_start if (turn_start and last_ts and last_ts >= turn_start) else None
    return {"steps": steps, "duration": dur}


def find_by_name(query: str, limit: int = 5) -> List[SessionInfo]:
    """Fuzzy-ish lookup for 'use my <name> conversation' — case-insensitive substring over the
    title/preview, newest first."""
    q = (query or "").strip().lower()
    if not q:
        return []
    hits = [s for s in list_sessions(limit=500, real_only=True)
            if q in s.title.lower() or q in s.preview.lower()]
    return hits[:limit]


# Politeness / glue words to drop so a spoken prompt collapses to its content words. Kept small and
# generic — it just has to make "hi go build me a small one page website" read as "Build Small One
# Page Website". ponytail: a word set + Title Case beats a cloud call for a cosmetic label.
_TITLE_FILLERS = {
    "a", "an", "the", "to", "for", "of", "and", "or", "my", "me", "i", "you", "your", "we", "us",
    "it", "that", "this", "there", "hi", "hey", "hello", "ok", "okay", "so", "um", "uh", "please",
    "pls", "can", "could", "would", "should", "now", "just", "go", "let", "lets", "want", "wanna",
    "gonna", "need", "like", "then", "actually", "basically", "really", "kinda", "maybe", "some",
    "be", "is", "are", "am", "do", "does", "get", "got", "with", "on", "in", "into", "up", "all",
    "them", "their", "its",
}


def generate_title(prompt: str, max_words: int = 5) -> str:
    """A short title for a task, built LOCALLY from its prompt — no AI, no network, instant. Takes the
    first sentence, drops filler/politeness words, keeps the content words and Title-Cases them
    (preserving acronyms like TODO/API/CSV). Always non-empty for a non-empty prompt.

    "hi go build me a small one page website" -> "Build Small One Page Website".
    """
    text = re.sub(r"\[screenshot \d+\]", " ", prompt or "").strip()   # receive-mode markers
    if not text:
        return ""
    head = re.split(r"[.!?\n]", text, 1)[0]
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'+#.-]*", head)
    content = [w for w in words if w.lower() not in _TITLE_FILLERS]
    picked = (content or words)[:max_words]
    if not picked:
        return text[:46].rstrip()
    title = " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in picked)
    return title[:48].rstrip()


# --- headless self-check -------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "projects" / "C--Users-me-Demo"
        proj.mkdir(parents=True)
        sess = proj / "abc123.jsonl"
        sess.write_text(
            json.dumps({"type": "summary", "summary": "Sponsor inquiry email drafts"}) + "\n"
            + json.dumps({"type": "user", "cwd": "C:\\Users\\me\\Demo",
                          "message": {"role": "user",
                                      "content": [{"type": "text", "text": "Draft a reply to the brand"}]}}) + "\n"
            + json.dumps({"type": "assistant", "cwd": "C:\\Users\\me\\Demo",
                          "message": {"role": "assistant", "content": "Sure"}}) + "\n",
            encoding="utf-8")
        info = _parse_session(sess)
        assert info is not None
        assert info.id == "abc123", info.id
        assert info.title == "Sponsor inquiry email drafts", info.title
        assert info.cwd == "C:\\Users\\me\\Demo", info.cwd
        assert "Draft a reply" in info.preview, info.preview
        assert info.project == "Demo", info.project
        s2 = proj / "def456.jsonl"
        s2.write_text(json.dumps({"type": "user", "cwd": "C:\\Users\\me\\Demo",
                                  "message": {"role": "user", "content": "Fix the login bug"}}) + "\n",
                      encoding="utf-8")
        i2 = _parse_session(s2)
        assert i2 is not None and i2.title == "Fix the login bug", i2.title
        s3 = proj / "empty.jsonl"
        s3.write_text("\n\nnot json\n", encoding="utf-8")
        assert _parse_session(s3) is None
        # a refused attempt (signed out) is not a chat
        s4 = proj / "refused.jsonl"
        s4.write_text(json.dumps({"type": "user", "cwd": "C:\\x", "message": {"content": "hi"}}) + "\n"
                      + json.dumps({"type": "assistant", "isApiErrorMessage": True,
                                    "message": {"content": "Not logged in"}}) + "\n", encoding="utf-8")
        assert _parse_session(s4) is None
        # a VS Code chat: the injected editor tag isn't the title
        s5 = proj / "vscode.jsonl"
        s5.write_text(json.dumps({"type": "user", "cwd": "C:\\x", "message": {"content": [
            {"type": "text", "text": "<ide_opened_file>The user opened x.py</ide_opened_file>"},
            {"type": "text", "text": "Rename the helper"}]}}) + "\n", encoding="utf-8")
        assert _parse_session(s5).title == "Rename the helper", _parse_session(s5).title
        os.environ["CLAUDE_CONFIG_DIR"] = d
        got = list_sessions(cwd="c:\\users\\ME\\Demo")         # case-insensitive on Windows
        assert {s.id for s in got} == {"abc123", "def456"}, got
        del os.environ["CLAUDE_CONFIG_DIR"]
    print("agent_sessions parser self-check OK")
    assert generate_title("") == ""
    assert generate_title("hi go build me a small one page website for filect") == "Build Small One Page Website"
    assert generate_title("find all the TODO comments in my project and list them") == "Find TODO Comments Project List"
    assert len(generate_title("x" * 200)) <= 48
    print("generate_title (local) OK")
    real = list_sessions(limit=8)
    print(f"\nFound {len(list_sessions(limit=10000))} local Claude Code sessions. Most recent:")
    for s in real:
        print(f"  [{s.when}]  {s.project:<22.22}  {s.title}")
