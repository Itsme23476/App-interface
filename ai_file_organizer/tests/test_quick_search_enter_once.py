"""One Enter in Quick Search acts once. Live (full feature test, 2026-11-13): an Enter in the search box ran the whole
fill-in twice (the box's returnPressed, then the same key passed on to the window's own Enter handler) - 7 s with Filect
frozen, the File name box still empty 3 s after Enter. Worse in voice search: Enter opened the file, the hide put the
mode back to 'fill', and the passed-on Enter then ran a fill-in on the program you were in (your clipboard replaced
with the path; the fill-in methods select-all + type into that program's text box).
The real overlay; the fill-in itself is never run (its signal is recorded)."""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
import app.ui.quick_search_overlay as qso  # noqa: E402

ROWS = [{"file_path": r"C:\x\garden notes.txt", "file_name": "garden notes.txt", "category": "Documents",
         "size": 10, "modified_date": "2026-11-13"},
        {"file_path": r"C:\x\holiday plan.txt", "file_name": "holiday plan.txt", "category": "Documents",
         "size": 10, "modified_date": "2026-11-13"}]


@pytest.fixture
def overlay():
    o = qso.QuickSearchOverlay(None)
    got = []
    o.pathSelected.connect(got.append)
    with mock.patch.object(o, "restore_dialog_focus", return_value=(True, "test")), \
            mock.patch.object(o, "create_comprehensive_debug_report"), \
            mock.patch.object(o, "log_debug_target_window"), \
            mock.patch.object(qso.settings, "_save_config"):
        o._on_search_results(ROWS)
        o.show()
        app.processEvents()
        yield o, got
    o.hide()
    o.deleteLater()


def _enter(widget):
    widget.setFocus()
    app.processEvents()
    QTest.keyClick(widget, Qt.Key_Return)
    app.processEvents()


def test_enter_in_the_search_box_fills_once(overlay):
    o, got = overlay
    o.results.selectRow(0)
    _enter(o.input)
    assert got == [ROWS[0]["file_path"]], got


def test_enter_with_no_row_picked_fills_the_first_once(overlay):
    o, got = overlay
    o.results.setCurrentCell(-1, -1)
    o.results.clearSelection()
    assert o.results.currentRow() < 0
    _enter(o.input)
    assert got == [ROWS[0]["file_path"]], got


def test_enter_on_the_list_fills_that_row_once(overlay):
    o, got = overlay
    o.results.selectRow(1)
    _enter(o.results)
    assert got == [ROWS[1]["file_path"]], got


def test_voice_search_enter_opens_the_file_and_fills_nothing(overlay):
    o, got = overlay
    o._primary_mode = "open"                      # (as run_voice_query sets it)
    o.results.selectRow(0)
    _enter(o.input)
    assert got == [ROWS[0]["file_path"] + "||OPEN"], got


def test_voice_search_enter_on_the_list_opens_too(overlay):
    o, got = overlay
    o._primary_mode = "open"
    o.results.selectRow(1)
    _enter(o.results)
    assert got == [ROWS[1]["file_path"] + "||OPEN"], got
