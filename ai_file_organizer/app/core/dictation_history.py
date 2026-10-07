"""Persistent store for recent dictation transcripts.

Entries are plain dicts: {"id": str, "text": str, "timestamp": str(ISO-8601)}.
The Voice History card reads these via get_all()/clear()/delete(); the dictation
controller appends new transcripts via add(). Stored as a JSON array in the app
data dir, newest first, capped at the most recent MAX_ENTRIES. All file IO is
wrapped so a missing/corrupt file never crashes the UI.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from app.core.settings import settings

MAX_ENTRIES = 200


def _store_path():
    return settings.get_app_data_dir() / "dictation_history.json"


def _load() -> list[dict]:
    """Read the raw list from disk (newest first). Never raises."""
    try:
        path = _store_path()
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        # Keep only well-formed entries.
        out = []
        for e in data:
            if isinstance(e, dict) and "text" in e:
                out.append({
                    "id": str(e.get("id") or uuid.uuid4().hex),
                    "text": str(e.get("text", "")),
                    "timestamp": str(e.get("timestamp", "")),
                })
        return out
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    """Write the list to disk. Never raises."""
    try:
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def add(text: str) -> None:
    """Append a new transcript as the newest entry and persist (capped)."""
    text = (text or "").strip()
    if not text:
        return
    entries = _load()
    entry = {
        "id": uuid.uuid4().hex,
        "text": text,
        "timestamp": datetime.now().isoformat(),
    }
    # Newest first.
    entries.insert(0, entry)
    if len(entries) > MAX_ENTRIES:
        entries = entries[:MAX_ENTRIES]
    _save(entries)


def get_all() -> list[dict]:
    """Return all entries, newest first. Each is {id, text, timestamp}."""
    return _load()


def clear() -> None:
    """Remove all stored history."""
    _save([])


def delete(entry_id) -> None:
    """Remove the entry with the given id (no-op if not found)."""
    entries = _load()
    entries = [e for e in entries if e.get("id") != entry_id]
    _save(entries)
