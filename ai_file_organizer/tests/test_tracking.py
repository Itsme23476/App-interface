"""Feature-usage analytics (port of the Mac's tests/test_tracking.py + Windows checks). No network, nothing sent:
the sign-in and the POST are faked - and deliberately SLOW (a token refresh taking 1.5 s, a POST taking 1 s).
  - track() returns in microseconds even then; the token + network happen ONLY on the one background thread
    ("filect-analytics"), never the caller's
  - batched: one POST of a JSON array per TRACK_BATCH events, or TRACK_DELAY_SEC after the first unsent one
  - offline -> kept and retried; a bad request (4xx) -> dropped; not signed in -> dropped
  - queue full (offline for ages) -> newer events dropped, still no slowdown, never an exception
  - odd values never block the batch; flush_tracking() sends at quit within its timeout
Windows: every event the Mac sends exists here, none carries text / names / paths / queries, and the real window's
tabs report Windows' tab names.
"""
import ast
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.core import supabase_client as sc  # noqa: E402

_REAL_SEND = sc._track_send                    # (conftest cuts the sender for every test; these tests need it)
POSTS, THREADS = [], []
MODE = {"net": "ok", "token_sec": 0.0, "post_sec": 0.0}


class _Resp(io.BytesIO):
    pass


def fake_urlopen(req, timeout=None, context=None):
    THREADS.append(threading.current_thread().name)
    time.sleep(MODE["post_sec"])
    if MODE["net"] == "offline":
        raise urllib.error.URLError("offline")
    if MODE["net"] == "400":
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, None)
    assert req.full_url.endswith("/rest/v1/app_events") and req.get_header("Prefer") == "return=minimal"
    POSTS.append(json.loads(req.data.decode()))
    return _Resp(b"")


class FakeAuth:
    def __init__(self):
        self.current_user = {"id": "user-123"}

    def get_access_token(self):
        THREADS.append(threading.current_thread().name)
        time.sleep(MODE["token_sec"])                 # a token refresh over the network
        return "tok"


def _wait(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def _rows():
    return [r for batch in POSTS for r in batch]


def _drain():
    with sc._track_q.mutex:
        sc._track_q.queue.clear()


@pytest.fixture
def net(monkeypatch):
    """The real sender, a fake network + sign-in, a short delay; the queue starts empty."""
    monkeypatch.setattr(sc, "_track_send", _REAL_SEND)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sc, "supabase_auth", FakeAuth())
    monkeypatch.setattr(sc, "TRACK_DELAY_SEC", 0.4)
    MODE.update(net="ok", token_sec=0.0, post_sec=0.0)
    _drain()
    sc.flush_tracking(2.0)                           # (anything another test left: gone before we count)
    POSTS.clear()
    THREADS.clear()
    yield
    MODE.update(net="ok", token_sec=0.0, post_sec=0.0)
    _drain()
    _wait(lambda: sc._track_pending[0] == 0 and sc._track_q.empty(), 8)   # (a slow send finishes before the next)


def test_never_blocks_the_caller(net):
    MODE.update(token_sec=1.5, post_sec=1.0)          # slow everything
    t0 = time.perf_counter()
    for i in range(200):
        sc.track("tab_opened", tab="search", i=i)
    dt = time.perf_counter() - t0
    assert dt < 0.05, f"200 events took {dt * 1000:.1f} ms on the caller's thread"
    assert _wait(lambda: len(_rows()) == 200, 40), len(_rows())
    assert set(THREADS) == {"filect-analytics"}, f"token / network ran on: {set(THREADS)}"
    assert all(len(b) <= sc.TRACK_BATCH for b in POSTS) and len(POSTS) >= 200 // sc.TRACK_BATCH
    r = _rows()[0]
    assert r["user_id"] == "user-123" and r["event_name"] == "tab_opened" and r["props"]["tab"] == "search"
    assert r["platform"] == "win" and r["created_at"].endswith("+00:00")


def test_batches_and_delay(net):
    for _ in range(3):
        sc.track("voice_used", mode="dictate", words=4)
    time.sleep(0.15)
    assert POSTS == [], "fewer than a batch: waits for the delay"
    assert _wait(lambda: len(_rows()) == 3, 3) and len(POSTS) == 1, "...then ONE request with all three"


def test_offline_bad_request_signed_out(net):
    MODE["net"] = "offline"
    sc.track("search_performed", results_count=3)
    time.sleep(0.8)
    assert POSTS == []
    MODE["net"] = "ok"
    assert _wait(lambda: len(_rows()) == 1, 3), "offline -> kept, sent once it's back"
    POSTS.clear()
    MODE["net"] = "400"
    sc.track("broken")
    time.sleep(0.8)
    MODE["net"] = "ok"
    sc.track("after")
    assert _wait(lambda: [r["event_name"] for r in _rows()] == ["after"], 3), "4xx: dropped, the next ones go"
    POSTS.clear()
    sc.supabase_auth.current_user = None
    sc.track("signed_out")
    time.sleep(0.8)
    assert POSTS == [], "not signed in: dropped"


def test_odd_values_and_full_queue(net):
    sc.track("odd", thing=object(), n=float("inf"))
    sc.track("next")
    assert _wait(lambda: {r["event_name"] for r in _rows()} == {"odd", "next"}, 3), "odd values never block"
    MODE.update(net="offline", post_sec=0.2)          # offline for ages: the queue fills
    t0 = time.perf_counter()
    for i in range(sc.TRACK_MAX_QUEUED * 3):
        sc.track("flood", i=i)
    dt = time.perf_counter() - t0
    assert dt < 0.3, f"{sc.TRACK_MAX_QUEUED * 3} events with a full queue took {dt * 1000:.0f} ms"
    assert sc._track_q.qsize() <= sc.TRACK_MAX_QUEUED


def test_flush_at_quit(net, monkeypatch):
    monkeypatch.setattr(sc, "TRACK_DELAY_SEC", 30.0)  # (would normally wait 30 s)
    time.sleep(0.5)
    sc.track("session_ended", duration_seconds=12)
    time.sleep(0.1)
    t0 = time.perf_counter()
    sc.flush_tracking(1.0)
    assert time.perf_counter() - t0 < 1.0 and [r["event_name"] for r in _rows()][-1:] == ["session_ended"]
    MODE["post_sec"] = 1.5                            # a hung network at quit: waits at most the timeout
    sc.track("late")
    t0 = time.perf_counter()
    sc.flush_tracking(0.3)
    assert time.perf_counter() - t0 < 0.5


def test_old_callers_sync_wait_is_not_a_prop(net):
    """track(..., sync_wait_seconds=2) (the old quit path) sends now - it's never stored as one of the props."""
    sc.track("session_ended", sync_wait_seconds=2.0, duration_seconds=5)
    assert _wait(lambda: len(_rows()) == 1, 3)
    assert _rows()[0]["props"] == {"duration_seconds": 5}


# ---- Windows: the events exist, and none carries anything private ------------------------------------------
MAC_EVENTS = {"app_opened", "session_ended", "tab_opened", "quick_search_opened", "search_performed", "voice_used",
              "index_completed", "assistant_task", "assistant_done", "assistant_error",
              "assistant_full_access_from_nudge", "organize_started", "organize_completed", "auto_organize_enabled",
              "auto_organize_disabled", "vision_call", "onboarding_step_viewed", "onboarding_dismissed",
              "onboarding_completed", "page_tour_opened", "page_tour_closed", "page_tour_completed",
              "assistant_auto_open", "live_organize_preview"}
PRIVATE = {"query", "text", "path", "file_path", "folder", "folder_path", "name", "file_name", "filename", "email",
           "title", "prompt", "said", "heard", "message", "msg", "error", "words_text", "transcript", "instruction"}


def _track_calls():
    """Every track(...) / _track(...) call in the app: (file, line, event, {keyword: value node})."""
    out = []
    for f in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("track", "_track"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    out.append((f.name, node.lineno, node.args[0].value, {k.arg: k.value for k in node.keywords}))
    return out


def test_every_event_the_mac_sends_is_sent_here_too():
    found = {e for _f, _l, e, _k in _track_calls()} | {"page_tour_closed", "page_tour_completed"}  # (via _end_page_tour)
    missing = MAC_EVENTS - found
    assert not missing, f"not sent on Windows: {sorted(missing)}"


def test_no_event_carries_text_names_paths_or_queries():
    bad = []
    allowed = {("page_tours.py", "page_tour_opened", "page")}     # (`name` there is the tour's slug: "voice")
    for f, line, event, kw in _track_calls():
        for k, v in kw.items():
            if k in PRIVATE:
                bad.append((f, line, event, k))
            if isinstance(v, ast.Name) and v.id in PRIVATE and (f, event, k) not in allowed:
                bad.append((f, line, event, f"{k}={v.id}"))
    assert not bad, bad


def test_the_tabs_report_their_windows_names():
    from real_window import RealWindow, pump
    rw = RealWindow()
    try:
        w = rw.W
        rw.tracked.clear()
        for page in (1, 2, 3, 4, 5, 6, 0):
            w.nav_button_group.button(page).click()
            pump(0.02)
        w.nav_button_group.button(0).click()               # (the same tab again: not counted)
        pump(0.02)
        tabs = [k.get("tab") for e, k in rw.tracked if e == "tab_opened"]
        assert tabs == ["organize", "analyze", "voice", "settings", "agents", "assistant", "search"], tabs
    finally:
        rw.close()
