"""Tags the person adds survive the file being read again. Live (Mac 2026-11-12; Windows stage 28, 2026-11-13): Add Tags
"list-tag" on 2 files, then Re-index → both lost list-tag (the fresh AI tags replaced all tags; user_tags existed but
was never written, and a re-read blanked it). Now Add Tags and tags typed in the Tags cell are kept in user_tags, and a
re-read puts them first among the new tags. Temporary databases only; the real index is never touched. Mac a9cad3d."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.database import FileIndex, _parse_tags_value  # noqa: E402
from app.core.file_operations import FileOperations  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="filect-usertags-"))
F = TMP / "date notes.txt"
F.write_text("number 3: dates", encoding="utf-8")


def meta(tags):
    """What a (re-)read hands add_file (the Windows keys: source_path, name, extension, size)."""
    return {"source_path": str(F), "name": F.name, "extension": ".txt", "size": F.stat().st_size,
            "category": "Documents/Text", "tags": tags, "caption": "notes about dates", "label": "note",
            "content_hash": "h"}


def tags_of(idx):
    with sqlite3.connect(idx.db_path) as c:
        tags, user = c.execute("SELECT tags, user_tags FROM files WHERE file_path = ?", (str(F),)).fetchone()
    return _parse_tags_value(tags) or [], _parse_tags_value(user) or []


def test_added_tags_survive_a_reindex():
    idx = FileIndex(TMP / "a.db")
    idx.add_file(meta(["date", "list test"]))
    fid = idx.get_file_by_path(str(F))["id"]
    FileOperations(idx).batch_add_tags([fid], ["list-tag"])
    assert "list-tag" in tags_of(idx)[0] and tags_of(idx)[1] == ["list-tag"], tags_of(idx)
    idx.add_file(meta(["date", "number 3", "calendar"]))         # Re-index: the AI's fresh tags
    tags, user = tags_of(idx)
    assert tags[0] == "list-tag" and "calendar" in tags and "list test" not in tags and user == ["list-tag"], (tags, user)
    idx.add_file(meta(["date"]))                                  # read again: still kept
    assert tags_of(idx) == (["list-tag", "date"], ["list-tag"]), tags_of(idx)


def test_tags_typed_in_the_cell_are_kept_and_removed_ones_go():
    idx = FileIndex(TMP / "b.db")
    idx.add_file(meta(["date", "list test"]))
    rec = idx.get_file_by_path(str(F))
    assert idx.set_tags_by_user(rec["id"], ["date", "my-trip"])  # typed my-trip, deleted "list test"
    assert tags_of(idx) == (["date", "my-trip"], ["my-trip"]), tags_of(idx)
    idx.add_file(meta(["date", "list test", "calendar"]))
    tags, user = tags_of(idx)
    assert tags[0] == "my-trip" and user == ["my-trip"], (tags, user)
    rec = idx.get_file_by_path(str(F))
    idx.set_tags_by_user(rec["id"], [t for t in tags if t != "my-trip"])   # now deletes it in the cell
    idx.add_file(meta(["date"]))
    assert "my-trip" not in tags_of(idx)[0] and tags_of(idx)[1] == [], tags_of(idx)


def test_no_user_tags_nothing_changes():
    idx = FileIndex(TMP / "c.db")
    idx.add_file(meta(["date"]))
    idx.add_file(meta(["date", "calendar"]))
    assert tags_of(idx) == (["date", "calendar"], []), tags_of(idx)


def test_a_kept_tag_isnt_doubled_by_the_ai_in_another_case():
    idx = FileIndex(TMP / "d.db")
    idx.add_file(meta(["date"]))
    FileOperations(idx).batch_add_tags([idx.get_file_by_path(str(F))["id"]], ["Invoice"])
    idx.add_file(meta(["invoice", "date"]))
    assert tags_of(idx)[0] == ["Invoice", "date"], tags_of(idx)


def test_editing_the_tags_cell_saves_them_as_yours():
    """The list's Tags cell (column 5) goes through set_tags_by_user, so what you type there survives a re-read."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem
    QApplication.instance() or QApplication([])
    import app.ui.main_window as mwm
    idx = FileIndex(TMP / "e.db")
    idx.add_file(meta(["date"]))
    fid = idx.get_file_by_path(str(F))["id"]
    t = QTableWidget(1, 16)
    c0 = QTableWidgetItem()
    c0.setData(Qt.UserRole, fid)
    t.setItem(0, 0, c0)
    cell = QTableWidgetItem("date, my-trip")
    t.setItem(0, 5, cell)
    w = SimpleNamespace(debug_table=t, status_bar=mock.MagicMock(), _populating_debug_table=False)
    with mock.patch.object(mwm, "file_index", idx):
        mwm.MainWindow.on_debug_cell_changed(w, cell)
    assert tags_of(idx) == (["date", "my-trip"], ["my-trip"]), tags_of(idx)
    idx.add_file(meta(["date", "calendar"]))
    assert tags_of(idx)[0][0] == "my-trip", tags_of(idx)
