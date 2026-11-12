"""'Connect to Claude' must write the config the INSTALLED Claude Desktop reads.

The Microsoft Store Claude never reads %APPDATA%\\Claude\\claude_desktop_config.json; newer Store
versions read %LOCALAPPDATA%\\Claude-Data\\claude_<id>\\, older ones the package's private
%APPDATA%. Found live: Filect wrote %APPDATA%\\Claude, the card said "Connected", and the Store
Claude never started Filect. Every test runs in a temp APPDATA/LOCALAPPDATA (never the real ones).
"""
import json
import os

import pytest

from app.core import mcp_connect as mc

PID = "pzs8sxrjxfjjc"
CFG = "claude_desktop_config.json"


@pytest.fixture
def home(tmp_path, monkeypatch):
    roaming, local = tmp_path / "Roaming", tmp_path / "Local"
    roaming.mkdir()
    local.mkdir()
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return roaming, local


def _store(local, moved=True, old_virtual=False, left_marker=False):
    pkg = local / "Packages" / f"Claude_{PID}"
    (pkg / "LocalCache" / "Roaming" / "Claude").mkdir(parents=True)
    if left_marker:
        (pkg / "LocalCache" / "Roaming" / "Claude" / f".user-data-left.claude_{PID}").write_text("")
    if moved:
        (local / "Claude-Data" / f"claude_{PID}").mkdir(parents=True)
    return pkg


def _same(a, b):
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def test_installer_claude_uses_appdata(home):
    roaming, _ = home
    assert _same(mc.claude_config_path(), roaming / "Claude" / CFG)


def test_store_claude_with_moved_data_uses_claude_data(home):
    _, local = home
    _store(local, moved=True, left_marker=True)          # this PC: data moved out of the package
    assert _same(mc.claude_config_path(), local / "Claude-Data" / f"claude_{PID}" / CFG)


def test_older_store_claude_uses_the_package_private_appdata(home):
    _, local = home
    pkg = _store(local, moved=False)
    assert _same(mc.claude_config_path(), pkg / "LocalCache" / "Roaming" / "Claude" / CFG)


def test_left_behind_package_folder_is_not_used(home):
    roaming, local = home
    _store(local, moved=False, left_marker=True)          # moved somewhere we don't know: not the old place
    assert _same(mc.claude_config_path(), roaming / "Claude" / CFG)


def test_connect_writes_the_file_claude_reads_and_keeps_its_other_settings(home):
    roaming, local = home
    _store(local, moved=True, left_marker=True)
    used = local / "Claude-Data" / f"claude_{PID}" / CFG
    used.write_text(json.dumps({"preferences": {"sidebarMode": "epitaxy"},
                                "mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    ok, msg = mc.connect()
    assert ok, msg
    cfg = json.loads(used.read_text(encoding="utf-8"))
    assert cfg["preferences"] == {"sidebarMode": "epitaxy"}           # Claude's own settings survive
    assert cfg["mcpServers"]["other"] == {"command": "x"}              # and other servers
    entry = cfg["mcpServers"]["filect"]
    assert entry["command"].lower().endswith("python.exe") and entry["args"][0].endswith("server.py")
    assert entry["env"]["QT_QPA_PLATFORM"] == "offscreen"
    assert mc.is_connected()
    assert not (roaming / "Claude" / CFG).exists()                     # not the file the Store Claude ignores


def test_stale_appdata_entry_alone_is_not_connected_for_the_store_claude(home):
    roaming, local = home
    _store(local, moved=True, left_marker=True)
    (roaming / "Claude").mkdir()
    (roaming / "Claude" / CFG).write_text(json.dumps({"mcpServers": {"filect": {"command": "py"}}}))
    assert not mc.is_connected()           # what an older Filect wrote: the card must not say Connected


def test_disconnect_removes_filect_everywhere_and_nothing_else(home):
    roaming, local = home
    _store(local, moved=True, left_marker=True)
    used = local / "Claude-Data" / f"claude_{PID}" / CFG
    stale = roaming / "Claude" / CFG
    stale.parent.mkdir()
    for p in (used, stale):
        p.write_text(json.dumps({"preferences": {"a": 1},
                                 "mcpServers": {"filect": {"command": "py"}, "other": {"command": "x"}}}))
    ok, _ = mc.disconnect()
    assert ok and not mc.is_connected()
    for p in (used, stale):
        cfg = json.loads(p.read_text())
        assert "filect" not in cfg["mcpServers"] and cfg["mcpServers"]["other"] == {"command": "x"}
        assert cfg["preferences"] == {"a": 1}
    old = local / "Packages" / f"Claude_{PID}" / "LocalCache" / "Roaming" / "Claude" / CFG
    assert not old.exists()                # Disconnect never creates files in Claude's folders


def test_reconnect_is_idempotent(home):
    _, local = home
    _store(local, moved=True)
    assert mc.connect()[0] and mc.connect()[0]
    cfg = json.loads((local / "Claude-Data" / f"claude_{PID}" / CFG).read_text())
    assert list(cfg["mcpServers"]) == ["filect"]


# ---- Codex: the installed Filect.exe takes ~30 s to answer (one file that unpacks itself); Codex's default wait is
# 10 s, then it drops the server for the session (found live 2026-11-11: "NO FILECT TOOL"). The entry asks Codex to
# wait 120 s, both when Filect writes config.toml itself and after `codex mcp add`.

def test_codex_entry_written_by_filect_asks_codex_to_wait(tmp_path, monkeypatch):
    import tomllib
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "x"\n\n[mcp_servers.other]\ncommand = "o"\n', encoding="utf-8")
    monkeypatch.setattr(mc, "codex_config_path", lambda: cfg)
    ok, _ = mc._codex_toml_connect()
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert ok and data["mcp_servers"]["filect"]["startup_timeout_sec"] == mc.CODEX_STARTUP_TIMEOUT == 120
    assert data["mcp_servers"]["other"] == {"command": "o"} and data["model"] == "x", "the rest untouched"


def test_codex_cli_connect_then_adds_the_wait(tmp_path, monkeypatch):
    import tomllib
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(mc, "codex_config_path", lambda: cfg)
    monkeypatch.setattr(mc, "_codex_bin", lambda: "codex.exe")

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_cli(args, timeout=20.0):
        if args[:2] == ["mcp", "add"]:          # (what `codex mcp add` writes: no startup_timeout_sec)
            cfg.write_text('[mcp_servers.filect]\ncommand = "Filect.exe"\nargs = ["--mcp-server"]\n', encoding="utf-8")
        return R()
    monkeypatch.setattr(mc, "_run_codex", fake_cli)
    ok, _ = mc.codex_connect()
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert ok and data["mcp_servers"]["filect"]["startup_timeout_sec"] == 120
    assert data["mcp_servers"]["filect"]["args"] == ["--mcp-server"]
