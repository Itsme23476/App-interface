"""Drive a local Claude Code agent as a saved, resumable session — with live steps and a
human Approve/Deny on each risky tool use.

This is the engine behind Filect's "Agents" feature (ported from the Mac app). It is deliberately
UI-agnostic: the caller passes three callbacks and the Qt layer adapts them to signals + an approval
card. Each task is a real Claude Code session stored under %USERPROFILE%\\.claude\\projects (the SAME
store Claude Code and the Claude app read), so tasks are automatically saved, listed, and resumable
there — pass `resume=<session_id>` to continue an existing chat.

Dependency: `claude-agent-sdk` (wraps the local `claude` CLI; uses the user's Claude Code login).

Windows notes (vs the Mac file):
  * Finding `claude`: the SDK checks its own bundled CLI (dev venv only — the installed app doesn't ship
    it), then PATH, then the native installer's %USERPROFILE%\\.local\\bin\\claude.exe. That last one
    matters: Anthropic's installer does NOT add that folder to PATH, so a just-installed Claude Code is
    found without a restart. `FILECT_CLAUDE_CLI` overrides it (tests).
  * Setup / sign-in / "Open in Terminal" run in a new PowerShell window instead of a .command script.
  * "Open in Claude" uses the Claude app's `claude://` link via the shell (the Claude app is an MSIX
    package that registers it — there's no HKCR key, so it's detected with AssocQueryString).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional, Set

logger = logging.getLogger(__name__)

IS_WIN = sys.platform.startswith("win")
LIVE_EVERY = 0.2      # the live reply ("partial" steps) goes to the UI at most 5×/s — smooth, and cheap to paint

# Read-only / safe tools run without interrupting the user. Everything else (Write, Edit, Bash,
# deletes, network side effects, MCP tools) raises an Approve/Deny card. Tunable per the product's
# safety posture. ponytail: allow-list the safe ones, gate the rest — simplest rule that's safe.
DEFAULT_AUTO_ALLOW: Set[str] = {
    "Read", "Glob", "Grep", "NotebookRead", "TodoWrite", "WebFetch", "WebSearch",
}

_login_path_done = False


def _home() -> Path:
    return Path(os.path.expanduser("~"))


# ---- your MCP tools inside Filect's tasks ----------------------------------------------------------------
# Filect runs the agent isolated from your own setup (setting_sources=[] — no hooks, plugins or global
# permissions) — but your TOOLS come along: "it should be able to use all the MCP tools my Claude and
# Codex have" (him, 2026-11-07). So Claude gets your MCP servers passed explicitly (and with them your
# claude.ai connectors).

def _claude_json() -> dict:
    try:
        return json.loads((_home() / ".claude.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _same_dir(a: str, b: str) -> bool:
    """Windows paths compare case-insensitively ("c:\\Users" == "C:\\Users"), with either slash."""
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except Exception:
        return False


def claude_mcp_servers(cwd: str = "") -> dict:
    """Your Claude Code MCP servers: ~/.claude.json user scope + this folder's local scope (minus the ones
    you turned off there). Passed with --mcp-config, so hooks / plugins / permissions still don't load.
    Measured on Mac: with at least one server, the claude.ai connectors (Gmail, Calendar, Notion…) load
    too; with none they don't — a known limit."""
    d = _claude_json()
    servers = dict(d.get("mcpServers") or {})
    if cwd:
        # Claude Code keys projects by the folder as it saw it ("C:\\Users\\…"); match it whatever the
        # case / slashes Filect has.
        proj = next((v for k, v in (d.get("projects") or {}).items() if _same_dir(k, cwd)), None)
        if isinstance(proj, dict):
            servers.update(proj.get("mcpServers") or {})
            for name in proj.get("disabledMcpServers") or []:
                servers.pop(name, None)
    return {k: v for k, v in servers.items() if isinstance(v, dict) and (v.get("command") or v.get("url"))}


def claude_connector_names() -> list:
    """The claude.ai connectors you've connected ("Gmail", "Google Calendar"…) — Claude Code's own record
    (~/.claude.json claudeAiMcpEverConnected). Names only (may include old ones)."""
    return [str(n).replace("claude.ai ", "", 1) for n in _claude_json().get("claudeAiMcpEverConnected") or []]


def codex_tool_names() -> list:
    """Your Codex MCP servers + enabled plugins (~/.codex/config.toml) — names only, for the note."""
    try:
        import tomllib
        d = tomllib.loads((Path(os.environ.get("CODEX_HOME") or _home() / ".codex") / "config.toml")
                          .read_text(encoding="utf-8"))
    except Exception:
        return []
    names = [k for k, v in (d.get("mcp_servers") or {}).items() if not (isinstance(v, dict) and v.get("enabled") is False)]
    names += [k.split("@", 1)[0] for k, v in (d.get("plugins") or {}).items()
              if isinstance(v, dict) and v.get("enabled", True) and k.split("@", 1)[0] not in names]
    return names


_CONNECT = {
    "claude": "in Claude: claude.ai → Settings → Connectors for apps like Gmail, Notion or Calendar, or "
              "`claude mcp add` in a terminal; a connected one that needs signing in → run /mcp in Claude Code",
    "codex": "in Codex: the Codex app → Settings → MCP servers or Plugins, or `codex mcp add` in a terminal",
}

_PLATFORM = "Windows PC" if IS_WIN else "Mac"


def _codex_available() -> bool:
    """Codex as Filect's second agent (codex_runner) — tell the agent about it only when that's there."""
    try:
        import importlib.util
        return importlib.util.find_spec("app.core.codex_runner") is not None
    except Exception:
        return False


def tools_note(agent: str, cwd: str = "") -> str:
    """How the agent should answer when a task needs an app it has no tool for: say which app, and where
    to connect it — and whether Filect's OTHER agent has it. (Claude: the system prompt — the SDK sends an
    EMPTY system prompt when none is given, so this IS the system prompt.)"""
    _N = AGENT_NAMES
    me = "codex" if agent == "codex" else "claude"
    other = "claude" if me == "codex" else "codex"
    s = (f"You are {_N[me]}, running inside Filect (the user's {_PLATFORM} assistant). If a task needs an app or "
         "account you have no tool for — or a tool that says it needs signing in or isn't connected — don't ask "
         "for passwords or API keys and don't pretend: say plainly, in one short sentence, which app it is and "
         f"how to connect it ({_CONNECT[me]}).")
    if other == "codex" and not _codex_available():
        return s
    if other == "claude":
        has = sorted(claude_mcp_servers(cwd))
        if has:                                     # (the connectors only load alongside a server)
            has += [n for n in claude_connector_names() if n not in has] or ["your claude.ai connectors"]
    else:
        has = codex_tool_names()
    if has:
        s += (f" Filect can also run {_N[other]}, which has: {', '.join(has[:24])}. If one of those is the app "
              f"they need, say so — they can switch Filect's agent to {_N[other]} in the Agents tab.")
    return s


_no_window_done = False


def hide_cli_windows():
    """Windows: start `claude.exe` WITHOUT a console window. Filect is a windowed app (no console), so a
    console program it starts gets a brand-new console — on Windows 11 a black Windows Terminal tab titled
    "claude" popped up for every task (and for the SDK's `claude -v` check). The SDK starts it with
    anyio.open_process and offers no flag for this, so give ITS module (only) an anyio whose open_process
    adds CREATE_NO_WINDOW. The agent's own commands inherit the hidden console, so they stay hidden too."""
    global _no_window_done
    if _no_window_done or not IS_WIN:
        return
    try:
        import subprocess
        import anyio as _anyio
        from claude_agent_sdk._internal.transport import subprocess_cli as _sc

        class _AnyioNoWindow:
            _filect_no_window = True

            def __getattr__(self, name):
                return getattr(_anyio, name)

            async def open_process(self, *a, **kw):
                kw["creationflags"] = int(kw.get("creationflags") or 0) | subprocess.CREATE_NO_WINDOW
                return await _anyio.open_process(*a, **kw)

        if not getattr(type(_sc.anyio), "_filect_no_window", False):
            _sc.anyio = _AnyioNoWindow()
        _no_window_done = True
    except Exception as e:                          # never block a task over this
        logger.warning("[agent] couldn't hide claude's console window: %s", e)


def use_login_path():
    """Make sure the tools `claude` (and the agent's own commands) need are on PATH, once, before launching
    `claude`. Mac: a Finder-launched app only gets a bare PATH, so it adopts the login shell's. Windows: a
    Start-menu app gets the user's normal PATH already; what's missing is Claude Code's native install dir
    (%USERPROFILE%\\.local\\bin), which Anthropic's installer doesn't add to PATH — add it (and npm's
    global bin) so a fresh install works without restarting Filect."""
    global _login_path_done
    hide_cli_windows()                  # (every launch path calls this first)
    if _login_path_done:
        return
    _login_path_done = True
    if IS_WIN:
        home = str(_home())
        extra = [os.path.join(home, ".local", "bin")]
        if os.environ.get("APPDATA"):
            extra.append(os.path.join(os.environ["APPDATA"], "npm"))
        current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        have = {os.path.normcase(os.path.normpath(p)) for p in current}
        add = [p for p in extra if os.path.normcase(os.path.normpath(p)) not in have]
        if add:
            os.environ["PATH"] = os.pathsep.join(current + add)
        logger.info("[agent] PATH for claude: +%d dir(s) %s", len(add), add)
        return
    import pwd
    import subprocess
    home = os.path.expanduser("~")
    entries = []
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/zsh"
        out = subprocess.run([shell, "-ilc", 'printf "\\n__FILECT_PATH__%s\\n" "$PATH"'],
                             capture_output=True, text=True, timeout=5,
                             stdin=subprocess.DEVNULL).stdout
        line = next((l for l in out.splitlines() if l.startswith("__FILECT_PATH__")), "")
        entries = line[len("__FILECT_PATH__"):].split(":") if line else []
    except Exception as e:
        logger.warning(f"[agent] login PATH unavailable ({e}); using common locations")
    entries += [f"{home}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    current = os.environ.get("PATH", "").split(":")
    merged = list(dict.fromkeys(p for p in entries + current if p))
    os.environ["PATH"] = ":".join(merged)
    logger.info(f"[agent] PATH for claude: {len(merged)} dirs (login shell gave {len(entries) - 3})")


def claude_cli_override() -> Optional[str]:
    """`FILECT_CLAUDE_CLI` = an explicit claude.exe to drive (tests, or a custom install). None → the SDK's
    own search (bundled → PATH → %USERPROFILE%\\.local\\bin\\claude.exe)."""
    v = (os.environ.get("FILECT_CLAUDE_CLI") or "").strip()
    return v or None


def find_claude() -> Optional[str]:
    """The `claude` Filect would launch — the SDK's own search order — or None if there's none."""
    use_login_path()
    if claude_cli_override():
        p = claude_cli_override()
        return p if os.path.exists(p) else None
    try:
        from claude_agent_sdk import ClaudeAgentOptions
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
        t = SubprocessCLITransport(prompt="", options=ClaudeAgentOptions())
        return t._find_cli()
    except Exception:
        return None


@dataclass
class Step:
    """One live update to show in the task's tile."""
    kind: str            # "text" | "tool" | "result" | "partial" (the reply so far, while Claude writes it)
    text: str = ""       # assistant text (kind="text"/"partial") / the tool's output (kind="result")
    tool: str = ""       # tool name (kind="tool")
    detail: str = ""     # the tool's key target — file path, command, url — for a readable line
    input: dict = field(default_factory=dict)   # the tool call's full input (file content, edit, todos…)
    tool_id: str = ""    # pairs a "result" with its "tool" step
    is_error: bool = False   # kind="result": the tool failed


def _tool_detail(name: str, inp: dict) -> str:
    """A short human-readable target for a tool call, so the feed reads 'Writing index.html'
    instead of 'Using Write'. (The full inputs/outputs live in the real Claude Code conversation.)"""
    inp = inp or {}
    if name in ("Write", "Edit", "MultiEdit"):
        return os.path.basename(str(inp.get("file_path", "")))
    if name == "NotebookEdit":
        return os.path.basename(str(inp.get("notebook_path", "")))
    if name in ("Bash", "PowerShell"):
        return str(inp.get("command", ""))[:90]
    if name in ("Read", "Glob"):
        return os.path.basename(str(inp.get("file_path", "") or inp.get("pattern", "")))
    if name == "Grep":
        return str(inp.get("pattern", ""))[:60]
    if name == "WebFetch":
        return str(inp.get("url", ""))[:70]
    if name in ("Task", "Agent", "TodoWrite"):
        return str(inp.get("description", ""))[:60]
    return ""


def wait_for_file(path: str, timeout: float = 5.0) -> bool:
    """screen_ink writes a screenshot's PNG in the background; it appears atomically when complete."""
    import time
    deadline = time.time() + timeout
    while not os.path.exists(path) and time.time() < deadline:
        time.sleep(0.05)
    return os.path.exists(path)


def _image_block(path: str) -> Optional[dict]:
    """A screenshot as an image content block for Claude: downscaled to 1568px on the long edge
    (Claude's sweet spot; full resolution stays on disk) and PNG-encoded so red marks stay crisp —
    JPEG only if the PNG is still too big for the API's per-image limit."""
    import base64
    try:
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtGui import QImage
    except Exception:
        return None
    wait_for_file(path)
    img = QImage(path)                       # QImage (not QPixmap) is fine off the UI thread
    if img.isNull():
        logger.warning("[agent] screenshot unreadable: %s", path)
        return None
    if max(img.width(), img.height()) > 1568:
        img = img.scaled(1568, 1568, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _enc(fmt, *q):
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, fmt, *q)
        return bytes(buf.data())

    data, media = _enc("PNG"), "image/png"
    if len(data) > 3_500_000:
        data, media = _enc("JPEG", 85), "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(data).decode()}}


def user_message(prompt: str, images) -> dict:
    """The user turn for a task with screenshots: the images, then your words + a note saying what they
    are and where each is saved ("[screenshot: …]" lines — Claude can reuse the file, and the chat
    shows them as thumbnails when it's reopened)."""
    imgs = [b for b in (_image_block(p) for p in images) if b]
    if len(imgs) == 1:
        blocks = imgs
    else:
        # several (receive mode): label each image so "[screenshot 2]" in my words points at it
        blocks = [x for i, b in enumerate(imgs, 1) for x in ({"type": "text", "text": f"[screenshot {i}]"}, b)]
    text = screenshot_text(prompt, images, len(imgs))
    return {"type": "user", "message": {"role": "user", "content": blocks + [{"type": "text", "text": text}]},
            "parent_tool_use_id": None}


def screenshot_text(prompt: str, images, n: int) -> str:
    """Your words + what the n attached screenshots are and where each is saved (shared with Codex)."""
    if n == 1:
        note = "Screenshot of my screen attached"
    else:
        note = (f"{n} screenshots of my screen attached, in order — [screenshot N] in my words marks "
                "where I took each")
    note += " — anything marked in red is what I'm pointing at."
    return f"{prompt}\n\n({note})\n" + "\n".join(f"[screenshot: {p}]" for p in images)


def _steps_from_message(msg) -> list:
    """Steps for one SDK message: Claude's text + tool calls (AssistantMessage), and the tool results
    the engine feeds back (UserMessage) — so the page can show whether a write/command worked and what
    it printed. Thinking and the echoed prompt are skipped."""
    from claude_agent_sdk import AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock
    from app.core.agent_sessions import tool_result_text
    out = []
    if isinstance(msg, AssistantMessage):
        for b in msg.content:
            if isinstance(b, TextBlock) and b.text.strip():
                out.append(Step("text", text=b.text.strip()))
            elif isinstance(b, ToolUseBlock):
                inp = getattr(b, "input", None) or {}
                out.append(Step("tool", tool=b.name, detail=_tool_detail(b.name, inp), input=inp, tool_id=b.id))
    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
        for b in msg.content:
            if isinstance(b, ToolResultBlock):
                out.append(Step("result", tool_id=b.tool_use_id, text=tool_result_text(b.content),
                                is_error=bool(b.is_error)))
    return out


@dataclass
class ApprovalRequest:
    """What the agent wants to do — rendered as the "needs your OK" card."""
    tool: str
    tool_input: dict = field(default_factory=dict)


# Setup problems in plain words. They are not task failures: the page shows them with a one-click fix
# (open_claude_setup) instead of a raw "Claude Code not found at: …" or a fake "Done: Not logged in".
SETUP_INSTALL = (f"Claude Code isn't installed on this {'PC' if IS_WIN else 'Mac'} yet. The Assistant runs on "
                 "Claude Code with your Claude plan. Set it up once (about a minute) and your task runs right after.")
SETUP_SIGNIN = ("Claude Code isn't signed in yet. Sign in once with your Claude account and your task "
                "runs right after.")
CODEX_SETUP_INSTALL = (f"Codex isn't installed on this {'PC' if IS_WIN else 'Mac'} yet. The Assistant can run on "
                       "Codex with your ChatGPT plan. Set it up once (about a minute) and your task runs right after.")
CODEX_SETUP_SIGNIN = ("Codex isn't signed in yet. Sign in once with your ChatGPT account and your task "
                      "runs right after.")
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}
_SETUP = {("claude", "install"): SETUP_INSTALL, ("claude", "signin"): SETUP_SIGNIN,
          ("codex", "install"): CODEX_SETUP_INSTALL, ("codex", "signin"): CODEX_SETUP_SIGNIN}


def usage_message(agent: str, detail: str = "") -> str:
    """Out of usage on the plan — the agent's own words carry when it resets."""
    return f"{AGENT_NAMES[agent]} is out of usage for now. {(detail or '').strip()}".strip()


class WarmStartGone(Exception):
    """A warm spare's Claude Code ended (crashed / was ended) before it replied to its request. Not the user's
    problem: AgentWorker runs the same request on a fresh Claude Code (or, with no request yet, just ends)."""
    def __init__(self, code=None):
        self.code = code
        super().__init__(f"the warm Claude Code had ended (exit code {code})")


def _cli_exit_code(client) -> Optional[int]:
    """Claude Code's exit code once its process has ended; None while it runs (or if that can't be told).
    Reads the SDK's live transport -- its internals, guarded (same as AgentWorker.kill_process)."""
    try:
        proc = client._transport._process
        return None if proc is None else proc.returncode
    except Exception:
        return None


class AgentSetupError(Exception):
    """The agent can't run right now: missing ("install"), signed out ("signin") or out of usage on the
    plan ("usage"). str() is the friendly message."""
    def __init__(self, kind: str, agent: str = "claude", detail: str = ""):
        self.kind, self.agent = kind, agent
        super().__init__(usage_message(agent, detail) if kind == "usage" else _SETUP[(agent, kind)])


def setup_info(message: str):
    """(agent, kind) for an AgentSetupError's message, else None (a real task error)."""
    m = (message or "").strip()
    for key, text in _SETUP.items():
        if m == text:
            return key
    for agent, name in AGENT_NAMES.items():
        if m.startswith(f"{name} is out of usage"):
            return agent, "usage"
    return None


def setup_kind(message: str) -> str:
    """"install" / "signin" / "usage" for a setup message, else "" (a real task error)."""
    info = setup_info(message)
    return info[1] if info else ""


# ---- terminal windows (setup, "Open in Terminal") ----------------------------------------------------------
_launch = None      # tests capture launches here instead of opening a window: fn(argv) -> None


def _ps_quote(s: str) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def _open_window(argv: list) -> None:
    if _launch is not None:
        _launch(argv)
        return
    import subprocess
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if IS_WIN else 0
    subprocess.Popen(argv, creationflags=flags, close_fds=True)


def open_terminal(name: str, lines: list, env_keys=()) -> str:
    """Run a small script in a new terminal window (setup, "Open in … → Terminal"). `env_keys`: a custom
    profile folder (CLAUDE_CONFIG_DIR / CODEX_HOME) the script must use too. Returns the script's path.
    Windows: a PowerShell script, opened with -NoExit so its last words stay readable."""
    import tempfile
    if IS_WIN:
        name = os.path.splitext(name)[0] + ".ps1"
        path = os.path.join(tempfile.gettempdir(), name)
        head = ["$ErrorActionPreference = 'Continue'",
                "$env:Path = \"$env:USERPROFILE\\.local\\bin;$env:Path\""]
        head += [f"$env:{k} = {_ps_quote(os.environ[k])}" for k in env_keys if os.environ.get(k)]
        with open(path, "w", encoding="utf-8-sig") as f:        # BOM: PowerShell 5 reads it as UTF-8
            f.write("\r\n".join(head + lines) + "\r\n")
        _open_window(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", path])
        return path
    import shlex
    import subprocess
    path = os.path.join(tempfile.gettempdir(), name)
    head = ["#!/bin/bash", 'export PATH="$HOME/.local/bin:$PATH"']
    head += [f"export {k}={shlex.quote(os.environ[k])}" for k in env_keys if os.environ.get(k)]
    with open(path, "w") as f:
        f.write("\n".join(head + lines) + "\n")
    os.chmod(path, 0o755)
    subprocess.Popen(["open", "-a", "Terminal", path])
    return path


def open_claude_setup(kind: str) -> str:
    """Open a terminal running Anthropic's OFFICIAL installer (only if it's missing), then `claude auth login`
    to sign in with your Claude account, then a plain "all set" — every step visible. Returns the path.
    Windows installer (code.claude.com/docs/en/setup, PowerShell): `irm https://claude.ai/install.ps1 | iex`.
    It puts claude.exe in %USERPROFILE%\\.local\\bin and does NOT add that to PATH — the script puts it on
    PATH for itself, and Filect finds it there directly (no restart)."""
    if IS_WIN:
        lines = ["$Host.UI.RawUI.WindowTitle = 'Filect - setting up Claude Code'", "Clear-Host",
                 "Write-Host 'Filect - setting up Claude Code'", "Write-Host ''"]
        if kind == "install":
            lines += ["Write-Host \"Installing Claude Code with Anthropic's official installer (claude.ai/install.ps1)...\"",
                      "Write-Host 'This shows no progress while it downloads - give it a minute.'",
                      "try { irm https://claude.ai/install.ps1 | iex } catch { Write-Host ''; "
                      "Write-Host 'The install did not finish. See above.'; return }",
                      "Write-Host ''"]
        # `claude auth login` signs in and then EXITS (plain `claude` takes over the window and leaves people
        # inside Claude Code). Then confirm it worked, in plain words.
        lines += ["$claude = \"$env:USERPROFILE\\.local\\bin\\claude.exe\"",
                  "if (-not (Test-Path $claude)) { $claude = 'claude' }",
                  "Write-Host 'Signing in with your Claude account. A browser window will open.'", "Write-Host ''",
                  "& $claude auth login --claudeai", "Write-Host ''",
                  "$status = (& $claude auth status 2>$null | Out-String)",
                  "if ($status -match '\"loggedIn\":\\s*true') {",
                  "  Write-Host 'All set. Filect is running your task now. You can close this window.' -ForegroundColor Green",
                  "} else {",
                  "  Write-Host \"Sign-in didn't finish. Click the button in Filect to try again.\" -ForegroundColor Yellow",
                  "}"]
        path = open_terminal("filect-claude-setup.ps1", lines, ("CLAUDE_CONFIG_DIR",))
        logger.info("[agent] opened Claude Code setup (%s)", kind)
        return path
    import subprocess
    import tempfile
    lines = ["#!/bin/bash", "clear", 'echo "Filect · setting up Claude Code"', "echo"]
    if kind == "install":
        lines += ['echo "Installing Claude Code with Anthropic\'s official installer (claude.ai/install.sh)…"',
                  "curl -fsSL https://claude.ai/install.sh | bash || "
                  '{ echo; echo "The install did not finish. See above."; read -n 1; exit 1; }', "echo"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        import shlex
        lines.append(f"export CLAUDE_CONFIG_DIR={shlex.quote(os.environ['CLAUDE_CONFIG_DIR'])}")
    lines += ['export PATH="$HOME/.local/bin:$PATH"',
              'echo "Signing in with your Claude account. A browser window will open."', "echo",
              "claude auth login --claudeai", "echo",
              "if claude auth status 2>/dev/null | grep -q '\"loggedIn\": true'; then",
              '  echo "✓ All set. Filect is running your task now. You can close this window."',
              "else",
              '  echo "Sign-in didn\'t finish. Click the button in Filect to try again."',
              "fi"]
    path = os.path.join(tempfile.gettempdir(), "filect-claude-setup.command")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
    subprocess.Popen(["open", "-a", "Terminal", path])
    logger.info("[agent] opened Claude Code setup (%s)", kind)
    return path


def has_app_for(url: str) -> bool:
    """Is an app installed that opens this kind of link (claude:// → the Claude app)? Windows: ask the shell
    (AssocQueryString) — the Claude app is an MSIX package, so its link isn't a plain HKCR key; an
    unclaimed scheme answers "Pick an app" (OpenWith), which doesn't count."""
    scheme = url.split(":", 1)[0]
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            f = ctypes.windll.shlwapi.AssocQueryStringW
            f.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            n = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(512)
            if f(0x1000, 20, scheme, "open", buf, ctypes.byref(n)) != 0:   # ASSOCF_IS_PROTOCOL, ASSOCSTR_PROGID
                return False
            return bool(buf.value) and buf.value.lower() != "undecided"
        except Exception:
            return False
    try:
        from AppKit import NSWorkspace
        from Foundation import NSURL
        return NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(NSURL.URLWithString_(url)) is not None
    except Exception:
        return False


def _open_url(url: str) -> None:
    if _launch is not None:
        _launch(["open-url", url])
        return
    if IS_WIN:
        os.startfile(url)
    else:
        import subprocess
        subprocess.Popen(["open", url])


def open_in_claude(session_id: str, cwd: str, where: str = "app") -> str:
    """"Open in Claude": the Claude desktop app (claude://resume — it imports the chat), or `where` =
    "terminal" — Claude Code in a terminal, in the chat's folder, on the SAME chat. No Claude app (or a
    custom profile it doesn't use) → terminal, instead of silently doing nothing."""
    if where == "app" and has_app_for("claude://resume") and not os.environ.get("CLAUDE_CONFIG_DIR"):
        url = f"claude://resume?session={session_id}"
        _open_url(url)
        return url
    logger.info("[agent] opening %s in a terminal", session_id)
    folder = cwd or os.path.expanduser("~")
    if IS_WIN:
        return open_terminal("filect-open-claude.ps1",
                             ["$Host.UI.RawUI.WindowTitle = 'Claude Code'",
                              f"Set-Location -LiteralPath {_ps_quote(folder)}",
                              "$claude = \"$env:USERPROFILE\\.local\\bin\\claude.exe\"",
                              "if (-not (Test-Path $claude)) { $claude = 'claude' }",
                              f"& $claude --resume {_ps_quote(session_id)}"], ("CLAUDE_CONFIG_DIR",))
    import shlex
    return open_terminal("filect-open-claude.command",
                         [f"cd {shlex.quote(folder)} || exit 1",
                          f"exec claude --resume {shlex.quote(session_id)}"], ("CLAUDE_CONFIG_DIR",))


def claude_setup_state() -> str:
    """"ready" (installed AND signed in), "install" or "signin" — for picking the agent a new chat uses."""
    if not find_claude():
        return "install"
    return "ready" if claude_ready() else "signin"


def claude_ready() -> bool:
    """Claude Code installed AND signed in (`claude auth status`, ~0.2s) — so a task waiting on its setup
    can run by itself the moment you finish (see AssistantPage._check_setup)."""
    import subprocess
    exe = find_claude()
    if not exe:
        return False
    try:
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
        out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", **kw).stdout
        return bool(json.loads(out).get("loggedIn"))
    except Exception:
        return False


# ---- Claude's real model list ---------------------------------------------------------------------------
# The model menu shows what Claude Code ITSELF offers this account (Opus 5.5 · Fable 5.1 · Sonnet 5.5 ·
# Haiku 5.5 + older versions) — never a hard-coded guess. Reading it starts Claude Code and asks for its
# server info: no message is sent, no tokens, no transcript (~2-5s), so it runs off the UI thread and is
# cached for the next start.
FALLBACK_CLAUDE_MODELS = [
    {"value": "opus", "name": "Opus", "efforts": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "sonnet", "name": "Sonnet", "efforts": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "haiku", "name": "Haiku", "efforts": []},
]


def _models_cache_path() -> Path:
    from app.core.settings import settings
    return settings.get_app_data_dir() / "claude_models.json"


def cached_claude_models() -> list:
    """The last list Claude Code reported (instant), else a small fallback."""
    try:
        data = json.loads(_models_cache_path().read_text(encoding="utf-8"))
        if isinstance(data, list) and data and all(isinstance(m, dict) and m.get("value") for m in data):
            return data
    except Exception:
        pass
    return [dict(m) for m in FALLBACK_CLAUDE_MODELS]


def _normalize_models(raw) -> list:
    out = []
    for m in raw or []:
        v = str(m.get("value") or "").strip()
        if not v or v == "default":                     # "Default (recommended)" = Filect's '' choice
            continue
        out.append({"value": v, "name": str(m.get("displayName") or v),
                    "efforts": [e for e in (m.get("supportedEffortLevels") or []) if isinstance(e, str)]})
    return out


def claude_models(timeout: float = 25.0) -> list:
    """Ask Claude Code for this account's models (call OFF the UI thread). Saves the cache; returns [] on
    failure (Claude Code missing / signed out) so the caller keeps the cached list."""
    import asyncio

    async def _ask():
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
        use_login_path()
        opts = ClaudeAgentOptions(setting_sources=[], max_turns=1, allowed_tools=[],
                                  **({"cli_path": claude_cli_override()} if claude_cli_override() else {}))
        async with ClaudeSDKClient(options=opts) as c:
            info = await c.get_server_info() or {}
        return _normalize_models(info.get("models"))

    try:
        loop = asyncio.new_event_loop()
        try:
            models = loop.run_until_complete(asyncio.wait_for(_ask(), timeout))
        finally:
            loop.close()
    except Exception as e:
        logger.info("[agent] model list unavailable: %s", e)
        return []
    if models:
        try:
            _models_cache_path().write_text(json.dumps(models), encoding="utf-8")
        except Exception:
            pass
    logger.info("[agent] Claude Code models: %s", [m["value"] for m in models])
    return models


class AgentRunner:
    """One task == one Claude session.

    Callbacks (all provided by the caller / Qt layer):
      on_step(Step)                         -> live progress into the chat
      on_approval(ApprovalRequest) -> Awaitable[bool]   True=approve, False=deny (await the user's tap)
      on_result(session_id, cost_usd)       -> task finished

    The session id is captured on finish so the task can be resumed later (here or in Claude).
    `allow_all` can be switched on mid-turn ("Full access" / "Approve & don't ask again"): every later
    tool call in this turn then runs without a card.
    """

    def __init__(
        self,
        cwd: str,
        on_step: Callable[[Step], None],
        on_approval: Callable[[ApprovalRequest], "Awaitable[bool]"],
        on_result: Callable[[Optional[str], float], None],
        auto_allow: Optional[Set[str]] = None,
    ):
        self.cwd = cwd
        self.on_step = on_step
        self.on_approval = on_approval
        self.on_result = on_result
        self.auto_allow = DEFAULT_AUTO_ALLOW if auto_allow is None else auto_allow
        self.session_id: Optional[str] = None
        self.allow_all = False
        self.on_session = lambda sid: None   # (the owner sets it) the chat's id as soon as Claude Code says it
        self._client = None          # live ClaudeSDKClient (set during run) — for interrupt()
        self._live = None            # the text block Claude is writing right now (from stream events)
        self._live_at = 0.0          # when the last "partial" step went out (they're throttled)

    def _on_stream_event(self, se) -> None:
        """Claude's reply word by word (include_partial_messages): every LIVE_EVERY seconds at most, a
        "partial" step with the text block so far — the page and the card show it growing. The finished block
        still arrives as a normal "text" step, which replaces it. Thinking, tool inputs and subagents' words
        are left out. (Windows 2026-11-08: a long answer showed nothing until it was done — or until Stop.)"""
        if getattr(se, "parent_tool_use_id", None):
            return
        ev = getattr(se, "event", None) or {}
        t = ev.get("type")
        if t == "content_block_start":
            self._live = "" if (ev.get("content_block") or {}).get("type") == "text" else None
        elif t == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and self._live is not None:
                self._live += d.get("text") or ""
                now = time.monotonic()
                if self._live.strip() and now - self._live_at >= LIVE_EVERY:
                    self._live_at = now
                    self.on_step(Step("partial", text=self._live))
        elif t in ("content_block_stop", "message_stop"):
            self._live = None

    async def _can_use_tool(self, tool_name, tool_input, context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        if self.allow_all or tool_name in self.auto_allow:
            return PermissionResultAllow()
        approved = await self.on_approval(ApprovalRequest(tool_name, dict(tool_input or {})))
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by user")

    async def run(self, prompt: str, resume: Optional[str] = None, max_turns: int = 40,
                  bypass: bool = False, model: Optional[str] = None,
                  effort: Optional[str] = None, images: Optional[list] = None,
                  wait_for=None) -> Optional[str]:
        """Run (or resume) a task to completion, streaming steps and gating tools. Returns the
        session id (for resuming later).

        bypass=True runs without any Approve/Deny cards — like Claude Code's bypass-permissions
        mode, the agent edits files and runs commands on its own. Use only for trusted tasks.

        wait_for (a warm spare — Filect Assistant): start Claude Code — and your MCP tools — NOW, then await
        wait_for() for the request: (prompt, images), or None = not needed after all. (Mac, measured: a request
        then answers in ~1.7 s instead of ~6 s — the start-up is already done.)
        """
        from claude_agent_sdk import (ClaudeSDKClient, ClaudeAgentOptions, ResultMessage,
                                      AssistantMessage, CLINotFoundError)
        try:
            from claude_agent_sdk import StreamEvent
        except ImportError:                      # (an SDK without stream events: replies arrive whole)
            StreamEvent = None
        use_login_path()
        # setting_sources=[] isolates from the user's global ~/.claude permissions in both modes.
        # Tool search: your MCP tools' full descriptions load only when one is needed (Mac, measured 2026-11-08:
        # plain answers ~0.5 s faster, tool requests 1–2 s faster, the same tools picked, no extra approval cards).
        # Your own ENABLE_TOOL_SEARCH, if you set one, wins.
        common = dict(cwd=self.cwd, setting_sources=[], resume=resume, max_turns=max_turns,
                      system_prompt=tools_note("claude", self.cwd),
                      env={"ENABLE_TOOL_SEARCH": os.environ.get("ENABLE_TOOL_SEARCH") or "true"})
        if StreamEvent is not None:
            common["include_partial_messages"] = True    # the reply streams in live (see _on_stream_event)
        self._live, self._live_at = None, 0.0
        if claude_cli_override():
            common["cli_path"] = claude_cli_override()
        servers = claude_mcp_servers(self.cwd)      # your MCP tools (+ claude.ai connectors) — not your hooks
        if servers:
            common["mcp_servers"] = servers
        if model:            # '' / None -> let Claude Code use its default model
            common["model"] = model
        if effort:           # '' / None -> default reasoning effort
            common["effort"] = effort
        if bypass:
            # Engine skips permission prompts entirely — no callback, no cards.
            opts = ClaudeAgentOptions(permission_mode="bypassPermissions", **common)
        else:
            # Our Approve/Deny cards are the gate (read-only tools auto-allowed).
            opts = ClaudeAgentOptions(permission_mode="default", can_use_tool=self._can_use_tool, **common)
        logger.info("[agent] starting%s: model=%s effort=%s bypass=%s resume=%s",
                    " (warm spare — waits for a request)" if wait_for else "",
                    model or "(default)", effort or "(default)", bypass, bool(resume))
        # Screenshots go in the message itself — built BEFORE connecting, since it may briefly wait for a
        # screenshot that's still being written.
        msg = user_message(prompt, images) if images and wait_for is None else None
        try:
            async with ClaudeSDKClient(options=opts) as client:
                self._client = client            # exposed so stop() can interrupt the running turn
                if wait_for is not None:         # a warm spare: running, waiting for its request
                    got = await self._await_request(client, wait_for)
                    if got is None:
                        return None              # not needed after all — closes Claude Code
                    prompt, images = got
                    msg = user_message(prompt, images) if images else None
                    logger.info("[agent] warm start — Claude Code was already running")
                heard = False                    # (any reply yet: a warm start gone before one is re-run fresh)
                try:
                    if msg is not None:
                        logger.info("[agent] sending %d screenshot(s)",
                                    sum(b["type"] == "image" for b in msg["message"]["content"]))

                        async def _one():
                            yield msg
                        await client.query(_one())
                    else:
                        await client.query(prompt)
                    async for msg in client.receive_response():
                        heard = True
                        if StreamEvent is not None and isinstance(msg, StreamEvent):
                            self._on_stream_event(msg)
                            continue
                        if getattr(msg, "subtype", None) == "init" and isinstance(getattr(msg, "data", None), dict):
                            try:                         # (History: openable while it works, and after a crash)
                                self.on_session(msg.data.get("session_id"))
                            except Exception:
                                pass
                            mcp = msg.data.get("mcp_servers") or []
                            if mcp:                          # which of your tools this task got (diagnostics)
                                logger.info("[agent] MCP: %s", ", ".join(f"{m.get('name')} ({m.get('status')})" for m in mcp))
                        err = getattr(msg, "error", None) if isinstance(msg, AssistantMessage) else None
                        if err == "authentication_failed":
                            # Signed out: Claude Code answers "Not logged in · Please run /login" as if it were
                            # the reply — that showed as a DONE card. It's a setup step, not an answer.
                            raise AgentSetupError("signin")
                        if err in ("rate_limit", "billing_error"):
                            # Out of usage ("You've hit your limit · resets 3pm") — also not an answer.
                            from claude_agent_sdk import TextBlock
                            said = " ".join(b.text for b in msg.content if isinstance(b, TextBlock))
                            raise AgentSetupError("usage", detail=said)
                        for st in _steps_from_message(msg):
                            self.on_step(st)
                        if isinstance(msg, ResultMessage):
                            self.session_id = getattr(msg, "session_id", None)
                            self.on_result(self.session_id, getattr(msg, "total_cost_usd", 0.0) or 0.0)
                except Exception as e:
                    # A warm spare's Claude Code that had ended before ANY reply to this request (it died in the
                    # moment between being picked and being written to): WarmStartGone -> the worker runs the same
                    # request on a fresh Claude Code. Anything else (or once it has replied) is a real error.
                    code = _cli_exit_code(client) if wait_for is not None and not heard else None
                    if code is not None and not isinstance(e, AgentSetupError):
                        raise WarmStartGone(code) from e
                    raise
        except CLINotFoundError as e:            # no `claude` anywhere (the installed app doesn't bundle it)
            raise AgentSetupError("install") from e
        finally:
            self._client = None
        return self.session_id

    @staticmethod
    async def _await_request(client, wait_for, every: float = 0.5):
        """A warm spare waits for its request -- and keeps an eye on its Claude Code meanwhile. One that ends while
        it waits (a crash; Windows ending it) is noticed within `every` s (WarmStartGone: the spare ends quietly and
        the next request starts fresh) -- before, nothing noticed until the request was written to it and the chat
        said "Cannot write to terminated process (exit code: 15)" (live test 2026-11-11)."""
        want = asyncio.ensure_future(wait_for())
        try:
            while not want.done():
                await asyncio.wait({want}, timeout=every)
                if not want.done():
                    code = _cli_exit_code(client)
                    if code is not None:
                        raise WarmStartGone(code)
            return want.result()
        finally:
            if not want.done():
                want.cancel()

    async def interrupt(self) -> None:
        """Stop the running turn now (the SDK-sanctioned stop). Ends receive_response() so the task
        finishes promptly — far more responsive than cancelling the asyncio task."""
        c = self._client
        if c is None:
            return
        try:
            await c.interrupt()
            logger.info("[agent] interrupt sent")
        except Exception as e:
            logger.warning("[agent] interrupt failed: %s", e)
