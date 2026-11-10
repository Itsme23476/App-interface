"""Auto-organize watcher, ported from the Mac ("set up auto-organize by voice" + three watcher fixes).

Automatic moves go in History; files that arrived while Filect was closed count as new; nothing is filed into,
taken out of, or tidied inside a code project / app / program folder; skip_known; paused folders; add_folder_live.

Temp folders only, a throwaway index and History (moves dir); settings.json is never written (_save_config is a
no-op and the folder list is a monkeypatched fake). No network or AI: the planner and the indexer are fakes.
Run: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_auto_watcher_voice.py -q
"""
import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

_qapp = QApplication.instance() or QApplication([])

from app.core import apply as apply_mod  # noqa: E402
from app.core import auto_watcher as aw  # noqa: E402
from app.core import database  # noqa: E402
from app.core.settings import settings  # noqa: E402

NEW_ONLY, AS_IS, REORGANIZE = 3, 2, 1


# ---------------------------------------------------------------------------------------------------- helpers
def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _file(path, text=None):
    """A file with unique content (Filect leaves content it already knows where it is)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else f"{path.name} {time.time_ns()}", encoding="utf-8")
    return path


def _index(path, at=None):
    """Put `path`'s content in the throwaway index, recorded at `at` (default: where it is)."""
    at = str(at or path)
    database.file_index.add_file({"source_path": at, "name": os.path.basename(at),
                                  "extension": os.path.splitext(at)[1], "size": os.path.getsize(path),
                                  "content_hash": _sha(path), "tags": ["note"], "label": "note"})


def _entry(folder, **fields):
    e = {"path": str(folder), "instruction": "", **fields}
    settings.auto_organize_folders.append(e)
    return e


def _pump(cond, timeout=10.0):
    t = time.time()
    while time.time() - t < timeout:
        _qapp.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


def _fake_planner(monkeypatch, folders):
    """The AI plan, by file name: {"Docs": ["a.txt"]}. Everything around it is a pass-through."""
    from app.core import ai_organizer

    def plan(instruction, files_info, *a, **k):
        ids = {f["file_name"]: f["id"] for f in files_info}
        return {"folders": {name: [ids[n] for n in names if n in ids] for name, names in folders.items()}}

    monkeypatch.setattr(ai_organizer, "request_organization_plan", plan)
    monkeypatch.setattr(ai_organizer, "_apply_tag_safety_net", lambda p, *a, **k: p)
    monkeypatch.setattr(ai_organizer, "deduplicate_plan", lambda p, *a, **k: p)
    monkeypatch.setattr(ai_organizer, "ensure_all_files_included", lambda p, *a, **k: p)
    monkeypatch.setattr(ai_organizer, "validate_plan", lambda p, ids, *a, **k: (True, []))


class _FakeIndexer:
    """Stands in for SearchService: indexing = a row in the throwaway index (no vision / network)."""
    calls = []

    def __init__(self, *a, **k):
        pass

    def index_single_file(self, path, force_ai=False, **k):
        _FakeIndexer.calls.append(str(path))
        _index(path)
        return {"success": True}


def _record_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(apply_mod, "record_auto_moves", lambda folder, moves: calls.append((folder, list(moves))))
    return calls


def _watcher(*folders, start=True):
    w = aw.AutoOrganizeWatcher()
    for f in folders:
        assert w.add_folder(str(f))
    if start:
        w.start(organize_existing=False)
        assert w.wait_ready(), "looked through (in the background)"
    return w


def _sent_to_ai(w, monkeypatch):
    """What the periodic check hands the planner (nothing runs)."""
    sent = []
    monkeypatch.setattr(w, "_process_files_with_ai",
                        lambda files, folder, instruction, existing=None: sent.append((list(files), folder, instruction)))
    w._debounce_seconds = 0
    return sent


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)      # NEVER write the user's settings.json
    moves = tmp_path / "moves"
    moves.mkdir()
    monkeypatch.setattr(settings, "get_moves_dir", lambda: moves)            # throwaway History
    monkeypatch.setattr(settings, "auto_organize_folders", [])
    monkeypatch.setattr(settings, "pinned_paths", [])
    monkeypatch.setattr(settings, "exclusion_patterns", settings._get_default_exclusions())
    monkeypatch.setattr(database, "file_index", database.FileIndex(tmp_path / "index.db"))   # throwaway index
    from app.core import search
    monkeypatch.setattr(search, "SearchService", _FakeIndexer)
    _FakeIndexer.calls = []
    d = tmp_path / "Downloads"
    d.mkdir()
    yield SimpleNamespace(tmp=tmp_path, d=d, moves=moves)


# ---------------------------------------------------------------------------------- B3: automatic moves in History
def test_worker_batch_goes_in_history(env, monkeypatch):
    d = env.d
    a, b = _file(d / "a.txt"), _file(d / "b.txt")
    _fake_planner(monkeypatch, {"Docs": ["a.txt", "b.txt"]})
    calls = _record_calls(monkeypatch)
    aw.AutoWatcherWorker([str(a), str(b)], str(d), "by type").run()
    assert (d / "Docs" / "a.txt").exists() and (d / "Docs" / "b.txt").exists()
    assert calls == [(str(d), [(str(a), str(d / "Docs" / "a.txt")), (str(b), str(d / "Docs" / "b.txt"))])], \
        "one History write per batch, with every (from, to)"


def test_a_batch_that_moved_nothing_writes_nothing(env, monkeypatch):
    d = env.d
    a = _file(d / "Docs" / "a.txt")
    _fake_planner(monkeypatch, {"Docs": ["a.txt"]})          # (already there)
    calls = _record_calls(monkeypatch)
    aw.AutoWatcherWorker([str(a)], str(d), "").run()
    assert calls == []


def test_history_failing_never_breaks_a_move(env, monkeypatch):
    d = env.d
    a = _file(d / "a.txt")
    _fake_planner(monkeypatch, {"Docs": ["a.txt"]})

    def broken(folder, moves):
        raise OSError("disk full")
    monkeypatch.setattr(apply_mod, "record_auto_moves", broken)
    w = aw.AutoWatcherWorker([str(a)], str(d), "")
    done = []
    w.finished_processing.connect(done.append)
    w.run()
    assert (d / "Docs" / "a.txt").exists() and done, "moved, and the batch still finished"


def test_a_stopped_batch_still_records_what_moved(env, monkeypatch):
    d = env.d
    a, b = _file(d / "a.txt"), _file(d / "b.txt")
    _fake_planner(monkeypatch, {"Docs": ["a.txt", "b.txt"]})
    calls = _record_calls(monkeypatch)
    w = aw.AutoWatcherWorker([str(a), str(b)], str(d), "")
    w.file_organized.connect(lambda *_: w.stop())                 # stop right after the first move
    w.run()
    assert calls == [(str(d), [(str(a), str(d / "Docs" / "a.txt"))])] and b.exists()


def test_watcher_moves_land_in_history_and_a_revert_is_not_sorted_again(env, monkeypatch):
    """The real apply.record_auto_moves + revert_moves, through a real worker thread."""
    d = env.d
    _entry(d, action=REORGANIZE, watching_since=time.time() - 5)
    a = _file(d / "a.txt")
    _fake_planner(monkeypatch, {"Docs": ["a.txt"]})
    w = _watcher(d, start=False)
    w._process_files_with_ai([str(a)], str(d), "", None)
    assert _pump(lambda: w._current_worker is not None and w._current_worker.isFinished()
                 and (d / "Docs" / "a.txt").exists())
    _pump(lambda: False, timeout=0.2)                             # (let the queued signals land)
    auto = [h for h in apply_mod.get_move_history() if h.get("source") == "auto"]
    assert len(auto) == 1 and auto[0]["folder"] == os.path.normpath(str(d)) and auto[0]["successful_moves"] == 1
    log = auto[0]["log_file"]
    moves = json.load(open(log, encoding="utf-8"))["moves"]
    assert moves[0]["from"] == str(a) and moves[0]["to"] == str(d / "Docs" / "a.txt")

    done, errors = apply_mod.revert_moves(log, moves)
    assert done == 1 and not errors and a.exists()
    assert settings.auto_organize_skip_known(str(d)), "reverting set skip_known on the folder"

    w2 = _watcher(d)                                              # (Filect restarted)
    try:
        assert str(a) in w2._processed_files, "indexed where it was put back: known, not new"
        fresh = _file(d / "fresh.txt")
        assert w2._filter_genuinely_new_files(str(d), [str(a), str(fresh)]) == [str(fresh)], \
            "what was put back stays where it is; new files are still sorted"
    finally:
        w2.stop()


def test_flatten_records_moves_and_never_reaches_into_projects(env, monkeypatch):
    d = env.d
    x = _file(d / "sub" / "x.txt")
    _file(d / "proj" / "main.py")
    (d / "proj" / ".git" / "refs" / "tags").mkdir(parents=True)  # (an empty folder git needs)
    (d / "proj" / "build").mkdir()
    _file(d / "Tool" / "tool.exe")                                # (a Windows program folder)
    _file(d / "Tool" / "core.dll")
    calls = _record_calls(monkeypatch)
    w = _watcher(d, start=False)
    assert w.flatten_folder(str(d)) == 1
    assert (d / "x.txt").exists() and not (d / "sub").exists()
    assert calls == [(str(d), [(str(x), str(d / "x.txt"))])]
    assert (d / "proj" / "main.py").exists() and (d / "Tool" / "tool.exe").exists() and (d / "Tool" / "core.dll").exists()
    assert (d / "proj" / ".git" / "refs" / "tags").is_dir() and (d / "proj" / "build").is_dir(), \
        "the empty-folder clean-up never deletes inside a code project"


# ------------------------------------------------------------- B5a: files that arrived while Filect was closed
def test_created_at_is_the_creation_time_on_windows(env, monkeypatch):
    p = _file(env.d / "c.txt")
    long_ago = time.time() - 7200
    os.utime(p, (long_ago, long_ago))                             # (mtime moves; creation time doesn't)
    assert aw._created_at(str(p)) > long_ago + 3600
    if os.name == "nt":
        real_stat = os.stat
        no_birthtime = SimpleNamespace(st_ctime=123.0)            # (a Python without st_birthtime)
        monkeypatch.setattr(os, "stat", lambda path, *a, **k: no_birthtime if str(path) == str(p) else real_stat(path, *a, **k))
        assert aw._created_at(str(p)) == 123.0, "Windows: st_ctime IS the creation time (never 'already there')"


def test_baseline_leaves_out_what_arrived_after_turning_on(env):
    d = env.d
    old = _file(d / "old.txt")
    time.sleep(0.05)
    since = time.time()
    time.sleep(0.05)
    late = _file(d / "late.txt")
    assert aw._created_at(str(old)) < since < aw._created_at(str(late))
    _entry(d, action=NEW_ONLY, watching_since=since)
    w = aw.AutoOrganizeWatcher()
    w._capture_baseline(str(d))
    base = w._baseline_hashes[os.path.normpath(str(d))]
    assert _sha(old) in base, "was there before it was turned on"
    assert _sha(late) not in base, "arrived while Filect was closed: that's new"

    settings.auto_organize_folders[0].pop("watching_since")       # (no watching_since: the old behaviour)
    w._capture_baseline(str(d))
    assert _sha(late) in w._baseline_hashes[os.path.normpath(str(d))]


def test_after_a_restart_new_only_sorts_what_came_in_while_closed(env, monkeypatch):
    d = env.d
    old = _file(d / "old.txt")
    time.sleep(0.05)
    since = time.time()
    time.sleep(0.05)
    sorted_ = _file(d / "Docs" / "sorted.txt")                    # (sorted by the watcher earlier: it's indexed)
    _index(sorted_)
    late = _file(d / "late.txt")                                  # (downloaded while Filect was closed)
    _entry(d, action=NEW_ONLY, watching_since=since)
    w = _watcher(d)
    try:
        assert str(old) in w._processed_files and str(sorted_) in w._processed_files
        assert str(late) not in w._processed_files
        sent = _sent_to_ai(w, monkeypatch)
        w._check_for_new_files()
        w._check_for_new_files()
        assert [s[0] for s in sent] == [[str(late)]], "only the file that came in while Filect was closed"
    finally:
        w.stop()


# ------------------------------------------------- B5b / B5c: never into, out of, or inside a project or an app
def test_usable_subfolders_leave_out_projects_apps_and_libraries(env):
    d = env.d
    (d / "Bills" / "2024").mkdir(parents=True)
    (d / "myproj" / ".git").mkdir(parents=True)
    (d / "myproj" / "src").mkdir()
    (d / "Thing.app").mkdir()
    (d / ".hidden").mkdir()
    (d / "node_modules").mkdir()
    _file(d / "Tool" / "tool.exe")
    _file(d / "Tool" / "core.dll")
    assert aw.AutoOrganizeWatcher._usable_subfolders(str(d)) == ["Bills", "Bills/2024"]


def test_all_three_as_is_listings_use_usable_subfolders(env, monkeypatch):
    d = env.d
    (d / "Bills").mkdir()
    (d / "myproj" / ".git").mkdir(parents=True)
    _file(d / "invoice.txt")
    _entry(d, action=AS_IS, instruction="bills go in Bills")
    w = _watcher(d, start=False)
    assert w._get_existing_folders_if_as_is(str(d)) == ["Bills"]
    seen = []
    monkeypatch.setattr(w, "_process_files_with_ai", lambda files, folder, instruction, existing=None: seen.append(existing))
    w.organize_single_folder(str(d), flatten_first=False)
    assert w.wait_ready()
    w.organize_folders_with_per_folder_options({str(d): AS_IS})
    assert w.wait_ready()
    assert seen == [["Bills"], ["Bills"]]


def test_existing_folders_instruction_is_the_as_is_prompt(env, monkeypatch):
    text = aw.existing_folders_instruction("by year", ["Bills", "Photos/Vacation"])
    assert "EXISTING FOLDERS ONLY" in text and "'Bills', 'Photos/Vacation'" in text and "by year" in text
    assert "Organize files into appropriate folders" in aw.existing_folders_instruction("", ["Bills"])
    w = aw.AutoOrganizeWatcher()
    started = []
    monkeypatch.setattr(w, "_start_worker", lambda files, folder, instruction, existing=None: started.append(instruction))
    w._process_files_with_ai([str(_file(env.d / "a.txt"))], str(env.d), "by year", ["Bills"])
    assert started == [aw.existing_folders_instruction("by year", ["Bills"])]


def test_should_ignore_anything_inside_a_project_or_app(env):
    d = env.d
    inside = _file(d / "proj" / "src" / "main.py")
    _file(d / "proj" / "package.json", "{}")
    in_app = _file(d / "Tool" / "readme.txt")
    _file(d / "Tool" / "tool.exe")
    _file(d / "Tool" / "core.dll")
    note = _file(d / "Docs" / "note.txt")
    w = _watcher(d, start=False)
    assert w._should_ignore(str(inside)) and w._should_ignore(str(in_app))
    assert not w._should_ignore(str(note)) and not w._should_ignore(str(d / "a.txt"))
    assert not w._should_ignore("main.py"), "a bare name has no location: only the old checks"
    other = env.tmp / "Elsewhere"
    p = _file(other / "repo" / ".git" / "HEAD")
    _file(other / "repo" / "x.txt")
    assert w._should_ignore(str(other / "repo" / "x.txt"), str(other)), "a folder that isn't watched: pass its root"
    assert w._should_ignore(str(p), str(other))


def test_new_files_inside_a_project_are_never_picked_up(env, monkeypatch):
    d = env.d
    (d / "proj" / ".git").mkdir(parents=True)
    _entry(d, action=REORGANIZE)
    w = _watcher(d)
    try:
        sent = _sent_to_ai(w, monkeypatch)
        new, deep = _file(d / "new.txt"), _file(d / "Docs" / "new2.txt")
        _file(d / "proj" / "new.py")
        w._check_for_new_files()
        w._check_for_new_files()
        assert sorted(p for s in sent for p in s[0]) == sorted([str(new), str(deep)])
    finally:
        w.stop()


# ---------------------------------------------------------------------------------------------- skip_known
def test_skip_known_leaves_files_filect_knows_where_they_are(env):
    d = env.d
    back = _file(d / "back.txt")                                  # (put back by a revert: indexed right here)
    _index(back)
    moved_in = _file(d / "moved_in.txt")                          # (known, indexed somewhere else)
    _index(moved_in, at=env.tmp / "Desktop" / "moved_in.txt")
    mine = _file(d / "mine.txt")                                  # (just indexed by this watcher, not sorted yet)
    _index(mine)
    fresh = _file(d / "fresh.txt")
    files = [str(back), str(moved_in), str(mine), str(fresh)]
    e = _entry(d, action=REORGANIZE)
    w = aw.AutoOrganizeWatcher()
    w._on_worker_file_indexed(str(mine))
    assert w._filter_genuinely_new_files(str(d), files) == files, "no skip_known, not new-only: unchanged"
    e["skip_known"] = True
    assert w._filter_genuinely_new_files(str(d), files) == [str(moved_in), str(mine), str(fresh)], \
        "known from ANOTHER folder = you just brought it in: new here (Mac bug 37)"
    assert str(back).lower() in w._left_in_place_paths, "and the scans skip it from now on"


def test_new_only_without_skip_known_is_unchanged(env):
    d = env.d
    back = _file(d / "back.txt")
    _index(back)
    moved_in = _file(d / "moved_in.txt")
    _index(moved_in, at=env.tmp / "Desktop" / "moved_in.txt")
    fresh = _file(d / "fresh.txt")
    _entry(d, action=NEW_ONLY)
    w = aw.AutoOrganizeWatcher()
    assert w._filter_genuinely_new_files(str(d), [str(back), str(moved_in), str(fresh)]) == \
        [str(back), str(moved_in), str(fresh)], ("same path = just indexed by this watcher (Windows' race fix) still "
                                                 "counts as new; known from the Desktop = dragged in: new (bug 37)")


def test_a_file_dragged_in_from_another_folder_is_sorted_but_one_moved_around_inside_stays(env):
    """Mac bug 37: "i moved the intro and it doesn't seem to organize it" - a file Filect already knew from another
    folder was silently left alone. Only a file that was already inside THIS folder (sorted into a subfolder, then
    dragged back out by you) stays where you put it. Paths compare without case, like Windows."""
    d = env.d
    _entry(d, action=NEW_ONLY)
    w = aw.AutoOrganizeWatcher()
    from_desktop = _file(d / "intro 204.mp4")
    _index(from_desktop, at=env.tmp / "Desktop" / "Videos" / "intro 204.mp4")
    from_inside = _file(d / "taxes.pdf")
    _index(from_inside, at=d / "documents" / "taxes.pdf")              # (Filect sorted it there; you pulled it out)
    other_case = _file(d / "Lease.docx")
    _index(other_case, at=str(d / "contracts" / "Lease.docx").upper())  # (same folder, other letter case)
    prefix_trap = _file(d / "photo.jpg")
    _index(prefix_trap, at=os.path.join(str(d) + " Old", "photo.jpg"))  # ("Downloads Old" only STARTS like it)
    got = w._filter_genuinely_new_files(str(d), [str(from_desktop), str(from_inside), str(other_case), str(prefix_trap)])
    assert got == [str(from_desktop), str(prefix_trap)], (
        "dragged in from the Desktop / from a folder whose name only starts like this one: new; pulled out of this "
        f"folder's own subfolders: stays. Got {got}")
    assert str(from_inside).lower() in w._left_in_place_paths and str(other_case).lower() in w._left_in_place_paths


# ------------------------------------------------------------------------------------------------ paused
def test_paused_folders_are_skipped(env):
    a, b = env.tmp / "Alpha", env.tmp / "Beta"
    a.mkdir()
    b.mkdir()
    _file(a / "x.txt")
    _entry(a, paused=True)
    _entry(b)
    w = aw.AutoOrganizeWatcher()
    assert w.add_folder(str(a)) is False and w.add_folder(str(a).upper()) is False, "paused, in any letter case"
    assert w.add_folder(str(b)) is True and w.watched_folders == [os.path.normpath(str(b))]
    w.start(organize_existing=False)
    assert w.wait_ready()
    try:
        assert not any(p.startswith(str(a)) for p in w._processed_files), "a paused folder isn't even looked at"
        assert w.add_folder_live(str(a), "by type"), "voice turning it on: the named folder starts"
        assert w.wait_ready()
        assert os.path.normpath(str(a)) in w.watched_folders
    finally:
        w.stop()


# ---------------------------------------------------------------------------------------- add_folder_live
def test_add_folder_live_joins_a_running_watcher(env, monkeypatch):
    a, b = env.tmp / "Alpha", env.tmp / "Beta"
    b.mkdir()
    old = _file(a / "old.txt")
    sorted_ = _file(a / "Docs" / "sorted.txt")
    _entry(b)
    w = _watcher(b)
    try:
        _entry(a, action=NEW_ONLY, instruction="by type", watching_since=time.time() + 1)   # (saved by the voice set-up; +1 s: after old.txt even with coarse file times)
        assert w.add_folder_live(str(a), "by type") is True
        assert w.add_folder_live(str(a).upper(), "by type") is True
        assert w.wait_ready(), "looked through (in the background)"
        assert [f for f in w.watched_folders if aw._path_key(f) == aw._path_key(str(a))] == [os.path.normpath(str(a))], \
            "once, in any letter case"
        assert w.folder_instructions[os.path.normpath(str(a))] == "by type"
        assert _sha(old) in w._baseline_hashes[os.path.normpath(str(a))], "its Organize New Only baseline"
        assert str(old) in w._processed_files and str(sorted_) in w._processed_files, "what was there is known"
        sent = _sent_to_ai(w, monkeypatch)
        new = _file(a / "new.txt")
        w._check_for_new_files()
        w._check_for_new_files()
        assert sent == [([str(new)], os.path.normpath(str(a)), "by type")], "files arriving from now on are sorted"
        assert w.add_folder_live(str(env.tmp / "nope")) is False
    finally:
        w.stop()


def test_add_folder_live_when_not_running_only_lists_it(env):
    a = env.tmp / "Alpha"
    _file(a / "old.txt")
    w = aw.AutoOrganizeWatcher()
    assert w.add_folder_live(str(a), "x") and w.watched_folders == [os.path.normpath(str(a))]
    assert not w._processed_files, "start() does the rest"


def test_remove_folder_in_any_case_drops_its_queued_work(env):
    a, b = env.tmp / "Alpha", env.tmp / "Beta"
    a.mkdir()
    b.mkdir()
    w = _watcher(a, b, start=False)
    w._worker_queue = [(str(a), "i", None, ["x"]), (str(b), "i", None, ["y"])]
    w.remove_folder(str(a).upper())
    assert w.watched_folders == [os.path.normpath(str(b))] and [q[0] for q in w._worker_queue] == [str(b)]


# ------------------------------------------- a big folder never freezes the window (the work is off the UI thread)
import threading  # noqa: E402


def _ui_thread_hashes(w, monkeypatch):
    """Paths hashed ON the UI thread (the background steps hash elsewhere)."""
    seen = []
    real = w._compute_file_hash

    def spy(path, gen=None):
        if threading.current_thread() is threading.main_thread():
            seen.append(str(path))
        return real(path, gen)
    monkeypatch.setattr(w, "_compute_file_hash", spy)
    return seen


def test_start_returns_at_once_and_looks_through_in_the_background(env, monkeypatch):
    d = env.d
    files = [_file(d / f"sub{i % 7}" / f"f{i}.txt") for i in range(400)]
    _entry(d, action=NEW_ONLY)
    w = _watcher(d, start=False)
    on_ui = _ui_thread_hashes(w, monkeypatch)
    t = time.monotonic()
    w.start(organize_existing=False)
    took = time.monotonic() - t
    try:
        assert took < 0.3, f"start() returned after {took:.2f} s: it must not walk / hash on the UI thread"
        assert w.is_running and aw._path_key(str(d)) in w._preparing
        assert w._file_check_timer is None, "the periodic check waits until the folder was looked through"
        w._check_for_new_files()
        assert not w._pending_files, "a folder still being looked through isn't checked (all would look new)"
        assert w.wait_ready()
        assert w._file_check_timer is not None and not w._preparing
        assert all(str(f) in w._processed_files for f in files), "everything already there counts as known"
        assert {_sha(f) for f in files} <= w._baseline_hashes[os.path.normpath(str(d))]
        assert on_ui == [], "no file was hashed on the UI thread"
    finally:
        w.stop()


def test_stopping_while_it_looks_through_drops_that_look(env):
    d = env.d
    for i in range(300):
        _file(d / f"f{i}.txt")
    _entry(d, action=NEW_ONLY)
    w = _watcher(d, start=False)
    w.start(organize_existing=False)
    w.stop()
    assert w.wait_ready(5) and not w._processed_files and not w._baseline_hashes and w._file_check_timer is None, \
        "a stopped run's look-through changes nothing"
    w.start(organize_existing=False)                              # (and it starts again cleanly)
    try:
        assert w.wait_ready() and len(w._processed_files) == 300 and w._file_check_timer is not None
    finally:
        w.stop()


def test_the_timer_check_walks_in_the_background_and_sorts_a_new_file(env, monkeypatch):
    d = env.d
    _file(d / "old.txt")
    _entry(d, action=NEW_ONLY, instruction="by type")
    w = _watcher(d)
    try:
        sent = _sent_to_ai(w, monkeypatch)
        on_ui = _ui_thread_hashes(w, monkeypatch)
        new = _file(d / "new download.bin", "x" * 3_000_000)
        assert _pump(lambda: (w._on_check_timer(), bool(sent))[1], 10), "found and sent by the timer's check"
        assert sent == [([str(new)], os.path.normpath(str(d)), "by type")]
        assert on_ui == [], "the new download was hashed in the background, not on the UI thread"
    finally:
        w.stop()


def test_a_check_already_running_isnt_started_twice(env, monkeypatch):
    d = env.d
    _entry(d)
    w = _watcher(d)
    try:
        started = []
        monkeypatch.setattr(w, "_off_ui", lambda fn, then: started.append(fn))
        w._on_check_timer()
        w._on_check_timer()
        assert len(started) == 1 and w._tick_busy, "one walk at a time"
    finally:
        w._tick_busy = False
        w.stop()


def test_a_queued_rescan_happens_in_the_background(env, monkeypatch):
    d = env.d
    _entry(d)
    w = _watcher(d)
    try:
        new = _file(d / "late.txt")
        batches = []
        monkeypatch.setattr(w, "_start_worker", lambda files, folder, instr, existing=None: batches.append(files))
        walked_on = []
        real = w._scan_folder_for_files
        monkeypatch.setattr(w, "_scan_folder_for_files",
                            lambda f, exclude_organized=False: (walked_on.append(threading.current_thread()),
                                                                real(f, exclude_organized))[1])
        w._worker_queue.append((os.path.normpath(str(d)), "[AUTO-ORGANIZE] x", None))
        w._on_worker_finished()
        assert w.wait_ready()
        assert batches == [[str(new)]] and walked_on and walked_on[0] is not threading.main_thread()
    finally:
        w.stop()


def test_the_fast_exclusion_check_matches_the_original():
    odd = [".json", ".env.*", "~$*", "*.pyc", "node_modules", "Build Output", "[ab]*.txt", "?.log", "My Folder", ".py"]
    names = ["file.txt", "data.JSON", ".env.local", "~$doc.docx", "mod.pyc", "Thumbs.db", "a.txt", "b.log",
             "x.py", "node_modules", "report final (1).pdf"]
    dirs = [r"C:\Users\u\Downloads", r"C:\Users\u\Desktop\node_modules\pkg", "C:/Users/u/Build Output",
            r"C:\Users\u\My Folder\sub", r"C:\Users\u\.git\objects"]
    for pats in (settings._get_default_exclusions(), settings._get_default_exclusions() + odd, odd, []):
        settings.exclusion_patterns = list(pats)
        for dd in dirs:
            for n in names:
                for p in (os.path.join(dd, n), dd + "/" + n, n):
                    assert settings.should_exclude(p) == settings._should_exclude_by_patterns(p), (pats[:2], p)


# ------------------------------------------------------------------------------------- a burst of new files (2026-11-10)
def test_many_files_landing_at_once_go_in_ONE_batch_and_a_busy_queue_keeps_them_all(env, monkeypatch):
    """Live: 200 downloads dropped at once were sorted one file per batch (~5 s each, one after another): 134 of 200
    still waiting after 7 minutes. Every file that's ready in one check goes in ONE batch; and while a batch runs,
    files for a folder that's already queued JOIN its queued request instead of being dropped as a duplicate."""
    d = env.d
    _entry(d, action=NEW_ONLY, watching_since=time.time() - 60)
    w = aw.AutoOrganizeWatcher()
    w.watched_folders = [os.path.normpath(str(d))]
    w._is_running = True
    sent = []
    monkeypatch.setattr(w, "_process_files_with_ai", lambda files, folder, *a, **k: sent.append(list(files)))
    files = [str(_file(d / f"download {i:03d}.txt")) for i in range(200)]
    t0 = time.time()
    w._after_check({"found": {os.path.normpath(str(d)): files}, "hashes": {}, "now": t0})
    assert sent == [], "first seen: waits out the debounce"
    w._after_check({"found": {os.path.normpath(str(d)): files}, "hashes": {}, "now": t0 + w._debounce_seconds + 1})
    assert len(sent) == 1 and sorted(sent[0]) == sorted(files), f"one batch of 200, not {len(sent)} batches"

    # the queue: a batch is running; two more checks find more files for the same folder
    w2 = aw.AutoOrganizeWatcher()
    w2._current_worker = type("Busy", (), {"isRunning": lambda self: True})()
    a = [str(d / f"a{i}.txt") for i in range(3)]
    b = [str(d / f"b{i}.txt") for i in range(4)] + [a[0].upper()]       # (one again, in other letter case)
    w2._process_files_with_ai(a, str(d), "", None)
    w2._process_files_with_ai(b, str(d), "", None)
    assert len(w2._worker_queue) == 1, "one queued request for the folder"
    queued = w2._worker_queue[0][3]
    assert len(queued) == 7 and set(map(os.path.normcase, queued)) == set(map(os.path.normcase, a + b[:4])), queued


def test_files_a_batch_is_already_sorting_are_not_new_again_at_every_check(env, monkeypatch):
    """Live 2026-11-10: while a 200-file batch ran, every check found those 200 again, looked each one up on the UI
    thread (0.2-0.3 s stalls every few seconds) and re-queued them. Files being sorted, or waiting in the queue,
    are skipped by the check."""
    d = env.d
    _entry(d, action=NEW_ONLY, watching_since=time.time() - 60)
    w = aw.AutoOrganizeWatcher()
    w.watched_folders = [os.path.normpath(str(d))]
    w._is_running = True
    files = [str(_file(d / f"f{i}.txt")) for i in range(5)]
    w._current_worker = type("Busy", (), {"isRunning": lambda self: True})()
    w._current_batch = files[:3]
    w._worker_queue = [(str(d), "", None, [files[3]])]
    busy = w._busy_keys()
    assert busy == {aw._path_key(p) for p in files[:4]}
    sent = []
    monkeypatch.setattr(w, "_process_files_with_ai", lambda fs, *a, **k: sent.append(list(fs)))
    t0 = time.time()
    for t in (t0, t0 + w._debounce_seconds + 1):
        w._after_check({"found": {os.path.normpath(str(d)): files}, "hashes": {}, "now": t})
    assert sent == [[files[4]]], f"only the one nobody has taken on: {sent}"
    w._current_worker = None
    w._worker_queue = []
    assert w._busy_keys() == set(), "nothing running: nothing is busy"
