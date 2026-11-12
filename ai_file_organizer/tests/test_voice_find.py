"""Find a file by voice: the search, the check, the flow and the panel (ported from the Mac's test_voice_find.py, plus
the Windows parts). Windows Search, Claude and reading are stubbed; real files only in throwaway folders under your home
folder (Filect never searches AppData, where the temp folder is); settings.json and the real index are never touched."""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.core import database, file_finder as ff, intent_router as ir, organize_reader as reader  # noqa: E402
from app.core import search as search_mod  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402
from app.ui import voice_find_controller as vfc  # noqa: E402
from app.ui.organize_overlay import OrganizeOverlay, _ResultRow, _INNER_W  # noqa: E402

_INDEX = database.FileIndex(Path(tempfile.mkdtemp(prefix="filect-index-")) / "i.db")


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch):
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)      # NEVER write the user's settings.json
    monkeypatch.setattr(settings, "exclusion_patterns", [])                  # (the user's own list mustn't matter)
    monkeypatch.setattr(database, "file_index", _INDEX)
    monkeypatch.setattr(search_mod.search_service, "index", _INDEX)
    monkeypatch.setattr(ff, "thumbnail", lambda p, d, px=96: "")              # (no pictures in tests)
    monkeypatch.setattr(ff, "wsearch", lambda where, roots, timeout=0, folders=False: [])
    yield


def _pump(cond, timeout=8.0):
    t = time.time()
    while time.time() - t < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


def _files(*names):
    """Real files in a throwaway folder under the home folder (C:\\Users\\you\\filect-test-…), one second apart."""
    d = Path(tempfile.mkdtemp(prefix="filect-test-", dir=str(Path.home())))
    out = []
    for i, n in enumerate(names):
        p = d / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(n)
        os.utime(p, (1_700_000_000 + i * 1000, 1_700_000_000 + i * 1000))
        out.append(str(p))
    return d, out


def _clean_up(d):
    shutil.rmtree(d, ignore_errors=True)


def _details(**kw):
    return ff.clean(dict({"words": [["invoice"]]}, **kw))


# ---- the details ----------------------------------------------------------------------------------------------
def test_clean_makes_anything_safe():
    d = ff.clean({"words": [['in"vo*ice\\', "bill"], "Mark", 7, [""]], "kind": "nonsense", "after": "not a date",
                  "before": "2026-11-01T10:00", "date_words": ["Nov\"ember"], "newest": 1})
    assert d["words"] == [["invoice", "bill"], ["Mark"]], d["words"]
    assert d["kind"] == "" and d["after"] == "" and d["before"] == "2026-11-01"
    assert d["date_words"] == ["November"] and d["newest"] is True
    assert ff.clean(None)["words"] == [] and ff.clean("x")["open"] is False
    assert ff.clean({"words": [["grandma’s"]]})["words"] == [["grandma's"]], "an apostrophe stays (it's in names)"
    assert ff.clean({"words": [["50%", "[x]"]]})["words"] == [["50", "x"]], "wildcards never reach the query"


def test_month_range():
    from datetime import datetime
    now = datetime(2026, 11, 9)
    assert ff.month_range("November", now) == ("2026-11-01", "2026-12-01")
    assert ff.month_range("december", now) == ("2025-12-01", "2026-01-01")
    assert ff.month_range("Acme", now) == ("", "")


def test_merge_adds_detail():
    d = ff.merge(_details(kind="pdf"), ff.clean({"words": [["Acme"]], "after": "2026-10-01"}))
    assert d["words"] == [["invoice"], ["Acme"]] and d["kind"] == "pdf" and d["after"] == "2026-10-01"


# ---- the query (Windows Search) -------------------------------------------------------------------------------
def test_query_all_words_any_alternative():
    q = ff.search_query(ff.clean({"words": [["invoice", "bill"], ["Mark"]], "kind": "pdf"}))
    assert q.count(") AND (") == 2, q
    assert "System.FileName LIKE '%invoice%'" in q and "CONTAINS(*, '\"bill*\"')" in q and "'%Mark%'" in q
    assert "System.FileExtension = '.pdf'" in q


def test_query_is_safe_with_odd_names():
    q = ff.search_query(ff.clean({"words": [["grandma's"], ["INV_125"]]}))
    assert "LIKE '%grandma''s%'" in q and "CONTAINS(*, '\"grandma''s*\"')" in q, q
    assert "LIKE '%INV[_]125%'" in q, "an underscore in a name is a letter, not 'any character'"


def test_query_date_or_month_written_inside():
    d = _details(after="2026-11-01", before="2026-12-01", date_words=["November"])
    q = ff.search_query(d)
    assert f"System.DateModified >= '{ff._utc('2026-11-01')}'" in q and "System.DateCreated < " in q
    assert "CONTAINS(*, '\"November*\"')" in q
    assert "DateModified" not in ff.search_query(d, use_date=False)


def test_a_day_starts_at_local_midnight():
    from datetime import datetime, timezone
    u = datetime.strptime(ff._utc("2026-11-05"), "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert u.timestamp() == datetime(2026, 11, 5).timestamp()
    u = datetime.strptime(ff._utc("2026-07-15"), "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert u.timestamp() == datetime(2026, 7, 15).timestamp(), "summer time too"


def test_a_day_matches_exactly_a_month_by_prefix():
    q = ff.search_query(_details(date_words=["October 2", "November"]))
    assert "CONTAINS(*, '\"October 2\"')" in q and "CONTAINS(*, '\"November*\"')" in q


def test_type_and_date_alone_need_no_check_and_the_count_is_true(monkeypatch):
    d0, paths = _files(*[f"2026-11-05 {i}.mp4" for i in range(11)])
    monkeypatch.setattr(ff, "search", lambda d: {"rows": [ff._row(p) for p in paths], "dropped": ""})
    calls = _stub_check(monkeypatch, None)
    try:
        r = ff.find(ff.clean({"kind": "recording", "after": "2026-11-05", "before": "2026-11-06"}), "x", ["claude"])
        assert not calls and r["exact"] == 11 and len(r["rows"]) == 3 and len(r["all"]) == 11
        times = [x["mtime"] for x in r["rows"]]
        assert times == sorted(times, reverse=True)
    finally:
        _clean_up(d0)


def test_widens_date_first_then_type(monkeypatch):
    calls = []
    monkeypatch.setattr(ff, "wsearch", lambda q, roots, timeout=0, folders=False: (calls.append(q) or []))
    d = _details(kind="pdf", after="2026-11-01", before="2026-12-01")
    assert ff.search(d)["rows"] == []
    assert len(calls) == 3 and "DateModified" in calls[0] and "'.pdf'" in calls[0]
    assert "DateModified" not in calls[1] and "'.pdf'" in calls[1]
    assert "DateModified" not in calls[2] and "'.pdf'" not in calls[2]


def test_a_photo_is_never_swapped_for_a_text_file(monkeypatch):
    """Live: "the photo of the hot air balloon" showed "birthday party ideas.txt" (it mentions balloons) after the type
    was dropped. For photos, videos and recordings the type is never dropped: the deep look comes next."""
    calls = []
    monkeypatch.setattr(ff, "wsearch", lambda q, roots, timeout=0, folders=False: (calls.append(q) or []))
    assert ff.search(ff.clean({"words": [["balloon"]], "kind": "image"}))["rows"] == []
    assert len(calls) == 1 and "'.png'" in calls[0], "only with the type"
    calls.clear()
    ff.search(ff.clean({"words": [["balloon"]], "kind": "pdf"}))
    assert len(calls) == 2 and "'.pdf'" not in calls[1], "a document type may still be dropped"


def test_says_what_it_dropped(monkeypatch):
    d0, (a,) = _files("invoice.pdf")
    try:
        monkeypatch.setattr(ff, "wsearch", lambda q, roots, timeout=0, folders=False: [a] if "DateModified" not in q else [])
        r = ff.search(_details(kind="pdf", after="2026-11-01"))
        assert r["dropped"] == "date" and len(r["rows"]) == 1
        monkeypatch.setattr(ff, "wsearch", lambda q, roots, timeout=0, folders=False: [a] if "'.pdf'" not in q else [])
        assert ff.search(_details(kind="pdf"))["dropped"] == "type"
    finally:
        _clean_up(d0)


def test_the_index_and_windows_search_are_one_list_without_case_twins(monkeypatch):
    d0, (a,) = _files("Invoice.pdf")
    try:
        rows = ff._combine([ff._row(a)], [a.upper(), a.lower()], _details())
        assert len(rows) == 1, "C:\\X.pdf and c:\\x.pdf are one file on Windows"
    finally:
        _clean_up(d0)


# ---- what's worth showing -------------------------------------------------------------------------------------
def test_junk_is_filtered_but_a_project_folders_invoice_stays():
    h = str(Path.home())
    assert ff._junk(rf"{h}\AppData\Local\Temp\invoice.pdf") and ff._junk(rf"{h}\p\node_modules\x\invoice.pdf")
    assert ff._junk(r"C:\Program Files\Acme\invoice.pdf") and ff._junk(r"C:\Windows\System32\invoice.pdf")
    assert ff._junk(rf"{h}\.hidden\invoice.pdf") and ff._junk(rf"{h}\p\venv\invoice.pdf")
    assert ff._junk(rf"{h}\Downloads\$RECYCLE.BIN\invoice.pdf")
    assert not ff._junk(rf"{h}\sg-invoicer\out\INV-125.pdf"), "a project folder's invoice is a real invoice"
    assert not ff._junk(rf"{h}\OneDrive\Documents\Taxes\invoice.pdf")
    assert not ff._junk(r"D:\Work\Clients\invoice.pdf")


def test_code_only_if_its_name_was_said():
    d0, (a, b) = _files("billing.py", "invoice.py")
    try:
        rows = ff._combine([], [a, b], _details())
        assert [r["file_name"] for r in rows] == ["invoice.py"]
    finally:
        _clean_up(d0)


def test_system_and_temp_files_never_show():
    d0, paths = _files("desktop.ini", "Thumbs.db", "~$invoice.docx", "invoice.crdownload", "invoice.pdf")
    try:
        assert [r["file_name"] for r in ff._combine([], paths, _details())] == ["invoice.pdf"]
    finally:
        _clean_up(d0)


def test_a_onedrive_file_that_is_only_in_the_cloud_is_shown_but_never_read(monkeypatch):
    """Unlike indexing (which skips them), a cloud-only file is a real file the person may be looking for."""
    d0, (a,) = _files("invoice cloud.pdf")
    try:
        monkeypatch.setattr(ff, "_cloud_only", lambda st: True)
        r = ff._row(a)
        assert r["dataless"] and [x["file_name"] for x in ff._combine([], [a], _details())] == ["invoice cloud.pdf"]
        read = []
        from app.core import text_extract as tx
        monkeypatch.setattr(tx, "extract_pdf_text", lambda p, max_pages=1: read.append(p) or "x")
        assert ff._excerpt(r) == "" and read == [], "its text isn't read (that would download it)"
    finally:
        _clean_up(d0)


def test_rank():
    d0, (a, b, c) = _files("mentions it.pdf", "invoice old.pdf", "invoice new.pdf")
    try:
        rows = [ff._row(p) for p in (a, b, c)]
        assert [r["file_name"] for r in ff.rank(rows, _details())][:2] == ["invoice new.pdf", "invoice old.pdf"]
        assert ff.rank(rows, _details(newest=True))[0]["file_name"] == "invoice new.pdf"
    finally:
        _clean_up(d0)


def test_candidates_keep_newest_even_with_a_dull_name():
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(30)], "INV-125.pdf")
    try:
        rows = ff.rank([ff._row(p) for p in paths], _details())
        assert "INV-125.pdf" in [r["file_name"] for r in ff.candidates(rows, _details())]
    finally:
        _clean_up(d0)


# ---- the check ------------------------------------------------------------------------------------------------
def _stub_check(monkeypatch, verdict):
    calls = []

    def fake(heard, rows, may_ask, agents, d=None, loosened=False):
        calls.append((heard, len(rows), may_ask, loosened))
        return verdict(rows) if callable(verdict) else verdict
    monkeypatch.setattr(ff, "check", fake)
    return calls


def _stub_search(monkeypatch, rows, dropped=""):
    monkeypatch.setattr(ff, "search", lambda d: {"rows": rows, "dropped": dropped})


def test_parse_check():
    v = ff.parse_check('```json\n{"picks":[2,"1",9,2,true],"about":{"2":"Hosting"},"ask":"Which?","choices":["A"]}\n```', 3)
    assert v["picks"] == [2, 1] and v["about"][2] == "Hosting" and v["ask"] == "Which?" and v["choices"] == ["A"]
    assert ff.parse_check("sorry", 3) is None and ff.parse_check("[1,2]", 3) is None


def test_clear_names_skip_the_check(monkeypatch):
    d0, (a, b) = _files("invoice one.pdf", "invoice two.pdf")
    _stub_search(monkeypatch, [ff._row(a), ff._row(b)])
    calls = _stub_check(monkeypatch, None)
    try:
        r = ff.find(_details(), "find my invoice", ["claude"])
        assert not calls and len(r["rows"]) == 2 and not r["checked"]
    finally:
        _clean_up(d0)


def test_only_mentions_are_thrown_out(monkeypatch):
    d0, (a, b) = _files("agreement.pdf", "INV-1.pdf")
    _stub_search(monkeypatch, [ff._row(a), ff._row(b)])
    calls = _stub_check(monkeypatch, {"picks": [2], "about": {2: "Hosting"}, "ask": "", "choices": []})
    try:
        r = ff.find(ff.clean({"words": [["invoice"], ["Kimi"]]}), "invoice for Kimi", ["claude"])
        assert [x["file_name"] for x in r["rows"]] == ["INV-1.pdf"] and r["rows"][0]["about"] == "Hosting"
        assert calls[0][2] is False                        # (a name was given: nothing to ask)
    finally:
        _clean_up(d0)


def test_a_real_match_is_not_lost_when_the_check_says_none(monkeypatch):
    d0, (a,) = _files("INV-116 WebCMD invoice.pdf")
    os.utime(a, (ff._ts("2026-10-08"), ff._ts("2026-10-08")))
    _stub_search(monkeypatch, [ff._row(a)])
    _stub_check(monkeypatch, {"picks": [], "about": {}, "ask": "", "choices": []})
    d = ff.clean({"words": [["WebCMD"], ["contract", "invoice"]], "after": "2026-10-01", "before": "2026-11-01"})
    d2 = ff.clean({"words": [["WebCMD"], ["invoice"], ["Acme"]], "after": "2026-10-01", "before": "2026-11-01"})
    try:
        r = ff.find(d, "x", ["claude"])
        assert [x["file_name"] for x in r["rows"]] == ["INV-116 WebCMD invoice.pdf"] and not r["dropped"]
        assert ff._sure([ff._row(a)], d2, False) == []     # (a word it doesn't say: still Claude's call)
        out = ff.clean({"words": [["WebCMD"]], "after": "2026-11-01", "before": "2026-12-01"})
        assert ff._sure([ff._row(a)], out, False) == []    # (the wrong month isn't overruled)
    finally:
        _clean_up(d0)


def test_asked_about_the_due_date_its_own_words_overrule_the_check(monkeypatch):
    d0, (a, b) = _files("INV-2041.pdf", "INV-1900.pdf")
    texts = {a: "INVOICE Bill to: Brambleton Bakery Issue date: October 2, 2026 Due date: November 3, 2026",
             b: "INVOICE Bill to: Brambleton Bakery Issue date: August 2, 2026 Due date: September 1, 2026"}
    monkeypatch.setattr(ff, "_describe", lambda rows: [texts[r["file_path"]] for r in rows])
    try:
        rows = [ff._row(a), ff._row(b)]
        due = ff.clean({"words": [["invoice"], ["Brambleton"]], "date_words": ["November"], "when": "due in November"})
        assert [r["file_name"] for r in ff._sure(rows, due, False)] == ["INV-2041.pdf"]
        issued = dict(due, when="from November")
        assert ff._sure(rows, issued, False) == [], "'from November' is the issue date: still Claude's call"
    finally:
        _clean_up(d0)


def test_none_fit_means_not_found(monkeypatch):
    d0, (a,) = _files("agreement.pdf")
    _stub_search(monkeypatch, [ff._row(a)])
    _stub_check(monkeypatch, {"picks": [], "about": {}, "ask": "", "choices": []})
    try:
        assert ff.find(ff.clean({"words": [["invoice"]]}), "x", ["claude"])["rows"] == []
    finally:
        _clean_up(d0)


def test_check_failing_still_shows_the_list(monkeypatch):
    d0, (a,) = _files("agreement.pdf")
    _stub_search(monkeypatch, [ff._row(a)])
    _stub_check(monkeypatch, None)
    try:
        r = ff.find(ff.clean({"words": [["invoice"]]}), "x", ["claude"])
        assert len(r["rows"]) == 1 and not r["checked"]
    finally:
        _clean_up(d0)


def test_many_asks_shows_newest(monkeypatch):
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(14)])
    _stub_search(monkeypatch, ff.rank([ff._row(p) for p in paths], _details()))
    calls = _stub_check(monkeypatch, lambda c: {"picks": list(range(1, 9)), "about": {}, "ask": "Which client?",
                                                "choices": ["Acme"]})
    try:
        r = ff.find(_details(), "find my invoice", ["claude"])
        assert r["many"] and calls[0][2] is True and r["ask"] == "Which client?" and r["choices"] == ["Acme"]
        times = [x["mtime"] for x in r["rows"]]
        assert times == sorted(times, reverse=True) and len(r["rows"]) == 3
        assert ff.find(_details(), "x", ["claude"], can_ask=False)["ask"] == ""     # one question per search
    finally:
        _clean_up(d0)


def test_latest_never_asks(monkeypatch):
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(14)])
    _stub_search(monkeypatch, [ff._row(p) for p in paths])
    calls = _stub_check(monkeypatch, lambda c: {"picks": [1], "about": {}, "ask": "", "choices": []})
    try:
        r = ff.find(_details(newest=True), "my latest invoice", ["claude"])
        assert not r["many"] and calls[0][2] is False and len(r["rows"]) == 1
    finally:
        _clean_up(d0)


def test_latest_is_one_file_even_with_clear_names(monkeypatch):
    d0, paths = _files("invoice old.pdf", "invoice new.pdf")
    _stub_search(monkeypatch, ff.rank([ff._row(p) for p in paths], _details(newest=True)))
    calls = _stub_check(monkeypatch, None)
    try:
        r = ff.find(_details(newest=True), "my latest invoice", ["claude"])
        assert [x["file_name"] for x in r["rows"]] == ["invoice new.pdf"] and len(r["all"]) == 2 and not calls
    finally:
        _clean_up(d0)


def test_only_a_few_fit_is_not_a_lot(monkeypatch):
    d0, paths = _files(*[f"mentions it {i}.pdf" for i in range(14)])
    _stub_search(monkeypatch, [ff._row(p) for p in paths])
    calls = _stub_check(monkeypatch, lambda c: {"picks": [1, 2], "about": {}, "ask": "Which client?", "choices": ["Acme"]})
    try:
        r = ff.find(_details(), "find my invoice", ["claude"])
        assert calls[0][2] is True and not r["many"] and r["ask"] == "" and len(r["rows"]) == 2
    finally:
        _clean_up(d0)


def test_check_is_told_what_was_understood_and_what_from_then_means():
    p = ff._check_prompt("Find the Gladways invoice.", [], [], False,
                         ff.clean({"words": [["invoice"], ["Gladways", "Cloudways"]]}))
    assert "Gladways or Cloudways" in p and "misheard" in p
    p = ff._check_prompt("x", [], [], False, _details(after="2026-11-01", before="2026-12-01", when="in November"))
    assert "in November" in p and "due date" in p
    p = ff._check_prompt("x", [], [], False, _details(after="2026-11-01"), loosened=True)
    assert "ignore the time" in p and "due date" not in p
    p = ff._check_prompt("find the invoice that's due in November", [], [], False,
                         _details(date_words=["November"], when="due in November"))
    assert "when it's DUE" in p and "doesn't count" not in p, "asked about the due date: then it counts"


def test_photos_are_not_offered_as_the_closest(monkeypatch):
    d0, (a,) = _files("IMG_1.png")
    _stub_search(monkeypatch, [ff._row(a)], dropped="date")
    calls = _stub_check(monkeypatch, {"picks": [], "about": {}, "ask": "", "choices": []})
    try:
        r = ff.find(ff.clean({"words": [["star"]], "kind": "image", "after": "2011-03-01", "before": "2011-04-01"}), "x",
                    ["claude"])
        assert r["rows"] == [] and calls[0][3] is False    # (not loosened: so it goes on to offer the deep look)
    finally:
        _clean_up(d0)


def test_only_looked_like_from_then_says_so(monkeypatch):
    d0, (a,) = _files("INV-125.pdf")
    row = ff._row(a)
    seen = []
    monkeypatch.setattr(ff, "search", lambda d: (seen.append(dict(d)) or {"rows": [row], "dropped": ""}))
    calls = []
    calls.extend(_stub_check(monkeypatch, lambda c: {"picks": [] if len(calls) == 1 else [1], "about": {}, "ask": "",
                                                     "choices": []}))
    holder = _stub_check(monkeypatch, lambda c: {"picks": [] if len(holder) == 1 else [1], "about": {}, "ask": "",
                                                 "choices": []})
    try:
        r = ff.find(_details(after="2026-11-01", before="2026-12-01", date_words=["November"]), "the November one",
                    ["claude"])
        assert r["dropped"] == "date" and [x["file_name"] for x in r["rows"]] == ["INV-125.pdf"]
        assert len(seen) == 2 and seen[1]["after"] == "" and seen[1]["date_words"] == []
    finally:
        _clean_up(d0)


def test_index_search_uses_plain_words(monkeypatch):
    seen = []
    monkeypatch.setattr(search_mod.search_service, "search_files", lambda q, limit=50: (seen.append((q, limit)) or []))
    ff._index_rows(ff.clean({"words": [["hot air balloon", "balloon", "hot-air balloon"]]}), True, True,
                   [str(Path.home())])
    assert seen == [("hot air balloon", 200)], seen


# ---- the deep look --------------------------------------------------------------------------------------------
def test_deep_targets_named_folder_unread_and_kind(monkeypatch):
    d0, (a, b, c) = _files("cat.jpg", "dog.png", "notes.txt")
    monkeypatch.setattr(ff, "find_folder", lambda n: str(d0))
    try:
        r = ff.deep_targets(ff.clean({"folder": str(d0.name), "kind": "image"}))
        names = sorted(os.path.basename(p) for p in r["paths"])
        assert r["named"] and names == ["cat.jpg", "dog.png"], (r, names)
        _INDEX.add_file({"source_path": a, "name": "cat.jpg", "extension": ".jpg", "size": 7, "label": "cat",
                         "tags": ["cat"], "caption": "A cat", "is_file": True})
        r = ff.deep_targets(ff.clean({"folder": str(d0.name), "kind": "image"}))
        assert [os.path.basename(p) for p in r["paths"]] == ["dog.png"], r
    finally:
        _clean_up(d0)


def _indexed(path, caption):
    _INDEX.add_file({"source_path": path, "name": os.path.basename(path), "extension": ".png", "size": 3,
                     "label": "image", "tags": ["x"], "caption": caption, "is_file": True})


def test_after_deep_exact_words_need_no_check_and_unread_never_counts(monkeypatch):
    d0, (a, b, c) = _files("A.png", "B.png", "C.png")
    _indexed(a, "A plain green triangle on white")
    _indexed(b, "A red circle on white")
    calls = _stub_check(monkeypatch, {"picks": [1], "about": {}, "ask": "", "choices": []})
    try:
        r = ff.after_deep(ff.clean({"words": [["green"], ["triangle"]]}), "x", [a, b, c], ["claude"])
        assert [x["file_name"] for x in r["rows"]] == ["A.png"] and not calls and r["rows"][0]["about"].startswith("A plain")
        assert ff.after_deep(ff.clean({"words": [["dog"]]}), "x", [c], ["claude"])["rows"] == []      # c couldn't be read
    finally:
        _clean_up(d0)


def test_deep_targets_missing_folder():
    assert ff.deep_targets(ff.clean({"folder": "No Such Folder Anywhere 123"})).get("missing")


def test_only_pdfs_without_text_count_as_scans(monkeypatch):
    d0, (a, b) = _files("real.pdf", "scan.pdf")
    from app.core import text_extract as tx
    monkeypatch.setattr(tx, "extract_pdf_text", lambda p, max_pages=1: "Invoice number 12 billed to Acme for web hosting "
                        "in March" if "real" in str(p) else "")
    try:
        assert ff._scans([a, b]) == [b]
    finally:
        _clean_up(d0)


def test_find_folder_by_its_name_as_said(monkeypatch):
    d0 = Path(tempfile.mkdtemp(prefix="filect-test-", dir=str(Path.home())))
    try:
        for n in ("Mom's Recipes", "globex-project-docs"):
            (d0 / n).mkdir()
        monkeypatch.setattr(ff, "_known_folders", lambda: {"desktop": str(d0), "downloads": str(d0 / "x"),
                                                           "documents": str(d0 / "y"), "pictures": str(d0 / "z"),
                                                           "videos": str(d0 / "v"), "music": str(d0 / "m")})
        assert ff.find_folder("moms recipes") == str(d0 / "Mom's Recipes")
        assert ff.find_folder("Globex project docs") == str(d0 / "globex-project-docs")
        assert ff.find_folder("desktop") == str(d0)
        assert ff.find_folder(f'"{d0}"') == str(d0), "a pasted path is that folder"
        assert ff.find_folder("nothing like it") == ""
    finally:
        _clean_up(d0)


# ---- the decider ----------------------------------------------------------------------------------------------
def test_decider_find_intents():
    d = ir._parse('{"intent":"FIND","task":"invoices","find":{"words":[["invoice"]]}}', "x", allow_ignore=True)
    assert d["intent"] == "FIND" and d["find"]["words"] == [["invoice"]]
    assert ir._parse('{"intent":"FIND_OPEN","task":"2"}', "x", allow_ignore=True)["intent"] == "IGNORE"     # not on offer
    assert ir._parse('{"intent":"FIND","task":"x"}', "x", allow_ignore=False)["intent"] == "NEW", "Shift+Z: as before"
    st = {"head": "H: ", "text": "t", "allowed": ["FIND_OPEN"]}
    d = ir._parse('{"intent":"FIND_OPEN","task":"2"}', "x", allow_ignore=True, organize=st)
    assert d["intent"] == "FIND_OPEN" and d["task"] == "2"
    p = ir._build_prompt("open it", None, [], live=True, organize=st)
    assert "H: t" in p and "FIND_OPEN:" in p and "Today is" in p
    assert "FILECT IS ORGANIZING" not in p
    assert "FIND:" in ir._LIVE and "find the bug" in ir._LIVE


def test_plain_answers_to_the_search_panel_need_no_ai():
    st = {"head": "H", "stage": "results", "rows": 3, "question": "",
          "allowed": ["FIND_OPEN", "FIND_SHOW", "FIND_ALL", "FIND_NOT", "FIND_MORE", "FIND_CLOSE"]}
    def q(t, s=st):
        d = ir.find_quick(t, s) or {}
        return d.get("intent"), d.get("task")
    for t in ("Close it.", "never mind", "Forget it!", "No.", "No thanks.", "that's all, thanks", "okay close it"):
        assert q(t) == ("FIND_CLOSE", ""), t
    for t, n in (("the second one", "2"), ("open the 2nd one", "2"), ("number three please", "3"), ("the last one", "3"),
                 ("um okay the first one thanks", "1"), ("2", "2"), ("open number two", "2")):
        assert q(t) == ("FIND_OPEN", n), t
    assert q("open it") == ("FIND_OPEN", "")
    assert q("show me where it is") == ("FIND_SHOW", "") and q("show me where the third one is") == ("FIND_SHOW", "3")
    assert q("show it in file explorer") == ("FIND_SHOW", "")
    assert q("show all of them") == ("FIND_ALL", "") and q("show me the rest") == ("FIND_ALL", "")
    assert q("not those") == ("FIND_NOT", "") and q("none of these") == ("FIND_NOT", "")
    for t in ("the Acme one", "November", "open the fifth one", "yes", "Close the second one", "never mind the first one",
              "find my invoice from Acme", "what is this"):
        assert q(t) == (None, None), t
    one = dict(st, rows=1)
    assert q("yes", one) == ("FIND_OPEN", "1") and q("yeah", one) == ("FIND_OPEN", "1"), "'Want me to open it?' — 'yes'"
    deep = dict(st, stage="ask_deep", allowed=["FIND_GO", "FIND_MORE", "FIND_CLOSE"])
    assert q("yes look through them", deep) == ("FIND_GO", "") and q("go ahead", deep) == ("FIND_GO", "")
    assert q("no", deep) == ("FIND_CLOSE", "")
    closest = dict(deep, stage="ask_closest")
    assert q("show me the closest", closest) == ("FIND_GO", "") and q("sure", closest) == ("FIND_GO", "")
    assert q("the second one", dict(st, stage="searching", allowed=["FIND_CLOSE"])) == (None, None)
    assert ir.find_quick("close it", {"stage": "preview", "allowed": ["ORGANIZE_CANCEL"]}) is None, "organize's own"


def test_codex_misses_found_live_are_plain_answers_now():
    """Measured with Codex as the decider (55/66): each of these was read as noise / a chat."""
    names = ["Invoice_Northwind_November.pdf", "Invoice_Acme_October.pdf"]
    res = {"head": "H", "stage": "results", "rows": 2, "names": names, "question": "",
           "allowed": ["FIND_OPEN", "FIND_SHOW", "FIND_ALL", "FIND_NOT", "FIND_MORE", "FIND_CLOSE"]}

    def q(t, s=res):
        d = ir.find_quick(t, s) or {}
        return d.get("intent"), d.get("task")
    assert q("the Northwind one") == ("FIND_OPEN", "1") and q("open the acme invoice") == ("FIND_OPEN", "2")
    assert q("where is the second one saved") == ("FIND_SHOW", "2") and q("where's the first one") == ("FIND_SHOW", "1")
    assert q("the invoice one") == (None, None), "both rows say invoice: not one row"
    assert q("Acme") == (None, None), "a bare name may be more detail: the decider reads it"
    assert q("the Acme one", dict(res, question="Which client?")) == (None, None), "an ANSWER narrows (the Mac's bug)"
    deep = dict(res, stage="ask_deep", names=[], rows=0, allowed=["FIND_GO", "FIND_MORE", "FIND_CLOSE"])
    assert q("no don't bother", deep) == ("FIND_CLOSE", "") and q("nah no need", deep) == ("FIND_CLOSE", "")
    closest = dict(deep, stage="ask_closest")
    assert q("yeah show me", closest) == ("FIND_GO", "") and q("ok show them", closest) == ("FIND_GO", "")
    g = ir.find_guard({"intent": "IGNORE", "index": None, "task": "x"}, "try October instead", closest)
    assert g["intent"] == "FIND_MORE" and g["find"]["after"].endswith("-10-01") and g["find"]["date_words"] == ["October"]
    g = ir.find_guard({"intent": "IGNORE", "index": None, "task": "x"}, "it was in the Pictures folder", deep)
    assert g["intent"] == "FIND_MORE" and g["find"]["folder"].lower() == "pictures"
    same = {"intent": "IGNORE", "index": None, "task": "x"}
    assert ir.find_guard(same, "hmm let me think", closest) is same


def test_a_request_to_find_a_file_read_as_a_chat_becomes_a_search():
    new = {"intent": "NEW", "index": None, "task": "x"}
    g = ir.find_request_guard(new, "find the cloudways invoice")
    assert g["intent"] == "FIND" and g["find"]["words"] == [["cloudways"], ["invoice"]], g
    g = ir.find_request_guard(new, "Can you find my lease agreement from March?")
    assert g["intent"] == "FIND" and g["find"]["date_words"] == ["March"] and ["lease"] in g["find"]["words"]
    g = ir.find_request_guard(new, "open the photo of the dog in my downloads folder")
    assert g["find"]["kind"] == "image" and g["find"]["open"] and g["find"]["folder"] == "downloads" and \
        g["find"]["words"] == [["dog"]], g["find"]
    for t in ("find the bug in this function", "find me a good restaurant", "search the web for flights",
              "find my chat about the landing page", "open the file I was editing in VS Code", "write me a report",
              "what files are in my downloads", "find out why the build fails", "I can't find my keys"):
        assert ir.find_request_guard(new, t) is new, t
    assert ir.find_request_guard({"intent": "RESUME", "index": 1, "task": "x"}, "find my invoice")["intent"] == "RESUME"
    # (measured on Codex, 2 runs of 3: "Look in my Downloads for the tax form" -> NEW)
    for said, folder, words in (("Look in my Downloads for the tax form", "downloads", [["tax"], ["form"]]),
                                ("look through my documents for the lease", "documents", [["lease"]]),
                                ("search my desktop for the invoice from Acme", "desktop", [["invoice"], ["acme"]]),
                                ("can you look in the Clients folder for the contract", "clients", [["contract"]])):
        g = ir.find_request_guard(new, said)
        assert g["intent"] == "FIND" and g["find"]["folder"] == folder and g["find"]["words"] == words, (said, g)
    for t in ("look in my code for the bug", "search my emails for the invoice", "check the spreadsheet for typos",
              "go through this file for mistakes", "look in the fridge for the milk", "search for the meaning of life",
              "look in my downloads and tell me what is taking space", "look for a restaurant near me"):
        assert ir.find_request_guard(new, t) is new, t


def test_the_last_codex_misses():
    d = ff.clean({"kind": "recording", "after": "2026-11-04", "before": "2026-11-05"})
    fixed = ff.fix_day(d, "Can you find the screen recordings I made on November fifth?")
    assert (fixed["after"], fixed["before"]) == ("2026-11-05", "2026-11-06"), "the words name the day: they win"
    far = ff.clean({"kind": "recording", "after": "2025-11-05", "before": "2025-11-06"})
    assert ff.fix_day(far, "my recordings from November fifth last year") == far, "a year off isn't a slip"
    assert ff.fix_day(d, "my recordings from last week") == d
    ign = {"intent": "IGNORE", "index": None, "task": "x"}
    assert ir.find_request_guard(ign, "Hey Filect, find the Globex invoice from November")["intent"] == "FIND"
    assert ir.find_request_guard(ign, "hey Siri find my invoice") is ign
    ask = {"head": "H", "stage": "ask_folder", "question": "Which folder?", "allowed": ["FIND_MORE", "FIND_CLOSE"]}
    g = ir.find_guard({"intent": "FIND", "index": None, "task": "Clients folder", "find": {"words": [["Clients"]]}},
                      "the Clients folder on the desktop", ask)
    assert g["intent"] == "FIND_MORE" and g["find"]["folder"].lower() == "clients"
    new_search = {"intent": "FIND", "index": None, "task": "x", "find": {}}
    assert ir.find_guard(new_search, "find my lease instead", ask) is new_search, "a new 'find…' stays a new search"


def test_details_from_plain_words():
    d = ff.details_from_text("the invoice from Acme")
    assert d["words"] == [["invoice"], ["acme"]] and not d["after"]
    d = ff.details_from_text("my latest pdf from Globex in my Documents")
    assert d["newest"] and d["kind"] == "pdf" and d["folder"].lower() == "documents" and d["words"] == [["globex"]]
    d = ff.details_from_text("invoice 2047")
    assert d["words"] == [["invoice"], ["2047"]]


def test_a_bare_time_from_the_decider_reads_as_a_sentence():
    # (live: "Want me to look through your photos October?" — the decider copied "October" from the answer)
    for said, shown in (("October", "from October"), ("November 5th", "from November 5th"), ("2025", "from 2025"),
                        ("two weeks ago", "from two weeks ago"), ("the summer", "from the summer"),
                        ("from last week", "from last week"), ("last month", "last month"), ("yesterday", "yesterday"),
                        ("in October", "in October"), ("this morning", "this morning"), ("Today", "Today"),
                        ("due in November", "due in November"), ("since Monday", "since Monday"), ("", "")):
        assert ff.clean({"when": said})["when"] == shown, said
    assert ff.clean({"when": "  October  "})["when"] == "from October"
    assert ff.clean({"when": None})["when"] == "" and ff.clean({})["when"] == ""
    d = ff.merge(ff.clean({"words": [["invoice"]], "when": "from last week"}), ff.clean({"when": "October"}))
    assert d["when"] == "from October"
    assert ff.when_phrase(ff.when_phrase("October")) == "from October", "never 'from from October'"


def test_the_place_they_said_fills_a_missing_folder():
    # (measured: Codex sent FIND {'what': 'PDFs', 'kind': 'pdf'} for "find all the PDFs on my desktop" — no folder)
    d = ff.fix_place(ff.clean({"what": "PDFs", "kind": "pdf"}), "find all the PDFs on my desktop")
    assert d["folder"] == "desktop" and d["kind"] == "pdf" and d["words"] == []
    for said, place in (("the invoice in my Downloads", "downloads"), ("find my lease in Documents", "documents"),
                        ("the photo in my desktop folder", "desktop"), ("the song on my music", "music")):
        assert ff.fix_place(ff.clean({"words": [["x"]]}), said)["folder"] == place, said
    d = ff.fix_place(ff.clean({"words": [["desktop"], ["invoice"]]}), "the invoice on the desktop")
    assert d["folder"] == "desktop" and d["words"] == [["invoice"]], "where it is, not a word in the file"
    for said in ("find the photo in the Desktop Wallpapers folder", "find the Documents Archive folder",
                 "the screenshot I took on my desktop computer", "find my desktop background", "find my documents",
                 "find the Downloads Old folder", "find the invoice"):
        assert ff.fix_place(ff.clean({"words": [["x"]]}), said)["folder"] == "", said
    given = ff.clean({"words": [["x"]], "folder": "Clients"})
    assert ff.fix_place(given, "the invoice in my Downloads")["folder"] == "Clients", "never over the decider's folder"
    assert ff.details_from_text("find all the PDFs on my desktop")["words"] == [], "'all' is not a word in the file"


def test_find_guard_keeps_an_answer_from_becoming_a_chat():
    ask = {"head": "H", "stage": "ask_folder", "question": "Which folder?", "allowed": ["FIND_MORE", "FIND_CLOSE"]}
    d = ir.find_guard({"intent": "RESUME", "index": 2, "task": "my downloads"}, "my downloads", ask)
    assert d["intent"] == "FIND_MORE" and d["task"] == "my downloads"
    new = {"intent": "NEW", "index": None, "task": "x"}
    assert ir.find_guard(new, "write me an email to Sam about the launch next week please", ask) is new
    assert ir.find_guard(new, "what is the capital of Peru", ask) is new
    res = {"head": "H", "stage": "results", "question": "", "allowed": ["FIND_MORE"]}
    assert ir.find_guard(new, "Acme", res) is new, "nothing asked: a new request stays one"
    assert ir.find_guard(new, "Acme", dict(res, question="Which client?"))["intent"] == "FIND_MORE"


# ---- the flow -------------------------------------------------------------------------------------------------
class MW(QObject):
    def __init__(self):
        super().__init__()
        self.opened = []

    def open_file_in_os(self, p):
        self.opened.append(p)


_overlay = OrganizeOverlay()
_KEEP = []                       # (the search threads are children of the window: it must outlive them, as the app's does)


def _flow(busy=False):
    mw, said = MW(), []
    _KEEP.append(mw)
    org = SimpleNamespace(overlay=_overlay, busy=lambda: busy, panel_busy=lambda: busy, _say=said.append, _agent_order=lambda: ["claude"])
    c = vfc.VoiceFindController(mw, org)
    return c, mw, said


def _result(rows, **kw):
    return dict({"rows": rows, "all": rows, "ask": "", "choices": [], "dropped": "", "many": False, "checked": True}, **kw)


def _stub_find(monkeypatch, result):
    monkeypatch.setattr(ff, "find", lambda d, heard, agents, can_ask=True: result(d) if callable(result) else result)


@pytest.fixture
def flow():
    made = []

    def make(busy=False):
        c, mw, said = _flow(busy)
        made.append(c)
        return c, mw, said
    yield make
    for c in made:
        c.close()
        _pump(lambda: c._f is None, 1)
        _pump(lambda: not any(t.isRunning() for t in list(c._threads)), 5)
        for sig, slot in ((_overlay.dismissed, c._on_dismissed), (_overlay.result_opened, c._row_clicked),
                          (_overlay.result_revealed, c._folder_clicked), (_overlay.result_chip, c._chip),
                          (_overlay.results_all, c.show_all), (_overlay.confirm_clicked, c._confirm_clicked),
                          (_overlay.text_submitted, c._typed)):
            try:
                sig.disconnect(slot)
            except Exception:
                pass
    _overlay.hide()


def _waiting(c, stage):
    return _pump(lambda: c._f is not None and c._f["stage"] == stage)


def test_flow_one_found_then_open(flow, monkeypatch):
    d0, (a,) = _files("invoice.pdf")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([ff._row(a)]))
    try:
        assert c.start("find my invoice", {"words": [["invoice"]]}) == "find"
        assert _waiting(c, "results")
        assert said[-1].startswith("Found it, in ") and said[-1].endswith("Want me to open it?")
        assert c.overlay._state == "results" and c.overlay._brand.text() == "+ Find"
        assert c.waiting_for_answer() and "FIND_OPEN" in c.decider_state()["allowed"]
        assert mw.opened == []                             # nothing opens by itself
        assert c.open("") == "find_opened" and mw.opened == [a] and c._f is None
        assert c.overlay._brand.text() == "+ Organize"
    finally:
        _clean_up(d0)


def test_flow_said_open_opens_straight_away(flow, monkeypatch):
    d0, (a,) = _files("invoice.pdf")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([ff._row(a)]))
    try:
        c.start("open my invoice", {"words": [["invoice"]], "open": True})
        assert _pump(lambda: mw.opened == [a]) and c._f is None
    finally:
        _clean_up(d0)


def test_flow_said_open_with_several_still_asks(flow, monkeypatch):
    d0, (a, b) = _files("invoice a.pdf", "invoice b.pdf")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([ff._row(a), ff._row(b)]))
    try:
        c.start("open my invoice", {"words": [["invoice"]], "open": True})
        assert _waiting(c, "results") and mw.opened == [] and said[-1] == "Found 2 that fit. Which one do you want?"
    finally:
        _clean_up(d0)


def test_flow_several_pick_by_number(flow, monkeypatch):
    d0, (a, b, e) = _files("invoice a.pdf", "invoice b.pdf", "invoice c.pdf")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([ff._row(a), ff._row(b), ff._row(e)]))
    try:
        c.start("find invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results")
        assert c.open("") == "find_which" and said[-1] == "Which one? Say the number." and mw.opened == []
        assert c.open("9") == "find_which"
        assert c.open("2") == "find_opened" and mw.opened == [b]
    finally:
        _clean_up(d0)


def test_flow_click_a_row_and_the_folder(flow, monkeypatch):
    d0, (a, b) = _files("invoice a.pdf", "invoice b.pdf")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([ff._row(a), ff._row(b)]))
    shown = []
    monkeypatch.setattr(ff, "reveal", shown.append)
    try:
        c.start("find invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results")
        c.overlay.result_rows[1].folder.click()
        assert shown == [b] and c._f is None
        c.start("find invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results")
        c.overlay.result_opened.emit(0)
        assert mw.opened == [a] and c._f is None
    finally:
        _clean_up(d0)


def test_flow_many_shows_and_asks_at_once_and_only_once(flow, monkeypatch):
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(5)])
    rows = [ff._row(p) for p in paths]
    c, mw, said = flow()
    seen = []

    def res(d):
        seen.append(d)
        if len(d["words"]) > 1:
            return _result(rows[:3], many=False)
        return _result(rows[:3], all=rows, many=True, ask="Which client or month?", choices=["Acme", "November"])
    _stub_find(monkeypatch, res)
    try:
        c.start("find my invoice", {"what": "invoices", "words": [["invoice"]]})
        assert _waiting(c, "results")
        assert said[-1] == "You've got a lot of invoices. Here are the newest. Which client or month?"
        assert len(c.overlay.result_rows) == 3 and set(c.overlay.result_chips) == {"Acme", "November"}
        assert c._f["asked"] is True and c.overlay.result_all_btn is not None and c.overlay.result_field is not None
        c.overlay.result_chips["Acme"].click()
        assert _pump(lambda: len(seen) == 2) and _waiting(c, "results") and seen[1]["words"][1] == ["Acme"]
    finally:
        _clean_up(d0)


def test_flow_month_chip_is_a_date(flow, monkeypatch):
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(3)])
    rows = [ff._row(p) for p in paths]
    c, mw, said = flow()
    seen = []
    _stub_find(monkeypatch, lambda d: (seen.append(d) or _result(rows)))
    try:
        c.start("find my invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results")
        c._chip("November")
        assert _pump(lambda: len(seen) == 2) and seen[1]["after"].endswith("-11-01") and seen[1]["date_words"] == ["November"]
        assert seen[1]["when"] == "from November"
    finally:
        _clean_up(d0)


def test_flow_not_those_drops_the_date_then_moves_on(flow, monkeypatch):
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(5)])
    rows = [ff._row(p) for p in paths]
    c, mw, said = flow()
    seen = []
    _stub_find(monkeypatch, lambda d: (seen.append(d) or _result(rows[:2], all=rows)))
    try:
        c.start("invoice from November", {"words": [["invoice"]], "after": "2026-11-01", "date_words": ["November"]})
        assert _waiting(c, "results") and len(seen) == 1
        assert c.not_those()
        assert _pump(lambda: len(seen) == 2) and seen[1]["after"] == "" and seen[1]["date_words"] == []
        assert _waiting(c, "results")
        got = [r["file_name"] for r in c._f["rows"]]
        assert got == ["invoice 2.pdf", "invoice 3.pdf", "invoice 4.pdf"], got      # what you turned down doesn't come back
        assert c.not_those() and said[-1] == "I couldn't find it by name or text. Which folder should I look in?"
    finally:
        _clean_up(d0)


def test_flow_more_detail_reaches_the_check(flow, monkeypatch):
    d0, (a,) = _files("invoice.pdf")
    c, mw, said = flow()
    heard = []
    monkeypatch.setattr(ff, "find", lambda d, h, agents, can_ask=True: (heard.append(h) or _result([ff._row(a)])))
    try:
        c.start("give me the runable invoice", {"words": [["invoice"], ["Runable"]]})
        assert _waiting(c, "results")
        assert c.more("the one created on October 2nd", {"after": "2026-10-02", "before": "2026-10-03"})
        assert _pump(lambda: len(heard) == 2) and "October 2nd" in heard[1], heard
        assert _waiting(c, "results") and "It asked" not in c.decider_state()["text"]
        c._f["question"] = "Which client?"
        assert "FIND_MORE" in c.decider_state()["text"]
    finally:
        _clean_up(d0)


def test_flow_typed_answers_are_read_like_spoken_ones(flow, monkeypatch):
    """Windows: a plain answer typed into the panel acts at once (no AI); anything else goes through the decider."""
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(5)])
    rows = [ff._row(p) for p in paths]
    c, mw, said = flow()
    typed = []
    mw.assistant_page = SimpleNamespace(run_voice_task=lambda t, source="": typed.append((t, source)))
    _stub_find(monkeypatch, _result([]))
    try:
        c.start("find it", {})
        assert c._f["stage"] == "ask_what" and c.overlay.choice_field is not None and c.overlay._state == "choice"
        c.overlay.choice_field.setText("the Cloudways invoice")
        c.overlay.choice_field.returnPressed.emit()
        assert typed == [("the Cloudways invoice", "live")]
        c.start("find the thing", {"words": [["thing"]]})
        assert _waiting(c, "ask_folder") and c.overlay.choice_field is not None
        c.close()
        monkeypatch.setattr(ff, "find", lambda d, h, a, can_ask=True: _result(rows[:3], all=rows, many=True,
                                                                             ask="Which client?", choices=["Acme"]))
        c.start("find my invoice", {"what": "invoices", "words": [["invoice"]]})
        assert _waiting(c, "results") and c.overlay.result_field is not None
        c.overlay.result_field.setText("the second one")
        c.overlay.result_field.returnPressed.emit()
        assert mw.opened == [rows[1]["file_path"]] and len(typed) == 1, "typed '2nd': opened at once, no decider"
        monkeypatch.setattr(ff, "find", lambda d, h, a, can_ask=True: _result(rows[:2]))
        c.start("find my invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results") and c.overlay.result_field is None          # (nothing asked: just pick one)
        c._typed("close it")
        assert c._f is None and len(typed) == 1
    finally:
        _clean_up(d0)


def test_flow_a_document_that_is_not_there_reads_nothing(flow, monkeypatch):
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([]))
    reads = _stub_deep(monkeypatch, {"paths": [], "named": False, "scans": True}, lambda d, h, p, ag: _result([]))
    c.start("find the mom and AI invoice", {"what": "invoices", "words": [["invoice"], ["mom"]], "kind": "pdf"})
    assert _waiting(c, "gone") and reads == [] and said[-1] == "I couldn't find that."
    assert c.overlay._state == "error" and not c.overlay._error_close_btn.isVisible(), "just the message (design 18)"


def test_flow_scanned_pdfs_are_offered(flow, monkeypatch):
    d0, (a,) = _files("scan.pdf")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([]))
    reads = _stub_deep(monkeypatch, {"paths": [a], "named": False, "scans": True}, lambda d, h, p, ag: _result([ff._row(a)]))
    try:
        c.start("find my tax letter", {"what": "letters", "words": [["tax"]], "kind": "pdf"})
        assert _waiting(c, "ask_deep") and reads == [] and said[-1] == ("I couldn't find it. Want me to look through your "
                                                                      "scanned PDFs?")
        assert c.overlay._state == "confirm" and c.overlay._confirm_btn.text() == "Look through them"
        assert c.deep_go() and _waiting(c, "results") and reads == [[a]]
    finally:
        _clean_up(d0)


def test_flow_nothing_from_that_month_asks_before_showing_the_closest(flow, monkeypatch):
    """Design 09-11 (newer than the Mac's voice-find-a-file.md): say so and ask; only show them after a yes."""
    d0, (a, b) = _files("Invoice_Northwind.pdf", "Invoice_Acme.pdf")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([ff._row(a), ff._row(b)], dropped="date"))
    try:
        c.start("find my invoice from November", {"words": [["invoice"]], "after": "2026-11-01", "when": "from November"})
        assert _waiting(c, "ask_closest") and said[-1] == "I couldn't find one from November. Want me to show the closest?"
        assert c.overlay._state == "confirm" and c.overlay._confirm_btn.text() == "Show the closest"
        assert "FIND_GO" in c.decider_state()["allowed"] and c.overlay.result_rows == [] or c.overlay._state == "confirm"
        c.overlay._confirm_btn.click()
        assert _waiting(c, "results") and said[-1] == "Here are the closest. Which one do you want?"
        assert len(c.overlay.result_rows) == 2
        c.close()
        _stub_find(monkeypatch, _result([ff._row(a)], dropped="date"))
        c.start("open my invoice from March", {"words": [["invoice"]], "after": "2026-03-01", "open": True})
        assert _waiting(c, "ask_closest") and mw.opened == [], "'open' doesn't open another month's file by itself"
        assert c.deep_go() and said[-1].startswith("Here's the closest, in ") and said[-1].endswith("Want me to open it?")
    finally:
        _clean_up(d0)


def test_flow_never_says_found_3_when_there_are_more(flow, monkeypatch):
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(5)])
    rows = [ff._row(p) for p in paths]
    c, mw, said = flow()
    _stub_find(monkeypatch, _result(rows[:3], all=rows, fit=5))
    try:
        c.start("find invoice", {"words": [["invoice"], ["Acme"]]})
        assert _waiting(c, "results") and said[-1] == "Found several. Here are the top 3. Which one do you want?"
        c.close()
        _stub_find(monkeypatch, _result(rows[:3], all=rows, exact=11))
        c.start("my recordings from november fifth", {"what": "recordings", "kind": "recording", "after": "2026-11-05",
                                                      "when": "from November fifth"})
        assert _waiting(c, "results") and said[-1] == ("You've got 11 recordings from November fifth. Here are the newest. "
                                                       "Which one do you want?")
        c.close()
        _stub_find(monkeypatch, _result(rows[:3], all=rows, checked=False))     # (unchecked: never "that fit")
        c.start("find invoice", {"words": [["invoice"], ["Acme"]]})
        assert _waiting(c, "results") and said[-1] == "Found several. Here are the top 3. Which one do you want?"
    finally:
        _clean_up(d0)


def test_flow_show_all(flow, monkeypatch):
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(5)])
    rows = [ff._row(p) for p in paths]
    c, mw, said = flow()
    _stub_find(monkeypatch, _result(rows[:3], all=rows))
    try:
        c.start("find invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results") and len(c.overlay.result_rows) == 3
        assert c.show_all() and len(c.overlay.result_rows) == 5 and c._f["stage"] == "results" and mw.opened == []
        assert c.overlay.result_list is None and not c._tuck.isActive() and said[-1].startswith("Here's everything")
        assert not c.show_all()                            # (it's already showing them)
        assert c.open("5") == "find_opened" and mw.opened == [paths[-1]]
    finally:
        _clean_up(d0)


def test_flow_show_all_long_list_scrolls(flow, monkeypatch):
    d0, paths = _files(*[f"invoice {i}.pdf" for i in range(25)])
    rows = [ff._row(p) for p in paths]
    c, mw, said = flow()
    _stub_find(monkeypatch, _result(rows[:3], all=rows))
    try:
        c.start("find invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results") and c.show_all()
        ov = c.overlay
        assert ov.result_list is not None and len(ov.result_rows) == 25 and ov.result_list.height() == 362
        assert len(c.decider_state()["text"].split(";")) == 25
        assert c.open("17") == "find_opened" and mw.opened == [paths[16]]
    finally:
        _clean_up(d0)


def test_flow_nothing_to_look_for_asks_what(flow):
    c, mw, said = flow()
    c.start("find it", {})
    assert c._f["stage"] == "ask_what" and said[-1] == "What should I look for?"
    assert c.decider_state()["allowed"] == ["FIND_MORE", "FIND_CLOSE"]


def _stub_deep(monkeypatch, paths, read):
    monkeypatch.setattr(ff, "deep_targets", lambda d: paths(d) if callable(paths) else paths)
    reads = []

    def fake_read(p, agents, progress=None, cancelled=None):
        reads.append(list(p))
        return {"read": len(p), "by": {}, "cancelled": False}
    monkeypatch.setattr(reader, "read_files", fake_read)
    monkeypatch.setattr(ff, "after_deep", read)
    return reads


def test_flow_nothing_found_named_folder_reads_straight_away(flow, monkeypatch):
    d0, (a,) = _files("beach.jpg")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([]))
    reads = _stub_deep(monkeypatch, {"paths": [a], "named": True}, lambda d, h, p, ag: _result([ff._row(a)]))
    try:
        c.start("find the beach photo in my Pictures", {"words": [["beach"]], "folder": "Pictures"})
        assert _waiting(c, "results") and reads == [[a]]
    finally:
        _clean_up(d0)


def test_flow_deep_look_asks_first_when_no_folder_named(flow, monkeypatch):
    d0, (a,) = _files("beach.jpg")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([]))
    reads = _stub_deep(monkeypatch, {"paths": [a], "named": False}, lambda d, h, p, ag: _result([ff._row(a)]))
    try:
        c.start("find my beach photo from last week", {"what": "photos", "words": [["beach"]], "kind": "image",
                                                       "when": "from last week"})
        assert _waiting(c, "ask_deep") and reads == []
        assert said[-1] == "I couldn't find it. Want me to look through your photos from last week?"
        assert "FIND_GO" in c.decider_state()["allowed"]
        assert c.deep_go() and _waiting(c, "results") and reads == [[a]]
    finally:
        _clean_up(d0)


def test_flow_deep_look_over_the_cap_asks_then_reads_only_the_cap(flow, monkeypatch):
    paths = [rf"C:\x\{i}.jpg" for i in range(ff.DEEP_CAP + 40)]
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([]))
    reads = _stub_deep(monkeypatch, {"paths": paths, "named": True}, lambda d, h, p, ag: _result([]))
    c.start("find the beach photo in Pictures", {"what": "photos", "words": [["beach"]], "folder": "Pictures"})
    assert _waiting(c, "ask_narrow") and said[-1] == "I'd have to read 140 photos. Which month?" and reads == []
    c.more("", {"after": "2026-10-01", "before": "2026-11-01"})
    assert _pump(lambda: reads and c._f["stage"] == "gone") and len(reads[0]) == ff.DEEP_CAP
    assert said[-1] == "I looked through them and couldn't find it."


def test_flow_a_month_typed_alone_is_a_time_and_a_folder_answer_is_a_folder(flow, monkeypatch):
    c, mw, said = flow()
    seen = []
    _stub_find(monkeypatch, lambda d: (seen.append(d) or _result([])))
    _stub_deep(monkeypatch, {"paths": [], "named": False}, lambda d, h, p, ag: _result([]))
    c.start("find the thing", {"words": [["thing"]]})
    assert _waiting(c, "ask_folder")
    assert c.more("my Downloads")
    assert _pump(lambda: len(seen) == 2) and seen[1]["folder"] == "my Downloads", seen[1]
    c.close()
    c.start("find the beach photo", {"words": [["beach"]], "folder": "Pictures"})
    _pump(lambda: len(seen) == 3)
    c._f["stage"] = "ask_narrow"
    assert c.more("october")
    assert _pump(lambda: len(seen) == 4) and seen[3]["after"].endswith("-10-01") and seen[3]["date_words"] == ["October"]


def test_flow_nothing_and_nowhere_to_look_asks_once(flow, monkeypatch):
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([]))
    _stub_deep(monkeypatch, {"paths": [], "named": False, "missing": True}, lambda d, h, p, ag: _result([]))
    c.start("find the thing", {"words": [["thing"]]})
    assert _waiting(c, "ask_folder")
    c.more("not sure")
    assert _waiting(c, "gone") and said[-1] == "I couldn't find a folder called not sure."


def test_flow_busy_and_close_and_stale(flow, monkeypatch):
    c, mw, said = flow(busy=True)
    assert c.start("find x", {"words": [["x"]]}) == "organize_busy" and c._f is None
    d0, (a,) = _files("invoice.pdf")
    c, mw, said = flow()
    hold = []

    def slow(d):
        time.sleep(0.2)
        hold.append(1)
        return _result([ff._row(a)])
    _stub_find(monkeypatch, slow)
    try:
        c.start("find invoice", {"words": [["invoice"]]})
        assert c.decider_state()["allowed"] == ["FIND_CLOSE"]
        assert c.close() and c._f is None
        assert _pump(lambda: hold) and _pump(lambda: False, 0.1) is False
        assert c._f is None and c.overlay._state != "results"          # the late answer can't reopen it
    finally:
        _clean_up(d0)


def test_flow_organize_takes_the_panel(flow, monkeypatch):
    d0, (a,) = _files("invoice.pdf")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([ff._row(a)]))
    try:
        c.start("find invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results")
        c.stop()
        assert c._f is None and not c.waiting_for_answer() and c.decider_state() is None
        assert c.overlay._brand.text() == "+ Organize"
    finally:
        _clean_up(d0)


def test_flow_ignored_tucks_away_then_answers_until_it_expires(flow, monkeypatch):
    d0, (a, b) = _files("invoice a.pdf", "invoice b.pdf")
    c, mw, said = flow()
    _stub_find(monkeypatch, _result([ff._row(a), ff._row(b)]))
    try:
        c.start("find invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results") and c._tuck.isActive() and c._expire.isActive()
        c._tuck_away()
        assert c.overlay._minimized and c.waiting_for_answer(), "tucked, still answering"
        assert c.overlay._tab is not None and c.overlay._tab._text.text() == "+ Find"
        assert c.open("2") == "find_opened" and mw.opened == [b]
        assert c.overlay._tab._text.text() == "+ Organize"
    finally:
        _clean_up(d0)


# ---- the panel ------------------------------------------------------------------------------------------------
def test_panel_rows_fit_and_names_are_plain_text():
    ov = OrganizeOverlay()
    rows = [{"name": "<b>weird</b> " + "x" * 90 + ".pdf", "sub": "Downloads, Oct 29. " + "y" * 90, "ext": "PDF"}
            for _ in range(3)]
    msg = "You've got a lot of invoices. Here are the newest. Which client or month?"
    ov.show_results(msg, rows, ["Acme Corporation", "Northwind Trading", "November 2026"], show_all=True,
                    field="Or type your answer and press Enter")
    assert all(lab.textFormat().name == "PlainText" for r in ov.result_rows for lab in (r.name, r.sub))
    assert all(r.name.fontMetrics().horizontalAdvance(r.name.text()) <= r._text_w + 1 for r in ov.result_rows)
    assert all(r.name.text().endswith(".pdf"), ) if False else all("…" in r.name.text() for r in ov.result_rows)
    used = sum(b.sizeHint().width() + 8 for b in ov.result_chips.values()) + ov.result_all_btn.sizeHint().width()
    assert used <= _INNER_W, used
    assert isinstance(ov.result_rows[0].thumb, QLabel) and ov.result_rows[0].thumb.text() == "PDF"
    ov.show_results("Here's everything I found.", rows * 9, scroll=True, show_all=False)
    assert ov.result_list is not None and ov.result_list.height() == 362
    assert all(r._text_w == ov.result_rows[0]._text_w for r in ov.result_rows[:9]), "same width, rows 1-9"
    assert ov.result_rows[12]._text_w == ov.result_rows[0]._text_w - 12, "a two-digit number takes its room"
    ov.hide()


def test_assistant_wiring():
    calls = []
    fake = SimpleNamespace(start=lambda h, d: calls.append(("start", h, d)) or "find",
                           open=lambda t: calls.append(("open", t)) or "find_opened",
                           reveal=lambda t: calls.append(("reveal", t)) or "find_shown",
                           show_all=lambda: True, not_those=lambda: True, more=lambda t, d: True, deep_go=lambda: True,
                           close=lambda: True)
    act = ap.AssistantPage._act_on_find
    assert act(fake, "FIND", "invoices", "find my invoice", {"words": []}) == "find"
    assert act(fake, "FIND_OPEN", "2", "x", {}) == "find_opened" and act(fake, "FIND_SHOW", "1", "x", {}) == "find_shown"
    assert act(fake, "FIND_ALL", "", "x", {}) == "find_all" and act(fake, "FIND_NOT", "", "x", {}) == "find_more"
    assert act(fake, "FIND_MORE", "", "x", {}) == "find_more" and act(fake, "FIND_GO", "", "x", {}) == "find"
    assert act(fake, "FIND_CLOSE", "", "x", {}) == "find_closed"
    assert calls[0] == ("start", "find my invoice", {"words": []})
    page = SimpleNamespace(_focused=None, _voice_target=lambda: None,
                           _find_state=lambda: {"head": "H", "text": "t", "allowed": []})
    assert ap.AssistantPage._organize_state(page, object())["head"] == "H"
    assert ap.AssistantPage._organize_state(page, None) is None


# ---- 2026-11-10: what an independent review of this code found (each test is its exact scenario) ------------------
def test_a_row_number_always_beats_a_word_in_a_file_name():
    st = {"head": "H", "stage": "results", "question": "", "rows": 3,
          "allowed": ["FIND_OPEN", "FIND_SHOW", "FIND_ALL", "FIND_NOT", "FIND_CLOSE", "FIND_MORE"],
          "names": ["Acme second notice.pdf", "Acme invoice.pdf", "Q3 first draft.docx"]}
    for said, row in (("open the second one", "2"), ("the second one", "2"), ("the first one", "1"),
                      ("open the first one", "1"), ("open the last one", "3"), ("the 3rd one", "3"),
                      ("the top one", "1"), ("the second invoice", "2"), ("open the first file", "1"),
                      ("number two", "2"), ("the draft one", "3")):
        d = ir.find_quick(said, st)
        assert d is not None and d["intent"] == "FIND_OPEN" and d["task"] == row, (said, d)
    for said in ("open the acme invoice",            # (two rows have "acme": the decider asks which)
                 "open the two invoices", "the other one", "the second notice one please now"):
        d = ir.find_quick(said, st)
        assert d is None or d["task"] not in ("1", "3"), (said, d)


def test_flow_a_late_picture_never_lands_on_the_next_list(flow, monkeypatch):
    d0, files = _files("a.jpg", "b.jpg", "c.jpg", "d.jpg")
    c, mw, said = flow()
    monkeypatch.setattr(c, "_thumbs", lambda paths: None)      # (the test hands the pictures in itself)
    painted = []
    monkeypatch.setattr(c.overlay, "set_result_thumb", lambda i, p: painted.append((i, p)))
    rows = [ff._row(p) for p in files]
    _stub_find(monkeypatch, _result(rows[:2], all=rows))
    try:
        c.start("find my photos", {"kind": "image"})
        assert _waiting(c, "results")
        first = c._shown
        assert c.not_those() and [r["file_path"] for r in c._f["rows"]] == files[2:]
        c._on_thumb(0, "a-thumb.png", first)                   # (the first list's picture, arriving late)
        assert painted == [], "an old picture must not be painted on the new list"
        c._on_thumb(0, "c-thumb.png", c._shown)
        assert painted == [(0, "c-thumb.png")]
    finally:
        _clean_up(d0)


def test_flow_show_all_works_again_on_a_new_list(flow, monkeypatch):
    d0, files = _files(*[f"invoice {i}.pdf" for i in range(9)])
    c, mw, said = flow()
    rows = [ff._row(p) for p in files]
    _stub_find(monkeypatch, _result(rows[:3], all=rows))
    try:
        c.start("find invoice", {"words": [["invoice"]]})
        assert _waiting(c, "results")
        assert c.show_all() and len(c._f["rows"]) == 9
        assert not c.show_all(), "already showing everything"
        assert c.more("acme", {"words": [["acme"]]})            # (more detail: a new list)
        assert _pump(lambda: c._f["stage"] == "results" and len(c._f["rows"]) == 3)
        assert c.show_all() and len(c._f["rows"]) == 9, "Show all works on the new list too"
    finally:
        _clean_up(d0)


def test_flow_said_open_never_opens_a_file_they_have_not_seen(flow, monkeypatch):
    d0, (a, b, e) = _files("invoice a.pdf", "invoice b.pdf", "resume.docx")
    c, mw, said = flow()
    try:
        # the type they said isn't there: say so and show it ("open my resume PDF" mustn't open resume.docx unseen)
        _stub_find(monkeypatch, _result([ff._row(e)], dropped="type"))
        c.start("open my resume PDF", {"words": [["resume"]], "kind": "pdf", "open": True})
        assert _waiting(c, "results") and mw.opened == [] and said[-1].startswith("Nothing of that type.")
        c.close()
        assert _pump(lambda: c._f is None)
        # several shown, "not those", one other comes back: shown, never opened straight away
        results = iter([_result([ff._row(a), ff._row(b)]), _result([ff._row(e)])])
        monkeypatch.setattr(ff, "find", lambda d, heard, agents, can_ask=True: next(results))
        c.start("open the invoice from October", {"words": [["invoice"]], "after": "2026-10-01",
                                                   "before": "2026-11-01", "open": True})
        assert _waiting(c, "results") and mw.opened == []
        assert c.not_those()
        assert _pump(lambda: c._f is not None and c._f["stage"] == "results" and
                     [r["file_path"] for r in c._f["rows"]] == [e])
        assert mw.opened == [] and said[-1].endswith("Want me to open it?")
    finally:
        _clean_up(d0)


def test_flow_a_question_on_the_results_never_tucks_away_mid_answer(flow, monkeypatch):
    d0, files = _files(*[f"invoice {i}.pdf" for i in range(4)])
    c, mw, said = flow()
    rows = [ff._row(p) for p in files]
    _stub_find(monkeypatch, _result(rows[:3], all=rows, many=True, ask="Which client?", choices=["Acme"]))
    try:
        c.start("find my invoices", {"words": [["invoice"]]})
        assert _waiting(c, "results") and c._f["question"] == "Which client?"
        assert not c._tuck.isActive(), "it is asking (and may be being typed into): it stays"
        assert c._expire.isActive()
    finally:
        _clean_up(d0)


def test_a_file_dated_before_1970_or_far_ahead_never_breaks_a_search():
    for ts in (-86400 * 365 * 10, -11644473600, 1e20, float("nan"), None):
        t = ff.local_time(ts)
        assert 1600 <= t.tm_year <= 9999, ts
        time.strftime("%b %d %Y", t)                            # (what the check prompt does with it)
    row = {"file_name": "old.txt", "folder": "Old Stuff", "about": "", "mtime": -315619200.0, "file_path": "C:/x"}
    assert vfc.VoiceFindController._sub(row).startswith("Old Stuff, ")
    prompt = ff._check_prompt("find the old notes", [row], [""], False, ff.clean({"words": [["notes"]]}))
    assert "old.txt" in str(prompt)


def test_fix_day_never_narrows_a_longer_time_and_reads_a_time_kept_as_a_word():
    from datetime import datetime as _dt
    today = _dt(2026, 11, 10, 15, 0)
    d = ff.clean({"after": "2026-11-06", "words": [["screenshot"]]})
    assert ff.fix_day(d, "screenshots I took after November 5th", today) == d, "after the 5th is not ON the 5th"
    d = ff.clean({"after": "2026-11-03", "before": "2026-11-10"})
    assert ff.fix_day(d, "the week of November 5th", today) == d
    d = ff.clean({"after": "2026-11-04", "before": "2026-11-06"})
    assert ff.fix_day(d, "around November 5th", today) == d
    d = ff.clean({"after": "2026-11-05", "before": "2026-11-05"})                       # (an empty range)
    out = ff.fix_day(d, "the recording from November 5th", today)
    assert (out["after"], out["before"]) == ("2026-11-05", "2026-11-06")
    d = ff.clean({"after": "2026-11-04", "before": "2026-11-05"})                       # (Codex's time-zone slip)
    out = ff.fix_day(d, "my screen recordings from November fifth", today)
    assert (out["after"], out["before"]) == ("2026-11-05", "2026-11-06")
    d = ff.clean({"after": "2025-11-05", "before": "2025-11-06"})
    assert ff.fix_day(d, "November fifth last year", today) == d
    # (measured on Codex: "…on November fifth" came back padded, the 4th to the 6th)
    d = ff.clean({"after": "2026-11-04", "before": "2026-11-06", "date_words": ["November 5"]})
    out = ff.fix_day(d, "Can you find the screen recordings I made on November fifth?", today)
    assert (out["after"], out["before"]) == ("2026-11-05", "2026-11-06")
    out = ff.fix_day(d, "the photo I sent to Sam on November fifth", today)
    assert (out["after"], out["before"]) == ("2026-11-05", "2026-11-06")
    for said in ("around November 5th", "the screenshots from about November fifth", "the week of November 5th",
                 "sometime near November 5th", "photos from early November"):
        assert ff.fix_day(d, said, today) == d, said
    d = ff.clean({"after": "2026-11-01", "before": "2026-11-10"})
    assert ff.fix_day(d, "the recordings from November fifth", today) == d, "a much longer time: the decider's"
    # (measured on Codex: "Open the photo I took yesterday" came back as words [['yesterday']] and no dates)
    d = ff.clean({"what": "photos", "words": [["yesterday"]], "kind": "image", "open": True})
    out = ff.fix_day(d, "Open the photo I took yesterday", today)
    assert out["words"] == [] and (out["after"], out["before"]) == ("2026-11-09", "2026-11-10")
    assert out["when"] == "yesterday" and out["open"] and out["kind"] == "image"
    out = ff.fix_day(ff.clean({"words": [["November"], ["report"]]}), "find my November report", today)
    assert out["words"] == [["report"]] and (out["after"], out["before"]) == ("2026-11-01", "2026-12-01")
    d = ff.clean({"words": [["acme"]]})
    assert ff.fix_day(d, "the acme invoice", today) == d
    t = ff.details_from_text("the photo I took today", today)
    assert (t["after"], t["before"], t["when"]) == ("2026-11-10", "2026-11-11", "today") and t["words"] == []


def test_odd_details_from_the_decider_are_still_a_sensible_search(flow, monkeypatch):
    assert ff.clean({"words": "Acme"})["words"] == [["Acme"]], "one word, not the letters A, c, m, e"
    assert ff.clean({"words": 5})["words"] == [] and ff.clean({"words": {"a": 1}})["words"] == []
    assert ff.clean({"date_words": "November"})["date_words"] == ["November"]
    seen = []
    _stub_find(monkeypatch, lambda d: seen.append(d) or _result([]))
    monkeypatch.setattr(ff, "deep_targets", lambda d: {"paths": [], "named": False})
    c, mw, said = flow()
    c.start("find the Zorblax invoice", {})                     # (FIND, with nothing to look for)
    assert _pump(lambda: bool(seen))
    assert seen[0]["words"] == [["zorblax"], ["invoice"]], seen[0]
    c.close()
    assert _pump(lambda: c._f is None)
    c.start("find something", {"what": "files"})
    assert c._f["stage"] == "ask_what", "nothing to go on: it asks"


def test_a_display_path_that_isnt_on_disk_falls_back_to_the_item_url():
    d0, (a,) = _files("café résumé.txt")
    try:
        url = "file:" + Path(a).as_posix()
        assert ff._real_path(a, url) == a, "the display path, when it's real"
        assert os.path.normcase(ff._real_path(r"C:\Benutzer\nope\café résumé.txt", url)) == os.path.normcase(a)
        assert ff._real_path(r"C:\nope\x.txt", "mapi:whatever") == r"C:\nope\x.txt", "(then the row is dropped later)"
        assert ff._real_path(None, None) == ""
    finally:
        _clean_up(d0)


def test_a_folder_too_big_to_read_is_narrowed_by_type_and_month(monkeypatch):
    d0, files = _files("a.jpg", "b.pdf", "sub/c.jpg", "sub/d.jpg", ".hidden/e.jpg", "f.jpg")
    oct_ = 1_791_000_000                                          # (2026-10-02)
    for p, ts in zip(files, (oct_, oct_, oct_ + 86400, oct_ + 40 * 86400, oct_, oct_ - 40 * 86400)):
        os.utime(p, (ts, ts))
    monkeypatch.setattr(reader, "scan", lambda folder, **k: {"error": "too_many"})
    monkeypatch.setattr(ff, "find_folder", lambda name: str(d0))
    monkeypatch.setattr(ff, "_unread", lambda paths: list(paths))
    try:
        r = ff.deep_targets(ff.clean({"folder": "Big", "words": [["balloon"]]}))
        assert r.get("too_many") and r["named"], "nothing to narrow by: still too many"
        r = ff.deep_targets(ff.clean({"folder": "Big", "kind": "image", "after": "2026-10-01", "before": "2026-11-01"}))
        assert sorted(os.path.basename(p) for p in r["paths"]) == ["a.jpg", "c.jpg"], r
        assert r["named"] and not r.get("too_many")
        r = ff.deep_targets(ff.clean({"folder": "Big", "kind": "pdf"}))
        assert [os.path.basename(p) for p in r["paths"]] == ["b.pdf"]
    finally:
        _clean_up(d0)
