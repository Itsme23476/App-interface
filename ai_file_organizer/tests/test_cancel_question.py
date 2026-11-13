"""Cancel's question during an analysis. Found in the full test (stage 42, 2026-11-13): "Are you sure you want to
cancel indexing?" stayed open after the analysis had finished - blocking the whole window - and a late "Yes" still
said "Indexing cancelled", or cancelled the NEXT folder in the queue that had started meanwhile. Now the question
closes by itself when its run ends, and "Yes" only cancels that same run (Entire PC: the whole run, whichever folder
it's on). Off-screen; nothing is analyzed."""
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])
import app.ui.main_window as mwm  # noqa: E402


@pytest.fixture
def w(monkeypatch):
    svc = mock.MagicMock()
    monkeypatch.setattr(mwm, "search_service", svc)
    win = QWidget()
    win.is_indexing, win.index_worker, win._pc_index_queue = True, mock.MagicMock(), None
    win.status_bar, win.search_button = mock.MagicMock(), mock.MagicMock()
    win._hide_index_controls = win.update_search_statistics = win.refresh_debug_view = mock.MagicMock()
    win.svc = svc
    win.timers = []
    yield win
    for t in win.timers:                            # (a timer left over from one test never answers the next one's box)
        t.stop()
    win.deleteLater()


def _later(w, ms, fn):
    t = QTimer(w)
    t.setSingleShot(True)
    t.timeout.connect(fn)
    t.start(ms)
    w.timers.append(t)


def _answer(w, button, after_ms):
    def click():
        box = QApplication.activeModalWidget()
        if isinstance(box, QMessageBox):
            box.button(button).click()
    _later(w, after_ms, click)


def _ask(w):
    def rescue():                                   # (a question that never closes fails the test, not hangs it)
        box = QApplication.activeModalWidget()
        if isinstance(box, QMessageBox):
            box.reject()
    _later(w, 3000, rescue)
    t0 = time.monotonic()
    mwm.MainWindow._cancel_indexing(w)
    return time.monotonic() - t0


def test_the_question_closes_itself_when_the_run_ends(w):
    _later(w, 300, lambda: setattr(w, "is_indexing", False))       # it finishes while you read the question
    took = _ask(w)
    assert took < 2.0, f"the question stayed up {took:.1f} s"
    assert not w.svc.cancel_indexing.called and not w.index_worker.cancel.called


def test_a_late_yes_never_cancels_the_next_folder(w):
    first = w.index_worker
    _later(w, 300, lambda: setattr(w, "index_worker", mock.MagicMock()))   # the next queued folder started
    _answer(w, QMessageBox.Yes, 900)                   # ...and THEN you click Yes
    _ask(w)
    assert not w.svc.cancel_indexing.called and not first.cancel.called and not w.index_worker.cancel.called


def test_yes_while_it_runs_cancels_it(w):
    _answer(w, QMessageBox.Yes, 300)
    _ask(w)
    assert w.svc.cancel_indexing.called and w.index_worker.cancel.called


def test_no_changes_nothing(w):
    _answer(w, QMessageBox.No, 300)
    _ask(w)
    assert not w.svc.cancel_indexing.called


def test_entire_pc_moving_to_its_next_folder_keeps_the_question(w):
    w._pc_index_queue = [Path("Desktop"), Path("Documents")]
    _later(w, 300, lambda: setattr(w, "index_worker", mock.MagicMock()))   # Entire PC: next folder, same run
    _answer(w, QMessageBox.Yes, 900)
    _ask(w)
    assert w.svc.cancel_indexing.called and w._pc_index_queue is None
