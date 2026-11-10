"""Filect Assistant — collect, then send (Ctrl Ctrl … Ctrl) and "Open answers automatically" — Windows port of the Mac
tests/test_live_collect.py. No real agents, no keystrokes anywhere (the key hook's events are fed straight to the
listener's handler — the Mac built real NSEvents in-process); your settings.json is never written.
  · the Ctrl tap detector (pure + the hook's events through the listener): a clean lone Ctrl tap only — not Ctrl+C,
    Ctrl-click, Shift+Ctrl (which still draws), Alt / Win + Ctrl, nor a long hold; not while paused / a Filect
    shortcut runs; left or right Ctrl
  · the listener's finish_now: Ctrl ends what you're saying at once (no "send after" wait) → heard as usual
  · the tab: Ctrl Ctrl starts collecting (one Ctrl alone does nothing); what you say + draw is kept, not sent, in ONE
    row; Ctrl → ONE request with all of it; waits for what's still being transcribed (gives up after COLLECT_WAIT_MS);
    Esc / Stop throw it away; nothing collected → nothing sent; never sent twice
  · auto-open: ON by default (behaviour unchanged); OFF → tasks / answers / plain errors don't pin, flash or open the
    card; "needs your OK" and setup steps still open it; the real card stays tucked
Run: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_live_collect.py -q"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.core import agent_runner as ar  # noqa: E402
from app.core import agent_sessions as asess  # noqa: E402
from app.core import codex_runner as crun  # noqa: E402
from app.core import intent_router as ir  # noqa: E402
from app.core import live_listener as ll  # noqa: E402
from app.core import speech_out as so  # noqa: E402
from app.core import supabase_client as sc  # noqa: E402
from app.core import transcription as tx  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402
from app.ui import live_page as lp  # noqa: E402
from app.ui import screen_ink  # noqa: E402

_REAL_KEYHOOK = ll._KeyHook          # (the fixture replaces it — never installed in tests)
saves = []
_PAGES = []
_SHOTS = {"dir": None}
_SETTINGS = (("live_send_after_sec", 2.0), ("live_sensitivity", "medium"), ("live_chime", False),
             ("live_noise_filter", True), ("live_speak", False), ("live_voice", "sal"), ("live_tour_seen", False),
             ("live_settings_collapsed", False), ("assistant_auto_open", True), ("assistant_bypass", False),
             ("assistant_model", ""), ("assistant_effort", ""), ("assistant_agent", "claude"),
             ("agents_list_collapsed", True), ("agents_tour_seen", False))


class _NoNet:
    def _no(self, *a, **k):
        raise RuntimeError("no network in tests")
    post = get = options = _no


class _NoRouter:
    def classify(self, *a, **k):
        raise RuntimeError("no real router in tests")


class _NeverRunner:
    def __init__(self, *a, **k):
        pass

    async def run(self, *a, **k):
        raise RuntimeError("no real agents in tests")

    async def interrupt(self):
        pass


class _NoHook:
    """The listener's low-level key hook in tests: never installed (its events are fed through _key_event)."""
    def __init__(self, on_event):
        self.installed = False

    def start(self):
        pass

    def stop(self):
        pass


def _no_codex(*a, **k):
    raise RuntimeError("no real Codex in tests")


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch, tmp_path):
    """Never the user's settings / app-data files, no sound, no analytics, no network, no real agents / router / key
    hook; every live_* / assistant_* setting starts known and is restored after."""
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: saves.append(1))
    for k, v in _SETTINGS:
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(sc, "track", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_track", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_chime", lambda name="Tink": None)
    monkeypatch.setattr(ap, "_track", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_play_chime", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap.AssistantPage, "_show_idle_overlays", lambda self: None)
    monkeypatch.setattr(ap.AssistantPage, "_load_recent", lambda self: None)
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", str(tmp_path / "Filect Assistant"))
    monkeypatch.setattr(asess, "_touched_path", lambda: tmp_path / "assistant_sessions.json")
    monkeypatch.setattr(asess, "_projects_root", lambda: tmp_path / "claude-projects")
    monkeypatch.setattr(ar, "_models_cache_path", lambda: tmp_path / "claude_models.json")
    monkeypatch.setattr(ar, "AgentRunner", _NeverRunner)
    monkeypatch.setattr(crun, "CodexRunner", _NeverRunner)
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: _NoRouter()))
    monkeypatch.setattr(ir, "classify_with_codex", _no_codex)
    monkeypatch.setattr(so, "_HTTP", _NoNet())
    monkeypatch.setattr(tx, "_get_auth_token", lambda: None)
    monkeypatch.setattr(ll, "_KeyHook", _NoHook)
    monkeypatch.setattr(ll, "_usage_path", lambda: tmp_path / "assistant_usage.json")
    shots = tmp_path / "Screenshots"
    shots.mkdir()
    _SHOTS["dir"] = shots
    monkeypatch.setattr(screen_ink, "SCREENSHOT_DIR", shots)
    orig = ap.AssistantPage.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        _PAGES.append(self)
    monkeypatch.setattr(ap.AssistantPage, "__init__", track)
    saves.clear()
    yield
    while _PAGES:
        _PAGES.pop().shutdown()


def _pump(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


# ---- a real listener with a fake mic + transcriber (as tests/test_live_listener.py) -------------------------
RATE = 16000
_CACHE = {}


def _say_wav(text: str, path: str) -> None:
    """Windows' own voice (SAPI) straight into a 16 kHz 16-bit mono WAV — nothing is played."""
    import comtypes.client
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
    fmt.Type = 18                                   # SAFT16kHz16BitMono
    stream.Format = fmt
    stream.Open(path, 3)                            # SSFMCreateForWrite
    voice.AudioOutputStream = stream
    try:
        voice.Speak(text)
    finally:
        stream.Close()


_SPEECH_DIR = Path(tempfile.gettempdir()) / "filect-test-speech"


def _speech_wav(text: str) -> Path:
    """The WAV for `text`, made once and kept (in %TEMP%) — SAPI under x64 emulation now and then fails a Speak()
    with E_OUTOFMEMORY, so it's retried, and fewer calls means fewer chances."""
    import hashlib
    _SPEECH_DIR.mkdir(exist_ok=True)
    p = _SPEECH_DIR / (hashlib.sha1(f"16k|{text}".encode("utf-8")).hexdigest() + ".wav")
    if p.exists() and p.stat().st_size > 1000:
        return p
    err = None
    for _ in range(5):
        part = p.parent / f"{p.stem}.{os.getpid()}.part"
        try:
            _say_wav(text, str(part))
            os.replace(part, p)
            return p
        except Exception as e:                      # (COMError E_OUTOFMEMORY — try again with a fresh voice)
            err = e
            time.sleep(0.2)
    raise err


def speech(text: str, gain: float = 0.35) -> np.ndarray:
    """`text` spoken by Windows, 16 kHz int16, at the Mac test's level (peak-normalised, × gain), trimmed."""
    if text not in _CACHE:
        import wave
        with wave.open(str(_speech_wav(text))) as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        a = a / max(1.0, float(np.max(np.abs(a)))) * 32767.0 * 0.6
        loud = np.nonzero(np.abs(a) > 0.01 * 32767)[0]
        a = a[max(0, loud[0] - 160): loud[-1] + 160] if len(loud) else a
        _CACHE[text] = a
    return np.clip(_CACHE[text] * gain, -32768, 32767).astype(np.int16)


def silence(secs: float, noise: float = 0.0004) -> np.ndarray:
    rng = np.random.default_rng(1)
    return (rng.standard_normal(int(secs * RATE)) * noise * 32768).astype(np.int16)


class FakeMic:
    def __init__(self):
        self.calls = []

    def __call__(self, on_chunk, voice_processing=False, background=False, on_end=None):
        stop_calls = []

        def stop():
            stop_calls.append(1)
        stop.proc = None
        stop.calls = stop_calls
        self.calls.append({"vp": voice_processing, "bg": background, "on_chunk": on_chunk, "on_end": on_end,
                           "stop": stop})
        return stop

    def push(self, audio, k=-1):
        cb = self.calls[k]["on_chunk"]
        b = audio.tobytes()
        for i in range(0, len(b), 3200):
            cb(b[i:i + 3200])
        app.processEvents()


class FakeTx(QObject):
    finished = Signal(str)
    error = Signal(str)
    reply = "Create a new folder called invoices"
    delay = 10
    made = []

    def __init__(self):
        super().__init__()
        self.fed, self.stopped, self.cancelled = 0, False, False
        FakeTx.made.append(self)

    def cancel(self):
        self.cancelled = True
        self.stop_recording()

    def start(self):
        pass

    def feed(self, data):
        self.fed += len(data)

    def stop_recording(self):
        self.stopped = True
        r = FakeTx.reply
        QTimer.singleShot(FakeTx.delay, lambda: self.finished.emit(r))


class FakeMonitor:
    others, locked, exclude = [], False, set()

    def start(self):
        pass

    def stop(self):
        pass


def _listener():
    FakeTx.made.clear()
    FakeTx.reply, FakeTx.delay = "Create a new folder called invoices", 10
    mic = FakeMic()
    L = ll.LiveListener(open_mic=mic, make_transcriber=FakeTx, monitor_factory=lambda: FakeMonitor())
    rec = {"states": [], "heard": [], "dropped": [], "ended": []}
    L.state_changed.connect(lambda s, d: rec["states"].append((s, d)))
    L.heard.connect(lambda u, t: rec["heard"].append((u, t)))
    L.dropped.connect(lambda u, t, w: rec["dropped"].append((u, t, w)))
    L.utterance_ended.connect(lambda u: rec["ended"].append(u))
    return L, mic, rec


# ---- the Ctrl tap -------------------------------------------------------------------------------------------
def test_ctrl_tap_detector():
    T = ll.CtrlTap()
    assert T.flags(True, False, 0.0) is None and T.flags(False, False, 0.2) == "tap", "a clean tap"
    assert T.flags(True, False, 1.0) is None and T.flags(False, False, 1.9) is None, "held too long"
    T.flags(True, False, 2.0)
    T.other()
    assert T.flags(False, False, 2.1) is None, "Ctrl+C / Ctrl-click: a shortcut"
    assert T.flags(True, True, 3.0) is None and T.flags(False, True, 3.1) is None, "Shift (Alt / Win) first: Shift+Ctrl"
    T.flags(False, False, 3.2)
    T.flags(True, False, 4.0)
    T.flags(True, True, 4.05)
    assert T.flags(False, True, 4.1) is None, "Ctrl then Shift: the drawing chord, not a tap"
    T.flags(False, False, 4.2)
    assert T.flags(True, False, 5.0) is None and T.flags(False, False, 5.1) == "tap", "clean again after"
    assert T.flags(False, False, 6.0) is None, "a release with nothing down"


# The key hook's events (live_listener._KeyHook → LiveListener._key_event): ("mods", shift=1 | ctrl=2 | Alt/Win=4),
# ("key", vk), ("click", 0). (Replaces the Mac's real NSEvents.)
SHIFT, CTRL, OTHER = 1, 2, 4
VK_C = 0x43


def test_ctrl_tap_through_the_listener():
    """The listener's real handler fed the key hook's events (no keystrokes anywhere)."""
    L, _, _ = _listener()
    taps, draws = [], []
    L.ctrl_tap.connect(lambda: taps.append(1))
    L.draw_tap.connect(lambda: draws.append(1))

    def feed(*evs):
        for e in evs:
            L._key_event(e[0], e[1] if len(e) > 1 else 0, 0)
        _pump(lambda: False, 0.05)
    L.start()
    try:
        feed(("mods", CTRL), ("mods", 0))
        assert taps == [1], "a lone Ctrl tap"
        feed(("mods", CTRL), ("key", VK_C), ("mods", 0))
        assert taps == [1], "Ctrl+C"
        feed(("mods", CTRL), ("click",), ("mods", 0))
        assert taps == [1], "Ctrl-click"
        feed(("mods", SHIFT), ("mods", SHIFT | CTRL), ("mods", SHIFT), ("mods", 0))
        assert taps == [1] and draws == [1], "Shift+Ctrl draws — and isn't a Ctrl tap"
        feed(("mods", OTHER), ("mods", OTHER | CTRL), ("mods", OTHER), ("mods", 0))
        assert taps == [1], "Win / Alt + Ctrl (the Mac's fn + ⌃): someone else's"
        feed(("mods", CTRL), ("mods", CTRL | OTHER), ("mods", CTRL), ("mods", 0))
        assert taps == [1], "Ctrl then Alt / Win (the Mac's ⌃⌘)"
        feed(("mods", CTRL))
        time.sleep(ll.CtrlTap.TAP_MAX_SEC + 0.1)                     # (a hold: real time must pass)
        feed(("mods", 0))
        assert taps == [1], "held Ctrl — not a tap"
        feed(("mods", CTRL), ("mods", 0))
        assert taps == [1, 1], "a clean tap counts again after"
        real_mic = L._mic
        L._mic = type("Busy", (), {"busy": staticmethod(lambda: True)})()
        try:
            feed(("mods", CTRL), ("mods", 0))
        finally:
            L._mic = real_mic
        assert taps == [1, 1], "a Filect shortcut is running (its own keys are its own)"
        L._set_state("paused", "Paused while you use a Filect shortcut")
        feed(("mods", CTRL), ("mods", 0))
        assert taps == [1, 1], "paused: not ours"
    finally:
        L.stop()
    feed(("mods", CTRL), ("mods", 0))
    assert taps == [1, 1], "off: nothing"


def test_left_and_right_ctrl_are_the_same_key():
    """The Mac's 'the right ⌃ taps too': the Windows hook folds left / right Ctrl (and Shift, Alt, Win) into one bit
    each, so a right-Ctrl tap reaches the listener exactly like a left one. (The hook is built, never installed.)"""
    h = _REAL_KEYHOOK(lambda *a: None)
    for vk, want in ((0xA2, (False, True, False)), (0xA3, (False, True, False)), (0x11, (False, True, False)),
                     (0xA0, (True, False, False)), (0xA1, (True, False, False)), (0xA4, (False, False, True)),
                     (0xA5, (False, False, True)), (0x5B, (False, False, True)), (0x5C, (False, False, True))):
        h._down = {vk}
        assert h._mods() == want, (hex(vk), h._mods())
    h._down = {0xA1, 0xA3}                                             # right Shift + right Ctrl: the drawing key
    assert h._mods() == (True, True, False)
    assert not h.installed and h._thread is None


def test_finish_now():
    """Ctrl (send) ends what you're saying at once — no waiting out "send after" — and it's heard as usual."""
    L, mic, rec = _listener()
    L.start()
    try:
        s = speech("Make the header blue.")
        # (1 s of room first: the detector learns the room for NOISE_WARMUP_SEC before anything can start, and SAPI
        #  says this short sentence faster than the Mac's `say` — with 0.5 s, too little of it counted as speech)
        mic.push(np.concatenate([silence(1.0), s, silence(0.8)]))       # paused, but < 2 s
        assert L.state == "countdown" and L._cur is not None, L.state
        assert L.finish_now() is True
        assert L._cur is None and rec["ended"] == [1] and L.state == "listening"
        assert _pump(lambda: rec["heard"], 2) and rec["heard"][0][0] == 1
        mic.push(np.concatenate([silence(0.5), s]))                       # still talking
        assert L.state == "hearing" and L._cur is not None
        assert L.finish_now() is True and rec["ended"] == [1, 2]
        assert _pump(lambda: len(rec["heard"]) == 2, 2)
        assert L.finish_now() is False, "nothing being said: nothing to do"
    finally:
        L.stop()


# ---- the tab: collect, then send ------------------------------------------------------------------------
def _img():
    im = QImage(200, 120, QImage.Format_ARGB32_Premultiplied)
    im.fill(QColor("#336699"))
    return im


class FakeListener(QObject):
    state_changed = Signal(str, str)
    level = Signal(float)
    countdown = Signal(float)
    utterance_started = Signal(int)
    utterance_ended = Signal(int)
    speech_confirmed = Signal(int)
    heard = Signal(int, str)
    dropped = Signal(int, str, str)
    usage = Signal(float)
    esc_late = Signal()
    draw_tap = Signal()
    draw_spoiled = Signal()
    ctrl_tap = Signal()

    def __init__(self):
        super().__init__()
        self.on, self.esc_filter, self.learned = False, None, []
        self.finished_now, self.on_finish = 0, None

    def start(self):
        self.on = True
        self.state_changed.emit("listening", "")

    def stop(self):
        self.on = False
        self.state_changed.emit("off", "")

    def learn(self, uid):
        self.learned.append(uid)

    def apply_settings(self):
        pass

    def used_today(self):
        return 0.0

    def set_answer_probe(self, fn):
        pass

    def set_esc_filter(self, fn):
        self.esc_filter = fn

    def finish_now(self):
        self.finished_now += 1
        if self.on_finish is not None:
            self.on_finish()
        return True


class FakeOverlay:
    def __init__(self):
        self.notes = []

    def set_listening(self, m):
        pass

    def flash_note(self, text, color="#B8B8C4", ms=2500):
        self.notes.append(text)


class FakeAP(QObject):
    live_routed = Signal(object, str, str, str)
    live_screen_used = Signal(object)

    def __init__(self):
        super().__init__()
        self.calls, self.ov, self._overlay = [], FakeOverlay(), None

    def run_voice_task(self, text, source="voice", images=None, tag=None, screen=None):
        self.calls.append({"text": text, "tag": tag, "images": list(images or []), "screen": screen})

    def _ensure_notch(self):
        return self.ov

    def live_answer_context(self):
        return None


class FakeInk:
    def __init__(self):
        self.log = []

    def begin(self):
        self.log.append("begin")

    def set_drawing(self, on, hint=""):
        self.log.append(("drawing", on, hint))

    def end(self):
        self.log.append("end")

    def snapshot(self):
        p = str(_SHOTS["dir"] / f"drawn-{len(self.log)}.png")
        _img().save(p)
        self.log.append("snap")
        return p, True


class FakeSpeaker(QObject):
    started = Signal()
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.said, self.holds, self.stops, self.speaking = [], [], 0, False

    def say(self, line, data=None):
        self.said.append(line)
        self.speaking = True

    def hold(self, on):
        self.holds.append(on)

    def stop(self):
        self.stops += 1
        if self.speaking:
            self.speaking = False
            self.finished.emit()


def _tab():
    mw = type("MW", (), {})()
    mw.assistant_page = FakeAP()
    L = FakeListener()
    page = lp.LivePage(mw, listener=L, capture=lambda rect: _img())
    page._ink = FakeInk()
    return page, L, mw.assistant_page


def _collect(page, L):
    page._ctrl_at = 0.0                                           # (no earlier Ctrl tap still within the window)
    L.ctrl_tap.emit()
    L.ctrl_tap.emit()
    assert page._collect is not None


def test_collect_then_send():
    page, L, AP = _tab()
    L.ctrl_tap.emit()
    L.ctrl_tap.emit()
    assert page._collect is None, "Filect Assistant off: nothing"
    page.start()
    L.ctrl_tap.emit()
    assert page._collect is None, "one Ctrl alone does nothing"
    page._ctrl_at -= lp.COLLECT_TAP_SEC + 0.1                    # (as if that tap were COLLECT_TAP_SEC + 0.1 s ago)
    L.ctrl_tap.emit()
    assert page._collect is None, "two Ctrl taps far apart: still nothing"
    L.ctrl_tap.emit()                                              # within COLLECT_TAP_SEC of that one
    assert page._collect is not None and page.status.text().startswith("Collecting — nothing yet")
    assert AP.ov.notes[-1] == "📥  Collecting · nothing yet — tap Ctrl to send"
    # what you say is kept, not sent — one row for all of it
    L.utterance_ended.emit(1)
    L.heard.emit(1, "Look at this page.")
    L.utterance_ended.emit(2)
    L.heard.emit(2, "Make the header blue.")
    assert AP.calls == [], "collecting: nothing sent"
    assert set(page._rows) == {1} and page._rows[1].text.text() == "“Look at this page. Make the header blue.”"
    assert "2 parts" in page._rows[1].outcome.text() and AP.ov.notes[-1].startswith("📥  Collecting · 2 parts")
    # a drawing is kept too — no 60 s expiry while collecting
    L.draw_tap.emit()
    L.draw_tap.emit()
    assert len(page._drawings) == 1 and not page._drawn_timer.isActive()
    assert AP.ov.notes[-1] == "📥  Collecting · 2 parts · 1 drawing — tap Ctrl to send"
    L.state_changed.emit("countdown", "")
    assert page.status.text().startswith("Adding it…"), page.status.text()
    L.state_changed.emit("listening", "")
    _pump(lambda: False, 0.3)
    assert AP.calls == [], "a long pause sends nothing"
    # Ctrl → ONE request with everything
    L.ctrl_tap.emit()
    assert L.finished_now == 1, "what you're saying right now is finished at once"
    assert len(AP.calls) == 1, AP.calls
    call = AP.calls[0]
    assert call["text"] == "Look at this page. Make the header blue." and call["tag"] == 1
    assert len(call["images"]) == 1 and call["screen"] is None, "the drawing went with it"
    assert page._collect is None and page._also[1] == [2] and page._drawings == []
    assert AP.ov.notes[-1] == "" and not page.status.text().startswith("Collecting")
    L.ctrl_tap.emit()
    assert page._collect is None, "the Ctrl that sent doesn't count toward a new Ctrl Ctrl"
    # routed as one request: the row says what started (and both parts tune the voice gate)
    page._on_routed(1, "new", "Header Color", "s1")
    assert page._rows[1].outcome.text().startswith("Started “Header Color”") and L.learned == [1, 2]
    # without drawings: the screen of the latest part that captured one (for "look at this")
    _collect(page, L)
    L.speech_confirmed.emit(3)
    L.heard.emit(3, "Look at this.")
    L.speech_confirmed.emit(4)
    L.heard.emit(4, "What's wrong with it?")
    L.ctrl_tap.emit()
    assert AP.calls[-1]["text"] == "Look at this. What's wrong with it?" and callable(AP.calls[-1]["screen"])
    assert AP.calls[-1]["images"] == []
    page.stop()


def test_collect_waits_for_the_last_part(monkeypatch):
    page, L, AP = _tab()
    page.start()
    _collect(page, L)
    L.utterance_ended.emit(1)
    L.heard.emit(1, "Make a landing page.")
    L.on_finish = lambda: L.utterance_ended.emit(2)               # you were still talking → transcribing
    L.ctrl_tap.emit()
    assert AP.calls == [] and page._collect["sending"] and AP.ov.notes[-1] == "Sending…"
    L.ctrl_tap.emit()
    assert AP.calls == [], "another Ctrl while it waits: still one request"
    L.heard.emit(2, "For my coffee shop.")
    assert len(AP.calls) == 1 and AP.calls[0]["text"] == "Make a landing page. For my coffee shop."
    # it never comes → it gives up waiting and sends what it has; the late part runs on its own
    monkeypatch.setattr(lp, "COLLECT_WAIT_MS", 200)
    _collect(page, L)
    L.utterance_ended.emit(3)
    L.heard.emit(3, "Rename the photos.")
    L.on_finish = lambda: L.utterance_ended.emit(4)
    L.ctrl_tap.emit()
    assert len(AP.calls) == 1
    assert _pump(lambda: len(AP.calls) == 2, 2) and AP.calls[1]["text"] == "Rename the photos."
    L.heard.emit(4, "By date.")
    assert len(AP.calls) == 3 and AP.calls[2]["text"] == "By date."
    # finishing it was too short (dropped at once) → sent exactly once
    _collect(page, L)
    L.heard.emit(5, "Zip the folder.")
    L.on_finish = lambda: L.dropped.emit(6, "", "too short")
    L.ctrl_tap.emit()
    _pump(lambda: False, 0.2)
    assert len(AP.calls) == 4 and AP.calls[3]["text"] == "Zip the folder.", [c["text"] for c in AP.calls]
    L.on_finish = None
    page.stop()


def test_collect_thrown_away():
    page, L, AP = _tab()
    page.start()
    # Ctrl with nothing collected: nothing sent, collecting ends
    _collect(page, L)
    L.ctrl_tap.emit()
    assert AP.calls == [] and page._collect is None and AP.ov.notes[-1] == "Nothing collected — say something first"
    # Esc: everything goes (what you said, the drawings, what you're saying now)
    _collect(page, L)
    L.heard.emit(1, "Make it blue.")
    L.draw_tap.emit()
    L.draw_tap.emit()
    L.draw_tap.emit()                                             # a 2nd drawing, still open
    assert page._drawing
    r = L.esc_filter()
    assert r == "speech" and page._collect is not None and not page._drawing and len(page._drawings) == 1, \
        "the 1st Esc cancels the open drawing only"
    r = L.esc_filter()
    assert r == "speech" and page._collect is None and page._drawings == [] and AP.calls == []
    assert page._rows[1].outcome.text() == "Thrown away (Esc) — not sent"
    assert AP.ov.notes[-1] == "✕  Collection thrown away"
    # Esc while Filect is talking: only quiets it — the collection stays
    _collect(page, L)
    L.heard.emit(2, "Rename it.")
    sp = FakeSpeaker()
    sp.speaking = True
    page._speaker_obj = sp
    assert L.esc_filter() is True and sp.stops == 1 and page._collect is not None
    page._speaker_obj = None
    # Stop: thrown away
    page.stop()
    assert page._collect is None and AP.calls == [] and page._rows[2].outcome.text() == "Not sent — you clicked Stop"


def test_cancelled_request_takes_its_drawing():
    """REGRESSION (his test, 2026-11-07): Ctrl Ctrl → draw → "what is this exactly?" → Ctrl → Esc while it was being
    checked → cancelled ✓ — but the drawing went back to waiting and was attached to his NEXT, unrelated request ("can
    you tell me a short poem?") 40 s later. Esc throws the drawing away with the request."""
    page, L, AP = _tab()
    page.start()
    _collect(page, L)
    L.draw_tap.emit()
    L.draw_tap.emit()
    L.heard.emit(6, "What is this exactly?")
    L.ctrl_tap.emit()
    assert len(AP.calls) == 1 and len(AP.calls[0]["images"]) == 1
    AP.live_routed.emit(6, "cancelled", "", "")                    # Esc while the router was checking it
    assert page._drawings == [] and not page._drawn_timer.isActive(), "gone with the request"
    L.heard.emit(7, "Hello Jarvis, can you tell me a short poem?")
    assert AP.calls[-1]["images"] == [], "the next request has no stray drawing"
    # (unchanged) not a request → the drawing still waits for what you say next
    L.draw_tap.emit()
    L.draw_tap.emit()
    L.heard.emit(8, "hmm")
    AP.live_routed.emit(8, "ignored", "", "")
    assert len(page._drawings) == 1 and page._drawn_timer.isActive()
    L.heard.emit(9, "circle this one")
    assert len(AP.calls[-1]["images"]) == 1
    page.stop()


# ---- open answers automatically ----------------------------------------------------------------------------
class FakeWorker(QObject):
    """Stands in for AgentWorker: records what the agent would be sent; never runs anything."""
    step = Signal(str, object)
    approvalRequested = Signal(str, object)
    result = Signal(str, str, float)
    error = Signal(str, str)
    done = Signal(str)
    sent = []

    def __init__(self, key, cwd, prompt, **kw):
        super().__init__()
        self.key = key
        FakeWorker.sent.append(prompt)

    def run(self):
        pass

    def resolve(self, ok):
        pass

    def stop(self):
        pass

    def cancel(self):
        pass

    def kill_process(self):
        pass

    def set_allow_all(self, on):
        pass


class _Router:
    def __init__(self, intent="NEW"):
        self.intent = intent

    def classify(self, text, focused, cands, live=False, asked=None):
        return {"intent": self.intent, "index": None, "task": text}


def _agents(monkeypatch):
    monkeypatch.setattr(ap, "AgentWorker", FakeWorker)
    FakeWorker.sent = []
    router = _Router("NEW")
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: router))
    page = ap.AssistantPage(type("MW", (), {"_on_nav_clicked": lambda s, i: None})())
    page._primary = "claude"
    page._agent_ok = lambda agent: True
    calls = []
    page._notch = lambda m, *a: calls.append((m, a))
    return page, calls


def _wait_sent(n):
    return _pump(lambda: len(FakeWorker.sent) >= n, 5)


def test_auto_open_setting_saved(monkeypatch, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"live_chime": True}))
    with monkeypatch.context() as m:
        m.setattr(type(settings), "get_app_data_dir", lambda self: tmp_path)
        s2 = type(settings)()
        assert s2.assistant_auto_open is True, "ON by default — nothing changes unless you turn it off"
        (tmp_path / "settings.json").write_text(json.dumps({"assistant_auto_open": False}))
        assert type(settings)().assistant_auto_open is False and s2._cfg_dict()["assistant_auto_open"] is True
        (tmp_path / "settings.json").write_text(json.dumps({"assistant_auto_open": "no"}))
        assert type(settings)().assistant_auto_open is True, "a bad value is ignored"
    # the tab's switch
    page, L, AP = _tab()
    assert page.open_toggle.isChecked() and "Open answers automatically" == page._open_lbl.text()
    n = len(saves)
    page.open_toggle.click()
    assert settings.assistant_auto_open is False and len(saves) == n + 1
    page.open_toggle.click()
    assert settings.assistant_auto_open is True


def test_auto_open_on_the_agents_page(monkeypatch):
    """Hands-free tasks: ON → the card pins open + flashes, the answer comes up; OFF → it stays tucked (no pin, no
    flash, no open on done); "needs your OK" still comes up. A plain error stays tucked when OFF; a sign-in step
    always comes up."""
    page, calls = _agents(monkeypatch)
    req = type("Req", (), {"tool": "Bash", "tool_input": {"command": "ls"}})()
    for on in (True, False):
        settings.assistant_auto_open = on
        calls.clear()
        page.run_voice_task(f"make page {on}", source="live", tag=1)
        assert _wait_sent(1 if on else 2)
        s = page._focused
        assert ("present", (on,)) in calls and (("flash", ()) in calls) == on, (on, calls)
        assert ("open_full", ()) not in calls
        calls.clear()
        page._on_approval(s.key, req)
        # needs your OK: OFF → it comes up (the agent waits for you). ON → the card is already pinned open; Windows
        # calls open_full here too (a no-op on an open card) — so only the OFF case is required.
        assert ("show_needs_you", ("Bash", {"command": "ls"})) in calls
        if not on:
            assert ("open_full", ()) in calls, "needs your OK: OFF → it comes up"
        calls.clear()
        s.result_text = "Done."
        page._on_result(s.key, f"sid-{on}", 0.0)
        assert any(m == "show_done" for m, _ in calls) and (("open_full", ()) in calls) == on, (on, calls)
        page._on_done(s.key)
    # (OFF still) a plain error: tucked; a sign-in step: always comes up (you have to do something)
    page.run_voice_task("and again", source="live", tag=2)
    assert _wait_sent(3)
    s = page._focused
    calls.clear()
    page._on_error(s.key, "Something broke.")
    assert any(m == "show_error" for m, _ in calls) and ("open_full", ()) not in calls, "a plain error: tucked"
    page._on_done(s.key)
    page.run_voice_task("and once more", source="live", tag=3)
    assert _wait_sent(4)
    s = page._focused
    page._agent_ok = lambda agent: False                        # (no hand-over to the other agent)
    from app.core.agent_runner import SETUP_SIGNIN
    calls.clear()
    page._on_error(s.key, SETUP_SIGNIN)
    assert ("open_full", ()) in calls, "a sign-in step: always comes up (you have to do something)"
    page._on_done(s.key)
    # the voice chord (Shift+Z) with auto-open ON: its "Routing…" card pins + flashes, as before
    page._agent_ok = lambda agent: True
    settings.assistant_auto_open = True
    calls.clear()
    page.run_voice_task("one more", source="voice")
    assert _wait_sent(5)
    assert ("present", (True,)) in calls and ("flash", ()) in calls
    page._on_done(page._focused.key)


def test_auto_open_off_voice_chord_stays_tucked(monkeypatch):
    page, calls = _agents(monkeypatch)
    settings.assistant_auto_open = False
    page.run_voice_task("one more", source="voice")             # Shift+Z (the Mac's fn + ⌃): the routing card too
    assert _wait_sent(1)
    assert ("present", (False,)) in calls and ("flash", ()) not in calls, calls
    assert ("present", (True,)) not in calls, "OFF: the card is never pinned open by itself"


def test_auto_open_real_card():
    """The real top card: present(pin=False) stays tucked through the answer; open_full opens it; ON pins it."""
    from app.ui.claude_overlay import ClaudeOverlay
    ov = ClaudeOverlay()
    try:
        ov.present(False)
        assert ov._minimized and not ov._pinned
        ov.show_done("sid", 0.0, "make a page", "I made the page.")
        app.processEvents()
        assert ov._minimized and not ov._pinned, "the answer didn't pop up"
        ov.open_full()
        assert not ov._minimized and ov._pinned, "…and opens when you click / it needs you"
        ov._pinned = False
        ov.minimize_panel()
        ov.present()
        assert ov._pinned, "ON (the default): pinned like before"
    finally:
        ov.hide()
        ov.close()
        ov.deleteLater()
