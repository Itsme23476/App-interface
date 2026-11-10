"""The organize top panel's pages for "organize a folder by voice" and "set up auto-organize by voice" (the Mac API,
so the Mac-ported VoiceOrganizeController works unchanged), headless:
  * every new signal fires from its button: undo_clicked, confirm_clicked, choice_made(key), text_submitted(text)
  * show_choice with and without a text box (Return submits the trimmed text, empty Return doesn't; Cancel closes)
  * show_done with / without Undo (20 s vs the usual read-time auto-dismiss)
  * show_confirm (question + Cancel + one main button), show_error(question=True) = an accent "?"
  * hands-free: the plan says what to say instead of the mic button; `action` renames the main button
  * at most _MAX_NAMES (200) file names per folder, then "… and N more"
  * the target folder shows ~ for home and shortens the MIDDLE of a long path (its end used to be cut off)
  * present(tucked=True) → ensure_open(); a body click never closes the hands-free "Reading…" job
  * every page is sized from its text: longer text → taller page, nothing cut, and it shrinks back
  * the typing box asks the panel for the keyboard on click, and gives it back when the page changes
  * the existing Windows hotkey flow's calls still work (positional move_count, allow_pick_folder, dismiss emits)"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea

app = QApplication.instance() or QApplication([])

from app.ui import organize_overlay as oo   # noqa: E402

HOME = str(Path.home())
AUTO_CHOICES = [("everything", "Sort everything now, plus new files"),
                ("existing", "Only use the folders already there"),
                ("new", "Only new files from now on")]
LONG = ('From now on, new files in Second Thoughts will be sorted automatically. Your instructions: "Put the bills in '
        'a folder called Money, the receipts in Taxes and anything from Northwind in Clients". Files already in '
        'Second Thoughts stay where they are, and new ones follow the instructions above.')


@pytest.fixture
def ov():
    w = oo.OrganizeOverlay()
    w.present()
    yield w
    w.dismiss()
    if w._tab is not None:
        w._tab.close()
    w.close()
    w.deleteLater()


def _record(sig):
    got = []
    sig.connect(lambda *a: got.append(a[0] if a else True))
    return got


def _wrapped_cut(page):
    """Wrapping labels on the current page whose text needs more height than they got."""
    return [l.text()[:40] for l in page.findChildren(QLabel)
            if l.isVisibleTo(page) and l.wordWrap() and l.text() and l.heightForWidth(l.width()) > l.height() + 1]


def _plan(ov, n_files=3, **kw):
    folders = {"Invoices": [("a.pdf", True), ("b.pdf", True), ("c.pdf", False)]}
    ov.show_plan("Move 2 files into 1 folder", folders, os.path.join(HOME, "Downloads"), n_files, 1, **kw)


# ---------------------------------------------------------------- signals from buttons
def test_new_signals_fire_from_their_buttons(ov):
    undo, conf, choice = _record(ov.undo_clicked), _record(ov.confirm_clicked), _record(ov.choice_made)
    ov.show_done("Organized 8 files.", undo=True)
    ov._done_undo_btn.click()
    assert undo == [True]
    ov.show_confirm("Turn on auto-organize for Downloads?", "Turn on auto-organize")
    ov._confirm_btn.click()
    assert conf == [True]
    ov.show_choice("How should I keep Downloads organized?", AUTO_CHOICES)
    ov.choice_buttons["existing"].click()
    ov.choice_buttons["new"].click()
    assert choice == ["existing", "new"]


def test_existing_signals_still_fire(ov):
    refine, organize = _record(ov.refine_requested), _record(ov.organize_clicked)
    change, dismissed = _record(ov.change_folder_requested), _record(ov.dismissed)
    _plan(ov)
    ov._refine_btn.click()
    assert refine == [True]
    ov._organize_btn.click()
    assert organize == [True]
    [b for b in ov._page_plan.findChildren(QPushButton) if b.text() == "Change"][0].click()
    assert change == [True]
    ov.show_error("Pick one.", allow_pick_folder=True)
    ov._error_pick_btn.click()
    assert change == [True, True]
    assert dismissed == []
    ov.findChild(QPushButton, "ovCloseBtn").click()          # ✕
    assert dismissed == [True] and not ov.isVisible()


def test_big_plan_still_needs_a_second_click(ov):
    organize = _record(ov.organize_clicked)
    _plan(ov, n_files=45, move_count=45)
    ov._organize_btn.click()
    assert organize == [] and ov._organize_armed and "Click to confirm" in ov._organize_btn.text()
    ov._organize_btn.click()
    assert organize == [True]


# ---------------------------------------------------------------- choice page
def test_choice_without_a_box(ov):
    ov.show_choice("How should I keep Downloads organized?", AUTO_CHOICES)
    assert ov._state == "choice"
    assert ov.choice_field is None
    assert list(ov.choice_buttons) == ["everything", "existing", "new"]
    assert [ov.choice_buttons[k].text() for k, _ in AUTO_CHOICES] == [lbl for _, lbl in AUTO_CHOICES]
    assert all(b.isVisibleTo(ov._page_choice) for b in ov.choice_buttons.values())


def test_choice_with_a_box_return_submits_the_text(ov):
    sub = _record(ov.text_submitted)
    ov.show_choice("Any instructions for how to sort Downloads?", [("none", "No instructions, you decide")],
                   field="Or type them here and press Return")
    f = ov.choice_field
    assert f is not None and f.placeholderText() == "Or type them here and press Return"
    # the box sits ABOVE the answers
    assert f.mapTo(ov._page_choice, QPoint(0, 0)).y() < \
        ov.choice_buttons["none"].mapTo(ov._page_choice, QPoint(0, 0)).y()
    QTest.keyClick(f, Qt.Key_Return)                       # empty: nothing
    QTest.keyClicks(f, "   ")
    QTest.keyClick(f, Qt.Key_Return)                       # blank: nothing
    assert sub == []
    f.clear()
    QTest.keyClicks(f, "  Put the bills in Money  ")
    QTest.keyClick(f, Qt.Key_Return)
    assert sub == ["Put the bills in Money"]


def test_choice_cancel_closes_it(ov):
    dismissed = _record(ov.dismissed)
    ov.show_choice("How should I keep Downloads organized?", AUTO_CHOICES, field="Or type them")
    ov._choice_cancel_btn.click()
    assert dismissed == [True] and not ov.isVisible()


def test_choice_rebuilds_cleanly(ov):
    ov.show_choice("First?", AUTO_CHOICES, field="Type")
    ov.show_choice("Second?", [("none", "No instructions, you decide")])
    QApplication.processEvents()
    assert ov.choice_field is None and list(ov.choice_buttons) == ["none"]
    visible = [b.text() for b in ov._page_choice.findChildren(QPushButton) if b.isVisibleTo(ov._page_choice)]
    assert visible == ["No instructions, you decide", "Cancel"]


# ---------------------------------------------------------------- done / confirm / error
def test_done_without_undo(ov):
    ov.show_done("Organized 8 files. Revert anytime in History.")
    assert ov._state == "done" and ov._done_label.text().startswith("Organized 8 files")
    assert not ov._done_undo_btn.isVisibleTo(ov._page_done)
    assert ov._dismiss_timer is not None and ov._dismiss_timer.interval() <= 9000


def test_done_with_undo_waits_20_seconds(ov):
    ov.show_done("Organized 8 files.", undo=True)
    assert ov._done_undo_btn.isVisibleTo(ov._page_done)
    assert "Undo" in ov._done_undo_btn.text()
    assert ov._dismiss_timer.interval() == 20000
    ov.show_thinking("Putting your files back…")             # a new page cancels the stale auto-dismiss
    assert ov._dismiss_timer is None


def test_confirm_page(ov):
    dismissed, conf = _record(ov.dismissed), _record(ov.confirm_clicked)
    ov.show_confirm("Read 412 files in Downloads first?", "Read 412 files")
    assert ov._state == "confirm"
    assert ov._confirm_label.text() == "Read 412 files in Downloads first?"
    assert ov._confirm_btn.text() == "Read 412 files"
    ov._confirm_cancel_btn.click()
    assert dismissed == [True] and conf == [] and not ov.isVisible()


def test_error_question_shows_an_accent_question_mark(ov):
    ov.show_error("Which folder should I stop?", question=True)
    assert ov._state == "error" and ov._error_label.text() == "Which folder should I stop?"
    assert ov._error_glyph.text() == "?" and oo._ACCENT_HD.lower() in ov._error_glyph.styleSheet().lower()
    assert ov._error_close_btn.isVisibleTo(ov._page_error) and ov._error_close_btn.text() == "Cancel"
    ov.show_error("Something broke.")
    assert ov._error_glyph.text() == "⚠" and oo._WARN.lower() in ov._error_glyph.styleSheet().lower()
    assert ov._error_close_btn.text() == "Close"
    ov.show_error("Did you mean the Second Thoughts folder?", allow_pick_folder=True, question=True)
    assert ov._error_glyph.text() == "?"
    assert ov._error_pick_btn.isVisibleTo(ov._page_error) and not ov._error_close_btn.isVisibleTo(ov._page_error)


# ---------------------------------------------------------------- plan
def test_hands_free_plan_says_what_to_say_instead_of_the_mic(ov):
    ov.set_hands_free(True)
    _plan(ov)
    assert not ov._refine_btn.isVisibleTo(ov._page_plan)
    assert ov._refine_hint.text() == "Say “yes”, “no”, or what to change"
    ov.set_hands_free(False)
    _plan(ov)
    assert ov._refine_btn.isVisibleTo(ov._page_plan)
    assert ov._refine_hint.text() == "Change by voice — tap the mic"


def test_action_renames_the_main_button(ov):
    _plan(ov, n_files=12, action="Turn on and organize")
    assert ov._organize_btn.text() == "✓  Turn on and organize 12 files"
    _plan(ov, n_files=1, action="Turn on and organize")
    assert ov._organize_btn.text() == "✓  Turn on and organize 1 file"
    ov.show_plan("Move 5", {"A": ["x.pdf"]}, HOME, 7, 1, 5)          # Windows hotkey: positional move_count
    assert ov._organize_btn.text() == "✓  Organize — 5 moving"
    ov.show_plan("Done", {"A": [("x.pdf", False)]}, HOME, 1, 1, 0)
    assert not ov._organize_btn.isEnabled() and "tap the mic" in ov._organize_btn.text()
    ov.set_hands_free(True)
    ov.show_plan("Done", {"A": [("x.pdf", False)]}, HOME, 1, 1, 0)
    assert "say what to change" in ov._organize_btn.text()


def _open_group(ov, name):
    hdr = [b for b in ov._page_plan.findChildren(QPushButton) if name in b.text()][0]
    hdr.click()
    return hdr.parentWidget()


@pytest.mark.parametrize("n,more", [(230, "… and 30 more"), (200, None), (5, None)])
def test_at_most_max_names_per_folder(ov, n, more):
    assert oo.OrganizeOverlay._MAX_NAMES == 200
    ov.show_plan("Move", {"Photos": [(f"IMG_{i:04d}.jpg", True) for i in range(n)]}, HOME, n, 1)
    group = _open_group(ov, "Photos")
    texts = [l.text() for l in group.findChildren(QLabel)]
    assert sum(t.startswith("• ") for t in texts) == min(n, 200)
    tails = [t for t in texts if t.startswith("…")]
    assert tails == ([more] if more else [])


def test_already_here_files_are_tagged(ov):
    _plan(ov)
    group = _open_group(ov, "Invoices")
    texts = [l.text() for l in group.findChildren(QLabel) if l.text().startswith("• ")]
    assert texts == ["• a.pdf", "• b.pdf", "• c.pdf   · already here"]


def test_opening_a_group_grows_the_list(ov):
    ov.show_plan("Move", {"A": [(f"f{i}.txt", True) for i in range(3)], "B": [("g.txt", True)]}, HOME, 4, 2)
    lst = ov._page_plan.findChild(QScrollArea)
    closed, win = lst.height(), ov.height()
    _open_group(ov, "A")
    assert lst.height() > closed and ov.height() > win
    big ={"P": [(f"IMG_{i}.jpg", True) for i in range(150)]}
    ov.show_plan("Move", big, HOME, 150, 1)
    _open_group(ov, "P")
    assert ov._page_plan.findChild(QScrollArea).height() == oo.OrganizeOverlay._LIST_MAX_H   # then it scrolls


@pytest.mark.parametrize("path,want", [
    (os.path.join(HOME, "Downloads"), "~" + os.sep + "Downloads"),
    (HOME, "~"),
    (HOME.upper() + os.sep + "Desktop", "~" + os.sep + "Desktop"),        # case-insensitive on Windows
    (HOME + "2" + os.sep + "x", HOME + "2" + os.sep + "x"),                # a sibling isn't home
    (r"D:\Shared\Work", r"D:\Shared\Work"),
    ("", "—"),
])
def test_display_path_uses_tilde_for_home(path, want):
    if path == HOME.upper() + os.sep + "Desktop" and not sys.platform.startswith("win"):
        pytest.skip("case-insensitive paths are a Windows thing")
    assert oo.display_path(path) == want


def test_long_target_path_is_shortened_in_the_middle(ov):
    target = os.path.join(HOME, "Documents", "Clients", "Northwind Traders International",
                          "2026 Quarterly Reports and Invoices", "Archive", "Second Thoughts")
    ov.show_plan("Move", {"A": ["x.pdf"]}, target, 1, 1)
    shown = ov._target_label.text()
    assert shown.startswith("~") and "…" in shown and shown.endswith("Second Thoughts")
    assert ov._target_label.toolTip() == target
    assert ov._target_label.sizeHint().width() <= ov._target_label.width() + 1      # fits: nothing cut off


def test_short_path_keeps_the_folder_name_whole():
    fm = QLabel().fontMetrics()
    p = os.sep.join(["~", "Documents", "Clients", "Northwind Traders International", "Archive", "Second Thoughts"])
    assert oo.short_path(p, fm, 10000) == p                                   # fits: untouched
    room = fm.horizontalAdvance(p) // 2
    s = oo.short_path(p, fm, room)
    assert s.startswith("~") and "…" in s and s.endswith(os.sep + "Second Thoughts")
    assert fm.horizontalAdvance(s) <= room
    tiny = fm.horizontalAdvance("Second Thoughts") // 2                       # not even the name fits
    t = oo.short_path(p, fm, tiny)
    assert "…" in t and fm.horizontalAdvance(t) <= max(40, tiny)


def test_long_folder_name_does_not_widen_the_list(ov):
    name = "A very long folder name for receipts and tax documents from last year and the year before"
    ov.show_plan("Move", {name: ["r.png"], "B": ["b.png"]}, HOME, 2, 2)
    lst = ov._page_plan.findChild(QScrollArea)
    assert lst.widget().width() <= lst.viewport().width()
    hdr = [b for b in ov._page_plan.findChildren(QPushButton) if "(1)" in b.text() and "…" in b.text()]
    assert hdr and hdr[0].toolTip() == name


def test_show_plan_page_reshows_the_same_plan(ov):
    organize = _record(ov.organize_clicked)
    _plan(ov)
    page, btn = ov._page_plan, ov._organize_btn
    ov.set_refining(True)
    ov.show_listening()
    assert ov._state == "listening" and not page.isVisible()
    ov.show_thinking("Transcribing…")
    ov.show_plan_page()
    assert ov._state == "plan" and ov._page_plan is page and page.isVisible() and ov._organize_btn is btn
    btn.click()
    assert organize == [True]


# ---------------------------------------------------------------- open / tuck / clicks
def test_present_tucked_then_ensure_open(ov):
    ov.dismiss()
    ov.present(tucked=True)
    assert ov._collapsed and ov._minimized and not ov.isVisible() and ov._tab.isVisible()
    ov.set_hands_free(True)
    ov.show_thinking("Reading Downloads, 3 of 40")         # work stays tucked
    _plan(ov)                                               # hands-free: the controller decides (ensure_open)
    assert not ov.isVisible() and ov._tab.isVisible()
    ov.ensure_open()
    assert ov.isVisible() and not ov._collapsed and not ov._tab.isVisible() and ov._state == "plan"
    ov.ensure_open()                                        # already open: stays open
    assert ov.isVisible()
    ov.dismiss()
    ov.ensure_open()                                        # closed: opens
    assert ov.isVisible()


def test_hotkey_flow_still_pops_a_tucked_panel_for_results(ov):
    ov.present(tucked=True)
    assert not ov.isVisible()
    ov.show_done("Organized 3 files.")                      # not hands-free: the existing auto-expand
    assert ov.isVisible() and not ov._collapsed


def test_body_click_never_cancels_hands_free_reading(ov):
    dismissed = _record(ov.dismissed)
    ov.set_hands_free(True)
    ov.show_thinking("Reading Downloads, 12 of 300")
    QTest.mouseClick(ov._card, Qt.LeftButton, Qt.NoModifier, QPoint(8, ov._card.height() - 6))
    assert dismissed == [] and ov._state == "thinking"
    ov.ensure_open()
    assert ov.isVisible() and ov._state == "thinking"
    ov.findChild(QPushButton, "ovCloseBtn").click()        # ✕ still closes it
    assert dismissed == [True]


def test_on_dismiss_is_the_mac_name_for_closing(ov):
    dismissed = _record(ov.dismissed)
    ov._on_dismiss()
    assert dismissed == [True] and not ov.isVisible()


# ---------------------------------------------------------------- heights from text
def _h(ov, show):
    show()
    QApplication.processEvents()
    return ov.height()


@pytest.mark.parametrize("page", ["confirm", "choice", "choice_box", "done", "done_undo", "error", "question"])
def test_heights_grow_with_longer_text_and_shrink_back(ov, page):
    show = {
        "confirm": lambda t: ov.show_confirm(t, "Turn on auto-organize"),
        "choice": lambda t: ov.show_choice(t, AUTO_CHOICES),
        "choice_box": lambda t: ov.show_choice(t, [("none", "No instructions, you decide")], field="Or type them"),
        "done": lambda t: ov.show_done(t),
        "done_undo": lambda t: ov.show_done(t, undo=True),
        "error": lambda t: ov.show_error(t, allow_pick_folder=True),
        "question": lambda t: ov.show_error(t, question=True),
    }[page]
    short = _h(ov, lambda: show("Turn it on?"))
    assert _wrapped_cut(ov._current) == []
    long_ = _h(ov, lambda: show(LONG))
    assert _wrapped_cut(ov._current) == []
    assert long_ > short + 30                               # 3-4 more lines of text
    again = _h(ov, lambda: show("Turn it on?"))
    assert again == short                                   # and back (a label's old height used to stick)
    # the page's content fits the window: nothing below the card's bottom edge
    card_bottom = ov._card.mapTo(ov, QPoint(0, ov._card.height())).y()
    assert card_bottom <= ov.height()


def test_long_thinking_line_wraps_instead_of_running_off(ov):
    ov.show_thinking("Reading Filect Client Project Archive From Last Year And The Year Before, 120 of 300")
    lbl = ov._thinking_lbl
    assert lbl.width() <= oo._INNER_W - 34
    assert lbl.heightForWidth(lbl.width()) <= lbl.height() + 1
    one = _h(ov, lambda: ov.show_thinking("Finding the folder…"))
    assert ov._thinking_lbl.heightForWidth(ov._thinking_lbl.width()) <= ov._thinking_lbl.height() + 1
    assert one < _h(ov, lambda: ov.show_thinking("Reading Filect Client Project Archive From Last Year And The "
                                                 "Year Before, 120 of 300 and then some more words to wrap"))


# ---------------------------------------------------------------- the typing box takes the keyboard
def test_box_click_asks_for_the_keyboard_and_gives_it_back(ov, monkeypatch):
    from app.ui import win_panel
    calls = []

    def fake(widget, on):
        calls.append((widget, bool(on)))
        widget._win_typing = bool(on)
    monkeypatch.setattr(win_panel, "allow_typing", fake)
    monkeypatch.setattr(oo, "IS_WIN", True)
    ov.show_choice("Any instructions?", [("none", "No instructions, you decide")], field="Type")
    QTest.mouseClick(ov.choice_field, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
    assert calls == [(ov, True)]
    assert ov._win_panel_capture is False                   # typing only: not hidden from screenshots
    ov.show_thinking("Updating the plan…")                  # the page changed: the keyboard goes back
    assert calls[-1] == (ov, False)
    ov.show_choice("Any instructions?", [("none", "No")], field="Type")
    QTest.mouseClick(ov.choice_field, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
    sub = _record(ov.text_submitted)
    QTest.keyClicks(ov.choice_field, "money")
    QTest.keyClick(ov.choice_field, Qt.Key_Return)          # submitted: given back
    assert sub == ["money"] and calls[-1] == (ov, False)
    QTest.mouseClick(ov.choice_field, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
    ov.dismiss()                                            # closed: given back (before hiding)
    assert calls[-1] == (ov, False)


def test_panel_never_refuses_focus_at_the_qt_level(ov):
    # Qt.WindowDoesNotAcceptFocus made Qt refuse to activate the panel even for the box (Windows made it the
    # foreground window, Qt's focus window stayed elsewhere). Not taking focus is WS_EX_NOACTIVATE's job.
    assert not (ov.windowFlags() & Qt.WindowDoesNotAcceptFocus)
    assert ov.testAttribute(Qt.WA_ShowWithoutActivating)


# ---------------------------------------------------------------- the existing Windows hotkey flow
def test_existing_hotkey_flow_calls(ov):
    dismissed = _record(ov.dismissed)
    ov.present()
    ov.show_listening("Listening… how should I change it?")
    assert ov._state == "listening" and ov._listen_caption.text() == "Listening… how should I change it?"
    ov.set_level(0.6)
    ov.set_refining(True)
    ov.set_refining(False)
    ov.show_thinking("Finding the folder…")
    ov.show_thinking("Analyzing your files…")               # rapid updates: one spinner, text only
    assert ov._state == "thinking" and ov._thinking_lbl.text() == "Analyzing your files…"
    assert len(ov._page_thinking.findChildren(oo.Spinner)) == 1
    ov.show_plan("Move 3 files into 2 folders", {"Docs": [("a.pdf", True)], "Pics": [("b.png", True)]},
                 os.path.join(HOME, "Downloads"), 3, 2, 2)
    assert ov._state == "plan" and ov._organize_btn.text() == "✓  Organize — 2 moving"
    ov.show_applying()
    assert ov._state == "applying" and ov._thinking_lbl.text() == "Organizing…"
    ov.show_done("Organized 2 files. Revert anytime in History.")
    assert ov._state == "done"
    ov.show_error("I couldn't tell which folder you meant. Pick one to organize.", allow_pick_folder=True)
    assert ov._error_pick_btn.isVisibleTo(ov._page_error)
    ov.dismiss()
    assert dismissed == [True] and not ov.isVisible()
