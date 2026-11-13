"""Filect Assistant (hands-free) — the router's IGNORE, the hands-free path through the Agents page, and the Assistant
tab (Windows port of the Mac tests/test_live_page.py). No mic, no real agents: a fake listener and a fake router.
  · IGNORE is accepted ONLY for hands-free; the voice chord's routing is unchanged (its fakes still take 3 args)
  · hands-free: ignored speech never opens the card or runs anything; a routing failure runs NOTHING (the voice
    chord still falls back to a new chat); a real request opens the card, runs, and is reported back
  · the tab: Start/Stop, rows for what it heard and what happened, the chime only for real requests, learning your
    voice level, settings saved (stubbed), the light under the top pill, opening a chat from a row
  · Windows: Windows key names everywhere (no ⌃ / ⇧ / "Mac" on screen), the noise-filter row hidden, the orb's
    60-fps clock only while it's on screen, the chime sounds exist
Your settings.json is never written."""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.core import intent_router as ir  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402
from app.ui import live_page as lp  # noqa: E402

saves, tracked, chimes = [], [], []
_PAGES = []
_REAL_CHIME = lp._chime            # (the fixture replaces it with a recorder)


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch, tmp_path):
    """Never the user's settings / app-data files, no sounds, no analytics; every live_* setting starts known."""
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: saves.append(1))
    monkeypatch.setattr(settings, "assistant_bypass", False)
    for k, v in (("live_send_after_sec", 2.0), ("live_sensitivity", "medium"), ("live_chime", True),
                 ("live_noise_filter", True), ("live_speak", False), ("live_voice", "sal"),
                 ("live_tour_seen", False), ("live_settings_collapsed", False), ("assistant_auto_open", True)):
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(ap, "_play_chime", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap.AssistantPage, "_show_idle_overlays", lambda self: None)
    monkeypatch.setattr(ap.AssistantPage, "_load_recent", lambda self: None)
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", str(tmp_path / "Filect Assistant"))
    from app.core import agent_sessions as _asn
    monkeypatch.setattr(_asn, "_touched_path", lambda: tmp_path / "assistant_sessions.json")
    from app.core import agent_runner as _ar
    monkeypatch.setattr(_ar, "_models_cache_path", lambda: tmp_path / "claude_models.json")
    monkeypatch.setattr(lp, "_track", lambda ev, **k: tracked.append(ev))
    monkeypatch.setattr(lp, "_chime", lambda name="Tink": chimes.append(name))
    orig = ap.AssistantPage.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        _PAGES.append(self)
    monkeypatch.setattr(ap.AssistantPage, "__init__", track)
    saves.clear(); tracked.clear(); chimes.clear()
    yield
    while _PAGES:
        _PAGES.pop().shutdown()


def _pump(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


class _MW:
    def __init__(self):
        self.nav = []

    def _on_nav_clicked(self, i):
        self.nav.append(i)


class _Router:
    def __init__(self, intent="NEW", fail=False):
        self.intent, self.fail, self.calls, self.asked = intent, fail, [], []

    def classify(self, text, focused, cands, live=False, asked=None):
        self.calls.append(live)
        self.asked.append(asked)
        if self.fail:
            raise RuntimeError("router down")
        return {"intent": self.intent, "index": None, "task": text}


def _with_router(router):
    real = (ir.IntentRouter.get, ir.classify_with_codex)
    ir.IntentRouter.get = classmethod(lambda cls: router)

    def codex(text, f, c, live=False, **_k):
        raise RuntimeError("codex unavailable too")
    ir.classify_with_codex = codex
    return real


def _restore(real):
    ir.IntentRouter.get, ir.classify_with_codex = real


# ---- the router -----------------------------------------------------------------------------------
def test_ignore_only_for_hands_free():
    assert ir._parse('{"intent":"IGNORE","index":null,"task":"x"}', "x", allow_ignore=True)["intent"] == "IGNORE"
    assert ir._parse('{"intent":"IGNORE","index":null,"task":"x"}', "x")["intent"] == "NEW", "voice chord: never IGNORE"
    assert "HANDS-FREE" in ir._build_prompt("hi", None, [], live=True)
    assert "HANDS-FREE" not in ir._build_prompt("hi", None, [])


# ---- the hands-free path through the Agents page ---------------------------------------------------
def _page():
    page = ap.AssistantPage(_MW())
    page._primary = "claude"
    notch, runs, reports = [], [], []
    page._notch = lambda m, *a: notch.append(m)

    def fake_run(prompt, bypass=None, resume=None, cwd=None, images=None, agent=None):
        runs.append(prompt)
        if resume:                                   # (the real one reuses that chat's session)
            return
        s = ap.AgentSession(f"k{len(runs)}", "C:/tmp", prompt, "Coffee Shop Site", True, "opus", "high")
        s.session_id = f"sid{len(runs)}"
        page._sessions[s.key] = s
        page._focus(s.key, fresh=True)               # (Windows: the real run_task opens the card HERE — "present")
    page.run_task = fake_run
    page.live_routed.connect(lambda tag, o, t, k: reports.append((tag, o, t, k)))
    return page, notch, runs, reports


def test_hands_free_routing():
    page, notch, runs, reports = _page()
    router = _Router("IGNORE")
    real = _with_router(router)
    try:
        page.run_voice_task("yeah I'll call you back in a minute", source="live", tag=7)
        assert _pump(lambda: reports, 3)
        assert reports == [(7, "ignored", "", "")] and runs == [] and "present" not in notch, (reports, notch)
        assert router.calls == [True], "hands-free asks with live=True"

        router.intent = "NEW"
        page.run_voice_task("make a landing page for my coffee shop", source="live", tag=8)
        assert _pump(lambda: len(reports) == 2, 3)
        assert reports[-1] == (8, "new", "Coffee Shop Site", "k1"), reports
        assert runs == ["make a landing page for my coffee shop"] and "present" in notch

        router.intent = "FOLLOW_UP"
        page.run_voice_task("make the header darker", source="live", tag=9)
        assert _pump(lambda: len(reports) == 3, 3)
        assert reports[-1][:2] == (9, "continued") and reports[-1][3] == "k1"

        router.fail = True                                    # Claude AND Codex can't route
        page.run_voice_task("delete my old screenshots", source="live", tag=10)
        assert _pump(lambda: len(reports) == 4, 3)
        assert reports[-1] == (10, "failed", "", "") and len(runs) == 2, "hands-free never guesses"
        page._focused_key = None                               # (so the voice chord's fallback is a NEW chat)

        n_present = notch.count("present")                     # the voice chord is unchanged:
        page.run_voice_task("open my notes")                   #   the card opens at once,
        assert notch.count("present") == n_present + 1
        assert _pump(lambda: len(runs) == 3, 3), "a router failure still falls back to running it"
        assert router.calls[-1] is False and len(reports) == 4, "no live flag, no live report"
    finally:
        _restore(real)


def test_esc_while_the_router_checks():
    """Esc after the transcript, while the router is still deciding: the request is dropped — it never runs — and
    reported as cancelled."""
    page, notch, runs, reports = _page()

    class Slow(_Router):
        def classify(self, text, focused, cands, live=False):
            time.sleep(0.6)
            return {"intent": "NEW", "index": None, "task": text}
    real = _with_router(Slow())
    try:
        page.run_voice_task("delete all my screenshots", source="live", tag=21)
        assert page.cancel_live(21) is True
        assert reports == [(21, "cancelled", "", "")]
        _pump(lambda: False, 1.2)                          # the router answers meanwhile
        assert runs == [] and len(reports) == 1, (runs, reports)
        assert page.cancel_live(21) is False, "nothing left to cancel"
        page.run_voice_task("open my notes", source="live", tag=22)   # the next one runs normally
        assert _pump(lambda: runs, 3) and reports[-1][:2] == (22, "new")
    finally:
        _restore(real)


def test_note_under_the_top_pill():
    """Windows: the note is drawn under the resting pill (the pill itself is always drawn → compare to it)."""
    from app.ui.claude_overlay import ClaudeOverlay
    ov = ClaudeOverlay()
    try:
        h = ov._handle
        h.resize(180, 40)
        blank = h.grab().toImage()
        ov.flash_note("Cancelled", ms=150)
        assert h._note == "Cancelled"
        img = h.grab().toImage()
        changed = sum(1 for x in range(0, img.width(), 2) for y in range(0, img.height(), 2)
                      if img.pixelColor(x, y) != blank.pixelColor(x, y))
        assert changed > 20, changed
        assert _pump(lambda: h._note == "", 1), "it goes away by itself"
        ov.set_listening("listening")
        lit = h.grab().toImage()
        assert any(lit.pixelColor(x, y) != blank.pixelColor(x, y)
                   for x in range(0, lit.width(), 2) for y in range(0, lit.height(), 2)), "the light shows"
        ov.flash_note("Not a request")
        h.grab()
        ov.set_listening("")
    finally:
        ov.close()
        ov.deleteLater()


def test_answers_to_the_agent():
    """The agent waits for you: an approval → a spoken "yes"/"no" answers it right away (no router); a question its
    last answer ended with → the router gets that question, so "yes" is a reply, not filler."""
    page, notch, runs, reports = _page()
    decided = []
    page._decide = lambda ok, key=None: decided.append(ok)
    s = ap.AgentSession("w1", "C:/tmp", "build a site", "Coffee Site", True, "opus", "high")
    page._sessions["w1"], page._focused_key = s, "w1"
    assert page.live_answer_context() is None, "working, nothing asked"
    s.status, s.approval_req = "needs_you", object()
    assert page.live_answer_context()["kind"] == "approval"
    router = _Router("NEW")
    real = _with_router(router)
    try:
        page.run_voice_task("Yes, go ahead.", source="live", tag=31)
        assert decided == [True] and reports[-1] == (31, "approved", "Coffee Site", "w1") and router.calls == []
        s.approval_req = object()
        page.run_voice_task("No, don't.", source="live", tag=32)
        assert decided == [True, False] and reports[-1][1] == "denied"
        s.status, s.approval_req = "done", None
        s.result_text = "I built the page and started it at localhost:4173.\n\nShould I also add a contact form?"
        s.duration = 30
        ctx = page.live_answer_context()
        assert ctx["kind"] == "question" and ctx["question"] == "Should I also add a contact form?", ctx
        for tail, want in (("Should I create it? (yes/no)", "Should I create it?"),
                           ("Want me to deploy it?\n\nLet me know.", "Want me to deploy it?"),
                           ("Should I create a folder called “yes test” inside the “Filect test” folder on your "
                            "Desktop? I’ll wait for your answer before creating it.",
                            "Should I create a folder called “yes test” inside the “Filect test” folder on your "
                            "Desktop?"),
                           ("Done. Created the folder.", ""),
                           ("Done — what a day? I created every folder. Then I tested every page. All good.", "")):
            assert ap._last_question(tail) == want, (tail, ap._last_question(tail))
        page.run_voice_task("Yes.", source="live", tag=33)
        assert _pump(lambda: len(reports) == 3, 3)
        assert router.asked[-1] == "Should I also add a contact form?", router.asked
        s.started_at = time.time() - page.ANSWER_WINDOW_SEC - 60          # long ago → no longer waiting
        assert page.live_answer_context() is None
        page.run_voice_task("Open my notes.", source="live", tag=34)
        assert _pump(lambda: len(reports) == 4, 3) and router.asked[-1] is None
        page.run_voice_task("open my notes")                            # the voice chord: never gets `asked`
        assert _pump(lambda: len(router.asked) == 3, 3) and router.asked[-1] is None and router.calls[-1] is False
    finally:
        _restore(real)


def test_voice_chord_fakes_with_three_args_still_work():
    page, notch, runs, reports = _page()

    class Old:
        def classify(self, text, focused, cands):             # a voice-chord fake/caller with no `live`
            return {"intent": "NEW", "index": None, "task": text}
    real = _with_router(Old())
    try:
        page.run_voice_task("what is 2 plus 2")
        assert _pump(lambda: runs, 3), "the voice chord never passes the new keyword"
    finally:
        _restore(real)


# ---- the Assistant tab ------------------------------------------------------------------------------
class FakeListener(QObject):
    esc_late = Signal()
    state_changed = Signal(str, str)
    level = Signal(float)
    countdown = Signal(float)
    utterance_started = Signal(int)
    utterance_ended = Signal(int)
    heard = Signal(int, str)
    dropped = Signal(int, str, str)
    usage = Signal(float)

    def __init__(self):
        super().__init__()
        self.on, self.learned, self.applied, self.probe = False, [], 0, None

    def set_answer_probe(self, fn):
        self.probe = fn

    def start(self):
        self.on = True
        self.state_changed.emit("starting", "Starting the microphone…")

    def stop(self):
        self.on = False
        self.state_changed.emit("off", "")

    def learn(self, uid):
        self.learned.append(uid)

    def apply_settings(self):
        self.applied += 1

    def used_today(self):
        return 125.0


class FakeOverlay:
    def __init__(self):
        self.lights, self.notes = [], []

    def set_listening(self, m):
        self.lights.append(m)

    def flash_note(self, text, color="#B8B8C4", ms=2500):
        self.notes.append(text)


class FakeAP(QObject):
    live_routed = Signal(object, str, str, str)
    live_screen_used = Signal(object)

    def __init__(self):
        super().__init__()
        self.runs, self.focused, self.ov = [], [], FakeOverlay()
        self._sessions, self._overlay = {"k1": object()}, None

    def run_voice_task(self, text, source="voice", images=None, tag=None, screen=None):
        self.runs.append((text, source, tag))
        self.last = {"images": list(images or []), "screen": screen}

    def cancel_live(self, tag):
        self.live_routed.emit(tag, "cancelled", "", "")
        return True

    def _ensure_notch(self):
        self._overlay = self.ov
        return self.ov

    def live_answer_context(self):
        return None

    def _focus(self, key):
        self.focused.append(key)


class _Btn:
    def __init__(self):
        self.checked = False

    def setChecked(self, v):
        self.checked = v


class FakeGroup:
    def __init__(self):
        self.b = _Btn()

    def button(self, i):
        return self.b if i == 5 else None


def _tab(capture=None):
    mw = _MW()
    mw.assistant_page, mw.nav_button_group = FakeAP(), FakeGroup()
    L = FakeListener()
    page = lp.LivePage(mw, listener=L, capture=capture)
    return page, L, mw


def test_tab_start_stop_and_rows():
    page, L, mw = _tab()
    assert page.start_btn.text().strip() == "Start listening" and "2 min of 3 h" in page.usage_label.text(), \
        page.usage_label.text()
    page.start_btn.click()
    assert L.on and page.start_btn.text().strip() == "Stop listening" and tracked[-1] == "live_started"
    assert L.probe is not None, "the tab tells the listener when the agent waits for your answer"
    L.state_changed.emit("listening", "")
    assert page.status.text() == "Listening — just talk" and mw.assistant_page.ov.lights[-1] == "listening"
    L.state_changed.emit("hearing", "")
    assert mw.assistant_page.ov.lights[-1] == "hearing"
    L.state_changed.emit("countdown", "")
    L.countdown.emit(1.4)
    assert page.status.text() == "Sending in 1.4 s"
    L.state_changed.emit("paused", "Paused while Zoom uses the mic")
    assert page.status.text() == "Paused while Zoom uses the mic" and mw.assistant_page.ov.lights[-1] == "paused"

    # a request: ended → transcribing → heard → run → reported
    L.utterance_ended.emit(1)
    assert page._rows[1].outcome.text() == "Transcribing…" and page._empty.isHidden()
    L.heard.emit(1, "make a landing page for my coffee shop")
    assert mw.assistant_page.runs == [("make a landing page for my coffee shop", "live", 1)]
    assert page._rows[1].text.text() == "“make a landing page for my coffee shop”"
    mw.assistant_page.live_routed.emit(1, "new", "Coffee Shop Site", "k1")
    assert page._rows[1].outcome.text() == "Started “Coffee Shop Site”  →" and page._rows[1].key == "k1"
    assert L.learned == [1] and chimes == ["Tink"] and "live_task" in tracked

    # not a request
    L.utterance_ended.emit(2)
    L.heard.emit(2, "yeah I'll call you back")
    mw.assistant_page.live_routed.emit(2, "ignored", "", "")
    assert page._rows[2].outcome.text() == "Ignored — not a request" and L.learned == [1] and chimes == ["Tink"]
    assert mw.assistant_page.ov.notes[-1] == "Not a request", "ignored speech gets a note at the top"

    # router down
    L.heard.emit(3, "rename the folder")
    mw.assistant_page.live_routed.emit(3, "failed", "", "")
    assert page._rows[3].outcome.text().startswith("Couldn't check this one")

    # Esc / a cough / Stop while transcribing
    L.dropped.emit(4, "", "cancelled")
    assert page._rows[4].outcome.text() == "Cancelled (Esc) — not sent"
    assert mw.assistant_page.ov.notes[-1] == "✕  Cancelled" and chimes[-1] == "Pop", "Esc is visible + audible"
    n_notes = len(mw.assistant_page.ov.notes)
    L.dropped.emit(4, "delete everything in downloads", "cancelled")   # the cancelled text arrives later
    assert page._rows[4].text.text() == "“delete everything in downloads”"
    assert len(mw.assistant_page.ov.notes) == n_notes, "one note per cancel, not two"
    # a late Esc while the router is still checking one
    L.heard.emit(11, "rename my project folder")
    assert page._rows[11].outcome.text() == "Checking…"
    L.esc_late.emit()
    assert page._rows[11].outcome.text() == "Cancelled (Esc) — not sent" and \
        mw.assistant_page.ov.notes[-1] == "✕  Cancelled"
    L.dropped.emit(5, "", "too short")
    assert 5 not in page._rows, "a cough doesn't get a row"
    L.utterance_ended.emit(6)
    L.dropped.emit(6, "", "stopped")
    assert page._rows[6].outcome.text() == "Not sent — you clicked Stop"

    # "yes" to a waiting approval
    L.heard.emit(12, "yes go ahead")
    mw.assistant_page.live_routed.emit(12, "approved", "Coffee Shop Site", "k1")
    assert page._rows[12].outcome.text() == "Approved — “Coffee Shop Site”  →" and \
        mw.assistant_page.ov.notes[-1] == "✓  Approved"
    assert 12 in L.learned
    # chime off → no chime
    settings.live_chime = False
    n_ch = len(chimes)
    L.heard.emit(7, "open my notes")
    mw.assistant_page.live_routed.emit(7, "continued", "Notes", "k1")
    assert len(chimes) == n_ch and page._rows[7].outcome.text() == "Continued “Notes”  →"
    settings.live_chime = True

    # clicking a row opens that chat in Agents (page 5 on Windows too)
    page._open_chat(page._rows[1].key)
    assert mw.nav == [5] and mw.nav_button_group.b.checked and mw.assistant_page.focused == ["k1"]

    # newest first, capped
    assert page._rows_box.itemAt(0).widget() is page._rows[7]
    for i in range(100, 100 + lp.MAX_ROWS + 5):
        L.dropped.emit(i, "", "cancelled")
    assert len(page._rows) == lp.MAX_ROWS
    page.clear_activity()
    assert not page._rows and not page._empty.isHidden()

    page.start_btn.click()
    assert not L.on and page.start_btn.text().strip() == "Start listening" and tracked[-1] == "live_stopped"
    assert mw.assistant_page.ov.lights[-1] == ""


def test_tab_settings():
    page, L, mw = _tab()
    saves.clear()
    page.wait_slider.setValue(7)
    assert settings.live_send_after_sec == 3.5 and page._wait_val.text() == "3.5 s" and saves and L.applied
    page.sens_btns["high"].click()
    assert settings.live_sensitivity == "high" and page.sens_btns["high"].isChecked()
    page.noise_toggle.click()
    assert settings.live_noise_filter is False and page.noise_toggle.text() == "Off"
    page.chime_toggle.click()
    assert settings.live_chime is False
    assert page.speak_toggle.isEnabled() and page.speak_toggle.isChecked() == bool(settings.live_speak)
    page.noise_toggle.click(); page.chime_toggle.click(); page.sens_btns["medium"].click(); page.wait_slider.setValue(4)
    assert (settings.live_noise_filter, settings.live_chime, settings.live_sensitivity,
            settings.live_send_after_sec) == (True, True, "medium", 2.0)
    page.open_toggle.click()
    assert settings.assistant_auto_open is False
    page.open_toggle.click()
    for theme in ("light", "dark"):
        page.apply_theme(theme)                                   # re-themes without errors
        assert page._c["dark"] == (theme == "dark") and page._c["bg"] == lp.get_theme_colors(theme)["bg"]


def test_windows_specifics():
    """Windows: the noise filter row is hidden (it would do nothing); the keys are Windows' names everywhere on
    screen; the chime sounds exist; the tour speaks of Shift + Ctrl and your PC."""
    page, L, mw = _tab()
    page.resize(1200, 800)
    page.show()
    try:
        assert not page.noise_toggle._row.isVisibleTo(page), "no noise-filter switch on Windows"
        assert page.speak_toggle._row.isVisibleTo(page) and page.chime_toggle._row.isVisibleTo(page)
        assert "Shift" in page.draw_key.text() and "Ctrl" in page.draw_key.text()
        assert page._many_key.text().count("Ctrl") == 3
        L.start()
        L.state_changed.emit("listening", "")
        assert "Shift" in page._sub.text() and "Ctrl" in page._sub.text(), page._sub.text()
        page._start_collecting()
        assert "Ctrl" in page._sub.text() and mw.assistant_page.ov.notes[-1].endswith("tap Ctrl to send")
        page._drop_collection("Thrown away — not sent")
        assert page._tray_send.text() == "Send all  Ctrl" and page._tray_drop.text() == "Throw away  Esc"
        mac = ("⌃", "⇧", "⌘", "Mac", "notch", "menu bar", "Control")
        for st in ("off", "starting", "listening", "hearing", "countdown", "paused", "error"):
            L.state_changed.emit(st, "")
            shown = [w.text() for w in page.findChildren(QLabel) + page.findChildren(QPushButton)
                     if w.isVisibleTo(page) and w.text()]
            shown += [w.toolTip() for w in page.findChildren(QLabel) + page.findChildren(QPushButton) if w.toolTip()]
            bad = [t for t in shown if any(m in t for m in mac)]
            assert not bad, (st, bad)
        from app.ui import page_tours
        tour = page_tours.steps("assistant")
        for st in tour:
            assert not any(m in st["title"] + st["description"] for m in mac), st
        assert tour[1]["art"] == ("keys", ["Shift", "→", "Ctrl", "+", "Win"]) and tour[1]["title"] == "Mute it"
        assert tour[2]["art"] == ("keys", ["Shift", "+", "Ctrl"]) and "your PC is locked" in tour[-1]["description"]
        assert lp.DRAW_KEY == "Shift + Ctrl"
        for f in lp._CHIMES.values():
            assert (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Media" / f).exists(), f
        assert page._add_tray_item() is None and page._tray_action is None, "no tray item on Windows"
    finally:
        L.stop()
        page.hide()


def test_orb_clock_only_while_on_screen():
    """The orb's 60-fps clock wakes the UI only while it's on screen: another page / hidden → stopped."""
    page, L, mw = _tab()
    page.resize(1200, 800)
    page.show()
    try:
        L.start()
        L.state_changed.emit("listening", "")
        assert page._orb._t.isActive()
        page.hide()
        assert not page._orb._t.isActive(), "hidden → no 60-Hz timer"
        L.state_changed.emit("hearing", "")
        assert not page._orb._t.isActive(), "a state change while hidden doesn't start it"
        page.show()
        assert page._orb._t.isActive(), "shown again → it animates"
        L.stop()
        assert not page._orb._t.isActive(), "off → stopped"
    finally:
        page.hide()


def test_chime_is_silent_offscreen():
    """The real _chime (not the fixture's fake): every Mac sound name maps to a Windows one, and offscreen (tests)
    it returns at once without playing or raising."""
    assert set(lp._CHIMES) == {"Tink", "Pop", "Morse"}
    for name in ("Tink", "Pop", "Morse", "unknown"):
        t = time.perf_counter()
        assert _REAL_CHIME(name) is None
        assert time.perf_counter() - t < 0.05, "never blocks"


def test_settings_load_validation(tmp_path):
    data = {"live_send_after_sec": 99, "live_sensitivity": "loud", "live_chime": "yes", "live_noise_filter": False}
    import json
    (tmp_path / "settings.json").write_text(json.dumps(data))
    real = type(settings).get_app_data_dir
    try:
        type(settings).get_app_data_dir = lambda self: Path(tmp_path)
        s2 = type(settings)()
    finally:
        type(settings).get_app_data_dir = real
    assert s2.live_send_after_sec == 10.0 and s2.live_sensitivity == "medium" and s2.live_chime is True
    assert s2.live_noise_filter is False


def test_real_wiring_tab_to_agents_page():
    """The real Agents page + the real tab: heard → run_voice_task(live) → router → live_routed (4 args) → the row.
    (A fake listener and router; run_task is faked so no agent runs.)"""
    page_ap, notch, runs, reports = _page()
    mw = _MW()
    mw.assistant_page, mw.nav_button_group = page_ap, FakeGroup()
    L = FakeListener()
    tab = lp.LivePage(mw, listener=L)
    real = _with_router(_Router("NEW"))
    try:
        tab.start()
        L.utterance_ended.emit(1)
        L.heard.emit(1, "summarize my last three emails")
        assert _pump(lambda: tab._rows[1].outcome.text().startswith("Started"), 3), tab._rows[1].outcome.text()
        assert runs == ["summarize my last three emails"] and tab._rows[1].key == "k1"
        assert page_ap._hands_free is True, "Start tells the Agents page (it keeps Claude Code ready)"
    finally:
        _restore(real)
        tab.stop()
    assert page_ap._hands_free is False, "Stop tells it too"


def test_redesigned_page():
    """The page in the screenshots: the orb shows the state; a heard request shows where it went and goes working →
    done; the tour button stands out until opened (saved); Settings hide (saved); the Ctrl Ctrl tray lists what goes
    as one message; every button sets its own padding (the app's QSS would hide its text)."""
    page, L, mw = _tab()
    assert page._orb._state == "off" and page._orb.width() == 236 and page.status.text().startswith("Talk to your agents")
    page._orb.clicked.emit()                            # the orb starts it too
    assert L.on and page._orb._state == "starting" and page._orb.width() == 176
    L.state_changed.emit("listening", "")
    assert page._orb._state == "listen" and page.status.text() == "Listening — just talk"
    for st, orb in (("hearing", "hear"), ("countdown", "count"), ("paused", "paused"), ("error", "error"),
                    ("listening", "listen")):
        L.state_changed.emit(st, "x")
        assert page._orb._state == orb, (st, page._orb._state)
    L.state_changed.emit("countdown", "")
    L.countdown.emit(0.5)
    assert 0.7 < page._orb._prog <= 0.8, page._orb._prog                  # 2 s wait, 0.5 s left → 75 %
    L.countdown.emit(1.0)                               # (a report that's behind the clock: ignored, never backwards)
    a = page._orb.left_now()
    time.sleep(0.15)
    assert a <= 0.5 and page._orb.left_now() < a - 0.1, "the ring keeps filling between the listener's reports"
    L.state_changed.emit("listening", "")
    L.utterance_ended.emit(1)
    L.heard.emit(1, "make a landing page")
    mw.assistant_page.live_routed.emit(1, "new", "Coffee Shop Site", "k1")
    r = page._rows[1]
    assert r.state == "run" and "New chat" in r.line.text() and "Coffee Shop Site" in r.line.text() and \
        r.open.isVisibleTo(r)
    assert page._feed.isVisibleTo(page) and page._heard.text() == "“make a landing page”"
    page._on_live_event("k1", "done", {"text": "Done."})
    assert r.state == "done" and r.state_lbl.text() == "done"
    # the tour: stands out until opened, then quiet (saved)
    assert "New here?" in page.tour_btn.text() and page.tour_btn.graphicsEffect() is not None
    L.state_changed.emit("off", ""); L.on = False
    page._show_tour()
    assert settings.live_tour_seen and page.tour_btn.text().strip() == "See what it can do"
    assert page._tour.steps[1]["title"] == "Mute it" and page._tour.steps[2]["title"] == "Point at your screen"
    page._tour._remind_later()
    # Settings: hide → saved; the header's Settings button brings them back
    page._set_settings_collapsed(True)
    assert settings.live_settings_collapsed and not page._set_card.isVisibleTo(page) and \
        page._show_set_btn.isVisibleTo(page)
    page._show_set_btn.click()
    assert not settings.live_settings_collapsed and page._set_card.isVisibleTo(page)
    # Ctrl Ctrl: the tray lists what goes as ONE message, in order
    L.start()
    L.state_changed.emit("listening", "")
    page._start_collecting()
    L.utterance_ended.emit(2)
    L.heard.emit(2, "make the header blue")
    assert page._tray_wrap.isVisibleTo(page) and page._tray_items.count() == 1 and page._orb._badge == 1
    assert page.status.text().startswith("Collecting — 1 thing")
    page._tray_drop.click()
    assert page._collect is None and not page._tray_wrap.isVisibleTo(page)
    from app.ui.onboarding import OnboardingOverlay   # (the closed tour's panel: its own window and style - it's
    unpadded = [b.text() or b.toolTip() for b in page.findChildren(QPushButton)   # checked in test_page_tours)
                if "padding" not in b.styleSheet() and not isinstance(b, lp._PlayBtn)
                and not isinstance(b.window(), OnboardingOverlay)]
    assert not unpadded, unpadded


# ---- 2026-11-09 fixes: switching agents, the Assistant tab's Agent choice, a longer wait --------------------------
def _real_pair():
    """The REAL Agents page (fake run_task — no agent runs) + the real Assistant tab on it."""
    page_ap, notch, runs, reports = _page()
    mw = _MW()
    mw.assistant_page, mw.nav_button_group = page_ap, FakeGroup()
    tab = lp.LivePage(mw, listener=FakeListener())
    tab._hook_setup()
    return page_ap, tab, runs


def test_assistant_tab_agent_choice():
    """Settings → Agent: Claude Code / Codex, the same pick as the Agents page's menu (saved, both directions); with a
    chat of the other agent open, switching starts a new chat (the next thing you say goes to the new agent); the
    line under it says when the picked agent isn't set up."""
    ap_, tab, runs = _real_pair()
    assert tab._agent_segs.isVisibleTo(tab) or not tab.isVisible()
    assert tab.agent_btns["claude"].isChecked() and not tab.agent_btns["codex"].isChecked()
    assert "Follow-ups stay with their chat's agent" in tab._agent_hint.text()
    saves.clear()
    tab.agent_btns["codex"].click()
    assert ap_._primary == "codex" and settings.assistant_agent == "codex" and saves, "picked here → saved"
    assert tab.agent_btns["codex"].isChecked() and ap_._pick_agent() == "codex"
    ap_._set_primary("claude")                                          # picked on the Agents page
    assert tab.agent_btns["claude"].isChecked(), "the Assistant tab follows the Agents page"
    # a Codex chat is open (you were talking to it) → picking Claude Code starts a NEW chat with Claude Code
    s = ap.AgentSession("cx", "C:/tmp", "count my files", "Count Files", True, "gpt", "high", agent="codex")
    s.session_id = "codex-thread"
    ap_._sessions["cx"], ap_._focused_key = s, "cx"
    tab.agent_btns["claude"].click()
    assert ap_._focused is None and ap_._primary == "claude", "the box talks to Claude Code now (a new chat)"
    assert "cx" in ap_._sessions, "the Codex chat stays in the list"
    # the picked agent isn't set up: said under it (and that the other one answers meanwhile)
    ap_._publish_agents({"claude": "ready", "codex": "install"})
    tab.agent_btns["codex"].click()
    assert "Codex isn't set up yet" in tab._agent_hint.text() and "Claude Code answers meanwhile" in tab._agent_hint.text()
    for theme in ("dark", "light"):
        tab.apply_theme(theme)
    assert all("padding" in b.styleSheet() for b in tab.agent_btns.values())


def test_the_30_second_age_tick_only_updates_the_ages():
    """'now' → '5 min' on the rows every 30 s — without restyling them all (a full list of 100 froze the window
    ~0.8 s every 30 s, measured)."""
    _, tab, _ = _real_pair()
    rows = [tab._row(i) for i in range(3)]
    for i, r in enumerate(rows):
        r.set_outcome("Opened it", "#B39DFF")
        r.t0 = time.time() - 60 * (5 + i)
    styled = []
    for r in rows:
        r.restyle = lambda r=r: styled.append(r)
    tab._age_timer.stop()
    tab._age_timer.setInterval(1)
    tab._age_timer.start()
    end = time.time() + 2
    while time.time() < end and rows[0].when.text() != "5 min":
        QApplication.processEvents()
    tab._age_timer.stop()
    assert [r.when.text() for r in rows] == ["5 min", "6 min", "7 min"]
    assert styled == [], "the tick must not restyle every row"


def test_agents_menu_switch_starts_a_new_chat():
    """The Agents page's agent menu: ✓ = the agent the box is using (the open chat's); the other row says it starts a
    new chat, and picking it does — even when it's already "new chats' agent" (that did NOTHING before: the user
    couldn't switch away from an open Codex chat). Picking the ✓ one changes nothing."""
    from PySide6.QtWidgets import QMenu
    from app.ui.agents_view import AgentsView
    v = AgentsView("light")
    picked = []
    v.agent_changed.connect(picked.append)
    v.set_agent("codex", "claude", "codex")                 # a Codex chat open; new chats → Claude Code already
    menu = QMenu(v)
    for agent in ("claude", "codex"):
        v._agent_row(menu, agent)
    rows = {a.text().split(" — ")[0]: a for a in menu.actions()}
    texts = {k: " ".join(l.text() for l in act.defaultWidget().findChildren(QLabel)) for k, act in rows.items()}
    assert "Starts a new chat" in texts["Claude Code"] and "Starts a new chat" not in texts["Codex"], texts
    v._pick_agent("codex")
    assert picked == [], "the ✓ one (the open chat's agent): nothing to change"
    v._pick_agent("claude")
    assert picked == ["claude"], "Claude Code picked while a Codex chat is open → switch"
    picked.clear()
    v.set_agent("claude", "claude", None)                   # no chat open
    v._pick_agent("claude")
    assert picked == []
    v._pick_agent("codex")
    assert picked == ["codex"]
    # the page: picking it leaves the open chat for a new one; what you typed stays
    ap_, _tab, _runs = _real_pair()
    s = ap.AgentSession("cx", "C:/tmp", "count my files", "Count Files", True, "gpt", "high", agent="codex")
    ap_._sessions["cx"], ap_._focused_key = s, "cx"
    ap_.view.set_input_text("make a landing page")
    ap_._set_primary("claude")                              # (already Claude: before, this returned at once)
    assert ap_._focused is None and ap_.view.composer.input.toPlainText() == "make a landing page"
    assert ap_.view.chip.text().startswith("Claude Code"), ap_.view.chip.text()


def test_countdown_follows_a_longer_wait():
    """It sounded unfinished → the listener waits longer: the ring and "Sending in" follow the new total (before, the
    'only ever forward' rule kept the old one: the ring filled, then sat full)."""
    tab, L, mw = _tab()
    L.send_after_now = lambda: 2.0
    L.start()
    L.state_changed.emit("countdown", "")
    L.countdown.emit(1.0)
    assert abs(tab._orb._count[2] - 2.0) < 0.01 and 0.45 <= tab._orb._prog <= 0.55
    L.send_after_now = lambda: 6.0                          # "…called" → 6 s
    L.countdown.emit(4.6)
    assert abs(tab._orb._count[2] - 6.0) < 0.01 and tab._orb.left_now() > 4.0, tab._orb._count
    assert tab.status.text().startswith("Sending in 4."), tab.status.text()
    L.stop()
