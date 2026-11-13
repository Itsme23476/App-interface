"""Analyze Files → More Options → Watch for New Downloads (Windows port of the Mac's test, 694b1b6; live test
2026-11-13): a new note in a watched folder was stored with no tags and none of its text - the watcher had its own
old pipeline where only photos and PDFs were read, nothing was checked against the monthly limit and nothing was
counted. Now each new file goes through search_service.index_single_file, like Analyze Files, with the watched
folder's own instructions. Real worker code; nothing is analyzed (the analyzer is a stand-in)."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.ui import main_window as mwm  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="filect-watch-"))


def worker_run(items, results):
    """Run the real AutoIndexWorker loop on (file, instructions) items; → (calls made, statuses sent to the UI)."""
    w = mwm.AutoIndexWorker()
    got = []
    w.file_indexed.connect(lambda name, status: got.append((name, status)))
    w._queue = list(items)
    with mock.patch.object(mwm.search_service, "index_single_file", side_effect=lambda p, **k: results[p]) as isf:
        w.run()                                                   # (in this thread, so the signals arrive at once)
    return [(c.args[0], c.kwargs.get("user_instructions")) for c in isf.call_args_list], got


def test_a_new_file_goes_through_the_main_analyzer():
    note, photo, full = TMP / "flour-order.txt", TMP / "parrot.jpg", TMP / "owl.jpg"
    for f in (note, photo, full):
        f.write_text("x")
    calls, got = worker_run([(note, None), (photo, "tag birds"), (full, None)],
                            {note: {'success': True}, photo: {'skipped': True},
                             full: {'error': 'Monthly limit reached', 'limit_reached': True}})
    assert calls == [(note, None), (photo, "tag birds"), (full, None)], calls
    assert got == [("flour-order.txt", "success"), ("parrot.jpg", "skipped"), ("owl.jpg", "error")], got


def test_a_crash_in_one_file_doesnt_stop_the_others():
    a, b = TMP / "a.txt", TMP / "b.txt"
    for f in (a, b):
        f.write_text("x")

    def boom(p, **k):
        if p == a:
            raise RuntimeError("disk gone")
        return {'success': True}
    w = mwm.AutoIndexWorker()
    got = []
    w.file_indexed.connect(lambda name, status: got.append((name, status)))
    w._queue = [(a, None), (b, None)]
    with mock.patch.object(mwm.search_service, "index_single_file", side_effect=boom):
        w.run()
    assert got == [("a.txt", "error"), ("b.txt", "success")], got
