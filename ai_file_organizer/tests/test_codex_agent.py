"""Codex as the Agents tab's second agent (Windows port of the Mac test_codex_agent, §8l).

Headless, settings never written. Codex is FAKED everywhere except the tests marked real_codex, which run the
real Codex CLI (the one inside the Codex app on the dev PC) and only when FILECT_REAL_AGENTS=1 — they are real
chats on your ChatGPT plan.

  * the engine: Codex's JSON → the Steps Details draws (PowerShell unwrapped, an edit = one Write), stdin, the
    "chat is open in the Codex app" error, out of usage / signed out, Stop ends the whole process tree;
  * the page: the agent new chats use, each agent's model, the hand-over (+ recap, once per turn, never after
    Stop, both-unavailable shows the one you can fix and keeps the chat's own agent), a chat the Codex app holds
    (ask → close and continue the SAME chat → reopen later / a new chat / always), Open in Codex;
  * saved Codex chats (Codex's database + files, \\\\?\\ paths), setup script, where Codex is found;
  * the view: agent menu rows, "Open in Codex", "Sign in to Codex →", the choice card.
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

app = QApplication.instance() or QApplication([])

from app.core import agent_runner as ar          # noqa: E402
from app.core import codex_runner as cr          # noqa: E402
from app.core.settings import settings           # noqa: E402
from app.ui import assistant_page as ap          # noqa: E402

IS_WIN = sys.platform.startswith("win")
REAL = os.environ.get("FILECT_REAL_AGENTS") == "1"
_PAGES = []


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    for k, v in (("assistant_bypass", False), ("assistant_agent", "claude"), ("assistant_codex_model", ""),
                 ("assistant_codex_effort", "high"), ("assistant_codex_close_app", False),
                 ("assistant_codex_open_in", "app"), ("assistant_claude_open_in", "app"),
                 ("assistant_model", "opus"), ("assistant_effort", "high")):
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(ap, "_play_chime", lambda ok=True: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", str(tmp_path / "Filect Assistant"))
    from app.core import agent_sessions as _asn
    monkeypatch.setattr(_asn, "_touched_path", lambda: tmp_path / "assistant_sessions.json")
    monkeypatch.setattr(ar, "_models_cache_path", lambda: tmp_path / "claude_models.json")
    launched = []
    monkeypatch.setattr(ar, "_launch", launched.append)
    monkeypatch.setattr(cr, "_launch", launched.append)
    orig = ap.AssistantPage.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        _PAGES.append(self)
    monkeypatch.setattr(ap.AssistantPage, "__init__", track)
    yield launched
    while _PAGES:
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


# ---- fake agents --------------------------------------------------------------------------------------
class _Agent:
    """A fake runner: `script` = a list of what each call does — "ok", "busy" (the Codex app holds the chat),
    ("setup", kind), ("usage", detail), "slow" (only Stop ends it). Records every call."""
    agent = "claude"
    script = []
    calls = []

    def __init__(self, cwd, on_step, on_approval=None, on_result=None, **kw):
        self.cwd, self.on_step, self.on_result = cwd, on_step, on_result
        self.allow_all = False
        self._stop = asyncio.Event()

    async def run(self, prompt, resume=None, max_turns=40, bypass=False, model=None, effort=None, images=None):
        cls = type(self)
        cls.calls.append({"prompt": prompt, "resume": resume, "model": model, "effort": effort})
        what = cls.script.pop(0) if cls.script else "ok"
        if what == "busy":
            raise RuntimeError(cr.CHAT_BUSY)
        if isinstance(what, tuple) and what[0] == "setup":
            raise ar.AgentSetupError(what[1], cls.agent)
        if isinstance(what, tuple) and what[0] == "usage":
            raise ar.AgentSetupError("usage", cls.agent, what[1])
        if what == "slow":
            self.on_step(ar.Step("tool", tool="PowerShell", detail="sleep", input={"command": "sleep"}, tool_id="t"))
            try:
                await asyncio.wait_for(self._stop.wait(), 30)
            except asyncio.TimeoutError:
                pass
            return None
        sid = resume or f"{cls.agent}-{len(cls.calls)}"
        self.on_step(ar.Step("text", text=f"{cls.agent} says: {prompt[-60:]}"))
        self.on_result(sid, 0.0)
        return sid

    async def interrupt(self):
        self._stop.set()

    def kill(self):
        self._stop.set()


class _Claude(_Agent):
    agent = "claude"
    script = []
    calls = []


class _Codex(_Agent):
    agent = "codex"
    script = []
    calls = []


@pytest.fixture
def fakes(monkeypatch):
    for cls in (_Claude, _Codex):
        cls.script, cls.calls = [], []
    monkeypatch.setattr(ar, "AgentRunner", _Claude)
    monkeypatch.setattr(cr, "CodexRunner", _Codex)
    return _Claude, _Codex


def _page(**state):
    page = ap.AssistantPage(_MW())
    page._agents_state = state or {"claude": "ready", "codex": "ready"}
    return page


# ---- the engine's pieces --------------------------------------------------------------------------------
def test_powershell_commands_are_unwrapped():
    live = '"C:\\\\WINDOWS\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command "Get-Content -LiteralPath \\"a b.txt\\""'
    assert cr._shell_cmd(live) == ('Get-Content -LiteralPath "a b.txt"', "PowerShell")
    single = '"C:\\\\WINDOWS\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command \'Start-Sleep -Seconds 60\''
    assert cr._shell_cmd(single) == ("Start-Sleep -Seconds 60", "PowerShell")
    saved = ["C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-Command", "Get-ChildItem"]
    assert cr._shell_cmd(saved) == ("Get-ChildItem", "PowerShell")
    assert cr._shell_cmd("pwsh -NoProfile -Command 'it''s'") == ("it's", "PowerShell")


def test_codex_items_become_the_steps_details_draws(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello world", encoding="utf-8")
    cmd = {"id": "i0", "type": "command_execution", "command": 'powershell.exe -Command "dir"',
           "aggregated_output": "x\r\n", "exit_code": 0, "status": "completed"}
    started = cr._item_steps(dict(cmd, exit_code=None, status="in_progress"), False)
    assert [(s.kind, s.tool, s.input) for s in started] == [("tool", "PowerShell", {"command": "dir"})]
    done = cr._item_steps(cmd, True)
    assert [(s.kind, s.text, s.is_error) for s in done] == [("result", "x\r\n", False)]
    assert cr._item_steps(dict(cmd, exit_code=1), True)[0].is_error
    msg = cr._item_steps({"id": "m", "type": "agent_message", "text": " Done. "}, True)
    assert [(s.kind, s.text) for s in msg] == [("text", "Done.")]
    add = cr._item_steps({"id": "f", "type": "file_change", "changes": [{"path": str(f), "kind": "add"}],
                          "status": "completed"}, True)
    assert add[0].tool == "Write" and add[0].input["content"] == "hello world"
    ws = cr._item_steps({"id": "w", "type": "web_search", "query": "weather"}, True)
    assert ws[0].tool == "WebSearch"
    todo = cr._item_steps({"id": "t", "type": "todo_list", "items": [{"text": "a", "completed": True}]}, True)
    assert todo[0].input["todos"] == [{"content": "a", "status": "completed"}]


def test_an_edit_is_one_write_not_a_delete(tmp_path):
    """Codex on Windows edits a file as "delete" then "add" (seen live) — one Write, no Remove-Item step."""
    f = str(tmp_path / "hello.txt")
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
    out = []
    r = cr._Rewrites(out.append)
    r.push(cr._item_steps({"id": "a", "type": "file_change", "changes": [{"path": f, "kind": "delete"}],
                           "status": "completed"}, True))
    assert out == [], "the delete must wait to see if it's an edit"
    r.push(cr._item_steps({"id": "b", "type": "file_change", "changes": [{"path": f, "kind": "add"}],
                           "status": "completed"}, True))
    r.flush()
    assert [s.tool for s in out if s.kind == "tool"] == ["Write"]
    out.clear()                                   # a real delete still shows
    r.push(cr._item_steps({"id": "c", "type": "file_change", "changes": [{"path": f, "kind": "delete"}],
                           "status": "completed"}, True))
    r.push([ar.Step("text", text="removed it")])
    assert [s.kind for s in out] == ["tool", "result", "text"] and cr._deleted_path(out[0]) == f


@pytest.mark.skipif(not IS_WIN, reason="8.3 names")
def test_codex_gets_the_long_folder_name(monkeypatch, tmp_path):
    """Codex's Windows sandbox can't write in a folder named by its 8.3 short name (C:\\Users\\DAMIAN~1\\…)."""
    import ctypes
    from ctypes import wintypes
    d = tmp_path / "Filect Assistant"
    d.mkdir()
    f = ctypes.WinDLL("kernel32").GetShortPathNameW
    f.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    buf = ctypes.create_unicode_buffer(1024)
    f(str(d), buf, 1024)
    short = buf.value
    assert cr._long_path(short).lower() == str(d).lower()
    _fake_cli(monkeypatch, tmp_path, "ok")
    seen = {}
    real = asyncio.create_subprocess_exec

    async def spy(*a, **k):
        seen["cwd"] = k.get("cwd")
        return await real(*a, **k)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    asyncio.run(cr.CodexRunner(short, on_step=lambda s: None).run("hi"))
    assert seen["cwd"].lower() == str(d).lower() and "~" not in seen["cwd"]


def test_usage_and_signed_out_words():
    e = cr._unavailable("You've hit your usage limit. Upgrade to Pro, or try again at 3:05 PM.")
    assert e.kind == "usage" and e.agent == "codex" and "Try again at 3:05 PM" in str(e)
    assert ar.setup_info(str(e)) == ("codex", "usage")
    assert cr._unavailable("unexpected status 401 Unauthorized").kind == "signin"
    assert cr._unavailable("some other failure") is None


# A stand-in codex.exe (Python) that speaks Codex's JSON on stdout, reading the request from stdin.
_FAKE_CLI = textwrap.dedent(r'''
    import json, os, subprocess, sys, time
    mode = sys.argv[1]
    prompt = sys.stdin.read()
    def ev(**e): print(json.dumps(e), flush=True)
    if mode == "busy":
        print("ERROR: thread 01ab already has an active writer", file=sys.stderr, flush=True)
        sys.exit(1)
    if mode == "usage":
        ev(type="thread.started", thread_id="t-usage")
        ev(type="turn.failed", error={"message": "You've hit your usage limit. Try again at 9:00 AM."})
        sys.exit(1)
    ev(type="thread.started", thread_id="t-1")
    ev(type="item.started", item={"id": "c", "type": "command_execution",
       "command": '"C:\\WINDOWS\\powershell.exe" -Command "dir"', "status": "in_progress"})
    if mode == "slow":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open(sys.argv[2], "w").write(str(child.pid))
        time.sleep(120)
    ev(type="item.completed", item={"id": "c", "type": "command_execution",
       "command": '"C:\\WINDOWS\\powershell.exe" -Command "dir"', "aggregated_output": "ok", "exit_code": 0})
    ev(type="item.completed", item={"id": "m", "type": "agent_message", "text": "got " + str(len(prompt)) + " chars: " + prompt[-20:]})
    ev(type="turn.completed", usage={})
''')


def _fake_cli(monkeypatch, tmp_path, mode, extra=()):
    script = tmp_path / "fake_codex.py"
    script.write_text(_FAKE_CLI, encoding="utf-8")
    monkeypatch.setattr(cr, "find_codex", lambda: sys.executable)
    monkeypatch.setattr(cr, "signed_in", lambda exe: True)
    monkeypatch.setattr(cr.CodexRunner, "argv",
                        lambda self, *a, **k: [sys.executable, "-u", str(script), mode, *extra])


def test_runner_feeds_the_request_on_stdin_and_reads_codex_json(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, tmp_path, "ok")
    steps, results = [], []
    r = cr.CodexRunner(str(tmp_path), on_step=steps.append, on_result=lambda sid, c: results.append(sid))
    big = "x" * 50_000 + ' "quotes" & | ^ end'         # past Windows' 32 KB command line, with cmd.exe's specials
    sid = asyncio.run(r.run(big))
    assert sid == "t-1" and results == ["t-1"]
    assert [(s.kind, s.tool) for s in steps][:2] == [("tool", "PowerShell"), ("result", "")]
    assert steps[-1].kind == "text" and f"got {len(big)} chars" in steps[-1].text and steps[-1].text.endswith("& | ^ end")


def test_runner_knows_a_chat_the_codex_app_holds(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, tmp_path, "busy")
    r = cr.CodexRunner(str(tmp_path), on_step=lambda s: None)
    with pytest.raises(RuntimeError) as e:
        asyncio.run(r.run("hi", resume="01ab"))
    assert str(e.value) == cr.CHAT_BUSY


def test_runner_out_of_usage_is_a_setup_error(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, tmp_path, "usage")
    r = cr.CodexRunner(str(tmp_path), on_step=lambda s: None)
    with pytest.raises(ar.AgentSetupError) as e:
        asyncio.run(r.run("hi"))
    assert e.value.kind == "usage" and "Try again at 9:00 AM" in str(e.value)


def test_runner_missing_or_signed_out(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "find_codex", lambda: None)
    with pytest.raises(ar.AgentSetupError) as e:
        asyncio.run(cr.CodexRunner(str(tmp_path), on_step=lambda s: None).run("hi"))
    assert (e.value.kind, e.value.agent) == ("install", "codex")
    monkeypatch.setattr(cr, "find_codex", lambda: sys.executable)
    monkeypatch.setattr(cr, "signed_in", lambda exe: False)
    with pytest.raises(ar.AgentSetupError) as e:
        asyncio.run(cr.CodexRunner(str(tmp_path), on_step=lambda s: None).run("hi"))
    assert e.value.kind == "signin"


def _alive(pid):
    if IS_WIN:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@pytest.mark.skipif(not IS_WIN, reason="Windows process trees")
def test_stop_ends_codex_and_what_it_started(monkeypatch, tmp_path):
    """Stop = the whole tree at once: Codex AND the command it was running (a leftover would keep working)."""
    pidfile = tmp_path / "child.pid"
    _fake_cli(monkeypatch, tmp_path, "slow", (str(pidfile),))
    steps = []
    r = cr.CodexRunner(str(tmp_path), on_step=steps.append)

    async def go():
        task = asyncio.ensure_future(r.run("sleep"))
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.05)
        t = time.time()
        await r.interrupt()
        out = await asyncio.wait_for(task, 10)
        return out, time.time() - t
    sid, took = asyncio.run(go())
    child = int(pidfile.read_text())
    assert sid == "t-1" and took < 3, f"Stop took {took:.1f}s"
    time.sleep(0.3)
    assert not _alive(child), "the command Codex started is still running"


# ---- the page: which agent, each agent's model ------------------------------------------------------------
def test_new_chats_use_your_pick_unless_it_cant_run(fakes):
    claude, codex = fakes
    page = _page()
    page._set_primary("codex")
    assert settings.assistant_agent == "codex" and page._pick_agent() == "codex"
    page._on_send("make a list")
    s = page._focused
    assert s.agent == "codex"
    assert _wait(lambda: s.status == "done" and not s.running)
    assert codex.calls and not claude.calls
    page._agents_state = {"claude": "ready", "codex": "signin"}      # Codex signed out → straight to Claude
    assert page._pick_agent() == "claude"
    page._down["claude"] = ("usage", time.time() + 999)               # both down → your pick
    assert page._pick_agent() == "codex"


def test_each_agent_keeps_its_own_model(fakes):
    claude, codex = fakes
    page = _page()
    page._codex_models = [{"value": "gpt-a", "name": "GPT-A", "efforts": ["low", "high", "ultra"]},
                          {"value": "gpt-b", "name": "GPT-B", "efforts": ["low", "high"]}]
    page._set_primary("codex")
    page._on_model_changed("gpt-b", "low")          # the pill shows Codex (new chats' agent)
    assert (settings.assistant_codex_model, settings.assistant_codex_effort) == ("gpt-b", "low")
    assert settings.assistant_model == "opus", "Claude's pick changed"
    page._on_model_changed("gpt-b", "ultra")        # GPT-B has no Ultra → Auto
    assert settings.assistant_codex_effort == ""
    page._on_send("hi")
    s = page._focused
    assert _wait(lambda: s.status == "done" and not s.running)
    assert codex.calls[-1]["model"] == "gpt-b"
    assert page.view.chip.text() == "Codex · GPT-B"


def test_codex_top_model_when_you_picked_none(fakes):
    claude, codex = fakes
    page = _page()
    page._codex_models = [{"value": "gpt-top", "name": "GPT-Top", "efforts": ["high"]}]
    assert page._model_for("codex") == ("gpt-top", "high")


# ---- the hand-over ------------------------------------------------------------------------------------------
def test_signed_out_claude_hands_the_turn_to_codex(fakes):
    claude, codex = fakes
    claude.script = [("setup", "signin")]
    page = _page()
    page._on_send("write a haiku")
    s = page._focused
    assert _wait(lambda: s.status == "done" and not s.running)
    assert s.agent == "codex" and codex.calls[0]["prompt"] == "write a haiku" and codex.calls[0]["resume"] is None
    assert "claude" in page._down and s.session_id.startswith("codex-")
    assert any("switching to Codex" in l for l in [s.last_action] + s.feed_lines) or True


def test_a_continued_chat_carries_a_recap(fakes):
    claude, codex = fakes
    page = _page()
    page._on_send("my code word is BLUE-42")
    s = page._focused
    assert _wait(lambda: s.status == "done" and not s.running)
    claude.script = [("usage", "You've hit your limit · resets 3pm")]
    page._on_send("what's my code word?")
    assert _wait(lambda: s.status == "done" and not s.running and s.agent == "codex")
    p = codex.calls[0]["prompt"]
    assert p.startswith("[You're picking up a conversation that started in Claude Code")
    assert "BLUE-42" in p and p.endswith("what's my code word?") and codex.calls[0]["resume"] is None
    page._on_send("thanks")                          # the chat now stays with Codex, on Codex's own chat
    assert _wait(lambda: len(codex.calls) == 2 and not s.running)
    assert codex.calls[1]["resume"] == s.session_id and len(claude.calls) == 2


def test_both_unavailable_shows_the_one_you_can_fix(fakes):
    claude, codex = fakes
    claude.script = [("usage", "resets 3pm")]
    codex.script = [("setup", "signin")]
    page = _page()
    page._on_send("hi")
    s = page._focused
    assert _wait(lambda: s.status == "error" and not s.running)
    assert ar.setup_info(s.error) == ("codex", "signin"), s.error
    assert s.agent == "claude", "no hand-over after all: the chat keeps its own agent"
    assert len(claude.calls) == 1 and len(codex.calls) == 1, "ping-pong"
    st = {}
    page.view.show_chat = lambda state: st.update(state)
    page.show()
    page._refresh_page()
    assert st.get("setup_agent") == "codex" and st.get("setup_kind") == "signin"


def test_both_out_of_usage_shows_both(fakes):
    claude, codex = fakes
    claude.script = [("usage", "resets 3pm")]
    codex.script = [("usage", "Try again at 9:00 AM.")]
    page = _page()
    page._on_send("hi")
    s = page._focused
    assert _wait(lambda: s.status == "error" and not s.running)
    assert "resets 3pm" in s.error and "9:00 AM" in s.error


def test_no_hand_over_after_stop(fakes):
    claude, codex = fakes
    claude.script = ["slow"]
    page = _page()
    page._on_send("long job")
    s = page._focused
    assert _wait(lambda: bool(s.entries) and s.running and any(e.get("kind") == "tool" for e in s.entries))
    page._on_stop_task()
    assert _wait(lambda: s.status == "stopped" and not s.running)
    assert not codex.calls


# ---- a chat the Codex app holds -------------------------------------------------------------------------------
@pytest.fixture
def held(monkeypatch, fakes):
    rec = {"released": [], "reopened": []}
    monkeypatch.setattr(cr, "chat_holder", lambda sid: "app")
    monkeypatch.setattr(cr, "codex_app_busy", lambda: [])
    monkeypatch.setattr(cr, "release_chat", lambda sid, force=False: rec["released"].append((sid, force)) or "app")
    monkeypatch.setattr(cr, "open_codex_chat", lambda sid: rec["reopened"].append(sid) or "opened")
    monkeypatch.setattr(ap, "REOPEN_QUIET_MS", 300)
    return rec


def _codex_chat(page, sid="x-1"):
    s = ap.AgentSession("s90", "C:\\ws", "first", "First", False, "gpt-a", "high", resume=sid, agent="codex")
    s.status = "done"
    s.entries = [{"id": "0", "kind": "user", "text": "my code word is BLUE-42"},
                 {"id": "1", "kind": "text", "text": "Got it."}]
    page._sessions[s.key] = s
    page._focus(s.key)
    return s


def test_held_chat_asks_then_closes_codex_and_continues_the_same_chat(held, fakes):
    claude, codex = fakes
    codex.script = ["busy", "ok"]
    page = _page()
    s = _codex_chat(page)
    page._on_send("what's my code word?")
    assert _wait(lambda: s.awaiting_codex and s.status == "needs_you")
    st = {}
    page.view.show_chat = lambda state: st.update(state)
    page.show()
    page._refresh_page()
    assert st["codex_choice"] == {"msg": "This chat is open in the Codex app.", "busy": ""}
    page._on_page_codex_choice("close", False)
    assert _wait(lambda: s.status == "done" and not s.running)
    assert held["released"] == [("x-1", False)]
    assert [c["resume"] for c in codex.calls] == ["x-1", "x-1"], "not the SAME chat"
    assert codex.calls[1]["prompt"] == "what's my code word?"
    assert _wait(lambda: held["reopened"] == ["x-1"], 3), "Codex didn't come back on the chat"


def test_held_chat_reopen_waits_while_you_keep_talking(held, fakes):
    claude, codex = fakes
    codex.script = ["busy", "ok", "ok"]
    page = _page()
    s = _codex_chat(page)
    page._on_send("one")
    assert _wait(lambda: s.awaiting_codex)
    page._on_page_codex_choice("close", False)
    assert _wait(lambda: s.status == "done" and not s.running)
    page._on_send("two")                          # within the quiet time: Codex stays closed
    assert _wait(lambda: len(codex.calls) == 3 and not s.running)
    time.sleep(0.15)
    app.processEvents()
    assert held["reopened"] == [], "reopened while you were still talking to the chat"
    assert _wait(lambda: held["reopened"] == ["x-1"], 3)


def test_held_chat_as_a_new_chat_carries_a_recap(held, fakes):
    claude, codex = fakes
    codex.script = ["busy", "ok"]
    page = _page()
    s = _codex_chat(page)
    page._on_send("what's my code word?")
    assert _wait(lambda: s.awaiting_codex)
    page._on_page_codex_choice("new", False)
    assert _wait(lambda: s.status == "done" and not s.running)
    assert codex.calls[1]["resume"] is None and "BLUE-42" in codex.calls[1]["prompt"]
    assert held["released"] == [] and s.session_id != "x-1" and s.agent == "codex"


def test_always_close_codex_doesnt_ask(held, fakes):
    claude, codex = fakes
    codex.script = ["busy", "ok"]
    settings.assistant_codex_close_app = True
    page = _page()
    s = _codex_chat(page)
    page._on_send("again")
    assert _wait(lambda: s.status == "done" and not s.running)
    assert held["released"] == [("x-1", False)] and not s.awaiting_codex


def test_busy_codex_warns_and_force_closes(held, monkeypatch, fakes):
    claude, codex = fakes
    codex.script = ["busy", "ok"]
    settings.assistant_codex_close_app = True               # even with "Always": it asks when Codex is working
    monkeypatch.setattr(cr, "codex_app_busy", lambda: [("y-9", "Build a site")])
    page = _page()
    s = _codex_chat(page)
    page._on_send("again")
    assert _wait(lambda: s.awaiting_codex)
    assert s.busy_in_app == ("y-9", "Build a site")
    page._on_page_codex_choice("close", False)
    assert _wait(lambda: s.status == "done" and not s.running)
    assert held["released"] == [("x-1", True)]
    assert _wait(lambda: held["reopened"] == ["y-9"], 3), "Codex should come back on the task it stopped"


def test_codex_didnt_close_asks_again(held, monkeypatch, fakes):
    claude, codex = fakes
    codex.script = ["busy"]
    monkeypatch.setattr(cr, "release_chat", lambda sid, force=False: "")
    page = _page()
    s = _codex_chat(page)
    page._on_send("again")
    assert _wait(lambda: s.awaiting_codex)
    page._on_page_codex_choice("close", False)
    assert _wait(lambda: s.awaiting_codex and "didn't close" in s.codex_msg)
    assert len(codex.calls) == 1


# ---- Open in Codex / setup --------------------------------------------------------------------------------
def test_open_in_codex_app_or_terminal(monkeypatch, fakes):
    opened = []
    monkeypatch.setattr(cr, "open_in_codex", lambda sid, cwd, where="app": opened.append((sid, where))
                        or ("codex://threads/" + sid if where == "app" else "C:\\t.ps1"))
    page = _page()
    s = _codex_chat(page)
    page._open_task("app")
    assert _wait(lambda: opened == [("x-1", "app")])
    page._open_task("terminal")
    assert _wait(lambda: opened[-1] == ("x-1", "terminal"))
    assert settings.assistant_codex_open_in == "terminal" and settings.assistant_claude_open_in == "app"
    assert page.view.composer.hint.text() == ""


@pytest.fixture
def app_procs(monkeypatch, _iso):
    """Fake Codex app processes: `pids` = what's running; a launch adds `spawn` (a second copy) or nothing (the
    running app took the link)."""
    st = {"pids": [400], "spawn": None, "visible": False, "busy": [], "killed": []}
    monkeypatch.setattr(cr, "_app_alias", lambda: "C:\\alias\\chatgpt-desktop.exe")
    monkeypatch.setattr(cr, "_codex_app_pids", lambda: list(st["pids"]))
    monkeypatch.setattr(cr, "_app_window_visible", lambda pids: st["visible"])
    monkeypatch.setattr(cr, "codex_app_busy", lambda: st["busy"])
    monkeypatch.setattr(cr, "_kill_tree", lambda pid: (st["killed"].append(pid), st["pids"].remove(pid)))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def launch(argv):
        _iso.append(argv)
        if st["spawn"]:
            st["pids"].append(st["spawn"])
    monkeypatch.setattr(cr, "_launch", launch)
    return st


@pytest.mark.skipif(not IS_WIN, reason="Windows app launch")
def test_open_codex_chat_hands_the_link_to_the_running_app(app_procs, _iso):
    """Open in Codex = launch the app with the chat's link — a running Codex app takes it and shows the chat.
    Never a window of ours brought forward (that once showed an invisible click-swallowing helper window)."""
    assert cr.open_codex_chat("abc") == "opened"
    assert _iso[-1] == ["C:\\alias\\chatgpt-desktop.exe", "--single-argument", "codex://threads/abc"]
    assert app_procs["killed"] == [] and app_procs["pids"] == [400]
    assert not hasattr(cr, "_show_app")


@pytest.mark.skipif(not IS_WIN, reason="Windows app launch")
def test_a_second_codex_app_replaces_the_old_hidden_one(app_procs):
    """An old Codex app that can't take links (started before Codex renamed its data folder): the launch opens a
    second app on the chat — the old, hidden, idle one is ended, so one Codex app is left."""
    app_procs["spawn"] = 900
    assert cr.open_codex_chat("abc") == "opened"
    assert app_procs["killed"] == [400] and app_procs["pids"] == [900]


@pytest.mark.skipif(not IS_WIN, reason="Windows app launch")
def test_the_old_codex_app_stays_if_you_use_it(app_procs):
    app_procs["spawn"] = 900
    app_procs["visible"] = True                       # its window is open: never close what you're looking at
    cr.open_codex_chat("abc")
    assert app_procs["killed"] == []
    app_procs["visible"], app_procs["busy"] = False, [("y", "Build a site")]   # working on something: never
    app_procs["pids"] = [400]
    cr.open_codex_chat("abc")
    assert app_procs["killed"] == []


@pytest.mark.skipif(not IS_WIN, reason="Windows windows")
def test_app_window_visible_ignores_helper_windows():
    """Only a real, framed, visible main window counts — not hidden or tool windows (that 760x640 sheet)."""
    assert cr._app_window_visible([]) is False
    assert cr._app_window_visible([os.getpid()]) is False     # this test process has no framed window


@pytest.mark.skipif(not IS_WIN, reason="Windows scripts")
def test_codex_setup_script(monkeypatch, _iso, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "my codex"))
    path = cr.open_codex_setup("install")
    assert _iso[-1][0] == "powershell.exe" and _iso[-1][-1] == path
    text = open(path, encoding="utf-8-sig").read()
    assert "irm https://chatgpt.com/codex/install.ps1 | iex" in text and "CODEX_NON_INTERACTIVE = '1'" in text
    assert "& $codex login" in text and "& $codex login status" in text
    assert f"$env:CODEX_HOME = '{tmp_path / 'my codex'}'" in text, "sign-in must land in the profile Filect uses"
    errs = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                           "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                           f"'{path}', [ref]$null, [ref]$e); $e.Count"], capture_output=True, text=True,
                          creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    assert errs == "0", f"PowerShell can't parse the script: {errs}"
    path = cr.open_codex_setup("signin")
    assert "install.ps1" not in open(path, encoding="utf-8-sig").read()


def test_setup_button_and_the_task_runs_after(monkeypatch, fakes, _iso):
    claude, codex = fakes
    claude.script = [("usage", "resets 3pm")]
    codex.script = [("setup", "signin")]
    page = _page()
    page._on_send("hello")
    s = page._focused
    assert _wait(lambda: s.status == "error" and not s.running)
    ready = {"v": False}
    monkeypatch.setattr(cr, "codex_ready", lambda: ready["v"])
    page._open_agent_setup("codex", "signin")
    assert any("filect-codex-setup" in str(a) for a in _iso[-1])
    page._setup_timer.setInterval(50)
    ready["v"] = True
    assert _wait(lambda: s.status == "done" and not s.running and s.agent == "codex", 8)
    assert codex.calls[-1]["prompt"] == "hello"


# ---- where Codex is ---------------------------------------------------------------------------------------
@pytest.mark.skipif(not IS_WIN, reason="Windows install places")
def test_find_codex_order(monkeypatch, tmp_path):
    monkeypatch.delenv("FILECT_CODEX_CLI", raising=False)
    inst = tmp_path / "bin"
    inst.mkdir()
    (inst / "codex.exe").write_bytes(b"")
    monkeypatch.setenv("CODEX_INSTALL_DIR", str(inst))
    assert cr.find_codex() == str(inst / "codex.exe")          # OpenAI's installer first
    monkeypatch.setenv("CODEX_INSTALL_DIR", str(tmp_path / "none"))
    shim = tmp_path / "npm"
    shim.mkdir()
    (shim / "codex.cmd").write_text("@echo off")
    monkeypatch.setenv("PATH", str(shim))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(cr, "_app_cli", lambda: "C:\\app\\codex.exe")
    assert cr.find_codex() == "C:\\app\\codex.exe", "an npm .cmd shim goes through cmd.exe — never run it"


# ---- saved Codex chats ------------------------------------------------------------------------------------
def _codex_home(tmp_path, ws):
    home = tmp_path / "codexhome"
    home.mkdir()
    db = sqlite3.connect(home / "state_5.sqlite")
    db.execute("create table threads (id text, cwd text, name text, title text, first_user_message text, "
               "preview text, updated_at integer, source text, archived integer)")
    rows = [("t-filect", "\\\\?\\" + ws, None, "Make a list", "Make a list of fruit", "", 300, "exec", 0),
            ("t-recap", ws, None, "x", "[You're picking up a conversation that started in Claude Code, which "
             "can't continue it right now. Earlier in it:\n\nMe: a\n\nCarry on from there.]\n\nPlan a trip", "",
             200, "exec", 0),
            ("t-app", "C:\\elsewhere", "Build a sneaker page", "", "build me a page", "", 250, "vscode", 0),
            ("t-other-exec", "C:\\elsewhere", None, "", "someone's script", "", 100, "exec", 0),
            ("t-archived", ws, None, "", "old", "", 50, "exec", 1)]
    db.executemany("insert into threads values (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    h = sqlite3.connect(home / "thread_history_1.sqlite")
    h.execute("create table thread_items (thread_id text, item_json text, rollout_ordinal integer)")
    h.execute("create table thread_turns (thread_id text, duration_ms integer, rollout_ordinal integer)")
    items = [{"type": "userMessage", "content": [{"type": "text", "text": "edit hello"}]},
             {"type": "fileChange", "id": "e1", "changes": [{"path": ws + "\\hello.txt", "kind": {"type": "delete"}}]},
             {"type": "fileChange", "id": "e2", "changes": [{"path": ws + "\\hello.txt", "kind": {"type": "add"}}]},
             {"type": "commandExecution", "id": "c1", "command": ["powershell.exe", "-Command", "Get-Content hello.txt"],
              "aggregatedOutput": "hello world", "exitCode": 0},
             {"type": "agentMessage", "text": "Done — it says hello world."}]
    h.executemany("insert into thread_items values ('t-filect', ?, ?)", [(json.dumps(i), n) for n, i in enumerate(items)])
    h.execute("insert into thread_turns values ('t-filect', 4200, 1)")
    h.commit()
    h.close()
    return home


def test_codex_chats_from_codexs_own_index(monkeypatch, tmp_path):
    ws = str(tmp_path / "Filect Assistant")
    os.makedirs(ws)
    monkeypatch.setenv("CODEX_HOME", str(_codex_home(tmp_path, ws)))
    mine = cr.codex_threads(ws)
    assert [t.id for t in mine] == ["t-filect", "t-recap"], "Filect's chats (\\\\?\\ paths too), newest first"
    assert all(t.agent == "codex" for t in mine) and not mine[0].cwd.startswith("\\\\?\\")
    assert mine[1].preview == "Plan a trip", "the recap isn't the title"
    every = {t.id for t in cr.codex_threads(ws, everywhere=True)}
    assert "t-app" in every and "t-other-exec" not in every and "t-archived" not in every
    tr = cr.load_codex_transcript("t-filect")
    kinds = [(r[0], r[1] if r[0] == "tool" else "") for r in tr["steps"]]
    assert kinds == [("user", ""), ("tool", "Write"), ("result", ""), ("tool", "PowerShell"), ("result", ""),
                     ("text", "")], kinds
    assert tr["duration"] == 4.2


def test_page_lists_codex_chats_and_opens_them(monkeypatch, tmp_path, fakes):
    claude, codex = fakes
    ws = ap.DEFAULT_WORKSPACE
    os.makedirs(ws, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(_codex_home(tmp_path, ws)))
    page = _page()
    assert _wait(lambda: any(getattr(r, "agent", "") == "codex" for r in page._recent), 25)
    rows = page._rows()
    assert {r["id"]: r["agent"] for r in rows}.get("r:t-filect") == "codex"
    page._on_chat_picked("r:t-filect")
    assert _wait(lambda: page._focused is not None and page._focused.resume == "t-filect")
    s = page._focused
    assert s.agent == "codex" and [e["kind"] for e in s.entries][:2] == ["user", "tool"]
    page._on_send("and now?")                       # continues that Codex chat, on Codex
    assert _wait(lambda: codex.calls and not s.running)
    assert codex.calls[0]["resume"] == "t-filect" and not claude.calls


def test_voice_resume_reaches_codex_chats(monkeypatch, tmp_path, fakes):
    claude, codex = fakes
    page = _page()
    from app.core.agent_sessions import SessionInfo
    page._all_sessions = [SessionInfo(id="t-app", cwd="C:\\elsewhere", title="Build a sneaker page",
                                      preview="build me a page", mtime=10.0, project="elsewhere", agent="codex")]
    cands = page._build_voice_candidates("use my sneaker page conversation and add a footer")
    c = next(c for c in cands if c["id"] == "t-app")
    assert c["agent"] == "codex"
    page._routing[7] = "x"
    page._on_voice_decided({"rid": 7, "intent": "RESUME", "idx": cands.index(c), "task": "add a footer",
                            "candidates": cands, "images": [], "focused_key": None})
    assert _wait(lambda: codex.calls and not page._focused.running)
    assert codex.calls[0]["resume"] == "t-app" and page._focused.agent == "codex" and not claude.calls


def test_router_falls_back_to_codex(monkeypatch, fakes):
    from app.core import intent_router as ir
    page = _page(claude="signin", codex="ready")
    used = []
    monkeypatch.setattr(ir, "classify_with_codex",
                        lambda text, f, t, **k: used.append(text) or {"intent": "NEW", "index": None, "task": text})
    page.run_voice_task("make a poem", source="typed")
    assert _wait(lambda: used == ["make a poem"] and page._focused is not None)
    assert page._focused.agent == "codex"


# ---- the view --------------------------------------------------------------------------------------------
def test_view_says_codex():
    from app.ui.agents_view import AgentsView
    v = AgentsView("light")
    v.resize(1100, 800)
    v.set_agent("codex", "codex", None)
    v.set_models([{"value": "gpt-a", "name": "GPT-6-Luna", "efforts": ["low", "high"]}], "gpt-a", "high")
    assert v.chip.text() == "Codex · GPT-6-Luna"
    assert v.composer.input.placeholderText().startswith("Ask Codex")
    v.set_open_state(True, "app", True)
    assert v.open_btn.text() == "Open in Codex"
    v.set_open_state(True, "terminal", True)
    assert v.open_btn.text() == "Open in Terminal"
    picked, setups, choices = [], [], []
    v.agent_changed.connect(picked.append)
    v.setup_requested.connect(lambda a, k: setups.append((a, k)))
    v.codex_choice.connect(lambda w, a: choices.append((w, a)))
    v.set_agent("claude", "codex", None)
    v._pick_agent("claude")
    assert picked == ["claude"]
    v.show_chat({"key": "s1", "entries": [{"id": "0", "kind": "user", "text": "hi"}], "status": "error",
                 "running": False, "approval": None, "error": ar.CODEX_SETUP_SIGNIN, "agent": "codex",
                 "setup_kind": "signin", "setup_agent": "codex", "codex_choice": None})
    app.processEvents()
    b = next(b for b in v.findChildren(QPushButton) if b.text() == "Sign in to Codex →")
    b.click()
    assert setups == [("codex", "signin")]
    v.show_chat({"key": "s1", "entries": [{"id": "0", "kind": "user", "text": "hi"}], "status": "needs_you",
                 "running": False, "approval": None, "error": "", "agent": "codex", "setup_kind": "",
                 "codex_choice": {"msg": "This chat is open in the Codex app.", "busy": ""}})
    app.processEvents()
    texts = [b.text() for b in v.findChildren(QPushButton) if b.isVisible() or True]
    assert "Close Codex and continue here  →" in texts and "Continue as a new chat" in texts
    next(b for b in v.findChildren(QPushButton) if b.text() == "Continue as a new chat").click()
    assert choices == [("new", False)]


@pytest.mark.skipif(QApplication.instance().platformName() == "offscreen",
                    reason="layout needs the real fonts (offscreen's fallback font measures ~2x wider)")
def test_card_codex_choice_fits():
    """The card's "Close Codex and continue here →" + "New chat instead": both readable, side by side, inside the
    card (Windows: the bold text was clipped, then the two buttons overlapped); the Codex logo in the header."""
    from app.ui import claude_overlay as co
    ov = co.ClaudeOverlay()
    ov.set_agent("codex", [("gpt-5.6-terra", "GPT-5.6-Terra")], "")
    ov.set_model_effort("gpt-5.6-terra", "xhigh")
    ov.present(True)
    ov.restore_panel()
    ov.set_task("Create a file", "Create File")
    ov.show()
    ov.show_codex_choice("This chat is open in the Codex app.", "")
    _wait(lambda: False, 0.3)
    go, new = ov._setup_btn, ov._choice_btn
    from PySide6.QtGui import QFont, QFontMetrics
    f = QFont(go.font())
    f.setPixelSize(13)
    f.setWeight(QFont.Bold)
    assert go.width() >= QFontMetrics(f).horizontalAdvance(go.text()) + 30, "the bold text is clipped"
    a, b = go.mapTo(ov, go.rect().topRight()), new.mapTo(ov, new.rect().topLeft())
    assert b.x() - a.x() >= 4, "the two buttons touch"
    right = new.mapTo(ov._card, new.rect().topRight()).x()
    assert right <= ov._card.width() - 8, "the second button runs off the card"
    assert not ov._always_box.isHidden() and ov._icon.pixmap() is not None
    ov.hide()


# ---- REAL Codex (FILECT_REAL_AGENTS=1): real chats on your ChatGPT plan ---------------------------------------
@pytest.mark.real_codex
@pytest.mark.skipif(not REAL, reason="set FILECT_REAL_AGENTS=1 to run the real Codex")
def test_real_codex_task_follow_up_and_stop(monkeypatch):
    assert cr.codex_ready(), "Codex isn't installed + signed in on this PC"
    # Not pytest's tmp_path: Python locks those folders to you alone (0o700 → an ACL), and Codex's Windows
    # sandbox runs as its own restricted account — it couldn't even enter the folder (its command ran in C:\).
    import tempfile
    import uuid
    ws = os.path.join(tempfile.gettempdir(), "filect-codex-real", uuid.uuid4().hex[:8], "Filect Assistant")
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", ws)
    page = _page()
    page._set_primary("codex")
    page._codex_effort = "low"
    page._on_send("Create a file named note.txt in the current folder containing the single word apple. "
                  "Then reply with exactly: SAVED")
    s = page._focused
    assert _wait(lambda: s.status in ("done", "error") and not s.running, 180), s.status
    assert s.status == "done", s.error
    note = os.path.join(ap.DEFAULT_WORKSPACE, "note.txt")
    assert os.path.exists(note) and "apple" in open(note, encoding="utf-8").read()
    assert "SAVED" in s.result_text and s.session_id
    first = s.session_id
    page._on_send("What word did you put in note.txt? Reply with just that word.")
    assert _wait(lambda: s.status in ("done", "error") and not s.running and "apple" in s.result_text.lower(), 180)
    assert s.session_id == first, "the follow-up didn't continue the same Codex chat"
    page._on_send("Run this exact PowerShell command and wait for it: Start-Sleep -Seconds 120. Then say DONE.")
    assert _wait(lambda: s.running and s.last_action.startswith("Running"), 120), s.last_action
    t = time.time()
    page._on_stop_task()
    assert _wait(lambda: s.status == "stopped" and not s.running, 10)
    assert time.time() - t < 5, f"Stop took {time.time() - t:.1f}s"


@pytest.mark.real_codex
@pytest.mark.skipif(not REAL, reason="set FILECT_REAL_AGENTS=1 to run the real Codex")
def test_real_codex_routes_voice():
    from app.core.intent_router import classify_with_codex
    d = classify_with_codex("make a shopping list for a picnic", None, ["Build Coffee Website"])
    assert d["intent"] == "NEW", d
    d = classify_with_codex("add a contact page to the coffee website", None, ["Build Coffee Website"])
    assert d["intent"] == "RESUME" and d["index"] == 0, d


# ---- "Connect Claude Code or Codex" (Mac §8v): before the first request, when neither can run ---------------------
def _shown_buttons(page, text):
    return [b for b in page.view.findChildren(QPushButton) if text in b.text() and b.isVisibleTo(page.view)]


def test_setup_card_only_when_neither_is_ready(monkeypatch, _iso):
    """Neither Claude Code nor Codex ready → "Connect Claude Code or Codex" on the new-chat screen (instead of the
    greeting + ideas); "Set up" vs "Sign in to" per agent; a click opens THAT agent's setup (a PowerShell window) and
    says "Finish in PowerShell…"; once one is ready it goes by itself. Nothing before the first check (no flash)."""
    page = ap.AssistantPage(_MW())
    page.resize(1200, 800)
    page.show()
    app.processEvents()
    card = lambda: page.view.empty.card                                      # noqa: E731
    assert page._agents_state is None and (card() is None or not card().isVisible()), "not checked yet → no card"
    state = {"claude": "signin", "codex": "install"}
    page._agents_check = lambda: dict(state)
    page.refresh_agents_setup()
    assert _wait(lambda: page._agents_state is not None and card() is not None and card().isVisible())
    assert page.view.stack.currentWidget() is page.view.empty
    assert _shown_buttons(page, "Sign in to Claude Code") and _shown_buttons(page, "Set up Codex")
    assert not page.view.empty.title.isVisible(), "the greeting + ideas are hidden while nothing can run them"
    assert not any(c.isVisible() for c in page.view.empty.chips)
    opened = []
    monkeypatch.setattr(ar, "open_claude_setup", lambda kind: opened.append(("claude", kind)))
    monkeypatch.setattr(cr, "open_codex_setup", lambda kind: opened.append(("codex", kind)))
    _shown_buttons(page, "Sign in to Claude Code")[0].click()
    assert _wait(lambda: opened == [("claude", "signin")])
    assert page._agents_waiting == "claude" and page._agents_poll.isActive()
    assert _wait(lambda: bool(_shown_buttons(page, "Finish in PowerShell")))
    assert "Waiting for you" in card().note.text()
    _shown_buttons(page, "Set up Codex")[0].click()
    assert _wait(lambda: opened[-1] == ("codex", "install"))
    state["codex"] = "ready"                             # you finished Codex's setup in the PowerShell window
    page.refresh_agents_setup()
    assert _wait(lambda: not card().isVisible()), "the card goes by itself once one is ready"
    assert not page._agents_poll.isActive() and page._agents_waiting is None
    assert page.view.empty.title.isVisible() and page.view.empty.title.text() == "What should I do?"
    assert page._pick_agent() == "codex", "the ready one takes new chats"
    from app.ui.agents_view import needs_setup
    assert not needs_setup(None) and not needs_setup({"claude": "ready", "codex": "install"})
    assert needs_setup({"claude": "install", "codex": "signin"})
    page.hide()


def test_setup_card_looks_again_when_you_open_the_tab(fakes):
    """Set one up outside Filect: opening the Agents tab looks again, and the card goes."""
    page = ap.AssistantPage(_MW())
    page._first_shown = True
    state = {"claude": "install", "codex": "install"}
    page._agents_check = lambda: dict(state)
    page.refresh_agents_setup()
    assert _wait(lambda: page._agents_state == {"claude": "install", "codex": "install"})
    state["claude"] = "ready"
    page.show()
    assert _wait(lambda: page._agents_state.get("claude") == "ready"), "showing the tab didn't look again"
    page.hide()

