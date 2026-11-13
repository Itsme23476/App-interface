"""'Only use the folders already there' leaves a file that fits none of them where it is. Live (stage 1 #6, full
feature test 2026-11-13): Willow Test with only 'Invoices' and 'Recipes' - the garden plan was moved into Recipes.
The mode's instruction said "Omitting a file is FORBIDDEN … pick the most similar folder", and the second ask for
left-out files said "You MUST include EVERY file_id" - against the planner's own rule ("If a file genuinely fits no
existing folder, OMIT IT … The app will leave omitted files in place"). Nothing is sent: the AI calls are stand-ins."""
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.core.ai_organizer as ao  # noqa: E402
from app.core.auto_watcher import existing_folders_instruction  # noqa: E402
from app.core.settings import settings as _settings  # noqa: E402

FILES = [{"id": 1, "file_name": "invoice 100 acme.txt", "file_path": r"C:\w\invoice 100 acme.txt", "tags": ["invoice"]},
         {"id": 2, "file_name": "soup recipe.txt", "file_path": r"C:\w\soup recipe.txt", "tags": ["recipe"]},
         {"id": 3, "file_name": "garden plan old.txt", "file_path": r"C:\w\garden plan old.txt",
          "tags": ["garden", "raised beds", "tomatoes"]}]


def test_the_instruction_lets_a_file_that_fits_none_stay():
    t = existing_folders_instruction("", ["Invoices", "Recipes"])
    assert "Omitting a file is FORBIDDEN" not in t and "most similar to the file's" not in t
    assert "LEAVE THE FILE OUT" in t and "stays where it is" in t
    assert "Step 2" in t and "catch-all" in t, "a catch-all folder still takes what fits nothing else"


def _plan(instruction, second_reply):
    asked = []

    def first(_ins, _files, info):
        return {"folders": {"Invoices": [1], "Recipes": [2]}}        # the garden plan (3) left out

    def ask(message):
        asked.append(message)
        return second_reply
    with mock.patch.object(ao, "_plan_all", first), mock.patch.object(ao, "_request_openai", ask), \
            mock.patch.object(_settings, "ai_provider", "openai"):
        plan, report = ao.plan_and_fill(instruction, FILES)
    return plan, asked


def test_left_out_on_purpose_stays_out():
    ins = existing_folders_instruction("", ["Invoices", "Recipes"])
    plan, asked = _plan(ins, {"folders": {}})                       # the second ask leaves it out too
    placed = {i for ids in plan["folders"].values() for i in ids}
    assert 3 not in placed, plan
    assert asked and "MUST include EVERY" not in asked[0] and "left OUT" in asked[0], asked[:1]


def test_a_forgotten_file_that_fits_is_still_placed():
    ins = existing_folders_instruction("", ["Invoices", "Recipes"])
    plan, _ = _plan(ins, {"folders": {"Recipes": [3]}})              # (as if it had been a recipe the planner forgot)
    assert 3 in plan["folders"]["Recipes"]


def test_other_plans_still_place_every_file():
    plan, asked = _plan("Sort these by what they are", {"folders": {"Garden": [3]}})
    assert asked and "MUST include EVERY" in asked[0]
    assert 3 in plan["folders"].get("Garden", [])
