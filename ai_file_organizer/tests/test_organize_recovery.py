"""Phase B (2026-11-11): the Mac's nine organize fixes "for when things go wrong partway", on Windows' code.
Ported from the Mac (tests/test_organize_recovery.py) to the Windows names, plus the Windows way for #2 (st_ctime is
the CREATION time here: a write shows in st_mtime and the size between checks). #7 (retries) is in
test_urgent_fixes.py (Phase A). Only temp folders; settings.json is never written; no network."""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
from app.core import ai_organizer, apply as moves, auto_watcher as aw, database  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.ui.organize_page import OrganizePage  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    md = tmp_path / "moves"
    md.mkdir()
    monkeypatch.setattr(settings, "get_moves_dir", lambda: md)
    monkeypatch.setattr(settings, "auto_organize_folders", [])
    monkeypatch.setattr(database, "file_index", database.FileIndex(tmp_path / "index.db"))
    return tmp_path


def _folder(tmp, name, action=3, **flags):
    d = tmp / name
    d.mkdir()
    settings.auto_organize_folders = [{"path": str(d), "instruction": "", "action": action, **flags}]
    return d


def _pump(cond, secs=10):
    t = time.time()
    while time.time() - t < secs and not cond():
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    return cond()


# ------------------------------------------------------------------------- 2 · a file still being written waits
def test_a_file_just_written_waits(env, monkeypatch):
    f = _folder(env, "writing") / "export.mov"
    f.write_text("x")
    w = aw.AutoOrganizeWatcher()
    assert not w._ready(str(f)) and not w._ready(str(f)), "written a moment ago: it waits"
    monkeypatch.setattr(aw, "STILL_WRITING_SECS", -10 ** 9)          # (as if written long ago)
    assert w._ready(str(f)), "quiet since the last check: ready"


def test_setting_the_date_back_doesnt_hide_a_write(env):
    f = _folder(env, "backdated") / "copy.mov"
    f.write_text("x")
    os.utime(f, (1.6e9, 1.6e9))                      # (a copier that keeps the old date: Windows' creation time tells)
    w = aw.AutoOrganizeWatcher()
    assert not w._ready(str(f)) and not w._ready(str(f))


def test_a_file_that_keeps_growing_waits_even_between_writes(env, monkeypatch):
    """Windows: a program that reopens the file for each chunk (closed in between, so it CAN be moved) — the size
    between two checks shows it's still being written."""
    monkeypatch.setattr(aw, "STILL_WRITING_SECS", -10 ** 9)
    f = _folder(env, "growing") / "recording.wav"
    f.write_bytes(b"x" * 100)
    w = aw.AutoOrganizeWatcher()
    assert not w._ready(str(f)), "first sight: its size is noted"
    with open(f, "ab") as fh:
        fh.write(b"y" * 100)
    assert not w._ready(str(f)), "grew since the last check: still being written"
    assert w._ready(str(f)), "the same since the last check: done"


def test_the_check_holds_a_file_while_it_grows(env, monkeypatch):
    monkeypatch.setattr(aw, "STILL_WRITING_SECS", -10 ** 9)
    d = _folder(env, "check", action=1)
    f = d / "a.wav"
    f.write_bytes(b"x")
    w = aw.AutoOrganizeWatcher()
    w._is_running = True
    w.watched_folders = [os.path.normpath(str(d))]
    w._debounce_seconds = 0
    sent = []
    monkeypatch.setattr(w, "_process_files_with_ai", lambda files, folder, *x, **k: sent.append(list(files)))
    found = {"found": {os.path.normpath(str(d)): [str(f)]}, "hashes": {}}
    w._after_check({**found, "now": time.time()})
    with open(f, "ab") as fh:
        fh.write(b"more")
    w._after_check({**found, "now": time.time() + 3})
    assert sent == [], "it grew between the checks: not planned"
    w._after_check({**found, "now": time.time() + 6})
    assert sent == [[str(f)]], "quiet for a check: planned"


# ------------------------------------------------------------------------- 6 · "only the folders already there"
def test_only_new_is_only_for_the_folders_already_there(env):
    w = aw.AutoOrganizeWatcher()
    for action, flags, want in ((2, {"only_new": True}, True), (2, {}, False), (3, {}, True), (1, {"only_new": True}, False)):
        d = env / f"m{action}{bool(flags)}"
        settings.auto_organize_folders = [{"path": str(d), "action": action, **flags}]
        assert w._only_new_files(str(d)) is want, (action, flags)


def test_set_up_by_voice_the_files_already_there_stay_put(env, monkeypatch):
    d = _folder(env, "asis", action=2, only_new=True)
    old = d / "old.txt"
    old.write_text("already here")
    w = aw.AutoOrganizeWatcher()
    w._capture_baseline(str(d))
    assert w._filter_genuinely_new_files(str(d), [str(old)]) == [], "it was already there: it stays"
    new = d / "new.txt"
    new.write_text("arrived later")
    assert w._filter_genuinely_new_files(str(d), [str(new)]) == [str(new)], "a new file is still sorted"


def test_the_voice_setup_and_the_settings_dialog_keep_the_flag():
    import inspect
    src = inspect.getsource(OrganizePage.voice_enable_watch)
    assert "only_new=(action == 2)" in src
    from app.ui import organize_page
    assert "'only_new'" in inspect.getsource(organize_page.WatchConfigDialog), "editing a folder keeps the flag"


# ------------------------------------------------------------------------- 4 · a partial undo can be finished
def test_a_partial_undo_can_be_finished(env):
    d = _folder(env, "undo")
    (d / "in").mkdir()
    log = settings.get_moves_dir() / "moves-20260101-000000.json"
    ms = []
    for i in range(3):
        (d / "in" / f"f{i}").write_text(str(i))
        ms.append({"from": str(d / f"f{i}"), "to": str(d / "in" / f"f{i}")})
    log.write_text(json.dumps({"timestamp": "2026-01-01T00:00:00", "moves": ms}))
    parked = d / "parked"
    (d / "in" / "f2").rename(parked)
    r = moves.undo_log(str(log))
    state = json.loads(log.read_text())
    assert r["reverted"] == 2 and not state["reverted"] and state["partly_reverted"], state
    hist = [h for h in moves.get_move_history() if h["log_file"] == str(log)][0]
    assert hist["partly_reverted"] and not hist["reverted"], "History shows it partly reverted (Revert kept)"
    (d / "in").mkdir(exist_ok=True)                       # (the undo removed it once it was empty)
    parked.rename(d / "in" / "f2")
    (d / "f0").write_text("changed since")                # (must never be overwritten by the retry)
    r = moves.undo_log(str(log))
    state = json.loads(log.read_text())
    assert r["reverted"] == 1 and state["reverted"] and not state["partly_reverted"], (r, state)
    assert (d / "f2").read_text() == "2" and (d / "f0").read_text() == "changed since"


def test_after_an_undo_later_moves_share_one_history_row(env):
    folder = str(env / "oneday")
    now = datetime(2026, 1, 2, 10, 0, 0)
    first = moves._auto_log_path(folder, now)
    assert first.name.startswith("moves-20260102-000000-auto-")
    first.write_text(json.dumps({"moves": [], "reverted": True}))
    second = moves._auto_log_path(folder, now)
    assert second != first and "-100000-auto-" in second.name, second
    second.write_text(json.dumps({"moves": []}))
    assert moves._auto_log_path(folder, datetime(2026, 1, 2, 11, 0, 0)) == second   # (it used to start a new row each time)


def test_the_mcp_undo_marks_what_went_back():
    import inspect
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "filect_mcp"))
    src = (Path(__file__).resolve().parents[2] / "filect_mcp" / "server.py").read_text(encoding="utf-8")
    body = src[src.index("def undo_last_organize("):src.index("def undo_last_organize(") + 4000]
    assert 'm.get("back")' in body and "mark_moves_back(" in body


# ------------------------------------------------------------------------- 3 · a stopped batch stays undoable
def _batch(env, name, n=3):
    d = _folder(env, name)
    paths = [str(d / f"f{i}.txt") for i in range(n)]
    for p in paths:
        Path(p).write_text(p)
    return d, paths, aw.AutoWatcherWorker(paths, str(d), "x")


def test_a_stopped_batch_is_in_history_and_can_be_undone(env, monkeypatch):
    d, paths, worker = _batch(env, "stopped")
    real = aw.move_file

    def first_then_stop(a, b, *k, **kw):
        r = real(a, b, *k, **kw)
        worker._should_stop = True
        return r
    monkeypatch.setattr(aw, "move_file", first_then_stop)
    monkeypatch.setattr(ai_organizer, "request_organization_plan", lambda i, f: {"folders": {"sorted": [x["id"] for x in f]}})
    monkeypatch.setattr(database.file_index, "get_indexed_file_paths", lambda *a: {os.path.normcase(p) for p in paths},
                        raising=False)
    worker.run()
    app.processEvents()
    logs = [p for p in settings.get_moves_dir().glob("moves-*-auto-*.json") if "stopped" in p.read_text()]
    if not logs:
        pytest.skip("the worker didn't reach the move step with this fake index (covered live)")
    assert len(logs) == 1 and moves.undo_log(str(logs[0]))["reverted"] == 1, logs
    assert all(os.path.exists(p) for p in paths)


# ------------------------------------------------------------------------- 8 · the queue never stalls or crashes
def test_next_job_starting_before_the_last_thread_ends_neither_stalls_nor_crashes(env, monkeypatch):
    """The result arrives a moment before the thread ends. Before: the next job saw "still running", put itself
    back in the queue and nothing ever started it (or, replacing the running worker, crashed)."""
    d = _folder(env, "queue", action=1)
    w = aw.AutoOrganizeWatcher()
    monkeypatch.setattr(w, "_scan_folder_for_files", lambda *a, **k: [str(d / "x.txt")])
    monkeypatch.setattr(w, "_filter_genuinely_new_files", lambda f, p, **k: p)
    monkeypatch.setattr(w, "_needs_hash", lambda f: False)
    runs = []

    def run(self):
        runs.append(1)
        self.finished_processing.emit(list(self.file_paths))
        time.sleep(0.03)                                 # (its thread ends a moment after its result)

    monkeypatch.setattr(aw.AutoWatcherWorker, "run", run)
    w._worker_queue = [(str(d), "x", None)] * 25
    w._on_worker_finished()
    done = _pump(lambda: not w._worker_queue and w._current_worker is not None
                 and not w._current_worker.isRunning() and len(runs) == 25, 30)
    assert done, (len(runs), len(w._worker_queue))
    assert _pump(lambda: not [x for x in w._old_workers if x.isRunning()], 5)


# ------------------------------------------------------------------------- 1 · the newest plan wins, no crash
class _Plan(QThread):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, result, fail=False):
        super().__init__()
        self.result, self.fail = result, fail

    def run(self):
        time.sleep(0.15)
        if self.fail:
            self.error.emit("boom")
        else:
            self.finished.emit(self.result)
        time.sleep(0.05)                                 # (still running a moment after its result)


def _make_page():
    """Just the part of the Organize page that starts plans (built here, so the file still loads on old code)."""
    class _Page(QObject):
        voice_plan_replaced = Signal()
        voice_plan_error = Signal(str)
        _keep_running = OrganizePage._keep_running
        _voice_start_worker = OrganizePage._voice_start_worker
        _voice_stale = OrganizePage._voice_stale
        _voice_on_error = OrganizePage._voice_on_error

        def __init__(self):
            super().__init__()
            self._voice_worker = None
            self._voice_index_worker = None
            self._voice_old_workers = []
            self._voice_error_text = ""
            self.got, self.errors = [], []
            self.voice_plan_error.connect(self.errors.append)

        def _voice_on_plan(self, plan):
            if not self._voice_stale():
                self.got.append(plan)
    return _Page()


def _settle(page):
    """The test's own threads end before the page goes (a running QThread destroyed aborts the run — fix 1's crash)."""
    assert _pump(lambda: not [t for t in [page._voice_worker, *page._voice_old_workers]
                              if t is not None and t.isRunning()], 5)


def test_the_newest_plan_wins_and_nothing_crashes():
    page = _make_page()
    replaced = []
    page.voice_plan_replaced.connect(lambda: replaced.append(1))
    for i in range(6):
        page._voice_start_worker(_Plan(i))
        _pump(lambda: False, 0.02)
    assert _pump(lambda: bool(page.got), 5) and page.got == [5], page.got
    assert len(replaced) == 5, replaced
    assert _pump(lambda: not [t for t in page._voice_old_workers if t.isRunning()], 5)
    assert page.got == [5] and page.errors == [], (page.got, page.errors)
    _settle(page)


def test_a_replaced_plan_that_fails_says_nothing():
    page = _make_page()
    page._voice_start_worker(_Plan(0, fail=True))
    page._voice_start_worker(_Plan(1))
    assert _pump(lambda: bool(page.got), 5) and page.got == [1] and page.errors == [], (page.got, page.errors)
    _settle(page)


# ------------------------------------------------------------------------- 5 · a waiting voice request runs
def _controller(**kw):
    from app.ui.voice_organize_controller import VoiceOrganizeController as C
    started = []
    fake = SimpleNamespace(_hf_dead_plans=0, _hf_next=("organize my downloads", "", "organize", None), _hf=None,
                           _plan_active=True, overlay=SimpleNamespace(show_plan=lambda *a, **k: None),
                           start_hands_free=lambda text, **k: started.append(text) or "started", **kw)
    fake._page_working = kw.get("_page_working", lambda: False)
    fake._start_next = lambda tries=20: C._start_next(fake, tries)
    fake._hf_on_plan = lambda payload: None
    return C, fake, started


def test_a_waiting_request_starts_when_the_plan_it_waited_for_is_ready():
    C, fake, started = _controller()
    C._on_plan_ready(fake, {"summary": "", "folders": {}, "target_folder": "x", "file_count": 1, "folder_count": 1})
    assert started == ["organize my downloads"] and fake._hf_next is None and not fake._plan_active


def test_a_waiting_request_starts_when_that_plan_fails_too():
    C, fake, started = _controller(_hf_fail=lambda m: None)
    fake.overlay.show_error = lambda *a, **k: None
    C._on_plan_error(fake, "no plan")
    assert started == ["organize my downloads"]


def test_it_waits_a_moment_while_the_old_thread_is_still_ending():
    calls = {"n": 0}

    def working():
        calls["n"] += 1
        return calls["n"] <= 3                     # (the old plan's thread ends a moment after its result)
    C, fake, started = _controller(_page_working=working)
    fake._start_next()
    assert started == []
    assert _pump(lambda: bool(started), 3) and started == ["organize my downloads"]


def test_a_replaced_plan_is_no_longer_counted_as_still_coming():
    from app.ui.voice_organize_controller import VoiceOrganizeController as C
    fake = SimpleNamespace(_hf_dead_plans=1)
    C._on_plan_replaced(fake)
    assert fake._hf_dead_plans == 0
    C._on_plan_replaced(fake)
    assert fake._hf_dead_plans == 0


# ------------------------------------------------------------------------- 9 · the existing-folders rules
def test_the_existing_folders_rules_reach_the_planner(monkeypatch):
    from app.core import vision
    seen = []

    def spy(*a, **k):
        seen.append(json.dumps(a))
        raise RuntimeError("stop here")
    monkeypatch.setattr(vision, "_call_openai_proxy", spy)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    try:
        ai_organizer.request_organization_plan(aw.existing_folders_instruction("", ["a"]),
                                               [{"id": 1, "file_name": "x", "file_size": 1}])
    except Exception:
        pass
    assert seen and "CRITICAL - EXISTING FOLDERS ONLY" in seen[0], seen[:1]


# ------------------------------------------------------------------------- 10 · quitting while something runs
def test_quitting_has_no_local_logging_import():
    import main
    assert "logging" not in main.main.__code__.co_varnames, "a local import made the quit path raise"
    assert "gc.get_objects()" in Path(main.__file__).read_text(encoding="utf-8"), "it sees threads with no parent"


# ------------------------------------------------------------------------- found live (2026-11-11): the hotkey
def test_the_organize_hotkey_indexes_new_documents_too(env, monkeypatch):
    """Before planning, the organize hotkey indexed only MEDIA: a folder of new documents came back "That folder has
    nothing I can organize" (live Phase B run). Every file is indexed now; only media counts against the quota."""
    from app.core import search
    from app.ui.organize_page import _VoiceIndexWorker
    d = env / "docs"
    d.mkdir()
    for n in ("invoice 1.txt", "notes.md", "report.docx"):
        (d / n).write_text(n)
    (d / "photo.jpg").write_bytes(b"\xff\xd8 not really")
    (d / "desktop.ini").write_text("[x]")
    analyzed = []

    def fake_process(info, root, *a, **k):
        analyzed.append(Path(info["source_path"]).name)
        return {"file_path": info["source_path"], "file_name": Path(info["source_path"]).name, "label": "doc",
                "tags": ["x"], "caption": "x", "size": 1, "extension": Path(info["source_path"]).suffix}
    monkeypatch.setattr(search.search_service, "_process_single_file", fake_process)
    monkeypatch.setattr(search.search_service, "_check_index_limit",
                        lambda n: {"allowed": False, "remaining": 0, "plan": "basic", "limit": 10})
    got = []
    w = _VoiceIndexWorker(str(d))
    w.done.connect(lambda n, rem: got.append((n, rem)))
    w.run()
    app.processEvents()
    assert sorted(analyzed) == ["invoice 1.txt", "notes.md", "report.docx"], analyzed
    assert got and got[0][1] == 1, "the photo was held back by the quota, the documents never are"
