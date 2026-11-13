"""A FOLDER given to the MCP file tools. Live (full feature test, 2026-11-13, real Codex): "analyze the files in this
folder" → Codex passed the folder to analyze_file → "Can't read this file ([Errno 13] Permission denied) … isn't
locked by another program" - and Codex stopped. Now the tools say it's a folder and list its files, so the client
carries on file by file. Temp folders and a temp index only; nothing is sent."""
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ai_file_organizer"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.settings import settings  # noqa: E402
from app.core import database  # noqa: E402
from filect_mcp import server as mcp  # noqa: E402

for _h in list(logging.getLogger().handlers):           # (importing the server logs to the user's real MCP log)
    if str(getattr(_h, "baseFilename", "")).endswith("filect_mcp.log"):
        logging.getLogger().removeHandler(_h)
        _h.close()


@pytest.fixture
def folder(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(database, "file_index", database.FileIndex(tmp_path / "index.db"))
    monkeypatch.setattr(mcp, "_require_access", lambda: None)
    d = tmp_path / "My Folder"
    (d / "sub").mkdir(parents=True)
    (d / "notes.txt").write_text("notes", encoding="utf-8")
    (d / "sub" / "receipt.txt").write_text("receipt", encoding="utf-8")
    (d / "desktop.ini").write_text("x", encoding="utf-8")         # (Windows' folder file: never listed)
    (d / ".hidden").write_text("x", encoding="utf-8")
    return d


@pytest.mark.parametrize("tool", ["analyze_file", "analyze_image", "transcribe_file", "get_file_details"])
def test_a_folder_says_so_and_lists_its_files(folder, tool):
    out = getattr(mcp, tool)(str(folder))
    assert "folder" in out.get("error", "") and "Permission denied" not in out.get("error", ""), out
    names = sorted(Path(f).name for f in out["files"])
    assert names == ["notes.txt", "receipt.txt"], names                # (no desktop.ini, no hidden file)
    assert out["folder"] == str(folder)


def test_a_real_file_is_still_read(folder):
    out = mcp.analyze_file(str(folder / "notes.txt"))
    assert "error" not in out and out.get("mode") == "raw_text_for_client" and "notes" in out.get("text", ""), out


def test_a_missing_path_is_still_not_found(folder):
    assert mcp.analyze_file(str(folder / "nope.txt")) == {"error": "file not found"}
