"""The Agents page view (agents_view.AgentsView), headless. What the user sees for a given state:
empty state, a conversation (bubble · folded steps · answer · file chips), the tails (approval card with the
Full-access nudge, setup card, working, stopped), the chat list (groups, search incl. other chats, rows reused,
collapsed by default), the composer (Enter sends, ↑ ↔ ■), the tour, dark mode — and the Qt trap from the
Mac: every button carries its OWN padding (the app's global QSS pads all buttons 10px 20px → blank icons)."""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

app = QApplication.instance() or QApplication([])

from app.ui import agents_view as av          # noqa: E402
from app.core import agent_runner as ar       # noqa: E402


def _texts(w):
    return " | ".join(l.text() for l in w.findChildren(QLabel) if l.isVisibleTo(w)) + " | " + \
        " | ".join(b.text() for b in w.findChildren(QPushButton) if b.isVisibleTo(w))


@pytest.fixture
def view():
    host = QWidget()
    host.resize(1180, 760)
    v = av.AgentsView("light", host)
    v.resize(1180, 760)
    host.show()
    app.processEvents()
    yield v
    host.close()


def _entries(tmp_path, with_file=True):
    f = tmp_path / "notes.txt"
    if with_file:
        f.write_text("hello", encoding="utf-8")
    return [
        {"id": "0", "kind": "user", "text": "Make a notes file and list my folder"},
        {"id": "1", "kind": "text", "text": "I'll create the file first."},
        {"id": "2", "kind": "tool", "tool": "Write", "input": {"file_path": str(f), "content": "hello"},
         "tool_id": "w1", "result": {"text": "ok", "error": False}},
        {"id": "3", "kind": "tool", "tool": "Bash", "input": {"command": "dir", "description": "List the folder"},
         "tool_id": "b1", "result": {"text": "notes.txt", "error": False}},
        {"id": "4", "kind": "tool", "tool": "Read", "input": {"file_path": str(f)}, "tool_id": "r1",
         "result": {"text": "hello", "error": False}},
        {"id": "5", "kind": "text", "text": "Done — **notes.txt** is ready:\n\n- one\n- two"},
    ]


def test_rounded_buttons_fit_their_radius(view, tmp_path):
    """Qt draws a button SQUARE when its border-radius is more than half its height (seen live)."""
    import re
    view.show_chat({"key": "s1", "entries": _entries(tmp_path), "status": "needs_you", "running": True,
                    "approval": ar.ApprovalRequest("Write", {"file_path": r"C:\x\a.txt"}), "bypass": False})
    view.set_list_collapsed(False)
    view.set_chats([{"id": "s:s1", "title": "Notes", "status": "working", "agent": "claude", "ts": time.time(),
                     "focused": True}])
    app.processEvents()
    bad = []
    for b in view.findChildren(QPushButton):
        m = re.search(r"#%s\s*\{[^}]*border-radius:\s*(\d+)px" % re.escape(b.objectName() or "~"), b.styleSheet() or "")
        if m and b.isVisibleTo(view) and int(m.group(1)) * 2 > b.height():
            bad.append((b.objectName(), b.height(), int(m.group(1))))
    assert not bad, f"radius > half height → drawn square: {bad}"


def test_every_button_has_its_own_padding(view, tmp_path):
    view.show_chat({"key": "s1", "entries": _entries(tmp_path), "status": "needs_you", "running": True,
                    "approval": ar.ApprovalRequest("Write", {"file_path": "C:\\x\\a.txt"}), "bypass": False})
    view.set_chats([{"id": "s:s1", "title": "Notes", "status": "working", "agent": "claude", "ts": time.time(),
                     "focused": True}])
    view.set_list_collapsed(False)
    app.processEvents()
    missing = [b.objectName() or b.text() for b in view.findChildren(QPushButton)
               if "padding" not in (b.styleSheet() or "")]
    assert not missing, f"buttons without their own padding (would show blank): {missing}"


def test_empty_state_and_idea_chip(view):
    view.show_empty()
    app.processEvents()
    assert view.stack.currentWidget() is view.empty
    t = _texts(view.empty)
    assert "What should I do?" in t and len(view.empty.chips) == 3
    view.empty.chips[0].click()
    assert "biggest files" in view.input_text()


def test_conversation_folds_steps_and_shows_answer_and_chip(view, tmp_path):
    view.show_chat({"key": "s1", "entries": _entries(tmp_path), "status": "done", "running": False,
                    "approval": None, "bypass": True})
    app.processEvents()
    assert view.stack.currentWidget() is view.chat
    folds = [b for b in view.chat.findChildren(QPushButton) if b.objectName() == "foldBtn"]
    assert folds and "3 steps · created 1 file, ran 1 command" in folds[0].text(), [b.text() for b in folds]
    browsers = view.chat.findChildren(av._AutoBrowser)
    reply = " ".join(b.toPlainText() for b in browsers)
    assert "notes.txt is ready" in reply and "**" not in reply, reply
    assert "I'll create the file first" not in reply, "an in-between line leaked into the answer"
    chips = [b for b in view.chat.findChildren(QPushButton) if b.objectName() == "fileChip"]
    assert chips and "notes.txt" in chips[0].text()
    folds[0].click()                                  # open the fold → the rich Details
    app.processEvents()
    details = " ".join(b.toPlainText() for b in view.chat.findChildren(av._AutoBrowser))
    assert "List the folder" in details and "Wrote" in details, details[:300]


def test_bubble_is_not_a_narrow_column(view, tmp_path):
    view.show_chat({"key": "s9", "entries": [{"id": "0", "kind": "user", "text": "Please build me a website about coffee"}],
                    "status": "working", "running": True, "approval": None})
    app.processEvents()
    bubble = view.chat.findChildren(av._Bubble)[0]
    assert bubble.width() > 200, bubble.width()


def test_approval_card_and_nudge(view, tmp_path):
    st = {"key": "s1", "entries": _entries(tmp_path)[:1], "status": "needs_you", "running": True,
          "approval": ar.ApprovalRequest("Bash", {"command": "rm -r build", "description": "Delete the build folder"}),
          "bypass": False}
    view.show_chat(st)
    app.processEvents()
    t = _texts(view.chat)
    assert "NEEDS YOUR OK" in t and "Delete the build folder" in t and "rm -r build" in t
    assert "Approve && don't ask again" in t and "Approve" in t and "Deny" in t   # (&& shows one "&")
    got = []
    view.approve.connect(got.append)
    view.approve_always.connect(lambda: got.append("always"))
    for b in view.chat.findChildren(QPushButton):
        if b.objectName() == "denyBtn":
            b.click()
        if b.objectName() == "alwaysBtn":
            b.click()
    assert got == [False, "always"], got
    view.show_chat(dict(st, bypass=True))
    app.processEvents()
    assert "don't ask again" not in _texts(view.chat), "the nudge shows on Full access"


def test_setup_card_and_working_tail_clears(view, tmp_path):
    view.show_chat({"key": "s1", "entries": _entries(tmp_path)[:1], "status": "error", "running": False,
                    "approval": None, "error": ar.SETUP_SIGNIN, "setup_kind": "signin"})
    app.processEvents()
    t = _texts(view.chat)
    assert "SIGN-IN NEEDED" in t and "Sign in to Claude Code →" in t
    asked = []
    view.setup_requested.connect(lambda a, k: asked.append((a, k)))
    [b for b in view.chat.findChildren(QPushButton) if b.objectName() == "setupBtn"][0].click()
    assert asked == [("claude", "signin")]
    base = {"key": "s2", "entries": _entries(tmp_path)[:1], "approval": None, "last_action": "Running dir"}
    view.show_chat(dict(base, status="working", running=True))
    app.processEvents()
    assert "Working · Running dir" in _texts(view.chat)
    view.show_chat(dict(base, status="done", running=False))      # same entries, just not running any more
    app.processEvents()
    assert "Working" not in _texts(view.chat), "a stale 'Working…' stayed after the turn ended"
    view.show_chat(dict(base, status="stopped", running=False))
    app.processEvents()
    assert "Stopped by you" in _texts(view.chat)


def test_chat_list_groups_search_and_reuse(view):
    now = time.time()
    rows = [
        {"id": "s:s1", "title": "Build Coffee Website", "status": "working", "agent": "claude", "ts": now, "focused": True},
        {"id": "r:a", "title": "Find Tax Docs", "status": "done", "agent": "claude", "ts": now - 60, "preview": "tax pdfs"},
        {"id": "r:b", "title": "Old Thing", "status": "done", "agent": "claude", "ts": now - 86400 * 9},
    ]
    other = [{"id": "r:z", "title": "Port the Mac app", "status": "done", "agent": "claude", "ts": now - 5000,
              "sub": "App", "preview": "subscription billing"}]
    view.set_list_collapsed(False)
    view.set_chats(rows, other)
    app.processEvents()
    lst = view.list
    vis = [w for w in lst._order]
    heads = [w.text() for w in vis if isinstance(w, QLabel)]
    assert heads == ["Today", "Earlier"], heads
    assert [w.rid for w in vis if isinstance(w, av._ChatRow)][:2] == ["s:s1", "r:a"], "running chat sits in Today"
    first_row = lst._rows["r:a"]
    view.set_chats(rows, other)                       # same data → nothing rebuilt
    assert lst._rows["r:a"] is first_row
    lst.search_box.setText("tax")
    app.processEvents()
    shown = [w.rid for w in lst._order if isinstance(w, av._ChatRow)]
    assert shown == ["r:a"], shown
    lst.search_box.setText("billing")                 # reaches the user's other Claude Code chats
    app.processEvents()
    assert any(isinstance(w, QLabel) and w.text() == "Other Claude Code & Codex chats" for w in lst._order)
    assert [w.rid for w in lst._order if isinstance(w, av._ChatRow)] == ["r:z"]
    picked = []
    view.chat_picked.connect(picked.append)
    lst._rows["r:z"].clicked.emit("r:z")
    assert picked == ["r:z"]


def test_list_hidden_for_new_user_and_toggle(view):
    assert not view.list.isVisible(), "the chat list must start hidden"
    seen = []
    view.list_collapsed_changed.connect(seen.append)
    view.show_list_btn.click()
    app.processEvents()
    assert view.list.isVisible() and seen == [False]
    view.list.hide_btn.click()
    app.processEvents()
    assert not view.list.isVisible() and seen == [False, True]


def test_composer_enter_sends_shift_enter_newline_and_stop(view):
    sent, stops = [], []
    view.send_requested.connect(sent.append)
    view.stop_requested.connect(lambda: stops.append(1))
    inp = view.composer.input
    inp.setFocus()
    inp.setPlainText("line one")
    inp.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.ShiftModifier, "\r"))
    assert "\n" in inp.toPlainText() or inp.document().blockCount() == 2
    inp.setPlainText("do it")
    inp.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))
    assert sent == ["do it"]
    view.set_running(True)
    inp.setPlainText("")
    inp.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))
    assert stops == [1], "empty Enter while it works = Stop"
    assert view.composer.send_btn.toolTip() == "Stop"
    view.set_running(False)
    assert view.composer.send_btn.toolTip().startswith("Send")


def test_models_pill_and_effort_fallback(view):
    models = [{"value": "opus", "name": "Opus 5.5", "efforts": ["low", "high", "max"]},
              {"value": "haiku", "name": "Haiku 5.5", "efforts": []},
              {"value": "claude-opus-4-8", "name": "Opus 4.8", "efforts": ["low", "high"]}]
    view.set_models(models, "opus", "max")
    assert "Opus 5.5 · Max" in view.composer.agent_pill.text()
    assert "Claude Code · Opus 5.5" in view.chip.text()
    got = []
    view.model_changed.connect(lambda m, e: got.append((m, e)))
    view._pick_model("haiku")                          # Haiku has no thinking levels → Auto
    assert got[-1] == ("haiku", "") and "·" not in view.composer.agent_pill.text().strip().split("Haiku 5.5")[-1]
    view.set_access(False)
    assert view.composer.access_pill.text() == "Ask first"
    view.set_access(True)
    assert view.composer.access_pill.text() == "Full access"


def test_tour_opens_steps_and_esc_closes(view):
    """"See what it can do" opens this page's tour (page_tours "agents", the first-run guide's panel); Esc closes it
    and takes its dim with it."""
    top = view.window()
    seen = []
    view.tour_opened.connect(lambda: seen.append(1))
    view.open_tour()
    app.processEvents()
    tour = top._page_tour
    assert tour is not None and tour.isVisible() and tour._tour == "agents" and seen == [1]
    assert tour.title_label.text() == "Ask for anything"
    tour._go_next()
    assert tour.title_label.text() == "Talk to it from anywhere" and "Ctrl + Alt" in tour.desc_label.text()
    tour.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    app.processEvents()
    assert top._page_tour is None and not tour.isVisible() and not tour.spotlight.isVisible()


def test_dark_theme_applies(view, tmp_path):
    view.show_chat({"key": "s1", "entries": _entries(tmp_path), "status": "done", "running": False})
    view.apply_theme("dark")
    app.processEvents()
    assert av._DARK["page"] in view.styleSheet()
    view.apply_theme("light")
