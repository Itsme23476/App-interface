"""Drive a local Codex CLI agent (OpenAI) — the Agents tab's second engine, next to Claude Code (Windows port of
the Mac app's codex_runner.py, §8l).

Same contract as AgentRunner (on_step(Step) live, on_result(session_id, cost) at the end), so the page, the card,
the drawer and Details render a Codex chat exactly like a Claude one. `codex exec --json` streams JSONL events
(thread.started → item.started/completed → turn.completed | turn.failed); each maps to the Step the UI already
knows. Every task is a real, saved Codex chat (~/.codex), continued with `codex exec resume <id>`.

No Approve/Deny with Codex (exec never asks): Full access = --dangerously-bypass-approvals-and-sandbox, Ask first =
the workspace-write sandbox (edits inside the chat's folder only, no network).

Windows, checked on this PC (2026-11-08, codex-cli 0.166.0-alpha.5.1 — the copy inside the Codex app):
- Where Codex is: the standalone CLI (OpenAI's install.ps1 → %LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin), PATH,
  an npm install's native codex.exe, or — most people — the CLI that ships INSIDE the Codex desktop app (an MSIX
  package, found through the package API in ~1 ms). The app and the CLI share ~/.codex, so its sign-in counts.
- The request goes in on stdin (`-`): no 32 KB command-line limit, no quoting surprises.
- Commands run in PowerShell: `"C:\\…\\powershell.exe" -Command "…"` — shown unwrapped, as a PowerShell step.
- An edit arrives as "delete" + "add" of the same file — shown as one Write.
- Stop = end Codex's whole process tree (taskkill /T /F, ~0.2 s): SIGINT doesn't reach a windowless child on
  Windows, and Codex starts helpers (node, its code-mode host) that would otherwise linger. The chat stays
  usable — the next follow-up continued it.
- workspace-write works (Codex's Windows sandbox); the JSON is UTF-8.
- The Codex app is ChatGPT.exe (like ChatGPT.app on the Mac); it registers codex:// (codex://threads/<id>).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from app.core.agent_runner import (AgentSetupError, Step, _tool_detail, screenshot_text, use_login_path,
                                   wait_for_file)

logger = logging.getLogger(__name__)
IS_WIN = sys.platform.startswith("win")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0

# Used only if Codex's own model list can't be read (checked on this PC 2026-11-08).
_FALLBACK_MODELS = [("gpt-6-luna", "GPT-6-Luna"), ("gpt-5.6-terra", "GPT-5.6-Terra"),
                    ("gpt-5.6-luna", "GPT-5.6-Luna")]
_FALLBACK_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# Codex's own wording for an exhausted plan (from the CLI binary): "You've hit your usage limit…",
# "Usage limit reached…", "You're out of credits…", "Quota exceeded…", "…spend cap…".
_USAGE_RE = re.compile(r"usage limit|out of credits|quota exceeded|spend cap", re.I)

CODEX_PACKAGE_FAMILY = "OpenAI.Codex_2p2nqsd0c76g0"      # the Codex desktop app (Microsoft Store / MSIX)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _run(argv, timeout: float = 15.0, **kw):
    """A quick `codex …` call: no console window, UTF-8, never waits on stdin."""
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=_NO_WINDOW, **kw)


# ---- where Codex is ----------------------------------------------------------------------------------------
def _package_root(family: str = CODEX_PACKAGE_FAMILY) -> Optional[str]:
    """The install folder of the Codex desktop app (an MSIX package — its folder name changes with every
    update, and C:\\Program Files\\WindowsApps can't be listed), from the package API. None if it isn't installed."""
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32")
        get = k.GetPackagesByPackageFamily
        get.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.LPWSTR),
                        ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        count, blen = wintypes.UINT(0), wintypes.UINT(0)
        get(family, ctypes.byref(count), None, ctypes.byref(blen), None)
        if not count.value:
            return None
        names = (wintypes.LPWSTR * count.value)()
        buf = ctypes.create_unicode_buffer(blen.value)
        if get(family, ctypes.byref(count), names, ctypes.byref(blen), buf) != 0:
            return None
        path_of = k.GetPackagePathByFullName
        path_of.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        for i in range(count.value):
            n = wintypes.UINT(0)
            path_of(names[i], ctypes.byref(n), None)
            if n.value:
                p = ctypes.create_unicode_buffer(n.value)
                if path_of(names[i], ctypes.byref(n), p) == 0 and os.path.isdir(p.value):
                    return p.value
    except Exception as e:
        logger.debug("[codex] package lookup failed: %s", e)
    return None


def _app_cli() -> Optional[str]:
    root = _package_root()
    exe = os.path.join(root, "app", "resources", "codex.exe") if root else ""
    return exe if exe and os.path.isfile(exe) else None


def _npm_native() -> Optional[str]:
    """An npm install's real codex.exe (its `codex.cmd` shim goes through cmd.exe, which would mangle a
    request with & or | in it)."""
    base = Path(os.environ.get("APPDATA") or "") / "npm" / "node_modules" / "@openai"
    try:
        for exe in sorted(base.glob("codex*/**/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True):
            return str(exe)
    except Exception:
        pass
    return None


def _standalone() -> Optional[str]:
    """OpenAI's own installer (chatgpt.com/codex/install.ps1) puts codex.exe here (CODEX_INSTALL_DIR overrides)."""
    d = os.environ.get("CODEX_INSTALL_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or "",
                                                            "Programs", "OpenAI", "Codex", "bin")
    exe = os.path.join(d, "codex.exe")
    return exe if os.path.isfile(exe) else None


def find_codex() -> Optional[str]:
    """The codex.exe Filect runs, or None: FILECT_CODEX_CLI (tests) → OpenAI's installer → PATH → npm → the CLI
    inside the Codex desktop app. (~2 ms — no use_login_path here: on Windows that loads the Claude SDK.)"""
    v = (os.environ.get("FILECT_CODEX_CLI") or "").strip()
    if v:
        return v if os.path.isfile(v) else None
    if not IS_WIN:
        use_login_path()
        return shutil.which("codex")
    exe = _standalone()
    if exe:
        return exe
    w = shutil.which("codex")
    if w and w.lower().endswith(".exe"):
        return w
    return _npm_native() or _app_cli()


def codex_source(exe: Optional[str] = None) -> str:
    """"app" = the CLI inside the Codex desktop app (it updates with the app), "cli" = a CLI you installed."""
    exe = exe or find_codex() or ""
    root = _package_root() if IS_WIN else None
    return "app" if root and os.path.normcase(exe).startswith(os.path.normcase(root)) else ("cli" if exe else "")


def signed_in(exe: str) -> bool:
    """`codex login status`: exit 0 = signed in (~60 ms). A signed-out `exec` instead retries 401s ~30 s."""
    try:
        return _run([exe, "login", "status"]).returncode == 0
    except Exception:
        return False


def codex_setup_state() -> str:
    """"ready" (installed AND signed in), "install" or "signin"."""
    exe = find_codex()
    if not exe:
        return "install"
    return "ready" if signed_in(exe) else "signin"


def codex_ready() -> bool:
    """Codex installed AND signed in — a task waiting on its setup runs by itself once this is true."""
    return codex_setup_state() == "ready"


def _levels(m: dict) -> list:
    out = []
    for e in m.get("supported_reasoning_levels") or m.get("supported_reasoning_efforts") or []:
        v = e.get("effort") if isinstance(e, dict) else e
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


def codex_model_info() -> list:
    """[{value, name, efforts}] — the models the installed Codex offers, best first (the same shape as
    agent_runner.claude_models): its live catalog (`codex debug models`, ~70 ms), else its cached list."""
    data = None
    exe = find_codex()
    if exe:
        try:
            data = json.loads(_run([exe, "debug", "models"], timeout=10).stdout)
        except Exception:
            data = None
    if data is None:
        try:
            data = json.loads((codex_home() / "models_cache.json").read_text(encoding="utf-8"))
        except Exception:
            data = None
    models = data.get("models", []) if isinstance(data, dict) else (data or [])
    ms = sorted((m for m in models if isinstance(m, dict) and m.get("visibility") == "list" and m.get("slug")),
                key=lambda m: m.get("priority", 99))
    out = [{"value": m["slug"], "name": m.get("display_name") or m["slug"], "efforts": _levels(m) or list(_FALLBACK_EFFORTS)}
           for m in ms]
    return out or [{"value": s, "name": n, "efforts": list(_FALLBACK_EFFORTS)} for s, n in _FALLBACK_MODELS]


def codex_models() -> list:
    """[(slug, display name)] — the card's model list (Mac shape)."""
    return [(m["value"], m["name"]) for m in codex_model_info()]


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3])


def codex_update_available() -> str:
    """The newer Codex version that exists, or "" — from Codex's own update check (~/.codex/version.json) vs
    `codex --version`. Never for the CLI inside the Codex app (the app updates it)."""
    exe = find_codex()
    if not exe or codex_source(exe) == "app":
        return ""
    try:
        latest = json.loads((codex_home() / "version.json").read_text(encoding="utf-8")).get("latest_version") or ""
        cur = _run([exe, "--version"], timeout=10).stdout
    except Exception:
        return ""
    return latest if latest and _ver(latest) > _ver(cur) else ""


def update_codex() -> tuple:
    """Codex's own updater (`codex update`: knows how it was installed; no questions). (ok, last line)."""
    exe = find_codex()
    if not exe:
        return False, "Codex isn't installed."
    if codex_source(exe) == "app":
        return False, "Codex updates together with the Codex app."
    try:
        r = _run([exe, "update"], timeout=300)
        lines = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        logger.info("[codex] update exit=%s: %s", r.returncode, lines[-1] if lines else "")
        return r.returncode == 0, lines[-1] if lines else ""
    except Exception as e:
        return False, str(e)


# The Codex desktop app holds a chat it has opened ("thread … already has an active writer") — until it quits —
# so Filect can't add a turn to it; the page asks: close Codex and continue here, or a new chat.
CHAT_BUSY = "This chat is open in the Codex app, so it can't be continued from here."


def _unavailable(msg: str) -> Optional[AgentSetupError]:
    """Codex's error text → out of usage / signed out (so Filect switches agents), else None."""
    if _USAGE_RE.search(msg or ""):
        m = re.search(r"try again (?:at|in) [^.()]+", msg, re.I)
        return AgentSetupError("usage", "codex", (m.group(0)[0].upper() + m.group(0)[1:] + ".") if m
                               else msg[:160])
    if "401" in (msg or "") and "nauthorized" in msg:
        return AgentSetupError("signin", "codex")
    return None


# ---- Codex's items → the Steps Details already draws ----------------------------------------------------
_SHELLS = ("powershell", "pwsh", "cmd", "bash", "zsh", "sh")
_PS_RE = re.compile(r'^\s*(?:"[^"]*?(?:powershell|pwsh)(?:\.exe)?"|\S*?(?:powershell|pwsh)(?:\.exe)?)\s+'
                    r'(?:-(?:NoProfile|NoLogo|NonInteractive)\s+)*-Command\s+(.*)$', re.I | re.S)


def _unquote_ps(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1].replace('\\"', '"')
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


def _shell_cmd(cmd) -> tuple:
    """(the command you'd recognise, "PowerShell" | "Bash"). Live events give one string
    (`"C:\\…\\powershell.exe" -Command "Get-ChildItem"`), saved chats a list ([exe, "-Command", "…"]);
    Mac: `/bin/zsh -lc 'npm test'`."""
    tool = "PowerShell" if IS_WIN else "Bash"
    try:
        if isinstance(cmd, list):
            if len(cmd) >= 3 and str(cmd[1]).lower() in ("-command", "-c", "-lc", "/c"):
                exe = os.path.basename(str(cmd[0])).lower()
                return str(cmd[2]), ("PowerShell" if exe.startswith(("powershell", "pwsh")) else tool)
            return " ".join(str(c) for c in cmd), tool
        s = cmd or ""
        m = _PS_RE.match(s)
        if m:
            return _unquote_ps(m.group(1)), "PowerShell"
        if not IS_WIN:
            import shlex
            argv = shlex.split(s)
            if len(argv) >= 3 and argv[1] in ("-lc", "-c"):
                return argv[2], "Bash"
        return s, tool
    except Exception:
        return (" ".join(map(str, cmd)) if isinstance(cmd, list) else (cmd or "")), tool


def _file_changes(item) -> list:
    """[(path, kind, content|None)] — the live event lists [{path, kind}], the saved chat a {path: {type,
    content}} map (the database: [{path, kind: {type}}])."""
    ch = item.get("changes") or []
    if isinstance(ch, dict):
        return [(p, (v or {}).get("type", "update"), (v or {}).get("content")) for p, v in ch.items()]
    out = []
    for c in ch:
        if isinstance(c, dict):
            kind = c.get("kind", "update")
            out.append((c.get("path", ""), kind.get("type", "update") if isinstance(kind, dict) else kind, None))
    return out


def _read(path: str, limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception:
        return ""


def _delete_cmd(path: str) -> str:
    if IS_WIN:
        return "Remove-Item -LiteralPath '" + path.replace("'", "''") + "'"
    import shlex
    return f"rm {shlex.quote(path)}"


def _deleted_path(st: Step) -> str:
    """The file a delete step (from _item_steps) removed, else ""."""
    c = (st.input or {}).get("command", "") if st.kind == "tool" else ""
    if c.startswith("Remove-Item -LiteralPath '") and c.endswith("'"):
        return c[len("Remove-Item -LiteralPath '"):-1].replace("''", "'")
    if c.startswith("rm "):
        try:
            import shlex
            return shlex.split(c)[1]
        except Exception:
            return ""
    return ""


def _item_steps(item: dict, completed: bool, saved: bool = False) -> list:
    """Steps for one Codex item, in the shapes Claude's steps have (PowerShell / Write / WebSearch / TodoWrite),
    so Details renders them unchanged. Reasoning and transport warnings are skipped."""
    t, iid = (item.get("type") or ""), item.get("id") or ""
    t = re.sub(r"(?<!^)(?=[A-Z])", "_", t).lower()          # saved chats say "CommandExecution"
    if t == "agent_message" and completed:
        text = item.get("text")
        if text is None:                                     # saved: content [{type: Text, text}]
            text = " ".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
        return [Step("text", text=text.strip())] if text and text.strip() else []
    if t == "command_execution":
        cmd, tool_name = _shell_cmd(item.get("command"))
        inp = {"command": cmd}
        tool = Step("tool", tool=tool_name, detail=_tool_detail(tool_name, inp), input=inp, tool_id=iid)
        if not completed:
            return [tool]
        code = item.get("exit_code", item.get("exitCode"))   # (the database spells it in camelCase)
        res = Step("result", tool_id=iid, text=item.get("aggregated_output") or item.get("aggregatedOutput")
                   or item.get("stdout") or "", is_error=code not in (0, None) or item.get("status") == "failed")
        return [tool, res] if saved else [res]               # live: the tool step came on item.started
    if t == "file_change" and completed:                     # one Write per file, with what's in it now
        out = []
        for i, (path, kind, content) in enumerate(_file_changes(item)):
            fid = f"{iid}:{i}"
            if kind == "delete":
                inp = {"command": _delete_cmd(path)}
                out.append(Step("tool", tool="PowerShell" if IS_WIN else "Bash", detail=os.path.basename(path),
                                input=inp, tool_id=fid))
            else:
                inp = {"file_path": path, "content": content if content is not None else _read(path)}
                out.append(Step("tool", tool="Write", detail=os.path.basename(path), input=inp, tool_id=fid))
            out.append(Step("result", tool_id=fid, is_error=item.get("status") == "failed"))
        return out
    if t == "web_search" and completed:
        q = item.get("query") or ""
        return [Step("tool", tool="WebSearch", detail=q[:60], input={"query": q}, tool_id=iid)]
    if t == "mcp_tool_call" and completed:
        name = f"{item.get('server', '')}.{item.get('tool', '')}".strip(".")
        return [Step("tool", tool=name or "MCP tool", input=dict(item.get("arguments") or {}), tool_id=iid),
                Step("result", tool_id=iid, is_error=bool(item.get("error")) or item.get("status") == "failed")]
    if t == "todo_list" and completed:
        todos = [{"content": x.get("text", ""), "status": "completed" if x.get("completed") else "pending"}
                 for x in item.get("items") or []]
        return [Step("tool", tool="TodoWrite", input={"todos": todos}, tool_id=iid)]
    return []


class _Rewrites:
    """Codex on Windows edits a file as "delete" then "add" (two items). Hold a delete back for a moment: if the
    same file is added next, it was an edit — one Write, no "Remove-Item" step that never really happened."""

    def __init__(self, emit):
        self.emit, self.held = emit, []

    def push(self, steps: list) -> None:
        dels = [s for s in steps if _deleted_path(s)]
        if dels and all(_deleted_path(s) or s.kind == "result" for s in steps):
            self.flush()
            self.held = list(steps)
            return
        if self.held:
            added = {os.path.normcase((s.input or {}).get("file_path", "")) for s in steps
                     if s.kind == "tool" and s.tool == "Write"}
            gone = {os.path.normcase(_deleted_path(s)) for s in self.held if _deleted_path(s)}
            if gone and gone <= added:
                self.held = []
            else:
                self.flush()
        for s in steps:
            self.emit(s)

    def flush(self) -> None:
        held, self.held = self.held, []
        for s in held:
            self.emit(s)


def _merge_rewrites(steps: list) -> list:
    """The same for a saved chat (a list of Steps)."""
    out = []
    r = _Rewrites(out.append)
    group = []
    for s in steps:                                          # (a tool step + its result = one item)
        if s.kind == "tool" and group:
            r.push(group)
            group = []
        group.append(s)
    if group:
        r.push(group)
    r.flush()
    return out


def _long_path(p: str) -> str:
    """The folder's full long name (C:\\Users\\DAMIAN~1\\… → C:\\Users\\damianosmalliaros\\…). Codex's Windows
    sandbox can't WRITE in a folder given by its short 8.3 name — "Access to the path … is denied" (seen
    2026-11-08; the long name of the same folder worked, spaces and all). Drive letters stay as they are."""
    if not IS_WIN or not p:
        return p
    try:
        import ctypes
        from ctypes import wintypes
        f = ctypes.WinDLL("kernel32").GetLongPathNameW
        f.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        f.restype = wintypes.DWORD
        n = f(p, None, 0)
        if n:
            buf = ctypes.create_unicode_buffer(n)
            if f(p, buf, n):
                return buf.value
    except Exception:
        pass
    return p


# ---- the process tree (Stop) --------------------------------------------------------------------------------
def _kill_tree(pid: int) -> None:
    """End Codex AND what it started (node, its code-mode host, a running command) — at once."""
    if not pid:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=10,
                           creationflags=_NO_WINDOW)
        else:
            import signal
            os.kill(pid, signal.SIGINT)
    except Exception as e:
        logger.warning("[codex] couldn't end Codex (pid %s): %s", pid, e)


class CodexRunner:
    """One task == one Codex chat. Same callbacks as AgentRunner (on_approval is never called)."""

    def __init__(self, cwd: str, on_step: Callable[[Step], None], on_approval=None,
                 on_result: Callable[[Optional[str], float], None] = None, auto_allow=None):
        self.cwd = cwd
        self.on_step = on_step
        self.on_result = on_result or (lambda *a: None)
        self.session_id: Optional[str] = None
        self.allow_all = False                               # (Claude's mid-turn Full access; Codex can't ask)
        self.on_session = lambda sid: None   # (the owner sets it) the thread's id as soon as Codex says it
        self._proc = None
        self._interrupted = False

    def argv(self, exe: str, resume: Optional[str], bypass: bool, model: Optional[str], effort: Optional[str],
             shots: list) -> list:
        """The command line; the request itself goes in on stdin (the "-" at the end)."""
        # Your normal Codex config (Mac 2026-11-07: the agent should have every tool your Codex has): MCP servers,
        # plugins and apps load as in the Codex app. MCP tools need Full access — exec can't ask.
        from app.core.agent_runner import tools_note
        a = [exe, "exec"] + (["resume"] if resume else []) + ["--json", "--skip-git-repo-check"]
        a += (["--dangerously-bypass-approvals-and-sandbox"] if bypass
              else ["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"'])
        a += ["-c", f"developer_instructions={json.dumps(tools_note('codex', self.cwd))}"]
        if model:
            a += ["-m", model]
        if effort:
            a += ["-c", f'model_reasoning_effort="{effort}"']
        for p in shots:
            a += ["-i", p]
        return a + ["--"] + ([resume] if resume else []) + ["-"]

    async def run(self, prompt: str, resume: Optional[str] = None, max_turns: int = 40,
                  bypass: bool = False, model: Optional[str] = None, effort: Optional[str] = None,
                  images: Optional[list] = None) -> Optional[str]:
        exe = find_codex()
        if not exe:
            raise AgentSetupError("install", "codex")
        if not signed_in(exe):
            raise AgentSetupError("signin", "codex")
        shots = [p for p in (images or []) if wait_for_file(p)]
        text = screenshot_text(prompt, images, len(shots)) if images else prompt
        argv = self.argv(exe, resume, bypass, model, effort, shots)
        logger.info("[codex] starting: model=%s effort=%s bypass=%s resume=%s screenshots=%d (%s)",
                    model or "(default)", effort or "(default)", bypass, bool(resume), len(shots), codex_source(exe))
        self.session_id = resume
        if self._interrupted:                                # Stop pressed while it was starting up
            return self.session_id
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=_long_path(self.cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            limit=1 << 24, creationflags=_NO_WINDOW)          # one command's output is one JSON line
        self._proc = proc
        if self._interrupted:
            _kill_tree(proc.pid)
        tail: List[str] = []
        rewrites = _Rewrites(self.on_step)

        async def feed():                                    # the request, then EOF
            try:
                proc.stdin.write(text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception:
                pass

        async def drain():                                   # stderr must be read or Codex blocks on it
            async for ln in proc.stderr:
                tail.append(ln.decode("utf-8", "replace").strip())
                del tail[:-15]
        feeder = asyncio.ensure_future(feed())
        drainer = asyncio.ensure_future(drain())
        finished, failure = False, ""
        try:
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "thread.started":
                    self.session_id = ev.get("thread_id") or self.session_id
                    try:
                        self.on_session(self.session_id)
                    except Exception:
                        pass
                elif t in ("item.started", "item.completed"):
                    item = ev.get("item") or {}
                    if item.get("type") == "error":          # a transport warning ("Falling back from…")
                        err = _unavailable(item.get("message", ""))
                        if err:
                            raise err
                        continue
                    steps = _item_steps(item, t == "item.completed")
                    if steps:
                        rewrites.push(steps)
                elif t in ("error", "turn.failed"):
                    msg = ev.get("message") or (ev.get("error") or {}).get("message") or ""
                    err = None if self._interrupted else _unavailable(msg)
                    if err:
                        raise err                            # don't sit through Codex's 30 s of retries
                    if t == "turn.failed":
                        failure = msg
                elif t == "turn.completed":
                    rewrites.flush()
                    finished = True
                    self.on_result(self.session_id, 0.0)     # the ChatGPT plan isn't billed per task
            rewrites.flush()
            await proc.wait()
        finally:
            if proc.returncode is None:                      # cancelled / raised: don't leave it running
                _kill_tree(proc.pid)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except Exception:
                    pass
            feeder.cancel()
            drainer.cancel()
            self._proc = None
        if not finished and not self._interrupted:
            why = failure or next((l for l in reversed(tail) if l and "ERROR codex_core::util" not in l), "")
            if "already has an active writer" in why or any("already has an active writer" in l for l in tail):
                raise RuntimeError(CHAT_BUSY)
            raise RuntimeError(why or f"Codex stopped unexpectedly (exit {proc.returncode}).")
        return self.session_id

    async def interrupt(self) -> None:
        """Stop the running turn: end Codex and everything it started. Pressed before Codex has started, it
        never starts."""
        self._interrupted = True
        p = self._proc
        if p is not None and p.returncode is None:
            _kill_tree(p.pid)
            logger.info("[codex] stopped (process tree ended)")

    def kill(self) -> None:
        """Quit only: end this turn's Codex now (from any thread)."""
        self._interrupted = True
        p = self._proc
        if p is not None and p.returncode is None:
            _kill_tree(p.pid)


# ---- the one-click fixes on the card -------------------------------------------------------------------
def _ps(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _codex_line() -> str:
    """PowerShell: $codex = the Codex Filect uses (found again after an install, without restarting Filect)."""
    known = find_codex() or ""
    return ("$codex = $null; "
            "foreach ($c in @(\"$env:LOCALAPPDATA\\Programs\\OpenAI\\Codex\\bin\\codex.exe\", " + _ps(known) + ")) "
            "{ if ($c -and (Test-Path -LiteralPath $c)) { $codex = $c; break } }; "
            "if (-not $codex) { $codex = 'codex' }")


def open_codex_setup(kind: str) -> str:
    """A PowerShell window: OpenAI's OFFICIAL installer (only if missing; CODEX_NON_INTERACTIVE so it doesn't ask
    questions or drop you into Codex), then `codex login` (ChatGPT sign-in in the browser, then it exits), then a
    plain "all set". Returns the script's path."""
    from app.core.agent_runner import open_terminal
    if not IS_WIN:
        lines = ["clear", 'echo "Filect · setting up Codex"', "echo"]
        if kind == "install":
            lines += ["curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh || "
                      '{ echo; echo "The install did not finish. See above."; read -n 1; exit 1; }', "echo"]
        lines += ['echo "Signing in with your ChatGPT account. A browser window will open."', "echo", "codex login",
                  "echo", "if codex login status >/dev/null 2>&1; then",
                  '  echo "✓ All set. Filect is running your task now. You can close this window."', "else",
                  '  echo "Sign-in didn\'t finish. Click the button in Filect to try again."', "fi"]
        return open_terminal("filect-codex-setup.command", lines, ("CODEX_HOME",))
    lines = ["$Host.UI.RawUI.WindowTitle = 'Filect - setting up Codex'", "Clear-Host",
             "Write-Host 'Filect - setting up Codex'", "Write-Host ''"]
    if kind == "install":
        lines += ["Write-Host \"Installing Codex with OpenAI's official installer (chatgpt.com/codex/install.ps1)...\"",
                  "$env:CODEX_NON_INTERACTIVE = '1'",
                  "try { irm https://chatgpt.com/codex/install.ps1 | iex } catch { Write-Host ''; "
                  "Write-Host 'The install did not finish. See above.' -ForegroundColor Yellow; return }",
                  "Write-Host ''"]
    lines += [_codex_line(),
              "Write-Host 'Signing in with your ChatGPT account. A browser window will open.'", "Write-Host ''",
              "& $codex login", "Write-Host ''",
              "& $codex login status *> $null",
              "if ($LASTEXITCODE -eq 0) {",
              "  Write-Host 'All set. Filect is running your task now. You can close this window.' -ForegroundColor Green",
              "} else {",
              "  Write-Host \"Sign-in didn't finish. Click the button in Filect to try again.\" -ForegroundColor Yellow",
              "}"]
    path = open_terminal("filect-codex-setup.ps1", lines, ("CODEX_HOME",))
    logger.info("[codex] opened Codex setup (%s)", kind)
    return path


def _desktop_app() -> bool:
    """Is the Codex desktop app installed? (Windows: its package — whether Windows has a default app for
    codex:// links varies: on the dev PC it answered "Pick an app".)"""
    if IS_WIN:
        return _package_root() is not None
    from app.core.agent_runner import has_app_for
    return has_app_for("codex://threads/new")


def _app_alias() -> Optional[str]:
    """The Codex app's launch alias (it passes a codex:// link to the app the way Windows would)."""
    p = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Microsoft", "WindowsApps",
                     CODEX_PACKAGE_FAMILY, "chatgpt-desktop.exe")
    return p if os.path.exists(p) else None


# ---- a chat the Codex app is holding: close the app, continue the SAME chat, reopen the app --------------
def _processes() -> list:
    """[(pid, parent pid, exe path)] of every process (Toolhelp snapshot + the image path; ~10 ms)."""
    if not IS_WIN:
        return []
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE)]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out = []
    try:
        e = PE()
        e.dwSize = ctypes.sizeof(PE)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            path = e.szExeFile
            h = k.OpenProcess(0x1000, False, e.th32ProcessID)      # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                buf = ctypes.create_unicode_buffer(1024)
                n = wintypes.DWORD(1024)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    path = buf.value
                k.CloseHandle(h)
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), path))
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def _codex_app_pids() -> list:
    """The Codex desktop app's top process(es) — ChatGPT.exe from the Codex package (NOT the CLI Filect runs
    from the same package, nor the separate ChatGPT app)."""
    root = _package_root()
    if not root:
        return []
    appdir = os.path.normcase(os.path.join(root, "app"))
    procs = _processes()
    mine = {pid for pid, _pp, path in procs
            if os.path.normcase(os.path.dirname(path)) == appdir
            and os.path.basename(path).lower() in ("chatgpt.exe", "codex.exe")}
    return [pid for pid, pp, _p in procs if pid in mine and pp not in mine]


def codex_app_running() -> bool:
    return bool(_codex_app_pids())


def codex_app_busy() -> list:
    """[(chat id, title)] the Codex app is working on RIGHT NOW (closing it would stop them): a turn still in
    progress with activity in the last 3 minutes. (Old crashed turns stay "inProgress" forever, and Filect's own
    runs aren't the app's.)"""
    try:
        cutoff = int(time.time() * 1000) - 180_000
        running = _db("thread_history").execute(
            "select distinct t.thread_id from thread_turns t where t.status = 'inProgress' and t.completed_at is null "
            "and exists (select 1 from thread_items i where i.thread_id = t.thread_id "
            "and coalesce(i.completed_at_ms, i.started_at_ms, i.created_at_ms) >= ?)", (cutoff,)).fetchall()
        if not running:
            return []
        db = _db("state")
        out = []
        for (tid,) in running:
            row = db.execute("select coalesce(nullif(name, ''), title, ''), source from threads where id = ?",
                             (tid,)).fetchone()
            if row and row[1] != "exec":                 # the app's own chats (not Filect / CLI runs)
                out.append((tid, (row[0] or "a task")[:60]))
        return out
    except Exception:
        return []


def _codex_cmdlines() -> list:
    """[(pid, command line)] of every codex.exe that could be Codex in a terminal. Command lines need WMI (~1-5 s),
    so it's asked only when such a codex.exe runs at all (the Codex app's own server, in
    %LOCALAPPDATA%\\OpenAI\\Codex\\bin, doesn't count) — and only ever off the UI thread."""
    if not IS_WIN:
        return []
    own = os.path.normcase(os.path.join(os.environ.get("LOCALAPPDATA") or "\0", "OpenAI", "Codex"))
    if not any(os.path.basename(p).lower() == "codex.exe" and not os.path.normcase(p).startswith(own)
               for _pid, _pp, p in _processes()):
        return []
    try:
        out = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\" | "
                    "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"], timeout=20).stdout
    except Exception:
        return []
    rows = []
    for ln in out.splitlines():
        pid, _, cmd = ln.partition("\t")
        if pid.strip().isdigit():
            rows.append((int(pid), cmd))
    return rows


# Codex in a terminal: `codex resume <id>` / `codex` (interactive). Not the app's own helpers (app-server, …).
_NOT_TUI = {"app-server", "exec", "e", "mcp-server", "mcp", "debug", "login", "logout", "update", "archive",
            "review", "completion", "app", "remote-control", "apply", "sandbox", "cloud", "features", "help",
            "exec-server"}


def _argv_words(cmd: str) -> list:
    return [w.strip('"') for w in re.findall(r'"[^"]*"|\S+', cmd or "")]


def _resume_pids(session_id: str) -> list:
    """Codex in a terminal on this chat (`codex resume <id>`) — never Filect's own `codex exec resume`."""
    out = []
    for pid, cmd in _codex_cmdlines():
        words = _argv_words(cmd)
        if session_id in words and "resume" in words and "exec" not in words and "e" not in words[1:3]:
            out.append(pid)
    return out


def _daemon_up() -> bool:
    return (codex_home() / "app-server-control").exists()


def _codex_tuis() -> list:
    """pids of interactive Codex sessions (Codex in a terminal) — the background server's users."""
    root = os.path.normcase(_package_root() or "\0")
    pids = []
    for pid, cmd in _codex_cmdlines():
        words = _argv_words(cmd)
        if words and not os.path.normcase(words[0]).startswith(root) and "OpenAI\\Codex\\bin" not in words[0] \
                and not _NOT_TUI.intersection(words[1:]):
            pids.append(pid)
    return pids


def _stop_daemon_if_idle() -> bool:
    """Codex 0.166+'s shared background server keeps holding a chat after Codex in a terminal closed: stop it
    (it saves its chats first) — only when no Codex in a terminal still uses it."""
    exe = find_codex()
    if not exe or not _daemon_up() or _codex_tuis():
        return False
    try:
        _run([exe, "app-server", "daemon", "stop"], timeout=20)
    except Exception as e:
        logger.warning("[codex] couldn't stop Codex's background server: %s", e)
        return False
    logger.info("[codex] stopped Codex's background server (it was still holding the chat)")
    return True


def chat_holder(session_id: str) -> str:
    """Who holds this chat: "terminal" (Codex in a terminal on it — or its background server), "app" (the Codex
    app), or "". Call off the UI thread (it asks WMI)."""
    if _resume_pids(session_id):
        return "terminal"
    if codex_app_running():
        return "app"
    return "terminal" if _daemon_up() else ""


def _close_app(force: bool) -> None:
    for pid in _codex_app_pids():
        argv = ["taskkill", "/PID", str(pid)] + (["/T", "/F"] if force else [])
        subprocess.run(argv, capture_output=True, timeout=10, creationflags=_NO_WINDOW)


# Windows (checked 2026-11-08): the Codex app's ✕ (a normal close) only HIDES it — it keeps running in the
# background and keeps holding the chat. So after a short grace it's ended; you chose "Close Codex" on the card,
# and Codex saves every chat as it goes (Filect then continued the same chat). Working (`force`): a little longer.
CLOSE_GRACE = 2.0
FORCE_GRACE = 6.0


def release_chat(session_id: str, timeout: float = 15.0, force: bool = False) -> str:
    """Free a chat another Codex program holds: Codex in a terminal on that chat is ended; else the Codex app is
    asked to close, then ended if it only hid itself (every chat is saved). `force` (you chose "Close Codex
    anyway" while it was working): the same after a longer grace — that stops its task, nothing saved is lost.
    Returns what was closed ("terminal" / "app"), or "" if it couldn't. Off the UI thread."""
    held_by = _resume_pids(session_id)
    if held_by:
        for pid in held_by:
            _kill_tree(pid)
        what = "terminal"
    elif codex_app_running():
        _close_app(False)
        what = "app"
    elif _stop_daemon_if_idle():
        time.sleep(0.5)
        return "terminal"
    else:
        return ""
    start = time.time()
    forced = False
    grace = FORCE_GRACE if force else CLOSE_GRACE
    while time.time() < start + timeout:
        if what == "app" and not forced and time.time() > start + grace:
            _close_app(True)
            forced = True
            logger.info("[codex] the Codex app stayed open in the background — ended it")
        alive = codex_app_running() if what == "app" else bool(_resume_pids(session_id))
        if not alive:
            logger.info("[codex] closed the %s holding %s", what, session_id)
            if what == "terminal":
                _stop_daemon_if_idle()
            time.sleep(0.5)                              # let its lock go
            return what
        time.sleep(0.4)
    logger.warning("[codex] the %s holding %s didn't close", what, session_id)
    return ""


def _open_url(url: str) -> None:
    from app.core import agent_runner as ar
    ar._open_url(url)


_launch = None      # tests capture app launches here: fn(argv) -> None


def _app_window_visible(pids) -> bool:
    """Does one of these Codex app processes show its MAIN window? (Its real window has a frame and isn't a tool
    window. Electron also keeps hidden helper windows — one is an invisible, always-on-top 760x640 sheet that
    swallowed every click on that part of the screen when it was shown by mistake, 2026-11-08. Never show them.)"""
    if not IS_WIN or not pids:
        return False
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32")
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    pids, seen = set(pids), []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(h):
            style = u.GetWindowLongW(h, -16) & 0xFFFFFFFF
            ex = u.GetWindowLongW(h, -20) & 0xFFFFFFFF
            if style & 0x00C40000 and not ex & 0x00000080:     # (WS_CAPTION | WS_THICKFRAME) and not a tool window
                seen.append(h)
        return True
    u.EnumWindows(cb, 0)
    return bool(seen)


def _allow_foreground() -> None:
    """Filect is in front (you just clicked): let the Codex app take the front when it opens the chat."""
    if IS_WIN:
        try:
            import ctypes
            ctypes.WinDLL("user32").AllowSetForegroundWindow(-1)        # ASFW_ANY
        except Exception:
            pass


def open_codex_chat(session_id: str, settle: float = 4.0) -> str:
    """(Re)open the Codex app right on this chat: its launch alias with the codex:// link — what a codex:// link
    does. A running Codex app gets the link from the new launch (Electron's one-copy lock) and shows the chat.
    Windows catch (2026-11-08): a Codex app started BEFORE the app renamed its data folder (...\\Codex\\web\\Codex
    -> ...\\web\\chatgpt-desktop) can't be reached that way, so the launch becomes a SECOND Codex app on the chat.
    Then the older copy is ended — only if its window is hidden (you'd closed it) and it isn't working on anything
    — so one Codex app is left, showing the chat. Returns "opened" (or "" if it couldn't launch). Off the UI
    thread."""
    url = f"codex://threads/{session_id}"
    if not IS_WIN:
        _open_url(url)
        return "opened"
    alias = _app_alias()
    if not alias:
        try:
            _open_url(url)
            return "opened"
        except Exception as e:
            logger.warning("[codex] couldn't open the Codex app: %s", e)
            return ""
    before = set(_codex_app_pids())
    _allow_foreground()
    argv = [alias, "--single-argument", url]
    if _launch is not None:
        _launch(argv)
    else:
        subprocess.Popen(argv, close_fds=True)
    if not before or settle <= 0:
        return "opened"
    end, new = time.time() + settle, set()
    while time.time() < end:                         # a link handed over: the new launch exits within ~0.5 s
        time.sleep(0.5)
        new = set(_codex_app_pids()) - before
        if not new:
            continue
        time.sleep(1.5)
        new = set(_codex_app_pids()) - before
        break
    if not new:
        logger.info("[codex] the running Codex app opened %s", session_id)
        return "opened"
    old = [p for p in before if p in set(_codex_app_pids())]
    if old and not _app_window_visible(old) and not codex_app_busy():
        for pid in old:
            _kill_tree(pid)
        logger.info("[codex] a second Codex app opened %s (the running one couldn't get the link) — ended the older, "
                    "hidden copy %s", session_id, old)
    else:
        logger.info("[codex] a second Codex app opened %s; the older copy %s is in use, left open", session_id, old)
    return "opened"


def _has_flag(flag: str, *cmd: str) -> bool:
    """Does this Codex know `flag` (older ones don't)?"""
    exe = find_codex()
    try:
        return bool(exe) and flag in _run([exe, *cmd, "--help"], timeout=10).stdout
    except Exception:
        return False


def open_in_codex(session_id: str, cwd: str, where: str = "app") -> str:
    """"Open in Codex": the chat in the Codex desktop app (codex://threads/<id>), or `where` = "terminal" (or no
    desktop app, or a custom CODEX_HOME the app doesn't use) → Codex in a PowerShell window, in the chat's folder.
    Either one holds the chat while open (CHAT_BUSY) — see AssistantPage._ask_or_close_codex."""
    from app.core.agent_runner import open_terminal
    if where == "app" and _desktop_app() and not os.environ.get("CODEX_HOME"):
        if open_codex_chat(session_id):
            logger.info("[codex] opened %s in the Codex app", session_id)
            return f"codex://threads/{session_id}"
    # --no-daemon (Codex 0.166+): runs on its own, so the chat is free the moment you close it there.
    flag = " --no-daemon" if _has_flag("--no-daemon", "resume") else ""
    folder = _long_path(cwd or str(Path.home()))
    if IS_WIN:
        return open_terminal("filect-open-codex.ps1",
                             ["$Host.UI.RawUI.WindowTitle = 'Codex'",
                              f"Set-Location -LiteralPath {_ps(folder)}", _codex_line(),
                              f"& $codex resume{flag} {_ps(session_id)}"], ("CODEX_HOME",))
    import shlex
    return open_terminal("filect-open-codex.command",
                         [f"cd {shlex.quote(folder)} || exit 1", f"exec codex resume{flag} {shlex.quote(session_id)}"],
                         ("CODEX_HOME",))


# ---- voice routing when Claude isn't available ---------------------------------------------------------
def complete(system: str, prompt: str, timeout: float = 60.0) -> str:
    """One quick answer from Codex (no tools, nothing saved): the intent router's fallback."""
    import tempfile
    exe = find_codex()
    if not exe or not signed_in(exe):
        raise RuntimeError("Codex unavailable")
    with tempfile.TemporaryDirectory() as d:
        out = subprocess.run(
            [exe, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
             "-c", 'sandbox_mode="read-only"', "-c", 'model_reasoning_effort="low"', "--", "-"],
            cwd=d, input=f"{system}\n\nDo not run any commands. Reply with the answer only.\n\n{prompt}",
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=_NO_WINDOW).stdout
    texts = []
    for ln in out.splitlines():
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        it = ev.get("item") or {}
        if ev.get("type") == "item.completed" and it.get("type") == "agent_message":
            texts.append(it.get("text") or "")
    if not texts:
        raise RuntimeError("Codex gave no answer")
    return texts[-1]


# ---- saved Codex chats (the page's list, the drawer, "use my conversation about…") --------------------
def _norm(p: str) -> str:
    """Codex keeps folders as `\\\\?\\C:\\…` in its database and `C:\\…` in its files: compare them as one."""
    p = (p or "").strip()
    if p.startswith("\\\\?\\"):
        p = p[4:]
    try:
        return os.path.normcase(os.path.normpath(p)) if p else ""
    except Exception:
        return p.lower()


def _norm_ws(p: str) -> str:
    """The workspace, as Codex records it: long names (no DAMIAN~1), then _norm."""
    try:
        if p and os.path.exists(p):
            p = os.path.realpath(p)
    except Exception:
        pass
    return _norm(p)


def _plain(p: str) -> str:
    return p[4:] if (p or "").startswith("\\\\?\\") else (p or "")


def _session_files(days: int = 45) -> list:
    cutoff = time.time() - days * 86400
    out = []
    for f in (codex_home() / "sessions").glob("*/*/*/rollout-*.jsonl"):
        try:
            if f.stat().st_mtime >= cutoff:
                out.append(f)
        except Exception:
            continue
    return out


def _events(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            yield d.get("type"), d.get("payload") or {}


def _user_text(item: dict) -> str:
    return " ".join(c.get("text", "") for c in item.get("content") or []
                    if isinstance(c, dict) and c.get("type") == "text").strip()


def chat_exists(thread_id: str) -> bool:
    """Is this chat still there (not deleted or archived)? If Codex's store can't be read, assume so and let Codex
    say. (History's Open in.)"""
    try:
        row = _db("state").execute("select archived from threads where id = ?", (thread_id,)).fetchone()
    except Exception:
        return True
    return row is not None and not row[0]


def _db(prefix: str):
    """Codex's own SQLite store (state_N / thread_history_N), newest version, read-only."""
    import sqlite3
    dbs = sorted(codex_home().glob(f"{prefix}_*.sqlite"), key=lambda f: _ver(f.stem))
    if not dbs:
        raise FileNotFoundError(prefix)
    return sqlite3.connect(f"file:{dbs[-1].as_posix()}?mode=ro", uri=True, timeout=2)


def _strip_recap(text: str) -> str:
    """A handed-over chat starts with the recap block — its title is the request after it."""
    from app.core.agent_sessions import strip_speak_note
    text = strip_speak_note(text)
    if text.startswith("[You're picking up a conversation") and "]\n\n" in text:
        return text.split("]\n\n", 1)[1]
    return text


def codex_threads(workspace: str, everywhere: bool = False) -> list:
    """Your Codex conversations from Codex's own index (the Codex app keeps most chats only there): the ones
    Filect ran in `workspace`, plus — `everywhere` — your Codex app and terminal chats, so "use my conversation
    about X" can find them. Codex's titles; newest first. Falls back to the files."""
    from app.core.agent_sessions import SessionInfo, generate_title
    try:
        rows = _db("state").execute(
            "select id, cwd, coalesce(nullif(name, ''), title, ''), coalesce(first_user_message, preview, ''), "
            "updated_at, source from threads where archived = 0 and coalesce(first_user_message, '') != ''"
        ).fetchall()                                 # (has_user_event is 0 on every row — not usable)
    except Exception:
        return list_codex_sessions(workspace)
    ws, out = _norm_ws(workspace), []
    for tid, cwd, title, first, updated, source in rows:
        in_ws = _norm(cwd) == ws
        if not (in_ws or (everywhere and source in ("vscode", "cli"))):
            continue                                 # (subagents, Codex's reviewer, other tools' runs)
        first = _strip_recap(first or title or "")
        if source == "exec":                         # Filect's: named like the drawer names them
            title = generate_title(first) if first else title
        folder = _plain(cwd) or workspace
        out.append(SessionInfo(id=tid, cwd=folder, title=(title or "Untitled")[:90],
                               preview=" ".join(first.split())[:220], mtime=float(updated or 0),
                               project=Path(folder).name, agent="codex"))
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def list_codex_sessions(cwd: str, days: int = 45) -> list:
    """Codex chats that ran in `cwd` (Filect's workspace), newest first, as agent_sessions.SessionInfo."""
    from app.core.agent_sessions import SessionInfo, generate_title
    want = _norm_ws(cwd)
    out = []
    for f in _session_files(days):
        try:
            first, sid = "", ""
            for i, (t, p) in enumerate(_events(f)):
                if i == 0:
                    if t != "session_meta" or _norm(p.get("cwd") or "") != want:
                        break
                    sid = p.get("id") or ""
                it = p.get("item") or {}
                if t == "event_msg" and p.get("type") == "item_completed" and it.get("type") == "UserMessage":
                    first = _user_text(it)
                    break
            if first and sid:
                first = _strip_recap(first)
                preview = " ".join(first.split())[:220]
                out.append(SessionInfo(id=sid, cwd=cwd, title=generate_title(first), preview=preview,
                                       mtime=f.stat().st_mtime, project=Path(cwd).name, agent="codex"))
        except Exception:
            continue
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def _as_rows(steps: list) -> list:
    rows = []
    for st in _merge_rewrites(steps):
        if st.kind == "text":
            rows.append(("text", st.text))
        elif st.kind == "tool":
            rows.append(("tool", st.tool, st.input, st.tool_id))
        else:
            rows.append(("result", st.tool_id, st.text, st.is_error))
    return rows


def _db_transcript(session_id: str) -> Optional[dict]:
    """A chat Codex keeps only in its database (most Codex app chats; old files get pruned)."""
    try:
        db = _db("thread_history")
        rows = db.execute("select item_json from thread_items where thread_id = ? order by rollout_ordinal",
                          (session_id,)).fetchall()
        last = db.execute("select duration_ms from thread_turns where thread_id = ? order by rollout_ordinal "
                          "desc limit 1", (session_id,)).fetchone()
    except Exception:
        return None
    if not rows:
        return None
    out, pending = [], []
    for (raw,) in rows:
        try:
            it = json.loads(raw)
        except Exception:
            continue
        if it.get("type") == "userMessage":
            out += _as_rows(pending)
            pending = []
            if _user_text(it):
                out.append(("user", _strip_recap(_user_text(it))))
            continue
        pending += _item_steps(it, True, saved=True)
    out += _as_rows(pending)
    return {"steps": out, "duration": (last[0] / 1000.0) if last and last[0] else None}


def load_codex_transcript(session_id: str) -> Optional[dict]:
    """A saved Codex chat in load_transcript()'s format: {"steps": [...], "duration": last turn secs} — from its
    file, else from Codex's database."""
    f = next(iter((codex_home() / "sessions").glob(f"*/*/*/rollout-*-{session_id}.jsonl")), None)
    if f is None:
        return _db_transcript(session_id)
    out, pending, dur = [], [], None
    try:
        for t, p in _events(f):
            if t != "event_msg":
                continue
            if p.get("type") == "task_complete" and p.get("duration_ms"):
                dur = p["duration_ms"] / 1000.0
            if p.get("type") != "item_completed":
                continue
            it = p.get("item") or {}
            if it.get("type") == "UserMessage":
                out += _as_rows(pending)
                pending = []
                if _user_text(it):
                    out.append(("user", _strip_recap(_user_text(it))))
                continue
            pending += _item_steps(it, True, saved=True)
        out += _as_rows(pending)
    except Exception:
        return None
    return {"steps": out, "duration": dur}
