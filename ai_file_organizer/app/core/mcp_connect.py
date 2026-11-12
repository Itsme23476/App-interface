"""Connect / disconnect the Filect MCP server in Claude Desktop and Codex (Windows port).

Powers the Settings cards: one click writes (or removes) the Filect server entry so the user
never edits config by hand. Claude Desktop is a JSON config we merge directly; Codex is driven
through its own `codex mcp add/remove/get` CLI (which writes ~/.codex/config.toml — the same file
the Codex CLI *and* the ChatGPT/Codex desktop app both read). Other servers are left untouched.

Windows adaptations of the macOS original (Mac 8f062de): the Claude Desktop config lives under
%APPDATA%\\Claude for the installer version, but elsewhere for the Microsoft Store version (see
claude_config_path); the launch command uses the console python (python.exe, never pythonw.exe, so
stdio pipes work); Codex binary is resolved via PATH + .exe/.cmd fallbacks (config is still the
cross-platform ~/.codex/config.toml).
"""
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SERVER_KEY = "filect"
_CLAUDE_CFG = "claude_desktop_config.json"


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))


def _roaming() -> Path:
    return Path(os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming"))


def _store_claude_ids() -> list:
    """The package ids of a Microsoft Store Claude Desktop (%LOCALAPPDATA%\\Packages\\Claude_<id>)."""
    try:
        return sorted(p.name.split("_", 1)[1] for p in (_local() / "Packages").glob("Claude_*")
                      if p.is_dir() and "_" in p.name)
    except OSError:
        return []


def claude_config_path() -> Path:
    """The claude_desktop_config.json that the installed Claude Desktop actually reads.

    * The installer version reads %APPDATA%\\Claude\\claude_desktop_config.json.
    * The Microsoft Store version never reads that file (a known Claude bug: its "Edit Config"
      even opened it). Newer Store versions keep their data in %LOCALAPPDATA%\\Claude-Data\\claude_<id>;
      older ones in the package's private %APPDATA% (Packages\\Claude_<id>\\LocalCache\\Roaming\\Claude
      — left behind with a ".user-data-left" marker once the data has moved).
    Writing only %APPDATA%\\Claude made 'Connect' look done while the Store Claude never started Filect."""
    for pid in _store_claude_ids():
        moved = _local() / "Claude-Data" / f"claude_{pid.lower()}"
        if moved.is_dir():
            return moved / _CLAUDE_CFG
        virt = _local() / "Packages" / f"Claude_{pid}" / "LocalCache" / "Roaming" / "Claude"
        try:
            if virt.is_dir() and not any(virt.glob(".user-data-left*")):
                return virt / _CLAUDE_CFG
        except OSError:
            pass
    return _roaming() / "Claude" / _CLAUDE_CFG


def _all_claude_config_paths() -> list:
    """Every Claude config Filect may have written (the one in use first), for a clean Disconnect."""
    out = [claude_config_path(), _roaming() / "Claude" / _CLAUDE_CFG]
    for pid in _store_claude_ids():
        out += [_local() / "Claude-Data" / f"claude_{pid.lower()}" / _CLAUDE_CFG,
                _local() / "Packages" / f"Claude_{pid}" / "LocalCache" / "Roaming" / "Claude" / _CLAUDE_CFG]
    seen, uniq = set(), []
    for p in out:
        k = os.path.normcase(str(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _python_exe() -> str:
    """The CONSOLE python for launching the stdio server. pythonw.exe has no real stdio and
    breaks the MCP pipe, so map it to python.exe when the app is running under pythonw."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def mcp_command():
    """(command, args, env) the client should use to launch the Filect MCP server."""
    if getattr(sys, "frozen", False):
        # Shipped app: the Filect .exe IS the MCP server when launched in MCP mode. FILECT_MODE
        # (env) selects the mode; the frozen build can drop sys.argv, so the flag alone is a hint.
        # (Wired in the packaging phase.)
        return str(Path(sys.executable).resolve()), ["--mcp-server"], {
            "QT_QPA_PLATFORM": "offscreen", "FILECT_MODE": "mcp-server"}
    # Running from source: the console python + the server script. server.py puts ai_file_organizer
    # on sys.path itself; PYTHONPATH=repo_root also makes `filect_mcp` importable.
    repo_root = Path(__file__).resolve().parents[3]   # core -> app -> ai_file_organizer -> win
    server = repo_root / "filect_mcp" / "server.py"
    return _python_exe(), [str(server)], {
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONPATH": str(repo_root),
    }


def _load_config(p=None) -> dict:
    p = p or claude_config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning(f"Claude config unreadable ({e}); treating as empty")
    return {}


def is_connected() -> bool:
    return SERVER_KEY in (_load_config().get("mcpServers") or {})


def connect():
    """Add the Filect MCP server to Claude Desktop's config (safe merge). (ok, message)."""
    try:
        p = claude_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        cfg = _load_config(p)
        cmd, args, env = mcp_command()
        entry = {"command": cmd}
        if args:
            entry["args"] = args
        if env:
            entry["env"] = env
        cfg.setdefault("mcpServers", {})[SERVER_KEY] = entry
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        logger.info(f"Filect MCP server added to Claude Desktop config ({p})")
        return True, "Connected. Quit and reopen Claude Desktop to finish."
    except Exception as e:
        logger.error(f"MCP connect failed: {e}")
        return False, f"Could not write the Claude config: {e}"


def disconnect():
    """Remove only the Filect server from Claude Desktop's config — from every config Filect may
    have written (an older Filect wrote %APPDATA%\\Claude even for the Store Claude). (ok, message)."""
    try:
        for p in _all_claude_config_paths():
            if not p.exists():
                continue
            cfg = _load_config(p)
            servers = cfg.get("mcpServers") or {}
            if SERVER_KEY in servers:
                del servers[SERVER_KEY]
                p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return True, "Disconnected. Quit and reopen Claude Desktop to apply."
    except Exception as e:
        logger.error(f"MCP disconnect failed: {e}")
        return False, f"Could not update the Claude config: {e}"


# ---------------- Codex (CLI + ChatGPT desktop app share ~/.codex/config.toml) ----------------

def _codex_bin():
    """Path to a usable `codex` executable, or None. Checks PATH (resolves .exe/.cmd), then the
    common standalone-install locations on Windows."""
    found = shutil.which("codex")
    if found:
        return found
    cands = [
        Path.home() / ".local" / "bin" / "codex.exe",
        Path.home() / ".local" / "bin" / "codex",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "Programs" / "codex" / "codex.exe")
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands.append(Path(appdata) / "npm" / "codex.cmd")
    for p in cands:
        if p.exists():
            return str(p)
    return None


def codex_config_path() -> Path:
    """Codex's config file (read by both the `codex` CLI and the ChatGPT/Codex desktop app)."""
    return Path.home() / ".codex" / "config.toml"


def codex_available() -> bool:
    """True if Codex is installed — either the `codex` CLI, or the desktop app (which ships no CLI
    but keeps ~/.codex/config.toml). So the 'Connect to Codex' card shows for both."""
    return _codex_bin() is not None or codex_config_path().exists() or (Path.home() / ".codex").is_dir()


def _run_codex(args, timeout: float = 20.0):
    """Run `codex <args>` headlessly. Returns the CompletedProcess, or None if codex is missing
    or couldn't be launched. Never raises; never shows UI."""
    bin_ = _codex_bin()
    if not bin_:
        return None
    try:
        # CREATE_NO_WINDOW keeps a console from flashing up on Windows.
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        cmd = [bin_, *args]
        # A .cmd/.bat shim (e.g. an npm-installed codex on Windows) can't be launched directly by
        # CreateProcess — run it through cmd.exe so the shim resolves.
        if sys.platform == "win32" and bin_.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c", bin_, *args]
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, creationflags=creationflags)
    except Exception as e:
        logger.warning(f"codex invocation failed: {e}")
        return None


def _codex_toml_connected() -> bool:
    """Is the Filect server present in ~/.codex/config.toml (the desktop-app path)?"""
    try:
        import tomllib
        p = codex_config_path()
        if not p.exists():
            return False
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        return SERVER_KEY in (data.get("mcp_servers") or {})
    except Exception:
        return False


def codex_is_connected() -> bool:
    if _codex_bin() is not None:
        r = _run_codex(["mcp", "get", SERVER_KEY])
        if r is not None:
            return bool(r.returncode == 0)
    return _codex_toml_connected()


def _codex_toml_connect():
    """Write [mcp_servers.filect] into ~/.codex/config.toml, preserving everything else (tomlkit
    keeps the user's comments, tables and literal strings intact)."""
    try:
        import tomlkit
    except Exception as e:
        return False, f"Cannot edit the Codex config (tomlkit missing: {e})."
    cmd, args, env = mcp_command()
    p = codex_config_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = tomlkit.parse(p.read_text(encoding="utf-8")) if p.exists() else tomlkit.document()
        servers = doc.get("mcp_servers")
        if servers is None:
            servers = tomlkit.table()
            doc["mcp_servers"] = servers
        entry = tomlkit.table()
        entry["command"] = cmd
        if args:
            entry["args"] = args
        if env:
            envt = tomlkit.table()
            for k, v in env.items():
                envt[k] = v
            entry["env"] = envt
        entry["startup_timeout_sec"] = CODEX_STARTUP_TIMEOUT
        servers[SERVER_KEY] = entry
        p.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return True, "Connected. Restart the Codex / ChatGPT desktop app to finish."
    except Exception as e:
        logger.error(f"Codex TOML connect failed: {e}")
        return False, f"Could not update the Codex config: {e}"


# The installed Filect.exe is one file that unpacks itself on every start: measured 26-31 s before it answers as
# an MCP server (2026-11-11). Codex gives a server 10 s by default and then drops it for the session ("no Filect
# tool"), so Filect's entry asks Codex to wait longer — the same setting Codex's own bundled tools use (120 s).
CODEX_STARTUP_TIMEOUT = 120


def _codex_set_startup_timeout() -> bool:
    """Add startup_timeout_sec to the Filect entry `codex mcp add` wrote (the CLI has no flag for it)."""
    try:
        import tomlkit
        p = codex_config_path()
        doc = tomlkit.parse(p.read_text(encoding="utf-8"))
        entry = (doc.get("mcp_servers") or {}).get(SERVER_KEY)
        if entry is None:
            return False
        entry["startup_timeout_sec"] = CODEX_STARTUP_TIMEOUT
        p.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Couldn't set Codex's startup wait for Filect: {e}")
        return False


def codex_connect():
    """Register the Filect MCP server with Codex. Uses the `codex` CLI when present; otherwise
    writes ~/.codex/config.toml directly (the desktop app, which has no CLI). (ok, message)."""
    if _codex_bin() is not None:
        cmd, args, env = mcp_command()
        add = ["mcp", "add", SERVER_KEY]
        for k, v in (env or {}).items():
            add += ["--env", f"{k}={v}"]
        add += ["--", cmd, *args]
        # `mcp add` errors if the name already exists; remove first (idempotent) so re-connect works.
        _run_codex(["mcp", "remove", SERVER_KEY])
        r = _run_codex(add)
        if r and r.returncode == 0:
            _codex_set_startup_timeout()
            return True, "Connected. Restart the Codex / ChatGPT desktop app (or start a new Codex session)."
        err = ((r.stderr or r.stdout).strip() if r else "codex not runnable") or "unknown error"
        logger.error(f"Codex connect failed: {err}")
        return False, f"Could not add Filect to Codex: {err}"
    if codex_available():
        return _codex_toml_connect()
    return False, "Codex isn't installed."


def _codex_toml_disconnect():
    try:
        import tomlkit
        p = codex_config_path()
        if not p.exists():
            return True, "Nothing to disconnect."
        doc = tomlkit.parse(p.read_text(encoding="utf-8"))
        servers = doc.get("mcp_servers")
        if servers is not None and SERVER_KEY in servers:
            del servers[SERVER_KEY]
            p.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return True, "Disconnected from Codex. Restart the Codex / ChatGPT desktop app to apply."
    except Exception as e:
        logger.error(f"Codex TOML disconnect failed: {e}")
        return False, f"Could not update the Codex config: {e}"


def codex_disconnect():
    """Remove the Filect MCP server from Codex (CLI when present, else edit config.toml)."""
    if _codex_bin() is not None:
        r = _run_codex(["mcp", "remove", SERVER_KEY])
        if r and r.returncode == 0:
            return True, "Disconnected from Codex. Restart the Codex / ChatGPT desktop app to apply."
        err = ((r.stderr or r.stdout).strip() if r else "codex not runnable") or "unknown error"
        logger.error(f"Codex disconnect failed: {err}")
        return False, f"Could not remove Filect from Codex: {err}"
    return _codex_toml_disconnect()


if __name__ == "__main__":
    # offline self-check against a temp config (never touches the real one: APPDATA/LOCALAPPDATA
    # point into a temp folder, so even Disconnect's sweep of every location stays inside it)
    import tempfile
    _t = Path(tempfile.mkdtemp())
    os.environ["APPDATA"], os.environ["LOCALAPPDATA"] = str(_t / "Roaming"), str(_t / "Local")
    tmp = claude_config_path()
    assert is_connected() is False
    ok, _ = connect(); assert ok and is_connected()
    cfg = json.loads(tmp.read_text()); assert "filect" in cfg["mcpServers"]
    # a pre-existing server must survive
    cfg["mcpServers"]["other"] = {"command": "x"}; tmp.write_text(json.dumps(cfg))
    connect()  # re-connect
    cfg = json.loads(tmp.read_text()); assert "other" in cfg["mcpServers"] and "filect" in cfg["mcpServers"]
    ok, _ = disconnect(); assert ok and not is_connected()
    cfg = json.loads(tmp.read_text()); assert "other" in cfg["mcpServers"]  # only filect removed
    print("mcp_connect self-check passed")
