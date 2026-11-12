"""Auto-Organize → Edit → Save applies a mode only to a folder whose mode was picked in that dialog, and never to a
paused folder. (Mac live test 2026-11-12: adding Tide Pool and pressing Save re-ran "Re-organize everything" on the
owner's paused `photos` folder — flatten and sort again from scratch.) Real dialog code, a fake watcher; nothing moves."""
import os
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.ui import organize_page as op  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="filect-watch-save-"))
SORTED, PAUSED, NEW = (os.path.normpath(str(TMP / n)) for n in ("Sorted Long Ago", "photos", "Tide Pool"))
for _d in (SORTED, PAUSED, NEW):
    os.makedirs(_d)
    Path(_d, "a.txt").write_text("x")


class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.auto_watcher = mock.MagicMock()


def picker(choice):
    class Fake:
        def __init__(self, *a, **k):
            self.result_choice = choice

        def exec(self):
            return 1
    return Fake


def run(monkeypatch, picks, start=None):
    """Open Edit on the list, add Tide Pool, pick modes as given, Save → the folders the watcher was told to sort."""
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)          # never the user's settings.json
    monkeypatch.setattr(settings, "auto_organize_folders", start if start is not None else [
        {"path": SORTED, "instruction": "by type", "action": 1},
        {"path": PAUSED, "instruction": "bears, dogs", "action": 1, "paused": True}])
    page = Page()
    dlg = op.WatchConfigDialog(page)
    with mock.patch.object(op.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: NEW)):
        dlg._add_folder()
    for path, choice in picks:
        with mock.patch.object(op, "ApplyInstructionsDialogSingleFolder", picker(choice)):
            dlg._show_folder_options(path)
    with mock.patch.object(dlg, "accept", lambda: None):
        dlg._save_and_close()
    told = {os.path.normpath(c.args[0]) for c in page.auto_watcher.organize_single_folder.call_args_list}
    return told, {os.path.normpath(f["path"]): f for f in settings.auto_organize_folders}


def test_adding_a_folder_doesnt_resort_the_others(monkeypatch):
    sorted_now, saved = run(monkeypatch, [(NEW, 3)])
    assert sorted_now == set(), f"Save re-sorted {sorted_now}"
    assert saved[PAUSED].get("paused") is True and saved[NEW]["action"] == 3 and saved[SORTED]["action"] == 1


def test_a_mode_picked_now_is_applied(monkeypatch):
    sorted_now, _ = run(monkeypatch, [(NEW, 1)])
    assert sorted_now == {NEW}, sorted_now
    sorted_now, _ = run(monkeypatch, [(SORTED, 2)])
    assert sorted_now == {SORTED}, sorted_now


def test_a_paused_folder_is_never_sorted_on_save(monkeypatch):
    sorted_now, saved = run(monkeypatch, [(PAUSED, 1)])
    assert PAUSED not in sorted_now, sorted_now
    assert saved[PAUSED].get("paused") is True
