"""Talking to the agent from anywhere, headless (offscreen; no mic, no keys, no `claude`; settings never written):
  * the chord engine: parse/label, press → claims, key-repeat ignored, Space down/up, Esc, release → claims gone
  * the voice controller: hold → listen → release → the words go to the Agents engine; Space TAP = drawing
    on, next tap = snapshot; HOLD = draw, release = snapshot; release while drawing = snapshot; Esc cancels;
    a chord tap is dropped; said nothing + a screenshot → the default prompt; several shots → [screenshot N];
    never while a dictation is recording
  * routing: NEW / FOLLOW_UP / RESUME through a fake router; Stop while routing drops the request
  * the card follows the focused chat (working → needs you → done), and a drawer pick opens it there."""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication.instance() or QApplication([])

from app.core import agent_runner as ar        # noqa: E402
from app.core.settings import settings         # noqa: E402
from app.ui import agent_hotkey as ah          # noqa: E402
from app.ui import agent_voice as av           # noqa: E402
from app.ui import assistant_page as ap        # noqa: E402


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_play_chime", lambda ok=True: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", str(tmp_path / "ws"))
    from app.core import agent_sessions as _asn
    monkeypatch.setattr(_asn, "_touched_path", lambda: tmp_path / "assistant_sessions.json")
    monkeypatch.setattr(ar, "_models_cache_path", lambda: tmp_path / "claude_models.json")
    yield


def _pump(secs=0.05):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def _wait(cond, secs=6.0):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# ---- the chord engine ---------------------------------------------------------------------------------
def test_parse_and_label():
    assert ah.parse("shift+z") == (ah.MOD_SHIFT, ord("Z"))
    assert ah.parse("ctrl+alt+f9") == (ah.MOD_CONTROL | ah.MOD_ALT, 0x78)
    assert ah.label("shift+z") == "Shift+Z" and ah.label("ctrl+win+k") == "Ctrl+Win+K"


def test_chord_engine_claims_space_esc_and_releases(monkeypatch):
    regs, unregs, keys = {}, [], {"Z": True, " ": False}

    class _U:
        def RegisterHotKey(self, hwnd, hid, mods, vk):
            regs[hid] = (mods, vk)
            return 1

        def UnregisterHotKey(self, hwnd, hid):
            unregs.append(hid)
            regs.pop(hid, None)
            return 1

    import ctypes
    monkeypatch.setattr(ctypes.windll, "user32", _U(), raising=False)
    monkeypatch.setattr(ah, "_down", lambda vk: keys["Z"] if vk == ord("Z") else keys[" "] if vk == 0x20 else False)
    hk = ah.AgentHotkey(0, "shift+z", poll_ms=10)
    monkeypatch.setattr(hk._mouse, "start", lambda: None)   # (the fake user32 has no mouse hook)
    monkeypatch.setattr(hk._mouse, "stop", lambda: None)
    ev = []
    hk.pressed.connect(lambda: ev.append("press"))
    hk.released.connect(lambda: ev.append("release"))
    hk.space.connect(lambda d: ev.append(f"space{int(d)}"))
    hk.escape.connect(lambda: ev.append("esc"))
    assert hk.start() and regs[hk._main_id] == (ah.MOD_SHIFT, ord("Z"))
    hk._on_hotkey(hk._main_id)
    hk._on_hotkey(hk._main_id)                        # key-repeat while held → ignored
    roles = sorted(hk._transient.values())
    assert roles == ["esc", "esc", "letter", "space", "space"], roles
    sp = next(h for h, r in hk._transient.items() if r == "space")
    es = next(h for h, r in hk._transient.items() if r == "esc")
    keys[" "] = True
    hk._on_hotkey(sp)
    hk._on_hotkey(sp)                                 # (no double "down")
    keys[" "] = False
    _pump(0.05)                                       # the poll sees Space up
    hk._on_hotkey(es)
    keys["Z"] = False
    _pump(0.05)                                       # the poll sees the chord's letter up
    assert ev == ["press", "space1", "space0", "esc", "release"], ev
    assert not hk._transient and set(regs) == {hk._main_id}, "the Space/Esc/letter claims outlived the hold"
    hk.stop()
    assert not regs


# ---- the voice controller ------------------------------------------------------------------------------
class _FakeRecorder(QObject):
    finished = Signal(str)
    error = Signal(str)
    level = Signal(float)
    capped = Signal()
    said = "make the button blue"

    def __init__(self, *a, **k):
        super().__init__()
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop_recording(self):
        self.stopped = True
        from PySide6.QtCore import QTimer
        QTimer.singleShot(10, lambda: self.finished.emit(_FakeRecorder.said))

    def words_so_far(self):
        return 2


class _FakeInk:
    def __init__(self):
        self.visible, self.drawing, self.shots, self.hint = False, False, 0, ""

    def isVisible(self):
        return self.visible

    def begin(self):
        self.visible = True

    def set_drawing(self, on, hint=""):
        self.drawing, self.hint = on, hint

    def snapshot(self):
        self.shots += 1
        return (f"C:\\shots\\s{self.shots}.png", True)

    def end(self):
        self.visible = self.drawing = False

    def clear(self):
        self.cleared = True


class _Pill:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _FakeHotkey(QObject):
    pressed = Signal()
    released = Signal()
    space = Signal(bool)
    escape = Signal()

    def start(self):
        return True

    def stop(self):
        pass


class _MW(QWidget):
    def __init__(self):
        super().__init__()
        self.sent = []
        page = self

        class _Page:
            def run_voice_task(self, text, images=None):
                page.sent.append((text, list(images or [])))
        self.assistant_page = _Page()
        self.dictation_controller = None


@pytest.fixture
def voice(monkeypatch):
    import app.core.transcription as tr
    monkeypatch.setattr(tr, "StreamingTranscriber", _FakeRecorder)
    mw = _MW()
    hk = _FakeHotkey()
    vc = av.AgentVoiceController(mw, hotkey=hk)
    vc._pill = _Pill()
    vc._ink = _FakeInk()
    return mw, hk, vc


def _hold(hk, secs=0.35):
    hk.pressed.emit()
    time.sleep(secs)


def test_hold_speak_release_sends_the_words(voice):
    mw, hk, vc = voice
    _hold(hk)
    assert vc._state == "recording" and vc._recorder.started
    hk.released.emit()
    assert _wait(lambda: mw.sent)
    assert mw.sent == [("make the button blue", [])] and vc._state == "idle"


def test_space_tap_draws_then_tap_snapshots_and_markers(voice):
    mw, hk, vc = voice
    _hold(hk)
    hk.space.emit(True)
    hk.space.emit(False)                       # a quick TAP → drawing stays on
    assert vc._inking and vc._ink.drawing and "tap Space again" in vc._ink.hint
    time.sleep(0.35)                           # (past the re-grip window)
    hk.space.emit(True)                        # the second tap → snapshot
    assert not vc._inking and vc._ink.shots == 1
    hk.space.emit(False)
    hk.space.emit(True)                        # a HOLD …
    time.sleep(0.4)
    hk.space.emit(False)                       # … release → snapshot 2
    assert vc._ink.shots == 2
    hk.released.emit()
    assert _wait(lambda: mw.sent)
    text, images = mw.sent[0]
    assert images == ["C:\\shots\\s1.png", "C:\\shots\\s2.png"]
    assert "[screenshot 1]" in text and "[screenshot 2]" in text, text


def test_release_while_drawing_snapshots_and_silence_gets_default_prompt(voice):
    mw, hk, vc = voice
    _FakeRecorder.said = ""
    try:
        _hold(hk)
        hk.space.emit(True)                    # drawing (held) — then the chord is let go
        hk.released.emit()
        assert _wait(lambda: mw.sent)
        assert mw.sent[0] == ("Look at what I've marked in red.", ["C:\\shots\\s1.png"])
    finally:
        _FakeRecorder.said = "make the button blue"


def test_esc_cancels_and_a_chord_tap_is_dropped(voice):
    mw, hk, vc = voice
    _hold(hk)
    hk.space.emit(True)
    hk.escape.emit()
    assert vc._state == "idle" and not vc._ink.visible
    hk.released.emit()
    _pump(0.1)
    assert mw.sent == [], "Esc must drop everything"
    hk.pressed.emit()                          # a quick accidental tap
    rec = vc._recorder
    cancelled = []
    rec.cancel = lambda: cancelled.append(1)   # (the real transcriber's cancel(): no batch re-upload)
    hk.released.emit()
    _pump(0.1)
    assert mw.sent == [] and vc._state == "idle"
    assert cancelled == [1] and not rec.stopped, \
        "a dropped tap CANCELS the recording (an empty stream is never re-uploaded for transcription)"


def test_right_click_toggles_drawing_and_esc_undoes_only_it(voice):
    mw, hk, vc = voice
    hk.right = getattr(hk, "right", None)
    _hold(hk)
    vc._on_right_click()                       # right-click → drawing on (left button draws)
    assert vc._inking and vc._ink.drawing and "right-click to capture" in vc._ink.hint
    vc._on_escape()                            # Esc on a right-click drawing: just that drawing goes
    assert vc._state == "recording" and not vc._inking, "Esc must only cancel the drawing, not the message"
    vc._on_right_click()
    vc._on_right_click()                       # right-click again → snapshot
    assert vc._ink.shots == 1 and not vc._inking
    hk.released.emit()
    assert _wait(lambda: mw.sent)
    assert mw.sent[0][1] == ["C:\\shots\\s1.png"]


def test_right_click_hook_is_scoped_to_the_hold(monkeypatch):
    """The mouse hook exists only while the chord is held (normal right-clicks are never touched)."""
    started, stopped = [], []
    hk = ah.AgentHotkey(0, "shift+z")
    monkeypatch.setattr(hk._mouse, "start", lambda: started.append(1))
    monkeypatch.setattr(hk._mouse, "stop", lambda: stopped.append(1))
    monkeypatch.setattr(hk, "_claim", lambda: None)
    monkeypatch.setattr(hk, "_release_claims", lambda: None)
    hk._main_id = 1
    hk._on_hotkey(1)
    assert started == [1] and stopped == []
    hk._end(emit=False)
    assert stopped == [1]


def test_never_while_dictation_records(voice):
    mw, hk, vc = voice
    mw.dictation_controller = type("D", (), {"_recorder": object()})()
    hk.pressed.emit()
    assert vc._state == "idle", "two mics at once"


# ---- routing + the card ---------------------------------------------------------------------------------
class _Router:
    answer = {"intent": "NEW", "index": None, "task": "x"}
    delay = 0.0

    def classify(self, text, focused, cands, **kw):
        time.sleep(_Router.delay)
        return dict(_Router.answer, task=text)


class _Runner:
    calls = []

    def __init__(self, cwd, on_step, on_approval, on_result):
        self.on_step, self.on_approval, self.on_result = on_step, on_approval, on_result
        self.allow_all = False

    async def run(self, prompt, resume=None, **kw):
        _Runner.calls.append((prompt, resume))
        if "approve" in prompt:
            await self.on_approval(ar.ApprovalRequest("Write", {"file_path": "C:\\x\\a.txt"}))
        self.on_step(ar.Step("text", text=f"answer to {prompt}"))
        self.on_result(resume or f"sid-{len(_Runner.calls)}", 0.0)

    async def interrupt(self):
        pass


class _PageMW:
    pass


@pytest.fixture
def page(monkeypatch):
    from app.core import intent_router as ir
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: _Router()))
    _Runner.calls = []
    monkeypatch.setattr(ar, "AgentRunner", _Runner)
    p = ap.AssistantPage(_PageMW())
    yield p
    p.shutdown()


def test_routing_new_followup_resume(page):
    _Router.answer = {"intent": "NEW", "index": None}
    page.run_voice_task("build a site")
    assert _wait(lambda: len(_Runner.calls) == 1 and not page._focused.running)
    first = page._focused_key
    _Router.answer = {"intent": "FOLLOW_UP", "index": None}
    page.run_voice_task("make it blue")
    assert _wait(lambda: len(_Runner.calls) == 2 and not page._sessions[first].running)
    assert _Runner.calls[1] == ("make it blue", "sid-1") and len(page._sessions) == 1
    _Router.answer = {"intent": "NEW", "index": None}
    page.run_voice_task("something else")
    assert _wait(lambda: len(page._sessions) == 2 and not page._focused.running)
    cands = page._build_voice_candidates("go back to the site one")
    idx = next(i for i, c in enumerate(cands) if c["key"] == first)
    _Router.answer = {"intent": "RESUME", "index": idx}
    page.run_voice_task("go back to the site one and add a footer")
    assert _wait(lambda: len(_Runner.calls) == 4 and not page._sessions[first].running)
    assert _Runner.calls[3][1] == "sid-1", "RESUME didn't continue the named chat"


def test_stop_while_routing_drops_the_request(page):
    _Router.answer, _Router.delay = {"intent": "NEW", "index": None}, 0.5
    try:
        page.run_voice_task("build a site")
        page._on_card_stop()                  # ■ on the card ("Routing…") — the page's ■ is for the page's chat
        _pump(0.9)
        assert _Runner.calls == [], "a request stopped while routing still ran"
    finally:
        _Router.delay = 0.0


def test_card_follows_the_focused_chat(page):
    _Router.answer = {"intent": "NEW", "index": None}
    page.run_voice_task("please approve this write")
    assert _wait(lambda: page._focused is not None and page._focused.approval_req is not None)
    ov = page._overlay
    assert ov is not None and ov._state == "needs_you", ov._state
    page._decide(True)
    assert _wait(lambda: not page._focused.running and page._focused.status == "done")
    _pump(0.1)
    assert ov._state == "done"
    assert "answer to please approve this write" in ov._done_view.toPlainText()
