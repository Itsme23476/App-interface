"""The Organize page never touches what's inside an app, a program folder, a library or a code project, nor hidden
files (Mac live test 2026-11-12: it planned bakery-website/.git/HEAD and Tiny Tool.app/Contents/Info.plist, and Apply
moved them). And the ".git" exclusion really covers what's inside .git (every dot pattern of up to 5 letters was skipped
for folders). The page's own methods run on a stand-in page: no window, no watcher, nothing sent anywhere.
Windows adds a program folder (an .exe next to .dll files)."""
import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.ui import organize_page as op  # noqa: E402

ROOT = Path(tempfile.mkdtemp(prefix="filect-protect-"))
for _rel in ("notes.txt", "photo.jpg", ".hidden", "Invoices/march.pdf",
             "bakery-website/.git/HEAD", "bakery-website/index.html", "bakery-website/README.md",
             "Tiny Tool.app/Contents/Info.plist", "Tiny Tool.app/Contents/MacOS/tool",
             "Portable Tool/tool.exe", "Portable Tool/helper.dll", "Portable Tool/readme.txt"):
    (ROOT / _rel).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / _rel).write_text("x")
OK = {"notes.txt", "photo.jpg", os.path.join("Invoices", "march.pdf")}     # the only files Organize may touch
ALL = [str(q) for q in ROOT.rglob("*") if q.is_file()]


def rel(paths):
    return {str(Path(p).relative_to(ROOT)) for p in paths}


def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)          # never the user's settings.json
    monkeypatch.setattr(settings, "exclusion_patterns", settings._get_default_exclusions())   # (".git" among them)
    monkeypatch.setattr(settings, "pinned_paths", [])


def test_the_git_exclusion_covers_whats_inside_git(monkeypatch):
    _defaults(monkeypatch)
    assert settings.should_exclude(str(ROOT / "bakery-website" / ".git" / "HEAD")), ".git\\HEAD must be excluded"
    monkeypatch.setattr(settings, "exclusion_patterns", [".json"])
    assert settings.should_exclude(r"C:\x\settings.json"), "an extension still works"
    assert not settings.should_exclude(r"C:\x\json\notes.txt"), "...and doesn't catch a folder called json"
    monkeypatch.setattr(settings, "exclusion_patterns", [".venv"])
    assert settings.should_exclude(r"C:\x\proj\.venv\lib\site.py")
    assert settings._should_exclude_by_patterns(r"C:\x\proj\.venv\lib\site.py") if hasattr(
        settings, "_should_exclude_by_patterns") else True                    # (the reference version agrees)


def test_new_files_never_counts_whats_inside_apps_projects_or_hidden(monkeypatch):
    _defaults(monkeypatch)
    db = Path(tempfile.mkdtemp()) / "empty.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (file_path TEXT)")
    con.commit()
    con.close()
    page = SimpleNamespace(destination_path=ROOT)
    monkeypatch.setattr(op.file_index, "db_path", str(db))
    new = op.OrganizePage._check_for_unindexed_files(page)
    assert rel(new) == OK, sorted(rel(new))


def test_the_plan_never_gets_them(monkeypatch):
    _defaults(monkeypatch)
    got = {}

    class FakePlanWorker:
        def __init__(self, instruction, files):
            got["files"] = files
            self.finished = self.error = mock.MagicMock()

        def start(self):
            pass

    page = mock.MagicMock(destination_path=ROOT, original_instruction="Sort these")
    files = [{"id": i, "file_path": p, "file_name": Path(p).name} for i, p in enumerate(ALL)]
    monkeypatch.setattr(op, "PlanWorker", FakePlanWorker)
    op.OrganizePage._on_files_prepared(page, {"stage": "ready", "files": files, "original_count": len(files)})
    assert rel(f["file_path"] for f in got["files"]) == OK, sorted(rel(f["file_path"] for f in got["files"]))


def test_apply_never_moves_them_even_if_a_plan_has_them(monkeypatch):
    _defaults(monkeypatch)
    got = {}

    class FakeApply:
        def __init__(self, move_plan, moves_snapshot, dest, parent):
            got["moves"] = move_plan
            self.progress = self.result_ready = self.failed = mock.MagicMock()

        def start(self):
            pass

    class Yes:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return True

    page = mock.MagicMock(destination_path=ROOT, auto_watcher=None,
                          current_plan={"folders": {"stuff": list(range(len(ALL)))}})
    page.current_moves = [{"file_id": i, "file_name": Path(p).name, "source_path": p, "size": 1,
                           "destination_folder": "stuff",
                           "destination_path": str(ROOT / "stuff" / Path(p).name)} for i, p in enumerate(ALL)]
    monkeypatch.setattr(op, "ApplyMovesWorker", FakeApply)
    monkeypatch.setattr(op, "ModernConfirmDialog", Yes)
    op.OrganizePage.apply_organization(page)
    assert "moves" in got, "apply never reached the move step"
    assert rel(m["source_path"] for m in got["moves"]) == OK, sorted(rel(m["source_path"] for m in got["moves"]))


def test_a_code_project_folder_is_refused(monkeypatch):
    _defaults(monkeypatch)
    page = mock.MagicMock(destination_path=ROOT / "bakery-website", _prep_worker=None, _skip_index_check_once=False)
    page.instruction_input.text.return_value = "Sort these"
    warn, prep = mock.MagicMock(), mock.MagicMock()
    monkeypatch.setattr(op.ModernInfoDialog, "show_warning", warn)
    monkeypatch.setattr(op, "OrganizePrepWorker", prep)
    op.OrganizePage.generate_plan(page)
    assert warn.called and "code project" in warn.call_args.kwargs.get("message", ""), warn.call_args
    assert not prep.called, "nothing is scanned or planned"
    page2 = mock.MagicMock(destination_path=ROOT, _prep_worker=None, _skip_index_check_once=False)
    page2.instruction_input.text.return_value = "Sort these"
    warn.reset_mock()
    op.OrganizePage.generate_plan(page2)
    assert not warn.called and prep.called, "a normal folder goes ahead"
