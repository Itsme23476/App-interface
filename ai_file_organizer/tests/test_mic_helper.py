"""mic_helper on Windows. The Mac test drives fake helper CHILD processes (every block reaches the parent, a wedged
child is killed, a warm spare is reused) — Windows has no helper process (the hands-free mic is a QAudioSource in
this process), so this checks what Windows has instead, with NO real microphone (QAudioSource / QMediaDevices are
faked):
  · busy() / the foreground hooks / foreground_begin's release(): the hooks run BEFORE the capture counts as busy (the
    hands-free listener lets go first); release is safe twice; overlapping captures; a failing hook; many threads
  · open_mic: the listener's own mic (background) never marks the mic busy; a foreground one does, until it stops; a
    mic that can't open releases what it took
  · _Mic: 16 kHz int16 mono, 100 ms blocks (a half sample carried over), stop() delivers what's buffered then closes
    (safe twice, nothing after), a device that stops by itself → on_end(1) + released; no mic / no access → errors
Run: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_mic_helper.py -q"""
import enum
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from PySide6 import QtMultimedia  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.core import mic_helper  # noqa: E402

BLOCK = mic_helper.CHUNK * 2                 # bytes in one 100 ms block


class _St(enum.Enum):
    ActiveState = 0
    StoppedState = 2


class _Err(enum.Enum):
    NoError = 0
    IOError = 2


class FakeIO(QObject):
    readyRead = Signal()

    def __init__(self):
        super().__init__()
        self.buf = bytearray()

    def readAll(self):
        b = bytes(self.buf)
        self.buf.clear()
        return b

    def give(self, b: bytes):
        self.buf += b
        self.readyRead.emit()


class FakeSource:
    """QAudioSource stand-in: nothing is recorded — the test hands it bytes."""
    made = []
    give_io = True

    def __init__(self, dev, fmt):
        self.dev, self.fmt = dev, fmt
        self.io, self.st, self.err, self.stopped, self.buffer = FakeIO(), _St.ActiveState, _Err.NoError, 0, None
        FakeSource.made.append(self)

    def setBufferSize(self, n):
        self.buffer = n

    def start(self):
        return self.io if FakeSource.give_io else None

    def stop(self):
        self.stopped += 1
        self.st = _St.StoppedState

    def state(self):
        return self.st

    def error(self):
        return self.err


class FakeDevice:
    def __init__(self, null=False):
        self.null = null

    def isNull(self):
        return self.null

    def description(self):
        return "Fake microphone"


class FakeDevices:
    dev = FakeDevice()

    @staticmethod
    def defaultAudioInput():
        return FakeDevices.dev


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """No real microphone, a clean busy count + hook list (restored after), your settings never written."""
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(mic_helper, "_fg_hooks", [])
    monkeypatch.setattr(mic_helper, "_busy", 0)
    monkeypatch.setattr(QtMultimedia, "QAudioSource", FakeSource)
    monkeypatch.setattr(QtMultimedia, "QMediaDevices", FakeDevices)
    FakeSource.made, FakeSource.give_io, FakeDevices.dev = [], True, FakeDevice()
    yield
    assert mic_helper._busy == 0, "a test left the mic marked busy"


def test_foreground_begin_runs_the_hooks_first_and_counts_busy():
    seen = []
    mic_helper.add_foreground_hook(lambda: seen.append(("hook", mic_helper.busy())))
    assert not mic_helper.busy()
    release = mic_helper.foreground_begin()
    assert seen == [("hook", False)], "the hooks run BEFORE it counts as busy (the listener lets go first)"
    assert mic_helper.busy()
    release()
    assert not mic_helper.busy()
    release()                                                 # safe more than once
    assert not mic_helper.busy() and mic_helper._busy == 0, "never below zero"


def test_overlapping_captures():
    r1 = mic_helper.foreground_begin()
    r2 = mic_helper.foreground_begin()
    assert mic_helper.busy()
    r1()
    r1()                                                      # (a double release doesn't end the other one)
    assert mic_helper.busy(), "the 2nd capture is still running"
    r2()
    assert not mic_helper.busy()


def test_hooks_once_removed_and_a_failing_hook():
    calls = []

    def hook():
        calls.append("a")

    def broken():
        raise RuntimeError("hook failed")
    mic_helper.add_foreground_hook(hook)
    mic_helper.add_foreground_hook(hook)                      # added twice → runs once
    mic_helper.add_foreground_hook(broken)
    mic_helper.add_foreground_hook(lambda: calls.append("c"))
    release = mic_helper.foreground_begin()
    assert calls == ["a", "c"] and mic_helper.busy(), "a failing hook doesn't stop the others (or the capture)"
    release()
    mic_helper.remove_foreground_hook(hook)
    mic_helper.remove_foreground_hook(hook)                   # (removing what isn't there: harmless)
    mic_helper.remove_foreground_hook(lambda: None)
    calls.clear()
    mic_helper.foreground_begin()()
    assert calls == ["c"]


def test_busy_from_many_threads():
    """Shortcuts start their captures on their own threads: the count stays right."""
    go = threading.Event()

    def worker():
        go.wait(2)
        for _ in range(200):
            r = mic_helper.foreground_begin()
            r()
            r()
    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    go.set()
    for t in ts:
        t.join(10)
    assert not any(t.is_alive() for t in ts) and mic_helper._busy == 0 and not mic_helper.busy()


def test_open_mic_background_vs_foreground(monkeypatch):
    made, hooks = [], []

    class FakeMic:
        def __init__(self, on_chunk, on_end=None, release=None):
            self.on_chunk, self.on_end, self.release = on_chunk, on_end, release
            made.append(self)

        def __call__(self):
            if self.release:
                self.release()
    monkeypatch.setattr(mic_helper, "_Mic", FakeMic)
    mic_helper.add_foreground_hook(lambda: hooks.append(1))
    stop = mic_helper.open_mic(lambda b: None, voice_processing=True, background=True, on_end=print)
    assert made[-1].release is None and not mic_helper.busy() and hooks == [], \
        "the listener's own mic: not a foreground capture (it would pause itself)"
    stop()
    stop = mic_helper.open_mic(lambda b: None)
    assert made[-1].release is not None and mic_helper.busy() and hooks == [1], "a shortcut's mic: busy + hooks"
    stop()
    assert not mic_helper.busy()

    def no_mic(*a, **k):
        raise RuntimeError("No microphone found.")
    monkeypatch.setattr(mic_helper, "_Mic", no_mic)
    with pytest.raises(RuntimeError):
        mic_helper.open_mic(lambda b: None)
    assert not mic_helper.busy(), "a mic that can't open gives back what it took"


def test_mic_blocks_and_stop():
    got, ends = [], []
    stop = mic_helper.open_mic(got.append, voice_processing=True, background=True, on_end=ends.append)
    src = FakeSource.made[-1]
    assert (src.fmt.sampleRate(), src.fmt.channelCount()) == (16000, 1)
    assert src.fmt.sampleFormat() == QtMultimedia.QAudioFormat.SampleFormat.Int16
    assert src.buffer == 16000 and stop.device == "Fake microphone" and stop.proc is None
    src.io.give(b"\x01\x02" * mic_helper.CHUNK + b"\x03")    # one whole block + half a sample
    assert got == [b"\x01\x02" * mic_helper.CHUNK], "100 ms blocks; the half sample waits"
    src.io.give(b"\x04" + b"\x05\x06" * 2000)                # …completed by the next read
    assert len(got) == 2 and got[1][:2] == b"\x03\x04" and len(got[1]) == BLOCK
    assert not mic_helper.busy(), "the listener's mic never marks the mic busy"
    stop()                                                    # Stop: what's buffered is delivered, then it closes
    assert len(got) == 3 and len(got[2]) == 2 + 4000 - BLOCK and src.stopped == 1 and ends == []
    assert not stop._watch.isActive()
    stop()                                                    # (safe twice)
    src.io.give(b"\x07\x08" * mic_helper.CHUNK)
    assert len(got) == 3 and src.stopped == 1, "nothing after stop"
    # a chunk handler that fails doesn't take the mic down
    seen = []

    def flaky(b):
        seen.append(len(b))
        if len(seen) == 1:
            raise ValueError("handler failed")
    stop = mic_helper.open_mic(flaky, background=True)
    io = FakeSource.made[-1].io
    io.give(b"\x00" * BLOCK * 2)
    assert seen == [BLOCK, BLOCK]
    stop()


def test_device_that_stops_by_itself():
    got, ends = [], []
    m = mic_helper.open_mic(got.append, on_end=ends.append)  # (a foreground one: busy while it runs)
    src = FakeSource.made[-1]
    assert mic_helper.busy()
    m._on_state()
    assert ends == [] and mic_helper.busy(), "running: nothing to report"
    src.st, src.err = _St.StoppedState, _Err.IOError         # unplugged / taken away
    m._on_state()
    assert ends == [1] and not mic_helper.busy() and src.stopped == 1 and not m._watch.isActive()
    m._on_state()
    m()
    assert ends == [1], "reported once"
    # a plain stop (no error) isn't "stopped by itself"
    m2 = mic_helper.open_mic(got.append, background=True, on_end=ends.append)
    FakeSource.made[-1].st = _St.StoppedState
    m2._on_state()
    assert ends == [1]
    m2()


def test_no_microphone_or_no_access():
    FakeDevices.dev = FakeDevice(null=True)
    with pytest.raises(RuntimeError, match="No microphone"):
        mic_helper.open_mic(lambda b: None)
    assert not mic_helper.busy()
    FakeDevices.dev = FakeDevice()
    FakeSource.give_io = False
    with pytest.raises(RuntimeError, match="Could not access"):
        mic_helper.open_mic(lambda b: None)
    assert not mic_helper.busy(), "released when the mic can't start"
    with pytest.raises(RuntimeError):
        mic_helper.open_mic(lambda b: None, background=True)
