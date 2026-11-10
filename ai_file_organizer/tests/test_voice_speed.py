"""Hands-free speed-ups — Windows port of the Mac tests/test_voice_speed.py (his "a few seconds pass before it
responds"):

  · a FINISHED sentence (the speech service closed it, ends in . ? !) is sent after 0.8 s of quiet, not after "send
    after" (2 s) — trailing off mid-sentence still waits the full time; its text is handed on early
  · the router decides while the final transcript arrives; that decision is used only if the final words, the chat
    you're in and what it asked you are the same — otherwise it decides again
  · while Filect Assistant listens, ONE Claude Code is already running (new chat, then the follow-up of the chat you
    just talked to); a request is handed straight to it; stop listening → it closes

Real listener + real AgentWorker; fake mic / transcriber / router / Claude Code. settings.json is never written.
(Not ported: the Mac's test_warm_on_intent — warming on Agents-tab / card typing and a held fn + ⌃ isn't part of the
Windows port: no agent_intent / _intent_throttled / INTENT_IDLE_SEC, and its dictation half is Quartz-only.)
Run: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_voice_speed.py -q"""
import asyncio
import inspect
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

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

_REAL_AGENT_RUNNER = ar.AgentRunner      # (the fixture replaces it — never run in tests)
_PAGES = []
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
    """Never the user's settings / app-data files / Claude Code chats, no sound, no analytics, no network, no real
    agents / router / key hook; every live_* / assistant_* setting starts known and is restored after."""
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    for k, v in _SETTINGS:
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(sc, "track", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_track", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_chime", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_track", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_play_chime", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap.AssistantPage, "_show_idle_overlays", lambda self: None)
    monkeypatch.setattr(ap.AssistantPage, "_load_recent", lambda self: None)
    monkeypatch.setattr(ap.AssistantPage, "_load_all_sessions", lambda self: None)
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
    orig = ap.AssistantPage.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        _PAGES.append(self)
    monkeypatch.setattr(ap.AssistantPage, "__init__", track)
    yield
    while _PAGES:
        _PAGES.pop().shutdown()


def pump(secs=0.2, cond=None):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        if cond is not None and cond():
            return True
        time.sleep(0.01)
    return cond() if cond is not None else None


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
    reply = "Create a new folder called invoices"
    delay = 10
    made = []

    def __init__(self):
        super().__init__()
        FakeTx.made.append(self)

    def cancel(self):
        self.stop_recording()

    def start(self):
        pass

    def feed(self, data):
        pass

    def stop_recording(self):
        r = FakeTx.reply
        QTimer.singleShot(FakeTx.delay, lambda: self.finished.emit(r))


class FakeMonitor:
    others, locked, exclude = [], False, set()

    def start(self):
        pass

    def stop(self):
        pass


class ClosedTx(FakeTx):
    closed = "Create a new folder called invoices."

    def text_so_far(self):
        return ClosedTx.closed


def _when_sent(closed: str, quiet: float = 3.0):
    """Speak, then `quiet` s of quiet: how long after you stopped did the utterance end (audio time)?"""
    FakeTx.made.clear()
    FakeTx.reply, FakeTx.delay = "Create a new folder called invoices", 10
    ClosedTx.closed = closed
    mic = FakeMic()
    L = ll.LiveListener(open_mic=mic, make_transcriber=ClosedTx, monitor_factory=lambda: FakeMonitor())
    ended, early = [], []
    L.utterance_ended.connect(lambda u: ended.append(L._det._t if L._det else None))
    L.early_text.connect(lambda u, t: early.append((u, t)))
    L.start()
    try:
        s = speech("Create a new folder on my desktop called invoices.")
        lead = silence(1.0)
        mic.push(np.concatenate([lead, s, silence(quiet)]))
        pump(1.0, lambda: ended)
    finally:
        L.stop()
    spoke_end = (len(lead) + len(s)) / RATE / ll.FRAME_SEC
    return (ended[0] - spoke_end) * ll.FRAME_SEC if ended else None, early


def test_finished_sentence_goes_sooner():
    quick, early = _when_sent("Create a new folder called invoices.")
    assert quick is not None and quick < 1.25, f"a finished sentence waits ~0.8 s of quiet, not 2 s ({quick})"
    assert early == [(1, "Create a new folder called invoices.")], early
    slow, early2 = _when_sent("Create a new folder called", quiet=8.0)   # trailing off: not finished
    # (2026-11-09: an unfinished sentence waits LONGER than "send after" — ×3 when it hangs on "called")
    assert slow is not None and 5.5 <= slow <= 6.6, f"mid-sentence waits 6 s for the rest ({slow})"
    plain, _ = _when_sent("Create a new folder for my invoices", quiet=6.0)   # not closed, doesn't hang
    assert plain is not None and 3.5 <= plain <= 4.6, f"not closed by the speech service: ×2 'send after' ({plain})"
    assert early2 == [(1, "Create a new folder called")], "its text is still handed on early, at the end"


def test_pause_or_mute_never_joins():
    """A finished sentence went early; then the mic was muted (Filect talking — on Windows always: no echo
    cancellation) or the assistant paused (a Filect shortcut, another app on the mic). What you say NEXT is a new
    request — it must not be "joined" with the earlier one. (Bug found 2026-11-08: mute / pause restart the
    listener's clock at 0, and the "join until" time of the earlier request was still set — so the next utterance
    matched it.) Talking on right away with no pause still joins."""

    def run(between):
        FakeTx.made.clear()
        FakeTx.reply, FakeTx.delay = "What's the weather in Tokyo", 10
        ClosedTx.closed = "What's the weather in Tokyo?"
        mic = FakeMic()
        L = ll.LiveListener(open_mic=mic, make_transcriber=ClosedTx, monitor_factory=lambda: FakeMonitor())
        quick, resumed = [], []
        L.quick_sent.connect(lambda u, left: quick.append(u))
        L.resumed.connect(lambda a, b: resumed.append((a, b)))
        L.start()
        try:
            mic.push(np.concatenate([silence(1.0), speech("What's the weather in Tokyo?"), silence(0.9)]))
            assert pump(2.0, lambda: quick), "the finished sentence went early"
            between(L)
            pump(0.3)
            mic.push(np.concatenate([speech("Open my downloads folder."), silence(3.0)]))
            pump(1.5)
        finally:
            L.stop()
        return resumed

    assert run(lambda L: None), "talking on right away still joins (the guard still works)"
    assert run(lambda L: (L.set_muted(True), L.set_muted(False))) == [], "after a mute: a NEW request, not joined"

    def pause_then_resume(L):
        L._pause("hotkey", "Paused while you use a Filect shortcut")
        L._tick()                                   # nothing pauses it any more…
        pump(1.1)                                   # …clear for a second (the real rule) → it resumes
        L._tick()
        assert L._paused is None and L.on, "resumed"
    assert run(pause_then_resume) == [], "after a pause: a NEW request, not joined"


# ---- the Agents page --------------------------------------------------------------------------------------
def _page(monkeypatch):
    """An Agents page for these tests. It does NOT load your saved chats in the background (the fixture): these
    tests are about routing / warm-up, and a list that finished loading mid-test changed the candidates between the
    early and the final decision. The chats the router may pick = this test's own chats."""
    monkeypatch.setattr(settings, "assistant_bypass", True)
    monkeypatch.setattr(settings, "live_speak", False)
    page = ap.AssistantPage(type("MW", (), {"_on_nav_clicked": lambda s, i: None})())
    page._build_voice_candidates = lambda text, p=page: [
        {"kind": "live", "key": s.key, "id": s.session_id or s.resume, "cwd": s.cwd, "title": s.title, "agent": s.agent}
        for s in sorted(p._sessions.values(), key=lambda s: s.started_at, reverse=True)]
    page._notch = lambda *a: None
    page._agent_ok = lambda a: True
    page._speedups = True                            # (off by default offscreen — here the Claude Code is fake)
    return page


class FakeRouter:
    calls = []

    def classify(self, text, focused, titles, **kw):
        FakeRouter.calls.append(text)
        time.sleep(0.2)
        return {"intent": "NEW", "index": None, "task": text, "screen": False}


def test_routes_while_you_finish(monkeypatch):
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: FakeRouter()))
    page = _page(monkeypatch)
    decided = []
    page._dispatch_decided = lambda r: decided.append(r)       # (Windows: what runs once a decision is made)
    FakeRouter.calls.clear()
    page.prepare_live_route(7, "Make a landing page for my coffee shop.")
    page.run_voice_task("make a landing page for my coffee shop", source="live", tag=7)
    assert pump(3, lambda: decided), "decided"
    assert FakeRouter.calls == ["Make a landing page for my coffee shop."], \
        f"the early decision is used — no second router call ({FakeRouter.calls})"
    assert decided[0]["intent"] == "NEW" and decided[0]["tag"] == 7
    # the final words differ → it decides again (the early one is never used for different words)
    decided.clear()
    FakeRouter.calls.clear()
    page.prepare_live_route(8, "Make a landing page.")
    page.run_voice_task("Make a landing page with a menu and our opening hours", source="live", tag=8)
    assert pump(3, lambda: decided)
    assert FakeRouter.calls[-1] == "Make a landing page with a menu and our opening hours", FakeRouter.calls
    assert not page._early, "used / dropped"


def test_early_decision_never_stale(monkeypatch):
    """The early decision is thrown away when what it was based on changed before the final words arrived: the chat
    you're in, or the question that chat just asked you."""
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: FakeRouter()))
    page = _page(monkeypatch)
    decided = []
    page._dispatch_decided = lambda r: decided.append(r)
    a = ap.AgentSession("sA", "C:/tmp", "x", "Chat A", True, "opus", "high")
    b = ap.AgentSession("sB", "C:/tmp", "y", "Chat B", True, "opus", "high")
    page._sessions = {"sA": a, "sB": b}
    page._focused_key = "sA"
    FakeRouter.calls.clear()
    page.prepare_live_route(11, "Make it darker.")
    page._focused_key = "sB"                       # you clicked another chat before the words were final
    page.run_voice_task("make it darker", source="live", tag=11)
    assert pump(3, lambda: decided)
    assert len(FakeRouter.calls) == 2, f"decided again for the chat you're in now ({FakeRouter.calls})"
    decided.clear()
    FakeRouter.calls.clear()
    ctx = {"v": {"kind": "question", "key": "sB", "title": "Chat B", "question": "Which folder?"}}
    page.live_answer_context = lambda: ctx["v"]
    page.prepare_live_route(12, "The second one.")
    ctx["v"] = {"kind": "question", "key": "sB", "title": "Chat B", "question": "Should I delete them?"}
    page.run_voice_task("The second one.", source="live", tag=12)
    assert pump(3, lambda: decided)
    assert len(FakeRouter.calls) == 2, "the chat asked something else meanwhile → decided again"


def test_last_message_id(monkeypatch, tmp_path):
    """A waiting Claude Code is used only if the chat's last REAL message is the same as when it started."""
    root = tmp_path / "projects"
    monkeypatch.setattr(asess, "_projects_root", lambda: root)
    proj = root / "-tmp-x"
    proj.mkdir(parents=True)
    f = proj / "sid1.jsonl"

    def add(**d):
        with open(f, "a") as fh:
            fh.write(json.dumps(d) + "\n")
    add(type="user", uuid="u1", message={"role": "user", "content": "hi"})
    add(type="assistant", uuid="a1", message={"role": "assistant", "content": "hello"})
    add(type="cost-state")
    add(type="last-prompt")
    assert asess.last_message_id("sid1", "/tmp/x") == "a1", "bookkeeping lines at the end don't count"
    add(type="atis-latch")
    assert asess.last_message_id("sid1", "/tmp/x") == "a1"
    add(type="user", uuid="u2", message={"role": "user", "content": "my favorite color is teal"})
    assert asess.last_message_id("sid1", "/tmp/x") == "u2", "a message said elsewhere is seen"
    assert asess.last_message_id("nope", "/tmp/x") is None
    assert asess.encode_cwd(r"C:\tmp\x") == "C--tmp-x", "(Windows: the folder name Claude Code uses for a cwd)"


def test_went_on_talking_is_joined(monkeypatch):
    """A sentence sent early (it LOOKED finished) is never started before the normal "send after" would have passed;
    if you go on talking within that time it's pulled back and joined with the rest — his "March, April… and May",
    which the real test split into two requests before this. Every order the parts can arrive in."""

    class L(QObject):
        esc_late = Signal()
        state_changed = Signal(str, str)
        level = Signal(float)
        countdown = Signal(float)
        utterance_started = Signal(int)
        utterance_ended = Signal(int)
        heard = Signal(int, str)
        dropped = Signal(int, str, str)
        usage = Signal(float)
        early_text = Signal(int, str)
        quick_sent = Signal(int, float)
        resumed = Signal(int, int)
        on = True

        def start(self):
            pass

        def stop(self):
            pass

        def apply_settings(self):
            pass

        def learn(self, uid):
            pass

        def used_today(self):
            return 0.0

    class MW(QWidget):
        def _on_nav_clicked(self, i):
            pass
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: FakeRouter()))
    mw = MW()
    page = _page(monkeypatch)
    page._build_voice_candidates = lambda text: []
    started = []
    # (Windows: _on_voice_decided already drops a pulled-back request — and holds one that went early — before it
    #  dispatches; the Mac's stub repeated those first lines itself)
    page._dispatch_decided = lambda r: started.append((time.monotonic(), r["heard"]))
    mw.assistant_page = page
    lis = L()
    live = lp.LivePage(mw, listener=lis)

    def go(*steps):
        n = len(started)
        for st in steps:
            getattr(lis, st[0]).emit(*st[1:])
            pump(0.05)
        pump(6, lambda: len(started) > n)               # it starts…
        pump(1.3)                                       # …and nothing else does (a held one would by now)
        return [t for _, t in started[n:]]
    # 1. you go on talking before the first part is even transcribed
    got = go(("utterance_ended", 1), ("quick_sent", 1, 1.0), ("resumed", 1, 2),
             ("heard", 1, "Find my invoices from March, April."), ("utterance_ended", 2), ("heard", 2, "And May."))
    assert got == ["Find my invoices from March, April. And May."], got
    assert 1 not in live._rows and live._rows[2].text.text().endswith("And May.”")
    # 2. it was already sent and being decided — still not started (held), so it's pulled back quietly
    got = go(("utterance_ended", 3), ("quick_sent", 3, 1.0), ("heard", 3, "Open the invoices folder."),
             ("resumed", 3, 4), ("utterance_ended", 4), ("heard", 4, "And the receipts one."))
    assert got == ["Open the invoices folder. And the receipts one."], got
    assert "Cancelled" not in " ".join(r.outcome.text() for r in live._rows.values()), "no 'Cancelled' — joined"
    # 3. the second part is transcribed before the first
    got = go(("utterance_ended", 5), ("quick_sent", 5, 1.0), ("resumed", 5, 6), ("utterance_ended", 6),
             ("heard", 6, "the second part."), ("heard", 5, "First part,"))
    assert got == ["First part, the second part."], got
    # 4. what came next was only a cough → the first part goes alone
    got = go(("utterance_ended", 7), ("quick_sent", 7, 1.0), ("heard", 7, "What time is it in Tokyo?"),
             ("resumed", 7, 8), ("dropped", 8, "", "too short"))
    assert got == ["What time is it in Tokyo?"], got
    # 5. nobody goes on talking → it starts — but not before the normal wait would have passed
    t0 = time.monotonic()
    got = go(("utterance_ended", 9), ("quick_sent", 9, 0.6), ("heard", 9, "Hello there."))
    assert got == ["Hello there."], got
    assert started[-1][0] - t0 >= 0.55, "held until the normal 'send after' would have passed"
    live.stop()


class FakeClaude:
    """Stands in for AgentRunner: records whether it was started ahead (wait_for) and what it got."""
    made = []

    def __init__(self, cwd, on_step=None, on_approval=None, on_result=None):
        self.cwd, self.on_result, self.got, self.parked, self.closed = cwd, on_result, None, False, False
        FakeClaude.made.append(self)

    async def run(self, prompt, resume=None, bypass=False, model=None, effort=None, images=None, wait_for=None):
        self.resume = resume
        if wait_for is not None:
            self.parked = True
            got = await wait_for()
            if got is None:
                self.closed = True
                return None
            prompt, images = got
        self.got = prompt
        await asyncio.sleep(0.05)
        self.on_result("sid-1" if resume is None else resume, 0.0)
        return "sid-1"

    async def interrupt(self):
        pass


def test_claude_code_is_already_running(monkeypatch):
    """While listening: a Claude Code is ready for a NEW chat (always — replaced the moment it's used) AND one for a
    follow-up of the chat you just talked to. Each request goes to the right one; settings changed → not used, and
    the ready one is replaced at once (Full access / model); 10 quiet minutes → the new-chat one is replaced by a
    fresh one, the follow-up one closes; stop / quit → all close, their threads end."""
    monkeypatch.setattr(ar, "AgentRunner", FakeClaude)
    FakeClaude.made.clear()
    parked = lambda: [f for f in FakeClaude.made if f.parked and not f.closed and f.got is None]  # noqa: E731
    page = _page(monkeypatch)
    page.set_hands_free(True)                     # listening → one Claude Code starts now, for a new chat
    assert pump(2, lambda: len(parked()) == 1), "a new-chat one started ahead"
    new1 = page._spares["new"]
    assert new1["sig"][1] is None and "follow" not in page._spares
    page._live_turn_next = True
    page.run_task("Summarize my Downloads")
    sess = next(iter(page._sessions.values()))
    assert sess.worker is new1["worker"], "the request went to the running one"
    assert pump(2, lambda: "new" in page._spares and page._spares["new"]["key"] != new1["key"]), \
        "…and the NEXT new chat's one starts right away"
    assert pump(3, lambda: sess.status == "done"), sess.status
    assert FakeClaude.made[0].got.startswith("Summarize my Downloads")
    # its follow-up is ready next (same chat, same settings) — and a new chat still is too
    assert pump(3, lambda: "follow" in page._spares and page._spares["follow"]["sig"][1] == "sid-1")
    assert pump(2, lambda: len(parked()) == 2), "two waiting: a new chat + this chat's follow-up"
    # a NEW request after that turn → the new-chat one (not cold); the follow-up one stays for that chat
    new2, follow = page._spares["new"], page._spares["follow"]
    page._live_turn_next = True
    page.run_task("A different new task")
    s2 = max(page._sessions.values(), key=lambda s: s.started_at)
    assert s2.worker is new2["worker"], ("a new chat after a turn → the ready new-chat one",
                                         s2.prompt, s2.session_id, [k for k in page._spares])
    assert page._spares.get("follow") is follow, "the other chat's follow-up one wasn't thrown away"
    assert pump(3, lambda: s2.status == "done")
    # a follow-up of the first chat → its follow-up one
    page._live_turn_next = True
    page.run_task("And the PDFs too", resume="sid-1")
    s3 = page._session_by_claude_id("sid-1")
    assert s3 is not None and s3.worker is follow["worker"], "the follow-up went to that chat's one"
    assert pump(3, lambda: s3.status == "done")
    # 10 quiet minutes → the new-chat one is replaced by a FRESH one; a follow-up one closes
    assert pump(3, lambda: "new" in page._spares and "follow" in page._spares)
    stale_new, stale_follow = page._spares["new"], page._spares["follow"]
    for sp in page._spares.values():
        sp["at"] -= page.SPARE_IDLE_SEC + 5
    page._check_spare()
    assert "follow" not in page._spares, "the old follow-up one closed"
    assert "new" in page._spares and page._spares["new"]["key"] != stale_new["key"], "a fresh new-chat one"
    assert pump(3, lambda: stale_new["key"] not in page._spare_threads and stale_follow["key"] not in page._spare_threads)
    # stop listening → all close, their threads end
    page.set_hands_free(False)
    assert not page._spares
    assert pump(3, lambda: not page._spare_threads), page._spare_threads
    # not listening → nothing runs ahead
    page._prewarm_new()
    assert not page._spares
    # settings changed (another model) → the waiting one is NOT used — it closes, one with the new settings starts
    page.set_hands_free(True)
    assert pump(2, lambda: "new" in page._spares)
    old = page._spares["new"]
    page._set_models("claude", "sonnet", "")
    page._live_turn_next = True
    page.run_task("Another new chat")
    newest = max(page._sessions.values(), key=lambda s: s.started_at)
    assert newest.worker is not old["worker"] and newest.model == "sonnet", "another model → not the waiting one"
    assert pump(3, lambda: old["key"] not in page._spare_threads), "the mismatched one closed (its thread ended)"
    assert pump(2, lambda: "new" in page._spares and page._spares["new"]["sig"][3] == "sonnet"), \
        "a new-chat one with the new model starts"
    assert pump(3, lambda: newest.status == "done")
    # Full access switched off while one waits → replaced right away; the next request gets the NEW one (warm).
    # (Windows: the access pill calls _on_access_changed; bypass_check is its hidden mirror, with no signals — the
    #  Mac's checkbox drives it directly.)
    assert pump(2, lambda: "new" in page._spares)
    old = page._spares["new"]
    page._on_access_changed(False)
    assert pump(2, lambda: old["key"] not in page._spare_threads and "new" in page._spares
                and page._spares["new"]["sig"][2] is False), "replaced with a Full-access-off one"
    ready = page._spares["new"]
    page._live_turn_next = True
    page.run_task("A chat without Full access")
    newest = max(page._sessions.values(), key=lambda s: s.started_at)
    assert newest.worker is ready["worker"] and not newest.bypass, "…and it's used: no waiting"
    assert pump(3, lambda: newest.status == "done")
    page._on_access_changed(True)
    page.set_hands_free(False)
    page._set_models("claude", "opus", "high")
    # quitting with them waiting: closed + their threads stopped (no "QThread destroyed while running")
    page.set_hands_free(True)
    assert pump(2, lambda: "new" in page._spares)
    th = page._spares["new"]["thread"]
    page.shutdown()
    assert not th.isRunning() and not page._spares, "quit stops the waiting ones"


def test_tool_search_and_voice_wake(monkeypatch):
    """Claude Code gets tool search (unless you set ENABLE_TOOL_SEARCH yourself); the voice service is woken when you
    start talking — at most every WAKE_EVERY_SEC, an empty OPTIONS call to speak-stream."""
    src = inspect.getsource(_REAL_AGENT_RUNNER.run)
    assert 'env={"ENABLE_TOOL_SEARCH": os.environ.get("ENABLE_TOOL_SEARCH") or "true"}' in src
    calls = []

    class Http:
        def options(self, url, timeout=None):
            calls.append(url)
    assert pump(15, lambda: not any(t.name == "filect-speak-warm" for t in threading.enumerate())), \
        "(earlier tests' wake-ups still running)"
    monkeypatch.setattr(so, "_HTTP", Http())
    monkeypatch.setattr(so, "_woke", 0.0)
    so.wake()
    assert pump(5, lambda: len(calls) == 1), calls
    assert calls[0].endswith("/functions/v1/speak-stream")
    so.wake()
    pump(0.3)
    assert len(calls) == 1, "not again within WAKE_EVERY_SEC"
    so._woke -= so.WAKE_EVERY_SEC
    so.wake()
    assert pump(5, lambda: len(calls) == 2), calls


def _one_agent_page(monkeypatch):
    monkeypatch.setattr(crun, "CodexRunner", FakeClaude)   # (never a REAL Codex from a unit test)
    monkeypatch.setattr(ar, "AgentRunner", FakeClaude)
    FakeClaude.made.clear()
    calls = {"router": 0, "codex": 0, "check": 0}
    nd = lambda t: {"intent": "NEW", "index": None, "task": t, "screen": False}  # noqa: E731

    class R:
        def classify(self, text, *a, **k):
            calls["router"] += 1
            return nd(text)
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: R()))

    def codex(text, *a, **k):
        calls["codex"] += 1
        return nd(text)
    monkeypatch.setattr(ir, "classify_with_codex", codex)
    state = {}

    def check():
        calls["check"] += 1
        return dict(state)
    page = _page(monkeypatch)
    del page._agent_ok                            # (the real one — _page() stubs it)
    page._agents_check = check
    decided = []
    page._dispatch_decided = lambda r: decided.append(r)

    def set_state(**st):
        state.clear()
        state.update(st)
        page.refresh_agents_setup()
        assert pump(2, lambda: not page._agents_checking and page._agents_state
                    and all(page._agents_state.get(k) == v for k, v in st.items()))
    return page, calls, decided, set_state


def test_one_agent_only(monkeypatch):
    """Readiness is checked AHEAD (background) and only READ when you ask: someone with only Codex goes straight to
    Codex (agent + decider), no Claude Code kept ready for nothing; only Claude → Claude; both → your primary. A
    sign-in error → the saved answer is re-checked."""
    page, calls, decided, set_state = _one_agent_page(monkeypatch)
    # only Codex
    set_state(claude="install", codex="ready")
    assert page._pick_agent() == "codex" and not page._agent_ok("claude")
    page.set_hands_free(True)
    assert pump(2, lambda: not page._agents_checking)            # (it checks once more, in the background)
    pump(0.3)
    assert "new" not in page._spares and not FakeClaude.made, "no Claude Code kept ready for nothing"
    n_check = calls["check"]
    page.run_voice_task("What time is it in Tokyo?", source="live", tag=1)
    assert pump(3, lambda: decided), "decided"
    assert calls["codex"] == 1 and calls["router"] == 0, ("straight to the Codex decider", calls)
    assert calls["check"] == n_check, "nothing is checked when you ask — the saved answer is read"
    # Claude Code set up meanwhile → from now on it's used (readied while listening: see the xfail below)
    set_state(claude="ready", codex="ready")
    assert page._pick_agent() == "claude", "both ready → your primary (Claude)"
    decided.clear()
    page.run_voice_task("And in London?", source="live", tag=2)
    assert pump(3, lambda: decided) and calls["router"] == 1, "the Claude decider again"
    page.set_hands_free(False)
    # only Claude
    set_state(claude="ready", codex="signin")
    assert page._pick_agent() == "claude" and not page._agent_ok("codex")
    # neither → your primary (its own setup card on the reply, as before) + the setup card
    set_state(claude="install", codex="install")
    assert page._pick_agent() == page._primary
    # a sign-in error on a reply → the saved answer is checked again (Codex not set up: no hand-over here)
    set_state(claude="ready", codex="install")
    n_check = calls["check"]
    page._live_turn_next = True
    page.run_task("Hello")
    sess = max(page._sessions.values(), key=lambda s: s.started_at)
    from app.core.agent_runner import SETUP_SIGNIN
    page._on_error(sess.key, SETUP_SIGNIN)
    assert pump(2, lambda: calls["check"] > n_check), "signed out since? checked again"
    page.shutdown()


def test_claude_set_up_while_listening_is_readied(monkeypatch):
    page, calls, decided, set_state = _one_agent_page(monkeypatch)
    set_state(claude="install", codex="ready")
    page.set_hands_free(True)
    assert pump(2, lambda: not page._agents_checking)
    assert "new" not in page._spares and not FakeClaude.made
    set_state(claude="ready", codex="ready")
    assert pump(2, lambda: "new" in page._spares), "Claude Code readied as soon as it's set up"


# ---- warm on intent: the Agents tab, the card, Shift+Z (Mac e677e8e) ------------------------------------------
def test_warm_on_intent(monkeypatch):
    """The agents warm up when you're ABOUT to ask (not only while listening hands-free): typing / clicking into the
    Agents tab, typing in the card, Shift+Z held. Each warms exactly what its request needs; the same matching rules
    decide; unused ones close after 2 min; Shift+Z decides while the final transcript arrives."""
    from PySide6.QtCore import QEvent, Qt as _Qt
    from PySide6.QtGui import QFocusEvent
    monkeypatch.setattr(ar, "AgentRunner", FakeClaude)
    FakeClaude.made.clear()
    calls = {"router_get": 0, "classify": []}

    class R:
        def classify(self, text, focused, titles, **kw):
            calls["classify"].append((text, dict(kw)))
            time.sleep(0.1)
            return {"intent": "NEW", "index": None, "task": text, "screen": False}

    def get(cls):
        calls["router_get"] += 1
        return R()
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(get))
    parked = lambda: [f for f in FakeClaude.made if f.parked and not f.closed and f.got is None]  # noqa: E731
    page = _page(monkeypatch)
    assert not page._hands_free
    page._prewarm_new()                                   # nothing runs ahead on its own (not listening)
    assert not page._spares and not FakeClaude.made
    # 1. Agents tab, new chat: typing warms a NEW-chat Claude Code; sending adopts it
    page.task_input.setPlainText("Summarize my Downloads")
    assert pump(2, lambda: "new" in page._spares and len(parked()) == 1), "typing → a new-chat one starts"
    assert "follow" not in page._spares and calls["router_get"] == 0, "the Agents tab isn't routed: no decider"
    spare = page._spares["new"]
    page._on_send("Summarize my Downloads")
    s1 = max(page._sessions.values(), key=lambda s: s.started_at)
    assert s1.worker is spare["worker"], "sent → it went to the one already running"
    assert pump(3, lambda: s1.status == "done" and not s1.running), s1.status
    assert not page._spares, "not listening: nothing refilled / kept by itself after the turn"
    # 2. a reply in that chat: typing warms THAT chat's follow-up; sending adopts it
    page._focus(s1.key)
    page._intent_at.clear()
    page.task_input.setPlainText("And the PDFs too")
    assert pump(2, lambda: (page._spares.get("follow") or {}).get("sig", (0, 0))[1] == "sid-1"), page._spares
    fsp = page._spares["follow"]
    page._on_send("And the PDFs too")
    assert s1.worker is fsp["worker"], "the reply went to its chat's one"
    assert pump(3, lambda: s1.status == "done" and not s1.running)
    # 3. a chat that's still working: nothing to warm (the reply queues on its own Claude Code); Codex: nothing
    s1.thread = object()
    page._intent_at.clear()
    page._intent_throttled("page")
    pump(0.3)
    assert "follow" not in page._spares, "a running chat queues the reply — nothing started"
    s1.thread = None
    s1.agent = "codex"
    page._intent_at.clear()
    page._intent_throttled("page")
    pump(0.3)
    assert "follow" not in page._spares, "a Codex chat: nothing to warm"
    s1.agent = "claude"
    # 4. typing throttle: once per 2 s
    n = len(FakeClaude.made)
    page._intent_at.clear()
    for _ in range(5):
        page._intent_throttled("page")
    pump(0.3)
    assert len(FakeClaude.made) <= n + 1
    page._drop_spare("test")
    pump(0.5)
    # 5. clicking INTO the box counts; code moving focus there doesn't
    page._intent_at.clear()
    page.eventFilter(page.task_input, QFocusEvent(QEvent.FocusIn, _Qt.OtherFocusReason))
    pump(0.3)
    assert not page._spares, "code moved focus there → nothing"
    page.eventFilter(page.task_input, QFocusEvent(QEvent.FocusIn, _Qt.MouseFocusReason))
    assert pump(2, lambda: "follow" in page._spares), "you clicked into it → its chat's follow-up warms"
    page._drop_spare("test")
    pump(0.5)
    # 6. Shift+Z / the card (routed): a new chat AND the chat you're in, + the decider
    page._intent_at.clear()
    page.agent_intent("voice")
    assert pump(2, lambda: "new" in page._spares and "follow" in page._spares), page._spares.keys()
    assert pump(2, lambda: calls["router_get"] >= 1), "the decider starts while you talk"
    page._drop_spare("test")
    pump(0.5)
    n = calls["router_get"]
    page._ensure_notch().typing_started.emit()
    assert pump(2, lambda: "new" in page._spares and calls["router_get"] > n), "typing in the card → same"
    # 7. Full access switched off after it warmed → swapped at once for one with the new setting
    old = page._spares["new"]
    page._on_access_changed(False)
    assert pump(2, lambda: "new" in page._spares and page._spares["new"]["key"] != old["key"]
                and page._spares["new"]["sig"][2] is False), "swapped for the new setting"
    # 8. unused → closed after INTENT_IDLE_SEC (not listening), and the clock stops
    for sp in page._spares.values():
        sp["at"] -= page.INTENT_IDLE_SEC + 5
    page._check_spare()
    assert not page._spares and not page._spare_timer.isActive(), "unused ones closed"
    assert pump(3, lambda: not page._spare_threads), page._spare_threads
    # 9. Shift+Z decides while the final transcript arrives (not hands-free: no 'ignore' option)
    decided = []
    page._dispatch_decided = lambda r: decided.append(r)
    calls["classify"].clear()
    page.prepare_live_route(("shiftz", 1), "What's on my calendar tomorrow?", live=False)
    page.run_voice_task("What's on my calendar tomorrow", source="voice", tag=("shiftz", 1))
    assert pump(3, lambda: decided)
    assert len(calls["classify"]) == 1 and calls["classify"][0][1] == {}, \
        f"the early decision is used, asked exactly like the final one ({calls['classify']})"
    decided.clear()
    calls["classify"].clear()
    page.prepare_live_route(("shiftz", 2), "Open my", live=False)
    page.run_voice_task("Open my downloads folder", source="voice", tag=("shiftz", 2))
    assert pump(3, lambda: decided) and calls["classify"][-1][0] == "Open my downloads folder", \
        "different final words → decided again"


class _FakeHotkey(QObject):
    pressed = Signal()
    released = Signal()
    space = Signal(bool)
    escape = Signal()
    holding_escape = False

    def start(self):
        return True

    def stop(self):
        pass

    def hold_escape(self, on=True):
        self.holding_escape = on

    def release_escape(self):
        self.holding_escape = False


class _ShiftZRecorder(QObject):
    """The streaming transcriber Shift+Z uses: words so far while you hold it, the final text after release."""
    finished = Signal(str)
    error = Signal(str)
    level = Signal(float)
    capped = Signal()
    so_far, final = "", ""

    def __init__(self, *a, **k):
        super().__init__()

    def start(self):
        pass

    def text_so_far(self):
        return _ShiftZRecorder.so_far

    def stop_recording(self):
        QTimer.singleShot(150, lambda: self.finished.emit(_ShiftZRecorder.final))

    def cancel(self):
        pass


def test_shift_z_warms_on_press_and_decides_on_release(monkeypatch):
    """The real Shift+Z controller: press → Claude Code + the decider start while you talk (once per hold); release →
    deciding starts on the words already transcribed; the final transcript uses that decision when the words match."""
    from app.ui import agent_voice as av
    monkeypatch.setattr(ar, "AgentRunner", FakeClaude)
    FakeClaude.made.clear()
    calls = []

    class R:
        def classify(self, text, focused, titles, **kw):
            calls.append(text)
            time.sleep(0.2)
            return {"intent": "NEW", "index": None, "task": text, "screen": False}
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: R()))
    monkeypatch.setattr(tx, "StreamingTranscriber", _ShiftZRecorder)
    page = _page(monkeypatch)
    decided = []
    page._dispatch_decided = lambda r: decided.append(r)
    told = []
    real_intent = page.agent_intent
    page.agent_intent = lambda w: (told.append(w), real_intent(w))
    mw = QObject()
    mw.assistant_page = page
    hk = _FakeHotkey()
    ctl = av.AgentVoiceController(mw, hotkey=hk)
    monkeypatch.setattr(ctl, "_get_pill", lambda: None)
    cases = (("Make me a landing page for my bakery.", "Make me a landing page for my bakery.", True),
             ("Make me a landing", "Make me a landing page for my bakery with a menu.", False))
    for words, final, one_call in cases:
        decided.clear()
        calls.clear()
        _ShiftZRecorder.so_far, _ShiftZRecorder.final = words, final
        hk.pressed.emit()
        assert told and told[-1] == "voice", "press → warm up"
        assert pump(2, lambda: "new" in page._spares), "a Claude Code started while you talk"
        ctl._pressed_at -= 2.0                         # (held 2 s)
        hk.released.emit()
        assert pump(3, lambda: decided), "decided"
        assert decided[0]["heard"] == final
        if one_call:
            assert calls == [words], f"decided on release, once — reused for the final text ({calls})"
        else:
            assert calls[-1] == final and len(calls) == 2, f"the final words differ → decided again ({calls})"
        pump(0.3)
    assert told.count("voice") == 2, "once per hold"


def test_a_warm_claude_code_closed_the_moment_it_starts_never_crashes(monkeypatch):
    """Mac bug 49: closing a warm Claude Code while its thread was still starting freed its worker, and the thread's
    `started` was delivered to a freed object — a hard crash (8 of 24 Mac runs). Windows holds the worker on the
    thread (th._spare_worker) until it ends. Start one and close it at once, at random tiny delays, many times.
    SPARE_OLD=1 removes that hold (the code before the fix) to show this test can catch the crash; run this file
    many times at once with PYTHONFAULTHANDLER=1 — one passing run proves little."""
    import gc
    import random
    monkeypatch.setattr(ar, "AgentRunner", FakeClaude)
    page = _page(monkeypatch)
    rnd = random.Random(int(os.environ.get("SPARE_SEED", "1")))
    old = bool(os.environ.get("SPARE_OLD"))
    for _ in range(int(os.environ.get("SPARE_ROUNDS", "60"))):
        page.set_hands_free(True)                    # one starts…
        if old:
            for sp in page._spares.values():
                try:
                    del sp["thread"]._spare_worker
                except AttributeError:
                    pass
        delay = rnd.choice([0, 0, 0, 0.0005, 0.002, 0.01])
        if delay:
            time.sleep(delay)
        if rnd.random() < 0.5:
            app.processEvents()
        page.set_hands_free(False)                   # …and closes at once
        if rnd.random() < 0.3:
            gc.collect()
        if rnd.random() < 0.3:
            app.processEvents()
    assert pump(6, lambda: not page._spare_threads), f"every spare's thread ended: {list(page._spare_threads)}"
    page.shutdown()
