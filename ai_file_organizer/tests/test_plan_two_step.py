"""Big folders are planned in two steps (ai_organizer._plan_two_step): design the folders, then sort the files into
them in parallel groups, with more thinking. Found 2026-11-10: asked for the whole plan in one answer with no
thinking, the planner made one 150-file "Notes" folder, a 102-file "misc", folders by file NUMBER, or left 69 of
200 files out (21-65% well filed); two steps filed 79-100%.

The planner here is a fake (no network): it reads the request like the real one would and answers by each file's
'about' text. Never the user's files, settings or backend."""
import json
import re
import threading

import pytest

from app.core import ai_organizer as ao
from app.core import vision

TOPICS = ["grocery list", "workout plan", "garden schedule", "podcast outline", "travel checklist"]


def _files(n, start=1):
    out = []
    for i in range(n):
        t = TOPICS[i % len(TOPICS)]
        out.append({"id": start + i, "file_name": f"note {i:03d}.txt", "label": "note", "tags": [t],
                    "caption": f"A {t} note with a few items", "subfolder": "."})
    return out


class Planner:
    """A fake gpt-5-mini behind the proxy: answers design and sorting requests; records every request."""
    def __init__(self, design=None, drop_first=0, bad_names=(), leave=(), fail_design=False):
        self.design = design or [{"name": t.title(), "holds": f"{t}s"} for t in TOPICS]
        self.drop_first, self.bad_names, self.leave, self.fail_design = drop_first, set(bad_names), set(leave), fail_design
        self.calls, self.lock, self.dropped = [], threading.Lock(), 0

    def __call__(self, system, user, max_tokens, effort):
        with self.lock:
            self.calls.append({"system": system, "user": user, "effort": effort})
        if system is ao._DESIGN_SYSTEM:
            return None if self.fail_design else {"folders": self.design}
        assign, leave = {}, []
        for m in re.finditer(r"^id:(\d+) \| ([^|]+)\|.*?about:(.*)$", user, re.M):
            fid, about = int(m.group(1)), m.group(3)
            if fid in self.leave:
                leave.append(fid)
                continue
            with self.lock:
                if self.dropped < self.drop_first:
                    self.dropped += 1
                    continue                        # (this group's answer forgets the file)
            topic = next(t for t in TOPICS if t in about)
            name = topic.title()
            assign[str(fid)] = "Made Up Folder" if fid in self.bad_names else name.lower()   # (case differs)
        return {"assign": assign, "leave": leave}

    def designs(self):
        return [c for c in self.calls if c["system"] is ao._DESIGN_SYSTEM]

    def sorts(self):
        return [c for c in self.calls if c["system"] is ao._ASSIGN_SYSTEM]


@pytest.fixture
def planner(monkeypatch):
    p = Planner()
    monkeypatch.setattr(ao, "_ask_planner", p)
    monkeypatch.setattr(ao, "_plan_with_retries", lambda *a, **k: pytest.fail("the one-call planner was used"))
    return p


def _placed(plan):
    return sorted(i for ids in plan["folders"].values() for i in ids)


def test_a_big_folder_is_designed_then_sorted_and_every_file_lands_by_topic(planner):
    files = _files(200)
    plan = ao.request_organization_plan("Sort the files by what's in them.", files)
    assert _placed(plan) == list(range(1, 201)), "every file, once"
    for name, ids in plan["folders"].items():
        assert {TOPICS[(i - 1) % 5].title() for i in ids} == {name}, f"{name} holds only its topic"
    assert len(planner.designs()) == 1 and len(planner.sorts()) == 5, "one design + 200/40 sorting groups"
    assert "id:" not in planner.designs()[0]["user"], "the design step tracks no file ids"
    assert planner.designs()[0]["effort"] == ao.DESIGN_EFFORT and planner.sorts()[0]["effort"] == ao.ASSIGN_EFFORT


def test_files_a_group_forgot_are_asked_about_once_more(monkeypatch):
    p = Planner(drop_first=7)
    monkeypatch.setattr(ao, "_ask_planner", p)
    plan = ao._plan_two_step("sort by content", _files(120))
    assert _placed(plan) == list(range(1, 121)), "the 7 forgotten files were asked about again and placed"
    assert len(p.sorts()) == 3 + 1, "3 groups, then one more request for just the 7"
    assert len(re.findall(r"^id:", p.sorts()[-1]["user"], re.M)) == 7


def test_a_folder_name_not_on_the_list_is_never_used(monkeypatch):
    p = Planner(bad_names={3, 4})
    monkeypatch.setattr(ao, "_ask_planner", p)
    plan = ao._plan_two_step("sort by content", _files(60))
    assert "Made Up Folder" not in plan["folders"]
    assert 3 not in _placed(plan) and 4 not in _placed(plan) and len(_placed(plan)) == 58, \
        "asked again, still made up: left for the callers' safety nets (never a folder nobody designed)"
    assert len(p.sorts()) == 2 + 1


def test_names_are_matched_whatever_their_capitals_and_never_hold_a_slash(monkeypatch):
    p = Planner(design=[{"name": "Notes/Grocery List", "holds": ""}, {"name": "grocery list"}, "Workout Plan",
                        {"name": "Garden Schedule"}, {"name": "Podcast Outline"}, {"name": "Travel Checklist"}])
    monkeypatch.setattr(ao, "_ask_planner", p)
    folders = ao._design_folders("x", _files(60), "manual")
    assert [d["name"] for d in folders] == ["Notes - Grocery List", "grocery list", "Workout Plan", "Garden Schedule",
                                            "Podcast Outline", "Travel Checklist"]
    assert all("/" not in d["name"] and "\\" not in d["name"] for d in folders)


def test_only_the_bills_go_in_the_folder_named_for_bills(monkeypatch):
    files = _files(60)
    for f in files[:10]:
        f.update(caption="An electricity bill from Northgrid", tags=["bill"])
    design = [{"name": "Money", "holds": "bills"}] + [{"name": t.title()} for t in TOPICS]
    p = Planner(design=design)

    def ask(system, user, max_tokens, effort):
        if system is ao._DESIGN_SYSTEM:
            p.calls.append({"system": system, "user": user, "effort": effort})
            return {"folders": design}
        out = {"assign": {}, "leave": []}
        for m in re.finditer(r"^id:(\d+) \|.*?about:(.*)$", user, re.M):
            about = m.group(2)
            out["assign"][m.group(1)] = "Money" if "bill" in about else next(t for t in TOPICS if t in about).title()
        return out
    monkeypatch.setattr(ao, "_ask_planner", ask)
    plan = ao._plan_two_step("put the bills in Money", files)
    assert sorted(plan["folders"]["Money"]) == list(range(1, 11))
    rules = p.calls[0]["user"]
    assert "EXACT folder names the user typed" in rules and "only bills" in rules


def test_keep_a_folder_as_is_leaves_its_files_put(monkeypatch):
    files = _files(60)
    for f in files[:5]:
        f["subfolder"] = "Work Projects"
    p = Planner(leave={1, 2, 3, 4, 5})
    monkeypatch.setattr(ao, "_ask_planner", p)
    plan = ao._plan_two_step("organize everything but keep Work Projects as is", files)
    assert not set(_placed(plan)) & {1, 2, 3, 4, 5} and len(_placed(plan)) == 55
    assert len(p.sorts()) == 2, "the left files aren't asked about again"
    assert "folder:Work Projects" in p.sorts()[0]["user"], "each file's current folder is shown"


def test_leave_is_ignored_when_the_instruction_cant_ask_for_it(monkeypatch):
    p = Planner(leave={1, 2})
    monkeypatch.setattr(ao, "_ask_planner", p)
    plan = ao._plan_two_step("", _files(60))                        # (no instruction: every file gets a folder)
    assert 1 not in _placed(plan) and 2 not in _placed(plan), "not placed by a 'leave'..."
    assert len(p.sorts()) == 3, "...so they were asked about again"


def test_auto_organize_into_existing_folders_skips_the_design(monkeypatch):
    instr = ("[AUTO-ORGANIZE - EXISTING FOLDERS ONLY]\nUser's instructions: Organize files into appropriate folders\n\n"
             "EXISTING FOLDERS YOU CAN USE: 'Grocery List', 'Workout Plan', 'Garden Schedule', 'Podcast Outline', "
             "'Travel Checklist'\n\nGROUND RULES:\n- ...")
    assert ao._existing_folders_in(instr) == [t.title() for t in TOPICS]
    p = Planner(leave={7})
    monkeypatch.setattr(ao, "_ask_planner", p)
    monkeypatch.setattr(ao, "_plan_with_retries", lambda *a, **k: pytest.fail("one-call planner"))
    plan = ao.request_organization_plan(instr, _files(80))
    assert not p.designs(), "the folders are the existing ones"
    assert set(plan["folders"]) <= {t.title() for t in TOPICS}, "never a folder that isn't already there"
    assert 7 not in _placed(plan) and len(_placed(plan)) == 79
    assert len(p.sorts()) == 2 + 1, "'leave' isn't allowed here (the instruction forbids leaving a file out): asked again"
    assert "EXISTING folders: use only them" in p.sorts()[0]["user"] and "GROUND RULES" in p.sorts()[0]["user"], \
        "the instruction's own rules (decision tree, catch-all) go with every sorting request"


def test_existing_folder_names_are_read_exactly_nested_ones_too():
    from app.core.auto_watcher import existing_folders_instruction
    instr = existing_folders_instruction("", ["lions", "everything-else", "everything-else/pigs", "Photos/Vacation"])
    assert ao._plan_mode(instr) == "auto_existing"
    assert ao._existing_folders_in(instr) == ["lions", "everything-else", "everything-else/pigs", "Photos/Vacation"]


def test_existing_folders_that_cant_be_read_use_the_old_planner(monkeypatch):
    used = []
    monkeypatch.setattr(ao, "_ask_planner", lambda *a, **k: pytest.fail("no two-step without the folder names"))
    monkeypatch.setattr(ao, "_plan_with_retries", lambda instr, files, *a: used.append(len(files)) or
                        {"folders": {"invoices": [f["id"] for f in files]}})
    ao.request_organization_plan("[AUTO-ORGANIZE] EXISTING FOLDERS ONLY. EXISTING FOLDERS YOU CAN USE: invoices", _files(80))
    assert used == [80]


def test_when_the_design_fails_the_one_call_planner_takes_over(monkeypatch):
    p = Planner(fail_design=True)
    monkeypatch.setattr(ao, "_ask_planner", p)
    used = []
    monkeypatch.setattr(ao, "_plan_with_retries", lambda instr, files, *a: used.append(len(files)) or
                        {"folders": {"notes": [f["id"] for f in files]}})
    plan = ao.request_organization_plan("sort by content", _files(70))
    assert used == [70] and _placed(plan) == list(range(1, 71))
    assert len(p.designs()) == 2, "the design was tried twice first"


def test_a_small_folder_keeps_the_one_call_planner(monkeypatch):
    monkeypatch.setattr(ao, "_ask_planner", lambda *a, **k: pytest.fail("two-step for a small folder"))
    used = []
    monkeypatch.setattr(ao, "_plan_with_retries", lambda instr, files, *a: used.append(len(files)) or {"folders": {}})
    ao.request_organization_plan("sort by content", _files(ao.BIG_PLAN_FILES))
    assert used == [ao.BIG_PLAN_FILES]


def test_a_huge_folder_shows_the_design_a_spread_out_sample(planner):
    files = _files(1500)
    ao._design_folders("sort by content", files, "manual")
    listing = planner.designs()[0]["user"]
    assert listing.count("\n- note ") == ao._DESIGN_LIST_MAX and "spread-out sample of 600 of them" in listing
    shown = {int(n) for n in re.findall(r"- note (\d+)\.txt", listing)}
    assert min(shown) < 10 and max(shown) > 1400, "from the whole folder, not just its start"


def test_the_thinking_level_reaches_the_proxy(monkeypatch):
    sent = []
    monkeypatch.setattr(vision, "_call_openai_proxy", lambda *a, **k: sent.append(k) or
                        {"choices": [{"message": {"content": json.dumps({"folders": [{"name": "Notes"}]})}}]})
    assert ao._ask_planner(ao._DESIGN_SYSTEM, "x json", 3000, "low") == {"folders": [{"name": "Notes"}]}
    assert sent[0]["reasoning_effort"] == "low" and sent[0]["model"] == ao.PLAN_MODEL


def test_the_proxy_call_sends_the_thinking_level_only_when_asked(monkeypatch):
    posted = []

    class R:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}
    monkeypatch.setattr(vision, "_get_auth_token", lambda: "t")
    monkeypatch.setattr(vision.requests, "post", lambda url, json=None, headers=None, timeout=None: posted.append((json, timeout)) or R())
    vision._call_openai_proxy("chat", [], max_tokens=10)
    vision._call_openai_proxy("chat", [], max_tokens=10, model=ao.PLAN_MODEL, reasoning_effort="medium")
    assert "reasoning_effort" not in posted[0][0], "every other caller: unchanged request"
    assert posted[1][0]["reasoning_effort"] == "medium" and posted[1][1] >= 180, "more thinking: a longer wait"
