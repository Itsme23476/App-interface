"""Quick Search shortcut rules. Found on the Mac 2026-11-12 (Windows stage 29, 2026-11-13): a saved "shift+a" passed
(Save only checked for a "+"), but Shift + a letter is just typing a capital letter - as a global shortcut it swallows
that letter in every app and pops Quick Search instead. And the setting's fallback was 'ctrl+x' (Cut, in every app)
while a new install used 'ctrl+alt+h'. Now a shortcut needs Ctrl, Alt or Win plus a key; an unusable saved one goes
back to Ctrl+Alt+H (saved, and shown in Settings); one default. Mac 0308d18.
Real MainWindow code on stand-ins; settings are never written; no real hotkey is registered."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication, QKeySequenceEdit  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core import settings as S  # noqa: E402
import app.ui.main_window as mwm  # noqa: E402


def test_shift_plus_a_letter_is_not_a_shortcut():
    for sc, ok in (("shift+a", False), ("Shift+Q", False), ("shift+space", False), ("a", False), ("", False),
                   ("ctrl+alt+h", True), ("Ctrl+Alt+H", True), ("ctrl+shift+space", True), ("alt+f5", True),
                   ("meta+k", True), ("win+k", True), ("ctrl+shift", False), ("ctrl", False)):
        assert mwm._is_valid_hotkey(sc) is ok, sc


def test_a_saved_shift_letter_goes_back_to_the_default():
    assert mwm._normalize_shortcut("shift+q") == "Ctrl+Alt+H"
    assert mwm._normalize_shortcut("ctrl+shift+space") == "Ctrl+Shift+Space"
    assert mwm._normalize_shortcut("alt+f5") == "Alt+F5"


def _save(seq_text):
    w = SimpleNamespace(qs_shortcut_input=QKeySequenceEdit(), status_bar=mock.MagicMock())
    w.qs_shortcut_input.setKeySequence(QKeySequence(seq_text))
    saved = []
    with mock.patch.object(S.settings, "set_quick_search_shortcut", lambda sc: saved.append(sc)), \
            mock.patch.object(S.settings, "quick_search_shortcut", "ctrl+alt+h"), \
            mock.patch("app.ui.organize_page.ModernInfoDialog.show_warning") as warn:
        mwm.MainWindow.on_qs_save_shortcut(w)
    return saved, warn, w.qs_shortcut_input.keySequence().toString(QKeySequence.PortableText)


def test_save_refuses_shift_letter_and_says_why():
    saved, warn, box = _save("Shift+B")
    assert saved == [] and warn.called, (saved, warn.called)
    msg = " ".join(str(a) for a in warn.call_args.args)
    assert "Shift" in msg and "Ctrl" in msg, msg
    assert box == "Ctrl+Alt+H", box                          # (the box shows the one that's still saved)


def test_save_takes_a_real_shortcut():
    saved, warn, _ = _save("Ctrl+Alt+K")
    assert saved == ["Ctrl+Alt+K"] and not warn.called, (saved, warn.called)


def test_one_default_everywhere():
    assert S.DEFAULT_QUICK_SEARCH_SHORTCUT == "ctrl+alt+h"
    assert mwm.QS_DEFAULT_SHORTCUT.lower() == S.DEFAULT_QUICK_SEARCH_SHORTCUT
    before = S.settings.quick_search_shortcut
    with mock.patch.object(S.settings, "_save_config", lambda *a, **k: None):
        try:
            S.settings.set_quick_search_shortcut("")             # (it fell back to 'ctrl+x': Cut, in every app)
            assert S.settings.quick_search_shortcut == "ctrl+alt+h", S.settings.quick_search_shortcut
        finally:
            S.settings.quick_search_shortcut = before


def _start_quick_search(stored):
    registered, saved = [], []
    w = SimpleNamespace(on_quick_path_selected=mock.MagicMock(), qs_shortcut_input=QKeySequenceEdit())
    w.qs_shortcut_input.setKeySequence(QKeySequence(stored))
    with mock.patch.dict(sys.modules, {"qhotkey": None}), \
            mock.patch.object(mwm, "QuickSearchOverlay", mock.MagicMock()), \
            mock.patch.object(mwm, "register_global_hotkey", lambda parent, ks, cb: registered.append(ks) or object()), \
            mock.patch.object(S.settings, "quick_search_shortcut", stored), \
            mock.patch.object(S.settings, "set_quick_search_shortcut", lambda sc: saved.append(sc)):
        mwm.MainWindow.setup_quick_search(w)
    return registered, saved, w.qs_shortcut_input.keySequence().toString(QKeySequence.PortableText)


def test_an_unusable_saved_shortcut_is_replaced_saved_and_shown():
    registered, saved, box = _start_quick_search("shift+q")
    assert registered == ["Ctrl+Alt+H"], registered
    assert saved == ["ctrl+alt+h"], saved
    assert box == "Ctrl+Alt+H", box


def test_a_good_saved_shortcut_is_left_alone():
    registered, saved, _ = _start_quick_search("ctrl+alt+h")
    assert registered == ["Ctrl+Alt+H"] and saved == [], (registered, saved)
