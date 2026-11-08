"""Agents engine (Windows port of the Mac tests test_worker_thread_affinity + test_concurrent_sessions).

Headless (QT_QPA_PLATFORM=offscreen), a FAKE runner (no `claude`), and settings._save_config stubbed so the
user's settings are never written.
  * worker signals must deliver on the MAIN thread (the hard crash on Mac: a lambda slot ran GUI code on the
    worker thread);
  * concurrent sessions never cross-talk; approvals answer the focused one; focus swaps;
  * typing in an open chat continues it; a follow-up sent while it works is queued and sent after;
  * Stop ends a turn (interrupt → force-cancel fallback) and says "Stopped", not done;
  * a setup error shows plain words + the setup kind; quit stops every thread.
"""
import asyncio
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.core import agent_runner as ar          # noqa: E402
from app.core.settings import settings           # noqa: E402
from app.ui import assistant_page as ap          # noqa: E402


_PAGES = []


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(settings, "assistant_bypass", False)
    monkeypatch.setattr(ap, "_play_chime", lambda ok=True: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", str(tmp_path / "Filect Assistant"))
    from app.core import agent_sessions as _asn          # never the real app-data files
    monkeypatch.setattr(_asn, "_touched_path", lambda: tmp_path / "assistant_sessions.json")
    from app.core import agent_runner as _ar
    monkeypatch.setattr(_ar, "_models_cache_path", lambda: tmp_path / "claude_models.json")
    orig = ap.AssistantPage.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        _PAGES.append(self)
    monkeypatch.setattr(ap.AssistantPage, "__init__", track)
    yield
    while _PAGES:                      # every page's threads end, even when a test failed
        _PAGES.pop().shutdown()


class _MW:
    pass


def _wait(cond, secs=8.0):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return cond()


class _FakeRunner:
    """Stands in for AgentRunner: a text step + a result, or a slow turn that only Stop ends."""
    calls = []
    slow = False
    approve_first = False

    def __init__(self, cwd, on_step, on_approval, on_result):
        self.cwd, self.on_step, self.on_approval, self.on_result = cwd, on_step, on_approval, on_result
        self.allow_all = False
        self._stop = asyncio.Event()

    async def run(self, prompt, resume=None, max_turns=40, bypass=False, model=None, effort=None, images=None):
        _FakeRunner.calls.append({"prompt": prompt, "resume": resume, "cwd": self.cwd, "model": model,
                                  "effort": effort, "bypass": bypass})
        if _FakeRunner.approve_first:
            ok = await self.on_approval(ar.ApprovalRequest("Write", {"file_path": "C:\\x\\a.txt", "content": "1"}))
            self.on_step(ar.Step("text", text=f"approved={ok}"))
        if _FakeRunner.slow:
            self.on_step(ar.Step("tool", tool="Bash", detail="ping", input={"command": "ping"}, tool_id="t1"))
            try:
                await asyncio.wait_for(self._stop.wait(), 30)
            except asyncio.TimeoutError:
                pass
            return None
        self.on_step(ar.Step("text", text=f"hello from the worker: {prompt}"))
        sid = resume or f"sid-{len(_FakeRunner.calls)}"
        self.on_result(sid, 0.01)
        return sid

    async def interrupt(self):
        pass                 # like Windows Claude Code under emulation: the interrupt is slow → force-cancel


@pytest.fixture
def fake(monkeypatch):
    _FakeRunner.calls, _FakeRunner.slow, _FakeRunner.approve_first = [], False, False
    monkeypatch.setattr(ar, "AgentRunner", _FakeRunner)
    return _FakeRunner


class _TidPage(ap.AssistantPage):
    tids = {}

    def _on_step(self, key, step):
        _TidPage.tids["step"] = threading.get_ident()
        return super()._on_step(key, step)

    def _on_result(self, key, sid, cost):
        _TidPage.tids["result"] = threading.get_ident()
        return super()._on_result(key, sid, cost)


def test_worker_signals_run_on_main_thread(fake):
    page = _TidPage(_MW())
    page.run_task("build a tiny thing")
    key = next(iter(page._sessions))
    assert _wait(lambda: page._sessions[key].status == "done" and not page._sessions[key].running)
    main = threading.get_ident()
    assert _TidPage.tids.get("step") == main, "step handler ran off the main thread"
    assert _TidPage.tids.get("result") == main, "result handler ran off the main thread"
    s = page._sessions[key]
    assert s.session_id == "sid-1"
    assert any(e["kind"] == "text" and "hello from the worker" in e["text"] for e in s.entries)
    assert os.path.isdir(ap.DEFAULT_WORKSPACE), "the default workspace is created for a new task"
    page.shutdown()


class _Step:
    def __init__(self, kind, tool="", text="", detail="", tool_id="", input=None, is_error=False):
        self.kind, self.tool, self.text, self.detail = kind, tool, text, detail
        self.tool_id, self.input, self.is_error = tool_id, input or {}, is_error


class _Req:
    def __init__(self, tool, tool_input=None):
        self.tool, self.tool_input = tool, (tool_input or {})


def test_concurrent_sessions_no_crosstalk():
    page = ap.AssistantPage(_MW())
    a = ap.AgentSession("s1", "C:\\a", "build a website", "Build Website", False, "opus", "high")
    b = ap.AgentSession("s2", "C:\\b", "find my tax docs", "Find Tax Docs", False, "opus", "high")
    page._sessions = {"s1": a, "s2": b}
    page._focused_key = "s1"
    page._on_step("s1", _Step("tool", tool="Write", detail="index.html", tool_id="w1"))
    page._on_step("s2", _Step("tool", tool="Bash", detail="npm install", tool_id="b1"))
    assert a.last_action == "Writing index.html" and b.last_action == "Running npm install"
    assert not any(e.get("tool") == "Bash" for e in a.entries), "cross-talk: s2's action leaked into s1"
    page._on_step("s2", _Step("text", text="Found 3 PDFs."))
    assert b.result_text == "Found 3 PDFs." and a.result_text == ""
    page._on_approval("s2", _Req("Bash", {"command": "rm x"}))
    assert b.status == "needs_you" and a.approval_req is None
    page._decide(True)                       # answers the FOCUSED one (s1, no gate) → no-op
    assert b.approval_req is not None, "the wrong session answered"
    page._focus("s2")
    page._decide(True)
    assert b.approval_req is None and b.status == "working"
    page._on_result("s1", "sidA", 0.42)
    assert a.session_id == "sidA" and a.status == "done" and b.status == "working"
    assert page._session_by_claude_id("sidA") is a
    page._on_error("s2", "boom")
    assert b.status == "error" and b.error == "boom"
    page._on_done("s1")
    assert a.thread is None and a.status == "done"


def test_typing_in_open_chat_continues_it_and_queues_while_running(fake):
    page = ap.AssistantPage(_MW())
    page._on_send("first task")
    key = page._focused_key
    assert _wait(lambda: page._sessions[key].status == "done" and not page._sessions[key].running)
    page._on_send("and a follow-up")               # the chat is open → same chat, resumed
    assert _wait(lambda: len(fake.calls) == 2 and not page._sessions[key].running)
    assert fake.calls[1]["resume"] == "sid-1" and len(page._sessions) == 1
    # a follow-up typed while it works is queued, then sent as the next turn of the same chat
    fake.slow = True
    page._on_send("slow one")
    assert _wait(lambda: page._sessions[key].running)
    page._on_send("queued one")
    assert page._sessions[key].pending_followup == "queued one"
    page._on_stop_task()                           # Stop drops the queued follow-up
    assert _wait(lambda: not page._sessions[key].running, 6)
    assert page._sessions[key].status == "stopped" and page._sessions[key].pending_followup is None
    # New chat → the next message is a NEW conversation
    fake.slow = False
    page._new_chat()
    page._on_send("something else")
    assert _wait(lambda: len(page._sessions) == 2)
    assert fake.calls[-1]["resume"] is None
    page.shutdown()


def test_follow_up_queued_then_sent(fake):
    page = ap.AssistantPage(_MW())
    fake.approve_first = True                      # holds the first turn at a card
    page._on_send("make a file")
    key = page._focused_key
    assert _wait(lambda: page._sessions[key].approval_req is not None)
    page._on_send("then this")                     # queued while it waits
    fake.approve_first = False                     # (the follow-up turn needn't ask)
    page._decide(True)
    assert _wait(lambda: len(fake.calls) == 2 and not page._sessions[key].running)
    assert fake.calls[1]["prompt"] == "then this" and fake.calls[1]["resume"] == "sid-1"
    assert any("approved=True" in e.get("text", "") for e in page._sessions[key].entries)
    page.shutdown()


def test_stop_while_a_card_waits_denies_it(fake):
    page = ap.AssistantPage(_MW())
    fake.approve_first = True
    page._on_send("make a file")
    key = page._focused_key
    assert _wait(lambda: page._sessions[key].approval_req is not None)
    page._on_stop_task()
    s = page._sessions[key]
    assert s.approval_req is None, "the card stayed up after Stop"
    assert _wait(lambda: not s.running, 6)
    assert s.status == "stopped"
    assert not any("approved=True" in e.get("text", "") for e in s.entries), "Stop must never approve"


def test_full_access_mid_turn_approves_waiting_card(fake):
    page = ap.AssistantPage(_MW())
    page.bypass_check.setChecked(False)
    fake.approve_first = True
    page._on_send("make a file")
    key = page._focused_key
    assert _wait(lambda: page._sessions[key].approval_req is not None)
    page._approve_always()                          # "Approve & don't ask again"
    assert page.bypass_check.isChecked() and settings.assistant_bypass is True
    assert _wait(lambda: page._sessions[key].status == "done" and not page._sessions[key].running)
    assert any("approved=True" in e.get("text", "") for e in page._sessions[key].entries)
    page.shutdown()


def test_model_choice_applies_to_next_turn(fake):
    page = ap.AssistantPage(_MW())
    page._on_model_changed("sonnet", "low")
    page._on_send("hi")
    assert _wait(lambda: fake.calls and not page._focused.running)
    assert fake.calls[0]["model"] == "sonnet" and fake.calls[0]["effort"] == "low"
    page.shutdown()


def test_setup_error_is_plain_words():
    page = ap.AssistantPage(_MW())
    s = ap.AgentSession("s1", "C:\\a", "hi", "Hi", False, "opus", "high")
    page._sessions = {"s1": s}
    page._focused_key = "s1"
    page._agents_state = {"claude": "ready", "codex": "install"}   # (else a signed-out Claude hands over to Codex)
    page._on_error("s1", ar.SETUP_SIGNIN)
    assert s.status == "error" and page._setup_kind(s.error) == "signin"
    page._on_error("s1", ar.SETUP_INSTALL)
    assert page._setup_kind(s.error) == "install"
    assert page._setup_kind("Some real failure") == ""


def test_quit_mid_task_stops_threads(fake):
    page = ap.AssistantPage(_MW())
    fake.slow = True
    page.run_task("slow")
    key = next(iter(page._sessions))
    assert _wait(lambda: page._sessions[key].running)
    t = page._sessions[key].thread
    page.shutdown()
    assert not t.isRunning(), "a running QThread outlived quit (crash on exit)"
