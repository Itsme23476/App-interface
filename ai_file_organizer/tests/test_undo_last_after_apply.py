"""↩ Undo Last right after an Apply. Found in the full feature test (2026-11-13): Apply said "Organization complete!
(Undo available)" / "You can use Undo to reverse this" - then cleared the plan, which hid Undo Last (only History →
Revert was left). Now it stays until there's nothing to undo. The real Organize page, off-screen; temp files only."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication.instance() or QApplication([])
import app.ui.organize_page as opm  # noqa: E402


@pytest.fixture(scope="module")
def page():
    from real_window import RealWindow
    rw = RealWindow()
    yield rw.W.organize_page
    rw.close()


def _applied(page, tmp):
    """As an Apply leaves it: a.txt moved into Notes/, the worker's success handed to the page."""
    src, dst = tmp / "a.txt", tmp / "Notes" / "a.txt"
    dst.parent.mkdir(exist_ok=True)
    dst.write_text("a", encoding="utf-8")
    page.current_plan = {"folders": {"Notes": [1]}}
    page.current_moves = [{"source_path": str(src), "destination_path": str(dst), "file_id": 1}]
    page.undo_button.setEnabled(False)
    with mock.patch.object(opm.ModernInfoDialog, "show_info") as done:
        page._on_manual_apply_finished({"success": True, "errors": [], "log_file": "x.log"})
    assert done.called
    return src, dst


def test_undo_last_is_there_after_an_apply(page):
    tmp = Path(tempfile.mkdtemp(prefix="filect-undo-"))
    _applied(page, tmp)
    assert page.current_plan is None, "the plan is cleared after an Apply"
    assert page.undo_button.isVisibleTo(page) and page.undo_button.isEnabled(), "the Undo it promises is hidden"


def test_using_it_puts_the_file_back_and_it_goes(page):
    tmp = Path(tempfile.mkdtemp(prefix="filect-undo-"))
    src, dst = _applied(page, tmp)
    with mock.patch.object(opm.QMessageBox, "question", return_value=QMessageBox.Yes), \
            mock.patch.object(opm.QMessageBox, "information"), mock.patch.object(opm.QMessageBox, "warning"):
        page.undo_button.click()
    assert src.exists() and not dst.exists(), "Undo Last didn't move it back"
    assert not page.undo_button.isVisibleTo(page) and not page.undo_button.isEnabled()


def test_clear_with_nothing_to_undo_hides_it(page):
    page.last_organization = None
    page.clear_plan()
    assert not page.undo_button.isVisibleTo(page)


def test_a_voice_organize_doesnt_show_the_pages_undo(page):
    """(The voice assistant offers its own Undo; that path never enabled this button - unchanged.)"""
    page.last_organization = [{"source": "s", "destination": "d", "file_id": 1}]
    page.undo_button.setEnabled(False)
    page.clear_plan()
    assert not page.undo_button.isVisibleTo(page)
