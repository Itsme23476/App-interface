"""Filect Assistant — your screen, two ways — Windows port of the Mac tests/test_live_screen.py (no real agents; your
settings are never written; captures go to a temp dir):
  · "Look at my screen…": captured (in memory) the moment it's clearly speech; written to disk and attached ONLY if
    the request is about the screen (router flag or the word check), else never written
  · Drawing with Shift + Ctrl (only while Filect Assistant is on): press → draw (the moment both are down — held or
    tapped), press again → screenshot; several in a row ALL go with the next request; said while drawing → waits for
    the drawing and goes with it; kept if that request is ignored; dropped after 60 s / Esc; Esc while drawing cancels
    it + what you said; Ctrl+Shift+Tab takes back the drawing it started
  · the Shift + Ctrl detector: either order; held = still fires once; Alt / Win or Ctrl+Tab-then-Shift → not ours
  · real Windows bits (replacing the Mac's Quartz / Carbon checks): the key hook's events through the listener, a real
    off-thread capture of a bit of the screen (in memory only), two global shortcuts that don't steal each other's
    WM_HOTKEY, two ink layers with their own titles
Run: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_live_screen.py -q"""
import ctypes
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
from PySide6.QtCore import QObject, QRect, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.core import agent_runner as ar  # noqa: E402
from app.core import agent_sessions as asess  # noqa: E402
from app.core import codex_runner as crun  # noqa: E402
from app.core import intent_router as ir  # noqa: E402
from app.core import live_listener as ll  # noqa: E402
from app.core import mic_helper  # noqa: E402
from app.core import speech_out as so  # noqa: E402
from app.core import supabase_client as sc  # noqa: E402
from app.core import transcription as tx  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402
from app.ui import live_page as lp  # noqa: E402
from app.ui import screen_ink  # noqa: E402

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
    hook; screenshots go to a temp dir; every live_* / assistant_* setting starts known and is restored after."""
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
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
    yield
    while _PAGES:
        _PAGES.pop().shutdown()


def _pump(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


def _img():
    im = QImage(200, 120, QImage.Format_ARGB32_Premultiplied)
    im.fill(QColor("#336699"))
    return im


# ---- the router + the Agents page --------------------------------------------------------------------
class _Router:
    def __init__(self, screen=False, intent="NEW"):
        self.screen, self.intent = screen, intent

    def classify(self, text, focused, cands, live=False, asked=None):
        return {"intent": self.intent, "index": None, "task": text, "screen": self.screen}


def _agents():
    page = ap.AssistantPage(type("MW", (), {})())
    page._primary = "claude"
    runs, used, reports = [], [], []
    page._notch = lambda *a: None

    def fake_run(prompt, bypass=None, resume=None, cwd=None, images=None, agent=None):
        runs.append((prompt, list(images or [])))
    page.run_task = fake_run
    page.live_screen_used.connect(lambda tag: used.append(tag))
    page.live_routed.connect(lambda tag, o, t, k: reports.append((tag, o)))
    return page, runs, used, reports


def test_router_screen_flag():
    d = ir._parse('{"intent":"NEW","index":null,"task":"x","screen":true}', "x", allow_ignore=True)
    assert d["screen"] is True
    assert ir._parse('{"intent":"NEW","index":null,"task":"x","screen":true}', "x")["screen"] is False, "Shift+Z: never"
    assert '"screen"' in ir._build_prompt("hi", None, [], live=True) and "screen" not in ir._build_prompt("hi", None, [])


def test_screen_attached_only_when_about_the_screen(monkeypatch, tmp_path):
    page, runs, used, reports = _agents()
    calls = []
    shot = str(tmp_path / "screen-test.png")

    def capture_cb():
        calls.append(1)
        return shot
    cases = [  # (router says screen, what you said, should attach)
        (True, "Help me with what I'm working on", True),
        (False, "What's this error?", True),                      # the word check catches it
        (False, "Create a folder called kitchen", False),
        (True, "ignored chatter", False),                          # IGNORE: nothing attached
    ]
    for i, (flag, text, want) in enumerate(cases):
        r = _Router(flag, "IGNORE" if "chatter" in text else "NEW")
        monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls, r=r: r))
        calls.clear()
        n = len(reports)
        page.run_voice_task(text, source="live", tag=i, screen=capture_cb)
        assert _pump(lambda: len(reports) > n, 3), text
        attached = bool(runs) and runs[-1][1] == [shot] and reports[-1] == (i, "new")
        assert attached == want, (text, runs[-1:], reports[-1])
        assert (len(calls) == 1) == want, f"the capture is only written when it's used: {text}"
        assert (i in used) == want
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: _Router(True)))
    n = len(runs)
    page.run_voice_task("look at my screen")                       # Shift+Z: no capture involved
    assert _pump(lambda: len(runs) > n, 3) and runs[-1][1] == []


# ---- the tab ------------------------------------------------------------------------------------------
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

    def __init__(self):
        super().__init__()
        self.on, self.esc_filter, self.learned = False, None, []

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


def _tab(capture=None):
    mw = type("MW", (), {})()
    mw.assistant_page = FakeAP()
    L = FakeListener()
    page = lp.LivePage(mw, listener=L, capture=capture or (lambda rect: _img()))
    page._ink = FakeInk()
    return page, L, mw


def test_tab_screen_capture_lifecycle():
    shots = _SHOTS["dir"]
    page, L, mw = _tab()
    page.start()
    L.utterance_ended.emit(1)
    L.speech_confirmed.emit(1)
    assert 1 in page._captures
    assert _pump(lambda: page._captures[1].get("img") is not None, 2), "captured off the UI thread"
    assert not os.listdir(shots), "nothing is written while you talk"
    L.heard.emit(1, "what's this error on my screen")
    call = mw.assistant_page.calls[-1]
    assert call["images"] == [] and callable(call["screen"])
    path = call["screen"]()                                            # the Agents page uses it
    assert _pump(lambda: os.path.exists(path) and os.path.exists(screen_ink.thumb_path(path)), 3), \
        "written to disk only now (+ its thumbnail)"
    assert QImage(path).width() == 200
    mw.assistant_page.live_screen_used.emit(1)
    mw.assistant_page.live_routed.emit(1, "new", "Fix The Error", "k1")
    assert page._rows[1].outcome.text() == "Started “Fix The Error”  · 📸 with your screen  →"
    assert mw.assistant_page.ov.notes[-1] == "📸  Screen attached" and 1 not in page._captures
    # not about the screen → the capture is simply dropped, nothing written
    L.speech_confirmed.emit(2)
    _pump(lambda: page._captures.get(2, {}).get("img") is not None, 2)
    L.heard.emit(2, "create a folder called kitchen")
    files = sorted(os.listdir(shots))
    mw.assistant_page.live_routed.emit(2, "new", "Kitchen", "k2")
    _pump(lambda: False, 0.2)
    assert 2 not in page._captures and sorted(os.listdir(shots)) == files
    # a cough / Esc: released
    L.speech_confirmed.emit(3)
    L.dropped.emit(3, "", "cancelled")
    assert 3 not in page._captures
    # bounded memory
    for u in range(10, 16):
        L.speech_confirmed.emit(u)
    assert len(page._captures) <= lp.MAX_CAPTURES
    page.stop()


def test_chord_tap():
    from app.core.live_listener import ChordTap
    c = ChordTap()
    f, k = c.flags, c.key
    assert f(True, False, False) is None and f(True, True, False) == "press", "Shift then Ctrl: fires as both go down"
    assert f(True, True, False) is None and f(False, True, False) is None, "once per press, however long it's held"
    assert f(False, False, False) is None
    assert f(False, True, False) is None and f(True, True, False) == "press", "Ctrl then Shift"
    f(False, False, False)
    # REGRESSION (his first try): press and HOLD, waiting → it fires on the press, not on letting go
    assert f(True, True, False) == "press"
    f(False, False, False)
    assert f(True, False, False) is None and f(False, False, False) is None, "Shift alone: nothing"
    assert f(False, True, False) is None and f(False, False, False) is None, "Ctrl alone: nothing"
    # Ctrl+Shift+Tab: fires, then Tab → spoil (take the drawing back); the rest of that hold is ignored
    f(False, True, False)
    assert f(True, True, False) == "press" and k() == "spoil" and k() is None
    assert f(True, True, False) is None
    f(False, False, False)
    # Ctrl + Tab first, THEN Shift (going back through tabs): a shortcut from the start — never fires
    f(False, True, False)
    k()
    assert f(True, True, False) == "blocked" and f(True, True, False) is None
    f(False, False, False)
    # Alt / Win held → not ours; one joining after it fired → spoil
    assert f(True, True, True) == "blocked"
    f(False, False, False)
    assert f(True, True, False) == "press" and f(True, True, True) == "spoil"
    f(False, False, False)
    assert k() is None, "a key with nothing held: harmless"
    assert f(True, True, False) == "press", "and the next clean press counts again"


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
        def stop():
            pass
        stop.proc = None
        self.calls.append({"vp": voice_processing, "on_chunk": on_chunk, "on_end": on_end, "stop": stop})
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
    reply = "Look at my screen and tell me what this error means"
    made = []

    def __init__(self):
        super().__init__()
        self.cancelled = False
        FakeTx.made.append(self)

    def cancel(self):
        self.cancelled = True
        self.stop_recording()

    def start(self):
        pass

    def feed(self, data):
        pass

    def stop_recording(self):
        r = FakeTx.reply
        QTimer.singleShot(10, lambda: self.finished.emit(r))


class FakeMonitor:
    others, locked, exclude = [], False, set()

    def start(self):
        pass

    def stop(self):
        pass


def _listener():
    FakeTx.made.clear()
    mic = FakeMic()
    L = ll.LiveListener(open_mic=mic, make_transcriber=FakeTx, monitor_factory=lambda: FakeMonitor())
    rec = {"heard": [], "dropped": []}
    L.heard.connect(lambda u, t: rec["heard"].append((u, t)))
    L.dropped.connect(lambda u, t, w: rec["dropped"].append((u, t, w)))
    return L, mic, rec


# The key hook's events (live_listener._KeyHook → LiveListener._key_event): ("mods", shift=1 | ctrl=2 | Alt/Win=4),
# ("key", vk), ("click", 0). (Replaces the Mac's real NSEvents.)
SHIFT, CTRL, OTHER = 1, 2, 4
VK_TAB, VK_ESC = 0x09, 0x1B


def test_key_events_through_the_listener():
    """The listener's real handler fed the key hook's events (no keystrokes anywhere)."""
    L, _, _ = _listener()
    got, late = [], []
    L.draw_tap.connect(lambda: got.append("tap"))
    L.draw_spoiled.connect(lambda: got.append("spoil"))
    L.esc_late.connect(lambda: late.append(1))

    def feed(*evs):
        for e in evs:
            L._key_event(e[0], e[1] if len(e) > 1 else 0, 0)
        _pump(lambda: False, L.DRAW_WAIT_SEC + 0.1)          # (Shift + Ctrl draws after a moment)
    L.start()
    try:
        feed(("mods", SHIFT), ("mods", SHIFT | CTRL))                    # press and hold…
        assert got == ["tap"], got
        feed(("mods", SHIFT | CTRL), ("mods", SHIFT | CTRL))             # …held: the same state again changes nothing
        feed(("mods", SHIFT), ("mods", 0))
        assert got == ["tap"], got
        feed(("mods", CTRL), ("mods", SHIFT | CTRL), ("key", VK_TAB), ("mods", CTRL), ("mods", 0))
        assert got == ["tap"], "Ctrl+Shift+Tab pressed at once: never drawn, nothing to take back"
        feed(("mods", CTRL), ("mods", SHIFT | CTRL))
        feed(("key", VK_TAB), ("mods", CTRL), ("mods", 0))
        assert got == ["tap", "tap", "spoil"], got                       # Tab only after it drew: taken back
        feed(("mods", SHIFT), ("mods", SHIFT | CTRL), ("mods", SHIFT | CTRL | OTHER), ("mods", SHIFT), ("mods", 0))
        assert got == ["tap", "tap", "spoil"], "Shift, then Ctrl + Win (mute): no drawing at all"
        feed(("mods", OTHER), ("mods", OTHER | SHIFT), ("mods", OTHER | SHIFT | CTRL), ("mods", 0))
        assert got == ["tap", "tap", "spoil"], "Win / Alt + Shift + Ctrl: not ours"
        feed(("mods", SHIFT | CTRL), ("click",), ("mods", 0))           # a click doesn't spoil a drawing press
        assert got == ["tap", "tap", "spoil", "tap"], got
        feed(("key", VK_ESC))                                            # Esc with nothing in flight → the page
        assert late == [1], "Esc reaches the listener (→ esc_late for the router stage)"
    finally:
        L.stop()
    feed(("mods", SHIFT), ("mods", SHIFT | CTRL), ("mods", 0))
    assert got == ["tap", "tap", "spoil", "tap"], "off: nothing"


def test_tab_drawing_flow(monkeypatch):
    page, L, mw = _tab()
    AP, ov = mw.assistant_page, mw.assistant_page.ov
    L.draw_tap.emit()
    assert not page._drawing, "nothing while Filect Assistant is off"
    page.start()
    assert "Shift" in page.draw_key.text() and "Ctrl" in page.draw_key.text()
    # 1) tap → draw, tap → ready; again → 2 ready; then say it → ONE request with BOTH
    L.draw_tap.emit()
    assert page._drawing and page._ink.log[:2] == ["begin", ("drawing", True, "press Shift + Ctrl to capture · Esc cancels")]
    assert ov.notes[-1] == "✏️  Drawing — press Shift + Ctrl to capture"
    L.speech_confirmed.emit(1)
    assert 1 not in page._captures, "while drawing: no separate screen capture"
    L.draw_tap.emit()
    assert not page._drawing and len(page._drawings) == 1 and ov.notes[-1] == "📸  Drawing ready — say what to do"
    assert page._drawn_timer.isActive()
    L.draw_tap.emit()
    assert page._drawing and ov.notes[-1] == "✏️  Drawing 2 — press Shift + Ctrl to capture" and not page._drawn_timer.isActive()
    L.draw_tap.emit()
    assert len(page._drawings) == 2 and ov.notes[-1] == "📸  2 drawings ready — say what to do"
    both = list(page._drawings)
    L.speech_confirmed.emit(2)
    assert 2 not in page._captures, "drawings waiting: they're the screenshots, no extra capture"
    L.heard.emit(2, "compare these two")
    call = AP.calls[-1]
    assert call["images"] == both and call["screen"] is None and call["tag"] == 2
    assert page._drawings == [], "in flight with that request"
    # …ignored → BOTH back to waiting (same order); the next request takes them; used → gone
    AP.live_routed.emit(2, "ignored", "", "")
    assert page._drawings == both and page._drawn_timer.isActive()
    L.heard.emit(3, "which one is bigger")
    assert AP.calls[-1]["images"] == both
    AP.live_routed.emit(3, "new", "Compare Two", "k3")
    assert page._drawings == [] and page._rows[3].outcome.text() == "Started “Compare Two”  · ✏️ with your 2 drawings  →"
    assert ov.notes[-1] == "📸  Your 2 drawings went with it"
    L.heard.emit(4, "and make it blue")
    assert AP.calls[-1]["images"] == [], "drawings go with ONE request"
    AP.live_routed.emit(4, "continued", "Compare Two", "k3")
    # 2) said WHILE drawing → waits, goes the moment you capture (YOUR TEST: it used to go without it)
    L.draw_tap.emit()
    n = len(AP.calls)
    L.heard.emit(5, "what did I just circle")
    assert len(AP.calls) == n, "not sent while you're still drawing"
    assert page._rows[5].outcome.text() == "Waiting for your drawing — press Shift + Ctrl to send it"
    L.heard.emit(6, "and why is it red")                                # a 2nd sentence, same drawing → one request
    assert 6 not in page._rows and page._rows[5].text.text() == "“what did I just circle and why is it red”"
    L.draw_tap.emit()
    call = AP.calls[-1]
    assert len(AP.calls) == n + 1 and call["tag"] == 5 and call["text"] == "what did I just circle and why is it red"
    assert len(call["images"]) == 1 and page._drawings == [] and not page._drawing
    AP.live_routed.emit(5, "new", "Circled Thing", "k5")
    assert L.learned[-2:] == [5, 6], "both sentences were you"
    assert page._rows[5].outcome.text() == "Started “Circled Thing”  · ✏️ with your drawing  →"
    # 3) Esc while drawing: that drawing + what you said during it are cancelled; earlier ones keep waiting
    L.draw_tap.emit()
    L.draw_tap.emit()                                                  # one ready
    L.draw_tap.emit()                                                  # drawing #2
    L.heard.emit(7, "look at this")
    r = L.esc_filter()
    assert r == "speech" and not page._drawing and len(page._drawings) == 1
    assert page._rows[7].outcome.text() == "Cancelled (Esc) — not sent" and ov.notes[-1] == "Drawing cancelled"
    assert page._drawn_timer.isActive()
    # Esc with drawings waiting → dropped (and Esc goes on to cancel a request in flight)
    assert L.esc_filter() is False and page._drawings == [] and ov.notes[-1] == "Drawing dropped"
    # 4) a minute with nothing said → dropped
    with monkeypatch.context() as m:
        m.setattr(lp, "DRAWN_KEEP_MS", 150)
        for _ in range(4):
            L.draw_tap.emit()
        assert len(page._drawings) == 2
        assert _pump(lambda: page._drawings == [], 2) and ov.notes[-1] == "2 drawings dropped"
    # 5) at most 5
    for _ in range(lp.MAX_DRAWINGS):
        L.draw_tap.emit()
        L.draw_tap.emit()
    assert len(page._drawings) == lp.MAX_DRAWINGS
    L.draw_tap.emit()
    assert not page._drawing and ov.notes[-1] == "5 drawings is the most — say what to do"
    # 5b) Ctrl+Shift+Tab: the drawing that press started is quietly taken back; after you've drawn, it stays
    page._drawings = []
    L.draw_tap.emit()
    n_notes = len(ov.notes)
    L.draw_spoiled.emit()
    assert not page._drawing and len(ov.notes) == n_notes, "taken back, no note"
    L.draw_tap.emit()
    page._ink._strokes = [[1, 2]]
    L.draw_spoiled.emit()
    assert page._drawing, "you'd already drawn: kept"
    page._ink._strokes = []
    L.draw_tap.emit()
    page._drawings = []
    # 6) Stop: drawing ends, waiting things are dropped, the request said during it isn't sent
    L.draw_tap.emit()
    L.heard.emit(8, "and this")
    n = len(AP.calls)
    page.stop()
    assert not page._drawing and page._drawings == [] and page._waiting is None and len(AP.calls) == n
    assert page._rows[8].outcome.text() == "Not sent — you clicked Stop" and "end" in page._ink.log
    L.draw_tap.emit()
    assert not page._drawing, "a stray tap after Stop: nothing"


def test_old_drawing_setting_is_ignored(monkeypatch, tmp_path):
    """⌘⇧D (live_draw_shortcut) was never released; a settings.json that has it still loads, and it's simply not
    saved again."""
    (tmp_path / "settings.json").write_text(json.dumps({"live_draw_shortcut": "cmd+shift+d", "live_chime": False}))
    monkeypatch.setattr(type(settings), "get_app_data_dir", lambda self: tmp_path)
    s2 = type(settings)()
    assert s2.live_chime is False and "live_draw_shortcut" not in s2._cfg_dict()


# ---- the listener ---------------------------------------------------------------------------------------
def test_listener_speech_confirmed_and_esc_filter(monkeypatch):
    L, mic, rec = _listener()
    confirmed = []
    L.speech_confirmed.connect(lambda u: confirmed.append(u))
    used = {"v": False}
    L.set_esc_filter(lambda: used["v"])
    s = speech("Look at my screen and tell me what this error means.")
    L.start()
    try:
        mic.push(np.concatenate([silence(0.5), s[: len(s) // 2]]))
        assert confirmed == [1], confirmed
        used["v"] = True                                              # the page took the Esc entirely
        L._on_esc()
        assert L._cur is not None, "Esc went to the page, not to what you're saying"
        used["v"] = False
        mic.push(np.concatenate([s[len(s) // 2:], silence(3.0)]))
        assert _pump(lambda: rec["heard"], 2) and confirmed == [1]
        mic.push(np.concatenate([silence(0.5), speech("Hm."), silence(3.0)]))
        assert confirmed == [1], "a lone 'hm' never confirms (no screen capture for noise)"
    finally:
        L.stop()
    # Esc while drawing ("speech"): what you're saying is cancelled too — but a request already being checked by the
    # router is NOT (no esc_late)
    L2, mic2, rec2 = _listener()
    late = []
    L2.esc_late.connect(lambda: late.append(1))
    L2.set_esc_filter(lambda: "speech")
    L2.start()
    try:
        mic2.push(np.concatenate([silence(0.5), s[: len(s) // 2]]))
        assert L2._cur is not None
        L2._on_esc()
        assert L2._cur is None and late == [], "what you were saying is dropped; nothing else"
        L2._on_esc()                                                   # nothing being heard: still no esc_late
        assert late == []
    finally:
        L2.stop()
    # the Shift + Ctrl tap → draw_tap, only while on and not while a Filect shortcut holds the mic
    L3, _, _ = _listener()
    taps = []
    L3.draw_tap.connect(lambda: taps.append(1))
    L3._on_tap()
    assert taps == [], "off: nothing"
    L3.start()
    try:
        L3._on_tap()
        assert taps == [1]
        with monkeypatch.context() as m:
            m.setattr(mic_helper, "busy", lambda: True)             # Shift+Z's receive mode uses Shift / Ctrl itself
            L3._on_tap()
        assert taps == [1]
    finally:
        L3.stop()


# ---- real Windows (replaces the Mac's Carbon hotkeys + Quartz capture) ---------------------------------------
def test_two_global_shortcuts_dont_steal_each_other(monkeypatch):
    """REGRESSION guard (the Mac's Carbon bug, which Windows had too): every registration got the same hotkey id, so
    each WM_HOTKEY matched BOTH filters and one key ran the other's action. Two registrations (RegisterHotKey faked —
    nothing is registered system-wide), real WM_HOTKEY messages posted to this thread (no keystrokes) → each reaches
    only its own callback."""
    from app.ui import win_hotkey as wh
    u32 = ctypes.windll.user32
    registered = []
    monkeypatch.setattr(u32, "RegisterHotKey", lambda hwnd, hid, mods, vk: registered.append((hid, mods, vk)) or 1)
    monkeypatch.setattr(u32, "UnregisterHotKey", lambda hwnd, hid: 1)
    hits = []
    a = wh.register_global_hotkey(None, "ctrl+alt+shift+f23", lambda: hits.append("A"))
    b = wh.register_global_hotkey(None, "ctrl+alt+shift+f24", lambda: hits.append("B"))
    try:
        assert a and b and a[0] != b[0], (a, b)
        assert [r[2] for r in registered] == [0x86, 0x87] and all(r[1] == 0x7 for r in registered), registered
        tid = ctypes.windll.kernel32.GetCurrentThreadId()

        def fire(hid):
            n = len(hits)
            assert ctypes.windll.user32.PostThreadMessageW(tid, 0x0312, hid, 0)      # WM_HOTKEY
            assert _pump(lambda: len(hits) > n, 2)
            _pump(lambda: False, 0.05)
        fire(a[0])
        fire(b[0])
        fire(a[0])
        assert hits == ["A", "B", "A"], hits
    finally:
        wh.unregister_global_hotkey(*a)
        wh.unregister_global_hotkey(*b)
    n = len(hits)
    ctypes.windll.user32.PostThreadMessageW(ctypes.windll.kernel32.GetCurrentThreadId(), 0x0312, a[0], 0)
    _pump(lambda: False, 0.2)
    assert len(hits) == n, "unregistered: its filter is gone"


def test_real_capture_and_ink_titles():
    """A real capture of a bit of the screen under the cursor (GDI, off the UI thread, in memory only — never
    written); two ink layers with their own titles (the hands-free one is the page's)."""
    import threading
    px = screen_ink.screen_px_rect(QRect(0, 0, 64, 48))
    assert px is not None and px[2] >= 64 and px[3] >= 48, px
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("img", screen_ink.grab_px(px)))
    t.start()
    t.join(5)
    img = box.get("img")
    assert img is not None and (img.width(), img.height()) == (px[2], px[3]), "real capture, off the UI thread"
    assert screen_ink.grab_px((0, 0, 0, 0)) is None and screen_ink.grab_px(None) is None
    a, b = screen_ink.ScreenInk(), screen_ink.ScreenInk(title="Filect Assistant Ink")
    try:
        assert a.windowTitle() == "Filect Screen Ink" and b.windowTitle() == "Filect Assistant Ink"
    finally:
        a.deleteLater()
        b.deleteLater()
    page, L, mw = _tab()
    page._ink = None
    ink = page._ensure_ink()
    try:
        assert ink.windowTitle() == "Filect Assistant Ink", "the hands-free page's own layer"
    finally:
        ink.deleteLater()
