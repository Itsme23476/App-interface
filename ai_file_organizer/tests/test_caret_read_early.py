"""The text before the cursor is read from the moment you let go of the dictation keys, not when the words arrive.
Found in the live rerun (2026-11-13): the third dictation's read ran out of its 0.4 s (logged "unreadable"), so no
space went before it ("…gmail.com.Send it to…") and it was pasted 0.4 s later. Now the read starts at key release (the
text before the cursor can't change while Filect transcribes), runs while the words are being finished, and has 0.8 s
from the release. A stand-in UI Automation read; nothing is read from your screen."""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.ui import win_hotkey  # noqa: E402


def test_the_read_runs_from_when_it_starts():
    def slow(n):
        time.sleep(0.3)
        return "This is a timing test."
    with mock.patch.object(win_hotkey, "_uia_text_before_caret", slow), mock.patch("os.getpid", lambda: -1):
        job = win_hotkey.start_text_before_caret()
        assert job.started <= time.perf_counter()
        time.sleep(1.0)                               # (meanwhile: the words are being finished. 0.35 s - a 50 ms
                                                      #  margin over the 0.3 s read - flaked once in a loaded suite)
        t0 = time.perf_counter()
        assert job(0.0) == "This is a timing test."  # ready - no waiting left
        assert time.perf_counter() - t0 < 0.05
        late = win_hotkey.start_text_before_caret()
        assert late(0.05) is None                    # (not ready within the time given: unreadable, as before)
        assert win_hotkey.text_before_caret(timeout=0.5) == "This is a timing test."


def _controller():
    from app.ui.dictation import DictationController
    pasted = []
    c = SimpleNamespace(_overlay=mock.MagicMock(), state_changed=mock.MagicMock(), transcript_ready=mock.MagicMock(),
                        _mode="dictate", _recorder=mock.MagicMock(), _custom_words=lambda: [],
                        _paste=lambda t: pasted.append(t), _save_history=lambda *a: None)
    return DictationController, c, pasted


def test_letting_go_starts_the_read_and_the_words_use_it():
    D, c, pasted = _controller()
    from app.core.settings import settings
    job = mock.MagicMock(return_value="This is a timing test.")
    job.started = time.perf_counter()
    with mock.patch("app.ui.win_hotkey.start_text_before_caret", lambda *a, **k: job) as _, \
            mock.patch("app.ui.win_hotkey.text_before_caret", lambda *a, **k: None), \
            mock.patch.object(settings, "dictation_polish_level", "none"), \
            mock.patch.object(settings, "live_dictation_custom_terms", lambda: []), \
            mock.patch("app.core.supabase_client.track", lambda *a, **k: None):
        D._on_dictate_stop(c)                         # you let go of the keys
        assert c._caret is job
        D._on_finished(c, "my email is john.")       # (a fresh read now would come back unreadable)
    assert pasted == [" My email is john."], pasted
    assert job.call_count == 1 and 0 < job.call_args.args[0] <= 0.8, job.call_args
    assert c._caret is None, "used once"


def test_a_new_dictation_never_uses_the_last_ones_read():
    D, c, _ = _controller()
    c._caret = mock.MagicMock()
    c.organize_controller = None
    c._overlay = mock.MagicMock()
    c.level = mock.MagicMock()
    c._recorder = None
    c._on_finished = c._on_error = lambda *a: None
    with mock.patch("app.core.transcription.StreamingTranscriber"):
        D._on_dictate_start(c, "dictate")
    assert c._caret is None
