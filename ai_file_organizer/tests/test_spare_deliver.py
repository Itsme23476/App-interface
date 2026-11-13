"""A warm Claude Code that stopped (couldn't start, signed out, out of usage, crashed) refuses a request instead of
swallowing it - the Mac's live test saw the chat say "working" for good (pack 2026-11-12 §5.4, Mac f2db4ea). The fix is
in Windows' AgentWorker; these are the Mac's 4 tests, ported. A fake runner: nothing real starts.
  · a stopped spare refuses (deliver() → False, the chat starts fresh)
  · failing just as a request is handed over: the error reaches THAT chat (it went out under the spare's own key)
  · a running spare gets the request; one given before it parks still arrives"""
import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])          # (a full one: other tests in the run need widgets)
import app.core.agent_runner as ar  # noqa: E402
from app.ui.assistant_page import AgentWorker  # noqa: E402

GOT = []


class FakeRunner:
    mode = "wait"

    def __init__(self, *a, **k):
        pass

    async def run(self, prompt, wait_for=None, **kw):
        if FakeRunner.mode == "die":
            raise ar.AgentSetupError("install")
        if wait_for is not None:
            GOT.append(await wait_for())


@pytest.fixture(autouse=True)
def _fake_runner(monkeypatch):
    monkeypatch.setattr(ar, "AgentRunner", FakeRunner)          # (only for these tests - put back after each)
    GOT.clear()
    yield


def worker():
    w = AgentWorker("spare1", str(Path.home()), "", parked=True, agent="claude")
    seen = {"done": [], "error": []}
    w.done.connect(lambda k: seen["done"].append(k))
    w.error.connect(lambda k, m: seen["error"].append((k, m)))
    return w, seen


def test_a_stopped_spare_refuses_the_request():
    FakeRunner.mode = "die"
    w, seen = worker()
    t = threading.Thread(target=w.run)
    t.start()
    t.join(5)
    app.processEvents()
    assert w.ended and seen["done"] == ["spare1"] and seen["error"], seen
    assert w.deliver("s7", "What is the capital of Canada?") is False, "nothing waits: it must say no"
    assert w.key == "spare1", "a refused request doesn't take the worker over"


def test_failing_while_the_request_is_handed_over_reaches_the_chat(monkeypatch):
    """The live race: it fails, and the request is handed over before it has finished stopping. Its error must reach
    the chat that took it (the old code sent it under "spare1", and the chat waited for good)."""
    FakeRunner.mode = "die"
    w, seen = worker()
    gate, inside = threading.Event(), threading.Event()
    real_new = asyncio.new_event_loop

    def held_loop():
        loop = real_new()
        real_close = loop.close

        def close():                                 # (hold it between failing and finishing)
            inside.set()
            gate.wait(5)
            real_close()
        loop.close = close
        return loop
    monkeypatch.setattr(asyncio, "new_event_loop", held_loop)
    t = threading.Thread(target=w.run)
    t.start()
    assert inside.wait(5)
    accepted = w.deliver("s7", "What is the capital of Canada?")
    gate.set()
    t.join(5)
    app.processEvents()
    assert accepted is True and seen["done"] == ["s7"], (accepted, seen)
    assert seen["error"] and seen["error"][0][0] == "s7", ("the chat must hear why", seen["error"])


def test_a_running_spare_gets_the_request():
    FakeRunner.mode = "wait"
    w, seen = worker()
    t = threading.Thread(target=w.run)
    t.start()
    time.sleep(0.3)                                  # (it's parked, waiting)
    assert not w.ended
    assert w.deliver("s8", "hello", ["img"]) is True
    t.join(5)
    app.processEvents()
    assert GOT == [("hello", ["img"])] and w.key == "s8" and seen["done"] == ["s8"], (GOT, w.key, seen)


def test_deliver_before_it_parks_still_arrives():
    FakeRunner.mode = "wait"
    w, seen = worker()
    assert w.deliver("s9", "early") is True          # (given before its loop even started)
    t = threading.Thread(target=w.run)
    t.start()
    t.join(5)
    app.processEvents()
    assert GOT == [("early", [])] and seen["done"] == ["s9"], (GOT, seen)
