"""Quick Search filling a file in (full feature test, 2026-11-13):
  · The UIA way never filled anything: it imported CONFIRM_NAMES (and FILENAME_LABELS) from app.core.vision, where
    they aren't - both are main_window's own. Every fill fell through to the Win32 way, 1.2 s later.
  · A Windows Open/Save window (#32770) now gets the Win32 way first (it finds the File name box at once), and the
    Win32 way puts the whole path in at once (typing it key by key took 1.2 s+). Filect is frozen while it fills.
  · Over a program that ISN'T an Open/Save window, the Open-window ways cleared its text box first (select all +
    delete) or pressed Alt+N in it. Now the path is pasted at the cursor (as the Mac does) - only with that program
    in front, nothing selected or cleared, no Enter.
Stand-in windows and controls; nothing is typed or pasted anywhere."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
import app.ui.main_window as mwm  # noqa: E402
import app.ui.win_hotkey as wh  # noqa: E402

MW = mwm.MainWindow
P = r"C:\Users\x\Search Test\garden notes.txt"


class Edit:
    """A text box: what's put in it is what it then says."""

    def __init__(self, takes_set=True):
        self.text, self.takes_set, self.typed = "", takes_set, []

    def set_focus(self):
        pass

    def set_value(self, t):
        self.text = t

    def set_edit_text(self, t):
        if self.takes_set:
            self.text = t

    def type_keys(self, keys, **k):
        self.typed.append(keys)
        if keys == "^a{BACKSPACE}":
            self.text = ""
        elif keys != "{ENTER}":
            self.text += keys

    def get_value(self):
        return self.text

    def window_text(self):
        return self.text


class Button:
    def __init__(self):
        self.pressed = 0

    def exists(self, *a, **k):
        return True

    def invoke(self):
        self.pressed += 1

    click = invoke


def stub():
    return SimpleNamespace(status_bar=mock.MagicMock())


def test_the_uia_way_fills_and_presses_open():
    """(It failed on its first line before: the import of CONFIRM_NAMES from app.core.vision.)"""
    box, open_btn = Edit(), Button()
    win = SimpleNamespace(child_window=lambda **k: open_btn, type_keys=lambda *a, **k: None)
    with mock.patch.object(mwm.settings, "quick_search_auto_confirm", True):
        ok = MW._insert_path_uia(stub(), box, P, win)
    assert ok and box.text == P and open_btn.pressed == 1


def test_no_import_of_names_vision_doesnt_have():
    src = (Path(mwm.__file__)).read_text(encoding="utf-8")
    assert "from app.core.vision import CONFIRM_NAMES" not in src
    assert "from app.core.vision import FILENAME_LABELS" not in src
    assert mwm.CONFIRM_NAMES and mwm.FILENAME_LABELS


def test_the_win32_way_puts_the_whole_path_in_at_once():
    box = Edit()
    win = SimpleNamespace(child_window=lambda **k: Button())
    with mock.patch.object(mwm.settings, "quick_search_auto_confirm", False):
        assert MW._insert_path_win32(stub(), box, P, win)
    assert box.text == P and box.typed == [], box.typed            # nothing typed key by key


def test_the_win32_way_still_types_it_if_that_didnt_take():
    box = Edit(takes_set=False)
    win = SimpleNamespace(child_window=lambda **k: Button())
    with mock.patch.object(mwm.settings, "quick_search_auto_confirm", False):
        assert MW._insert_path_win32(stub(), box, P, win)
    assert box.text == P and box.typed[0] == "^a{BACKSPACE}"


def test_auto_confirm_finds_the_open_button_without_waiting():
    """Live: the Win32 'Open' button is titled '&Open' - the lookup by "Open" missed every name, each miss waiting
    pywinauto's 0.5 s: 2.8 s before Enter was tried. Now '&Open' matches and a missing name doesn't wait."""
    import re
    looked, waits, open_btn = [], [], Button()

    class Spec:
        def __init__(self, k):
            self.k = k

        def exists(self, timeout=None):
            waits.append(timeout)
            pat = self.k.get("title_re")
            return bool(pat and re.match(pat, "&Open"))

        def click(self):
            open_btn.click()

    win = SimpleNamespace(child_window=lambda **k: looked.append(k) or Spec(k))
    box = Edit()
    with mock.patch.object(mwm.settings, "quick_search_auto_confirm", True), mock.patch.object(mwm.time, "sleep"):
        assert MW._insert_path_win32(stub(), box, P, win)
    assert open_btn.pressed == 1 and "{ENTER}" not in box.typed, (open_btn.pressed, box.typed)
    assert waits and all(t == 0 for t in waits), waits


def _overlay(cls):
    return SimpleNamespace(_saved_window_hwnd=4242, _saved_window_title="Open", _saved_window_class=cls,
                           _is_dialog_verified=True, _saved_cursor_pos=(1, 1), _saved_window_rect=(0, 0, 9, 9),
                           has_valid_saved_state=lambda: True)


def _run(cls, fg=4242):
    w = stub()
    w.quick_overlay = _overlay(cls)
    calls = []
    for name in ("_autofill_targeted_uia", "_autofill_targeted_win32", "_autofill_keyboard_altn"):
        setattr(w, name, (lambda n: lambda *a: calls.append(n) or n == "_autofill_targeted_win32")(name))
    w._autofill_with_saved_state = lambda p, o: MW._autofill_with_saved_state(w, p, o)
    w._paste_path_at_cursor = lambda p, h: MW._paste_path_at_cursor(w, p, h)
    import keyboard
    with mock.patch.object(wh, "create_autofill_debug_report"), \
            mock.patch.object(wh, "window_still_exists", return_value=True), \
            mock.patch.object(wh, "get_window_title", return_value="Open"), \
            mock.patch.object(wh, "get_window_class", return_value=cls), \
            mock.patch.object(wh, "get_foreground_hwnd", return_value=fg), \
            mock.patch.object(mwm, "put_back_if_changed") as guard, \
            mock.patch.object(keyboard, "send") as send:
        MW.try_autofill_file_dialog(w, P)
    return calls, [c.args for c in send.call_args_list], guard.called


def test_an_open_window_gets_the_win32_way_first():
    calls, sent, _ = _run("#32770")
    assert calls == ["_autofill_targeted_win32"], calls
    assert sent == []


def test_another_program_gets_a_paste_at_the_cursor_and_no_open_window_ways():
    calls, sent, guarded = _run("Chrome_WidgetWin_1")
    assert calls == [], f"the Open-window ways ran on another program: {calls}"
    assert sent == [("ctrl+v",)] and guarded, sent                  # no select-all, no Enter, no Alt+N


def test_no_paste_if_that_program_isnt_in_front():
    calls, sent, _ = _run("Notepad", fg=999)
    assert calls == [] and sent == [], (calls, sent)
