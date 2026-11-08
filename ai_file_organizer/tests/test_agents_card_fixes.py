"""The fixes from the 2026-11-08 manual test (headless; no mic, no keys, no `claude`; settings never written):

  * ✕ on the card mid-task closed it, but left it frozen on "Working" for a chat it no longer followed — Stop did
    nothing, the answer never came, and hovering never offered the input bar again. Now ✕ rests the card (the
    chat keeps running in the drawer), and the card follows ITS OWN chat (not whatever the page has open).
  * After Stop, late steps flipped the card back to "Working" with a live ■ Stop (it looked ignored → clicked
    again). Now "Stopping…" stays until the turn ends, and a second click changes nothing.
  * Esc right AFTER letting go of Shift+Z (you lift the letter to reach Esc) did nothing — now it still cancels
    while the words are written down and routed.
  * The chord's hotkey ids ran out after ~50 presses (Shift+Z dead until restart) — now reused.
  * Screenshot markers land where you took them (the transcriber had no words_so_far)."""
import asyncio
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtCore import QObject, QTimer, Signal
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
    monkeypatch.setattr(settings, "assistant_bypass", False)
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


# ---- fakes ----------------------------------------------------------------------------------------------
class _Runner:
    """A fake Claude Code turn: `slow` prompts step every 50 ms until stopped (late steps after Stop, like the
    real thing); "approve" prompts ask first; everything else answers at once."""
    calls = []

    def __init__(self, cwd, on_step, on_approval, on_result):
        self.on_step, self.on_approval, self.on_result = on_step, on_approval, on_result
        self.allow_all = False

    async def run(self, prompt, resume=None, **kw):
        _Runner.calls.append((prompt, resume))
        sid = resume or f"sid-{len(_Runner.calls)}"
        if "approve" in prompt:
            ok = await self.on_approval(ar.ApprovalRequest("Write", {"file_path": "C:\\x\\a.txt"}))
            self.on_step(ar.Step("text", text=f"approved={ok}"))
        if "slow" in prompt:
            for i in range(400):                    # ~20 s unless stopped (Stop force-cancels after 2.5 s)
                self.on_step(ar.Step("tool", tool="Bash", detail=f"step {i}", input={"command": "x"},
                                     tool_id=f"t{i}"))
                await asyncio.sleep(0.05)
        self.on_step(ar.Step("text", text=f"answer to {prompt}"))
        self.on_result(sid, 0.0)
        return sid

    async def interrupt(self):
        pass                                        # slow to honour, like Windows → the 2.5 s force-cancel


class _Router:
    answer = {"intent": "NEW", "index": None}
    delay = 0.0

    def classify(self, text, focused, cands, **kw):
        time.sleep(_Router.delay)
        return dict(_Router.answer, task=text)


class _PageMW:
    pass


@pytest.fixture
def page(monkeypatch):
    from app.core import intent_router as ir
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: _Router()))
    _Runner.calls, _Router.answer, _Router.delay = [], {"intent": "NEW", "index": None}, 0.0
    monkeypatch.setattr(ar, "AgentRunner", _Runner)
    p = ap.AssistantPage(_PageMW())
    yield p
    p.shutdown()


def _sess(page, prompt):
    return next(s for s in page._sessions.values() if s.prompt == prompt)


# ---- ✕ mid-task ------------------------------------------------------------------------------------------
def test_x_mid_task_rests_the_card_and_the_chat_keeps_running(page):
    page.run_voice_task("slow count to a big number")
    assert _wait(lambda: page._sessions and next(iter(page._sessions.values())).running)
    s = next(iter(page._sessions.values()))
    ov = page._overlay
    ov.open_full()
    assert _wait(lambda: ov._state == "working" and not ov._minimized)
    ov._on_dismiss()                                          # ✕
    assert ov._state == "idle" and ov._minimized, (ov._state, ov._minimized)
    assert page._card_key is None and page._focused_key is None
    assert s.running, "✕ must not stop the chat"
    assert not ov._handle._timer.isActive(), "the resting lip still breathes (CPU) after ✕"
    # hovering the top now offers the input bar again (the user's "I can't open it")
    ov._hover_in = True
    ov._show_hover_bar()
    assert ov._hover_bar_on, "the hover bar won't show after ✕ mid-task"
    ov._hide_hover_bar()
    page._on_stop_task()                                      # (no page chat → no-op)
    page._stop_session(s)                                     # end it; the card stays at rest
    assert _wait(lambda: not s.running, 6)
    _pump(0.1)
    assert ov._state == "idle", f"a chat the card let go of repainted it: {ov._state}"


def test_x_during_routing_runs_it_in_the_drawer_and_the_card_stays_shut(page, monkeypatch):
    pings = []                                                # (Windows has no tray toast — the chime is the cue)
    monkeypatch.setattr(ap, "_play_chime", lambda ok=True: pings.append(ok))
    _Router.delay = 0.4
    page.run_voice_task("what is two plus two")
    ov = page._overlay
    ov._on_dismiss()                                          # ✕ while it says "Routing…"
    assert _wait(lambda: len(_Runner.calls) == 1), "✕ isn't Stop — the request should still run"
    assert _wait(lambda: page._sessions and not next(iter(page._sessions.values())).running)
    _pump(0.1)
    assert ov._minimized and ov._state == "idle", "the card popped back open after you closed it"
    assert page._card_key is None
    assert pings == [True], "no 'task finished' chime for the closed chat"
    page.run_voice_task("and a new one")                      # the card works normally afterwards
    assert _wait(lambda: len(_Runner.calls) == 2)
    assert _wait(lambda: ov._state == "done")


# ---- the card follows ITS chat ---------------------------------------------------------------------------
def test_card_follows_its_own_chat_when_the_page_picks_another(page):
    page.run_task("quick question")
    assert _wait(lambda: not _sess(page, "quick question").running)
    b = _sess(page, "quick question")
    page.run_voice_task("slow build the site")
    assert _wait(lambda: any(s.prompt == "slow build the site" and s.running for s in page._sessions.values()))
    a = _sess(page, "slow build the site")
    ov = page._overlay
    assert page._card_key == a.key
    page._focus(b.key)                                        # the page shows the other chat now
    assert page._card_key == a.key and page._focused_key == b.key
    n = len(a.entries)
    assert _wait(lambda: len(a.entries) > n + 2)
    _pump(0.1)
    assert ov._state == "working" and "step" in ov._tool_chip.text(), "the card stopped hearing its chat"
    page._on_card_stop()                                      # the CARD's Stop stops the card's chat
    assert a.stopping and not b.stopping
    assert _wait(lambda: not a.running, 6)
    _pump(0.1)
    assert ov._state == "done" and ov._stopped, "the card didn't show its chat stopped"


def test_card_approve_answers_the_cards_chat_never_the_pages(page):
    page.run_task("approve the first file")
    assert _wait(lambda: _sess(page, "approve the first file").approval_req is not None)
    a = _sess(page, "approve the first file")
    page._from_page = True                                    # (a second chat, sent from the page)
    page.run_task("approve the second file")
    page._from_page = False
    assert _wait(lambda: _sess(page, "approve the second file").approval_req is not None)
    b = _sess(page, "approve the second file")
    page._render_card(a.key)                                  # the card shows A; the page shows B
    page._focused_key = b.key
    page._overlay._on_approve()                               # Approve on the card
    assert a.approval_req is None and b.approval_req is not None, "the card approved the page's chat"
    page._decide(False)                                       # Deny on the page → B
    assert _wait(lambda: not a.running and not b.running, 6)
    assert any("approved=True" in e.get("text", "") for e in a.entries)
    assert any("approved=False" in e.get("text", "") for e in b.entries)


def test_stale_card_is_resynced_by_stop_and_by_the_check(page):
    ov = page._ensure_notch()
    ov.present(True)
    ov.show_working("Writing x", "")                          # a card on "Working" with no chat behind it
    page._card_key = None
    page._on_card_stop()
    assert ov._state == "idle", "Stop on a stale card did nothing"
    ov.present(True)
    ov.show_working("Writing y", "")
    page._check_card()                                        # the 3 s safety net
    assert ov._state == "idle", "the safety net left the card stuck"
    page.run_task("quick one")
    s = _sess(page, "quick one")
    assert _wait(lambda: not s.running)
    ov.show_working("late", "")                               # stuck on Working for a FINISHED chat
    page._check_card()
    assert ov._state == "done", "a finished chat's card wasn't repainted from its real state"


# ---- Stop ------------------------------------------------------------------------------------------------
def test_stopping_stays_through_late_steps_and_a_second_click_is_ignored(page, monkeypatch):
    page.run_voice_task("slow long task")
    assert _wait(lambda: page._sessions and next(iter(page._sessions.values())).running)
    s = next(iter(page._sessions.values()))
    ov = page._overlay
    stops = []
    orig = s.worker.stop
    monkeypatch.setattr(s.worker, "stop", lambda: (stops.append(1), orig()))
    page._on_card_stop()
    page._on_card_stop()                                      # the user's "click it again"
    assert stops == [1], f"Stop was sent {len(stops)}×"
    n = len(s.entries)
    assert _wait(lambda: len(s.entries) > n + 3, 2)           # late steps keep arriving…
    _pump(0.05)
    btn = ov._primary_btn
    assert btn.text() == "Stopping…" and not btn.isEnabled(), f"late steps re-armed Stop: {btn.text()!r}"
    assert ov._status_label.text() == "Stopping…"
    assert _wait(lambda: not s.running, 6)
    _pump(0.1)
    assert ov._state == "done" and ov._stopped and btn.isEnabled()
    assert s.status == "stopped"


# ---- Esc just after letting go --------------------------------------------------------------------------
class _SlowRecorder(QObject):
    finished = Signal(str)
    error = Signal(str)
    level = Signal(float)
    capped = Signal()

    def __init__(self, *a, **k):
        super().__init__()

    def start(self):
        pass

    def stop_recording(self):
        QTimer.singleShot(300, lambda: self.finished.emit("make the button blue"))

    def words_so_far(self):
        return 0


class _EscHotkey(QObject):
    pressed = Signal()
    released = Signal()
    space = Signal(bool)
    escape = Signal()

    def __init__(self):
        super().__init__()
        self.holding_escape = False

    def start(self):
        return True

    def stop(self):
        pass

    def hold_escape(self, seconds=6.0):
        self.holding_escape = True

    def drop_escape(self):
        self.holding_escape = False


class _VoiceMW(QWidget):
    def __init__(self):
        super().__init__()
        self.sent, self.cancelled, self.routing = [], [], set()
        mw = self

        class _Page:
            def run_voice_task(self, text, images=None):
                mw.sent.append(text)
                mw.routing.add(len(mw.sent))
                return len(mw.sent)

            def is_routing(self, rid):
                return rid in mw.routing

            def cancel_routing(self, rid=None):
                mw.cancelled.append(rid)
                mw.routing.discard(rid)
                return True
        self.assistant_page = _Page()
        self.dictation_controller = None


class _Pill:
    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def voice(monkeypatch):
    import app.core.transcription as tr
    monkeypatch.setattr(tr, "StreamingTranscriber", _SlowRecorder)
    mw, hk = _VoiceMW(), _EscHotkey()
    vc = av.AgentVoiceController(mw, hotkey=hk)
    vc._pill = _Pill()
    return mw, hk, vc


def _speak(hk):
    hk.pressed.emit()
    time.sleep(0.3)
    hk.released.emit()


def test_esc_after_letting_go_while_it_is_written_down_cancels(voice):
    mw, hk, vc = voice
    _speak(hk)
    assert vc._state == "transcribing" and hk.holding_escape, "Esc isn't claimed after letting go"
    hk.escape.emit()                                          # the user's test 18: Z lifted, then Esc
    assert vc._state == "idle" and not hk.holding_escape
    _pump(0.5)
    assert mw.sent == [], "Esc right after letting go still sent it"


def test_esc_while_routing_cancels_the_request_then_esc_is_released(voice):
    mw, hk, vc = voice
    _speak(hk)
    assert _wait(lambda: mw.sent)
    assert vc._route_rid == 1 and hk.holding_escape
    hk.escape.emit()                                          # Esc while the card says "Routing…"
    assert mw.cancelled == [1] and not hk.holding_escape
    _speak(hk)                                                # next one: routed → Esc is let go by itself
    assert _wait(lambda: len(mw.sent) == 2)
    mw.routing.discard(2)
    assert _wait(lambda: not hk.holding_escape, 1), "Esc stayed claimed after the request became a chat"
    hk.escape.emit()
    assert mw.cancelled == [1], "Esc cancelled a request that had already started"


# ---- the chord engine -------------------------------------------------------------------------------------
def _fake_user32(monkeypatch):
    regs = {}

    class _U:
        def RegisterHotKey(self, hwnd, hid, mods, vk):
            if hid in regs:
                return 0
            regs[hid] = (mods, vk)
            return 1

        def UnregisterHotKey(self, hwnd, hid):
            regs.pop(hid, None)
            return 1
    import ctypes
    monkeypatch.setattr(ctypes.windll, "user32", _U(), raising=False)
    return regs


def test_chord_ids_are_reused_and_never_run_out(monkeypatch):
    regs = _fake_user32(monkeypatch)
    keys = {"Z": False}
    monkeypatch.setattr(ah, "_down", lambda vk: keys["Z"] if vk == ord("Z") else False)
    hk = ah.AgentHotkey(0, "shift+z", poll_ms=10)
    monkeypatch.setattr(hk._mouse, "start", lambda: None)
    monkeypatch.setattr(hk._mouse, "stop", lambda: None)
    presses = []
    hk.pressed.connect(lambda: presses.append(1))
    assert hk.start()
    used = set()
    for _ in range(150):                                      # ~3× the old limit
        keys["Z"] = True
        hk._on_hotkey(hk._main_id)
        used |= set(regs)
        keys["Z"] = False
        hk._poll()
        hk.hold_escape(1.0)
        used |= set(regs)
        hk.drop_escape()
    assert len(presses) == 150, f"the chord died after {len(presses)} presses"
    assert used <= set(hk._block), "ids outside the hotkey's own block"
    hk.stop()
    assert not regs


def test_escape_claim_after_letting_go_and_its_timeout(monkeypatch):
    regs = _fake_user32(monkeypatch)
    hk = ah.AgentHotkey(0, "shift+z", poll_ms=10)
    assert hk.start()
    esc = []
    hk.escape.connect(lambda: esc.append(1))
    hk.hold_escape(0.2)
    claimed = {v for k, v in regs.items() if k != hk._main_id}
    assert claimed == {(ah.MOD_NOREPEAT, ah.VK_ESCAPE), (ah.MOD_SHIFT | ah.MOD_NOREPEAT, ah.VK_ESCAPE)}, claimed
    hk._on_hotkey(next(iter(hk._post_esc)))
    assert esc == [1]
    assert _wait(lambda: not hk.holding_escape, 1), "the post-release Esc claim never timed out"
    assert set(regs) == {hk._main_id}
    hk.stop()


# ---- the crash: a cancelled transcriber freed by its own worker thread -----------------------------------
def test_cancelled_transcriber_is_never_freed_on_its_worker_thread(monkeypatch):
    """Esc right after letting go dropped the only reference while the worker thread was still writing the
    words down → when the thread ended it freed the QObject on the wrong thread → Filect vanished. Same
    steps here, for real threads: start, drop it, let the thread deliver + end, collect garbage."""
    import gc
    import threading
    from app.core import transcription as tr
    monkeypatch.setattr(tr.StreamingTranscriber, "_start_capture", lambda self: None)
    monkeypatch.setattr(tr.StreamingTranscriber, "_mute_output_if_enabled", lambda self: None)
    monkeypatch.setattr(tr.StreamingTranscriber, "_restore_output", lambda self: None)

    async def _session(self):
        import asyncio
        await asyncio.sleep(0.3)                 # "writing it down" — the user cancels meanwhile
        return "make the button blue", True
    monkeypatch.setattr(tr.StreamingTranscriber, "_session", _session)
    import weakref
    freed_on = []
    refs = []
    for _ in range(3):
        t = tr.StreamingTranscriber()
        t.start()
        th = t._ws_thread
        refs.append(weakref.ref(t, lambda _r: freed_on.append(threading.current_thread().name)))
        t.stop_recording()
        del t                                    # the controller's _cancel(): its reference is gone
        gc.collect()
        th.join(3)                               # the worker writes the words down, delivers, ENDS
        assert not th.is_alive()
        _pump(0.1)
        gc.collect()
    assert all(n == threading.main_thread().name for n in freed_on), \
        f"a transcriber was freed by its own worker thread ({freed_on}) — that's the crash"
    t = tr.StreamingTranscriber()                # the next start lets go of the finished ones, on the GUI thread
    t.start()
    gc.collect()
    assert not any(r() for r in refs), "finished transcribers were never let go"
    assert len(freed_on) == 3 and all(n == threading.main_thread().name for n in freed_on), freed_on
    t._ws_thread.join(3)


# ---- screenshot markers ----------------------------------------------------------------------------------
def test_words_so_far_counts_what_was_heard_so_far():
    from app.core import transcription as tr
    t = tr.StreamingTranscriber.__new__(tr.StreamingTranscriber)
    assert t.words_so_far() == 0
    t._live = (["change this one"], {"t": "and also"})
    assert t.words_so_far() == 5
    t._live = (["change this one"], {"t": "change this one and also"})   # a cumulative interim
    assert t.words_so_far() == 5
    assert av.with_markers("change this one and also that", [("a", True, 3), ("b", True, 6)]) == \
        "change this one [screenshot 1] and also that [screenshot 2]"
