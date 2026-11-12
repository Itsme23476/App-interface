"""Phase A urgent fixes (2026-11-11), from the Mac handoff of 2026-11-11 and what the analysis found on Windows.

A1  Signing out signs out THIS device only (the library's default "global" signed the Mac out too).
A2  A file open in another program is never half-copied: shutil.move copied it to the destination and then failed
    to delete the original (two copies). Now it stays where it was, reported as busy.
A3  Auto-organize never marks files done when their plan or move failed (one offline moment and they were never
    sorted that session); tries again after 1, 5, 30 min. A file open elsewhere waits with no AI call; files the
    plan deliberately left in place aren't planned again every few seconds.

Temp folders only, a throwaway index and History; settings.json never written; no network or AI (fake planner).
"""
import errno
import os
import subprocess
import sys
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


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    moves = tmp_path / "moves"
    moves.mkdir()
    monkeypatch.setattr(settings, "get_moves_dir", lambda: moves)
    monkeypatch.setattr(settings, "auto_organize_folders", [])
    monkeypatch.setattr(database, "file_index", database.FileIndex(tmp_path / "index.db"))
    # (these files are written long ago, as far as the watcher's still-being-written check goes: Phase B #2 has its
    # own tests in test_organize_recovery.py)
    monkeypatch.setattr(aw, "STILL_WRITING_SECS", -10 ** 9)
    from app.core import search
    monkeypatch.setattr(search, "SearchService", _FakeIndexer)        # (no real indexing: no network, no AI)
    from app.core import vision
    monkeypatch.setattr(vision, "_call_openai_proxy",
                        lambda *a, **k: pytest.fail("a test reached Filect's backend"))
    d = tmp_path / "Downloads"
    d.mkdir()
    yield SimpleNamespace(tmp=tmp_path, d=d, moves=moves)


class _FakeIndexer:
    """Stands in for SearchService: indexing = a row in the throwaway index."""
    def __init__(self, *a, **k):
        pass

    def index_single_file(self, path, force_ai=False, **k):
        database.file_index.add_file({"source_path": str(path), "name": os.path.basename(str(path)),
                                      "extension": os.path.splitext(str(path))[1], "size": os.path.getsize(path),
                                      "tags": ["note"], "label": "note"})
        return {"success": True}


def _file(path, text=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else f"{path.name} {time.time_ns()}", encoding="utf-8")
    return path


class Holder:
    """Another program holding a file open for writing (default Windows sharing, like Word or a recorder)."""
    def __init__(self, path):
        self.p = subprocess.Popen([sys.executable, "-c",
                                   f"f = open(r'{path}', 'ab'); print('open', flush=True); import time; time.sleep(60)"],
                                  stdout=subprocess.PIPE, text=True)
        assert self.p.stdout.readline().strip() == "open"

    def close(self, path=None):
        self.p.kill()
        self.p.wait(10)
        # (Windows Defender opens a file for a moment right after a program lets go of it: wait that out, the way
        # the watcher does by simply checking again a few seconds later)
        end = time.time() + 10
        while path is not None and not aw._can_move(str(path)) and time.time() < end:
            time.sleep(0.1)


def _fake_planner(monkeypatch, folders=None, result="plan"):
    from app.core import ai_organizer

    def plan(instruction, files_info, *a, **k):
        if result == "none":
            return None
        if result == "raise":
            raise RuntimeError("the network went away")
        ids = {f["file_name"]: f["id"] for f in files_info}
        return {"folders": {name: [ids[n] for n in names if n in ids] for name, names in (folders or {}).items()}}

    monkeypatch.setattr(ai_organizer, "request_organization_plan", plan)
    monkeypatch.setattr(aw, "request_organization_plan", plan, raising=False)
    for name in ("_apply_tag_safety_net", "deduplicate_plan", "ensure_all_files_included"):
        monkeypatch.setattr(ai_organizer, name, lambda p, *a, **k: p)
        monkeypatch.setattr(aw, name, lambda p, *a, **k: p, raising=False)
    monkeypatch.setattr(ai_organizer, "validate_plan", lambda p, ids, *a, **k: (True, []))
    monkeypatch.setattr(aw, "validate_plan", lambda p, ids, *a, **k: (True, []), raising=False)


def _run(worker):
    out = SimpleNamespace(done=None, not_done=[], busy=[])
    worker.finished_processing.connect(lambda files: setattr(out, "done", list(files)))
    worker.not_done.connect(lambda files, why: out.not_done.append((list(files), why)))
    worker.busy.connect(lambda files: out.busy.append(list(files)))
    worker.run()
    return out


# ------------------------------------------------------------------------------------------------ A1 sign-out
def test_sign_out_ends_only_this_devices_session(monkeypatch):
    from app.core.supabase_client import supabase_auth
    calls = []

    class FakeAuth:
        def sign_out(self, options=None):
            calls.append(options)
    monkeypatch.setattr(supabase_auth, "_auth_client", FakeAuth())
    monkeypatch.setattr(supabase_auth, "_user", {"id": "u"})
    assert supabase_auth.sign_out()["success"]
    assert calls == [{"scope": "local"}], "never the library's default 'global' (it signed the Mac out as well)"
    assert supabase_auth._user is None


def test_the_installed_library_turns_local_into_a_local_logout():
    """What `scope` reaches Supabase's logout endpoint: the library passes it straight through as ?scope=local."""
    import inspect
    from supabase_auth._sync import gotrue_admin_api, gotrue_client
    src = inspect.getsource(gotrue_client.SyncGoTrueClient.sign_out)
    assert 'signout_options["scope"]' in src and 'options or {"scope": "global"}' in src
    assert "query=QueryParams(scope=scope)" in inspect.getsource(gotrue_admin_api.SyncGoTrueAdminAPI.sign_out)


# ----------------------------------------------------------------------------- A2 a file open in another program
def test_a_file_open_elsewhere_is_never_half_copied(env):
    """(Reproduced on this PC first: shutil.move raised 'being used by another process' AFTER writing a full copy.)"""
    src = _file(env.d / "recording.txt", "x" * 200_000)
    dst = env.d / "Sorted" / "recording.txt"
    dst.parent.mkdir()
    h = Holder(src)
    try:
        with pytest.raises(apply_mod.FileBusyError):
            apply_mod.move_file(src, dst)
        assert src.exists() and src.stat().st_size == 200_000 and not dst.exists(), "untouched; no copy anywhere"
    finally:
        h.close(src)
    assert apply_mod.move_file(src, dst) == str(dst) and dst.exists() and not src.exists(), "free again: it moves"


def test_move_file_never_overwrites(env):
    a, b = _file(env.d / "a.txt", "mine"), _file(env.d / "b.txt", "theirs")
    with pytest.raises(FileExistsError):
        apply_mod.move_file(a, b)
    assert b.read_text(encoding="utf-8") == "theirs" and a.exists()


def test_to_another_drive_it_copies_then_deletes_and_never_leaves_two(env, monkeypatch):
    real_rename, real_unlink = os.rename, os.unlink

    def cross_drive(s, d):
        e = OSError(errno.EXDEV, "not the same device")
        e.winerror = 17
        raise e
    monkeypatch.setattr(os, "rename", cross_drive)
    a = _file(env.d / "a.txt", "aaa")
    apply_mod.move_file(a, env.d / "x" / "a.txt") if (env.d / "x").mkdir() is None else None
    assert (env.d / "x" / "a.txt").read_text(encoding="utf-8") == "aaa" and not a.exists()

    b = _file(env.d / "b.txt", "bbb")

    def busy_unlink(p, *k, **kw):
        if str(p) == str(b):
            e = PermissionError(13, "in use")
            e.winerror = 32
            raise e
        return real_unlink(p, *k, **kw)
    monkeypatch.setattr(os, "unlink", busy_unlink)
    with pytest.raises(apply_mod.FileBusyError):
        apply_mod.move_file(b, env.d / "x" / "b.txt")
    assert b.exists() and not (env.d / "x" / "b.txt").exists(), "the copy is removed again: never two copies"
    monkeypatch.setattr(os, "rename", real_rename)


def test_organize_moves_the_rest_and_says_which_was_open(env):
    a, b = _file(env.d / "a.txt"), _file(env.d / "open.docx")
    plan = [{"file_name": n, "source_path": str(env.d / n), "destination_path": str(env.d / "Docs" / n)}
            for n in ("a.txt", "open.docx")]
    h = Holder(b)
    try:
        ok, errors, log_file, _ = apply_mod.apply_moves(plan)
    finally:
        h.close()
    assert not ok and (env.d / "Docs" / "a.txt").exists()
    assert b.exists() and not (env.d / "Docs" / "open.docx").exists(), "the open file stays, no copy in Docs"
    assert errors == ["open.docx is open in another program, so it was left where it is. Close it and organize again."]


def test_undo_leaves_an_open_file_and_can_be_finished_later(env):
    a = _file(env.d / "a.txt")
    plan = [{"file_name": "a.txt", "source_path": str(a), "destination_path": str(env.d / "Docs" / "a.txt")}]
    _, _, log_file, _ = apply_mod.apply_moves(plan)
    moved = env.d / "Docs" / "a.txt"
    h = Holder(moved)
    try:
        res = apply_mod.undo_log(log_file)
    finally:
        h.close()
    assert res["reverted"] == 0 and moved.exists() and not a.exists(), "nothing copied back to the old place"
    assert "open in another program" in res["errors"][0]


# ------------------------------------------------------------------- A3 auto-organize: failures are tried again
def test_no_plan_marks_nothing_done_and_asks_for_a_retry(env, monkeypatch):
    a, b = _file(env.d / "a.txt"), _file(env.d / "b.txt")
    _fake_planner(monkeypatch, result="none")
    out = _run(aw.AutoWatcherWorker([str(a), str(b)], str(env.d), ""))
    assert out.done == [] and out.not_done == [([str(a), str(b)], "no plan")]


def test_a_planner_error_marks_nothing_done(env, monkeypatch):
    a = _file(env.d / "a.txt")
    _fake_planner(monkeypatch, result="raise")
    out = _run(aw.AutoWatcherWorker([str(a)], str(env.d), ""))
    assert out.done == [] and out.not_done == [([str(a)], "plan failed")] and a.exists()


def test_a_file_open_elsewhere_in_a_batch_waits_and_the_rest_are_sorted(env, monkeypatch):
    a, b = _file(env.d / "a.txt"), _file(env.d / "open.docx")
    _fake_planner(monkeypatch, {"Docs": ["a.txt", "open.docx"]})
    h = Holder(b)
    try:
        out = _run(aw.AutoWatcherWorker([str(a), str(b)], str(env.d), ""))
    finally:
        h.close()
    assert (env.d / "Docs" / "a.txt").exists() and b.exists() and not (env.d / "Docs" / "open.docx").exists()
    assert out.busy == [[str(b)]] and out.not_done == []
    assert str(b) not in out.done, "not marked done: sorted once it's closed"


def test_files_the_plan_leaves_in_place_count_as_dealt_with(env, monkeypatch):
    """(They weren't marked done, so the next check — 3 s later — planned them again, every time.)"""
    a, b = _file(env.d / "a.txt"), _file(env.d / "keep.txt")
    _fake_planner(monkeypatch, {"Docs": ["a.txt"]})                    # (b: the plan leaves it where it is)
    out = _run(aw.AutoWatcherWorker([str(a), str(b)], str(env.d), ""))
    assert str(b) in out.done and out.not_done == [] and b.exists()


def test_the_retry_ladder_1_5_30_minutes_then_left_until_the_next_start(env):
    w = aw.AutoOrganizeWatcher()
    p = str(env.d / "a.txt")
    seen = []
    for expect in aw.RETRY_AFTER:
        t = time.time()
        w._on_worker_not_done([p], "no plan")
        tries, at = w._retries[aw._path_key(p)]
        seen.append(round(at - t))
        assert abs(at - t - expect) < 2
    assert seen == [60, 300, 1800]
    w._on_worker_not_done([p], "no plan")
    assert aw._path_key(p) not in w._retries and os.path.normpath(p) in w._organized_files, "left until next start"


def test_the_check_holds_a_failed_file_until_its_retry_time(env, monkeypatch):
    a = _file(env.d / "a.txt")
    w = aw.AutoOrganizeWatcher()
    w._is_running = True
    w.watched_folders = [os.path.normpath(str(env.d))]
    sent = []
    monkeypatch.setattr(w, "_process_files_with_ai", lambda files, folder, *x, **k: sent.append(list(files)))
    monkeypatch.setattr(w, "_filter_genuinely_new_files", lambda folder, ready, **k: ready)
    w._debounce_seconds = 0
    w._retries[aw._path_key(str(a))] = (1, time.time() + 60)
    found = {"found": {os.path.normpath(str(env.d)): [str(a)]}, "hashes": {}}
    w._after_check({**found, "now": time.time()})
    w._after_check({**found, "now": time.time() + 1})
    assert sent == [], "waits for its next try"
    w._after_check({**found, "now": time.time() + 61})
    w._after_check({**found, "now": time.time() + 62})
    assert sent == [[str(a)]], "tried again once its time came"


def test_the_check_never_plans_a_file_that_is_open_elsewhere(env, monkeypatch):
    """No AI call per check while Word holds the file; planned once it's free."""
    a = _file(env.d / "report.docx")
    w = aw.AutoOrganizeWatcher()
    w._is_running = True
    w.watched_folders = [os.path.normpath(str(env.d))]
    sent = []
    monkeypatch.setattr(w, "_process_files_with_ai", lambda files, folder, *x, **k: sent.append(list(files)))
    monkeypatch.setattr(w, "_filter_genuinely_new_files", lambda folder, ready, **k: ready)
    w._debounce_seconds = 0
    found = {"found": {os.path.normpath(str(env.d)): [str(a)]}, "hashes": {}}
    h = Holder(a)
    try:
        assert aw._can_move(str(a)) is False
        for i in range(4):
            w._after_check({**found, "now": time.time() + i})
        assert sent == [], "open in another program: no plan, no AI call"
    finally:
        h.close(a)
    assert aw._can_move(str(a)) is True
    w._after_check({**found, "now": time.time() + 10})
    assert sent == [[str(a)]], "closed: planned"


def test_an_organized_file_leaves_the_retry_list(env):
    w = aw.AutoOrganizeWatcher()
    p = str(env.d / "a.txt")
    w._on_worker_not_done([p], "no plan")
    w._on_worker_file_organized(p, str(env.d / "Docs" / "a.txt"), "Docs")
    assert aw._path_key(p) not in w._retries


# ------------------------------------------------------------------ A7 the voice decider saves no chats
def test_the_decider_tells_claude_code_to_save_nothing(monkeypatch, tmp_path):
    """Every sentence the decider classified became a saved Claude Code chat (114 of 166 on this PC)."""
    import asyncio
    import claude_agent_sdk as sdk
    from app.core import intent_router as ir
    seen = {}

    class FakeClient:
        def __init__(self, options=None):
            seen["opts"] = options

        async def __aenter__(self):
            return self
    monkeypatch.setattr(sdk, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(settings, "get_app_data_dir", lambda: tmp_path)
    r = ir.IntentRouter.__new__(ir.IntentRouter)
    r._model = "haiku"
    asyncio.run(r._open())
    o = seen["opts"]
    assert "no-session-persistence" in (o.extra_args or {}), o.extra_args
    assert o.max_turns == 1 and o.allowed_tools == [] and str(o.cwd).endswith("agent-router")


def _chat(root, folder, sid, title, mtime):
    import json
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text(json.dumps({"type": "user", "sessionId": sid, "cwd": "C:\\work",
                             "message": {"role": "user", "content": title}}) + "\n", encoding="utf-8")
    os.utime(f, (mtime, mtime))
    return f


def test_junk_never_uses_up_the_chat_lists_places(monkeypatch, tmp_path):
    from app.core import agent_sessions as a
    root = tmp_path / "projects"
    monkeypatch.setattr(a, "_projects_root", lambda: root)
    now = time.time()
    for i in range(6):                                   # the decider's own chats: newest of all
        _chat(root, "C--Users-me-AppData-Roaming-ai-file-organizer-agent-router", f"d{i}",
              f"make it bigger {i}", now - i)
    _chat(root, "C--work", "j1", "Current conversation: none. Spoken command: hi", now - 10)
    for i in range(3):
        _chat(root, "C--work", f"r{i}", f"Build a landing page {i}", now - 100 - i)
    old = a.list_sessions(limit=4)
    assert not any(s.id.startswith("r") for s in old), "(before: junk filled the 4 places)"
    real = a.list_sessions(limit=4, real_only=True)
    assert [s.id for s in real] == ["r0", "r1", "r2"], [s.id for s in real]
    assert all(not a.is_junk(s) for s in real)
    assert [s.id for s in a.find_by_name("landing page")] == ["r0", "r1", "r2"]


def test_the_page_and_the_shared_rule_agree():
    from app.core import agent_sessions as a
    from app.ui.assistant_page import AssistantPage
    for title, junk in [("", True), ("Current conversation: x", True), ("Spoken command: open", True),
                        ("This session is being continued from a previous", True), ("Fix the login bug", False)]:
        s = SimpleNamespace(title=title)
        assert a.is_junk(s) is junk and AssistantPage._junk_session(s) is junk, title
