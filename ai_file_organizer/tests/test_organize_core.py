"""Organize a folder by voice + set up auto-organize by voice: the CORE (core/organize_reader, apply, ai_organizer,
intent_router, settings). Ported from the Mac's tests/test_voice_organize.py (the parts in these files), plus the
Windows-only rules: hidden / system attribute, junctions, program folders, paths without case, OneDrive
placeholders, HEIC through Pillow, a scanned PDF through pypdfium2, psutil, Codex on stdin.

No real agents, network or backend; real files only in temp folders; a throwaway index and History; the user's
settings.json is never written.
Run: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_organize_core.py -q -p no:cacheprovider"""
import builtins
import io
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.settings import Settings, settings
from app.core import ai_organizer, database, intent_router as ir, organize_reader as r
from app.core import apply as apply_mod

IS_WIN = sys.platform.startswith("win")
win_only = pytest.mark.skipif(not IS_WIN, reason="Windows-only rule")


def _no_agent(agent, items, cancelled):
    raise r._AgentOut("no real agents in tests")


def _no_network(*a, **k):
    return None


def _no_backend(*a, **k):
    return {"_error": "no backend in tests"}


@pytest.fixture(autouse=True)
def sandbox(tmp_path_factory, monkeypatch):
    """A throwaway index + History, an empty auto-organize list, and nothing real to call."""
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)      # NEVER write the user's settings.json
    moves = tmp_path_factory.mktemp("filect-moves")
    monkeypatch.setattr(settings, "get_moves_dir", lambda: moves)
    monkeypatch.setattr(database, "file_index", database.FileIndex(tmp_path_factory.mktemp("filect-index") / "i.db"))
    monkeypatch.setattr(settings, "auto_organize_folders", [])
    monkeypatch.setattr(settings, "exclusion_patterns", settings._get_default_exclusions())
    monkeypatch.setattr(settings, "pinned_paths", [])
    monkeypatch.setattr(settings, "enable_ocr_indexing", False)
    monkeypatch.setattr(r, "_ask", _no_agent)
    import app.core.vision as vision
    monkeypatch.setattr(vision, "_call_openai_proxy", _no_network)
    from app.core.search import search_service
    monkeypatch.setattr(search_service, "_process_single_file", _no_backend)
    yield moves


@pytest.fixture
def mk(tmp_path):
    """A new empty test folder each call."""
    n = [0]

    def make(name=None):
        n[0] += 1
        d = tmp_path / (name or f"filect-test-{n[0]}")
        d.mkdir(parents=True, exist_ok=True)
        return d
    return make


def _ok(items):
    return json.dumps({"files": [{"id": i + 1, "type": "note", "caption": "c", "tags": ["t"]} for i in range(len(items))]})


def _notes(d, n):
    out = []
    for i in range(n):
        p = d / f"n{i}.txt"
        p.write_text(f"note {i} in {d.name}")
        out.append(str(p))
    return out


def _rel(paths, d):
    return sorted(os.path.relpath(p, d) for p in paths)


_HIDDEN, _SYSTEM, _READONLY, _NORMAL = 0x2, 0x4, 0x1, 0x80


def _attr(path, attrs):
    import ctypes
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs), path


# ---- organize_reader: what may move ------------------------------------------------------------------------
def test_parse_answer():
    assert r.parse_answer('```json\n{"files":[{"id":1,"type":"Invoice","tags":["A"]},{"id":9,"type":"x"}]}\n```', 2) == {
        1: {"label": "invoice", "caption": "", "tags": ["a"]}}
    assert r.parse_answer("sorry", 2) == {} and r.parse_answer("{not json}", 2) == {}
    assert r.parse_answer('{"files":[{"id":"2","caption":"c"}]}', 2) == {2: {"label": "other", "caption": "c", "tags": []}}


def test_only_what_may_move(mk):
    d = mk()
    (d / "a.txt").write_text("a")
    (d / ".hidden").write_text("h")
    try:
        os.symlink(d / "a.txt", d / "link")
    except (OSError, NotImplementedError):
        pass        # (Windows without Developer Mode can't make one: the junction test covers links)
    for rel in ("proj/.git/HEAD", "proj/src/x.js", "tool/requirements.txt", "tool/m.txt", "Cool.app/Contents/i.txt",
                "Lib.photoslibrary/o/p.jpg", "node_modules/x/i.js"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("x")
    (d / "normal").mkdir()
    (d / "normal" / "b.txt").write_text("b")
    s = r.scan(d)
    assert _rel(s["files"], d) == ["a.txt", os.path.join("normal", "b.txt")], s
    assert r.protected(d / "tool" / "m.txt", d) and r.protected(d / "Cool.app/Contents/i.txt", d)
    assert not r.protected(d / "normal" / "b.txt", d) and r.protected(d / ".hidden", d)
    # a lone project file at the top is just a file; a repository at the top is refused
    e = mk()
    (e / "package.json").write_text("{}")
    assert len(r.scan(e)["files"]) == 1
    (e / ".git").mkdir()
    assert r.scan(e) == {"error": "project"}
    assert r.scan(e / "nope") == {"error": "missing"}


def test_too_many_stops_early(mk, monkeypatch):
    d = mk()
    monkeypatch.setattr(r, "MAX_FILES", 5)
    _notes(d, 8)
    assert r.scan(d) == {"error": "too_many"}


def test_batches_run_at_once(mk, monkeypatch):
    d = mk()
    paths = _notes(d, 45)
    monkeypatch.setattr(r, "_memory_gb", lambda: 16.0)     # (this dev PC has 8 GB: only 2 runs at once there)
    sizes, live, peak, lock = [], [0], [0], threading.Lock()

    def fake(agent, items, cancelled):
        with lock:
            sizes.append(len(items))
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.4)
        with lock:
            live[0] -= 1
        return _ok(items)
    monkeypatch.setattr(r, "_ask", fake)
    seen = []
    res = r.read_files(paths, ["claude"], progress=lambda a, b: seen.append(a))
    assert sorted(sizes) == [5, 20, 20] and peak[0] == 3, (sizes, peak)
    assert res["by"] == {"claude": 45} and seen[-1] == 45 and r.scan(d)["unread"] == []
    assert r.parallel_runs(1) == 1 and r.parallel_runs(21) == 2 and r.parallel_runs(10000) <= 4


def test_when_an_agent_cant(mk, monkeypatch):
    d = mk()
    paths = _notes(d, 6)
    from app.core.search import search_service as ss
    monkeypatch.setattr(ss, "_process_single_file", lambda fd, dp, force_ai=False, **k: {
        "source_path": fd["source_path"], "name": fd["name"], "extension": ".txt", "size": 1, "label": "b", "tags": ["b"],
        "caption": "b", "is_file": True, "_file_path": Path(fd["source_path"])})

    def out(agent, items, c):
        if agent == "claude":
            raise r._AgentOut("usage")
        return _ok(items)
    monkeypatch.setattr(r, "_ask", out)
    assert r.read_files(paths, ["claude", "codex"])["by"] == {"codex": 6}

    def both(agent, items, c):
        raise r._AgentOut("usage")
    monkeypatch.setattr(r, "_ask", both)
    assert r.read_files(paths, ["claude", "codex"])["by"] == {"backend": 6}
    monkeypatch.setattr(r, "_ask", lambda a, i, c: "garbage")
    assert r.read_files(paths, ["claude"])["by"] == {"backend": 6}
    monkeypatch.setattr(ss, "_process_single_file", lambda *a, **k: {"_error": "no"})
    assert r.read_files(paths, ["claude"])["by"] == {"name": 6}, "never lost"


# ---- apply: moves, Undo, History -----------------------------------------------------------------------------
def test_undo_and_history(mk):
    d = mk()
    (d / "a.txt").write_text("a")
    (d / "b.txt").write_text("b")
    plan = [{"source_path": str(d / n), "destination_path": str(d / "Notes" / n), "file_name": n, "size": 1, "category": "x"}
            for n in ("a.txt", "b.txt")]
    ok, errors, log, _ = apply_mod.apply_moves(plan)
    assert ok and (d / "Notes" / "a.txt").exists()
    mine = [h for h in apply_mod.get_move_history() if h["log_file"] == log]
    assert mine and not mine[0]["reverted"] and mine[0]["source"] == "" and mine[0]["folder"] == ""
    res = apply_mod.undo_log(log)
    assert res == {"reverted": 2, "skipped": 0, "errors": []} and (d / "a.txt").exists()
    assert not (d / "Notes").exists(), "the folder it made is gone"
    assert [h for h in apply_mod.get_move_history() if h["log_file"] == log][0]["reverted"]
    # one file went elsewhere; another's old spot was taken
    time.sleep(1.1)                     # (move logs are named to the second)
    (d / "c.txt").write_text("c")
    (d / "e.txt").write_text("e")
    plan = [{"source_path": str(d / n), "destination_path": str(d / "N2" / n), "file_name": n, "size": 1, "category": "x"}
            for n in ("c.txt", "e.txt", "a.txt")]
    _, _, log, _ = apply_mod.apply_moves(plan)
    (d / "N2" / "c.txt").unlink()
    (d / "e.txt").write_text("someone else")
    res = apply_mod.undo_log(log)
    assert res["reverted"] == 1 and res["skipped"] == 2 and (d / "N2" / "e.txt").exists(), "nothing overwritten"


def test_a_file_named_like_a_new_folder(mk):
    d = mk()
    (d / "Money").write_text("I am a file")
    (d / "a.txt").write_text("a")
    plan = [{"source_path": str(d / "a.txt"), "destination_path": str(d / "Money" / "a.txt"), "file_name": "a.txt"},
            {"source_path": str(d / "Money"), "destination_path": str(d / "other" / "Money"), "file_name": "Money"}]
    ok, errors, log, renamed = apply_mod.apply_moves(plan)
    assert ok and (d / "Money" / "a.txt").read_text() == "a", errors
    assert (d / "other" / "Money").read_text() == "I am a file", "the file moved, not the new folder"
    assert renamed == 1
    assert json.loads(Path(log).read_text(encoding="utf-8"))["renamed_files"][0]["new_name"] == "Money (file)"


@win_only
def test_a_file_named_like_a_new_folder_in_other_case(mk):
    """Windows names have no case: a file "money" blocks a "Money" folder, and a later move of "MONEY" is it."""
    d = mk()
    (d / "money").write_text("lower-case file")
    (d / "b.txt").write_text("b")
    plan = [{"source_path": str(d / "b.txt"), "destination_path": str(d / "Money" / "b.txt"), "file_name": "b.txt"},
            {"source_path": str(d / "MONEY"), "destination_path": str(d / "other" / "MONEY"), "file_name": "MONEY"}]
    ok, errors, log, _ = apply_mod.apply_moves(plan)
    assert ok, errors
    assert "Money" in os.listdir(d) and (d / "Money" / "b.txt").read_text() == "b"
    assert (d / "other" / "MONEY").read_text() == "lower-case file", "followed it to its new name"
    renamed = json.loads(Path(log).read_text(encoding="utf-8"))["renamed_files"][0]
    assert renamed["original_name"] == "money" and renamed["new_name"] == "money (file)", "keeps its own spelling"


def test_emptied_folders_wait_for_undo(mk):
    d = mk()
    (d / "old").mkdir()
    (d / "old" / "a.txt").write_text("a")
    plan = [{"source_path": str(d / "old" / "a.txt"), "destination_path": str(d / "Notes" / "a.txt"), "file_name": "a.txt"}]
    _, _, log, _ = apply_mod.apply_moves(plan)
    assert (d / "old").is_dir(), "apply itself never removes it"
    apply_mod.tidy_emptied_sources(log, str(d))
    assert not (d / "old").exists() and (d / "Notes").is_dir() and d.is_dir(), "gone once Undo isn't offered; never the folder itself"
    # undone in time: nothing to tidy
    time.sleep(1.1)
    (d / "old").mkdir()
    (d / "old" / "b.txt").write_text("b")
    plan = [{"source_path": str(d / "old" / "b.txt"), "destination_path": str(d / "N" / "b.txt"), "file_name": "b.txt"}]
    _, _, log, _ = apply_mod.apply_moves(plan)
    apply_mod.undo_log(log)
    apply_mod.tidy_emptied_sources(log, str(d))
    assert (d / "old" / "b.txt").exists()


@win_only
def test_explorer_files_dont_keep_a_folder(mk):
    """desktop.ini / Thumbs.db (Explorer's own) count as empty, like the Mac's .DS_Store; real files never do."""
    d = mk()
    for n in ("a.txt", "b.txt"):
        (d / n).write_text(n)
    plan = [{"source_path": str(d / "a.txt"), "destination_path": str(d / "Notes" / "a.txt"), "file_name": "a.txt"},
            {"source_path": str(d / "b.txt"), "destination_path": str(d / "Keep" / "b.txt"), "file_name": "b.txt"}]
    _, _, log, _ = apply_mod.apply_moves(plan)
    ini = d / "Notes" / "desktop.ini"
    ini.write_text("[.ShellClassInfo]\n")
    _attr(ini, _HIDDEN | _SYSTEM | _READONLY)
    (d / "Notes" / "Thumbs.db").write_bytes(b"x")
    (d / "Notes" / ".DS_Store").write_bytes(b"x")
    (d / "Keep" / "mine.txt").write_text("someone put this here")
    res = apply_mod.undo_log(log)
    assert res["reverted"] == 2 and (d / "a.txt").exists() and (d / "b.txt").exists()
    assert not (d / "Notes").exists(), "only Explorer's files were left: gone"
    assert (d / "Keep" / "mine.txt").exists(), "a real file keeps its folder"


# ---- ai_organizer: which folder, how, big plans, changing a plan ---------------------------------------------
def test_folder_named_outright_is_never_guessed(monkeypatch):
    h = "/Users/x"
    cands = [f"{h}/Desktop", f"{h}/Desktop/Filect Organize Test", f"{h}/Desktop/Filect Test Small", f"{h}/Desktop/Money",
             f"{h}/Downloads", f"{h}/Documents", f"{h}/Documents/Reports", f"{h}/Desktop/Reports"]
    pick = ai_organizer._folder_named_in
    assert pick("Organize the Filect Test Small folder on my desktop.", cands) == f"{h}/Desktop/Filect Test Small"
    assert pick("In the Filect Test Small folder, put invoices in a folder called Money.", cands) == f"{h}/Desktop/Filect Test Small"
    assert pick("Organize the Reports folder in my Documents", cands) == f"{h}/Documents/Reports", "its parent says which"
    assert pick("Organize the Reports folder", cands) is None, "two of them: not for this step to choose"
    assert pick("Organize my Downloads", cands) is None and pick("Organize my desktop", cands) is None, "never a whole top folder"
    assert pick("Organize the folder called Money", cands) == f"{h}/Desktop/Money"
    assert pick("Organize my blue elephant folder", cands) is None
    more = cands + [f"{h}/Downloads/Folder", f"{h}/Desktop/Project", f"{h}/Desktop/test", f"{h}/Desktop/Pictures", f"{h}/Desktop/cool-site"]
    assert pick("Organize my Blue Elephant Project folder.", more) is None, "not the one called Project"
    assert pick("Organize my Filect Test Tiny folder on the desktop.", more) is None, "not the one called test"
    assert pick("Put sunset.png in a folder called Pictures.", more) is None, "where it goes, and 'folder' isn't Folder"
    assert pick("Organize the cool site folder on my desktop", more) == f"{h}/Desktop/cool-site"
    assert ai_organizer.folder_is_named("Filect Dup Folder", "Organize my Filect Dup Folder.")
    assert ai_organizer.folder_is_named("Downloads", "organize my download folder")
    assert ai_organizer.folder_is_named("Reports", "organize Reports by year")
    assert not ai_organizer.folder_is_named("Folder", "put it in a folder")
    assert not ai_organizer.folder_is_named("put", "Put sunset.png in a folder called Pictures.")
    assert ai_organizer.folder_is_named("Desktop", "Organize my desktop.")
    called = []
    import app.core.vision as v
    monkeypatch.setattr(v, "_call_openai_proxy", lambda *a, **k: called.append(1))
    assert ai_organizer.resolve_folder_with_ai("Organize the Filect Test Small folder", cands) == cands[2] and not called


def test_folder_named_outright_windows_paths(monkeypatch):
    """Windows paths: backslashes, a trailing slash, another case, OneDrive's Documents: same answers."""
    h = r"C:\Users\x"
    cands = [h + r"\Desktop", h + r"\Desktop\Filect Test Small", h + r"\Desktop\Reports", h + r"\Downloads",
             h + r"\OneDrive\Documents", h + r"\OneDrive\Documents\Reports"]
    pick = ai_organizer._folder_named_in
    assert pick("Organize the Filect Test Small folder on my desktop.", cands) == cands[1]
    assert pick("Organize the Reports folder in my Documents", cands) == cands[5], "its parent says which"
    assert pick("Organize the Reports folder", cands) is None
    assert pick("Organize my Downloads", cands) is None and pick("Organize my Documents folder", cands) is None
    mixed = [h + "\\desktop\\", "C:/Users/x/Desktop/Money", "c:\\users\\x\\downloads"]
    assert pick("Organize the folder called Money", mixed) == mixed[1], "parent spelled another way is still its parent"
    assert pick("Organize my downloads", mixed) is None, "a top folder, whatever its case"
    assert pick("organize the money folder", ["D:\\", "D:\\Money"]) == "D:\\Money", "a drive root is a top folder"
    called = []
    import app.core.vision as v
    monkeypatch.setattr(v, "_call_openai_proxy", lambda *a, **k: called.append(1))
    assert ai_organizer.resolve_folder_with_ai("Organize the Filect Test Small folder", cands) == cands[1] and not called


def test_sentence_names_a_folder():
    s = ai_organizer.sentence_names_a_folder
    assert s("Organize the Billing folder") and s("clean up my desktop") and s("sort my Downloads by type")
    assert not s("Put the invoice and the receipt in Billing"), "says where they go, not which folder"
    assert not s("put these in a folder called Money")


def test_big_plans_dont_split_the_same_folder():
    got = ai_organizer._merge_similar_folders({
        "invoices-northwind": [1, 2, 3], "invoices": [4], "recipes-tomato-soup": [5, 6], "tomato-soup-recipes": [7],
        "workout-logs": [8], "fitness-workout-logs": [9, 10], "Photos/2023": [11], "photos": [12], "Taxes": [13]})
    assert got == {"invoices-northwind": [1, 2, 3, 4], "recipes-tomato-soup": [5, 6, 7],
                   "fitness-workout-logs": [9, 10, 8], "Photos/2023": [11], "photos": [12], "Taxes": [13]}, got
    assert ai_organizer._merge_similar_folders({"Photos\\2023": [1], "photos": [2]}) == {"Photos\\2023": [1], "photos": [2]}


def test_merged_only_when_filect_chose_the_names(monkeypatch):
    files = [{"id": i, "file_name": f"f{i}.pdf"} for i in range(1, 301)]     # 2 chunks: 250 + 50

    def fake(instruction, chunk, idx=0, n_chunks=1):
        ids = [f["id"] for f in chunk]
        return {"folders": {"invoices-northwind" if idx == 0 else "invoices": ids}}
    monkeypatch.setattr(ai_organizer, "_plan_with_retries", fake)
    got = ai_organizer.request_organization_plan("[AUTO-ORGANIZE] Organize ALL files into logical folders", files)
    assert list(got["folders"]) == ["invoices-northwind"] and len(got["folders"]["invoices-northwind"]) == 300
    for said in ("[AUTO-ORGANIZE] User's specific instructions: invoices in invoices",
                 "[AUTO-ORGANIZE] EXISTING FOLDERS ONLY. EXISTING FOLDERS YOU CAN USE: invoices, invoices-northwind",
                 "put the invoices in invoices"):
        got = ai_organizer.request_organization_plan(said, files)
        assert set(got["folders"]) == {"invoices-northwind", "invoices"}, "names you gave are never merged: " + said


def test_refine_asks_again_when_nothing_changed(monkeypatch):
    calls = []
    old = {"folders": {"a": [2, 1]}}

    def same_then_new(message):
        calls.append(message)
        return {"folders": {"a": [1, 2]}} if len(calls) == 1 else {"files_mentioned": [1], "folders": {"b": [1, 2]}}
    monkeypatch.setattr(ai_organizer, "_request_openai", same_then_new)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    got = ai_organizer.request_plan_refinement("x", old, "rename it b", [{"id": 1, "file_name": "n"}])
    assert got == {"folders": {"b": [1, 2]}} and len(calls) == 2 and "make it" in calls[1]
    assert '"files_mentioned"' in calls[0] and "FIRST" in calls[0], "it must list the files first"


def test_refine_strips_files_mentioned(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_organizer, "_request_openai",
                        lambda m: calls.append(m) or {"files_mentioned": [1], "folders": {"Billing": [1], "a": [2]}})
    monkeypatch.setattr(settings, "ai_provider", "openai")
    got = ai_organizer.request_plan_refinement("x", {"folders": {"a": [1, 2]}}, "put the invoice in Billing",
                                               [{"id": 1, "file_name": "invoice.pdf"}, {"id": 2, "file_name": "n"}])
    assert got == {"folders": {"Billing": [1], "a": [2]}} and len(calls) == 1, "changed the first time: asked once"
    monkeypatch.setattr(ai_organizer, "_request_openai", lambda m: None)
    assert ai_organizer.request_plan_refinement("x", {"folders": {"a": [1]}}, "y", [{"id": 1}]) is None
    assert not ai_organizer._same_folders(None, {"folders": {}}) and ai_organizer._same_folders(
        {"folders": {"a": [1, "2"]}}, {"folders": {"a": ["2", 1]}})


def test_how_is_read_from_the_sentence(monkeypatch):
    from app.core import vision
    seen = []

    def said(text):
        def fake(*a, **k):
            seen.append((a, k))
            return {"choices": [{"message": {"content": text}}]}
        monkeypatch.setattr(vision, "_call_openai_proxy", fake)
        return ai_organizer.organize_how("Organize the Reports folder by type", "/x/Reports")
    assert said("NONE") == "" and said("none.") == ""
    assert said('"by type"') == "by type"
    assert said("by date") == "", "you never said that: Filect decides"
    assert said("financial reports in Finance") == "", "the folder's own name doesn't count as saying how"
    monkeypatch.setattr(vision, "_call_openai_proxy",
                        lambda *a, **k: {"choices": [{"message": {"content": "by file type into Documents, Images"}}]})
    assert ai_organizer.organize_how("Sort out my documents folder", "/x/Documents") == ""
    assert said("") == "Organize the Reports folder by type", "no answer: the sentence, as before"
    monkeypatch.setattr(vision, "_call_openai_proxy", lambda *a, **k: None)
    assert ai_organizer.organize_how("Organize it", "/x") == "Organize it"

    def boom(*a, **k):
        raise RuntimeError("no network")
    monkeypatch.setattr(vision, "_call_openai_proxy", boom)
    assert ai_organizer.organize_how("Organize it", "/x") == "Organize it"


def test_how_is_asked_for_as_json(monkeypatch):
    """Windows: the shared proxy forces a JSON object on every chat call, so the question asks for {"how": ...}."""
    from app.core import vision
    seen = []

    def said(text, sentence="Organize the Reports folder by type", folder=r"C:\Users\x\Reports"):
        def fake(*a, **k):
            seen.append((a, k))
            return {"choices": [{"message": {"content": text}}]}
        monkeypatch.setattr(vision, "_call_openai_proxy", fake)
        return ai_organizer.organize_how(sentence, folder)
    assert said('{"how": "by type"}') == "by type"
    assert said('{"how": "NONE"}') == "" and said('{"how": ""}') == "" and said('{"how": null}') == ""
    assert said('{"how": "by date"}') == "", "the word check still applies"
    assert said('{"how": "put the invoices in Money"}', "In Reports, put the invoices in a folder called Money") == \
        "put the invoices in Money"
    a, k = seen[-1]
    assert a[0] == "chat" and k.get("model") == ai_organizer.PLAN_MODEL, "gpt-5-mini: nano missed real instructions"
    assert "json" in " ".join(m["content"] for m in a[1]).lower(), "the proxy rejects a chat that doesn't ask for JSON"
    assert "Reports" in a[1][1]["content"] and "C:\\" not in a[1][1]["content"], "only the folder's name is sent"


# ---- intent_router: the decider ------------------------------------------------------------------------------
def test_decider_hands_free_only():
    assert ir._parse('{"intent":"ORGANIZE","task":""}', "x", allow_ignore=True) == {
        "intent": "ORGANIZE", "index": None, "task": "", "screen": False}
    assert ir._parse('{"intent":"ORGANIZE","task":"by type"}', "x", allow_ignore=True)["task"] == "by type"
    assert ir._parse('{"intent":"ORGANIZE"}', "x")["intent"] == "NEW", "Shift+Z never gets it"
    assert "ORGANIZE" not in ir._build_prompt("x", None, []) and "ORGANIZE" in ir._build_prompt("x", None, [], live=True)
    st = {"text": "Filect did x.", "allowed": ["ORGANIZE_UNDO"]}
    assert ir._parse('{"intent":"ORGANIZE_UNDO"}', "x", allow_ignore=True, organize=st)["intent"] == "ORGANIZE_UNDO"
    assert ir._parse('{"intent":"ORGANIZE_CANCEL"}', "x", allow_ignore=True, organize=st)["intent"] == "IGNORE", "not offered"
    # (Mac: IGNORE here. Windows keeps Shift+Z exactly as it was: an intent it doesn't know is NEW, as before.)
    assert ir._parse('{"intent":"ORGANIZE_UNDO"}', "x", organize=st)["intent"] == "NEW", "Shift+Z never gets it"
    assert ir._parse('{"intent":"ORGANIZE_CHANGE","task":"put a in b"}', "x", allow_ignore=True,
                     organize={"text": "t", "allowed": ["ORGANIZE_CHANGE"]})["task"] == "put a in b"
    assert "Filect did x." in ir._build_prompt("x", None, [], live=True, organize=st)
    assert "ORGANIZE_CANCEL" not in ir._build_prompt("x", None, [], live=True, organize=st), "only what fits"
    assert "Filect did x." not in ir._build_prompt("x", None, [], organize=st), "hands-free only"


def test_windows_live_text_is_kept():
    """Today's Windows additions to the hands-free text stay, next to the Mac's ORGANIZE text; Shift+Z's prompt is
    exactly what it was."""
    live = ir._build_prompt("hi", None, [], live=True)
    for words in ("Hey Siri", "Alexa", "OK Google", "named person", "watch my screen", "look at this",
                  "ORGANIZE_AUTO", "ORGANIZE_AUTO_OFF", "organize the imports"):
        assert words in live, words
    assert ir._build_prompt("hi", "T", ["a"]) == "Current conversation: T\nOther conversations:\n0. a\n\nSpoken command: 'hi'"
    assert ir._build_prompt("hi", "T", ["a"], organize={"text": "t", "allowed": ["ORGANIZE"]}) == \
        ir._build_prompt("hi", "T", ["a"]), "Shift+Z: organize= changes nothing"


def test_decider_codex_and_claude_take_organize(monkeypatch):
    import inspect
    assert "organize" in inspect.signature(ir.IntentRouter.classify).parameters
    from app.core import codex_runner
    prompts = []
    monkeypatch.setattr(codex_runner, "complete",
                        lambda system, prompt, timeout=60.0: prompts.append(prompt) or '{"intent":"ORGANIZE_UNDO","task":""}')
    st = {"text": "Filect organized the Reports folder 2 minutes ago.", "allowed": ["ORGANIZE_UNDO", "ORGANIZE"]}
    d = ir.classify_with_codex("put them back", None, [], live=True, organize=st)
    assert d["intent"] == "ORGANIZE_UNDO" and st["text"] in prompts[-1]
    d = ir.classify_with_codex("put them back", None, [], organize=st)
    assert d["intent"] == "NEW" and st["text"] not in prompts[-1], "Shift+Z unchanged"


def test_decider_auto_answers_are_hands_free_only():
    raw = '{"intent":"ORGANIZE_AUTO","task":"","mode":"new"}'
    assert ir._parse(raw, "x", allow_ignore=True)["mode"] == "new"
    assert ir._parse('{"intent":"ORGANIZE_AUTO","mode":"banana"}', "x", allow_ignore=True)["mode"] is None
    assert ir._parse(raw, "x")["intent"] == "NEW", "Shift+Z is unchanged"
    assert ir._parse('{"intent":"ORGANIZE_AUTO_OFF","task":"Downloads"}', "x", allow_ignore=True) == {
        "intent": "ORGANIZE_AUTO_OFF", "index": None, "task": "Downloads", "screen": False, "mode": None}
    pick = '{"intent":"ORGANIZE_CHOICE","task":"new"}'
    assert ir._parse(pick, "x", allow_ignore=True)["intent"] == "IGNORE", "only while that question is up"
    assert ir._parse(pick, "x", allow_ignore=True, organize={"allowed": ["ORGANIZE_CHOICE"]})["task"] == "new"
    no = '{"intent":"ORGANIZE_NO_RULES"}'
    assert ir._parse(no, "x", allow_ignore=True, organize={"text": "t", "allowed": ["ORGANIZE_NO_RULES"]})["intent"] == \
        "ORGANIZE_NO_RULES"
    assert "ORGANIZE_NO_RULES: they have no instructions" in ir._build_prompt(
        "nope", None, [], live=True, organize={"text": "t", "allowed": ["ORGANIZE_NO_RULES", "ORGANIZE_CHOICE"]})


# ---- History of automatic moves ------------------------------------------------------------------------------
def test_auto_history_one_entry_per_folder_per_day(mk, sandbox):
    d, e = mk(), mk()

    def auto():
        return [x for x in apply_mod.get_move_history() if x["source"] == "auto" and x["folder"] in (str(d), str(e))]
    apply_mod.record_auto_moves(str(d), [])
    assert auto() == [], "no moves, no entry"
    (d / "Docs").mkdir()
    (d / "Docs" / "a").write_text("a")
    apply_mod.record_auto_moves(str(d), [(str(d / "a"), str(d / "Docs" / "a"))])
    (d / "Docs" / "b").write_text("b")
    apply_mod.record_auto_moves(str(d), [(str(d / "b"), str(d / "Docs" / "b"))])
    apply_mod.record_auto_moves(str(e), [(str(e / "c"), str(e / "X" / "c"))])
    h = {x["folder"]: x for x in auto()}
    assert len(auto()) == 2 and h[str(d)]["successful_moves"] == 2, "a batch adds to today's entry for that folder"
    name = os.path.basename(h[str(d)]["log_file"])
    assert re.fullmatch(time.strftime("moves-%Y%m%d") + r"-000000-auto-[0-9a-f]{8}\.json", name), name
    (d / "Other").mkdir()
    shutil.move(str(d / "Docs" / "a"), str(d / "Other" / "a"))
    apply_mod.record_auto_moves(str(d), [(str(d / "Docs" / "a"), str(d / "Other" / "a"))])
    log = h[str(d)]["log_file"]
    with open(log, encoding="utf-8") as f:
        moves = json.load(f)["moves"]
    assert len(moves) == 2 and {"from": str(d / "a"), "to": str(d / "Other" / "a")}.items() <= moves[0].items(), "moved twice: straight home"
    settings.auto_organize_folders = [{"path": str(d)}]
    done, errors = apply_mod.revert_moves(log, moves)
    assert done == 2 and not errors and (d / "a").exists() and (d / "b").exists()
    assert settings.auto_organize_skip_known(str(d)), "what was put back must not be sorted again"
    (d / "Docs").mkdir(exist_ok=True)         # (the revert tidied the emptied folder away)
    (d / "Docs" / "z").write_text("z")
    apply_mod.record_auto_moves(str(d), [(str(d / "z"), str(d / "Docs" / "z"))])
    assert sum(x["folder"] == str(d) for x in auto()) == 2, "after a revert: a new entry"


@win_only
def test_auto_history_folder_and_paths_without_case(mk):
    d = mk()
    apply_mod.record_auto_moves(str(d), [(str(d / "a.txt"), str(d / "Docs" / "a.txt"))])
    apply_mod.record_auto_moves(str(d).upper() + "\\", [(str(d / "DOCS" / "A.TXT"), str(d / "Other" / "a.txt"))])
    mine = [x for x in apply_mod.get_move_history() if x["source"] == "auto"]
    assert len(mine) == 1, "the same folder however it's spelled"
    with open(mine[0]["log_file"], encoding="utf-8") as f:
        log = json.load(f)
    assert log["source"] == "auto" and log["folder"] == str(d)
    assert [(m["from"], m["to"]) for m in log["moves"]] == [(str(d / "a.txt"), str(d / "Other" / "a.txt"))], \
        "moved again under another spelling: still one entry, straight home"


def test_record_auto_moves_never_raises(mk, monkeypatch, sandbox):
    d = mk()
    (sandbox / "garbage").mkdir()
    real = settings.get_moves_dir

    def broken():
        raise OSError("disk full")
    monkeypatch.setattr(settings, "get_moves_dir", broken)
    apply_mod.record_auto_moves(str(d), [(str(d / "a"), str(d / "B" / "a"))])          # (no exception)
    monkeypatch.setattr(settings, "get_moves_dir", real)
    path = apply_mod._auto_log_path(str(d), __import__("datetime").datetime.now())
    path.write_text("{not json", encoding="utf-8")
    apply_mod.record_auto_moves(str(d), [(str(d / "a"), str(d / "B" / "a"))])          # (no exception)


# ---- settings -------------------------------------------------------------------------------------------------
def test_settings_flags(monkeypatch):
    saves = []
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: saves.append(1))
    monkeypatch.setattr(settings, "auto_organize_paused", False)
    settings.auto_organize_folders = [{"path": r"C:\Users\x\Downloads", "instruction": ""}, {"path": r"C:\Users\x\Desktop"}]
    dl = settings.auto_organize_folders[0]
    assert settings._auto_organize_entry(r"c:/users/x/downloads/") is dl, "no case, either slash, a trailing slash"
    assert settings._auto_organize_entry(r"C:\Users\x\Downloads2") is None and settings._auto_organize_entry("") is None
    settings.set_auto_organize_flags(r"C:\USERS\X\DOWNLOADS", paused=True, watching_since=123.5)
    assert dl["paused"] is True and settings.auto_organize_watching_since(r"c:\users\x\downloads") == 123.5 and saves
    assert settings.auto_organize_watching_since(r"C:\Users\x\Desktop") == 0
    assert settings.auto_organize_watching_since(r"D:\not-listed") == 0
    assert settings.auto_organize_paused is False, "a folder's own `paused` never touches the global Stop"
    n = len(saves)
    settings.set_auto_organize_flags(r"D:\not-listed", paused=True)
    assert len(saves) == n and all(f.get("path") != r"D:\not-listed" for f in settings.auto_organize_folders)
    assert not settings.auto_organize_skip_known(r"C:\Users\x\Downloads")
    settings.mark_auto_organize_skip_known([r"c:\users\x\Downloads2\a.txt", r"C:\Users\x\Downloads"])
    assert not settings.auto_organize_skip_known(r"C:\Users\x\Downloads"), "a look-alike folder or the folder itself"
    settings.mark_auto_organize_skip_known([r"c:/USERS/x/downloads/sub/a.txt"])
    assert settings.auto_organize_skip_known(r"C:\Users\x\Downloads") and not settings.auto_organize_skip_known(
        r"C:\Users\x\Desktop")
    settings.clear_auto_organize_paused()
    assert "paused" not in dl and settings.auto_organize_paused is False, "Start: the whole list, the global flag as it was"
    n = len(saves)
    settings.clear_auto_organize_paused()
    assert len(saves) == n, "nothing to clear: nothing saved"


def test_settings_saved_and_loaded(tmp_path, monkeypatch):
    """live_organize_preview, and the per-folder flags, survive a real save and load (into a temp folder)."""
    monkeypatch.setattr(Settings, "get_app_data_dir", lambda self: tmp_path)
    s = Settings()
    assert s.live_organize_preview is True, "on by default"
    s.live_organize_preview = False
    entry = {"path": r"C:\x\Downloads", "instruction": "bills in Money", "action": 3, "paused": True,
             "skip_known": True, "watching_since": 1700000000.5}
    s.auto_organize_folders = [dict(entry)]
    s.auto_organize_paused = True
    s._save_config()
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert data["live_organize_preview"] is False and data["auto_organize_folders"] == [entry]
    s2 = Settings()
    assert s2.live_organize_preview is False and s2.auto_organize_folders == [entry] and s2.auto_organize_paused is True
    assert s2.auto_organize_skip_known(r"c:\x\downloads") and s2.auto_organize_watching_since(r"C:\x\Downloads") == 1700000000.5
    # a flag set later on the loaded settings is saved too (the in-place change is seen)
    s2.set_auto_organize_flags(r"C:\x\Downloads", paused=False)
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["auto_organize_folders"][0]["paused"] is False
    data["live_organize_preview"] = "yes"                     # not a bool: ignored
    (tmp_path / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    assert Settings().live_organize_preview is True


# ---- Windows: hidden, junctions, program folders, case, OneDrive, HEIC, scanned PDF, psutil, Codex, Claude -----
@win_only
def test_hidden_attribute(mk):
    d = mk()
    (d / "seen.txt").write_text("a")
    secret, sysf, hd = d / "secret.txt", d / "sys.txt", d / "HiddenDir"
    secret.write_text("h")
    sysf.write_text("s")
    hd.mkdir()
    (hd / "in.txt").write_text("i")
    _attr(secret, _HIDDEN)
    _attr(sysf, _SYSTEM)
    _attr(hd, _HIDDEN)
    s = r.scan(d)
    assert _rel(s["files"], d) == ["seen.txt"], s
    assert r.protected(secret, d) and r.protected(sysf, d) and r.protected(hd / "in.txt", d)
    assert not r.protected(d / "seen.txt", d) and r._dir_is_off_limits(str(hd))
    for p in (secret, sysf, hd):
        _attr(p, _NORMAL)


@win_only
def test_junction_is_skipped(mk):
    import _winapi
    d, outside = mk(), mk("outside")
    (outside / "far.txt").write_text("far away")
    (d / "a.txt").write_text("a")
    jn = d / "jn"
    _winapi.CreateJunction(str(outside), str(jn))
    try:
        assert r._is_link(str(jn)) and not os.path.islink(jn)
        s = r.scan(d)
        assert _rel(s["files"], d) == ["a.txt"] and s["skipped"] >= 1, "never followed"
    finally:
        os.rmdir(jn)
    assert (outside / "far.txt").exists()


def test_program_folder_is_off_limits(mk):
    d = mk()
    tool, inst, libs = d / "PortableTool", d / "Installers", d / "Libs"
    for folder, names in ((tool, ("Tool.EXE", "core.dll", "readme.txt")), (inst, ("setup.exe",)), (libs, ("a.dll",))):
        folder.mkdir()
        for n in names:
            (folder / n).write_text("x")
    (d / "setup.exe").write_text("x")
    (d / "stray.dll").write_text("x")
    s = r.scan(d)
    assert _rel(s["files"], d) == sorted([os.path.join("Installers", "setup.exe"), os.path.join("Libs", "a.dll"),
                                          "setup.exe", "stray.dll"]), "a lone .exe or .dll is just a file"
    assert r._dir_is_off_limits(str(tool)) and r.protected(tool / "readme.txt", d), "an .exe next to .dll files"
    assert not r._dir_is_off_limits(str(inst)) and not r._dir_is_off_limits(str(libs))


@win_only
def test_paths_compare_without_case(mk):
    d = mk()
    paths = _notes(d, 2)
    assert len(r.scan(d)["unread"]) == 2
    for p in paths:                        # indexed under another spelling of the same path
        database.file_index.add_file({"source_path": p.upper(), "name": os.path.basename(p).upper(), "extension": ".txt",
                                      "size": os.path.getsize(p), "label": "note", "tags": ["n"]})
    assert r.scan(d)["unread"] == [], "the same files"
    assert r.scan(str(d).upper())["unread"] == [] and r.scan(str(d).lower() + "\\")["unread"] == []
    (d / "tool").mkdir()
    (d / "tool" / "requirements.txt").write_text("x")
    (d / "tool" / "m.txt").write_text("x")
    assert r.protected(d / "tool" / "m.txt", str(d).upper()) and not r.protected(d / "n0.txt", str(d).lower())


def test_onedrive_placeholder_never_opened(mk, monkeypatch):
    from PIL import Image
    from app.core import scan as scan_mod
    d = mk()
    cloud = d / "cloud photo.jpg"
    Image.new("RGB", (50, 50), "blue").save(cloud)           # a real picture: opening it WOULD give one
    local = d / "here.txt"
    local.write_text("here")
    monkeypatch.setattr(scan_mod, "_is_onedrive_cloud_file",
                        lambda p: os.path.normcase(str(p)) == os.path.normcase(str(cloud)))
    opened = []
    real_open, real_img_open = builtins.open, Image.open

    def guard(file, *a, **k):
        if isinstance(file, (str, os.PathLike)) and os.path.normcase(os.fspath(file)) == os.path.normcase(str(cloud)):
            opened.append(os.fspath(file))
        return real_open(file, *a, **k)

    def img_guard(fp, *a, **k):
        opened.append(fp)
        return real_img_open(fp, *a, **k)
    monkeypatch.setattr(builtins, "open", guard)
    monkeypatch.setattr(io, "open", guard)
    monkeypatch.setattr(Image, "open", img_guard)
    item = r.prepare(str(cloud))
    assert item.get("cloud") and item["images"] == [] and item["text"] == ""
    assert item["meta"]["size"] == cloud.stat().st_size and item["meta"]["content_hash"] is None
    assert r.scan(d)["files"] == [str(local)], "left out, as everywhere in Filect's scanning"
    asked = []
    monkeypatch.setattr(r, "_ask", lambda a, items, c: asked.append(len(items)) or _ok(items))
    res = r.read_files([str(cloud)], ["claude"])
    assert res["by"] == {"cloud": 1} and asked == [], "nothing to send"
    assert opened == [], opened
    assert r._indexed_rows(str(d)) == {}, "not indexed either: the index would look inside it for a date"


def test_heic_through_pillow(mk):
    pytest.importorskip("pillow_heif")
    from PIL import Image
    p = mk() / "IMG_0001.HEIC"
    try:
        Image.new("RGB", (1600, 1200), (200, 30, 30)).save(p, format="HEIF")
    except Exception as e:
        pytest.skip(f"can't make a HEIC on this PC: {e}")
    jpg = r._picture(str(p))
    assert jpg and jpg[:2] == b"\xff\xd8"
    with Image.open(io.BytesIO(jpg)) as im:
        assert max(im.size) == r.THUMB_PX and im.format == "JPEG"
    assert len(r.prepare(str(p))["images"]) == 1


def test_raw_is_labelled_from_its_name(mk):
    d = mk()
    raw = d / "DSC_0001.NEF"
    raw.write_bytes(b"II*\x00" + b"\x00" * 64)
    assert r._picture(str(raw)) is None
    item = r.prepare(str(raw))
    assert item["images"] == [] and item["name"] == "DSC_0001.NEF" and not item.get("cloud")


def test_scanned_pdf_first_page(mk, monkeypatch):
    pytest.importorskip("pypdfium2")
    from PIL import Image
    d = mk()
    p = d / "scan.pdf"
    Image.new("RGB", (850, 1100), "white").save(p, "PDF")    # pictures only, no text layer: a scan
    jpg = r._pdf_page(str(p))
    assert jpg and jpg[:2] == b"\xff\xd8"
    with Image.open(io.BytesIO(jpg)) as im:
        assert 600 <= max(im.size) <= r.THUMB_PX, im.size
    item = r.prepare(str(p))
    assert item["text"] == "" and len(item["images"]) == 1, "a scan: its first page is looked at"
    from app.core import text_extract
    monkeypatch.setattr(text_extract, "extract_file_text", lambda path: "Invoice 42 from Northwind")
    item = r.prepare(str(p))
    assert item["text"] == "Invoice 42 from Northwind" and item["images"] == [], "a text PDF sends its text"
    bad = d / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 not really")
    assert r._pdf_page(str(bad)) is None and r.prepare(str(bad))["images"] == []


def test_memory_from_psutil(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("M", (), {"total": int(7.99 * 1024 ** 3)})())
    assert r._memory_gb() == 8.0 and r.parallel_runs(100) == 2, "an 8 GB PC says 7.99: still two at once"
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("M", (), {"total": int(3.7 * 1024 ** 3)})())
    assert r.parallel_runs(100) == 1
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("M", (), {"total": 64 * 1024 ** 3})())
    assert r.parallel_runs(100) == r.MAX_PARALLEL and r.parallel_runs(20) == 1


def test_codex_reads_its_prompt_from_stdin(monkeypatch):
    from app.core import codex_runner
    monkeypatch.setattr(codex_runner, "find_codex", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(codex_runner, "signed_in", lambda exe: True)
    seen = {}
    answer = {"type": "item.completed", "item": {"type": "agent_message", "text": '{"files":[{"id":1,"type":"note"}]}'}}

    class FakeProc:
        reply = json.dumps(answer) + "\n"

        def __init__(self, cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            seen["images"] = [os.path.exists(cmd[i + 1]) for i, a in enumerate(cmd) if a == "-i"]

        def communicate(self, text):
            seen["stdin"] = text
            return FakeProc.reply, None

        def kill(self):
            pass
    monkeypatch.setattr(r.subprocess, "Popen", FakeProc)
    items = [{"name": "a.txt", "meta": {"size": 10}, "text": "hello " * 2000, "images": [b"\xff\xd8jpg"], "speech": False}]
    out = r._ask_codex(items, threading.Event())
    assert r.parse_answer(out, 1) == {1: {"label": "note", "caption": "", "tags": []}}
    cmd = seen["cmd"]
    assert cmd[-2:] == ["--", "-"] and cmd[cmd.index("-m") + 1] == r.CODEX_MODEL
    assert "a.txt" in seen["stdin"] and "hello" in seen["stdin"] and "hello" not in " ".join(cmd), "the text goes on stdin"
    assert seen["images"] == [True] and cmd.index("-i") < cmd.index("--"), "pictures as files"
    assert "--ephemeral" in cmd and 'sandbox_mode="read-only"' in cmd, "saves no chat, runs nothing"
    assert seen["kw"].get("creationflags") == codex_runner._NO_WINDOW and seen["kw"].get("encoding") == "utf-8"
    FakeProc.reply = json.dumps({"type": "turn.failed", "error": "You've hit your usage limit"}) + "\n"
    with pytest.raises(r._AgentOut):
        r._ask_codex(items, threading.Event())
    monkeypatch.setattr(codex_runner, "signed_in", lambda exe: False)
    with pytest.raises(r._AgentOut):
        r._ask_codex(items, threading.Event())


def test_claude_run_has_no_tools_and_saves_nothing(monkeypatch):
    import claude_agent_sdk as sdk
    seen = {}

    async def fake_query(*, prompt, options=None, transport=None):
        seen["opts"] = options
        async for m in prompt:
            seen["msg"] = m
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text='{"files":[{"id":1,"type":"photo"}]}')], model="haiku")
    monkeypatch.setattr(sdk, "query", fake_query)
    monkeypatch.delenv("FILECT_CLAUDE_CLI", raising=False)
    items = [{"name": "p.jpg", "meta": {"size": 5}, "text": "", "images": [b"\xff\xd8x"], "speech": False}]
    out = r._ask_claude(items, threading.Event())
    assert r.parse_answer(out, 1)[1]["label"] == "photo"
    o = seen["opts"]
    assert o.model == r.CLAUDE_MODEL and o.tools == [] and o.setting_sources == [] and o.strict_mcp_config
    assert o.max_turns == 1 and "no-session-persistence" in o.extra_args
    assert not os.path.exists(o.cwd), "a throwaway folder, gone afterwards"
    assert [b["type"] for b in seen["msg"]["message"]["content"]] == ["text", "text", "image"]

    async def out_of_usage(*, prompt, options=None, transport=None):
        yield sdk.AssistantMessage(content=[], model="haiku", error="rate_limit")
    monkeypatch.setattr(sdk, "query", out_of_usage)
    with pytest.raises(r._AgentOut):
        r._ask_claude(items, threading.Event())
