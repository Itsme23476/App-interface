"""Pasting through the clipboard (Mac c143812, and Windows' own restore). Live (Windows stage 34, 2026-11-13): another
program wrote to the clipboard the moment the dictation put its text there, and THAT was pasted - not your words; and
a picture you had copied was gone after a dictation (only the clipboard's text was put back: it became empty text).
Now: right before Ctrl+V, Filect's text is put back if it changed (the dictation and every Quick Search paste); after
the dictation's paste, ALL of what was there goes back - unless something newer was copied. Qt's own (off-screen)
clipboard: your real one is never touched; no keys are pressed."""
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from PySide6.QtGui import QGuiApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
if not isinstance(app, QGuiApplication) or QGuiApplication.platformName() != "offscreen":
    pytest.skip("needs Qt's off-screen clipboard (never YOUR clipboard)", allow_module_level=True)
from app.ui import clipboard_guard as G  # noqa: E402

CB = QApplication.clipboard()


def picture(w=64, h=48):
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("#7C4DFF"))
    return img


def test_put_back_only_when_it_changed():
    CB.setText("the dictation")
    assert G.put_back_if_changed("the dictation") is None and CB.text() == "the dictation"
    CB.setText("INTRUDER")                                       # another program wrote meanwhile
    newer = G.put_back_if_changed("the dictation")
    assert CB.text() == "the dictation", "what gets pasted is Filect's text"
    assert newer is not None and newer.text() == "INTRUDER", "what the other program wrote is kept for afterwards"


def test_restore_puts_everything_back_unless_something_newer_was_copied():
    CB.setImage(picture())
    saved = G.snapshot()
    CB.setText("the dictation")
    assert G.restore_if_still("the dictation", saved) is True
    img = CB.image()
    assert not img.isNull() and (img.width(), img.height()) == (64, 48), "the copied picture is back"
    CB.setText("the dictation")
    saved = G.snapshot()
    CB.setText("copied right after the paste")
    assert G.restore_if_still("the dictation", saved) is False and CB.text() == "copied right after the paste"


def _dictation():
    from app.ui.dictation import DictationController
    later = []
    c = SimpleNamespace()
    for m in ("_paste", "_do_paste_and_restore", "_restore_clipboard"):
        setattr(c, m, (lambda name: lambda *a, **k: getattr(DictationController, name)(c, *a, **k))(m))
    return c, later


def test_the_dictation_pastes_your_words_even_if_another_program_writes():
    import app.ui.dictation as D
    c, later = _dictation()
    CB.setText("your old clipboard")
    pasted = []
    with mock.patch.object(D.QTimer, "singleShot", lambda ms, fn: later.append(fn)), \
            mock.patch.object(D, "_send_ctrl_v", lambda: pasted.append(CB.text())):
        c._paste("This is the paste test.")
        CB.setText("INTRUDER")                                   # in the 90 ms before Ctrl+V
        later.pop(0)()                                           # Ctrl+V
        later.pop(0)()                                           # 0.4 s later: put the clipboard back
    assert pasted == ["This is the paste test."], pasted
    assert CB.text() == "INTRUDER", "afterwards: the other program's copy (the newest), not your older one"


def test_the_dictation_keeps_a_copied_picture():
    import app.ui.dictation as D
    c, later = _dictation()
    CB.setImage(picture())
    pasted = []
    with mock.patch.object(D.QTimer, "singleShot", lambda ms, fn: later.append(fn)), \
            mock.patch.object(D, "_send_ctrl_v", lambda: pasted.append(CB.text())):
        c._paste("Hello again.")
        later.pop(0)()
        later.pop(0)()
    img = CB.image()
    assert pasted == ["Hello again."] and not img.isNull() and (img.width(), img.height()) == (64, 48)


def test_the_dictation_leaves_a_copy_made_after_the_paste():
    import app.ui.dictation as D
    c, later = _dictation()
    CB.setText("your old clipboard")
    with mock.patch.object(D.QTimer, "singleShot", lambda ms, fn: later.append(fn)), \
            mock.patch.object(D, "_send_ctrl_v", lambda: None):
        c._paste("Hello again.")
        later.pop(0)()
        CB.setText("you copied this right after")
        later.pop(0)()
    assert CB.text() == "you copied this right after"


def test_every_quick_search_paste_is_guarded():
    """Quick Search fills a file dialog by pasting the path (Ctrl+A, 50 ms, Ctrl+V): each of those Ctrl+V's is right
    after the guard."""
    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    sends = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("keyboard.send('ctrl+v')")]
    assert len(sends) == 10, len(sends)        # (the 10th: Quick Search over a program that isn't an Open window)
    for i in sends:
        assert lines[i - 1].strip().startswith("put_back_if_changed(path)"), (i + 1, lines[i - 1])
    assert re.search(r"^from app\.ui\.clipboard_guard import put_back_if_changed$", src, re.M)


@pytest.fixture(autouse=True)
def _clean_clipboard():
    yield
    CB.clear()
