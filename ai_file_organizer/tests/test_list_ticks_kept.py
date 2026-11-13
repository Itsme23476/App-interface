"""View Analyzed Files: ticked rows survive the list reloading. Live (Mac 2026-11-12; Windows stage 28, 2026-11-13): tick
2 files, Add Tags → the list reloads (as after Re-index, Remove, a search, or an analysis finishing) with every row
unticked, but the action bar still said "2 files selected"; Re-index, Open Folder, Export and Remove then did nothing.
Now the ticks are kept (by path: Re-index re-inserts the row with a new id) and the bar always matches them.
Real MainWindow code on a stand-in window. Mac a9cad3d."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QTableWidget, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])
import app.ui.main_window as mwm  # noqa: E402

MW = mwm.MainWindow


class Row(dict):
    """Like sqlite3.Row: row["x"] and row.keys()."""


def row(i, name):
    return Row({"id": i, "file_name": name, "file_path": f"C:\\Users\\x\\Desktop\\List Test\\{name}", "metadata": "{}",
                "tags": "[]", "caption": "", "label": "", "category": "Documents/Text", "file_size": 1, "ocr_text": "",
                "has_ocr": 0, "ai_source": "", "vision_confidence": None, "user_tags": "[]"})


ROWS = [row(i, n) for i, n in enumerate(["apple notes.txt", "banana notes.txt", "cherry notes.txt", "date notes.txt",
                                         "elder notes.txt"], 1)]


def window():
    page = QWidget()
    w = SimpleNamespace(debug_table=QTableWidget(0, 16), debug_selection_count_label=QLabel(page),
                        debug_quick_actions_widget=QWidget(page), debug_info_label=QLabel(page),
                        status_bar=mock.MagicMock(), _page=page)
    w.debug_table.itemChanged.connect(lambda item: MW._on_debug_selection_changed(w, item))
    for m in ("_render_debug_table", "_get_selected_file_ids", "_get_selected_files", "_on_debug_selection_changed"):
        setattr(w, m, (lambda name: lambda *a, **k: getattr(MW, name)(w, *a, **k))(m))
    for m in ("copy_path_to_clipboard", "open_file_in_os"):
        setattr(w, m, mock.MagicMock())
    return w


def tick(w, *names):
    t = w.debug_table
    for r in range(t.rowCount()):
        if t.item(r, 1).text() in names:
            t.item(r, 0).setCheckState(Qt.Checked)


def bar(w):
    return (not w.debug_quick_actions_widget.isHidden(), w.debug_selection_count_label.text())


def test_ticks_survive_a_reload():
    w = window()
    w._render_debug_table(ROWS)
    tick(w, "banana notes.txt", "date notes.txt")
    assert bar(w) == (True, "2 files selected"), bar(w)
    w._render_debug_table(ROWS)                                   # Add Tags done → the list reloads
    assert sorted(w._get_selected_file_ids("debug")) == [2, 4], w._get_selected_file_ids("debug")
    assert bar(w) == (True, "2 files selected"), bar(w)
    w._render_debug_table([r for r in ROWS if r["id"] != 2])       # banana removed → reload
    assert w._get_selected_file_ids("debug") == [4] and bar(w) == (True, "1 file selected"), bar(w)


def test_ticks_survive_new_ids_after_a_reindex():
    w = window()
    w._render_debug_table(ROWS)
    tick(w, "banana notes.txt", "date notes.txt")
    reread = [Row({**r, "id": r["id"] + 100}) for r in ROWS]       # Re-index re-inserts the rows: new ids
    w._render_debug_table(reread)
    assert sorted(w._get_selected_file_ids("debug")) == [102, 104] and bar(w) == (True, "2 files selected"), bar(w)
    assert sorted(f["file_name"] for f in w._get_selected_files("debug")) == ["banana notes.txt", "date notes.txt"]


def test_nothing_ticked_hides_the_bar_after_a_reload():
    w = window()
    w._render_debug_table(ROWS)
    tick(w, "cherry notes.txt")
    assert bar(w) == (True, "1 file selected"), bar(w)
    w._render_debug_table([r for r in ROWS if r["id"] != 3])       # the ticked one is gone (Remove)
    assert w._get_selected_file_ids("debug") == [] and bar(w)[0] is False, bar(w)


def test_a_first_load_ticks_nothing():
    w = window()
    w._render_debug_table(ROWS)
    assert w._get_selected_file_ids("debug") == [] and bar(w)[0] is False, bar(w)
