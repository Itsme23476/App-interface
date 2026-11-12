"""Organize New Only keeps the snapshot of what was already there ON DISK and uses it again after a restart, so a file
that came in while Filect was closed is sorted however it arrived (2026-11-12, the owner's design). Before, the snapshot
was taken again at every start and a file MOVED in (cut/paste keeps its old creation date) counted as already there.
Already there = its content OR its name is in the snapshot: a renamed old file and an edited old file both stay.
Temp folders, a throwaway index, History and snapshot file; settings.json never written; no AI (the planner is a fake)."""
import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

_qapp = QApplication.instance() or QApplication([])
from app.core import auto_watcher as aw  # noqa: E402
from app.core import database  # noqa: E402
from app.core.settings import settings  # noqa: E402

NEW_ONLY = 3


def _sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _file(path, text=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else f"{path.name} {time.time_ns()}", encoding="utf-8")
    return path


def _age(path, secs_ago):
    """Make a file look as old as a file MOVED in keeps looking: old modified AND old creation date."""
    t = time.time() - secs_ago
    os.utime(path, (t, t))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.CreateFileW.restype = wintypes.HANDLE
        h = k.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x80, None)        # FILE_WRITE_ATTRIBUTES
        ft = int((t + 11644473600) * 10 ** 7)
        c = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
        assert k.SetFileTime(wintypes.HANDLE(h), ctypes.byref(c), None, None)
        k.CloseHandle(wintypes.HANDLE(h))
    assert aw._created_at(str(path)) < t + 5


class _FakeIndexer:
    def __init__(self, *a, **k):
        pass

    def index_single_file(self, path, force_ai=False, **k):
        database.file_index.add_file({"source_path": str(path), "name": os.path.basename(str(path)),
                                      "extension": os.path.splitext(str(path))[1], "size": os.path.getsize(path),
                                      "content_hash": _sha(path), "tags": ["note"], "label": "note"})
        return {"success": True}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)      # NEVER write the user's settings.json
    moves = tmp_path / "moves"
    moves.mkdir()
    monkeypatch.setattr(settings, "get_moves_dir", lambda: moves)
    monkeypatch.setattr(settings, "auto_organize_folders", [])
    monkeypatch.setattr(settings, "pinned_paths", [])
    monkeypatch.setattr(settings, "exclusion_patterns", settings._get_default_exclusions())
    monkeypatch.setattr(database, "file_index", database.FileIndex(tmp_path / "index.db"))
    monkeypatch.setattr(aw, "STILL_WRITING_SECS", -10 ** 9)
    from app.core import search
    monkeypatch.setattr(search, "SearchService", _FakeIndexer)
    snap = tmp_path / "auto_organize_baseline.json"
    monkeypatch.setattr(aw, "_baselines_path", lambda: snap, raising=False)                # (a throwaway snapshot file)
    monkeypatch.setattr(aw, "_saving_baselines", lambda: True, raising=False)
    d = tmp_path / "Downloads"
    d.mkdir()
    yield SimpleNamespace(tmp=tmp_path, d=d, snap=snap)


def _turn_on(d, since=None):
    since = since or time.time() - 3600         # (turned on an hour ago)
    settings.auto_organize_folders.append({"path": str(d), "instruction": "", "action": NEW_ONLY,
                                           "only_new": True, "watching_since": since})
    return since


def _watcher(d):
    w = aw.AutoOrganizeWatcher()
    assert w.add_folder(str(d))
    w.start(organize_existing=False)
    assert w.wait_ready(), "looked through (in the background)"
    return w


def _quit(w):
    """Filect quits: the watcher stops (and keeps what it learned)."""
    w.stop()


def _sent(w, monkeypatch):
    sent = []
    monkeypatch.setattr(w, "_process_files_with_ai",
                        lambda files, folder, instruction, existing=None: sent.append(sorted(files)))
    w._debounce_seconds = 0
    w._check_for_new_files()
    w._check_for_new_files()
    return sent


def test_a_file_moved_in_while_closed_is_sorted_after_a_restart(env, monkeypatch):
    d = env.d
    old = _file(d / "old notes.txt")
    _age(old, 7200)
    _turn_on(d)
    w = _watcher(d)
    _quit(w)
    saved = json.loads(env.snap.read_text(encoding="utf-8"))
    assert list(saved.values())[0]["names"] == ["old notes.txt"], saved
    moved = _file(d / "moved in.txt")
    _age(moved, 86400)                          # (cut & paste: it keeps its old dates)
    w2 = _watcher(d)
    try:
        assert str(moved) not in w2._processed_files
        assert _sent(w2, monkeypatch) == [[str(moved)]], "the moved-in file is new; the old one stays"
    finally:
        w2.stop()


def test_an_old_file_renamed_while_closed_stays(env, monkeypatch):
    d = env.d
    old = _file(d / "report.txt")
    _age(old, 7200)
    _turn_on(d)
    _quit(_watcher(d))
    renamed = d / "report final.txt"
    os.replace(old, renamed)                    # (same content, new name)
    w2 = _watcher(d)
    try:
        assert _sent(w2, monkeypatch) == [], "known by its content"
        assert "report final.txt" in w2._baseline_names[os.path.normpath(str(d))], "its new name is remembered"
    finally:
        w2.stop()


def test_an_old_file_edited_while_closed_stays(env, monkeypatch):
    d = env.d
    old = _file(d / "budget.txt", "v1")
    _age(old, 7200)
    _turn_on(d)
    _quit(_watcher(d))
    old.write_text("v2 - edited while Filect was closed", encoding="utf-8")   # (new content, same name)
    w2 = _watcher(d)
    try:
        assert str(old) in w2._processed_files, "known by its name"
        assert _sent(w2, monkeypatch) == []
    finally:
        w2.stop()


def test_a_file_sorted_last_time_is_not_sorted_again(env, monkeypatch):
    d = env.d
    _age(_file(d / "old.txt"), 7200)
    _turn_on(d)
    w = _watcher(d)
    new = _file(d / "invoice 9.txt")
    dest = d / "Invoices" / "invoice 9.txt"
    dest.parent.mkdir()
    os.replace(new, dest)
    w._on_worker_file_organized(str(new), str(dest), "Invoices")            # (the watcher sorted it)
    _quit(w)
    w2 = _watcher(d)
    try:
        assert str(dest) in w2._processed_files
        assert _sent(w2, monkeypatch) == [], "sorted last time = already there now"
    finally:
        w2.stop()


def test_stop_then_start_takes_a_fresh_snapshot(env, monkeypatch):
    d = env.d
    _age(_file(d / "old.txt"), 7200)
    _turn_on(d)
    _quit(_watcher(d))
    later = _file(d / "later.txt")
    _age(later, 86400)
    settings.auto_organize_folders[0]["watching_since"] = time.time()       # (Stop, then Start again: a new session)
    w2 = _watcher(d)
    try:
        assert str(later) in w2._processed_files, "there when it was turned on again = already there"
        assert _sent(w2, monkeypatch) == []
    finally:
        w2.stop()


def test_a_damaged_snapshot_file_just_means_a_fresh_snapshot(env, monkeypatch):
    d = env.d
    _age(_file(d / "old.txt"), 7200)
    _turn_on(d)
    env.snap.write_text("{ this is not json", encoding="utf-8")
    w = _watcher(d)
    try:
        assert _sent(w, monkeypatch) == []
    finally:
        w.stop()
    assert json.loads(env.snap.read_text(encoding="utf-8")), "saved again, properly"


def test_the_test_suite_never_writes_the_real_snapshot(monkeypatch):
    monkeypatch.undo()                                    # (the real guard and path)
    assert aw._saving_baselines() is False, "under pytest nothing is saved"
    assert aw._write_baselines({"x": 1}) is False
