"""The Assistant's History tab: the saved rows, and which saved chats a voice request can reach.

Ported from the Mac (tests/test_assistant_history.py, 11 tests) plus the Windows hardening: a file that can't be read
right now is never overwritten, damaged files are set aside under their own names, and the real file is never
written from a test. Only temp folders; settings.json is never touched.
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from app.core import agent_sessions  # noqa: E402
from app.core import assistant_history as hist  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """A throwaway app-data folder; writes are on ONLY here (the store refuses under pytest otherwise)."""
    monkeypatch.setattr(type(settings), "_save_config", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(type(settings), "get_app_data_dir", lambda self: tmp_path)
    monkeypatch.setattr(hist, "_saving", lambda: True)
    hist._live.clear()
    yield tmp_path
    hist._live.clear()


def test_the_real_file_is_never_written_from_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(type(settings), "get_app_data_dir", lambda self: tmp_path)
    hist.add("find", "x", "y")
    assert not (tmp_path / "assistant_history.json").exists()


def test_one_row_per_conversation(tmp_data):
    hist.chat("s1", "claude", "", r"C:\w", "Landing", "make a landing page")
    hist.set_session("s1", "A", "claude", r"C:\w")
    hist.chat("s1", "claude", "A", r"C:\w", "Landing", "add a footer")
    rows = hist.get_all()
    assert len(rows) == 1 and rows[0]["said"] == "add a footer" and rows[0]["session_id"] == "A", rows


def test_keys_restart_but_rows_dont_mix(tmp_data):
    hist.chat("s1", "claude", "A", r"C:\w", "Old", "first")
    hist._live.clear()                                    # (Filect restarted: keys count from s1 again)
    hist.chat("s1", "codex", "", r"C:\w", "New", "second")
    assert len(hist.get_all()) == 2, hist.get_all()
    hist.chat("s7", "claude", "A", r"C:\w", "Old", "back to it")
    rows = hist.get_all()
    assert len(rows) == 2 and rows[0]["said"] == "back to it", rows


def test_hand_over_changes_the_agent(tmp_data):
    hist.chat("s1", "claude", "", r"C:\w", "T", "x")
    hist.set_session("s1", "X", "codex", r"C:\w")
    row = hist.get_all()[0]
    assert (row["agent"], row["session_id"]) == ("codex", "X"), row


def test_two_rows_for_one_chat_are_merged(tmp_data):
    hist.chat("s1", "claude", "A", r"C:\w", "Old", "first")
    hist.chat("s2", "claude", "", r"C:\w", "New", "second")        # (its id isn't known yet)
    hist.set_session("s2", "A", "claude", r"C:\w")                 # ... and it turns out to be the same chat
    rows = hist.get_all()
    assert len(rows) == 1 and rows[0]["said"] == "second", rows


def test_never_answered_leaves_no_row(tmp_data):
    hist.chat("s1", "claude", "", r"C:\w", "T", "x")
    hist.forget("s1")
    assert hist.get_all() == []


def test_a_damaged_file_is_set_aside_not_overwritten(tmp_data):
    (tmp_data / "assistant_history.json").write_bytes(b"\x00nope")
    assert hist.get_all() == []
    hist.add("find", "x", "y")
    assert (tmp_data / "assistant_history.corrupt.json").read_bytes() == b"\x00nope"
    assert len(hist.get_all()) == 1


def test_a_second_damaged_file_doesnt_replace_the_first(tmp_data):
    (tmp_data / "assistant_history.json").write_bytes(b"\x00one")
    hist.get_all()
    (tmp_data / "assistant_history.json").write_bytes(b"\x00two")
    hist.get_all()
    assert (tmp_data / "assistant_history.corrupt.json").read_bytes() == b"\x00one"
    assert (tmp_data / "assistant_history.corrupt-1.json").read_bytes() == b"\x00two"


def test_a_failed_save_keeps_the_old_file(tmp_data, monkeypatch):
    hist.add("find", "a", "A")
    before = (tmp_data / "assistant_history.json").read_bytes()

    def full(fd):
        raise OSError(28, "disk full")
    with monkeypatch.context() as m:
        m.setattr(os, "fsync", full)
        hist.add("find", "b", "B")
    assert (tmp_data / "assistant_history.json").read_bytes() == before
    assert not list(tmp_data.glob("*.tmp"))


def test_a_file_held_by_another_program_is_not_overwritten(tmp_data, monkeypatch):
    """Windows: antivirus / OneDrive holds the file -> reading it fails -> the write is SKIPPED (its rows stay)."""
    hist.add("find", "a", "A")
    before = (tmp_data / "assistant_history.json").read_bytes()
    import builtins
    real_open = builtins.open

    def held(p, *a, **k):
        if str(p).endswith("assistant_history.json") and (not a or "r" in a[0]):
            raise PermissionError(13, "held")
        return real_open(p, *a, **k)
    with monkeypatch.context() as m:
        m.setattr(builtins, "open", held)
        m.setattr(hist.time, "sleep", lambda s: None)
        hist.add("find", "b", "B")
    assert (tmp_data / "assistant_history.json").read_bytes() == before
    assert [r["title"] for r in hist.get_all()] == ["A"]


def test_a_replace_blocked_for_a_moment_still_saves(tmp_data, monkeypatch):
    real_replace = os.replace
    tries = []

    def flaky(a, b):
        tries.append(1)
        if len(tries) < 3:
            raise PermissionError(13, "held")
        return real_replace(a, b)
    with monkeypatch.context() as m:
        m.setattr(hist.os, "replace", flaky)
        m.setattr(hist.time, "sleep", lambda s: None)
        hist.add("find", "a", "A")
    assert [r["title"] for r in hist.get_all()] == ["A"] and len(tries) == 3


def test_capped(tmp_data):
    (tmp_data / "assistant_history.json").write_text(json.dumps([{"id": str(i), "ts": i} for i in range(900)]))
    hist.add("find", "x", "newest")
    rows = hist.get_all()
    assert len(rows) == 500 and rows[0]["title"] == "newest", len(rows)


def test_bad_rows_are_skipped(tmp_data):
    (tmp_data / "assistant_history.json").write_text(json.dumps(
        [{"id": "a", "ts": 5}, {"id": 3, "ts": 1}, {"id": "b"}, {"id": "c", "ts": True}, "x", {"id": "d", "ts": 9}]))
    assert [r["id"] for r in hist.get_all()] == ["d", "a"]


def test_listeners_hear_each_write(tmp_data):
    heard = []
    hist.subscribe(lambda: heard.append(1))
    try:
        hist.add("find", "x", "y")
        hist.update(hist.get_all()[0]["id"], status="Opened x")
        assert len(heard) == 2 and hist.get_all()[0]["status"] == "Opened x"
    finally:
        hist._listeners.clear()


# ---- which saved chats a voice request can reach ---------------------------------------------------------------
def _chat(i, title, preview="", age=0):
    return SimpleNamespace(id=f"c{i}", cwd=r"C:\w", title=title, preview=preview, mtime=1000.0 - i - age,
                           agent="claude")


def _candidates(text, saved):
    page = SimpleNamespace(_sessions={}, _recent=[], _all_sessions=saved,
                           _junk_session=ap.AssistantPage._junk_session, _display_title=lambda r: r.title)
    return [c["id"] for c in ap.AssistantPage._build_voice_candidates(page, text)]


OLD = [_chat(i, f"Unrelated topic number {i}") for i in range(40)]


def test_one_big_word_reaches_an_old_chat():
    target = _chat(99, "Zanzibar budget notes", age=500)
    assert "c99" in _candidates("Continue the Zanzibar chat", OLD + [target])


def test_a_small_word_alone_does_not():
    target = _chat(99, "Tax notes", age=500)
    assert "c99" not in _candidates("Continue the tax chat", OLD + [target])


def test_a_word_only_in_the_first_message_does_not():
    target = _chat(99, "Budget notes", preview="a long message that mentions Zanzibar in passing", age=500)
    assert "c99" not in _candidates("Continue the Zanzibar chat", OLD + [target])


def test_junk_doesnt_use_up_the_limit(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    for i in range(30):
        (root / f"{i}.jsonl").write_text("x")
    monkeypatch.setattr(agent_sessions, "_parse_session", lambda p: SimpleNamespace(
        title=("Spoken command: x" if int(p.stem) < 20 else f"Real {p.stem}"), mtime=1000.0 - int(p.stem),
        id=p.stem, cwd=r"C:\w"))
    monkeypatch.setattr(agent_sessions, "_projects_root", lambda: tmp_path / "projects")
    assert len(agent_sessions.list_sessions(limit=5)) == 5
    assert all(s.title.startswith("Spoken") for s in agent_sessions.list_sessions(limit=5))
    got = agent_sessions.list_sessions(limit=5, real_only=True)
    assert len(got) == 5 and all(s.title.startswith("Real") for s in got), got


# ---- the tab -------------------------------------------------------------------------------------------------
def _view(rows, monkeypatch):
    from app.ui import assistant_history_view as hv
    from app.ui.live_page import _colors
    monkeypatch.setattr(hist, "get_all", lambda: rows)
    v = hv.HistoryView(SimpleNamespace(), _colors("dark"))
    v.resize(700, 600)
    v.show()
    return v


def test_the_tab_shows_one_line_per_row_and_the_right_button(monkeypatch):
    now = __import__("time").time()
    hist._live.clear()
    rows = [{"id": "r1", "ts": now, "kind": "chat", "agent": "claude", "session_id": "A", "cwd": r"C:\w",
             "said": "make a landing page", "title": "Landing page"},
            {"id": "r2", "ts": now - 60, "kind": "chat", "agent": "codex", "session_id": "", "cwd": r"C:\w",
             "said": "fix it", "title": "Fix"},
            {"id": "r3", "ts": now - 120, "kind": "find", "agent": "filect", "said": "find my tax pdf",
             "title": "Looked for a PDF", "status": "Opened tax.pdf"}]
    v = _view(rows, monkeypatch)
    v.refresh()
    from PySide6.QtWidgets import QPushButton, QFrame
    frames = [f for f in v.findChildren(QFrame) if f.property("history_id")]
    assert sorted(f.property("history_id") for f in frames) == ["r1", "r2", "r3"]
    texts = {f.property("history_id"): [b.text() for b in f.findChildren(QPushButton)] for f in frames}
    assert any(t.startswith("Open in Claude") for t in texts["r1"]), texts
    assert any("Not saved" in t for t in texts["r2"]), texts          # (no id and not running now)
    hist._live["s9"] = "r2"
    try:
        v.refresh()
        app.processEvents()
        frames = [f for f in v.findChildren(QFrame) if f.property("history_id") == "r2"]
        assert frames and any("Starting" in b.text() for f in frames for b in f.findChildren(QPushButton)), \
            [b.text() for f in frames for b in f.findChildren(QPushButton)]
    finally:
        hist._live.clear()
