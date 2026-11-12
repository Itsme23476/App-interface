"""A4: changing a plan ("put the recipes in a folder called Food") moves only the files the change is about; every
other file keeps the folder it had in the plan you saw. Ported from the Mac (26e1a3b) — it replays the Mac's live
failure, where the AI named the 4 recipes but put 41 of 42 files in Food — plus the Windows additions: a rename
or merge of whole folders is taken whole when the AI SAYS so ("folders_renamed", Mac 09e83da — the earlier guess
from the plan's shape took a plan that dumped every file in Food as a "merge"), and a change never drops a file
out of the plan.
No network: the AI's answers are fakes."""
import copy

import pytest

from app.core import ai_organizer as ao
from app.core.settings import settings

RECIPES = [3506, 3508, 3524, 3525]
PLAN = {"folders": {"harbor-and-pine-bakery": [3486, 3487, 3495, 3499, 3506, 3508] + list(range(3530, 3540)),
                    "personal-documents-and-bills": [3490, 3491, 3496, 3502, 3511, 3512] + list(range(3540, 3546)),
                    "notes-texts-and-code": [3520, 3522, 3524, 3525, 3526, 3493],
                    "photos-animals-nature": list(range(3550, 3556))}}
ALL = [i for ids in PLAN["folders"].values() for i in ids]
FILES = [{"id": i, "file_name": f"f{i}.txt"} for i in ALL]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(settings, "ai_provider", "openai")


def answer(monkeypatch, *replies):
    calls, it = [], iter(replies)

    def fake(msg):
        calls.append(msg)
        return copy.deepcopy(next(it))
    monkeypatch.setattr(ao, "_request_openai", fake)
    return calls


def where(plan):
    return {int(i): n for n, ids in plan["folders"].items() for i in ids}


def change(monkeypatch, feedback, *replies, plan=PLAN):
    calls = answer(monkeypatch, *replies)
    return ao.request_plan_refinement("Organize this folder", copy.deepcopy(plan), feedback, FILES), calls


FOOD = "Put the recipes in a folder called Food."


# ---------------------------------------------------------------------------------------- ported from the Mac
def test_the_live_failure_only_the_recipes_move(monkeypatch):
    bad = {"files_mentioned": RECIPES, "folders": {"food": [i for i in ALL if i != 3493]}}   # (41 of 42 in Food)
    out, _ = change(monkeypatch, FOOD, bad)
    w, before = where(out), where(PLAN)
    assert sorted(i for i in w if w[i] == "food") == sorted(RECIPES), sorted(i for i in w if w[i] == "food")
    assert all(w[i] == before[i] for i in ALL if i not in RECIPES), "everything else keeps its folder"
    assert sorted(w) == sorted(ALL), "every file is still in the plan, once"
    assert "files_mentioned" not in out


def test_no_list_the_new_plan_as_it_came(monkeypatch):
    out, _ = change(monkeypatch, FOOD, {"folders": {"a": ALL[:20], "b": ALL[20:]}})
    assert out["folders"] == {"a": ALL[:20], "b": ALL[20:]}


def test_everything_named_the_whole_new_plan(monkeypatch):
    out, _ = change(monkeypatch, FOOD, {"files_mentioned": ALL, "folders": {"one": ALL}})
    assert out["folders"] == {"one": ALL}


def test_numbers_as_text_repeats_and_junk_are_handled(monkeypatch):
    new = {"files_mentioned": [str(i) for i in RECIPES] + ["x", 3506],
           "folders": {"Food": [str(i) for i in RECIPES] + [99999, "y"]}}
    out, _ = change(monkeypatch, FOOD, new)
    w = where(out)
    assert sorted(i for i in w if w[i] == "Food") == sorted(RECIPES) and sorted(w) == sorted(ALL), w


def test_a_named_file_the_answer_didnt_place_stays_put(monkeypatch):
    out, _ = change(monkeypatch, FOOD, {"files_mentioned": RECIPES + [3550], "folders": {"food": RECIPES}})
    assert where(out)[3550] == "photos-animals-nature"


def test_a_named_file_that_wasnt_in_the_plan_is_added(monkeypatch):
    out, _ = change(monkeypatch, FOOD, {"files_mentioned": RECIPES + [4000], "folders": {"food": RECIPES + [4000]}})
    assert where(out)[4000] == "food"


def test_an_emptied_folder_disappears(monkeypatch):
    pics = PLAN["folders"]["photos-animals-nature"]
    out, _ = change(monkeypatch, "Put the animal photos in Pictures", {"files_mentioned": pics,
                                                                        "folders": {"pictures": pics}})
    assert "photos-animals-nature" not in out["folders"] and len(out["folders"]["pictures"]) == 6


def test_unchanged_answer_still_asks_once_more(monkeypatch):
    same = {"folders": {k: list(v) for k, v in PLAN["folders"].items()}}
    out, calls = change(monkeypatch, FOOD, same, {"files_mentioned": RECIPES, "folders": {"food": RECIPES}})
    assert len(calls) == 2 and sorted(i for i, n in where(out).items() if n == "food") == sorted(RECIPES)


# ------------------------------------------------------------------------------------------ Windows additions
def test_a_rename_takes_the_whole_folder(monkeypatch):
    """'Rename the photos folder to Pictures' names a folder, not files: the AI listed only one photo, and SAYS it's a
    rename, so all six go (the plan-change fix alone left the other five in a folder the plan no longer had)."""
    folders = {("Pictures" if k == "photos-animals-nature" else k): v for k, v in PLAN["folders"].items()}
    out, _ = change(monkeypatch, "Rename the photos folder to Pictures",
                    {"files_mentioned": [3550], "folders_renamed": {"photos-animals-nature": "Pictures"},
                     "folders": folders})
    assert where(out) == {i: ("Pictures" if n == "photos-animals-nature" else n) for i, n in where(PLAN).items()}
    assert "folders_renamed" not in out and "photos-animals-nature" not in out["folders"]


def test_a_merge_of_whole_folders_is_taken_whole(monkeypatch):
    f = PLAN["folders"]
    merged = {"bakery-and-notes": f["harbor-and-pine-bakery"] + f["notes-texts-and-code"],
              "personal-documents-and-bills": f["personal-documents-and-bills"],
              "photos-animals-nature": f["photos-animals-nature"]}
    out, _ = change(monkeypatch, "Merge the bakery and the notes folders",
                    {"files_mentioned": [3486], "folders": merged,
                     "folders_renamed": {"harbor-and-pine-bakery": "bakery-and-notes",
                                         "notes-texts-and-code": "bakery-and-notes"}})
    assert where(out) == where({"folders": merged})


def test_every_file_dumped_in_food_is_not_a_merge(monkeypatch):
    """The trap the shape guess fell into (Mac section 5): the answer lists the 4 recipes but puts EVERY file in Food.
    That looks exactly like a merge of all the folders, and was taken whole. Now only the recipes move."""
    out, _ = change(monkeypatch, FOOD, {"files_mentioned": RECIPES, "folders": {"food": list(ALL)}})
    w, before = where(out), where(PLAN)
    assert sorted(i for i in w if w[i] == "food") == sorted(RECIPES)
    assert all(w[i] == before[i] for i in ALL if i not in RECIPES)


def test_the_food_bug_with_a_rename_field_that_is_junk(monkeypatch):
    bad = {"files_mentioned": RECIPES, "folders_renamed": ["not", "a", "map"],
           "folders": {"food": [i for i in ALL if i != 3493]}}
    out, _ = change(monkeypatch, FOOD, bad)
    w, before = where(out), where(PLAN)
    assert sorted(i for i in w if w[i] == "food") == sorted(RECIPES)
    assert all(w[i] == before[i] for i in ALL if i not in RECIPES)


def test_the_prompt_asks_for_renames(monkeypatch):
    out, calls = change(monkeypatch, "Rename the photos folder to Pictures",
                        {"files_mentioned": RECIPES, "folders": {"food": RECIPES}})
    assert '"folders_renamed"' in calls[0]


def test_whole_plan_words_take_the_whole_new_plan(monkeypatch):
    new = {"files_mentioned": [3506], "folders": {"by-type-docs": ALL[:30], "by-type-images": ALL[30:]}}
    out, _ = change(monkeypatch, "Start over and sort them by type", new)
    assert where(out) == where(new)


def test_a_change_never_drops_a_file_from_the_plan(monkeypatch):
    """(The Organize page's Refine button lost files the AI's new plan forgot.)"""
    new = {"folders": {"food": RECIPES, "rest": [i for i in ALL if i not in RECIPES][:-3]}}     # 3 forgotten
    out, _ = change(monkeypatch, "Start over: food and the rest", new)
    assert sorted(where(out)) == sorted(ALL), "the 3 forgotten files kept their folders"
    for i in [i for i in ALL if i not in RECIPES][-3:]:
        assert where(out)[i] == where(PLAN)[i]


def test_moving_some_files_out_of_a_folder_is_not_a_rename(monkeypatch):
    """Splitting a folder is a file change: only the named files move."""
    f = PLAN["folders"]
    split = dict(f)
    split["notes-texts-and-code"] = [i for i in f["notes-texts-and-code"] if i not in RECIPES]
    split["food"] = [i for i in f["notes-texts-and-code"] if i in RECIPES]
    out, _ = change(monkeypatch, "Put the recipe notes in Food", {"files_mentioned": [3524, 3525], "folders": split})
    w = where(out)
    assert w[3524] == w[3525] == "food" and w[3506] == "harbor-and-pine-bakery"
