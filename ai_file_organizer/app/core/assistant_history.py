"""What the voice assistant did, for the History tab on the Assistant page: one row per conversation or job.

Local only, never uploaded (`<app data>/assistant_history.json`). Everything is called from the UI thread; the agent
runners only emit a signal from their worker threads. File problems are swallowed: history must never break the
assistant. Only real work is written - room talk, cancels, failures, yes/no answers never reach this file.

Ported from the Mac (assistant_history.py, v14.15), with Windows hardening:
  * antivirus / OneDrive can hold the file for a moment: the replace is retried, and a file that can't be READ right
    now is never overwritten (its rows would be lost) - that write is skipped instead;
  * a damaged file is set aside under a new name each time (a second one never overwrites the first);
  * the test suite never writes the real file (offscreen Qt OR pytest - two Windows test files drive the organize /
    find controllers without the offscreen platform).
"""
import json
import os
import sys
import time
import uuid

from app.core.settings import settings

_MAX = 500
_SAID_MAX = 300
_TITLE_MAX = 120

_live: dict = {}          # this run's chat key ("s3") -> its row id. Keys restart at each launch, so they stay in memory
_listeners: list = []


class _Unreadable(Exception):
    """The file is there but can't be read right now: don't write over it."""


def _saving() -> bool:
    """Tests and off-screen renders never write your real history."""
    return os.environ.get("QT_QPA_PLATFORM") != "offscreen" and "pytest" not in sys.modules


def _path():
    return settings.get_app_data_dir() / "assistant_history.json"


def _valid(e) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("ts"), (int, float))
            and not isinstance(e.get("ts"), bool))


def _set_aside(p) -> None:
    """A file that can't be understood is kept (renamed), never overwritten - each one under its own name."""
    for n in range(100):
        name = "assistant_history.corrupt.json" if n == 0 else f"assistant_history.corrupt-{n}.json"
        q = p.with_name(name)
        if not q.exists():
            try:
                os.replace(p, q)
            except OSError:
                pass
            return


def _read(strict: bool = False) -> list:
    """The saved rows. `strict` (before a write): a file that exists but can't be read raises _Unreadable."""
    p = _path()
    for attempt in range(3):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            break
        except FileNotFoundError:
            return []
        except PermissionError:
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))       # (antivirus / OneDrive, for a moment)
                continue
            if strict:
                raise _Unreadable()
            return []
        except OSError:
            if strict:
                raise _Unreadable()
            return []
        except Exception:
            _set_aside(p)
            return []
    return [e for e in data if _valid(e)] if isinstance(data, list) else []


def _write(items: list) -> None:
    """All or nothing: a crash half way leaves the old file. Never raises."""
    if not _saving():
        return
    items.sort(key=lambda e: e["ts"], reverse=True)
    p = _path()
    tmp = p.with_name(f"assistant_history.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items[:_MAX], f)             # (ascii: a broken character from speech can't fail the write)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:                # (Windows: the file is held for a moment)
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    changed()


def _clean(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def changed() -> None:
    """Tell the tab to redraw (a row was written, or something it reads from elsewhere changed)."""
    for cb in list(_listeners):
        try:
            cb()
        except Exception:
            pass


def subscribe(callback) -> None:
    if callback not in _listeners:
        _listeners.append(callback)


def get_all() -> list:
    """Every row, newest first. Never raises."""
    try:
        return sorted(_read(), key=lambda e: e["ts"], reverse=True)[:_MAX]
    except Exception:
        return []


def add(kind: str, said: str, title: str, status: str = "", log: str = "") -> str:
    """A finished job Filect did itself: kind = "organize" | "auto" | "find". Returns the row's id. Never raises."""
    rid = uuid.uuid4().hex
    try:
        items = _read(strict=True)
        items.append({"id": rid, "ts": time.time(), "kind": kind, "agent": "filect", "said": _clean(said, _SAID_MAX),
                      "title": _clean(title, _TITLE_MAX), "status": _clean(status, _TITLE_MAX), "log": str(log or "")})
        _write(items)
    except Exception:
        pass
    return rid


def update(row_id: str, **fields) -> None:
    """Change a row without moving it. Never raises."""
    try:
        items = _read(strict=True)
        for e in items:
            if e["id"] == row_id:
                for k, v in fields.items():
                    e[k] = _clean(v, _SAID_MAX if k == "said" else _TITLE_MAX) if k in ("said", "title", "status") else v
                _write(items)
                return
    except Exception:
        pass


def chat(key: str, agent: str, session_id: str, cwd: str, title: str, said: str) -> None:
    """A voice request went to a chat: its row comes to the top, or a new one is made. One row per conversation,
    so "add a footer" after "make a landing page" isn't a second row. Never raises."""
    try:
        items = _read(strict=True)
        row = next((e for e in items if session_id and e.get("session_id") == session_id), None)
        if row is None and key in _live:
            row = next((e for e in items if e["id"] == _live[key]), None)
        if row is None:
            row = {"id": uuid.uuid4().hex, "kind": "chat", "session_id": ""}
            items.append(row)
        row.update(ts=time.time(), agent=agent, cwd=cwd or row.get("cwd", ""), said=_clean(said, _SAID_MAX),
                   title=_clean(title, _TITLE_MAX) or row.get("title", ""))
        if session_id:
            row["session_id"] = session_id
        _live[key] = row["id"]
        _write(items)
    except Exception:
        pass


def set_session(key: str, session_id: str, agent: str, cwd: str = "") -> None:
    """The chat's real id and agent (it can change: a hand-over from Claude to Codex). Never raises."""
    if not session_id or key not in _live:
        return
    try:
        items = _read(strict=True)
        row = next((e for e in items if e["id"] == _live[key]), None)
        if row is None or (row.get("session_id") == session_id and row.get("agent") == agent):
            return
        row.update(session_id=session_id, agent=agent, cwd=cwd or row.get("cwd", ""))
        _write([e for e in items if e is row or e.get("session_id") != session_id])   # (two rows, one chat: merged)
    except Exception:
        pass


def forget(key: str) -> None:
    """The chat never got an answer (it failed, or was stopped at once): nothing worth opening, so no row."""
    try:
        rid = _live.pop(key, None)
        if rid is not None:
            _write([e for e in _read(strict=True) if e["id"] != rid])
    except Exception:
        pass
