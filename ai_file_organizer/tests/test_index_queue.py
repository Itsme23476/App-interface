"""Analyze Files: a folder dropped while another is being analyzed waits in the queue and is analyzed next.
_update_queue_ui used `self.index_group`, which doesn't exist, so queuing threw and, when the first folder finished,
the queued one was taken off the list and never started (silently lost). Real MainWindow methods on a stand-in
window; nothing is analyzed. Ported from the Mac (pack §6.2, item 1). The queue line's own look is tested on the real
window in test_queue_line.py.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.ui.main_window import MainWindow  # noqa: E402


def window():
    w = SimpleNamespace(index_queue=[], _pending_files=[], is_indexing=True, _start_indexing_path=mock.MagicMock(),
                        _index_individual_files=mock.MagicMock())
    for n in ("queue_label", "queue_items_label", "status_bar"):
        setattr(w, n, mock.MagicMock())
    w._update_queue_ui = lambda: MainWindow._update_queue_ui(w)
    return w


def test_a_dropped_folder_is_queued_then_started():
    w = window()
    pantry = Path(r"C:\tmp\Pantry")
    MainWindow._add_to_index_queue(w, pantry)                     # dropped while Lantern Box runs
    assert w.index_queue == [pantry]
    w.queue_label.setVisible.assert_called_with(True)
    w.queue_label.setText.assert_called_with("📋 Queued, starts when this one finishes: Pantry")
    # (the 300 ms "start next" timer runs at once here: pumping Qt's event loop in a test also finishes deleting
    #  other tests' leftovers - a still-running thread among them aborts the whole run)
    with mock.patch("app.ui.main_window.QTimer.singleShot", lambda ms, fn: fn()):
        assert MainWindow._start_next_queued(w) is True           # Lantern Box finished
    w._start_indexing_path.assert_called_once_with(pantry, user_instructions=None)
    assert w.index_queue == [] and w.queue_label.setVisible.call_args.args == (False,)
