"""The reply streams in live (2026-11-08: a long answer showed nothing until it was done — or until Stop).
Headless; no `claude`; settings never written.
  * the runner turns Claude's word-by-word stream events into throttled "partial" steps (text only — no
    thinking, no tool input, no subagent words)
  * one live row grows; the finished text REPLACES it (never shown twice); Stop mid-reply keeps what was written
  * the Agents page updates that reply in place (same widget, no rebuild); the card shows the latest words"""
import asyncio
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.core import agent_runner as ar        # noqa: E402
from app.core.settings import settings         # noqa: E402
from app.ui import assistant_page as ap        # noqa: E402
from app.ui import details_view as dv          # noqa: E402


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(settings, "assistant_bypass", True)
    monkeypatch.setattr(ap, "_play_chime", lambda ok=True: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", str(tmp_path / "ws"))
    from app.core import agent_sessions as _asn
    monkeypatch.setattr(_asn, "_touched_path", lambda: tmp_path / "assistant_sessions.json")
    monkeypatch.setattr(ar, "_models_cache_path", lambda: tmp_path / "claude_models.json")
    yield


def _wait(cond, secs=6.0):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class _SE:
    def __init__(self, event, parent=None):
        self.event, self.parent_tool_use_id = event, parent


def _start(kind):
    return _SE({"type": "content_block_start", "index": 0, "content_block": {"type": kind}})


def _delta(text, kind="text_delta"):
    key = "text" if kind == "text_delta" else ("thinking" if kind == "thinking_delta" else "partial_json")
    return _SE({"type": "content_block_delta", "index": 0, "delta": {"type": kind, key: text}})


# ---- the runner ------------------------------------------------------------------------------------------
def test_runner_streams_text_deltas_throttled(monkeypatch):
    steps = []
    r = ar.AgentRunner("C:\\x", steps.append, None, None)
    clock = [100.0]
    monkeypatch.setattr(ar.time, "monotonic", lambda: clock[0])
    r._on_stream_event(_start("thinking"))
    r._on_stream_event(_delta("let me think…", "thinking_delta"))     # thinking: never shown
    r._on_stream_event(_SE({"type": "content_block_stop", "index": 0}))
    r._on_stream_event(_start("text"))
    r._on_stream_event(_delta("1, "))
    clock[0] += 0.05
    r._on_stream_event(_delta("2, "))                                  # < 0.2 s since the last: held back
    clock[0] += 0.3
    r._on_stream_event(_delta("3, "))
    r._on_stream_event(_delta("4"))                                    # (0 s later: held back)
    r._on_stream_event(_SE({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "!"}},
                           parent="toolu_sub"))                         # a subagent's words: not this reply
    r._on_stream_event(_SE({"type": "content_block_stop", "index": 0}))
    r._on_stream_event(_start("tool_use"))
    r._on_stream_event(_delta('{"command": "ls"}', "input_json_delta"))  # tool input: not text
    assert [(s.kind, s.text) for s in steps] == [("partial", "1, "), ("partial", "1, 2, 3, ")], \
        [(s.kind, s.text) for s in steps]


def test_runner_asks_claude_code_for_the_live_stream(monkeypatch):
    """include_partial_messages=True reaches ClaudeAgentOptions, and stream events are consumed (not steps)."""
    import claude_agent_sdk as sdk
    seen = {}

    class _Client:
        def __init__(self, options):
            seen["partial"] = getattr(options, "include_partial_messages", None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, prompt):
            pass

        async def receive_response(self):
            yield sdk.StreamEvent(uuid="u", session_id="s", event={"type": "content_block_start", "index": 0,
                                                                    "content_block": {"type": "text"}})
            yield sdk.StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta", "index": 0,
                                                                    "delta": {"type": "text_delta", "text": "Lis"}})
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Lisbon")], model="m")
            yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                    num_turns=1, session_id="sid-9", total_cost_usd=0.0)
    monkeypatch.setattr(sdk, "ClaudeSDKClient", _Client)
    monkeypatch.setattr(ar, "claude_mcp_servers", lambda cwd: {})
    steps, results = [], []
    r = ar.AgentRunner("C:\\x", steps.append, None, lambda sid, cost: results.append(sid))
    asyncio.run(r.run("capital of Portugal?", bypass=True))
    assert seen["partial"] is True
    assert [(s.kind, s.text) for s in steps] == [("partial", "Lis"), ("text", "Lisbon")]
    assert results == ["sid-9"]


# ---- the live row ----------------------------------------------------------------------------------------
def test_live_row_grows_then_the_finished_text_replaces_it():
    entries = []
    dv.add_step(entries, "user", text="count to 5")
    dv.set_live_text(entries, "1, 2")
    dv.set_live_text(entries, "1, 2, 3, 4")
    assert len(entries) == 2 and entries[-1]["live"] and dv.live_len(entries) == len("1, 2, 3, 4")
    dv.add_step(entries, "text", text="1, 2, 3, 4, 5")
    assert len(entries) == 2, "the answer was shown twice"
    assert entries[-1]["text"] == "1, 2, 3, 4, 5" and "live" not in entries[-1] and dv.live_len(entries) == -1
    dv.set_live_text(entries, "Now I'll")                       # next message streams in…
    dv.add_step(entries, "tool", tool="Bash", input={"command": "ls"}, tool_id="t1")   # …then a tool call
    assert entries[-2]["text"] == "Now I'll" and "live" not in entries[-2]
    dv.set_live_text(entries, "Stopped mid")                    # Stop mid-reply: it stays, as a normal row
    dv.end_live(entries)
    assert entries[-1]["text"] == "Stopped mid" and "live" not in entries[-1]


# ---- the page + the card ---------------------------------------------------------------------------------
class _StreamRunner:
    """Streams "1 2 3 … 40" as partial steps, then the finished text."""
    calls = []

    def __init__(self, cwd, on_step, on_approval, on_result):
        self.on_step, self.on_result = on_step, on_result
        self.allow_all = False

    async def run(self, prompt, resume=None, **kw):
        _StreamRunner.calls.append(prompt)
        words = []
        for i in range(1, 41):
            words.append(str(i))
            if i % 5 == 0:
                self.on_step(ar.Step("partial", text=" ".join(words)))
                await asyncio.sleep(0.08)
        self.on_step(ar.Step("text", text=" ".join(words) + " — done."))
        self.on_result(resume or "sid-s", 0.0)
        return "sid-s"

    async def interrupt(self):
        pass


class _MW:
    pass


def test_page_and_card_show_the_reply_while_it_streams(monkeypatch):
    monkeypatch.setattr(ar, "AgentRunner", _StreamRunner)
    page = ap.AssistantPage(_MW())
    page.show()                                               # (the page renders only while visible)
    try:
        page.run_task("count to 40")
        s = next(iter(page._sessions.values()))
        ov = page._ensure_notch()
        assert _wait(lambda: any(e.get("live") and "15" in e["text"] for e in s.entries), 5), \
            "no live row while it streams"
        cv = page.view.chat                                   # (the page's 60 ms repaint — a busy run: longer)
        assert _wait(lambda: bool(cv._turn_widgets) and cv._turn_widgets[-1].reply_browser is not None
                     and "15" in cv._turn_widgets[-1].reply_text(), 3), "the page shows nothing yet"
        w = cv._turn_widgets[-1]
        assert ov._state == "working" and any(ch.isdigit() for ch in ov._tool_chip.text()), ov._tool_chip.text()
        assert _wait(lambda: any(e.get("live") and "35" in e["text"] for e in s.entries), 5)
        assert _wait(lambda: "35" in cv._turn_widgets[-1].reply_text(), 3)
        assert cv._turn_widgets[-1] is w, "the turn was rebuilt for every word (flicker) — not updated in place"
        assert _wait(lambda: s.status == "done" and not s.running, 6)
        assert _wait(lambda: "done." in cv._turn_widgets[-1].reply_text(), 3)
        texts = [e for e in s.entries if e["kind"] == "text"]
        assert len(texts) == 1 and texts[0]["text"].endswith("— done.") and "live" not in texts[0]
        assert "done." in cv._turn_widgets[-1].reply_text()
        assert ov._state == "done"
    finally:
        page.shutdown()


def test_chat_view_finishes_a_streamed_reply_in_place():
    """The finished text replaces the live row without a structure change — the page must still show it."""
    from app.ui.agents_view import AgentsView
    v = AgentsView("light")
    v.resize(900, 700)
    v.show()
    entries = []
    dv.add_step(entries, "user", text="hi")
    dv.set_live_text(entries, "Hel")
    st = {"key": "s1", "entries": entries, "status": "working", "running": True, "approval": None,
          "last_action": "Writing the answer", "queued": None, "bypass": True, "error": "", "agent": "claude",
          "setup_kind": ""}
    v.show_chat(dict(st))
    _wait(lambda: False, 0.05)
    w = v.chat._turn_widgets[-1]
    assert "Hel" in w.reply_text()
    dv.add_step(entries, "text", text="Hello there")
    v.show_chat(dict(st))
    _wait(lambda: False, 0.05)
    assert v.chat._turn_widgets[-1] is w and "Hello there" in w.reply_text()
    v.close()


# ---- long replies stay cheap -------------------------------------------------------------------------------
_CHUNK = ("Here is the next part with **bold** and `code`.\n\n"
          "```python\ndef f(x):\n    return [i * x for i in range(100)]\n```\n\n- a point\n- another point\n\n")


def test_reply_chunks_cut_only_where_markdown_allows_and_stay_stable():
    text = _CHUNK * 120                                   # ~16 KB
    chunks = dv.reply_chunks(text)
    assert len(chunks) > 3 and all(len(c) <= dv.CHUNK_CHARS + 200 for c in chunks)
    for c in chunks:                                      # never a code fence cut in half
        assert sum(1 for ln in c.splitlines() if ln.lstrip().startswith("```")) % 2 == 0, c[:80]
    norm = lambda s: " ".join(s.split())
    assert norm(" ".join(chunks)) == norm(text), "text lost or changed by the cut"
    grown = dv.reply_chunks(text + "and the reply keeps going " * 20)
    assert grown[:len(chunks) - 1] == chunks[:-1], "earlier pieces changed as the reply grew (they'd re-layout)"
    assert dv.reply_chunks("short answer") == ["short answer"]
    big_code = "```js\n" + "\n".join(f"let v{i} = {i};" for i in range(900)) + "\n```"
    parts = dv.reply_chunks(big_code)
    assert len(parts) > 2 and all(p.startswith("```js") and p.rstrip().endswith("```") for p in parts)
    one_line = " ".join(str(i) for i in range(3000))      # "count to 3000" with spaces: one huge paragraph
    parts = dv.reply_chunks(one_line)
    assert len(parts) > 2 and norm(" ".join(parts)) == norm(one_line)


def test_streaming_a_long_reply_only_re_lays_out_its_last_piece(monkeypatch):
    from app.ui import agents_view as av
    calls = []
    orig = av._AutoBrowser.setHtml
    monkeypatch.setattr(av._AutoBrowser, "setHtml", lambda self, h: (calls.append(len(h)), orig(self, h)))
    v = av.AgentsView("light")
    v.resize(900, 700)
    v.show()
    entries = []
    dv.add_step(entries, "user", text="write a lot")
    st = {"key": "s1", "entries": entries, "status": "working", "running": True, "approval": None,
          "last_action": "Writing the answer", "queued": None, "bypass": True, "error": "", "agent": "claude",
          "setup_kind": ""}
    text = ""
    for _ in range(40):                                   # grows to ~16 KB
        text += _CHUNK * 3
        dv.set_live_text(entries, text)
        calls.clear()
        v.chat._live_hold = 0.0                           # (no slow-repaint pause: this counts pieces, not time —
        v.show_chat(dict(st))                             #  a loaded test run tripped it and skipped an update)
        app.processEvents()
        assert len(calls) <= 2, f"{len(calls)} pieces re-laid-out for one update (should be the last one or two)"
    w = v.chat._turn_widgets[-1]
    shown = " ".join(w.reply_text().split())
    assert len(w._pieces) > 3 and shown.startswith("Here is the next part with bold and code")
    assert shown.count("another point") == text.count("another point"), "pieces lost or repeated text"
    dv.add_step(entries, "text", text=text + "The end.")  # finished: in place, last piece only
    calls.clear()
    v.show_chat(dict(st))
    st2 = dict(st, status="done", running=False, last_action="")
    v.show_chat(st2)                                      # …and "done" doesn't rebuild the whole reply
    app.processEvents()
    assert v.chat._turn_widgets[-1] is w and "The end." in w.reply_text()
    assert len(calls) <= 3, f"finishing re-laid-out {len(calls)} pieces"
    v.close()
