"""Claude Code missing / signed out → plain words + one-click setup, and the waiting task runs BY ITSELF once
Claude Code is ready (Mac §8k + "the waiting task runs by itself after setup"). The terminal launch is
captured (nothing installs, no browser opens), the readiness check is simulated; the scripts Filect would run
are checked with PowerShell's own parser. Settings are never written."""
import os
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.core import agent_runner as ar     # noqa: E402
from app.core.settings import settings      # noqa: E402
from app.ui import assistant_page as ap     # noqa: E402

IS_WIN = sys.platform.startswith("win")


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_play_chime", lambda ok=True: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", str(tmp_path / "ws"))
    from app.core import agent_sessions as _asn          # never the real app-data files
    monkeypatch.setattr(_asn, "_touched_path", lambda: tmp_path / "assistant_sessions.json")
    from app.core import agent_runner as _ar
    monkeypatch.setattr(_ar, "_models_cache_path", lambda: tmp_path / "claude_models.json")
    launched = []
    monkeypatch.setattr(ar, "_launch", launched.append)
    yield launched


def _ps_parse_errors(path):
    """PowerShell's own parser on a script file → its syntax errors (empty = valid)."""
    cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{path}', [ref]$null, [ref]$e); $e | ForEach-Object {{ $_.Message }}")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    return out.stdout.strip()


@pytest.mark.skipif(not IS_WIN, reason="Windows scripts")
@pytest.mark.parametrize("kind", ["install", "signin"])
def test_setup_script_is_the_official_one_and_parses(_iso, kind):
    path = ar.open_claude_setup(kind)
    assert path.endswith(".ps1") and os.path.exists(path)
    argv = _iso[-1]
    assert argv[0] == "powershell.exe" and "-NoExit" in argv and argv[-1] == path, argv
    text = open(path, encoding="utf-8-sig").read()
    if kind == "install":
        assert "irm https://claude.ai/install.ps1 | iex" in text, "not Anthropic's official Windows installer"
    else:
        assert "install.ps1" not in text, "sign-in must not reinstall"
    assert "auth login --claudeai" in text and "auth status" in text
    assert "\\.local\\bin\\claude.exe" in text, "a fresh install isn't on PATH yet — the script must find it"
    assert _ps_parse_errors(path) == "", _ps_parse_errors(path)


@pytest.mark.skipif(not IS_WIN, reason="Windows scripts")
def test_setup_script_carries_a_custom_profile(_iso, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "my profile's dir"))
    path = ar.open_claude_setup("signin")
    text = open(path, encoding="utf-8-sig").read()
    assert "$env:CLAUDE_CONFIG_DIR = 'C:" in text or "$env:CLAUDE_CONFIG_DIR = '" in text
    assert "my profile''s dir" in text, "the quote in the path must be escaped for PowerShell"
    assert _ps_parse_errors(path) == ""


@pytest.mark.skipif(not IS_WIN, reason="Windows scripts")
def test_open_in_terminal_script(_iso, monkeypatch):
    monkeypatch.setattr(ar, "has_app_for", lambda url: False)          # no Claude app → terminal
    path = ar.open_in_claude("abc-123", r"C:\Users\x\Filect Assistant", "app")
    text = open(path, encoding="utf-8-sig").read()
    assert "Set-Location -LiteralPath 'C:\\Users\\x\\Filect Assistant'" in text
    assert "--resume 'abc-123'" in text
    assert _ps_parse_errors(path) == ""


def test_open_in_claude_app_uses_the_link(_iso, monkeypatch):
    monkeypatch.setattr(ar, "has_app_for", lambda url: True)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    url = ar.open_in_claude("abc-123", r"C:\x", "app")
    assert url == "claude://resume?session=abc-123" and _iso[-1] == ["open-url", url]


class _MW:
    pass


def _wait(cond, secs=6.0):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_waiting_task_runs_by_itself_after_sign_in(_iso, monkeypatch):
    """Signed out → card → you click "Sign in to Claude Code →" → once Claude Code reports signed in, the
    SAME request runs in the SAME chat — you don't send it again."""
    calls = []

    class _Runner:
        def __init__(self, cwd, on_step, on_approval, on_result):
            self.on_step, self.on_result = on_step, on_result
            self.allow_all = False

        async def run(self, prompt, **kw):
            calls.append(prompt)
            if len(calls) == 1:
                raise ar.AgentSetupError("signin")
            self.on_step(ar.Step("text", text="done after sign-in"))
            self.on_result("sid-ok", 0.0)

        async def interrupt(self):
            pass

    monkeypatch.setattr(ar, "AgentRunner", _Runner)
    ready = {"v": False}
    monkeypatch.setattr(ar, "claude_ready", lambda: ready["v"])
    page = ap.AssistantPage(_MW())
    try:
        page._on_send("make me a list")
        key = page._focused_key
        assert _wait(lambda: page._sessions[key].status == "error" and not page._sessions[key].running)
        s = page._sessions[key]
        assert page._setup_kind(s.error) == "signin"
        page._open_agent_setup("claude", "signin")       # the card's button
        assert _iso and _iso[-1][0] == "powershell.exe", "the sign-in window didn't open"
        page._setup_timer.setInterval(50)
        app.processEvents()
        time.sleep(0.3)
        app.processEvents()
        assert len(calls) == 1, "it ran again before Claude Code was ready"
        ready["v"] = True                                 # you finished signing in
        assert _wait(lambda: len(calls) == 2 and s.status == "done" and not s.running, 8)
        assert calls[1] == "make me a list", "not the same request"
        assert len(page._sessions) == 1 and s.session_id == "sid-ok"
    finally:
        page.shutdown()
