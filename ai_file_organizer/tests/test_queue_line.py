"""Analyze Files: what waits for its turn is said ON THE PAGE, under Pause / Cancel - folders, then files - and queuing
never opens a window. (The old queue_row and its two spacers were never placed on the page, so showing them opened
three blank white "Filect" windows: live test 2026-11-13, as on the Mac, d1cc875.) The real main window, off-screen,
in a temp app-data folder; nothing is analyzed (the start of the next item is stubbed)."""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from PySide6.QtWidgets import QApplication  # noqa: E402
from real_window import RealWindow, pump  # noqa: E402


@pytest.fixture
def busy():
    rw = RealWindow()
    w = rw.W
    w.page_stack.setCurrentIndex(2)                            # the Analyze Files page, as when you use it
    w.is_indexing = True                                       # (something is being analyzed)
    w.index_progress_container.setVisible(True)
    pump(0.05)
    yield w
    w.is_indexing = False
    rw.close()


def _windows():
    return {id(x): x for x in QApplication.topLevelWidgets() if x.isVisible()}


def test_queuing_opens_no_window_and_the_page_says_what_waits(busy):
    w = busy
    before = _windows()
    w._add_to_index_queue(Path(r"C:\tmp\Pantry Box"))
    pump(0.05)
    new = [type(x).__name__ for k, x in _windows().items() if k not in before]
    assert not new, f"queuing a folder opened a window: {new}"
    assert w.queue_label.window() is w and w.queue_label.isVisible(), "the queue line isn't on the page"
    assert w.queue_label.text() == "📋 Queued, starts when this one finishes: Pantry Box"


def test_files_are_listed_after_folders_and_the_line_follows_the_queue(busy):
    w = busy
    with mock.patch("app.ui.main_window.QTimer.singleShot", lambda ms, fn: None):   # (nothing really starts)
        w._add_to_index_queue(Path(r"C:\tmp\Pantry Box"))
        w._index_files_or_queue([r"C:\tmp\loose one.txt", r"C:\tmp\loose two.txt"])
        assert w.queue_label.text() == ("📋 Queued, starts when this one finishes: Pantry Box • loose one.txt "
                                        "+1 more"), w.queue_label.text()
        assert w._start_next_queued()                          # the folder starts: only the files wait
        assert w.queue_label.text() == "📋 Queued, starts when this one finishes: loose one.txt • loose two.txt"
        assert w.queue_label.isVisible()
        assert w._start_next_queued()                          # the files start: nothing waits
        assert not w.queue_label.isVisible()


def test_the_old_placeholders_are_gone(busy):
    for n in ("queue_row", "queue_top_spacer", "queue_spacer"):
        assert not hasattr(busy, n), f"{n} is back (a parentless widget that becomes a window when shown)"
