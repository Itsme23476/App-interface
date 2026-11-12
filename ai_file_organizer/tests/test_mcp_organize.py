"""The MCP organize tools (filect_mcp/server.py) with files Filect hasn't read yet — Mac bug 13 (2026-11-10): the
plan used to skip every file that wasn't in the index. Now they're read first (like organizing by voice: on the
person's own Claude Code / Codex, organize_reader.read_files), then every file is planned.

Never the user's own data: a throwaway index, History and plans folder, settings never written, test folders in
%TEMP%, the agent is a fake (no Claude / Codex runs), and so is the planner (no backend call)."""
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]              # (the repo: filect_mcp/ lives next to ai_file_organizer/)
sys.path.insert(0, str(ROOT / "ai_file_organizer"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.settings import settings  # noqa: E402
from app.core import ai_organizer, database, organize_reader as r  # noqa: E402
from filect_mcp import server as mcp  # noqa: E402

for _h in list(logging.getLogger().handlers):           # (importing the server logs to the user's real MCP log)
    if str(getattr(_h, "baseFilename", "")).endswith("filect_mcp.log"):
        logging.getLogger().removeHandler(_h)
        _h.close()

_REAL_ASK = r._ask


class Agent:
    """A fake Claude Code / Codex: labels each file by its name, and records what it was asked."""
    def __init__(self, delay=0.0, out=()):
        self.asked, self.delay, self.out = [], delay, set(out)
        self.lock = threading.Lock()

    def __call__(self, agent, items, cancelled):
        if agent in self.out:
            raise r._AgentOut("out of usage")
        with self.lock:
            self.asked.append((agent, [it["name"] for it in items]))
        if self.delay:
            time.sleep(self.delay)
        return json.dumps({"files": [{"id": n, "type": _kind(it["name"]), "caption": f"about {it['name']}",
                                      "tags": [_kind(it["name"])]} for n, it in enumerate(items, 1)]})

    def names(self):
        return sorted(n for _, batch in self.asked for n in batch)


def _kind(name):
    return "invoice" if "invoice" in name else "photo" if name.endswith(".jpg") else "note"


def _planner(instructions, files, *a, **k):
    """The plan, by what Filect knows of each file (its label): a stand-in for the backend's planner."""
    out = {}
    for f in files:
        out.setdefault((f.get("label") or "other").title() + "s", []).append(f["id"])
    return {"folders": out}


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(database, "file_index", database.FileIndex(tmp_path / "index.db"))
    moves = tmp_path / "moves"
    moves.mkdir()
    monkeypatch.setattr(settings, "get_moves_dir", lambda: moves)
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(mcp, "_mcp_plans_dir", lambda: plans)
    monkeypatch.setattr(mcp, "_require_access", lambda: None)
    monkeypatch.setattr(mcp, "READ_WAIT_SECS", 10)
    monkeypatch.setattr(ai_organizer, "request_organization_plan", _planner)
    monkeypatch.setattr(settings, "assistant_agent", "claude")
    mcp._READS.clear()
    mcp._PLANS.clear()
    mcp._SERVED.clear()
    yield plans
    for job in list(mcp._READS.values()) + list(mcp._PLANS.values()):
        job["finished"].wait(10)
    mcp._READS.clear()
    mcp._PLANS.clear()
    monkeypatch.setattr(r, "_ask", _REAL_ASK)


@pytest.fixture
def folder():
    d = Path(tempfile.mkdtemp(prefix="filect-mcp-test-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _files(d, *names):
    out = []
    for i, n in enumerate(names):
        p = d / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{n}: test file {i} in {d.name}")      # (unique content)
        out.append(str(p))
    return out


def _index(path, label="note"):
    """Already in Filect's index, described, the way normal indexing leaves a file."""
    meta = r._base_metadata(Path(path), False)
    meta.update(label=label, caption=f"a {label}", tags=[label], ai_source="test")
    assert database.file_index.add_file(meta)


def _planned(res):
    return sorted(n for names in res.get("proposed_folders", {}).values() for n in names)


def _gen(*a, **k):
    """Filect's own planner (filect_plans=True): what these tests check. The client-plans flow: further down."""
    k.setdefault("filect_plans", True)
    return mcp.generate_organization_plan(*a, **k)


def test_files_filect_hasnt_read_are_read_then_planned(monkeypatch, folder):
    old = _files(folder, "a.txt", "b.txt")
    new = _files(folder, "invoice 1.txt", "invoice 2.txt", "sub/c.txt")
    for p in old:
        _index(p)
    agent = Agent()
    monkeypatch.setattr(r, "_ask", agent)
    res = _gen(str(folder))
    assert "plan_id" in res, res
    assert _planned(res) == ["a.txt", "b.txt", "c.txt", "invoice 1.txt", "invoice 2.txt"], res
    assert "not_indexed" not in res, "every file was planned"
    assert agent.names() == sorted(os.path.basename(p) for p in new), "only the files Filect hadn't read"
    assert res["read_first"]["files"] == 3 and res["read_first"]["by"] == {"claude": 3}
    assert res["proposed_folders"]["Invoices"] == ["invoice 1.txt", "invoice 2.txt"] or \
        sorted(res["proposed_folders"]["Invoices"]) == ["invoice 1.txt", "invoice 2.txt"]
    for p in new:
        row = database.file_index.get_file_by_path(p)
        assert row and row.get("label") and row.get("caption"), "read into Filect's search index"
    again = _gen(str(folder), "by type")
    assert "plan_id" in again and "read_first" not in again and len(agent.asked) == 1, "nothing read twice"


def test_a_slow_read_says_how_far_it_got_and_carries_on_between_calls(monkeypatch, folder, sandbox):
    _files(folder, "a.txt", "b.txt", "c.txt")
    agent = Agent(delay=1.5)
    monkeypatch.setattr(r, "_ask", agent)
    monkeypatch.setattr(mcp, "READ_WAIT_SECS", 0.3)
    res = _gen(str(folder))
    assert res["status"] == "reading" and res["of"] == 3 and "plan_id" not in res, res
    assert "call generate_organization_plan again" in res["note"] and "Nothing has been moved" in res["note"]
    assert list(sandbox.iterdir()) == [], "no plan yet"
    res2 = _gen(str(folder))           # (still reading: it doesn't start another)
    assert res2.get("status") == "reading"
    mcp._READS[mcp._nkey(folder)]["finished"].wait(10)
    res3 = _gen(str(folder))
    assert _planned(res3) == ["a.txt", "b.txt", "c.txt"] and res3["read_first"]["files"] == 3, res3
    assert agent.names() == ["a.txt", "b.txt", "c.txt"], "each file read once, across the three calls"


def test_a_lot_of_unread_files_asks_first(monkeypatch, folder):
    _files(folder, "a.txt", "b.txt", "c.txt")
    agent = Agent()
    monkeypatch.setattr(r, "_ask", agent)
    monkeypatch.setattr(r, "ASK_ABOVE", 2)
    res = _gen(str(folder))
    assert res["confirmation_required"] and res["unread_files"] == 3 and "read_new_files=true" in res["warning"]
    assert agent.asked == [] and database.file_index.get_file_by_path(str(folder / "a.txt")) is None, \
        "nothing read before they agree"
    res = _gen(str(folder), read_new_files=True)
    assert _planned(res) == ["a.txt", "b.txt", "c.txt"], res


def test_the_agent_picked_in_filect_reads_first_and_the_other_takes_over(monkeypatch, folder):
    _files(folder, "a.txt", "b.txt")
    monkeypatch.setattr(settings, "assistant_agent", "codex")
    agent = Agent(out={"codex"})                                 # (Codex is out of usage)
    monkeypatch.setattr(r, "_ask", agent)
    res = _gen(str(folder))
    assert _planned(res) == ["a.txt", "b.txt"] and res["read_first"]["by"] == {"claude": 2}, res
    assert mcp._agent_order() == ["codex", "claude"]
    monkeypatch.setattr(settings, "assistant_agent", "something else")
    assert mcp._agent_order() == ["claude", "codex"]


def test_a_file_nothing_can_read_is_still_planned_by_its_name(monkeypatch, folder):
    _files(folder, "mystery.qqq", "b.txt")
    monkeypatch.setattr(r, "_ask", Agent(out={"claude", "codex"}))
    from app.core.search import search_service
    monkeypatch.setattr(search_service, "_process_single_file", lambda *a, **k: None)   # (the backend can't either)
    res = _gen(str(folder))
    assert _planned(res) == ["b.txt", "mystery.qqq"] and res["read_first"]["by"] == {"name": 2}, res


def test_a_file_that_cant_go_in_the_index_is_reported_and_not_read_over_and_over(monkeypatch, folder):
    _files(folder, "a.txt", "locked.txt")
    agent = Agent()
    monkeypatch.setattr(r, "_ask", agent)
    real_add = database.file_index.add_file
    monkeypatch.setattr(database.file_index, "add_file",
                        lambda meta: False if str(meta.get("source_path", "")).endswith("locked.txt") else real_add(meta))
    res = _gen(str(folder))
    assert _planned(res) == ["a.txt"] and res["not_indexed"] == 1 and res["not_indexed_examples"] == ["locked.txt"]
    assert "couldn't be read" in res["note"] and "stays where it is" in res["note"]
    res = _gen(str(folder))
    assert _planned(res) == ["a.txt"] and res["not_indexed"] == 1
    assert agent.names() == ["a.txt", "locked.txt"], "locked.txt isn't read again on every call"


def test_new_files_dropped_in_after_a_read_are_read_too(monkeypatch, folder):
    _files(folder, "a.txt")
    agent = Agent()
    monkeypatch.setattr(r, "_ask", agent)
    assert _planned(_gen(str(folder))) == ["a.txt"]
    _files(folder, "b.txt")
    res = _gen(str(folder))
    assert _planned(res) == ["a.txt", "b.txt"] and res["read_first"]["files"] == 1
    assert agent.names() == ["a.txt", "b.txt"]


def test_apply_and_undo_keep_the_newly_read_files_findable(monkeypatch, folder):
    new = _files(folder, "invoice 1.txt", "notes.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    res = _gen(str(folder))
    plan_id = res["plan_id"]
    preview = mcp.apply_organization_plan(plan_id)
    assert preview["confirmation_required"] and os.path.exists(new[0]), "nothing moves without confirm"
    done = mcp.apply_organization_plan(plan_id, confirm=True)
    assert done["moved"] == 2 and done["paths_updated"] == 2, done
    landed = folder / "Invoices" / "invoice 1.txt"
    assert landed.exists() and not os.path.exists(new[0])
    assert database.file_index.get_file_by_path(str(landed)), "the index follows the move"
    assert all(str(p).startswith(str(folder)) for p in folder.rglob("*")), "nothing left the test folder"
    back = mcp.undo_last_organize()
    assert back["reverted"] == 2 and os.path.exists(new[0]) and database.file_index.get_file_by_path(new[0])


def test_files_the_planner_forgets_under_broad_instructions_get_a_second_pass(monkeypatch, folder):
    """(Found live in Claude Desktop, 200 files, 'sort the files by what's in them': the planner left 53 notes out,
    and they'd have stayed put. Filect asks again about just those, naming the folders it made.)"""
    _files(folder, *[f"note {i}.txt" for i in range(8)], "invoice 1.txt", "invoice 2.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    asks = []

    def forgetful(instructions, files, *a, **k):
        asks.append((instructions, sorted(f["file_name"] for f in files)))
        if len(asks) == 1:                    # the first plan: forgets 5 of the notes
            keep = [f for f in files if "invoice" in f["file_name"] or f["file_name"] in ("note 0.txt", "note 1.txt",
                                                                                          "note 2.txt")]
            return _planner(instructions, keep)
        return {"folders": {"Notes": [f["id"] for f in files]}}
    monkeypatch.setattr(ai_organizer, "request_organization_plan", forgetful)
    res = _gen(str(folder), "sort the files by what's in them")
    assert res["total_files"] == 10 and "left_where_they_are" not in res, res
    assert len(asks) == 2 and asks[1][1] == [f"note {i}.txt" for i in range(3, 8)], "asked again about ONLY those"
    assert "sort the files by what's in them" in asks[1][0] and "Notes" in asks[1][0], "same words + its folders"
    assert sorted(res["proposed_folders"]["Notes"]) == sorted(f"note {i}.txt" for i in range(8))


def test_files_the_instructions_dont_cover_still_stay_put_after_the_second_pass(monkeypatch, folder):
    _files(folder, "invoice 1.txt", "invoice 2.txt", "a.txt", "b.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    asks = []

    def invoices_only(instructions, files, *a, **k):
        asks.append(sorted(f["file_name"] for f in files))
        return {"folders": {"Billing": [f["id"] for f in files if "invoice" in f["file_name"]]}}
    monkeypatch.setattr(ai_organizer, "request_organization_plan", invoices_only)
    res = _gen(str(folder), "put the invoices in Billing")
    assert res["proposed_folders"] == {"Billing": ["invoice 1.txt", "invoice 2.txt"]}, res
    assert res["left_where_they_are"] == 2 and sorted(res["left_examples"]) == ["a.txt", "b.txt"]
    assert asks == [["a.txt", "b.txt", "invoice 1.txt", "invoice 2.txt"], ["a.txt", "b.txt"]]


def test_a_second_pass_that_fails_or_invents_files_changes_nothing(monkeypatch, folder):
    _files(folder, "a.txt", "b.txt", "c.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    calls = []

    def flaky(instructions, files, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            return {"folders": {"Notes": [f["id"] for f in files if f["file_name"] == "a.txt"]}}
        if len(calls) == 2:
            return {"folders": {"Notes": [99999, "x"], "Also": [f["id"] for f in files if f["file_name"] == "a.txt"]}}
        raise AssertionError("asked a third time")
    monkeypatch.setattr(ai_organizer, "request_organization_plan", flaky)
    res = _gen(str(folder), "sort by content")
    assert res["proposed_folders"] == {"Notes": ["a.txt"]} and res["left_where_they_are"] == 2, res



def test_a_big_plan_says_the_full_count_of_each_folder(monkeypatch, folder):
    _files(folder, *[f"note {i:02d}.txt" for i in range(25)])
    monkeypatch.setattr(r, "_ask", Agent())
    res = _gen(str(folder))
    assert len(res["proposed_folders"]["Notes"]) == 20 and res["files_per_folder"] == {"Notes": 25}, res
    assert "up to 20" in res["proposed_folders_note"]


def test_a_folder_the_organize_emptied_is_explained_and_comes_back_on_undo(monkeypatch, folder):
    """(Found live in Claude Desktop: apply said only removed_empty_folders=[...\\old], so Claude told the person
    Filect "deleted a folder that wasn't in the plan" — and after undo, that it "didn't come back". It held a file
    the organize moved out, and undo recreates it. The answers now say so.)"""
    _files(folder, "old/untitled.txt", "invoice 1.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    plan_id = _gen(str(folder))["plan_id"]
    done = mcp.apply_organization_plan(plan_id, confirm=True)
    old = folder / "old"
    assert not old.exists() and [os.path.normcase(p) for p in done["removed_empty_folders"]] == \
        [os.path.normcase(str(old))], done
    note = done["removed_empty_folders_note"]
    assert "Nothing in them was deleted" in note and "undo_last_organize" in note and "recreates" in note
    back = mcp.undo_last_organize()
    assert (old / "untitled.txt").exists(), "undo put the file back, in its folder"
    assert [os.path.normcase(p) for p in back["recreated_folders"]] == [os.path.normcase(str(old))], back
    assert "Nothing in them was deleted" in back["removed_empty_folders_note"]
    assert not (folder / "Notes").exists() and not (folder / "Invoices").exists(), "its own folders are gone"


def test_apply_and_undo_say_nothing_about_folders_when_none_changed(monkeypatch, folder):
    _files(folder, "a.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    done = mcp.apply_organization_plan(_gen(str(folder))["plan_id"], confirm=True)
    assert done["removed_empty_folders"] == [] and "removed_empty_folders_note" not in done
    back = mcp.undo_last_organize()
    assert "recreated_folders" not in back


def test_a_file_the_planner_puts_in_two_folders_moves_once(monkeypatch, folder):
    """(Found live, 80 files: the planner put 3 files in two folders each — 83 moves for 80 files.)"""
    _files(folder, "a.txt", "b.txt", "c.txt")
    monkeypatch.setattr(r, "_ask", Agent())

    def sloppy(instructions, files, *a, **k):
        ids = [f["id"] for f in files]
        return {"folders": {"Notes": ids, "Misc": [ids[0], 999999], "Also": [str(ids[1])]}}   # (+ an invented id)
    monkeypatch.setattr(ai_organizer, "request_organization_plan", sloppy)
    res = _gen(str(folder))
    assert res["total_files"] == 3 and _planned(res) == ["a.txt", "b.txt", "c.txt"], res
    done = mcp.apply_organization_plan(res["plan_id"], confirm=True)
    assert done["moved"] == 3 and not done["errors"], done


def test_files_the_planner_forgets_still_get_a_place_or_are_named(monkeypatch, folder):
    """(Found live, 80 files: the planner left 6 notes out — they'd have stayed put without a word.)"""
    _files(folder, "invoice 1.txt", "invoice 2.txt", "a.txt", "b.txt")
    monkeypatch.setattr(r, "_ask", Agent())

    def forgetful(instructions, files, *a, **k):
        inv = [f["id"] for f in files if "invoice" in f["file_name"]]
        return {"folders": {"Invoices": inv, "Notes": [f["id"] for f in files if f["file_name"] == "a.txt"]}}
    monkeypatch.setattr(ai_organizer, "request_organization_plan", forgetful)
    res = _gen(str(folder))                       # "organize this folder": all of it
    assert _planned(res) == ["a.txt", "b.txt", "invoice 1.txt", "invoice 2.txt"] and "left_where_they_are" not in res
    res = _gen(str(folder), "put the invoices in Invoices")
    assert _planned(res) == ["a.txt", "invoice 1.txt", "invoice 2.txt"], res
    assert res["left_where_they_are"] == 1 and res["left_examples"] == ["b.txt"], res
    assert "the instructions don't cover stays where it is" in res["note"]


def test_the_tool_offers_read_new_files_to_clients():
    tools = {t.name: t for t in asyncio.run(mcp.server.list_tools())}
    t = tools["generate_organization_plan"]
    props = (t.input_schema if hasattr(t, "input_schema") else t.inputSchema)["properties"]
    assert set(props) == {"folder", "instructions", "read_new_files", "folders", "filect_plans", "page"}
    assert props["read_new_files"].get("default") is False
    assert "read first" in t.description and "status='reading'" in t.description


# ---- the client designs the plan (default): Filect lists the files, checks the client's plan, does the moves ----
# (Measured 2026-11-10, 200 files: Claude and Codex filed 100% into 12 clean folders; Filect's one-call planner 21-65%.)

def _no_filect_planner(monkeypatch):
    monkeypatch.setattr(ai_organizer, "request_organization_plan",
                        lambda *a, **k: pytest.fail("Filect's planner ran: the client plans this folder"))


def _ids(res):
    return {row.split(" | ")[1]: int(row.split(" | ")[0]) for row in res["files"]}


def test_by_default_the_client_gets_the_files_to_plan_and_filect_plans_nothing(monkeypatch, folder):
    _files(folder, "invoice 1.txt", "notes.txt", "sub/c.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    _no_filect_planner(monkeypatch)
    res = mcp.generate_organization_plan(str(folder), "sort by what's in them")
    assert res["status"] == "plan_needed" and res["total_files"] == 3 and "plan_id" not in res, res
    rows = {row.split(" | ")[1]: row.split(" | ") for row in res["files"]}
    assert set(rows) == {"invoice 1.txt", "notes.txt", "c.txt"}
    assert rows["c.txt"][2] == "sub" and rows["notes.txt"][2] == "." and rows["invoice 1.txt"][3] == "invoice"
    assert rows["invoice 1.txt"][4] == "about invoice 1.txt", "what's in each file (its caption)"
    assert res["read_first"]["files"] == 3, "the read is reported with the list it was read for"
    for words in ("folders={", "exactly one folder", "not its name or number", "apply_organization_plan"):
        assert words in res["next_step"], words
    assert res["instructions"] == "sort by what's in them"


def test_the_clients_plan_becomes_a_plan_id_and_applies_and_undoes(monkeypatch, folder):
    new = _files(folder, "invoice 1.txt", "notes.txt", "sub/c.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    _no_filect_planner(monkeypatch)
    ids = _ids(mcp.generate_organization_plan(str(folder)))
    res = mcp.generate_organization_plan(str(folder), folders={"Billing": [ids["invoice 1.txt"]],
                                                                "Notes": [ids["notes.txt"], ids["c.txt"]]})
    assert res["planned_by"] == "you" and res["total_files"] == 3 and "read_first" not in res, res
    assert res["proposed_folders"] == {"Billing": ["invoice 1.txt"], "Notes": ["notes.txt", "c.txt"]} or \
        sorted(res["proposed_folders"]["Notes"]) == ["c.txt", "notes.txt"]
    done = mcp.apply_organization_plan(res["plan_id"], confirm=True)
    assert done["moved"] == 3 and done["paths_updated"] == 3 and (folder / "Billing" / "invoice 1.txt").exists()
    assert database.file_index.get_file_by_path(str(folder / "Notes" / "c.txt")), "the index follows the move"
    back = mcp.undo_last_organize()
    assert back["reverted"] == 3 and all(os.path.exists(p) for p in new)


def test_a_clients_plan_is_checked_never_trusted(monkeypatch, folder, tmp_path):
    _files(folder, "a.txt", "b.txt", "c.txt", "d.txt")
    other = tmp_path / "other folder"
    _files(other, "x.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    _no_filect_planner(monkeypatch)
    ids = _ids(mcp.generate_organization_plan(str(folder)))
    outside = _ids(mcp.generate_organization_plan(str(other)))["x.txt"]
    res = mcp.generate_organization_plan(str(folder), folders={
        "../../escape": [ids["a.txt"]],                    # (a path out of the folder)
        r"C:\Windows": [ids["b.txt"]],                     # (an absolute path)
        "CON": ["c.txt"],                                  # (a reserved name; a file NAME instead of an id)
        "Dupes": [ids["a.txt"], 999999, outside, "nope.txt", f"id:{ids['d.txt']}"],
    })
    assert res["planned_by"] == "you" and res["duplicates_dropped"] == 1, res
    assert sorted(res["ignored"]) == sorted(["999999", str(outside), "nope.txt"]), "not files of this folder"
    plan = json.loads((mcp._mcp_plans_dir() / f"plan-{res['plan_id']}.json").read_text(encoding="utf-8"))
    for m in plan:
        dest = os.path.normcase(os.path.normpath(m["destination_path"]))
        assert dest.startswith(os.path.normcase(str(folder)) + os.sep), f"every move stays inside: {dest}"
        assert os.path.basename(os.path.dirname(dest)).lower() != "con", "never a reserved Windows name"
    assert {m["file_name"] for m in plan} == {"a.txt", "b.txt", "c.txt", "d.txt"}, "each file once"
    assert not (folder / "escape").exists() and not other.joinpath("escape").exists()
    empty = mcp.generate_organization_plan(str(folder), folders={"X": [outside, 424242]})
    assert "no files of this folder" in empty["error"], empty


def test_files_the_client_leaves_out_stay_put_and_are_named(monkeypatch, folder):
    _files(folder, "invoice 1.txt", "invoice 2.txt", "a.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    _no_filect_planner(monkeypatch)
    ids = _ids(mcp.generate_organization_plan(str(folder), "just the invoices into Billing"))
    res = mcp.generate_organization_plan(str(folder), "just the invoices into Billing",
                                         folders={"Billing": [ids["invoice 1.txt"], ids["invoice 2.txt"]]})
    assert res["left_where_they_are"] == 1 and res["left_examples"] == ["a.txt"]
    assert "your plan leaves out stays where it is" in res["note"], res["note"]


def test_a_folder_too_big_to_hand_over_is_planned_by_filect_in_the_background(monkeypatch, folder):
    _files(folder, *[f"n{i}.txt" for i in range(6)])
    monkeypatch.setattr(r, "_ask", Agent())
    monkeypatch.setattr(mcp, "CLIENT_PLAN_MAX", 5)
    monkeypatch.setattr(mcp, "READ_WAIT_SECS", 0.5)
    gate = threading.Event()

    def slow(instructions, files, *a, **k):
        gate.wait(10)
        return _planner(instructions, files)
    monkeypatch.setattr(ai_organizer, "request_organization_plan", slow)
    res = mcp.generate_organization_plan(str(folder), "sort")
    if res.get("status") == "reading":                     # (the read itself may take one call)
        mcp._READS[mcp._nkey(folder)]["finished"].wait(10)
        res = mcp.generate_organization_plan(str(folder), "sort")
    assert res["status"] == "planning" and res["files"] == 6 and "call generate_organization_plan again" in res["note"]
    again = mcp.generate_organization_plan(str(folder), "sort")
    assert again["status"] == "planning" and len(mcp._PLANS) == 1, "the same planning, not a second one"
    gate.set()
    list(mcp._PLANS.values())[0]["finished"].wait(10)
    done = mcp.generate_organization_plan(str(folder), "sort")
    assert done["planned_by"] == "filect" and done["total_files"] == 6 and not mcp._PLANS, done


def test_filect_plans_when_asked_even_for_a_small_folder(monkeypatch, folder):
    _files(folder, "a.txt", "invoice 1.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    res = mcp.generate_organization_plan(str(folder), filect_plans=True)
    assert res["planned_by"] == "filect" and _planned(res) == ["a.txt", "invoice 1.txt"]


def test_a_clients_plan_that_moves_nothing_says_so(monkeypatch, folder):
    _files(folder, "Notes/a.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    _no_filect_planner(monkeypatch)
    ids = _ids(mcp.generate_organization_plan(str(folder)))
    res = mcp.generate_organization_plan(str(folder), folders={"Notes": [ids["a.txt"]]})
    assert "already where the plan puts it" in res["error"], res


def test_a_long_list_comes_in_pages_and_a_plan_waits_for_all_of_them(monkeypatch, folder):
    """(Found live: Codex cut a 350-file list at ~10k tokens, so the model saw 306 rows. A plan from part of the list
    would leave the rest out. The list comes 120 a page; a plan is refused until every page was fetched.)"""
    _files(folder, *[f"n{i:03d}.txt" for i in range(7)])
    monkeypatch.setattr(r, "_ask", Agent())
    monkeypatch.setattr(mcp, "PAGE_ROWS", 3)
    _no_filect_planner(monkeypatch)
    p1 = mcp.generate_organization_plan(str(folder))
    assert p1["page"] == 1 and p1["pages"] == 3 and len(p1["files"]) == 3 and p1["files_on_this_page"] == "1-3 of 7"
    assert p1["next_step"].startswith("FIRST get the rest of the list") and "page=2" in p1["next_step"]
    ids = _ids(p1)
    early = mcp.generate_organization_plan(str(folder), folders={"Notes": list(ids.values())})
    assert early["missing_pages"] == [2, 3] and "Nothing has been moved" in early["error"], early
    p2 = mcp.generate_organization_plan(str(folder), page=2)
    p3 = mcp.generate_organization_plan(str(folder), page=3)
    assert len(p2["files"]) == 3 and len(p3["files"]) == 1 and "FIRST" not in p3["next_step"]
    ids.update(_ids(p2)); ids.update(_ids(p3))
    assert len(ids) == 7, "every file across the pages, once"
    done = mcp.generate_organization_plan(str(folder), folders={"Notes": list(ids.values())})
    assert done["total_files"] == 7 and done["planned_by"] == "you", done
    big = mcp.generate_organization_plan(str(folder), page=99)
    assert big["page"] == 3, "a page past the end gives the last page"


def test_a_short_list_is_one_page_and_never_waits(monkeypatch, folder):
    _files(folder, "a.txt", "b.txt")
    monkeypatch.setattr(r, "_ask", Agent())
    _no_filect_planner(monkeypatch)
    res = mcp.generate_organization_plan(str(folder))
    assert "page" not in res and len(res["files"]) == 2 and "FIRST" not in res["next_step"]
    mcp._SERVED.clear()                                   # (a client that kept its list from before: still fine)
    ok = mcp.generate_organization_plan(str(folder), folders={"Notes": list(_ids(res).values())})
    assert ok["total_files"] == 2


def test_the_list_fits_one_page_per_call_at_the_real_size(monkeypatch, folder):
    _files(folder, *[f"meeting notes with a client about the plan {i:03d}.txt" for i in range(130)])
    monkeypatch.setattr(r, "_ask", Agent())
    _no_filect_planner(monkeypatch)
    res = mcp.generate_organization_plan(str(folder))
    assert res["pages"] == 2 and len(res["files"]) == mcp.PAGE_ROWS
    size = len(json.dumps(res))
    assert size < 24000, f"one page stays well under Codex's ~10k-token cut ({size} chars)"
