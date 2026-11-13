"""Two things the full feature test found (2026-11-13):
  · Editing a cell in the Analyzed Files list was never saved: the handler read the file's id from the ✓ cell (it's on
    the File Name cell), and its column numbers were one off (from before the ✓ column) - fixing the id alone would have
    saved a Label into Tags. Now edits find their column by name.
  · The Search page's Type and Date filters were made hidden and never placed on the page, though the search used them
    and the tour promised them. Now they show with the results; Clear Filters shows while one is on.
The real list rendering and handlers on a stand-in window; a temporary index - your real one is never touched."""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from PySide6.QtWidgets import QApplication, QLabel, QTableWidget, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])
import app.ui.main_window as mwm  # noqa: E402
from app.core.database import FileIndex, _parse_tags_value  # noqa: E402

MW = mwm.MainWindow
TMP = Path(tempfile.mkdtemp(prefix="filect-listedit-"))
F = TMP / "delta note.txt"
F.write_text("delta", encoding="utf-8")


@pytest.fixture
def setup():
    idx = FileIndex(TMP / f"{os.urandom(4).hex()}.db")
    idx.add_file({"source_path": str(F), "name": F.name, "extension": ".txt", "size": 5, "category": "Documents",
                  "tags": ["delta", "notes"], "caption": "a delta note", "label": "note", "purpose": "testing"})
    page = QWidget()
    w = SimpleNamespace(debug_table=QTableWidget(0, len(mwm.DEBUG_COLUMNS)), debug_selection_count_label=QLabel(page),
                        debug_quick_actions_widget=QWidget(page), debug_info_label=QLabel(page),
                        status_bar=mock.MagicMock(), _page=page, _populating_debug_table=False)
    w.debug_table.setHorizontalHeaderLabels(mwm.DEBUG_COLUMNS)
    for m in ("_render_debug_table", "_get_selected_files", "_on_debug_selection_changed"):
        setattr(w, m, (lambda name: lambda *a, **k: getattr(MW, name)(w, *a, **k))(m))
    for m in ("copy_path_to_clipboard", "open_file_in_os"):
        setattr(w, m, mock.MagicMock())
    import sqlite3
    with sqlite3.connect(idx.db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM files").fetchall()
    with mock.patch.object(mwm, "file_index", idx):
        w._render_debug_table(rows)
        yield w, idx


def _edit(w, idx, column, text):
    item = w.debug_table.item(0, mwm.DEBUG_COLUMNS.index(column))
    with mock.patch.object(mwm, "file_index", idx):
        item.setText(text)                                 # (as the list's edit popup does → itemChanged)
        MW.on_debug_cell_changed(w, item)
    return idx.get_file_by_path(str(F))


def test_the_columns_are_where_the_list_puts_them(setup):
    w, idx = setup
    t = w.debug_table
    assert t.item(0, mwm.DEBUG_COLUMNS.index("File Name")).text() == "delta note.txt"
    assert t.item(0, mwm.DEBUG_COLUMNS.index("Label")).text() == "note"
    assert "delta" in t.item(0, mwm.DEBUG_COLUMNS.index("Tags")).text()


def test_editing_the_label_saves_the_label_and_nothing_else(setup):
    w, idx = setup
    rec = _edit(w, idx, "Label", "my own label")
    assert rec["label"] == "my own label", rec["label"]
    assert _parse_tags_value(rec["tags"]) == ["delta", "notes"], "a Label edit must never touch the tags"


def test_editing_tags_and_caption_go_to_their_own_fields(setup):
    w, idx = setup
    rec = _edit(w, idx, "Tags", "delta, my-trip")
    assert _parse_tags_value(rec["tags"]) == ["delta", "my-trip"] and rec["caption"] == "a delta note"
    rec = _edit(w, idx, "Caption", "a new caption")
    assert rec["caption"] == "a new caption" and rec["label"] == "note"


def test_a_tick_or_a_read_only_cell_saves_nothing(setup):
    w, idx = setup
    with mock.patch.object(idx, "update_file_field") as upd, mock.patch.object(idx, "set_tags_by_user") as tags:
        for column in ("✓", "Has OCR", "Category", "File Path"):
            _edit(w, idx, column, "x")
    assert not upd.called and not tags.called


def test_a_double_click_opens_one_editor_the_popup():
    """The table's own in-cell editor is off: a double-click opened it AND the popup (two editors at once)."""
    from real_window import RealWindow
    rw = RealWindow()
    try:
        assert rw.W.debug_table.editTriggers() == QTableWidget.NoEditTriggers
    finally:
        rw.close()


def test_the_edit_popup_lets_you_edit_exactly_those_columns():
    assert set(mwm.DEBUG_EDITABLE) == {"Label", "Tags", "Caption", "Purpose", "Suggested Filename"}
    assert all(c in mwm.DEBUG_COLUMNS for c in mwm.DEBUG_EDITABLE)
    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "editable_columns = {4:" not in src, "the old one-off column numbers are back"


# ---- the Search page's filters ------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def win():
    from real_window import RealWindow
    rw = RealWindow()
    yield rw.W
    rw.close()


def test_the_filters_are_on_the_page_with_the_results(win):
    w = win
    assert w.type_filter.parent() is not None and w.type_filter.window() is w, "the Type filter isn't on the page"
    w.results_container.setVisible(True)
    w.page_stack.setCurrentIndex(0)
    app.processEvents()
    assert w.type_filter.isVisibleTo(w.results_container) and w.date_filter.isVisibleTo(w.results_container)
    assert not w.clear_filters_btn.isVisibleTo(w.results_container), "Clear Filters with no filter on"


def test_clear_filters_shows_while_a_filter_is_on(win):
    w = win
    w.results_container.setVisible(True)
    with mock.patch.object(w, "search_files"):
        w.type_filter.setCurrentText("Images")
        assert w.clear_filters_btn.isVisibleTo(w.results_container)
        w.clear_filters_btn.click()
        assert w.type_filter.currentText() == "All Types" and not w.clear_filters_btn.isVisibleTo(w.results_container)
