"""Analyze Files: 'View Analyzed Files (N)' and 'Showing N indexed files' follow the files AS they're analyzed (a folder,
loose files, Entire PC) - they waited for the folder to end, so the page said (0) while files were saved - and a
folder's end keeps the real total (it was overwritten with that run's count). Live test 2026-11-13. The real main
window off-screen, a temp index; nothing is analyzed (rows are put in the temp index directly)."""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from real_window import RealWindow  # noqa: E402


def _add(n, start=0):
    from app.core.database import file_index
    for i in range(start, start + n):
        assert file_index.add_file({"source_path": rf"C:\tmp\count test\note {i}.txt", "name": f"note {i}.txt",
                                    "extension": ".txt", "size": 10, "category": "Documents"})


@pytest.fixture
def win():
    rw = RealWindow()
    w = rw.W
    w.page_stack.setCurrentIndex(2)
    w._live_count_at = 0.0
    yield w
    rw.close()


def _counts(w):
    return w.view_files_btn.text(), w.debug_info_label.text()


def test_a_folder_run_updates_the_count_as_files_are_saved(win):
    w = win
    _add(5)
    w._on_index_progress(5, 30, "Processing")                   # (a folder's progress, as from its worker)
    assert _counts(w) == ("View Analyzed Files (5)", "Showing 5 indexed files"), _counts(w)


def test_loose_files_and_entire_pc_too(win):
    w = win
    _add(2)
    w._on_file_index_progress(2, 4, "Analyzing file 2 of 4")
    assert _counts(w)[0] == "View Analyzed Files (2)"
    import app.ui.main_window as mwm
    from PySide6.QtCore import QObject, Signal

    class FakeWorker(QObject):
        index_completed = Signal(dict)
        index_error = Signal(str)
        progress_data = Signal(int, int, str)

        def __init__(self, folder):
            super().__init__()

        def start(self):
            pass

        def isRunning(self):
            return False
    with mock.patch.object(mwm, "IndexWorker", FakeWorker):
        w.is_indexing = True
        w._pc_index_queue = [Path(r"C:\home\Desktop")]
        w._index_next_pc_folder()
        _add(3, start=10)
        w._live_count_at = 0.0
        w.index_worker.progress_data.emit(3, 6, "x")
        w.is_indexing = False
    assert _counts(w) == ("View Analyzed Files (5)", "Showing 5 indexed files"), _counts(w)


def test_at_most_once_a_second(win):
    from app.core.database import file_index
    w = win
    with mock.patch.object(file_index, "get_file_count", wraps=file_index.get_file_count) as counted:
        for i in range(20):                                     # 20 progress updates in a burst
            w._on_index_progress(i, 20, "Processing")
    assert counted.call_count == 1, f"{counted.call_count} index counts for one burst"


def test_a_folders_end_keeps_the_real_total(win):
    w = win
    _add(33)
    with mock.patch.object(w, "_start_next_queued", return_value=False):
        w.is_indexing = True
        w.on_index_completed({"indexed_files": 30, "files_with_ocr": 0})
    assert _counts(w) == ("View Analyzed Files (33)", "Showing 33 indexed files"), _counts(w)
