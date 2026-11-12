"""Files the planner leaves out get asked about once more (by what they are); only what's still missing after that
falls back to the closest folder by tags and name. A big folder planned in independent batches gets one tidy pass
(same-meaning folders folded, catch-alls spread). Every plan ends with each file in one folder.
Ported from the Mac (tests/test_plan_fill.py, 2026-11-10/11: plan_and_fill + _tidy_batches) — the Mac cases model the
one-call/batch planner, so they run with the two-step planner off. Windows additions at the end: the two-step planner
(Windows plans folders over 50 files that way) — the files it was told to leave are never asked about again, and its
plans are never tidied (one design call names every folder). No network: every AI answer is a fake."""
import json
import re
from pathlib import Path

import pytest

from app.core import ai_organizer as ao
from app.core import vision as V
from app.core.settings import settings

KINDS = [("photo", "IMG_{}.jpg", ["photo", "beach"]), ("invoice", "invoice_{}.pdf", ["invoice", "bill"]),
         ("recipe", "recipe_{}.txt", ["recipe", "baking"]), ("screenshot", "Screenshot {}.png", ["screenshot"])]
BASE_HOME = {"photo": "Photos", "invoice": "Invoices", "recipe": "Recipes", "screenshot": "Screenshots"}
HOME = dict(BASE_HOME)


def files(n):
    out = []
    for i in range(n):
        label, pat, tags = KINDS[i % 4]
        out.append({"id": 1000 + i, "file_name": pat.format(i), "label": label, "tags": tags, "caption": label})
    return out


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(ao, "BIG_PLAN_FILES", 10 ** 9)          # (the Mac's planner: one call / batches of 250)
    HOME.clear()
    HOME.update(BASE_HOME)


class Fake:
    """plan: how the plan request answers a chunk (drops the ids the case picks). ask: the second ask."""

    def __init__(self, drop=lambda fid: False, second="right", fail_chunks=(), names=None, tidy=None, twice=()):
        self.drop, self.second, self.fail_chunks = drop, second, set(fail_chunks)
        self.names = names or (lambda batch, label: HOME[label])   # (the folder name a batch picks for a kind)
        self.tidy = tidy if tidy is not None else {"same": {}, "catch_all": []}
        self.twice = set(twice)                                    # (ids the planner lists in two folders)
        self.plan_calls, self.asks, self.tidy_calls = 0, [], []

    def plan(self, instruction, chunk):
        self.plan_calls += 1
        if chunk[0]["id"] in self.fail_chunks:
            return None
        out = {}
        for f in chunk:
            if not self.drop(f["id"]):
                out.setdefault(self.names((f["id"] - 1000) // 250, f["label"]), []).append(f["id"])
                if f["id"] in self.twice:
                    out.setdefault("Extra", []).append(f["id"])
        return {"folders": out}

    def proxy(self, endpoint, messages, **kw):                    # (the tidy pass's own call)
        self.tidy_calls.append(messages[-1]["content"])
        if self.tidy == "fail":
            return None
        return {"choices": [{"message": {"content": json.dumps(self.tidy)}}]}

    def ask(self, message):
        self.asks.append(message)
        ids = [int(x) for x in re.findall(r"id:(\d+)", message)]
        if self.second == "fail":
            return None
        if self.second == "half":
            ids = ids[: len(ids) // 2]
        by_id = {f["id"]: f for f in files(2000)}
        out = {}
        for fid in ids:
            name = HOME[by_id[fid]["label"]]
            out.setdefault(name.lower() + "  " if self.second == "respelled" else name, []).append(fid)
        if self.second == "stray":
            out["Photos"] += [1000, 1000, 99999, "junk"]           # (already placed, twice, unknown, not a number)
        return {"folders": out}


def run(monkeypatch, fake, fs, instruction="Sort these by what they are."):
    monkeypatch.setattr(ao, "_request_plan_single", fake.plan)
    monkeypatch.setattr(ao, "_request_openai", fake.ask)
    monkeypatch.setattr(V, "_call_openai_proxy", fake.proxy)
    return ao.plan_and_fill(instruction, fs)


def where(plan):
    return {int(fid): name for name, ids in plan["folders"].items() for fid in ids}


def right(plan, fs):
    w = where(plan)
    return sum(1 for f in fs if w.get(f["id"]) == HOME[f["label"]])


def check_once_each(plan, fs):
    all_ids = [int(fid) for ids in plan["folders"].values() for fid in ids]
    assert len(all_ids) == len(set(all_ids)) == len(fs), (len(all_ids), len(set(all_ids)), len(fs))


# ------------------------------------------------------------------------------------------- ported from the Mac
def test_missed_files_are_asked_about_and_land_by_what_they_are(monkeypatch):
    fs = files(200)
    fake = Fake(drop=lambda fid: fid % 7 == 0)
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    assert rep == {"left_out": 29, "placed_on_second_ask": 29, "guessed": []}, rep
    assert right(plan, fs) == 200, right(plan, fs)
    assert len(fake.asks) == 1 and len(re.findall(r"id:(\d+)", fake.asks[0])) == 29, "one more ask, only the missed files"
    assert all(f'"{n}"' in fake.asks[0] for n in ("Photos", "Invoices", "Recipes", "Screenshots")), "it sees the folders"


def test_names_that_mean_the_same_but_arent_spelled_alike(monkeypatch):
    """The old fallback matched spelling: a photo tagged "photo" never reached "Images". The second ask goes by meaning."""
    HOME.update({"photo": "Images", "invoice": "Bills", "recipe": "Baking", "screenshot": "Screen grabs"})
    fs = files(200)
    plan, rep = run(monkeypatch, Fake(drop=lambda fid: fid % 7 == 0), fs)
    check_once_each(plan, fs)
    assert rep["guessed"] == [] and right(plan, fs) == 200, (rep, right(plan, fs))


def test_nothing_missed_no_second_ask(monkeypatch):
    fs = files(120)
    fake = Fake()
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    assert fake.asks == [] and rep["left_out"] == 0


def test_a_slightly_different_spelling_joins_the_plans_folder(monkeypatch):
    fs = files(80)
    plan, rep = run(monkeypatch, Fake(drop=lambda fid: fid % 5 == 0, second="respelled"), fs)
    check_once_each(plan, fs)
    assert sorted(plan["folders"]) == ["Invoices", "Photos", "Recipes", "Screenshots"], sorted(plan["folders"])
    assert right(plan, fs) == 80


def test_stray_or_repeated_ids_in_the_answer_are_ignored(monkeypatch):
    fs = files(80)
    plan, rep = run(monkeypatch, Fake(drop=lambda fid: fid % 5 == 0, second="stray"), fs)
    check_once_each(plan, fs)
    assert rep["placed_on_second_ask"] == 16 and right(plan, fs) == 80, rep


def test_second_ask_fails_too_then_tags_and_name_decide(monkeypatch):
    fs = files(80)
    fake = Fake(drop=lambda fid: fid % 5 == 0, second="fail")
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    assert rep["placed_on_second_ask"] == 0 and len(rep["guessed"]) == 16, rep
    assert len(fake.asks) == ao._PLAN_CHUNK_RETRIES, "a failed second ask is retried, then given up"
    assert right(plan, fs) == 80, "the fallback uses the tags: a photo tagged 'photo' goes to Photos"


def test_second_ask_places_some_the_rest_by_tags(monkeypatch):
    fs = files(80)
    plan, rep = run(monkeypatch, Fake(drop=lambda fid: fid % 5 == 0, second="half"), fs)
    check_once_each(plan, fs)
    assert rep["placed_on_second_ask"] == 8 and len(rep["guessed"]) == 8, rep


def test_a_whole_failed_chunk_of_a_big_folder_is_asked_about(monkeypatch):
    fs = files(600)                                              # (3 chunks of 250, 250, 100)
    fake = Fake(fail_chunks={1250})
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    assert {k: rep[k] for k in ("left_out", "placed_on_second_ask", "guessed")} == \
        {"left_out": 250, "placed_on_second_ask": 250, "guessed": []}, rep
    assert right(plan, fs) == 600


def test_existing_folders_only_stays_inside_the_list(monkeypatch):
    fs = files(40)
    fake = Fake(drop=lambda fid: fid % 4 == 0)
    run(monkeypatch, fake, fs, "[AUTO-ORGANIZE - EXISTING FOLDERS ONLY] Use these folders: Photos, Invoices")
    assert "Use ONLY folders from the list" in fake.asks[0] and "make a new folder" not in fake.asks[0]


def test_no_plan_at_all_stays_none(monkeypatch):
    fs = files(30)
    fake = Fake(fail_chunks={1000})
    plan, rep = run(monkeypatch, fake, fs)
    assert plan is None and fake.asks == []


def _batches(batch, label):
    """Three batches that never saw each other's names: batch 1 says "Pictures" for photos, batch 2 puts invoices and
    recipes in a catch-all "Documents" (the Mac's live 1,200-file run: "financial-documents" next to "invoices")."""
    if batch == 1 and label == "photo":
        return "Pictures"
    if batch == 2 and label in ("invoice", "recipe"):
        return "Documents"
    return HOME[label]


def test_batches_tidy_folds_same_meaning_and_spreads_a_catch_all(monkeypatch):
    fs = files(600)
    fake = Fake(names=_batches, tidy={"same": {"Pictures": "Photos"}, "catch_all": ["Documents"]})
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    assert sorted(plan["folders"]) == ["Invoices", "Photos", "Recipes", "Screenshots"], sorted(plan["folders"])
    assert right(plan, fs) == 600, right(plan, fs)
    assert rep["tidy"]["folded"] == {"Pictures": "Photos"} and rep["tidy"]["catch_alls"] == ["Documents"], rep["tidy"]
    assert re.search(r'"Pictures": \d+ files, e\.g\. IMG_', fake.tidy_calls[0]), "it sees names, sizes, examples"
    assert "Use ONLY folders from the list" in fake.asks[-1], "a catch-all's files only go to folders that exist"


def test_a_catch_all_file_with_no_answer_stays_put(monkeypatch):
    fs = files(600)
    fake = Fake(names=_batches, tidy={"same": {}, "catch_all": ["Documents"]}, second="half")
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    w = where(plan)
    left = [f for f in fs if w[f["id"]] == "Documents"]
    assert 0 < len(left) < 50 and rep["tidy"]["moved"] == 50 - len(left), (len(left), rep["tidy"])


def test_tidy_never_folds_or_empties_a_name_you_gave(monkeypatch):
    fs = files(600)
    fake = Fake(names=_batches, tidy={"same": {"Documents": "Invoices", "Pictures": "Photos"}, "catch_all": ["Documents"]})
    plan, rep = run(monkeypatch, fake, fs, "Put work papers in Documents, sort the rest by what they are.")
    check_once_each(plan, fs)
    assert len(plan["folders"]["Documents"]) == 50 and "Pictures" not in plan["folders"], sorted(plan["folders"])


def test_tidy_ignores_names_that_are_not_in_the_plan(monkeypatch):
    fs = files(600)
    fake = Fake(names=_batches, tidy={"same": {"Pictures": "Brand New Folder", "Nope": "Photos"}, "catch_all": ["Nope", 7]})
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    assert "Pictures" in plan["folders"] and "Brand New Folder" not in plan["folders"] and "Documents" in plan["folders"]


def test_tidy_never_folds_specific_folders_into_a_catch_all(monkeypatch):
    """The Mac's live run 2 (1,200 files): the answer folded "invoices" etc. INTO "financial-documents" and also called
    it a catch-all, so its files were forced into "contracts" and "recipes". Folds into or out of a catch-all are
    ignored, and a catch-all file the second ask keeps in its own catch-all stays there."""
    fs = files(600)
    fake = Fake(names=_batches, tidy={"same": {"Invoices": "Documents", "Recipes": "Documents", "Pictures": "Photos"},
                                      "catch_all": ["Documents"]})
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    assert {"Invoices", "Recipes", "Photos"} <= set(plan["folders"]) and "Pictures" not in plan["folders"], sorted(plan["folders"])
    assert right(plan, fs) == 600 and '"Documents"' in fake.asks[-1], "the catch-all is offered as a place to stay"


def test_tidy_fails_the_plan_stays_as_it_was(monkeypatch):
    fs = files(600)
    fake = Fake(names=_batches, tidy="fail")
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    assert {"Pictures", "Documents", "Photos"} <= set(plan["folders"]) and rep["tidy"]["moved"] == 0


def test_small_plans_are_not_tidied(monkeypatch):
    fake = Fake(tidy={"same": {"Photos": "Invoices"}, "catch_all": []})
    plan, rep = run(monkeypatch, fake, files(200))
    assert fake.tidy_calls == [] and "tidy" not in rep and "Photos" in plan["folders"]


def test_no_file_is_ever_in_two_folders(monkeypatch):
    fs = files(40)
    plan, rep = run(monkeypatch, Fake(twice={1000, 1005, 1010}), fs)
    check_once_each(plan, fs)


def test_every_caller_goes_through_it():
    import inspect
    from app.core import auto_watcher
    from app.ui import organize_page
    src = Path(__file__).resolve().parents[2] / "filect_mcp" / "server.py"
    for name, text in (("Organize page + voice", inspect.getsource(organize_page.PlanWorker)),
                       ("Auto-Organize", inspect.getsource(auto_watcher)), ("MCP", src.read_text(encoding="utf-8"))):
        assert "request_organization_plan(" in text, name
    src_ao = Path(ao.__file__).read_text(encoding="utf-8")      # (the file: another test may swap the function out)
    body = src_ao[src_ao.index("def request_organization_plan("):src_ao.index("def plan_and_fill(")]
    assert "plan_and_fill(" in body


# ------------------------------------------------------------------- Windows: the two-step planner (over 50 files)
def _two_step(plan_ids, leave=()):
    def fake(instruction, fs, given=None, info=None):
        if info is not None and leave:
            info["leave"] = set(leave)
        out = {}
        for f in fs:
            if f["id"] in plan_ids:
                out.setdefault(HOME[f["label"]], []).append(f["id"])
        return {"folders": out}
    return fake


def test_two_step_files_you_kept_are_never_asked_about_again(monkeypatch):
    """"Organize, but leave the screenshots": the two-step plan leaves them out ON PURPOSE. Only files it simply
    forgot get the second ask; the kept ones stay out of the plan (they stay where they are)."""
    monkeypatch.setattr(ao, "BIG_PLAN_FILES", 50)
    fs = files(120)
    kept = {f["id"] for f in fs if f["label"] == "screenshot"}
    forgot = {f["id"] for f in fs if f["label"] == "photo"}
    monkeypatch.setattr(ao, "_plan_two_step", _two_step({f["id"] for f in fs} - kept - forgot, leave=kept))
    fake = Fake()
    plan, rep = run(monkeypatch, fake, fs, "Organize these, but leave the screenshots where they are.")
    asked = {int(x) for x in re.findall(r"id:(\d+)", fake.asks[0])}
    assert asked == forgot and rep["left_out"] == len(forgot), (len(asked), rep)
    assert not (set(where(plan)) & kept), "the kept screenshots are not in the plan"
    assert all(where(plan)[i] == "Photos" for i in forgot)


def test_two_step_plans_are_never_tidied(monkeypatch):
    """One design call names every folder (no batches that didn't see each other): no tidy call, even for 600 files."""
    monkeypatch.setattr(ao, "BIG_PLAN_FILES", 50)
    fs = files(600)
    monkeypatch.setattr(ao, "_plan_two_step", _two_step({f["id"] for f in fs}))
    fake = Fake(tidy={"same": {"Photos": "Invoices"}, "catch_all": []})
    plan, rep = run(monkeypatch, fake, fs)
    assert fake.tidy_calls == [] and "tidy" not in rep and right(plan, fs) == 600


def test_two_step_failing_falls_back_to_batches_which_are_tidied(monkeypatch):
    monkeypatch.setattr(ao, "BIG_PLAN_FILES", 50)
    monkeypatch.setattr(ao, "_plan_two_step", lambda *a, **k: None)
    fs = files(600)
    fake = Fake(names=_batches, tidy={"same": {"Pictures": "Photos"}, "catch_all": ["Documents"]})
    plan, rep = run(monkeypatch, fake, fs)
    check_once_each(plan, fs)
    assert sorted(plan["folders"]) == ["Invoices", "Photos", "Recipes", "Screenshots"] and right(plan, fs) == 600
