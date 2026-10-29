"""Verify the full-text search index stays in sync (no drift) after every kind of change.

The trigger-based FTS maintenance must leave the index identical to what a clean rebuild from the
`files` content would produce — after an insert, a move, an edit, a delete, and a re-index. This is
the test for the move/edit/delete drift fix.

Run it:
    cd ai_file_organizer
    PYTHONPATH=. python -m pytest tests/test_fts_triggers.py -v
"""
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.core.database import FileIndex

# Distinctive tokens we search for before/after each operation.
PROBES = ["alphaword", "oldalpha", "newalpha", "betaword", "editcap",
          "delword", "gammaword", "reindexold", "reindexnew", "keepword"]


def _matchset(c):
    """For each probe term, the set of rowids the FTS index currently matches."""
    return {t: tuple(sorted(r[0] for r in c.execute(
        "SELECT rowid FROM files_fts WHERE files_fts MATCH ?", (t,)))) for t in PROBES}


def _assert_no_drift(c, label):
    """The live index must equal a clean rebuild — if it doesn't, a stale term leaked somewhere."""
    live = _matchset(c)
    c.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
    c.connection.commit()
    rebuilt = _matchset(c)
    assert live == rebuilt, f"{label}: index DRIFTED from a clean rebuild\n  live={live}\n  rebuilt={rebuilt}"


def _ins(c, i, path, name, ocr):
    c.execute("INSERT INTO files(id,file_path,file_name,file_extension,file_size,mime_type,"
              "category,ocr_text,caption,tags,has_ocr) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (i, path, name, ".txt", 1, "text/plain", "docs", ocr, "", "[]", 0))
    c.connection.commit()


@pytest.fixture
def idx():
    tmp = tempfile.mktemp(suffix=".db")
    fi = FileIndex(Path(tmp))            # creates schema + the FTS sync triggers
    db = sqlite3.connect(tmp)
    try:
        yield fi, db, db.cursor()
    finally:
        db.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(tmp + ext)
            except OSError:
                pass


def test_no_drift_on_insert_move_edit_delete_reindex(idx):
    fi, db, c = idx
    _ins(c, 1, "/oldalpha/a.txt", "a.txt", "alphaword keepword")
    _ins(c, 2, "/x/b.txt", "b.txt", "betaword")
    _assert_no_drift(c, "after inserts")

    # MOVE: the old folder name must stop matching, the new one must match (this is the bug fix)
    c.execute("UPDATE files SET file_path='/newalpha/a.txt' WHERE id=1")
    db.commit()
    assert _matchset(c)["oldalpha"] == (), "old path still matched after a move (drift!)"
    assert _matchset(c)["newalpha"] == (1,)
    _assert_no_drift(c, "after move")

    # EDIT
    c.execute("UPDATE files SET caption='editcap' WHERE id=2")
    db.commit()
    assert _matchset(c)["editcap"] == (2,)
    _assert_no_drift(c, "after edit")

    # DELETE
    c.execute("DELETE FROM files WHERE id=2")
    db.commit()
    assert _matchset(c)["betaword"] == ()
    _assert_no_drift(c, "after delete")

    # RE-INDEX (INSERT OR REPLACE changes the row id; recursive_triggers removes the old entry)
    c.execute("PRAGMA recursive_triggers=ON")
    _ins(c, 3, "/y/c.txt", "c.txt", "reindexold")
    c.execute("INSERT OR REPLACE INTO files(file_path,file_name,file_extension,file_size,mime_type,"
              "category,ocr_text,caption,tags,has_ocr) VALUES('/y/c.txt','c.txt','.txt',1,'t','docs',"
              "'reindexnew','','[]',0)")
    db.commit()
    assert _matchset(c)["reindexold"] == (), "old content still matched after re-index (drift!)"
    _assert_no_drift(c, "after re-index")


def test_no_drift_through_the_real_methods(idx):
    fi, db, c = idx
    _ins(c, 10, "/foo/d.txt", "d.txt", "gammaword")
    _ins(c, 11, "/foo/e.txt", "e.txt", "delword")

    assert fi.update_file_path(10, "/bar/d.txt") is True
    _assert_no_drift(c, "update_file_path")

    assert fi.update_file_paths_batch([(10, "/baz/d.txt")]) == 1
    _assert_no_drift(c, "update_file_paths_batch")

    assert fi.update_file_field(10, "caption", "newcap") is True
    _assert_no_drift(c, "update_file_field")

    assert fi.delete_file(11) is True
    _assert_no_drift(c, "delete_file")
