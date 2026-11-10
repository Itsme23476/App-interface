"""Mic coordination + the hands-free listener's microphone (Windows port of the Mac app's mic_helper.py).

Two jobs, the same names as on the Mac:

1. **Foreground captures** — dictation, Shift+Z (the agent voice key), the Agents page / Organize mic — call
   `foreground_begin()` right BEFORE they open the mic and the returned `release()` when they're done.
   Meanwhile `busy()` is True, and the hooks added with `add_foreground_hook()` run first: Filect Assistant
   (hands-free) lets go of the mic at once and pauses ("Paused while you use a Filect shortcut"), so what you
   dictate is never ALSO sent as a hands-free request.

2. **`open_mic()`** — the always-on listener's mic: QtMultimedia `QAudioSource` at 16 kHz int16 mono, in this
   process. (Mac: a child process with Apple's voice processing, because PortAudio there can deadlock in
   Pa_StopStream. Windows uses QtMultimedia, like dictation — PortAudio has no ARM64 binary on the dev PC, and
   QAudioSource has never hung here.) Qt has no echo cancellation / noise suppression, so `voice_processing` is
   accepted and ignored: the listener runs the way the Mac runs with its noise filter OFF (learned noise floor;
   the mic ignored while Filect speaks).
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK = 1600            # samples per delivered block (100 ms)
VP_UNAVAILABLE = 4      # (Mac: voice processing couldn't start) — never returned on Windows
VP_STALLED = 5

_busy = 0               # foreground captures running — hands-free pauses meanwhile
_busy_lock = threading.Lock()
_fg_hooks = []          # called right BEFORE a foreground capture starts (any thread)


def busy() -> bool:
    """Is a foreground capture (dictation, Shift+Z, a mic button) running?"""
    return _busy > 0


def add_foreground_hook(fn) -> None:
    """fn() runs (on the capturing thread) right before any foreground capture starts — the hands-free listener
    uses it to let go of the mic."""
    if fn not in _fg_hooks:
        _fg_hooks.append(fn)


def remove_foreground_hook(fn) -> None:
    try:
        _fg_hooks.remove(fn)
    except ValueError:
        pass


def foreground_begin():
    """A Filect shortcut / mic button is about to record: tell the hooks, count it as busy. Returns release()
    (safe to call more than once)."""
    global _busy
    for fn in list(_fg_hooks):
        try:
            fn()
        except Exception:
            pass
    with _busy_lock:
        _busy += 1
    done = [False]

    def release():
        global _busy
        with _busy_lock:
            if not done[0]:
                done[0] = True
                _busy = max(0, _busy - 1)
    return release


class _Mic:
    """The listener's QAudioSource. Lives on the thread that opened it (the GUI thread: QtMultimedia needs its
    event loop). Delivers 100 ms blocks; tells `on_end` if the device stops by itself (unplugged, taken away)."""

    def __init__(self, on_chunk, on_end=None, release=None):
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
        self._on_chunk, self._on_end, self._release = on_chunk, on_end, release
        self._stopped = False
        self._rest = b""
        self.proc = None                     # (the Mac's child process — none here)
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Int16)
        dev = QMediaDevices.defaultAudioInput()
        if dev is None or dev.isNull():
            raise RuntimeError("No microphone found.")
        self.device = dev.description()
        self._src = QAudioSource(dev, fmt)
        self._src.setBufferSize(SAMPLE_RATE * 2 // 2)           # 0.5 s of slack if the UI is briefly busy
        self._io = self._src.start()
        if self._io is None:
            raise RuntimeError("Could not access the microphone.")
        self._io.readyRead.connect(self._read)
        # A device that stops by itself (unplugged, taken away): PySide 6.11 can't deliver stateChanged to Python at
        # all ("QAudio::State cannot be converted", even into a lambda) — so it's checked twice a second instead.
        from PySide6.QtCore import QTimer
        self._watch = QTimer()
        self._watch.setInterval(500)
        self._watch.timeout.connect(self._on_state)
        self._watch.start()
        logger.info("[mic] listening on %s", self.device)

    def _read(self):
        if self._stopped:
            return
        try:
            data = self._rest + bytes(self._io.readAll())
        except Exception:
            return
        n = len(data) - len(data) % 2
        self._rest = data[n:]
        data = data[:n]
        step = CHUNK * 2
        while len(data) >= step:
            self._deliver(data[:step])
            data = data[step:]
        if data:
            self._rest = data + self._rest

    def _deliver(self, b: bytes):
        try:
            self._on_chunk(b)
        except Exception as e:
            logger.warning("[mic] chunk handler failed: %s", e)

    def _on_state(self):
        if self._stopped:
            return
        st = self._src.state()
        if st == type(st).StoppedState:                  # (compare within its own enum — QtAudio vs QAudio)
            err = self._src.error()
            if err != type(err).NoError:
                logger.warning("[mic] stopped by itself (%s)", err)
                self._finish(rc=1)

    def _finish(self, rc=None):
        if self._stopped:
            return
        self._stopped = True
        try:
            self._watch.stop()
        except Exception:
            pass
        try:
            self._src.stop()
        except Exception:
            pass
        if self._release is not None:
            self._release()
        if rc is not None and self._on_end is not None:
            try:
                self._on_end(rc)
            except Exception:
                pass

    def __call__(self):
        """stop(): deliver what's buffered, then close the mic."""
        if self._stopped:
            return
        try:
            self._read()
            if self._rest:
                self._deliver(self._rest)
                self._rest = b""
        except Exception:
            pass
        self._finish()


def open_mic(on_chunk, voice_processing: bool = False, background: bool = False, on_end=None):
    """Start capturing (call on the GUI thread). on_chunk(bytes) runs on the GUI thread for every 100 ms block.
    Returns stop() — it delivers what's already captured, then closes the mic. `background`: don't count as a
    foreground capture (the hands-free listener's own mic). on_end(returncode): the device stopped by itself.
    `voice_processing` is ignored on Windows (see the module docstring)."""
    release = None
    if not background:
        release = foreground_begin()
    try:
        return _Mic(on_chunk, on_end=on_end, release=release)
    except Exception:
        if release is not None:
            release()
        raise
