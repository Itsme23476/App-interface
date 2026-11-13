"""Index Entire PC (Mac 694b1b6 + de286ca + 122924c, ported to Windows 2026-11-13; live test the same day). The real
main window, off-screen, in a temp app-data folder; the worker is a stand-in (nothing is analyzed, nothing is sent):
  · Cancel never says "Analysis Complete", and nothing after the cancelled folder starts
  · a cancelled run's folder finishing can't continue a NEW run
  · the bar moves: progress reaches the page (a QTimer started on the worker thread never fired: busy at 0%)
  · the drop box stays and says what's analyzed; Entire PC counts as busy, so drops (and a second Entire PC) wait"""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from PySide6.QtCore import QObject, Signal  # noqa: E402
from real_window import RealWindow, pump  # noqa: E402
import app.ui.main_window as mwm  # noqa: E402


class FakeWorker(QObject):
    index_completed = Signal(dict)
    index_error = Signal(str)
    progress_data = Signal(int, int, str)
    made = []

    def __init__(self, folder):
        super().__init__()
        self.folder, self.started, self.cancelled = folder, False, False
        FakeWorker.made.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return False

    def cancel(self):
        self.cancelled = True

    def is_paused(self):
        return False

    def is_cancelled(self):
        return self.cancelled


@pytest.fixture
def win(monkeypatch):
    FakeWorker.made = []
    monkeypatch.setattr(mwm, "IndexWorker", FakeWorker)
    rw = RealWindow()
    w = rw.W
    w.page_stack.setCurrentIndex(2)
    boxes = []
    monkeypatch.setattr(mwm.QMessageBox, "information", lambda *a, **k: boxes.append(a[1:3]))
    monkeypatch.setattr(mwm.QMessageBox, "question", lambda *a, **k: mwm.QMessageBox.Yes)
    monkeypatch.setattr(mwm.search_service, "cancel_indexing", lambda: None)
    monkeypatch.setattr(mwm.QTimer, "singleShot", lambda ms, fn: fn())     # (no event loop pumped)
    w._test_boxes = boxes
    yield w
    w.is_indexing = False
    rw.close()


def _start(w, names=("Desktop", "Documents", "Downloads")):
    w.is_indexing = True
    w._pc_index_queue = [Path(r"C:\home") / n for n in names]
    w._pc_total_indexed = 0
    w._index_next_pc_folder()
    return FakeWorker.made[-1]


def test_the_drop_box_stays_and_says_what_is_analyzed(win):
    w = win
    worker = _start(w)
    pump(0.05)
    assert worker.started and worker.folder.name == "Desktop"
    assert w.drop_zone.isVisible(), "the drop box was hidden during Entire PC"
    assert w.drop_title.text() == "Analyzing: Desktop", w.drop_title.text()
    assert w.drop_subtitle.text() == "Entire PC: 2 more folder(s) after this", w.drop_subtitle.text()


def test_the_bar_moves(win):
    w = win
    worker = _start(w)
    assert w.index_percent_label.text() != "0%", "it says 0% before anything is counted"
    worker.progress_data.emit(3, 10, "Processing file 3 of 10")     # (the worker's signal, as from its thread)
    assert (w.progress_bar.minimum(), w.progress_bar.maximum(), w.progress_bar.value()) == (0, 10, 3)
    assert w.index_percent_label.text() == "30%" and w.index_progress_label.text() == "Processing file 3 of 10"


def test_cancel_never_says_complete_and_nothing_after_it_starts(win):
    w = win
    worker = _start(w)
    w._cancel_indexing()
    n = len(FakeWorker.made)
    worker.index_completed.emit({"indexed_files": 4})               # the cancelled folder finishes
    assert not w._test_boxes, f"Cancel said: {w._test_boxes}"
    assert len(FakeWorker.made) == n, "the next folder started after Cancel"
    assert not w.is_indexing, "still busy after a cancelled Entire PC"


def test_a_cancelled_folder_finishing_cant_continue_a_new_run(win):
    w = win
    old = _start(w)
    w._cancel_indexing()
    new = _start(w, names=("Pictures", "Videos"))                     # Start again before the old folder ended
    n = len(FakeWorker.made)
    old.index_completed.emit({"indexed_files": 9})                   # the OLD folder ends now
    assert len(FakeWorker.made) == n, "the old run's finish started the new run's next folder (two at once)"
    assert w._pc_total_indexed == 0, "the old run's count went into the new run"
    new.index_completed.emit({"indexed_files": 2})
    assert FakeWorker.made[-1].folder.name == "Videos"


def test_entire_pc_is_busy_so_drops_wait(win):
    w = win
    _start(w)
    with mock.patch.object(w, "_start_indexing_path") as started:
        w._index_confirmed([Path(r"C:\tmp\PC Drop Test")], [], None)
    started.assert_not_called()
    assert [p.name for p in w.index_queue] == ["PC Drop Test"]
    last = FakeWorker.made[-1]                                        # Desktop … then the last folder ends
    for _ in range(3):
        last.index_completed.emit({"indexed_files": 1})
        last = FakeWorker.made[-1]
    assert w._test_boxes and w._test_boxes[-1][1] == "Successfully analyzed 3 files from your PC."
    assert w.index_queue == [], "the folder dropped mid-run didn't start after Entire PC"


def test_a_second_entire_pc_waits_for_the_running_analysis(win, tmp_path, monkeypatch):
    w = win
    for n in ("Desktop", "Documents"):
        (tmp_path / n).mkdir()
    monkeypatch.setattr(mwm.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(mwm.QDialog, "exec", lambda self: mwm.QDialog.Accepted)
    w.is_indexing = True                                              # a normal analysis runs
    n = len(FakeWorker.made)
    w.on_index_entire_pc()
    assert len(FakeWorker.made) == n, "Entire PC started on top of a running analysis"
    assert [p.name for p in w.index_queue] == ["Desktop", "Documents"]
    w.index_queue.clear()
    w.is_indexing = False
    w.on_index_entire_pc()                                            # free: it starts, and counts as busy
    assert w.is_indexing and FakeWorker.made[-1].folder.name == "Desktop"
