"""Analyze Files: starting a new analysis while one runs must never drop the running worker (Qt aborts the app:
"QThread: Destroyed while thread is still running"); files dropped meanwhile wait their turn; a folder queued
during a file batch starts after it (it used to wait for good). Ported from the Mac (pack §6.1, item 1).
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SWAP = textwrap.dedent("""
    import gc, os, sys, tempfile, time
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, {root!r})
    from pathlib import Path
    from types import SimpleNamespace as NS
    from PySide6.QtCore import QCoreApplication, QThread
    app = QCoreApplication([])
    from app.core import settings as S
    _tmp = Path(tempfile.mkdtemp(prefix="filect-swap-"))
    S.Settings.get_app_data_dir = lambda self: _tmp          # (never your folder)
    S.Settings._save_config = lambda self, *a, **k: None
    from app.ui.main_window import MainWindow

    class Slow(QThread):
        def run(self):
            time.sleep(1.0)

    mw = NS(index_worker=Slow())
    mw.index_worker.start()
    time.sleep(0.1)
    if {use_fix}:
        MainWindow._keep_running_worker(mw)
    mw.index_worker = None                    # what starting the next analysis did to the running one
    gc.collect()
    time.sleep(1.3)
    print("survived")
""")


def _swap(use_fix: bool):
    code = SWAP.format(root=str(ROOT), use_fix=use_fix)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=90)


def test_replacing_a_running_worker_no_longer_aborts():
    old = _swap(False)
    assert old.returncode != 0 and "survived" not in old.stdout, ("the old pattern should abort", old.returncode,
                                                                  old.stderr[-300:])
    new = _swap(True)
    assert new.returncode == 0 and "survived" in new.stdout, (new.returncode, new.stderr[-500:])


def _stand_in():
    from app.ui.main_window import MainWindow
    calls, msgs = [], []
    mw = NS(is_indexing=False, index_queue=[], index_worker=None,
            status_bar=NS(showMessage=lambda m, *a: msgs.append(m)), _update_queue_ui=lambda: None)
    mw._index_individual_files = lambda files, user_instructions=None: calls.append(("files", list(files)))
    mw._start_indexing_path = lambda p, user_instructions=None: calls.append(("folder", p))
    mw.index_path, mw.index_label, mw.index_button_action = None, NS(setText=lambda t: None), NS(setEnabled=lambda b: None)
    mw._add_to_index_queue = lambda p, user_instructions=None: (
        mw.index_queue.append(p),
        user_instructions and mw.__dict__.setdefault("_queue_instructions", {}).__setitem__(str(p), user_instructions))
    for name in ("_start_next_queued", "_index_files_or_queue", "_keep_running_worker", "_index_confirmed"):
        setattr(mw, name, getattr(MainWindow, name).__get__(mw))
    return mw, calls, msgs


def _now(monkeypatch):
    """The 300 ms "start next" timer runs at once (no Qt event loop in these tests: pumping it also finishes
    deleting other tests' leftovers, and a still-running thread among them aborts the whole run)."""
    import app.ui.main_window as mwm
    monkeypatch.setattr(mwm.QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))


def test_files_dropped_while_busy_wait_their_turn(monkeypatch):
    _now(monkeypatch)
    mw, calls, msgs = _stand_in()
    mw._index_files_or_queue(["/a.mp4"])                       # idle: starts at once
    assert calls == [("files", ["/a.mp4"])], calls
    calls.clear()
    mw.is_indexing = True
    mw._index_files_or_queue(["/b.mp4", "/c.pdf"])
    mw._index_files_or_queue(["/c.pdf", "/d.jpg"])             # (a file dropped twice waits once)
    assert calls == [] and mw._pending_files == ["/b.mp4", "/c.pdf", "/d.jpg"], (calls, mw._pending_files)
    assert "to the queue" in msgs[-1]
    mw.index_queue = [Path("/Folder")]
    assert mw._start_next_queued() is True                     # queued folders first...
    assert calls == [("folder", Path("/Folder"))], calls
    assert mw._start_next_queued() is True                     # ...then the waiting files
    assert mw._start_next_queued() is False
    assert calls == [("folder", Path("/Folder")), ("files", ["/b.mp4", "/c.pdf", "/d.jpg"])], calls


def test_a_folder_queued_during_a_file_batch_starts_after_it(monkeypatch):
    """_on_individual_files_done used to just stop: the folder dropped while the files ran waited for good."""
    from app.ui.main_window import MainWindow
    _now(monkeypatch)
    mw, calls, _msgs = _stand_in()
    mw.index_queue = [Path("/Pantry")]
    mw.refresh_debug_view = lambda: None
    MainWindow._on_individual_files_done(mw, {"indexed": 3})
    assert calls == [("folder", Path("/Pantry"))], calls


def _recording(mw, calls):
    """The stand-in, but recording the instructions each job starts with."""
    mw._index_individual_files = lambda files, user_instructions=None: calls.append(("files", list(files),
                                                                                    user_instructions))
    def start_folder(p, user_instructions=None):             # (like the real one: busy from its first line)
        mw.is_indexing = True
        calls.append(("folder", p, user_instructions))
    mw._start_indexing_path = start_folder


def test_a_folder_and_loose_files_dropped_together_are_all_analyzed(monkeypatch):
    """"Index 1 folder and 3 files?" -> Index Now used to analyze only the folder: the 3 files were dropped."""
    _now(monkeypatch)
    mw, calls, _msgs = _stand_in()
    _recording(mw, calls)
    ask = "Tag every file 'mixed-drop'"
    mw._index_confirmed([Path("/Taxes")], ["/a.pdf", "/b.pdf", "/c.pdf"], ask)   # (the folder starts; files wait)
    assert calls == [("folder", Path("/Taxes"), ask)], calls
    assert mw._pending_files == ["/a.pdf", "/b.pdf", "/c.pdf"], mw._pending_files
    assert mw._start_next_queued() is True                     # the folder finished -> the files, same instructions
    assert calls[-1] == ("files", ["/a.pdf", "/b.pdf", "/c.pdf"], ask), calls
    assert mw._start_next_queued() is False


def test_only_files_still_start_at_once():
    mw, calls, _msgs = _stand_in()
    _recording(mw, calls)
    mw._index_confirmed([], ["/a.pdf"], None)
    assert calls == [("files", ["/a.pdf"], None)], calls


def test_whatever_waits_keeps_the_instructions_typed_for_it(monkeypatch):
    """Queued folders and waiting files used to lose the custom instructions (\"current batch only\")."""
    _now(monkeypatch)
    mw, calls, _msgs = _stand_in()
    _recording(mw, calls)
    mw.is_indexing = True                                      # something is running
    mw._index_confirmed([Path("/Pantry")], ["/x.mp4"], "tag A")
    mw._index_files_or_queue(["/y.wav"], user_instructions="tag B")   # a second drop, other instructions
    assert calls == []
    assert mw._start_next_queued() and calls[-1] == ("folder", Path("/Pantry"), "tag A"), calls
    assert mw._start_next_queued() and calls[-1] == ("files", ["/x.mp4"], "tag A"), calls
    assert mw._start_next_queued() and calls[-1] == ("files", ["/y.wav"], "tag B"), calls
    assert mw._start_next_queued() is False
