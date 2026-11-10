"""Windows port of the Mac app's live_listener.py (Filect Assistant, hands-free). The pure parts —
UtteranceDetector, ChordTap, CtrlTap and LiveListener's logic — are the Mac's, unchanged. Windows pieces: the mic
(mic_helper: QAudioSource in-process; no echo cancellation → `echo_cancelled` is False and the page mutes the mic
while Filect speaks), who else uses the mic (Windows' privacy record — the taskbar mic icon's), the lock check
(OpenInputDesktop), and Esc / Shift+Ctrl / Ctrl taps from an observe-only low-level hook on its own thread.

Filect Assistant — hands-free listening (phase 1).

Start (in the Assistant tab) → Filect listens. Whatever you say, once you've stopped talking for "send
after" seconds, is transcribed and handed to the Assistant exactly like an fn + ⌃ request (router →
Claude / Codex → the notch card). What keeps it from picking up the wrong things:
  1. Apple's voice processing in the mic child (mic_helper "go-vp"): background noise and whatever the
     Mac itself plays are removed. It filters the mic for EVERY app while it runs (measured), so the
     listener lets go of the mic the moment anything else records — a Filect hotkey instantly (via
     mic_helper's foreground hook), another app (Zoom, FaceTime, a screen recorder) within ~1 s (via
     CoreAudio's per-process "is capturing input" flag) — and comes back after.
  2. Speech detection (webrtcvad) + a loudness gate: it only starts on ~0.2 s of clear speech that is
     loud enough — you near the mic, not someone across the room.
  3. The router's IGNORE (AssistantPage.run_voice_task, source="live"): speech that isn't a request is
     dropped.
Only speech is sent for transcription (never the silence), so only that counts toward the daily limit.
"""
from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import struct
import sys
import threading
import time
from collections import deque
from statistics import median

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger("ai_file_organizer")

RATE = 16000
FRAME = 480                 # 30 ms of 16 kHz audio — webrtcvad's frame
FRAME_SEC = FRAME / RATE
BYTES_PER_SEC = RATE * 2

# sensitivity → (webrtcvad aggressiveness, loudness floor (RMS 0..1), share of YOUR usual loudness a
# sound must reach to count). ponytail: hand-tuned heuristics; if real rooms trip them, swap in a speech
# model (Silero VAD) + per-user calibration.
SENSITIVITY = {
    "low":    (3, 0.006, 0.40),     # ignores more: only clear, close speech
    "medium": (2, 0.003, 0.25),
    "high":   (1, 0.0015, 0.12),    # picks up softer / farther speech
}
START_WINDOW, START_NEEDED = 10, 7     # ≥ 7 of the last 10 frames clear speech (~0.2 s) = you started
RESUME_WINDOW, RESUME_NEEDED = 6, 4    # after a pause, ~0.12 s of clear speech = you went on
GAP_SEC = 0.5               # this much non-speech = a pause: audio stops being sent, the countdown runs
PREROLL_SEC = 0.4           # audio kept from just before speech starts (no clipped first word)
MIN_SPEECH_SEC = 0.6        # less speech than this in total = a cough / "uh" → dropped (never even sent)
MIN_ANSWER_SEC = 0.25       # …unless the agent just asked you something: a short "yes" / "no" counts
# The room's own noise level (measured live): speech must stand clearly above it. Without Apple's filter
# a real room measured ~0.012 RMS and webrtcvad called 80% of it "speech" — so a request never ended.
NOISE_SEC = 5.0             # how much quiet audio the noise estimate looks at
NOISE_WARMUP_SEC = 1.0      # nothing starts until it has heard the room this long
NOISE_START = 4.0           # to start: ≥ 4× the noise floor (+12 dB)
NOISE_CONT = 3.0            # to count as still talking: ≥ 3× (+10 dB)
QUIET_FRAC = 0.3            # "you paused" = under 30% speech in the last GAP_SEC (noise has stray hits)
MIN_WORDS = 2               # a one-word transcript ("okay", "yeah") isn't a request
QUICK_SEND_SEC = 0.8        # a FINISHED sentence (it ends in . ? ! and its text has held still QUICK_STABLE_SEC)
QUICK_STABLE_SEC = 0.3      # goes after this much quiet instead of waiting out "send after" — trailing off
                            # mid-sentence still waits the full time
MAX_UTTERANCE_SEC = 90      # runaway guard (a TV left on): it ends here
# …and the other way round (Windows, 2026-11-09 — "Rename the folder on my desktop called… [pause] …test stuff" went as
# two requests, and the half sentence started a task): what the speech service hasn't closed with . ? ! waits longer
# before it's sent — ×2 "send after"; ×3 when it plainly hangs ("…called", "…the", "…and", a comma). Never over 10 s.
UNFINISHED_X = 2.0
DANGLING_X = 3.0
UNFINISHED_MAX_SEC = 10.0
_DANGLING = {"a", "an", "the", "and", "or", "but", "so", "to", "of", "for", "in", "on", "at", "by", "with", "from",
             "into", "onto", "about", "called", "named", "titled", "my", "your", "our", "their", "his", "her", "its",
             "this", "that", "these", "those", "which", "who", "where", "when", "because", "if", "then", "than",
             "like", "is", "are", "was", "were", "be", "as", "also", "plus", "via", "per", "some", "any", "every",
             "each", "all", "please", "can", "could", "would", "should", "will", "make", "put", "move", "add"}


def unfinished_wait(text: str, send_after: float) -> float | None:
    """How long to wait for the rest of `text` (seconds), or None when it isn't unfinished: text the speech service
    closed (. ? ! …), or under two words, goes as usual."""
    t = (text or "").strip().rstrip("\"'”’)]")
    if len(t.split()) < MIN_WORDS or t[-1:] in (".", "?", "!", "…"):
        return None
    last = t.split()[-1].lower().strip(",;:-–—")
    hangs = t[-1:] in (",", ";", ":", "-", "–", "—") or last in _DANGLING
    want = max(send_after * (DANGLING_X if hangs else UNFINISHED_X), send_after + (3.0 if hangs else 1.5))
    want = min(UNFINISHED_MAX_SEC, want)
    return want if want > send_after + 0.05 else None


def _rms(frame: bytes) -> float:
    import numpy as np
    x = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
    return float(math.sqrt(float(np.mean(x * x)))) if len(x) else 0.0


class UtteranceDetector:
    """Pure (no Qt, no mic): 16 kHz int16 audio in → events out. Time is counted in frames, so it's
    exact and testable. Events, in order:
      ("start", preroll_bytes)  you started talking — send this audio (incl. just before you started)
      ("audio", bytes)          more of your speech — send it
      ("pause",)                you paused: stop sending; the "send after" countdown runs
      ("resume",)               you went on before it ran out (the ("audio") after it continues)
      ("end", stats)            you finished → transcribe what was sent; stats: speech_sec, level, secs
      ("drop", stats)           too short to be a request (a cough, "uh") → throw it away
    """

    def __init__(self, send_after: float = 2.0, sensitivity: str = "medium", vad=None):
        self.send_after = float(send_after)
        self._vad_override = vad
        self.set_sensitivity(sensitivity)
        self._refs = deque(maxlen=5)
        self.ref = None                 # your usual speech loudness, learned from real requests
        self.min_speech = MIN_SPEECH_SEC  # the listener lowers it while an answer is expected
        self.reset_noise()
        self.reset()

    def reset_noise(self) -> None:
        """A (re)opened mic — maybe a different one (filter on/off): learn the room again."""
        self._noise = deque(maxlen=int(round(NOISE_SEC / FRAME_SEC)))

    def noise_floor(self) -> float:
        """The room's quiet level: the 20th percentile of recent non-speech-time frames."""
        if len(self._noise) < 10:
            return 0.0
        import numpy as np
        return float(np.percentile(np.fromiter(self._noise, dtype=np.float32), 20))

    def _warm(self) -> bool:
        return len(self._noise) * FRAME_SEC >= NOISE_WARMUP_SEC

    def set_sensitivity(self, sensitivity: str) -> None:
        mode, self.floor, self.ratio = SENSITIVITY.get(sensitivity, SENSITIVITY["medium"])
        self.sensitivity = sensitivity if sensitivity in SENSITIVITY else "medium"
        if self._vad_override is not None:
            self.vad = self._vad_override
            return
        try:
            import webrtcvad
            self.vad = webrtcvad.Vad(mode)
        except Exception as e:          # no webrtcvad → the built-in spectral check (Windows; still gated by IGNORE)
            logger.warning("[live] webrtcvad unavailable (%s) — using the built-in speech check", e)
            self.vad = SpectralVad(mode)

    def learn(self, level: float) -> None:
        """A request that turned out real: remember how loud YOU are (the gate is relative to it)."""
        if level and level > 0:
            self._refs.append(float(level))
            self.ref = float(median(self._refs))

    def threshold(self) -> float:
        return max(self.floor, self.ratio * self.ref) if self.ref else self.floor

    def suppress(self) -> None:
        """Esc: drop what you're saying — the REST of that sentence too: nothing starts until you've
        stopped talking (GAP_SEC of non-speech)."""
        keep = (self.ref, self._refs, self._t)
        self.reset()
        self.ref, self._refs, self._t = keep
        self.state = "suppressed"
        self._quiet = 0

    def reset(self) -> None:
        self.state = "idle"             # idle | speaking | trailing | suppressed
        self._rem = b""
        self._pre = deque(maxlen=int(round(PREROLL_SEC / FRAME_SEC)))
        self._win = deque(maxlen=START_WINDOW)
        self._recent = deque(maxlen=int(round(GAP_SEC / FRAME_SEC)))
        self._t = 0                     # frames seen (the clock)
        self._started = self._last_speech = 0
        self._speech_frames = 0
        self._levels = []
        self.last_level = 0.0

    def speech_sec(self) -> float:
        """How much clear speech the request being heard has so far."""
        return self._speech_frames * FRAME_SEC if self.state in ("speaking", "trailing") else 0.0

    def countdown_left(self) -> float | None:
        if self.state != "trailing":
            return None
        return max(0.0, self.send_after - (self._t - self._last_speech) * FRAME_SEC)

    def _frame(self, f: bytes):
        rms = _rms(f)
        if rms < self.floor * 0.5:
            return rms, False
        if self.vad is None:
            return rms, rms >= self.floor
        try:
            return rms, bool(self.vad.is_speech(f, RATE))
        except Exception:
            return rms, rms >= self.floor

    def _stats(self) -> dict:
        return {"speech_sec": round(self._speech_frames * FRAME_SEC, 2),
                "level": round(median(self._levels), 5) if self._levels else 0.0,
                "secs": round((self._last_speech - self._started) * FRAME_SEC, 2)}

    def _finish(self, ev: list, forced: bool = False) -> None:
        st = self._stats()
        st["forced"] = forced
        ev.append(("end" if st["speech_sec"] >= self.min_speech else "drop", st))
        keep_ref, keep_t = (self.ref, self._refs), self._t
        self.reset()
        self.ref, self._refs = keep_ref
        self._t = keep_t

    def feed(self, pcm: bytes) -> list:
        ev, out = [], []

        def flush():
            if out:
                ev.append(("audio", b"".join(out)))
                out.clear()

        data = self._rem + pcm
        n = (len(data) // (FRAME * 2)) * FRAME * 2
        self._rem = data[n:]
        for i in range(0, n, FRAME * 2):
            f = data[i:i + FRAME * 2]
            self._t += 1
            rms, voiced = self._frame(f)
            self.last_level = rms
            if self.state != "speaking":            # learn the room only when you're not talking
                self._noise.append(rms)
            nf = self.noise_floor()
            start_thr = max(self.threshold(), NOISE_START * nf)
            cont_thr = max(self.threshold() * 0.5, NOISE_CONT * nf)
            if self.state == "suppressed":
                self._quiet = 0 if (voiced and rms >= cont_thr) else self._quiet + 1
                if self._quiet * FRAME_SEC >= GAP_SEC:
                    self.state = "idle"
                continue
            if self.state == "idle":
                self._pre.append(f)
                self._win.append(voiced and rms >= start_thr and self._warm())
                if sum(self._win) >= START_NEEDED:
                    self.state = "speaking"
                    self._started = self._last_speech = self._t
                    self._speech_frames = sum(self._win)
                    self._levels = [rms]
                    self._recent.clear()
                    self._recent.extend([True] * len(self._win))
                    ev.append(("start", b"".join(self._pre)))
                    self._pre.clear()
                    self._win = deque(maxlen=RESUME_WINDOW)
            elif self.state == "speaking":
                out.append(f)
                talking = voiced and rms >= cont_thr
                self._recent.append(talking)
                if talking:
                    self._last_speech = self._t
                    self._speech_frames += 1
                    self._levels.append(rms)
                full = len(self._recent) == self._recent.maxlen
                if ((self._t - self._last_speech) * FRAME_SEC >= GAP_SEC
                        or (full and sum(self._recent) < QUIET_FRAC * self._recent.maxlen)):
                    flush()
                    self.state = "trailing"
                    self._pre.clear()
                    self._win.clear()
                    ev.append(("pause",))
                if self.state == "speaking" and (self._t - self._started) * FRAME_SEC >= MAX_UTTERANCE_SEC:
                    flush()
                    self._finish(ev, forced=True)
            elif self.state == "trailing":
                self._pre.append(f)
                self._win.append(voiced and rms >= start_thr)
                if sum(self._win) >= RESUME_NEEDED:
                    self.state = "speaking"
                    self._last_speech = self._t
                    self._recent.clear()
                    self._recent.extend([True] * RESUME_NEEDED)
                    ev.append(("resume",))
                    out.append(b"".join(self._pre))   # the start of what you said next
                    self._pre.clear()
                    self._win.clear()
                elif (self._t - self._last_speech) * FRAME_SEC >= self.send_after:
                    self._finish(ev)
        flush()
        return ev


# ---- the speech check when webrtcvad isn't installed (Windows dev PC) -----------------------------------
class SpectralVad:
    """A small speech / not-speech call per 30 ms frame, for when webrtcvad isn't there: voice puts most of its
    energy in 250–3800 Hz in a few strong bands (not flat like hiss / fans), with a moderate zero-crossing rate.
    The loudness gates (the room's noise floor ×4, your usual level) still decide first — this only stops steady
    noise and clicks from counting as talking. `mode` 1–3 like webrtcvad (3 = strictest)."""

    HANG = 4                    # a speech frame keeps the next 4 (120 ms) counted as speech — like webrtcvad's own
                                #   hangover: the soft ends of syllables don't make a talker "pause"

    def __init__(self, mode: int = 2):
        self.mode = int(mode)
        self._band = {1: 0.68, 2: 0.75, 3: 0.82}.get(self.mode, 0.75)
        self._flat = {1: 0.55, 2: 0.48, 3: 0.40}.get(self.mode, 0.48)
        self._hang = 0

    def is_speech(self, frame: bytes, rate: int = RATE) -> bool:
        voiced = self._check(frame, rate)
        if voiced:
            self._hang = self.HANG
            return True
        if self._hang > 0:
            self._hang -= 1
            return True
        return False

    def _check(self, frame: bytes, rate: int) -> bool:
        import numpy as np
        x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        if len(x) < 64:
            return False
        x = x - x.mean()
        e = float(np.dot(x, x))
        if e <= 1e-3:
            return False
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2 + 1e-9
        freqs = np.fft.rfftfreq(len(x), 1.0 / rate)
        band = spec[(freqs >= 80) & (freqs <= 4000)]        # (voices: the fundamental starts ~85 Hz)
        frac = float(band.sum() / spec.sum())
        flat = float(np.exp(np.mean(np.log(band))) / np.mean(band))
        zcr = float(np.mean(np.abs(np.diff(np.sign(x))) > 0))
        return frac >= self._band and flat <= self._flat and 0.01 <= zcr <= 0.5


# ---- who else is using the mic: Windows' own record (the taskbar mic icon reads the same place) ------------
_MIC_STORE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
_FRIENDLY = {"zoom": "Zoom", "ms-teams": "Teams", "teams": "Teams", "discord": "Discord", "slack": "Slack",
             "skype": "Skype", "obs64": "OBS", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox",
             "webex": "Webex", "ciscowebexstart": "Webex", "audacity": "Audacity", "whatsapp": "WhatsApp",
             "windowssoundrecorder": "Sound Recorder", "soundrecorder": "Sound Recorder", "code": "VS Code"}
IGNORED_MIC_USERS = set()


def _app_name(key: str) -> str:
    """"C:#Program Files#Zoom#bin#Zoom.exe" → "Zoom"; "Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe" → "Sound Recorder"."""
    if "#" in key:
        stem = key.replace("#", "\\").rsplit("\\", 1)[-1]
        stem = stem[:-4] if stem.lower().endswith(".exe") else stem
    else:
        stem = key.split("_", 1)[0].rsplit(".", 1)[-1]
    return _FRIENDLY.get(stem.lower(), stem[:1].upper() + stem[1:])


def _own_paths() -> set:
    """This process's exe (and, for a dev run, the Python it really runs on) — never "another app"."""
    out = set()
    for p in (sys.executable, getattr(sys, "_base_executable", ""), os.path.join(sys.base_prefix, "python.exe"),
              os.path.join(sys.base_prefix, "pythonw.exe")):
        if p:
            out.add(os.path.normcase(os.path.abspath(p)))
    return out


_OWN = None


def own_label() -> str:
    """The name the mic check would give Filect itself (it never reports it — see mic_users)."""
    return "Filect"


def _in_use(root, path: str) -> list:
    """[(subkey name, …)] under `path` whose LastUsedTimeStop is 0 (= using the mic right now)."""
    import winreg
    out = []
    try:
        k = winreg.OpenKey(root, path)
    except OSError:
        return out
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(k, i)
        except OSError:
            break
        i += 1
        try:
            sk = winreg.OpenKey(k, sub)
            stop = winreg.QueryValueEx(sk, "LastUsedTimeStop")[0]
            start = winreg.QueryValueEx(sk, "LastUsedTimeStart")[0]
            if stop == 0 and start:
                out.append(sub)
        except OSError:
            continue
    return out


_COM_READY = threading.local()


def _exe_of(pid: int) -> str:
    """The full path of process `pid`'s exe ("" if it can't be read)."""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32")
        k.OpenProcess.restype = wintypes.HANDLE
        h = k.OpenProcess(0x1000, False, int(pid))           # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value
            return ""
        finally:
            k.CloseHandle(h)
    except Exception:
        return ""


def _session_mic_users(exclude_pids=()) -> list:
    """The apps with an ACTIVE audio session on any microphone right now — Windows' audio engine, per process
    (the same sessions the volume mixer lists). Filect's own process and `exclude_pids` are left out."""
    if not getattr(_COM_READY, "done", False):              # (each thread that asks needs COM)
        import comtypes
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass                                            # (already initialised on this thread, either model)
        _COM_READY.done = True
    import comtypes
    from pycaw.constants import CLSID_MMDeviceEnumerator, DEVICE_STATE, EDataFlow
    from pycaw.pycaw import IAudioSessionControl2, IAudioSessionManager2, IMMDeviceEnumerator
    ens = getattr(_COM_READY, "enum", None)
    if ens is None:
        ens = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
        _COM_READY.enum = ens
    skip = {0, os.getpid()} | {int(p) for p in exclude_pids if p}
    out = []
    devs = ens.EnumAudioEndpoints(EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value)
    for i in range(devs.GetCount()):
        try:
            mgr = devs.Item(i).Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None) \
                .QueryInterface(IAudioSessionManager2)
            sessions = mgr.GetSessionEnumerator()
        except Exception:
            continue
        for j in range(sessions.GetCount()):
            try:
                c = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
                if c.GetState() != 1:                       # AudioSessionStateActive = capturing right now
                    continue
                pid = int(c.GetProcessId())
            except Exception:
                continue
            if pid in skip:
                continue
            exe = _exe_of(pid)
            name = _app_name(exe.replace("\\", "#")) if exe else f"another app (process {pid})"
            if name not in out and name not in IGNORED_MIC_USERS:
                out.append(name)
    return out


def mic_users(exclude_pids=()) -> list:
    """Names of the OTHER apps capturing from a microphone right now ([] if none / unreadable). Filect itself is
    left out (its own mic, its shortcuts — mic_helper.busy() covers those).

    Windows' audio sessions, per process (_session_mic_users). (Until 2026-11-09 this read Windows' privacy record
    in the registry — but Windows 11 24H2+ stopped updating it there: with Zoom / VS Code voice / another recorder
    holding the mic it reported nothing, so hands-free never paused. The registry is still read if the audio
    engine can't be asked, for older Windows.)"""
    try:
        return _session_mic_users(exclude_pids)
    except Exception as e:
        logger.debug("[live] audio-session mic check failed (%s) — Windows' privacy record instead", e)
    return _registry_mic_users()


def _registry_mic_users() -> list:
    """The old check: Windows' privacy record (packaged apps by family name; desktop apps under NonPackaged by exe
    path). Stale on Windows 11 24H2+ (see mic_users)."""
    global _OWN
    try:
        import winreg
        if _OWN is None:
            _OWN = _own_paths()
        out = []
        for sub in _in_use(winreg.HKEY_CURRENT_USER, _MIC_STORE + r"\NonPackaged"):
            if os.path.normcase(sub.replace("#", "\\")) in _OWN:
                continue
            name = _app_name(sub)
            if name not in out and name not in IGNORED_MIC_USERS:
                out.append(name)
        for sub in _in_use(winreg.HKEY_CURRENT_USER, _MIC_STORE):
            if sub == "NonPackaged":
                continue
            name = _app_name(sub)
            if name not in out and name not in IGNORED_MIC_USERS:
                out.append(name)
        return out
    except Exception:
        return []


def screen_locked() -> bool:
    """Windows: the input desktop can't be opened while the lock screen (or a UAC prompt) is up."""
    try:
        u = ctypes.WinDLL("user32")
        u.OpenInputDesktop.restype = ctypes.c_void_p
        u.OpenInputDesktop.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        u.CloseDesktop.argtypes = [ctypes.c_void_p]
        h = u.OpenInputDesktop(0, False, 0x0100)              # DESKTOP_SWITCHDESKTOP
        if not h:
            return True
        u.CloseDesktop(h)
        return False
    except Exception:
        return False


class _KeyHook:
    """Esc, Shift+Ctrl and lone Ctrl taps, anywhere — OBSERVE ONLY (every key goes on to your apps): a low-level
    keyboard + mouse hook on its own thread with its own message loop, so a busy Filect can never slow your typing
    (Windows drops a hook that's slow to answer). `on_event(kind, a, b)` is called on THAT thread with
    ("mods", shift, ctrl_and_other) / ("key", vk, 0) / ("click", 0, 0) — keep it instant (emit a signal)."""
    _SHIFT = {0x10, 0xA0, 0xA1}
    _CTRL = {0x11, 0xA2, 0xA3}
    _OTHER = {0x12, 0xA4, 0xA5, 0x5B, 0x5C}            # Alt, Win: (the Mac's ⌘ / ⌥ / fn)

    def __init__(self, on_event):
        self._on = on_event
        self._thread = None
        self._tid = 0
        self._procs = []
        self._down = set()
        self.installed = False

    def start(self):
        if self._thread is not None:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(ready,), daemon=True, name="filect-live-keys")
        self._thread.start()
        ready.wait(1.0)

    def _mods(self):
        d = self._down
        return bool(d & self._SHIFT), bool(d & self._CTRL), bool(d & self._OTHER)

    def _loop(self, ready):
        from ctypes import wintypes
        u = ctypes.WinDLL("user32")
        k = ctypes.WinDLL("kernel32")
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        u.SetWindowsHookExW.restype = wintypes.HHOOK
        u.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u.CallNextHookEx.restype = LRESULT
        u.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HMODULE       # (a 32-bit default here = a failed hook)

        class KBD(ctypes.Structure):
            _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

        def kproc(n, w, l):
            try:
                if n == 0:
                    vk = ctypes.cast(l, ctypes.POINTER(KBD)).contents.vk
                    down = w in (0x0100, 0x0104)              # WM_KEYDOWN / WM_SYSKEYDOWN
                    if vk in self._SHIFT or vk in self._CTRL or vk in self._OTHER:
                        before = self._mods()
                        (self._down.add if down else self._down.discard)(vk)
                        after = self._mods()
                        if after != before:                   # (auto-repeat while held changes nothing)
                            self._on("mods", after[0] * 1 + after[1] * 2 + after[2] * 4, 0)
                    elif down:
                        self._on("key", int(vk), 0)
            except Exception:
                pass
            return u.CallNextHookEx(None, n, w, l)            # never swallowed

        def mproc(n, w, l):
            try:
                if n == 0 and w in (0x0201, 0x0204, 0x0207, 0x020B):   # L / R / M / X button down
                    self._on("click", 0, 0)
            except Exception:
                pass
            return u.CallNextHookEx(None, n, w, l)

        kp, mp = HOOKPROC(kproc), HOOKPROC(mproc)
        self._procs = [kp, mp]
        self._tid = k.GetCurrentThreadId()
        mod = k.GetModuleHandleW(None)
        hk = u.SetWindowsHookExW(13, kp, mod, 0)              # WH_KEYBOARD_LL
        hm = u.SetWindowsHookExW(14, mp, mod, 0)              # WH_MOUSE_LL
        self.installed = bool(hk)
        ready.set()
        if not hk:
            logger.warning("[live] key watcher unavailable")
            if hm:
                u.UnhookWindowsHookEx(hm)
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        u.UnhookWindowsHookEx(hk)
        if hm:
            u.UnhookWindowsHookEx(hm)

    def stop(self):
        t, self._thread = self._thread, None
        if t is not None and self._tid:
            ctypes.WinDLL("user32").PostThreadMessageW(self._tid, 0x0012, 0, 0)      # WM_QUIT
            t.join(1.0)
        self._tid, self.installed = 0, False
        self._down.clear()


class _Monitor(threading.Thread):
    """Background poll (1 s): other apps using the mic, and the screen lock. Cheap (~0.4 ms CPU)."""

    def __init__(self):
        super().__init__(daemon=True, name="filect-live-monitor")
        self.exclude = set()
        self.others, self.locked = [], False
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            self.others = mic_users(set(self.exclude))
            self.locked = screen_locked()
            self._stop.wait(1.0)

    def stop(self):
        self._stop.set()


# ---- usage (local estimate of what counts toward the daily voice limit) -----------------------------
def _usage_path():
    from app.core.settings import settings
    return settings.get_app_data_dir() / "assistant_usage.json"


def load_usage_today() -> float:
    try:
        d = json.loads(_usage_path().read_text())
        return float(d.get("seconds", 0.0)) if d.get("day") == time.strftime("%Y-%m-%d") else 0.0
    except Exception:
        return 0.0


def save_usage_today(seconds: float) -> None:
    try:
        p = _usage_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": time.strftime("%Y-%m-%d"), "seconds": round(seconds, 1)}))
        os.replace(tmp, p)
    except Exception:
        pass


class ChordTap:
    """⇧ Shift + ⌃ Control pressed together — the drawing key while Filect Assistant is on. Fires the
    moment BOTH are down (either order; hold them or tap them), once per press: let go of both to arm it
    again. Pure: feed it modifier states and key presses.
      · not ours if ⌘ / ⌥ / fn is involved, or a key was pressed while one of them was already held
        (⌃Tab, Tab… then ⇧ is still a shortcut)
      · a key or ⌘/⌥/fn AFTER it fired, while they're still held (⌃⇧Tab, ⌃⇧⌘4…) → "spoil": it was a
        shortcut after all, so the drawing that press started can be taken back
    REGRESSION (his first try): it used to fire on RELEASE, within 0.8 s — pressing and holding, waiting
    for something to happen, never did anything."""

    def __init__(self):
        self._held = False      # ⇧ and/or ⌃ down right now
        self._dirty = False     # a key / ⌘ ⌥ fn during this hold → a shortcut, not ours
        self._fired = False     # fired during this hold
        self._told = False      # "not counted" already reported for this hold

    def flags(self, shift: bool, ctrl: bool, other: bool):
        """A modifier changed → "press" (draw key), "spoil", "blocked" (both down but a shortcut), or None."""
        if not shift and not ctrl:
            self._held = self._dirty = self._fired = self._told = False
            return None
        self._held = True
        if other:
            spoil = self._fired and not self._dirty
            self._dirty = True
            if spoil:
                return "spoil"
        if shift and ctrl:
            if self._dirty:
                if not self._fired and not self._told:
                    self._told = True
                    return "blocked"
                return None
            if not self._fired:
                self._fired = True
                return "press"
        return None

    def key(self):
        """A key was pressed (anything). "spoil" if it lands while a press that fired is still held."""
        if not self._held:
            return None
        spoil = self._fired and not self._dirty
        self._dirty = True
        return "spoil" if spoil else None


class CtrlTap:
    """⌃ Control pressed and let go ON ITS OWN — the collect key while Filect Assistant is on (the page
    counts them: ⌃⌃ starts collecting, ⌃ sends). A tap = ⌃ down then up within TAP_MAX_SEC with nothing
    else in between: no key (⌃C), no click (⌃-click = right-click), no ⇧ / ⌘ / ⌥ / fn (⇧⌃ draws, fn + ⌃
    is Claude). Pure: feed it modifier states and "something else happened"."""
    TAP_MAX_SEC = 0.6

    def __init__(self):
        self._down_at = None    # when ⌃ went down (None: it's up)
        self._clean = False     # nothing else happened since

    def flags(self, ctrl: bool, other: bool, now: float):
        """A modifier changed → "tap" when a clean ⌃ tap just ended, else None."""
        if ctrl:
            if self._down_at is None:
                self._down_at, self._clean = now, not other
            elif other:
                self._clean = False
            return None
        if self._down_at is None:
            return None
        tap = self._clean and not other and now - self._down_at <= self.TAP_MAX_SEC
        self._down_at, self._clean = None, False
        return "tap" if tap else None

    def other(self) -> None:
        """A key / click while ⌃ is down: a shortcut, not a tap."""
        self._clean = False


def _live_text(tr) -> str:
    """The transcriber's text so far ('' for transcribers without it — the batch one, test fakes)."""
    f = getattr(tr, "text_so_far", None)
    try:
        return (f() or "").strip() if f else ""
    except Exception:
        return ""


class LiveListener(QObject):
    """Owns the always-on mic while Filect Assistant is on: detects each thing you say, transcribes it
    (streaming, only your speech), and emits `heard(id, text)` for the page to run. Lets go of the mic
    whenever something else records, the screen is locked, and resumes after."""

    state_changed = Signal(str, str)      # (state, detail): off|starting|listening|hearing|countdown|paused|error
    level = Signal(float)                 # mic level 0..1, for the meter
    early_text = Signal(int, str)         # uid, what you said — already transcribed as it ends (route it early)
    quick_sent = Signal(int, float)       # uid, seconds of the normal "send after" wait still left when it went
    resumed = Signal(int, int)            # previous uid, new uid: you went on talking within that time
    countdown = Signal(float)             # seconds left before what you said is sent (while paused talking)
    utterance_started = Signal(int)
    speech_confirmed = Signal(int)        # it's clearly speech (being transcribed now) — the page snaps
                                          #   your screen here, in case the request turns out to be about it
    utterance_ended = Signal(int)         # you stopped talking → it's being transcribed
    heard = Signal(int, str)              # (id, text): run it
    dropped = Signal(int, str, str)       # (id, text, why): not run
    usage = Signal(float)                 # seconds of your voice sent today (≈ what counts toward the limit)
    esc_late = Signal()                   # Esc with nothing being heard/transcribed: the page may cancel a
                                          #   request still being checked by the router
    draw_tap = Signal()                   # ⇧⌃ pressed (the drawing key) — not while a Filect shortcut runs
    draw_spoiled = Signal()               # …that ⇧⌃ turned out to be a shortcut (⌃⇧Tab): take it back
    ctrl_tap = Signal()                   # ⌃ tapped on its own (the page: ⌃⌃ = collect, ⌃ = send)
    _chunk_sig = Signal(int, object)
    _end_sig = Signal(int, int)
    _esc_sig = Signal()
    _tap_sig = Signal()
    _spoil_sig = Signal()
    _ctrl_sig = Signal()
    _key_sig = Signal(str, int, int)      # (Windows) from the key hook's thread → handled on the main thread
    _yield_sig = Signal()                 # (Windows) a Filect shortcut is about to record (any thread): let go

    def __init__(self, parent=None, open_mic=None, make_transcriber=None, monitor_factory=None,
                 detector=None):
        super().__init__(parent)
        from app.core import mic_helper
        self._mic = mic_helper
        self._open_mic = open_mic or mic_helper.open_mic
        self._make_tx = make_transcriber or self._default_transcriber
        self._monitor_factory = monitor_factory or _Monitor
        self._det = detector
        self.on = False
        self.state, self._detail = "off", ""
        self._paused = None             # pause reason, or None
        self._clear_since = 0.0
        self._yield_at = 0.0            # when the foreground hook last took the mic from us (any thread)
        self._gen = 0                   # mic generation: blocks/ends from an older mic are ignored
        self._stop_fn = None
        self._mic_vp = False
        self._vp_broken = False
        self._fails = deque(maxlen=6)
        self._uid = 0
        self._cur = None                # (uid, transcriber) being fed right now
        self._pending = {}              # uid → (transcriber, stats) waiting for text
        self._cancelled = {}            # uid → why it was dropped (its text, if it still comes, is ignored)
        self._levels = {}               # uid → speech level (for learn())
        self._used = load_usage_today()
        self._used_day = time.strftime("%Y-%m-%d")
        self._monitor = None
        self._esc_monitors = []
        self._held = []                 # audio of the request being heard, kept on the Mac until it's clearly speech
        self._answer_probe = None       # () -> bool: is the agent waiting for your answer? (set by the page)
        self._esc_filter = None         # () -> bool: True = the page used this Esc (e.g. it cancelled a drawing)
        self._muted = False             # Filect is speaking and the noise filter is off: ignore the mic
        self._chunk_sig.connect(self._on_chunk)
        self._qs_text, self._qs_since = None, 0.0
        self._quick = None                  # (uid, until) — the last quick send, while it can still be joined
        self._end_sig.connect(self._on_mic_end)
        self._esc_sig.connect(self._on_esc)
        self._tap_sig.connect(self._on_tap)
        self._spoil_sig.connect(self._on_spoil)
        self._ctrl_sig.connect(self._on_ctrl)
        self._key_sig.connect(self._key_event)
        self._yield_sig.connect(self._on_yield)
        self._keys = None
        self._chord = ChordTap()
        self._ctrl = CtrlTap()
        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self._tick)

    # ---- public ----------------------------------------------------------------------------------
    def start(self) -> None:
        if self.on:
            return
        from app.core.settings import settings
        if self._det is None:
            self._det = UtteranceDetector(settings.live_send_after_sec, settings.live_sensitivity)
        else:
            self._det.send_after = settings.live_send_after_sec
            self._det.set_sensitivity(settings.live_sensitivity)
            self._det.reset()
        self._quick = None                  # (a fresh start: nothing to join)
        self.on = True
        self._paused, self._yield_at, self._vp_broken, self._muted = None, 0.0, False, False
        self._fails.clear()
        self._mic.add_foreground_hook(self._fg_hook)
        self._monitor = self._monitor_factory()
        self._monitor.start()
        self._install_esc()
        self._open()
        self._poll.start()
        logger.info("[live] Filect Assistant started (send after %.1fs, sensitivity %s, noise filter %s)",
                    settings.live_send_after_sec, settings.live_sensitivity, settings.live_noise_filter)

    def stop(self) -> None:
        if not self.on:
            return
        self.on = False
        self._muted = False
        self._poll.stop()
        self._mic.remove_foreground_hook(self._fg_hook)
        self._remove_esc()
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor = None
        self._abandon("stopped")
        for uid in list(self._pending):     # finished talking but not transcribed yet: Stop = don't run it
            self._cancelled[uid] = "stopped"
            self._pending.pop(uid, None)
            self.dropped.emit(uid, "", "stopped")
        self._close()
        save_usage_today(self._used)
        self._set_state("off", "")
        logger.info("[live] Filect Assistant stopped")

    def apply_settings(self) -> None:
        """The tab's settings changed while it runs: the next thing you say uses them."""
        from app.core.settings import settings
        if self._det is not None:
            self._det.send_after = settings.live_send_after_sec
            self._det.set_sensitivity(settings.live_sensitivity)
        # Windows: the mic never has voice processing (see _open), so the noise filter setting never changes it.
        # (Comparing against the setting restarted the mic on EVERY change in the tab — the Preview switch, the
        # chime… — dropping what you were saying and deaf for the 1 s it takes to learn the room again.)
        want_vp = False
        if self.on and not self._paused and self._mic_vp != want_vp:
            self._abandon("restarting the mic")
            self._close()
            self._open()

    def set_answer_probe(self, fn) -> None:
        """fn() → True while the agent you're talking to is waiting for your answer (it just asked you
        something, or wants an approval). Then a short "yes" / "no" counts instead of being dropped."""
        self._answer_probe = fn

    def set_esc_filter(self, fn) -> None:
        """fn() is asked first on every Esc. True = the page used it, nothing else happens. "speech" = the
        page used it (it cancelled a drawing) AND what you're saying / what's being transcribed is
        cancelled too — but never a request already being checked (esc_late isn't sent)."""
        self._esc_filter = fn

    @property
    def echo_cancelled(self) -> bool:
        """Apple's voice processing is on, so Filect's own voice (its speakers) is removed from the mic. (Windows:
        always False — see _open.)"""
        return bool(self.on and self._mic_vp and not self._vp_broken)

    def set_muted(self, on: bool) -> None:
        """While Filect speaks WITHOUT the noise filter, the mic would hear it — ignore the mic meanwhile
        (what you were saying is dropped; the page waits for you to finish before it speaks)."""
        on = bool(on)
        if on == self._muted:
            return
        self._muted = on
        if on and self._cur is not None:
            self._abandon("muted")
        self._held = []
        if self._det is not None:
            self._det.reset()
        self._quick = None                  # (the clock restarts at 0: an old "join until" would match anything)
        logger.info("[live] mic %s while Filect speaks", "ignored" if on else "back")

    def _answer_expected(self) -> bool:
        try:
            return bool(self._answer_probe and self._answer_probe())
        except Exception:
            return False

    def learn(self, uid: int) -> None:
        """That one became a task (it was really you): tune the loudness gate to your voice."""
        lvl = self._levels.pop(uid, None)
        if lvl and self._det is not None:
            self._det.learn(lvl)

    def cancel_current(self) -> None:
        """Esc: throw away what you're saying right now (before it's sent)."""
        if self._cur is None or self._det is None:
            return
        logger.info("[live] cancelled with Esc")
        self._abandon("cancelled")
        self._det.suppress()
        self._set_state("listening", "")

    def _on_esc(self) -> None:
        """Esc — cancels the request at whatever stage it's in, until it actually starts: while you talk
        or pause, while it's being transcribed (here), or while the router checks it (the page:
        esc_late). A late Esc used to do nothing and the request ran."""
        logger.info("[live] Esc (state=%s, hearing=%s, transcribing=%s)", self.state, self._cur is not None,
                    sorted(self._pending))
        used = None
        try:
            used = self._esc_filter() if self._esc_filter is not None else None
        except Exception:
            used = None
        if used is True:
            logger.info("[live] Esc used by the page (a drawing / Filect talking)")
            return
        if self._cur is not None:
            self.cancel_current()
        elif self._pending:
            uid = max(self._pending)
            self._pending.pop(uid, None)
            self._levels.pop(uid, None)
            self._cancelled[uid] = "cancelled"
            logger.info("[live] utterance %d cancelled with Esc while transcribing", uid)
            self.dropped.emit(uid, "", "cancelled")
        elif used != "speech":
            self.esc_late.emit()

    def used_today(self) -> float:
        return self._used

    # ---- mic ---------------------------------------------------------------------------------------
    def _open(self) -> None:
        from app.core.settings import settings
        # Windows: Qt has no voice processing (echo cancellation) — so never claim it, or the page would let Filect's
        # own spoken replies into the mic (it mutes the mic while speaking only when echo_cancelled is False).
        vp = False
        self._gen += 1
        gen = self._gen
        self._mic_vp = vp
        if self._det is not None:
            self._det.reset_noise()         # a new mic (maybe filtered differently): learn the room again
        self._set_state("starting", "Starting the microphone…")
        try:
            self._stop_fn = self._open_mic(lambda b, g=gen: self._chunk_sig.emit(g, b), voice_processing=vp,
                                           background=True,
                                           on_end=lambda rc, g=gen: self._end_sig.emit(g, -1 if rc is None else rc))
        except Exception as e:
            logger.error("[live] mic start failed: %s", e)
            self._stop_fn = None
            self._set_state("error", "Couldn't open the microphone.")
            return
        if self._monitor is not None and getattr(self._stop_fn, "proc", None) is not None:
            self._monitor.exclude = {self._stop_fn.proc.pid}

    def _close(self) -> None:
        self._gen += 1                      # anything still arriving from this mic is ignored
        stop, self._stop_fn = self._stop_fn, None
        if stop is None:
            return
        try:
            stop()                          # (Windows: the QAudioSource, on this — the main — thread)
        except Exception:
            pass

    YIELD_SEC = 2.0     # the hook's "step aside" counts this long; by then busy() has taken over

    def _yielding(self) -> bool:
        return time.monotonic() - self._yield_at < self.YIELD_SEC

    def _fg_hook(self) -> None:
        """A Filect hotkey/mic button is about to record (any thread): let go of the mic NOW.
        REGRESSION (his test, 01:10:25): a 2nd shortcut while already paused for the 1st (before it had
        resumed) set a permanent "step aside" flag that only a held mic could clear → paused forever.
        Now it's a timestamp that expires by itself, and only taken when we actually hold the mic."""
        stop = self._stop_fn
        if stop is None:                     # not holding the mic (paused / reopening): nothing to give up
            return
        self._yield_at = time.monotonic()
        self._gen += 1                       # its audio from now on is ignored at once (the shortcut's, too)
        self._yield_sig.emit()               # Windows: paused + the mic closed on the main thread it lives on

    def _on_yield(self) -> None:
        """(Windows, main thread) A Filect shortcut is about to record: pause now — that drops what you were
        saying and closes the mic (no child process whose exit would report it, as on the Mac)."""
        if self.on and not self._paused:
            self._pause("hotkey", "Paused while you use a Filect shortcut")

    def _on_mic_end(self, gen: int, rc: int) -> None:
        if gen != self._gen or not self.on or self._paused:
            return
        if self._yielding():
            self._pause("hotkey", "Paused while you use a Filect shortcut")
            return
        if rc == self._mic.VP_UNAVAILABLE and self._mic_vp:
            logger.warning("[live] voice processing unavailable — listening without the noise filter")
            self._vp_broken = True
            self._open()
            return
        self._fails.append(time.monotonic())
        if len(self._fails) >= 5 and self._fails[-1] - self._fails[0] < 60:
            logger.error("[live] the microphone keeps stopping (rc=%s) — giving up", rc)
            self._abandon("microphone stopped")
            self._close()
            self._set_state("error", "The microphone keeps stopping. Click Start to try again.")
            self.on = False
            self._poll.stop()
            return
        logger.warning("[live] mic ended (rc=%s) — reopening", rc)
        self._abandon("microphone restarted")
        self._close()
        QTimer.singleShot(800, lambda: (self.on and not self._paused and self._stop_fn is None) and self._open())

    # ---- pause / resume ------------------------------------------------------------------------------
    def _pause_reason(self):
        if self._yielding() or self._mic.busy():
            return ("hotkey", "Paused while you use a Filect shortcut")
        m = self._monitor
        if m is not None and m.locked:
            return ("locked", "Paused — your PC is locked")
        if m is not None and m.others:
            others = [n for n in m.others if n != own_label()]
            if not others:                  # Filect's own mic (a shortcut's capture winding down)
                return ("hotkey", "Paused while you use a Filect shortcut")
            return ("other_app", f"Paused while {others[0]} uses the mic")
        return None

    def _pause(self, reason: str, detail: str = "") -> None:
        if self._paused == reason:
            return
        self._paused = reason
        self._abandon("paused")
        if self._det is not None:
            self._det.reset()
        self._quick = None                  # (paused = you didn't "go on talking"; the clock restarts at 0)
        self._close()
        r = self._pause_reason()
        self._set_state("paused", detail or (r[1] if r else "Paused"))
        logger.info("[live] paused (%s)", reason)

    def _tick(self) -> None:
        if not self.on:
            return
        r = self._pause_reason()
        now = time.monotonic()
        if r is not None:
            self._clear_since = 0.0
            if self._paused != r[0]:
                self._pause(r[0], r[1])
            elif self.state == "paused":
                self._set_state("paused", r[1])
        elif self._paused:
            if not self._clear_since:
                self._clear_since = now
            elif now - self._clear_since >= 1.0:          # clear for a second → listen again
                logger.info("[live] resuming (was %s)", self._paused)
                self._paused, self._yield_at, self._clear_since = None, 0.0, 0.0
                self._open()
        if self._det is not None and self.state == "countdown":
            left = self._det.countdown_left()
            if left is not None:
                self.countdown.emit(left)
        if time.strftime("%Y-%m-%d") != self._used_day:       # a new day: the limit resets
            self._used_day, self._used = time.strftime("%Y-%m-%d"), 0.0
            self.usage.emit(0.0)

    # ---- audio → utterances ---------------------------------------------------------------------------
    def _on_chunk(self, gen: int, b: bytes) -> None:
        if gen != self._gen or not self.on or self._paused or self._det is None or self._muted:
            return
        if self.state == "starting":
            self._set_state("listening", "")
        self.level.emit(min(1.0, _rms(b) * 12.0))
        self._det.min_speech = MIN_ANSWER_SEC if self._answer_expected() else MIN_SPEECH_SEC
        for ev in self._det.feed(b):
            kind = ev[0]
            if kind == "start":
                self._begin(ev[1])
            elif kind == "audio":
                self._send(ev[1])
            elif kind == "pause":
                self._set_state("countdown", "")
                left = self._det.countdown_left()
                self.countdown.emit(left if left is not None else 0.0)
            elif kind == "resume":
                self._reset_wait()                # (you went on — the next pause is judged afresh)
                self._set_state("hearing", "")
            elif kind == "end":
                self._finish(ev[1])
            elif kind == "drop":
                self._abandon("too short", stats=ev[1])
                self._set_state("listening", "")
        self._maybe_send()
        self._maybe_quick_send()

    def _maybe_quick_send(self) -> None:
        """You've stopped for QUICK_SEND_SEC and the speech service already closed what you said as a finished
        sentence (. ? !) → send it now instead of waiting out "send after" (his "a few seconds pass")."""
        if self.state != "countdown" or self._cur is None or self._cur[1] is None or self._det is None:
            return
        text, now = _live_text(self._cur[1]), self._det._t * FRAME_SEC   # (audio time, like the countdown)
        if text != self._qs_text:                     # (still changing: the transcript is catching up)
            self._qs_text, self._qs_since = text, now
            return
        left = self._det.countdown_left()
        if left is not None and now - self._qs_since >= QUICK_STABLE_SEC and text[-1:] not in (".", "?", "!"):
            self._maybe_wait_longer(text)
            return
        if (left is None or self._det.send_after - left < QUICK_SEND_SEC or now - self._qs_since < QUICK_STABLE_SEC
                or text[-1:] not in (".", "?", "!")):
            return
        logger.info("[live] a finished sentence — sending now (%.1f s sooner)", left)
        uid = self._cur[0]
        if self.finish_now():
            self._quick = (uid, self._det._t + left / FRAME_SEC)     # (audio time: until when it could still be
            self.quick_sent.emit(uid, left)                          #  joined with what you say next)

    def send_after_now(self) -> float:
        """The wait before what you're saying now is sent (longer while it sounds unfinished)."""
        if self._det is not None:
            return float(self._det.send_after)
        from app.core.settings import settings
        return float(settings.live_send_after_sec)

    def _reset_wait(self) -> None:
        if self._det is None:
            return
        from app.core.settings import settings
        if self._det.send_after != float(settings.live_send_after_sec):
            self._det.send_after = float(settings.live_send_after_sec)
        self._waited_longer = False

    def _maybe_wait_longer(self, text: str) -> None:
        """What you've said so far sounds unfinished: give you longer to go on (once per pause). Not while the agent
        waits for a quick answer ("yes" / "blue")."""
        if getattr(self, "_waited_longer", False) or self._answer_expected():
            return
        from app.core.settings import settings
        base = float(settings.live_send_after_sec)
        want = unfinished_wait(text, base)
        if want is None or want <= self._det.send_after:
            return
        self._det.send_after = want
        self._waited_longer = True
        logger.info("[live] sounds unfinished (…%s) — waiting %.1f s instead of %.1f s", text[-24:], want, base)
        left = self._det.countdown_left()
        self.countdown.emit(left if left is not None else 0.0)

    def _begin(self, preroll: bytes) -> None:
        """You started talking. Nothing is sent yet: the audio is held here until it's clearly speech
        (_maybe_send) — so a cough or a lone "uh" never leaves the Mac or counts toward your limit."""
        if self._cur is not None:           # shouldn't happen — close the previous one cleanly
            self._abandon("superseded")
        self._uid += 1
        self._cur = [self._uid, None]       # [id, transcriber (None until it's clearly speech)]
        q, self._quick = self._quick, None
        if q is not None and self._det is not None and self._det._t <= q[1]:
            logger.info("[live] you went on talking — %d joins %d", self._uid, q[0])
            self.resumed.emit(q[0], self._uid)
        self._qs_text, self._qs_since = None, 0.0   # (quick send: the text, and since when it held still)
        self._reset_wait()
        self._held = [preroll]
        self._set_state("hearing", "")
        self.utterance_started.emit(self._uid)

    def _maybe_send(self) -> None:
        """Once what you're saying is clearly speech (≥ min_speech), start transcribing: everything held
        so far goes up at once (streaming catches up instantly), the rest streams as you talk."""
        cur = self._cur
        if cur is None or cur[1] is not None or self._det is None or self._det.speech_sec() < self._det.min_speech:
            return
        uid = cur[0]
        try:
            tr = self._make_tx()
            tr._live_uid = uid
            tr.finished.connect(self._on_tx_text)
            tr.error.connect(self._on_tx_error)
            tr.start()
        except Exception as e:
            logger.error("[live] couldn't start transcription: %s", e)
            self._cur, self._held = None, []
            self.dropped.emit(uid, "", "Couldn't start transcription")
            return
        cur[1] = tr
        held, self._held = self._held, []
        for b in held:
            self._send(b)
        self.speech_confirmed.emit(uid)

    def _send(self, data: bytes) -> None:
        if self._cur is None or not data:
            return
        if self._cur[1] is None:            # not clearly speech yet: keep it here
            self._held.append(data)
            return
        self._cur[1].feed(data)
        self._used += len(data) / BYTES_PER_SEC
        self.usage.emit(self._used)

    def _finish(self, stats: dict) -> None:
        self._maybe_send()                  # (enough speech arrived in this very block)
        self._reset_wait()                  # (a longer wait was for THIS request only)
        cur, self._cur = self._cur, None
        self._set_state("listening", "")
        if cur is None:
            return
        uid, tr = cur
        if tr is None:                      # (can't happen: "end" means ≥ min_speech) — nothing was sent
            self._held = []
            self.dropped.emit(uid, "", "too short")
            return
        self._pending[uid] = (tr, stats)
        self._levels[uid] = stats.get("level", 0.0)
        early = _live_text(tr)              # (already transcribed — the page can route it right away)
        tr.stop_recording()                 # → final text (streaming), or the batch fallback
        self.utterance_ended.emit(uid)
        if early:
            self.early_text.emit(uid, early)
        save_usage_today(self._used)
        logger.info("[live] utterance %d ended: %s", uid, stats)

    def _abandon(self, why: str, stats=None) -> None:
        """Drop the utterance being heard right now (its text, if any, is ignored)."""
        self._reset_wait()
        cur, self._cur = self._cur, None
        self._held = []
        if cur is None:
            return
        uid, tr = cur
        if tr is not None:                  # it was being sent: stop it, and no batch upload after
            self._cancelled[uid] = why
            try:
                (tr.cancel if hasattr(tr, "cancel") else tr.stop_recording)()
            except Exception:
                pass
        self.dropped.emit(uid, "", why)
        logger.info("[live] utterance %d dropped: %s %s", uid, why, stats or "")

    def _on_tx_text(self, text: str) -> None:
        tr = self.sender()
        uid = getattr(tr, "_live_uid", None)
        if uid is None:
            return
        self._pending.pop(uid, None)
        if uid in self._cancelled:
            why = self._cancelled.pop(uid)
            if why == "cancelled" and (text or "").strip():
                self.dropped.emit(uid, text.strip(), "cancelled")   # show WHAT was cancelled (still not run)
            return
        text = (text or "").strip()
        min_words = 1 if self._answer_expected() else MIN_WORDS     # "yes" is an answer when one is expected
        if len(text.split()) < min_words:
            self._levels.pop(uid, None)
            self.dropped.emit(uid, text, "too short to be a request" if text else "no words recognized")
            return
        logger.info("[live] heard %d: %r", uid, text)
        self.heard.emit(uid, text)

    def _on_tx_error(self, msg: str) -> None:
        tr = self.sender()
        uid = getattr(tr, "_live_uid", None)
        if uid is None:
            return
        self._pending.pop(uid, None)
        self._levels.pop(uid, None)
        if uid in self._cancelled:
            self._cancelled.pop(uid, None)
            return
        self.dropped.emit(uid, "", f"Couldn't transcribe: {msg}")
        if "limit" in (msg or "").lower():          # the daily voice limit → stop listening
            self.stop()
            self._set_state("error", "Daily voice limit reached — Filect Assistant stopped.")

    @staticmethod
    def _default_transcriber():
        from app.core.settings import settings
        from app.core.transcription import StreamingTranscriber
        try:
            terms = settings.live_dictation_custom_terms()
        except Exception:
            terms = getattr(settings, "dictation_custom_terms", None)
        return StreamingTranscriber(terms=terms,
                                    language=(settings.dictation_language or None), external=True)

    # ---- Esc + the ⇧⌃ drawing key (observe only — never swallowed, so both still work in your apps) ------
    def _key_event(self, kind: str, a: int, b: int) -> None:
        """Every key down / modifier change / click anywhere (from the key hook; main thread). Windows: Shift + Ctrl
        = the drawing key (the Mac's ⇧⌃), a lone Ctrl tap = collect / send (⌃), Esc. Alt / Win = the Mac's ⌘ ⌥ fn."""
        if kind == "mods":
            shift, ctrl, other = bool(a & 1), bool(a & 2), bool(a & 4)
            r = self._chord.flags(shift, ctrl, other)
            if self._ctrl.flags(ctrl, shift or other, time.monotonic()) == "tap":
                self._ctrl_sig.emit()
        elif kind == "key":
            r = self._chord.key()
            self._ctrl.other()
        else:                                         # a click (Ctrl+click): not a Ctrl tap
            self._ctrl.other()
            return
        if r == "press":
            self._tap_sig.emit()
        elif r == "spoil":
            self._spoil_sig.emit()
        elif r == "blocked":
            logger.info("[live] Shift+Ctrl not counted — another key or Alt/Win was part of it (a shortcut)")
        if kind == "key" and a == 0x1B:               # VK_ESCAPE
            self._esc_sig.emit()

    def _on_tap(self) -> None:
        if not self.on:
            return
        if self._mic.busy():                          # fn + ⌃⌃ etc. use ⇧ / ⌃ themselves — theirs, not ours
            logger.info("[live] Shift+Ctrl ignored (a Filect shortcut is running)")
            return
        logger.info("[live] Shift+Ctrl pressed (draw)")
        self.draw_tap.emit()

    def _on_ctrl(self) -> None:
        if not self.on or self.state == "paused":
            return
        if self._mic.busy():                          # fn + ⌃⌃ receive mode sends with ⌃ too — theirs
            logger.info("[live] Ctrl ignored (a Filect shortcut is running)")
            return
        self.ctrl_tap.emit()

    def finish_now(self) -> bool:
        """End what you're saying NOW instead of waiting out "send after" (⌃ sends a collection) → it's
        transcribed and `heard` as usual. True if there was something (too short → dropped)."""
        if self._cur is None or self._det is None:
            return False
        ev = []
        self._det._finish(ev)
        kind, stats = ev[0]
        if kind == "end":
            self._finish(stats)
            return True
        self._abandon("too short", stats=stats)
        self._set_state("listening", "")
        return False

    def _on_spoil(self) -> None:
        if self.on:
            logger.info("[live] Shift+Ctrl was part of a shortcut (another key followed) — taking it back")
            self.draw_spoiled.emit()

    def _install_esc(self) -> None:
        self._chord = ChordTap()                      # nothing carried over from the last session
        self._ctrl = CtrlTap()
        if self._keys is None:
            self._keys = _KeyHook(lambda kind, a, b: self._key_sig.emit(kind, a, b))
        try:
            self._keys.start()
        except Exception as e:
            logger.info("[live] key watcher unavailable: %s", e)

    def _remove_esc(self) -> None:
        k = self._keys
        if k is not None:
            try:
                k.stop()
            except Exception:
                pass

    def _set_state(self, state: str, detail: str) -> None:
        if state == self.state and detail == getattr(self, "_detail", ""):
            return
        self.state, self._detail = state, detail
        self.state_changed.emit(state, detail)
