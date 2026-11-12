"""Organize by voice and auto-organize by voice: the flow (Windows port of the Mac tests/test_voice_organize.py, the
controller / Agents page / Assistant tab half; the core, watcher and panel halves are in test_organize_core.py,
test_auto_watcher_voice.py and test_organize_overlay_pages.py).

No real agents or backend; real files only in temp folders; settings.json, the real index and the real History are
never touched (a throwaway index and moves folder).

Windows differences from the Mac tests:
  * the controller is VoiceOrganizeController(organize_page, main_window=...);
  * the hotkey is Shift+letter, not fn + Option, and its plan request is (folder, how, False, None, True): the
    page's own 300-file limit, and how_only (only how to sort goes to the planner);
  * a plan with nothing to move arrives as a plan (move_count 0), not as an error;
  * the agent picker test is in test_live_page.py (Windows built its own).
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.core import apply as apply_mod, database, intent_router as ir, organize_reader as r  # noqa: E402
from app.core import ai_organizer  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402
from app.ui import voice_organize_controller as voc  # noqa: E402
import test_live_page as tp  # noqa: E402
from test_live_page import _no_side_effects  # noqa: E402,F401  (autouse: no settings / sounds / analytics)

_INDEX = database.FileIndex(Path(tempfile.mkdtemp(prefix="filect-index-")) / "i.db")
_REAL_HOW = ai_organizer.organize_how
_REAL_ASK = r._ask


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    """A throwaway index and History; the AI's folder pick and "how" are stubbed per test."""
    monkeypatch.setattr(database, "file_index", _INDEX)
    moves = tmp_path / "moves"
    moves.mkdir()
    monkeypatch.setattr(settings, "get_moves_dir", lambda: moves)
    monkeypatch.setattr(settings, "auto_organize_folders", [])
    monkeypatch.setattr(settings, "live_organize_preview", True)
    monkeypatch.setattr(settings, "assistant_auto_open", True)
    monkeypatch.setattr(ai_organizer, "resolve_folder_with_ai", lambda text, cands: cands[0])
    monkeypatch.setattr(ai_organizer, "organize_how", lambda sentence, folder: "")
    monkeypatch.setattr(ap, "_track", lambda *a, **k: None)
    monkeypatch.setattr(r, "_ask", _REAL_ASK)
    yield
    for c in list(_CTRLS):
        _pump(lambda: all(not t.isRunning() for t in c._hf_threads), 5)
        try:
            c.overlay.hide()
        except RuntimeError:
            pass
    _CTRLS.clear()


_CTRLS = []


def _pump(cond, timeout=10.0):
    t = time.time()
    while time.time() - t < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return bool(cond())


def _tmp():
    return Path(tempfile.mkdtemp(prefix="filect-test-"))


def _ok(items):
    return json.dumps({"files": [{"id": i + 1, "type": "note", "caption": "c", "tags": ["t"]} for i in range(len(items))]})


def _notes(d, n):
    out = []
    for i in range(n):
        p = d / f"n{i}.txt"
        p.write_text(f"note {i} in {d.name}")          # (unique content: no two test files alike)
        out.append(str(p))
    return out


class FakePage(QObject):
    voice_plan_ready = Signal(dict)
    voice_plan_error = Signal(str)
    voice_apply_done = Signal(str)
    voice_apply_error = Signal(str)
    voice_status = Signal(str)
    destination_path = None
    auto_watcher = None

    def __init__(self):
        super().__init__()
        self.generated, self.refined, self.applied = [], [], 0
        self.last_voice_log = ""
        self._voice_plan_owned = False
        self.moves = []
        self.working = False
        self.current_plan = {"folders": {}}

    def has_plan(self):
        return bool(self.moves)

    def voice_busy(self):
        return self.working

    def plan(self):
        self.voice_plan_ready.emit({"summary": "2 files in 1 folder", "folders": {"Notes": [("a", True), ("b", True)]},
                                    "target_folder": "C:/t", "file_count": 2, "move_count": 2, "folder_count": 1})

    def voice_generate(self, folder, instruction, _after_index=False, max_files=None, how_only=False):
        self.generated.append((folder, instruction, _after_index, max_files, how_only))
        self.plan()

    def voice_refine(self, text):
        self.refined.append(text)
        self.current_plan = {"folders": {"changed": len(self.refined)}}
        self.plan()

    def voice_apply(self, keep_empty=False):
        self.applied += 1
        self.keep_empty = keep_empty
        self.voice_apply_done.emit("Moved 2 files. Undo from Organization History.")


class MW:
    def __init__(self):
        self.organize_page = FakePage()
        self.said = []
        self.live_page = type("L", (), {"say_line": lambda s, line: self.said.append(line)})()


def _ctrl(folder):
    mw = MW()
    c = voc.VoiceOrganizeController(mw.organize_page, main_window=mw)
    _CTRLS.append(c)
    c._folder_candidates = lambda: [str(folder)]
    c._said_folder = lambda *a: True          # (these sentences stand in for one that names the folder)
    return c, mw


def _stage(c):
    return c._hf["stage"] if c._hf else None


def _read_ok():
    r._ask = lambda a, items, cancelled: _ok(items)


# ================================================================== Part A: organize a folder by voice
def test_preview_then_yes_then_undo():
    _read_ok()
    d = _tmp()
    _notes(d, 3)
    c, mw = _ctrl(d)
    assert c.start_hands_free("organize it", how="by type") == "started"
    assert _pump(lambda: _stage(c) == "preview")
    assert mw.organize_page.generated == [(str(d), "by type", True, r.MAX_FILES, True)], "only HOW goes to the planner"
    assert mw.organize_page.applied == 0 and c.waiting_for_answer() and "go ahead" in c.question()
    assert "move 2 files into 1 folder" in c.question()
    assert r.scan(d)["unread"] == [], "read before planning"
    assert c.start_hands_free("again") == "busy"
    c.refine("put a in Foo")
    assert mw.organize_page.refined == ["put a in Foo"] and _pump(lambda: _stage(c) == "preview")
    (d / "l.json").write_text(json.dumps({"moves": []}))
    mw.organize_page.last_voice_log = str(d / "l.json")
    c.answer(True)
    assert _pump(lambda: _stage(c) == "done") and mw.organize_page.applied == 1 and c.can_undo()
    assert mw.said[-1] == "Moved 2 files.", "said without the History pointer"
    assert mw.organize_page.keep_empty is True, "emptied folders wait until Undo is over"
    assert c.undo() and not c.can_undo(), "one undo"
    assert _pump(lambda: mw.said[-1].startswith("Put 0"))


def test_no_means_nothing_moves():
    _read_ok()
    d = _tmp()
    _notes(d, 1)
    c, mw = _ctrl(d)
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "preview")
    c.answer(False)
    assert c._hf is None and mw.organize_page.applied == 0


def test_preview_off_moves_at_once():
    _read_ok()
    d = _tmp()
    _notes(d, 1)
    c, mw = _ctrl(d)
    settings.live_organize_preview = False
    c.start_hands_free(f"organize the {d.name} folder")
    assert _pump(lambda: _stage(c) == "done") and mw.organize_page.applied == 1


def test_nothing_to_move_says_so():
    """(Windows) the planner reports 'already arranged' as a plan with nothing moving."""
    _read_ok()
    d = _tmp()
    _notes(d, 2)
    c, mw = _ctrl(d)
    p = mw.organize_page
    p.plan = lambda: p.voice_plan_ready.emit({"summary": "Already arranged this way", "folders": {},
                                              "target_folder": str(d), "file_count": 2, "move_count": 0,
                                              "folder_count": 1})
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "failed") and "Nothing needs moving" in mw.said[-1] and p.applied == 0


def test_a_guessed_folder_is_never_touched_without_asking():
    _read_ok()
    d = _tmp()
    _notes(d, 1)
    c, mw = _ctrl(d)
    del c._said_folder
    settings.live_organize_preview = False
    c.start_hands_free("organize my blue elephant folder")
    assert _pump(lambda: _stage(c) == "ask_guess") and d.name in c.question() and c.overlay._state == "choice"
    assert "guess_yes" in c.overlay.choice_buttons and c.overlay.choice_field is not None, "a button, and a box to type"
    assert r.scan(d)["unread"] and mw.organize_page.generated == [], "nothing read yet"
    assert c.answer(True) and _pump(lambda: _stage(c) == "done"), "you confirmed it: Preview off applies"
    c, mw = _ctrl(d)
    del c._said_folder
    c.start_hands_free("organize my blue elephant folder")
    assert _pump(lambda: _stage(c) == "ask_guess")
    c.answer(False)
    assert c._hf is None and mw.organize_page.generated == []
    S = voc.VoiceOrganizeController._said_folder
    assert S(r"C:\x\Downloads", "organize my download folder")
    assert S("C:/x/Downloads/", "organize my download folder"), "(Windows: either slash, trailing too)"
    assert not S(r"C:\x\Reports", "organize my photos")
    assert not S(r"C:\x\test", "organize my filect test tiny folder")


def test_a_change_that_changes_nothing():
    _read_ok()
    d = _tmp()
    _notes(d, 1)
    c, mw = _ctrl(d)
    p = mw.organize_page
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "preview")

    def same(text):
        p.refined.append(text)
        p.plan()
    p.voice_refine = same
    p.current_plan = {"folders": {"a": [1]}}
    c.refine("put x in y")
    assert _pump(lambda: _stage(c) == "preview" and "couldn't" in c.question()), p.refined
    assert p.refined == ["put x in y"] and "couldn't make that change" in mw.said[-1]
    assert c.answer(False)


def test_tucked_until_it_needs_you():
    _read_ok()
    d = _tmp()
    _notes(d, 1)
    c, mw = _ctrl(d)
    settings.assistant_auto_open = False
    calls, opened = [], []
    c.overlay.present = lambda tucked=False: calls.append(tucked)
    c.overlay.ensure_open = lambda: opened.append(_stage(c))
    c.start_hands_free("organize")
    assert calls == [True]
    assert _pump(lambda: _stage(c) == "preview") and opened == ["preview"], "the preview opens it anyway"


def test_failures_say_why():
    d = _tmp()
    c, mw = _ctrl(d)
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "failed") and "empty" in mw.said[-1]
    (d / ".git").mkdir()
    c, mw = _ctrl(d)
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "failed") and "code project" in mw.said[-1]


def test_a_windows_program_folder_is_left_alone():
    """(Windows) a folder holding an .exe and .dll files is a program: nothing in it moves."""
    _read_ok()
    d = _tmp()
    (d / "Tool").mkdir()
    (d / "Tool" / "tool.exe").write_bytes(b"MZ tool")
    (d / "Tool" / "helper.dll").write_bytes(b"MZ helper")
    c, mw = _ctrl(d)
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "failed") and "nothing I can move" in mw.said[-1], mw.said
    _notes(d, 1)
    c, mw = _ctrl(d)
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "preview")
    assert not any("tool" in u.lower() for u in r.scan(d)["files"]), "the program's files are never offered"


def test_big_read_asks_first():
    _read_ok()
    d = _tmp()
    _notes(d, 3)
    real, r.ASK_ABOVE = r.ASK_ABOVE, 2
    try:
        c, mw = _ctrl(d)
        c.start_hands_free("organize")
        assert _pump(lambda: _stage(c) == "ask_read") and c.overlay._state == "confirm"
        assert r.scan(d)["unread"], "nothing read yet"
        c.answer(True)
        assert _pump(lambda: _stage(c) == "preview")
    finally:
        r.ASK_ABOVE = real


def test_two_folders_same_name_asks():
    _read_ok()
    a, b = _tmp() / "Desktop" / "test", _tmp() / "Documents" / "test"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _notes(a, 1)
    c, mw = _ctrl(a)
    c._folder_candidates = lambda: [str(a), str(b)]
    c.start_hands_free("organize my test folder")
    assert _pump(lambda: _stage(c) == "ask_folder") and "Which one" in c.question()
    assert c.answer(True) is False and _stage(c) == "ask_folder", "'yes' doesn't say which one"
    c.refine("the one on my desktop")
    assert _pump(lambda: _stage(c) == "preview") and mw.organize_page.generated[0][0] == str(a)
    # said it up front: no question
    c, mw = _ctrl(a)
    c._folder_candidates = lambda: [str(b), str(a)]
    c.start_hands_free("organize the test folder on my desktop")
    assert _pump(lambda: _stage(c) == "preview") and mw.organize_page.generated[0][0] == str(a)


def test_closing_it_stops_the_reading():
    d = _tmp()
    _notes(d, 5)
    started, stopped = threading.Event(), threading.Event()

    def slow(agent, items, cancelled):
        started.set()
        while not cancelled.is_set():
            time.sleep(0.01)
        stopped.set()
        raise r._Cancelled()
    r._ask = slow
    c, mw = _ctrl(d)
    c.start_hands_free("organize")
    assert _pump(started.is_set)
    c.overlay._on_dismiss()
    assert _pump(stopped.is_set) and c._hf is None and mw.organize_page.generated == []
    assert r.scan(d)["unread"], "stopped before saving"


def test_a_plan_open_elsewhere_blocks_it():
    d = _tmp()
    _notes(d, 1)
    c, mw = _ctrl(d)
    mw.organize_page.moves = [1]
    assert c.start_hands_free("organize") == "page_busy" and c._hf is None
    mw.organize_page._voice_plan_owned = True
    _read_ok()
    assert c.start_hands_free("organize") == "started", "a leftover voice plan doesn't block"
    assert _pump(lambda: _stage(c) == "preview")
    c.overlay._on_dismiss()


def test_the_hotkey_takes_over():
    _read_ok()
    d = _tmp()
    _notes(d, 1)
    c, mw = _ctrl(d)
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "preview") and c.overlay._hands_free
    c.start_from_transcript("organize something else")
    assert c._hf is None and not c.overlay._hands_free
    assert _pump(lambda: len(mw.organize_page.generated) == 2)
    assert mw.organize_page.generated[1][2:] == (False, None, True), "the hotkey indexes first, with its own limit"
    n = c._req_id
    c.prepare_new()
    assert c._hf is None and c._req_id > n, "a new hotkey recording drops whatever was in flight"


def test_hotkey_change_while_a_plan_is_up():
    d = _tmp()
    c, mw = _ctrl(d)
    del c._said_folder
    c._plan_active = True
    c.start_from_transcript("Put sunset.png in a folder called Pictures.")
    assert _pump(lambda: mw.organize_page.refined), "the AI's guess at a folder doesn't replace the plan"
    assert mw.organize_page.refined == ["Put sunset.png in a folder called Pictures."] and not mw.organize_page.generated
    other = _tmp()
    c._plan_active = True
    ai_organizer.resolve_folder_with_ai = lambda text, cands: str(other)
    c.start_from_transcript(f"Organize the {other.name} folder")
    assert _pump(lambda: mw.organize_page.generated), "asking for another folder by name still starts over"
    assert mw.organize_page.generated[0][0] == str(other)
    c.overlay._on_dismiss()


def test_hotkey_sends_only_how_to_the_planner(monkeypatch):
    d = _tmp()
    c, mw = _ctrl(d)
    c.start_from_transcript("Organize the Reports folder on my desktop")
    assert _pump(lambda: mw.organize_page.generated)
    assert mw.organize_page.generated == [(str(d), "", False, None, True)], mw.organize_page.generated
    c, mw = _ctrl(d)
    monkeypatch.setattr(ai_organizer, "organize_how", lambda s, f: "by file type")
    c.start_from_transcript("Sort the Reports folder by file type")
    assert _pump(lambda: mw.organize_page.generated)
    assert mw.organize_page.generated[0][1] == "by file type"
    # choosing another folder keeps it
    other = _tmp()
    monkeypatch.setattr(voc.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(other)))
    c._on_change_folder()
    assert mw.organize_page.generated[-1] == (str(other), "by file type", False, None, True), mw.organize_page.generated
    # never resolved: the whole sentence, as before
    c, mw = _ctrl(d)
    monkeypatch.setattr(ai_organizer, "resolve_folder_with_ai", lambda text, cands: "")
    c.start_from_transcript("Organize the blue folder")
    assert _pump(lambda: c.overlay._state == "error")
    c._on_change_folder()
    assert mw.organize_page.generated[-1] == (str(other), "Organize the blue folder", False, None, False)
    c.overlay._on_dismiss()


def test_how_is_read_from_the_sentence():
    from app.core import vision
    real = vision._call_openai_proxy

    def said(text):
        vision._call_openai_proxy = lambda *a, **k: {"choices": [{"message": {"content": text}}]}
        return _REAL_HOW("Organize the Reports folder by type", r"C:\x\Reports")
    try:
        assert said("NONE") == "" and said("none.") == ""
        assert said('"by type"') == "by type"
        assert said("by date") == "", "you never said that: Filect decides"
        vision._call_openai_proxy = lambda *a, **k: None
        assert _REAL_HOW("Organize it", r"C:\x") == "Organize it"
    finally:
        vision._call_openai_proxy = real


def test_the_decider_is_told_what_is_happening():
    d = _tmp()
    c, mw = _ctrl(d)
    assert c.decider_state() is None
    base = {"folder": r"C:\x\Reports", "asked": "Which one?", "text": "t", "how": ""}
    for stage, says, allowed in (
            ("preview", "preview of organizing the Reports folder", {"ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"}),
            ("ask_read", "unread files in the Reports folder", {"ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE"}),
            ("ask_guess", "Which one?", {"ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"}),
            ("ask_folder", "Which one?", {"ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"}),
            ("reading", "hasn't moved anything yet", {"ORGANIZE_CANCEL", "ORGANIZE"}),
            ("planning", "hasn't moved anything yet", {"ORGANIZE_CANCEL", "ORGANIZE"})):
        c._hf = {**base, "stage": stage}
        st = c.decider_state()
        assert says in st["text"] and set(st["allowed"]) == allowed, (stage, st)
    c._hf = {**base, "stage": "applying"}
    assert c.decider_state() is None, "moving files: nothing to decide"
    c._hf = None
    c._undo_log, c._undo_until, c._undo_info = r"C:\x\l.json", time.monotonic() + 100, ("Reports", 5, time.time() - 130)
    st = c.decider_state()
    assert "Reports folder (5 files moved) 2 minutes ago" in st["text"] and st["allowed"] == ["ORGANIZE_UNDO", "ORGANIZE"]
    assert c.UNDO_SECS == 3600


def test_right_after_it_means_the_same_folder():
    """'Put the invoice and the receipt in Billing' right after an organize: that folder, no question."""
    _read_ok()
    d = _tmp()
    _notes(d, 2)
    c, mw = _ctrl(d)
    c.start_hands_free("organize it")
    assert _pump(lambda: _stage(c) == "preview")
    (d / "l.json").write_text(json.dumps({"moves": []}))
    mw.organize_page.last_voice_log = str(d / "l.json")
    c.answer(True)
    assert _pump(lambda: _stage(c) == "done")
    c._folder_candidates = lambda: []                  # (it must not look the folder up again)
    assert c.start_hands_free("put the invoice and the receipt in Billing", how="the invoice and receipt in Billing",
                              replace=True) == "started"
    assert _pump(lambda: _stage(c) == "preview") and mw.organize_page.generated[-1][0] == str(d)
    assert mw.organize_page.generated[-1][1].startswith("the invoice and receipt in Billing. Only move the files")


def test_a_new_request_takes_over():
    _read_ok()
    d = _tmp()
    _notes(d, 1)
    c, mw = _ctrl(d)
    p = mw.organize_page
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "preview")
    assert c.start_hands_free("the other one") == "busy", "not asked to take over"
    assert c.start_hands_free("the other one", how="by year", replace=True) == "started"
    assert _pump(lambda: _stage(c) == "preview" and len(p.generated) == 2) and p.generated[1][1] == "by year"
    c._hf["stage"] = "applying"
    assert c.start_hands_free("another", replace=True) == "busy" and c._hf["stage"] == "applying", "moving: it waits"
    c._hf["stage"] = "preview"
    c.overlay._on_dismiss()
    # while a plan is being worked out: it starts when that ends, without touching the running plan
    c, mw = _ctrl(d)
    p = mw.organize_page
    held = []
    real_plan = p.plan
    p.plan = lambda: held.append(1)
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "planning" and held)
    p.working = True
    assert c.start_hands_free("the other one", how="b", replace=True) == "started"
    assert c._hf_next == ("the other one", "b", "organize", None)
    assert c._hf is None and c._hf_dead_plans == 1, "the old run is gone"
    p.working, p.plan = False, real_plan
    real_plan()                               # the old plan arrives: ignored, and the new request starts
    assert _pump(lambda: _stage(c) == "preview" and len(p.generated) == 2) and p.generated[1][1] == "b"
    assert c._hf_next is None
    c.overlay._on_dismiss()


def test_a_new_request_stops_the_reading():
    d = _tmp()
    _notes(d, 5)
    started, stopped = threading.Event(), threading.Event()

    def slow(agent, items, cancelled):
        started.set()
        while not cancelled.is_set():
            time.sleep(0.01)
        stopped.set()
        raise r._Cancelled()
    r._ask = slow
    c, mw = _ctrl(d)
    c.start_hands_free("organize")
    assert _pump(started.is_set)
    r._ask = lambda a, items, cancelled: _ok(items)
    assert c.start_hands_free("other", replace=True) == "started" and _pump(stopped.is_set)
    assert _pump(lambda: _stage(c) == "preview")
    c.overlay._on_dismiss()


def test_cancel_at_each_stage():
    _read_ok()
    d = _tmp()
    _notes(d, 1)
    c, mw = _ctrl(d)
    assert c.cancel() is False, "nothing to cancel"
    c.start_hands_free("organize")
    assert _pump(lambda: _stage(c) == "preview")
    assert c.cancel() and c._hf is None and "left everything" in mw.said[-1]
    for stage in ("ask_read", "ask_folder", "ask_guess", "reading", "planning", "finding", "scanning"):
        c._hf = {"stage": stage, "folder": r"C:\x\R", "asked": "", "text": "t", "how": ""}
        assert c.cancel() and c._hf is None, stage
    assert c._hf_dead_plans == 1, "the plan that was still running is ignored when it arrives"
    c._hf_dead_plans = 0
    c._hf = {"stage": "applying", "folder": r"C:\x\R", "asked": "", "text": "t", "how": ""}
    assert c.cancel() is False and c._hf["stage"] == "applying", "files are moving: it finishes, Undo takes it back"
    c._hf = None


def test_folder_candidates_skip_hidden_and_junctions(monkeypatch, tmp_path):
    """(Windows) hidden / system folders and junctions are never offered to the AI."""
    import subprocess
    from PySide6.QtCore import QStandardPaths
    root = tmp_path / "Desk"
    (root / "Bills").mkdir(parents=True)
    (root / "Secret").mkdir()
    subprocess.run(["attrib", "+h", str(root / "Secret")], check=True, capture_output=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "Link"), str(root / "Bills")], check=True,
                   capture_output=True)
    monkeypatch.setattr(QStandardPaths, "writableLocation", staticmethod(lambda loc: str(root)))
    c = voc.VoiceOrganizeController(FakePage(), main_window=MW())
    _CTRLS.append(c)
    got = [os.path.basename(p) for p in c._folder_candidates()]
    assert got == ["Desk", "Bills"], got


# ================================================================== the Agents page (hands-free decisions)
class FakeOrg:
    def __init__(self):
        self.wait, self.undoable, self.rules = False, False, False
        self.answers, self.refines, self.started, self.undone, self.chosen, self.stopped = [], [], [], 0, [], []

    def takes_yes_no(self):
        return self.waiting_for_answer() and not self.rules

    def waiting_for_answer(self):
        return self.wait

    def question(self):
        return "Should I go ahead?" if self.wait else ""

    finished_at = 0.0
    cancelled = 0

    stage = None                         # (a waiting stage other than preview / ask_rules, for the guard tests)

    def decider_state(self):
        if self.wait and self.stage:
            return {"text": f"Filect asked something ({self.stage}).", "stage": self.stage, "kind": "organize",
                    "allowed": ["ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"]}
        if self.wait and self.rules:
            return {"text": "Filect asked for instructions.", "stage": "ask_rules", "kind": "auto",
                    "allowed": ["ORGANIZE_CHANGE", "ORGANIZE_NO_RULES", "ORGANIZE_CHOICE", "ORGANIZE_CANCEL", "ORGANIZE"]}
        if self.wait:
            return {"text": "A preview is on screen.", "stage": "preview", "kind": "organize",
                    "allowed": ["ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"]}
        if self.undoable:
            return {"text": "Filect finished organizing.", "stage": "after", "kind": None,
                    "allowed": ["ORGANIZE_UNDO", "ORGANIZE"]}
        return None

    def cancel(self):
        self.cancelled += 1
        return True

    def can_undo(self):
        return self.undoable

    def undo(self):
        self.undone += 1
        return True

    def answer(self, y):
        self.answers.append(y)
        return True

    def refine(self, t):
        self.refines.append(t)
        return True

    def refine_fits(self, sentence):
        from app.core.ai_organizer import sentence_names_a_folder
        return self.wait and not sentence_names_a_folder(sentence)

    def choose(self, m):
        self.chosen.append(m)
        return m in ("everything", "existing", "new")

    def no_rules(self):
        return self.rules

    def stop_auto(self, named="", asking=False):
        self.stopped.append(named)
        return "auto_off"

    def start_hands_free(self, text, how="", replace=False, kind="organize", mode=None):
        self.started.append((text, how, kind, mode))
        self.replaced = replace
        return "started"


class Router:
    answer = {"intent": "NEW", "index": None, "task": "x", "screen": False}
    seen = []

    def classify(self, text, focused, titles, **kw):
        Router.seen.append((text, focused, kw))
        return dict(Router.answer)


def _agents(monkeypatch):
    mw = tp._MW()
    page = ap.AssistantPage(mw)
    mw.voice_organize = FakeOrg()
    page._build_voice_candidates = lambda text: []
    page._notch = lambda *a: None
    page._agent_ok = lambda a: True
    page.ran = []
    page.run_task = lambda t, **k: page.ran.append(t)
    page.out = []
    page.live_routed.connect(lambda tag, o, t, k: page.out.append(o))
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: Router()))
    return page, mw.voice_organize


def _say(page, text):
    n = len(page.out)
    page.run_voice_task(text, source="live", tag=1)
    assert _pump(lambda: len(page.out) > n), text
    return page.out[-1]


def test_spoken_organize_starts_it(monkeypatch):
    page, org = _agents(monkeypatch)
    Router.answer = {"intent": "ORGANIZE", "index": None, "task": "by type", "screen": False}
    assert _say(page, "organize my downloads by type") == "organize"
    assert org.started == [("organize my downloads by type", "by type", "organize", None)] and page.ran == []


def test_spoken_answers_to_the_preview(monkeypatch):
    page, org = _agents(monkeypatch)
    org.wait = True
    assert _say(page, "Go ahead") == "approved" and org.answers == [True]
    assert _say(page, "no") == "denied" and org.answers == [True, False]
    Router.answer = {"intent": "ORGANIZE_CHANGE", "index": None, "task": "put a in Foo", "screen": False}
    assert _say(page, "put a in Foo") == "organize_changed" and org.refines == ["put a in Foo"]
    assert Router.seen[-1][2]["organize"]["text"] == "A preview is on screen.", "the decider is told"
    Router.answer = {"intent": "ORGANIZE_CANCEL", "index": None, "task": "x", "screen": False}
    assert _say(page, "I changed my mind, don't move anything") == "denied" and org.cancelled == 1
    Router.answer = {"intent": "ORGANIZE_GO", "index": None, "task": "x", "screen": False}
    assert _say(page, "yeah that looks right") == "approved" and org.answers == [True, False, True]
    Router.answer = {"intent": "ORGANIZE", "index": None, "task": "", "screen": False}
    assert _say(page, "do the Recipes folder instead") == "organize" and org.replaced is True
    Router.answer = {"intent": "IGNORE", "index": None, "task": "x", "screen": False}
    assert _say(page, "did you feed the dog") == "ignored" and org.refines == ["put a in Foo"]
    Router.answer = {"intent": "NEW", "index": None, "task": "weather", "screen": False}
    assert _say(page, "what's the weather") == "new" and page.ran == ["weather"]
    assert page.live_answer_context()["kind"] == "organize", "a short reply isn't dropped as noise"


def test_spoken_undo_only_when_it_can(monkeypatch):
    page, org = _agents(monkeypatch)
    Router.answer = {"intent": "NEW", "index": None, "task": "undo", "screen": False}
    assert _say(page, "undo that") == "new" and org.undone == 0, "nothing to undo: an ordinary request"
    assert "organize" not in Router.seen[-1][2], "not told about organizing when it isn't in play"
    org.undoable = True
    Router.answer = {"intent": "ORGANIZE_UNDO", "index": None, "task": "x", "screen": False}
    assert _say(page, "I changed my mind, undo those changes") == "undone" and org.undone == 1
    assert page.live_answer_context()["kind"] == "organize_undo", "a one-word 'undo' isn't dropped as noise"


def test_a_chat_that_changed_files_more_recently_wins(monkeypatch):
    page, org = _agents(monkeypatch)
    org.undoable, org.finished_at = True, time.time() - 60
    s = ap.AgentSession("k1", "C:/tmp", "p", "Header Color", True, "opus", "high")
    page._sessions[s.key], page._focused_key = s, s.key
    s.n_tools, s.started_at, s.duration = 3, time.time() - 5, 2
    assert "changed files more recently" in page._organize_state(org)["text"]
    s.n_tools = 0
    assert "changed files more recently" not in page._organize_state(org)["text"], "a chat that only answered doesn't count"
    s.n_tools, s.started_at = 3, time.time() - 600
    assert "changed files more recently" not in page._organize_state(org)["text"], "the organize was more recent"


def test_setup_failed_still_works(monkeypatch):
    page, org = _agents(monkeypatch)
    page.mw.voice_organize = None
    Router.answer = {"intent": "ORGANIZE", "index": None, "task": "", "screen": False}
    assert _say(page, "organize my downloads") == "new" and page.ran


def test_spoken_auto_organize(monkeypatch):
    page, org = _agents(monkeypatch)
    Router.answer = {"intent": "ORGANIZE_AUTO", "index": None, "task": "", "screen": False, "mode": "new"}
    assert _say(page, "keep my downloads organized from now on, only new files") == "auto_setup"
    assert org.started[-1] == ("keep my downloads organized from now on, only new files", "", "auto", "new")
    Router.answer = {"intent": "ORGANIZE_AUTO_OFF", "index": None, "task": "Downloads", "screen": False}
    assert _say(page, "stop auto organizing my downloads") == "auto_off" and org.stopped == ["Downloads"]
    org.wait = True
    Router.answer = {"intent": "ORGANIZE_CHOICE", "index": None, "task": "existing", "screen": False}
    assert _say(page, "the second one") == "organize_changed" and org.chosen == ["existing"]
    org.rules = True
    assert _say(page, "no") != "denied", "'any instructions?' — a bare no isn't a cancel: the decider reads it"
    Router.answer = {"intent": "ORGANIZE_NO_RULES", "index": None, "task": "", "screen": False}
    assert _say(page, "nah you decide") == "auto_setup"


def test_only_a_bare_yes_or_no_skips_the_decider():
    for t in ("Yes", "yes please", "Sure.", "Go ahead"):
        assert ap._plain_answer(t) is True, t
    for t in ("No.", "Cancel", "stop"):
        assert ap._plain_answer(t) is False, t
    for t in ("okay", "yeah", "right", "No, put them in Billing", "Forget it", "wait"):
        assert ap._plain_answer(t) is None, t


# ================================================================== the Assistant tab
def test_voice_page_says_what_happened():
    page, L, mw = tp._tab()
    for outcome, words in (("organize", "Organizing"), ("organize_changed", "Updating"), ("undone", "Putting"),
                           ("organize_busy", "Already"), ("organize_page_busy", "Organize page"),
                           ("auto_setup", "Setting up auto-organize"), ("auto_off", "turned off"),
                           ("auto_asked", "which folder"), ("auto_none", "isn't on")):
        row = page._row(1)
        page._on_routed(1, outcome, "", "")
        assert words in row.outcome.text(), (outcome, row.outcome.text())
    said = []
    page._speaker = lambda: type("S", (), {"hold": lambda s, x: None, "say": lambda s, line: said.append(line)})()
    settings.live_speak, L.on = False, True
    page.say_line("x")
    assert said == [], "only with Speak replies"
    settings.live_speak, L.on = True, False
    page.say_line("x")
    assert said == [], "only while listening"
    settings.live_speak, L.on = True, True
    page.say_line("x")
    assert said == ["x"]
    assert page.organize_toggle.isChecked() == settings.live_organize_preview
    page.organize_toggle.setChecked(not settings.live_organize_preview)
    assert page.organize_toggle.isChecked() == settings.live_organize_preview


# ================================================================== Part B: auto-organize by voice
def _auto(d):
    c, mw = _ctrl(d)
    p = mw.organize_page
    p.on, p.off = [], []
    p.voice_enable_watch = lambda f, how, action: p.on.append((f, how, action))
    p.voice_disable_watch = lambda f: p.off.append(f)
    return c, mw, p


def _plan_log(d, p):
    (d / "l.json").write_text(json.dumps({"moves": []}))
    p.last_voice_log = str(d / "l.json")


def test_auto_asks_which_way_then_confirms():
    _read_ok()
    d = _tmp()
    _notes(d, 3)
    c, mw, p = _auto(d)
    assert c.start_hands_free("keep it tidy", kind="auto") == "started"
    assert _pump(lambda: _stage(c) == "ask_mode") and c.overlay._state == "choice" and not p.generated
    assert c.choose("banana") is False and _stage(c) == "ask_mode"
    settings.live_organize_preview = False
    c.overlay.choice_buttons["new"].click()
    assert _stage(c) == "ask_rules" and "instructions" in c.question() and p.on == []
    c.overlay.choice_buttons["none"].click()
    assert _stage(c) == "confirm_auto" and c.overlay._state == "confirm" and p.on == [], "always asks, even with Preview off"
    assert "stay where they are" in c.question() and not p.generated, "new files only: nothing is read or moved"
    c.overlay._confirm_btn.click()
    assert p.on == [(str(d), "", 3)] and _stage(c) == "done" and c.can_undo()
    assert c.undo() and p.off == [str(d)] and not c.can_undo(), "Undo turns it off"
    c.overlay._on_dismiss()


def test_auto_everything_previews_and_never_saves_reorganize_all():
    _read_ok()
    d = _tmp()
    _notes(d, 3)
    c, mw, p = _auto(d)
    settings.live_organize_preview = False
    c.start_hands_free("keep it tidy and sort what's there", kind="auto", mode="everything")
    assert _pump(lambda: _stage(c) == "ask_rules") and c.no_rules()
    assert _pump(lambda: _stage(c) == "preview") and p.applied == 0 and p.on == [], "preview even with Preview off"
    assert "Turn on and organize" in c.overlay._organize_btn.text()
    _plan_log(d, p)
    c.answer(True)
    assert _pump(lambda: _stage(c) == "done") and p.on == [(str(d), "", 3)], "action 1 would flatten at the next Start"
    assert c.undo() and p.off == [str(d)]
    assert _pump(lambda: c.overlay._done_label.text().startswith("Put")) and "off again" in c.overlay._done_label.text()
    c.overlay._on_dismiss()


def test_auto_only_existing_folders():
    _read_ok()
    d = _tmp()
    _notes(d, 3)
    c, mw, p = _auto(d)
    c.start_hands_free("keep it tidy", kind="auto", mode="existing")
    assert _pump(lambda: _stage(c) == "ask_mode") and "any folders" in mw.said[-1], "nothing to use: says so and asks again"
    (d / "Taxes").mkdir()
    c.choose("existing")
    assert _stage(c) == "ask_rules" and c.refine("bills go in Taxes")
    assert _pump(lambda: _stage(c) == "preview") and "'Taxes'" in p.generated[-1][1] and "bills go in" in p.generated[-1][1]
    _plan_log(d, p)
    c.answer(True)
    assert _pump(lambda: _stage(c) == "done") and p.on[-1][2] == 2 and p.on[-1][1] == "bills go in Taxes"
    c.overlay._on_dismiss()


def test_auto_existing_ignores_hidden_and_program_folders():
    """(Windows) a hidden folder or a program folder doesn't count as 'a folder already there'."""
    import subprocess
    _read_ok()
    d = _tmp()
    _notes(d, 1)
    (d / "Hid").mkdir()
    subprocess.run(["attrib", "+h", str(d / "Hid")], check=True, capture_output=True)
    (d / "App").mkdir()
    (d / "App" / "a.exe").write_bytes(b"MZ a")
    (d / "App" / "a.dll").write_bytes(b"MZ b")
    c, mw, p = _auto(d)
    c.start_hands_free("keep it tidy", kind="auto", mode="existing")
    assert _pump(lambda: _stage(c) == "ask_mode") and "any folders" in mw.said[-1], c._subfolders()
    c.overlay._on_dismiss()


def test_auto_nothing_to_sort_goes_to_the_confirmation():
    d = _tmp()
    c, mw, p = _auto(d)
    c.start_hands_free("keep it tidy", how="by type", kind="auto", mode="everything")
    assert _pump(lambda: _stage(c) == "confirm_auto"), "empty folder: new files will still be sorted"
    c.overlay._on_dismiss()
    _read_ok()
    _notes(d, 2)
    p.plan = lambda: p.voice_plan_ready.emit({"summary": "Already arranged this way", "folders": {},
                                              "target_folder": str(d), "file_count": 2, "move_count": 0,
                                              "folder_count": 1})
    c.start_hands_free("keep it tidy", how="by type", kind="auto", mode="everything")
    assert _pump(lambda: _stage(c) == "confirm_auto"), "already sorted: same"
    c.overlay._on_dismiss()


def test_auto_asks_for_instructions_unless_given():
    _read_ok()
    d = _tmp()
    _notes(d, 3)
    c, mw, p = _auto(d)
    c.start_hands_free("keep it tidy by year", how="by year", kind="auto", mode="new")
    assert _pump(lambda: _stage(c) == "confirm_auto"), "said how already: no question"
    assert '"by year"' in c.overlay._confirm_label.text()
    c.overlay._on_dismiss()
    c.start_hands_free("keep it tidy", kind="auto", mode="new")
    assert _pump(lambda: _stage(c) == "ask_rules") and set(c.overlay.choice_buttons) == {"none"}
    assert c.waiting_for_answer() and not c.takes_yes_no(), "here 'no' carries on and 'stop' doesn't: the decider reads it"
    assert c.choose("everything") and _stage(c) == "ask_rules", "switching the way still leaves the question open"
    c.choose("new")
    assert c.overlay.choice_field is not None and _stage(c) == "ask_rules"
    c.overlay.choice_field.setText("typed rule")
    c.overlay.choice_field.returnPressed.emit()
    assert _stage(c) == "confirm_auto" and '"typed rule"' in c.overlay._confirm_label.text(), "typing works like saying it"
    c.overlay._on_dismiss()
    c.start_hands_free("keep it tidy", kind="auto", mode="new")
    assert _pump(lambda: _stage(c) == "ask_rules")
    assert c.refine("put bills in Money") and _stage(c) == "confirm_auto"
    c.overlay._confirm_btn.click()
    assert p.on[-1] == (str(d), "put bills in Money", 3), "kept for the files that arrive later"
    c.overlay._on_dismiss()
    c.start_hands_free("keep it tidy", kind="auto", mode="new")
    assert _pump(lambda: _stage(c) == "ask_rules") and c.no_rules() and _stage(c) == "confirm_auto"
    assert c.answer(True) and p.on[-1] == (str(d), "", 3)
    c.overlay._on_dismiss()
    n = len(p.on)
    c.start_hands_free("keep it tidy", kind="auto", mode="new")
    assert _pump(lambda: _stage(c) == "ask_rules") and c.cancel() and _stage(c) is None and len(p.on) == n
    assert c.no_rules() is False
    c.start_hands_free("keep it tidy", kind="auto", mode="everything")
    assert _pump(lambda: _stage(c) == "ask_rules") and c.no_rules() and _pump(lambda: _stage(c) == "preview")
    assert c.refine("recipes go in Food") and _pump(lambda: _stage(c) == "preview")
    assert c.choose("everything") and _pump(lambda: _stage(c) == "preview"), "changing the way doesn't ask again"
    _plan_log(d, p)
    c.answer(True)
    assert _pump(lambda: _stage(c) == "done") and p.on[-1][1] == "recipes go in Food", "a change at the preview applies to new files too"
    c.overlay._on_dismiss()


def test_auto_refuses_a_code_project():
    d = _tmp()
    (d / ".git").mkdir()
    c, mw, p = _auto(d)
    c.start_hands_free("keep it tidy", kind="auto", mode="new")
    assert _pump(lambda: _stage(c) == "failed") and p.on == [] and "code project" in mw.said[-1]
    c.overlay._on_dismiss()


def test_auto_turning_off():
    d = _tmp()
    c, mw, p = _auto(d)
    assert c.stop_auto("") == "auto_none" and "isn't on" in mw.said[-1]
    left_off = {"path": r"C:\x\Old Stuff", "paused": True}
    settings.auto_organize_folders = [{"path": r"C:\x\Alpha Stuff"}, {"path": r"C:\x\Beta Stuff"}, left_off]
    assert c.stop_auto("") == "auto_asked" and _stage(c) == "ask_stop" and p.off == []
    assert "Old Stuff" not in c.question(), "a folder that's off isn't one of the choices"
    assert c.refine("yes, that folder") and _stage(c) == "ask_stop" and p.off == [], "not a name: asks again"
    assert c.refine("the Beta one") and p.off == [r"C:\x\Beta Stuff"] and _stage(c) is None
    assert c.stop_auto("Old Stuff") == "auto_none", "left off: it isn't on"
    assert c.stop_auto("my Recipes folder") == "auto_none" and len(p.off) == 1, "a folder that isn't on stays a no"
    settings.auto_organize_folders = [{"path": r"C:\x\Alpha Stuff"}, left_off]
    assert c.stop_auto("") == "auto_off" and p.off[-1] == r"C:\x\Alpha Stuff", "only one is running: no need to ask"
    # "both" / "all of them" (the 2026-11-10 hunt: "both" only asked again); a folder really called "All" is still one
    for answer in ("both", "Both of them.", "all of them", "everything", "all my folders"):
        settings.auto_organize_folders = [{"path": r"C:\x\Alpha Stuff"}, {"path": r"C:\x\Beta Stuff"}, left_off]
        p.off.clear()
        assert c.stop_auto("") == "auto_asked" and c.typed(answer), answer
        assert sorted(p.off) == [r"C:\x\Alpha Stuff", r"C:\x\Beta Stuff"] and _stage(c) is None, (answer, p.off)
    settings.auto_organize_folders = [{"path": r"C:\x\All"}, {"path": r"C:\x\Beta Stuff"}]
    p.off.clear()
    assert c.stop_auto("all") == "auto_off" and p.off == [r"C:\x\All"], "a folder named All is that folder"
    p.auto_watcher = type("W", (), {"is_running": False})()
    assert c.stop_auto("") == "auto_none", "listed, but the watcher is stopped"
    c.overlay._on_dismiss()


def test_right_after_only_those_files_move():
    """(Found live) 'Now put the bills in a folder called Money' right after an organize moved ALL the files into
    Money: the planner is now told the rest stay in the folder they're in."""
    _read_ok()
    d = _tmp()
    _notes(d, 2)
    c, mw = _ctrl(d)
    c.start_hands_free("organize it")
    assert _pump(lambda: _stage(c) == "preview")
    (d / "l.json").write_text(json.dumps({"moves": []}))
    mw.organize_page.last_voice_log = str(d / "l.json")
    c.answer(True)
    assert _pump(lambda: _stage(c) == "done")
    c.start_hands_free("Now put the bills in a folder called Money.", how="Put the bills in a folder called Money.",
                       replace=True)
    assert _pump(lambda: _stage(c) == "preview")
    how = mw.organize_page.generated[-1][1]
    assert how.startswith("Put the bills in a folder called Money. Only move the files this is about.") and \
        "stays in the folder it is in now" in how, how
    assert ai_organizer._extract_category_to_folder_map(how) == {"bill": "Money"} or \
        ai_organizer._extract_category_to_folder_map(how) == {"bill": "money"}
    assert ai_organizer.catchall_folder_from_instruction(how) is None, "never read as 'everything else goes in…'"
    # a fresh organize (not right after) is a normal plan
    c.overlay._on_dismiss()
    c2, mw2 = _ctrl(d)
    c2.start_hands_free("organize it by type", how="by type")
    assert _pump(lambda: _stage(c2) == "preview") and mw2.organize_page.generated[-1][1] == "by type"


def test_a_change_sends_every_file_it_means_to_its_folder():
    """(Found live) the model listed the invoice AND the receipt as meant, then left the invoice behind."""
    g = ai_organizer._gather_mentioned
    cur = {"folders": {"personal-finance": [2, 4], "photos": [1]}}
    plan = {"files_mentioned": [2, 4], "folders": {"personal-finance": [2], "photos": [1], "billing": [4]}}
    assert g(plan, cur, "Put the invoice and the receipt in a folder called Billing.")["folders"] == {
        "photos": [1], "billing": [4, 2]}, "both go to Billing; the emptied folder is dropped"
    plan = {"files_mentioned": [2, 4], "folders": {"personal-finance": [2, 3], "billing": [4]}}
    assert g(plan, cur, "the invoice and receipt into billing")["folders"] == {"personal-finance": [3], "billing": [4, 2]}
    keep = {"files_mentioned": [2, 4], "folders": {"personal-finance": [2], "billing": [4]}}
    assert g(keep, cur, "put the receipt in Billing and leave the invoice where it is")["folders"] == {
        "personal-finance": [2], "billing": [4]}, "'leave' keeps one where it is"
    two = {"files_mentioned": [2, 1], "folders": {"Billing": [2], "Pictures": [1]}}
    assert g(two, cur, "put the invoice in Billing and the photo in Pictures")["folders"] == {
        "Billing": [2], "Pictures": [1]}, "two destinations: nothing changes"
    same = {"files_mentioned": [2, 4], "folders": {"personal-finance": [2, 4]}}
    assert g(same, cur, "put them together")["folders"] == {"personal-finance": [2, 4]}
    new = {"files_mentioned": [2, 4], "folders": {"personal-finance": [2], "Tax stuff 2025": [4]}}
    assert g(new, cur, "put the invoice and the receipt together in a new folder")["folders"] == {
        "Tax stuff 2025": [4, 2]}, "no folder named: the only new one holding them"
    assert g(None, cur, "x") is None and g({"folders": {}}, cur, "x") == {"folders": {}}


def test_which_folder_heard_with_a_space():
    """(Found live) speech-to-text wrote "on my desk top": it still means the Desktop one, no question."""
    _read_ok()
    a, b = _tmp() / "Desktop" / "Maple", _tmp() / "Documents" / "Maple"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _notes(a, 1)
    c, mw = _ctrl(a)
    c._folder_candidates = lambda: [str(b), str(a)]
    c.start_hands_free("Keep the maple folder on my desk top tidy.")
    assert _pump(lambda: _stage(c) == "preview") and mw.organize_page.generated[0][0] == str(a)
    c, mw = _ctrl(a)
    c._folder_candidates = lambda: [str(a), str(b)]
    c.start_hands_free("organize the maple folder in my Docu-ments")
    assert _pump(lambda: _stage(c) in ("failed", "preview")) and c._hf["folder"] == str(b)


def test_a_change_read_as_another_folder_is_still_a_change(monkeypatch):
    """(Measured on Codex) with the plan up, "put the screenshots in their own folder" came back as ORGANIZE (organize
    a different folder). It names no folder, so it's a change to the plan; naming one still starts over."""
    page, org = _agents(monkeypatch)
    org.wait = True
    Router.answer = {"intent": "ORGANIZE", "index": None, "task": "", "screen": False}
    assert _say(page, "put the screenshots in their own folder") == "organize_changed"
    assert org.refines == ["put the screenshots in their own folder"] and org.started == [], "the whole sentence"
    assert _say(page, "actually do my Documents folder instead") == "organize" and org.started, "a folder named: new one"
    assert ap._plain_answer("No, don't.") is False and ap._plain_answer("Please don't") is False


def test_refine_fits_per_stage():
    d = _tmp()
    c, mw = _ctrl(d)
    assert c.refine_fits("put the PDFs in Docs") is False, "nothing waiting"
    for stage, sentence, fits in (("preview", "put the screenshots in their own folder", True),
                                  ("preview", "do my Documents folder instead", False),
                                  ("ask_rules", "bills in a folder called Money", True),
                                  ("ask_folder", "the one on my desktop", True),
                                  ("ask_guess", "no, the Zebra Archive folder", True),
                                  ("confirm_auto", "put the bills in Money", False)):
        c._hf = {"stage": stage, "folder": "C:/x/R", "asked": "", "text": "t", "how": "", "kind": "organize"}
        assert c.refine_fits(sentence) is fits, (stage, sentence)
    c._hf = None


# ---- found by the live typed test (2026-11-09) --------------------------------------------------------------------
_H = os.path.join("C:" + os.sep, "Users", "u")
_DESK, _DOWN = os.path.join(_H, "Desktop"), os.path.join(_H, "Downloads")
_CANDS = [_DESK, _DOWN, os.path.join(_H, "Documents")] + [os.path.join(_DESK, n) for n in (
    "organize-and-auto-organize-for-windows", "auto organizing", "New Only", "Demo Messy Desk", "Demo Inbox",
    "Demo Camera Roll", "images", "Filect Test Files 2", "Filect Search Test")] + [os.path.join(_DOWN, n) for n in (
    "Demo Downloads Pile", "Demo Study Notes")]


def test_a_vague_folder_is_guessed_by_its_own_words_never_by_organize():
    g = lambda s: ai_organizer._word_guess_folder(s, _CANDS)
    assert g("Organize my messy stuff.") == os.path.join(_DESK, "Demo Messy Desk")
    assert g("Keep my inbox organized automatically.") == os.path.join(_DESK, "Demo Inbox")
    assert g("Tidy up my camera roll.") == os.path.join(_DESK, "Demo Camera Roll")
    assert g("Organize the pile in my downloads.") == os.path.join(_DOWN, "Demo Downloads Pile")
    assert g("Auto organize my downloads pile.") == os.path.join(_DOWN, "Demo Downloads Pile")
    assert g("Clean up the study notes in my downloads.") == os.path.join(_DOWN, "Demo Study Notes")
    # the how after the folder never picks one ("New Only" is a real folder on the dev Desktop)
    assert g("Keep my inbox tidy, only new files from now on.") == os.path.join(_DESK, "Demo Inbox")
    assert g("Keep my inbox tidy and put the bills in a folder called Money.") == os.path.join(_DESK, "Demo Inbox")
    # nothing in common, only action words, a place on its own, or a tie: the AI decides
    assert g("Organize my photos.") is None
    assert g("Organize my downloads.") is None
    assert g("Auto organize it.") is None
    assert g("Organize my test files.") is None                       # two Filect … Test folders: a tie


_REAL_RESOLVE = ai_organizer.resolve_folder_with_ai     # (the sandbox fixture stubs it in each test)


def test_an_ai_pick_that_only_shares_organize_is_not_a_match(monkeypatch):
    assert ai_organizer._only_action_words_match("Organize my photos.",
                                                 os.path.join(_DESK, "organize-and-auto-organize-for-windows"))
    assert ai_organizer._only_action_words_match("Keep my stuff organized automatically",
                                                 os.path.join(_DESK, "auto organizing"))
    assert not ai_organizer._only_action_words_match("Organize my photos.", os.path.join(_DESK, "images"))
    assert not ai_organizer._only_action_words_match("Organize my downloads.", _DOWN)
    from app.core import vision
    monkeypatch.setattr(vision, "_call_openai_proxy", lambda *a, **k: {"choices": [{"message": {
        "content": json.dumps({"folder": os.path.join(_DESK, "organize-and-auto-organize-for-windows")})}}]})
    assert _REAL_RESOLVE("Organize my photos.", _CANDS) is None
    monkeypatch.setattr(vision, "_call_openai_proxy", lambda *a, **k: {"choices": [{"message": {
        "content": json.dumps({"folder": os.path.join(_DESK, "images")})}}]})
    assert _REAL_RESOLVE("Organize my photos.", _CANDS) == os.path.join(_DESK, "images")


def test_the_safety_net_reads_only_the_users_rule_not_the_watchers_examples():
    from app.core.auto_watcher import AutoOrganizeWatcher
    w = AutoOrganizeWatcher()
    caught = {}
    w._start_worker = lambda files, folder, instr, existing=None: caught.update(instr=instr)
    w._current_worker = None
    w._process_files_with_ai(["x"], os.path.join(_DOWN, "Pile"), "put the bills in a folder called Money", None)
    instr = caught["instr"]
    assert "different folder" in instr and "lions folder" in instr          # (the watcher's own examples are there)
    assert ai_organizer._extract_category_to_folder_map(instr) == {"bill": "Money"}   # (never video -> "different")
    assert ai_organizer._extract_category_to_folder_map(
        ai_organizer.existing_folders_instruction("receipts in the Receipts folder", ["Receipts", "Photos"])
        if hasattr(ai_organizer, "existing_folders_instruction") else
        __import__("app.core.auto_watcher", fromlist=["x"]).existing_folders_instruction(
            "receipts in the Receipts folder", ["Receipts", "Photos"])) == {"receipt": "Receipts"}


def test_a_file_the_rule_doesnt_cover_is_never_sent_to_the_rules_folder(monkeypatch):
    sent = []
    monkeypatch.setattr(ai_organizer, "_request_openai", lambda msg: sent.append(msg) or {"folders": {"Money": [1]}})
    monkeypatch.setattr(ai_organizer, "_request_local", lambda msg: sent.append(msg) or {"folders": {"Money": [1]}},
                        raising=False)
    ai_organizer.request_organization_plan(
        "[AUTO-ORGANIZE] User's specific instructions: put the bills in a folder called Money\n\nPARENT FOLDER NAME: 'p'",
        [{"id": 1, "file_name": "pasta recipe.txt", "tags": ["recipe"], "label": "recipe"}])
    assert sent, "the planner was asked"
    msg = sent[0]
    assert "closest match" not in msg and "a recipe, a photo or a ticket is not a bill" in msg, msg


# ---- Codex: plain answers need no AI, and a misread reply is caught (2026-11-09) ------------------------------------
def _st(stage, allowed, text="", kind="organize"):
    return {"text": text or f"Filect is at {stage}.", "stage": stage, "kind": kind, "allowed": list(allowed)}


_PREVIEW = _st("preview", ["ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"])
_RULES = _st("ask_rules", ["ORGANIZE_CHANGE", "ORGANIZE_NO_RULES", "ORGANIZE_CHOICE", "ORGANIZE_CANCEL", "ORGANIZE"],
             kind="auto")
_GUESS = _st("ask_guess", ["ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"])
_WHICH = _st("ask_folder", ["ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"])
_AFTER = _st("after", ["ORGANIZE_UNDO", "ORGANIZE"])


def test_plain_answers_are_decided_without_the_ai():
    q = lambda text, st: (ir.organize_quick(text, st) or {}).get("intent")
    for text, st, want in [
            ("hold on, scrap that", _RULES, "ORGANIZE_CANCEL"), ("actually stop, I don't want this", _RULES, "ORGANIZE_CANCEL"),
            ("never mind, leave them on", _st("ask_stop", ["ORGANIZE_CANCEL", "ORGANIZE_CHANGE"]), "ORGANIZE_CANCEL"),
            ("no wait, don't do anything", _PREVIEW, "ORGANIZE_CANCEL"), ("nah forget it", _PREVIEW, "ORGANIZE_CANCEL"),
            ("nah just do whatever makes sense", _RULES, "ORGANIZE_NO_RULES"), ("nope", _RULES, "ORGANIZE_NO_RULES"),
            ("no, you decide", _RULES, "ORGANIZE_NO_RULES"), ("uh yeah I do", _RULES, "IGNORE"), ("yes", _RULES, "IGNORE"),
            ("yeah that one", _GUESS, "ORGANIZE_GO"), ("correct", _GUESS, "ORGANIZE_GO"), ("yep looks good", _PREVIEW, "ORGANIZE_GO"),
            ("undo", _AFTER, "ORGANIZE_UNDO"), ("put it all back please", _AFTER, "ORGANIZE_UNDO"),
            ("desktop", _WHICH, "ORGANIZE_CHANGE"), ("the downloads one", _WHICH, "ORGANIZE_CHANGE"),
            ("the one in documents", _WHICH, "ORGANIZE_CHANGE")]:
        assert q(text, st) == want, (text, st["stage"], q(text, st))
    mode_q = _st("ask_mode", ["ORGANIZE_CHOICE", "ORGANIZE_CANCEL", "ORGANIZE"], kind="auto")
    for text, mode in [("number two please", "existing"), ("the last one", "new"), ("the first one", "everything"),
                       ("option 3", "new"), ("just the new stuff", "new"), ("only the folders already there", "existing"),
                       ("sort everything", "everything")]:
        d = ir.organize_quick(text, mode_q)
        assert d and d["intent"] == "ORGANIZE_CHOICE" and d["task"] == mode, (text, d)
    # anything with more in it goes to the decider
    for text, st in [("yes but put the PDFs in Docs", _PREVIEW), ("don't move the PDFs", _PREVIEW),
                     ("put the bills in Money", _RULES), ("the Zebra one on my desktop", _WHICH),
                     ("no", _RULES | {"stage": "x"}),
                     ("okay", _PREVIEW), ("yeah", _GUESS), ("undo the last edit in the script", _AFTER),
                     ("stop the downloads one", _st("ask_stop", ["ORGANIZE_CANCEL", "ORGANIZE_CHANGE"])),
                     ("undo", _AFTER | {"text": "Filect finished. The current conversation changed files more recently than that."}),
                     ("undo", None), ("never mind", None)]:
        assert ir.organize_quick(text, st) is None, (text, st and st.get("stage"))


def test_the_guard_turns_a_misread_reply_into_its_answer():
    new = {"intent": "NEW", "index": None, "task": "x", "mode": None}
    g = lambda text, st, d=new: ir.organize_guard(dict(d), text, st)
    assert g("the documents one", _WHICH)["intent"] == "ORGANIZE_CHANGE"
    assert g("the documents one", _WHICH, {**new, "intent": "RESUME", "index": 1})["intent"] == "ORGANIZE_CHANGE"
    assert g("can you stick the screenshots somewhere separate", _PREVIEW)["intent"] == "ORGANIZE_CHANGE"
    assert g("rename the photos folder to Snapshots", _PREVIEW)["intent"] == "ORGANIZE_CHANGE"
    assert g("make it sort everything that's there now too", _RULES) == \
        {"intent": "ORGANIZE_CHOICE", "index": None, "task": "everything", "mode": None, "screen": False}
    assert g("actually forget it", _AFTER, {**new, "intent": "IGNORE"})["intent"] == "ORGANIZE_UNDO"
    off = g("turn off auto organize", None)
    assert off["intent"] == "ORGANIZE_AUTO_OFF" and off["task"] == ""
    assert g("disable auto organize for my downloads", None)["task"] == "downloads"
    assert g("don't sort my downloads automatically anymore", None)["intent"] == "ORGANIZE_AUTO_OFF"
    on = g("automatically sort my downloads, but only into the folders that are already there", None)
    assert on["intent"] == "ORGANIZE_AUTO" and on["mode"] == "existing"
    on = g("keep my downloads organized, put the bills in a folder called Money", None)
    assert on["intent"] == "ORGANIZE_AUTO" and on["task"] == "put the bills in a folder called Money"
    # left alone: questions, remarks, another folder named at the preview, a right answer, an unrelated request
    for text, st in [("how do I turn off auto organize", None), ("what's the weather", _PREVIEW),
                     ("my sister keeps her downloads organized automatically, it's crazy", None),
                     ("actually do my Documents folder instead", _PREVIEW), ("who won the game last night", _PREVIEW),
                     ("write me a python script that sorts files by extension", None),
                     ("remind me to organize the garage on saturday", None)]:
        assert g(text, st)["intent"] == "NEW", text
    right = {"intent": "ORGANIZE_GO", "index": None, "task": "x", "mode": None}
    assert g("the documents one", _WHICH, right) == right, "a decision that fits is never touched"
    org_ = {"intent": "ORGANIZE", "index": None, "task": "", "mode": None}
    assert g("hmm, can you put the invoices and receipts together in one folder called Money", _PREVIEW,
             org_)["intent"] == "ORGANIZE_CHANGE", "a change to the plan, not another folder"
    assert g("merge the docs and the PDFs into one folder", _PREVIEW, org_)["intent"] == "ORGANIZE_CHANGE"
    assert g("actually do my Documents folder instead", _PREVIEW, org_) == org_, "another folder stays ORGANIZE"
    assert g("uh could you tidy up my documents folder and like put all the PDFs together", None, org_)["task"] == \
        "put all the PDFs together", "how to sort, when the decider dropped it"
    assert g("organize my downloads", None, org_) == org_
    assert g("put the invoice and the receipt in Billing", _AFTER, {**org_, "task": "put the invoice and the receipt in"
                                                                    " Billing"})["intent"] == "ORGANIZE", "right after"
    # right after organizing, moving files is that same folder — never a new agent chat (Codex, live)
    assert g("Now put the recipe in a folder called Kitchen.", _AFTER)["intent"] == "ORGANIZE"
    assert g("Now put the recipe in a folder called Kitchen.", _AFTER,
             {**new, "intent": "FOLLOW_UP"})["intent"] == "ORGANIZE"
    assert g("Now put the recipe in a folder called Kitchen.", _AFTER,
             {**new, "intent": "RESUME", "index": 0})["intent"] == "ORGANIZE", "(an old chat with that very title: live)"
    assert g("Now put the recipe in a folder called Kitchen.", _AFTER | {
        "text": "Filect finished. The current conversation changed files more recently than that."})["intent"] == "NEW", \
        "a chat that changed files since: it may be about that chat's work"
    assert g("Now put the recipe in a folder called Kitchen.", None)["intent"] == "NEW", "nothing organized: a request"
    assert g("what should I put in the kitchen folder", _AFTER)["intent"] == "NEW", "a question"
    # "organize my <a folder that exists by that name>" is organizing it, even when the decider read it as coding
    game = os.path.join(_DESK, "Demo Game Code")
    fo = lambda text, cands: ir.organize_guard(dict(new), text, None, folders=cands)
    assert fo("Organize my Demo Game Code folder.", _CANDS + [game])["intent"] == "ORGANIZE"
    assert fo("Organize my Demo Game Code folder.", lambda: _CANDS + [game])["intent"] == "ORGANIZE", "(a callable)"
    assert fo("Organize my Demo Game Code folder.", _CANDS)["intent"] == "NEW", "no folder by that name: left alone"
    assert fo("organize the imports in my utils module", _CANDS + [game])["intent"] == "NEW", "coding, no such folder"
    assert fo("how do I organize my Demo Game Code folder", _CANDS + [game])["intent"] == "NEW", "a question"
    assert fo("Organize my Demo Game Code folder.", None)["intent"] == "NEW", "(no folder list: unchanged)"
    assert fo("Organize my messy stuff.", _CANDS)["intent"] == "ORGANIZE", "its words fit one folder (then: 'did you mean')"
    assert fo("organize my thoughts about the messy launch", _CANDS)["intent"] == "NEW", "not about files"
    assert fo("sort my files", _CANDS)["intent"] == "NEW", "no folder to guess: the decider's call stands"


def test_spoken_plain_answers_and_codex_misreads_in_the_assistant_tab(monkeypatch):
    page, org = _agents(monkeypatch)
    org.wait = True
    Router.answer = {"intent": "IGNORE", "index": None, "task": "x", "screen": False}    # (a decider that misreads)
    n = len(Router.seen)
    assert _say(page, "hold on, scrap that") == "denied" and org.cancelled == 1
    assert len(Router.seen) == n, "said plainly: decided without the AI"
    Router.answer = {"intent": "NEW", "index": None, "task": "x", "screen": False}
    assert _say(page, "can you stick the screenshots somewhere separate") == "organize_changed"
    assert org.refines[-1] == "can you stick the screenshots somewhere separate" and page.ran == []
    org.stage = "ask_folder"
    Router.answer = {"intent": "RESUME", "index": 0, "task": "resume the Tax Documents 2025 conversation", "screen": False}
    assert _say(page, "the documents one") == "organize_changed" and page.ran == [], "never an old chat"
    org.stage, org.wait = None, False
    Router.answer = {"intent": "NEW", "index": None, "task": "turn off auto organize", "screen": False}
    assert _say(page, "turn off auto organize") == "auto_off" and org.stopped == [""] and page.ran == []
    assert _say(page, "how do I turn off auto organize?") == "new", "a question stays a question"


# ------------------------------------------------------------------ typing into the pop-up (Mac bugs 46, 47; 2026-11-10 hunt)
def test_every_question_in_your_own_words_has_a_box_and_typed_answers_are_read_like_spoken_ones(monkeypatch):
    """"Which folder?", "Did you mean…?" and the preview take typing (speech mishears names). What's typed is read the
    way a spoken answer is: "yes" never becomes a folder called "yes" or a change to the plan, "cancel" / "nvm" close
    it, a pasted path (Explorer's "Copy as path" puts quotes round it) is that folder."""
    _read_ok()
    d = _tmp()
    _notes(d, 2)
    c, mw = _ctrl(d)
    monkeypatch.setattr(ai_organizer, "resolve_folder_with_ai", lambda text, cands: "")
    c.start_hands_free("organize that folder")
    assert _pump(lambda: _stage(c) == "ask_folder")
    assert c.overlay._state == "choice" and "pick_folder" in c.overlay.choice_buttons and c.overlay.choice_field
    assert c.typed("yes") and _stage(c) == "ask_folder", "'yes' doesn't say which: still asking"
    assert c.typed(f'  "{d}"  ') and _pump(lambda: _stage(c) == "preview") and _same(c._hf["folder"], d), "pasted path"
    assert c.overlay.plan_field is not None, "the preview has a box too"
    assert c.typed("nvm") and c._hf is None and mw.organize_page.applied == 0, "'nvm' closes it, nothing moved"
    # "Did you mean…?": a button and a box; "y" goes on
    monkeypatch.setattr(ai_organizer, "resolve_folder_with_ai", lambda text, cands: cands[0])
    c, mw = _ctrl(d)
    del c._said_folder
    c.start_hands_free("organize my blue elephant folder")
    assert _pump(lambda: _stage(c) == "ask_guess") and c.overlay.choice_field is not None
    assert c.typed("y") and _pump(lambda: _stage(c) == "preview")
    c.overlay._on_dismiss()
    # auto-organize's "Any instructions?": "yes" isn't the rules; "n" is none; Enter twice sends once
    c, mw, p = _auto(d)
    c.start_hands_free("keep it tidy", kind="auto", mode="new")
    assert _pump(lambda: _stage(c) == "ask_rules")
    assert c.typed("yes") and _stage(c) == "ask_rules" and not c._hf["how"], "'yes' isn't saved as the rules"
    f = c.overlay.choice_field
    f.setText("bills go in Money")
    f.returnPressed.emit()
    f.returnPressed.emit()                        # (Enter twice)
    assert _stage(c) == "confirm_auto" and c._hf["how"] == "bills go in Money" and f.text() == ""
    c.overlay._on_dismiss()
    c.start_hands_free("keep it tidy", kind="auto", mode="new")
    assert _pump(lambda: _stage(c) == "ask_rules") and c.typed("n") and _stage(c) == "confirm_auto" and not c._hf["how"]
    c.overlay._on_dismiss()


def _same(a, b):
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def test_folder_names_people_really_have(tmp_path):
    """The 2026-11-10 hunt: "organize new folder 2" picked Windows' "New folder" (the wrong folder, no question);
    apostrophes, "&" and accents never counted as named ("Did you mean…?" every time)."""
    desk = tmp_path / "Desktop"
    down = tmp_path / "Downloads"
    subs = {desk: ["New folder", "New folder (2)", "Mom's Recipes", "Bills & Receipts", "Café Photos", "Old Stuff"],
            down: ["Old Stuff"]}
    cands = []
    for root, names in subs.items():
        cands.append(str(root))
        for n in names:
            (root / n).mkdir(parents=True)
            cands.append(str(root / n))
    pick = lambda s: os.path.basename(ai_organizer._folder_named_in(s, cands) or "")
    assert pick("organize new folder 2") == "New folder (2)" and pick("organize new folder two") == "New folder (2)"
    assert pick("can you organize my new folder") == "New folder"
    assert pick("clean up my moms recipes folder") == "Mom's Recipes"
    assert pick("organize the bills and receipts folder") == "Bills & Receipts"
    assert pick("tidy my cafe photos folder") == "Café Photos"
    assert pick("organize my old stuff folder") == "", "two of them: never pick one"
    typed = lambda t: os.path.basename(voc._typed_folder(t, cands) or "")
    assert typed("moms recipes") == "Mom's Recipes" and typed("bills and receipts") == "Bills & Receipts"
    assert typed("new folder 2") == "New folder (2)" and typed("cafe photos") == "Café Photos"
    assert typed("old stuff") == "" and typed("downloads") == "", "two Old Stuffs / a place: the usual question"
    assert typed(f'"{desk / "Old Stuff"}"') == "Old Stuff", "a pasted path says exactly which"
    f = desk / "Old Stuff" / "a.txt"
    f.write_text("x")
    assert typed(f'"{f}"') == "", "a file's path isn't a folder"


# ================================================================== 2026-11-10: sharing the panel with finding a file
class _FakeFind:
    """The search panel, as the Agents page sees it: one file on screen ("Found it… Want me to open it?")."""
    def __init__(self):
        self.said_at, self.opened, self.closed = 0.0, [], 0

    def waiting_for_answer(self):
        return True

    def decider_state(self):
        return {"head": "FILECT IS FINDING A FILE: ", "text": "One file is on screen.", "stage": "results",
                "question": "", "rows": 1, "names": ["invoice.pdf"],
                "allowed": ["FIND_OPEN", "FIND_SHOW", "FIND_ALL", "FIND_NOT", "FIND_MORE", "FIND_CLOSE"]}

    def open(self, which=""):
        self.opened.append(which)
        return "find_opened"

    def close(self):
        self.closed += 1
        return True


def test_yes_goes_to_whatever_asked_last_a_chat_approval_or_the_search_panel(monkeypatch):
    """(The review: a search panel left on screen took the "yes" meant for "Allow Bash?" and opened a file.)"""
    page, org = _agents(monkeypatch)
    finder = _FakeFind()
    page.mw.voice_find = finder
    decided = []
    page._decide = lambda yn, key=None: decided.append((yn, key))
    s = ap.AgentSession("k1", "C:/tmp", "count my files", "Count Files", True, "m", "high")
    s.status, s.approval_req = "needs_you", type("Req", (), {"tool": "Bash", "tool_input": {}})()
    page._sessions["k1"] = s
    page._voice_target = lambda: s
    finder.said_at, s.approval_at = time.time() - 60, time.time()          # the chat asked after the panel spoke
    assert _say(page, "yes") == "approved" and decided == [(True, "k1")] and finder.opened == []
    decided.clear()
    s.approval_at = time.time() + 5
    assert _say(page, "no") == "denied" and decided == [(False, "k1")] and finder.closed == 0
    decided.clear()
    s.approval_at, finder.said_at = time.time() - 120, time.time()         # the panel spoke last
    assert _say(page, "yes") == "find_opened" and finder.opened == ["1"] and decided == []


def test_organize_takes_the_panel_only_when_it_really_does():
    d = _tmp()
    c, mw, p = _auto(d)
    stopped = []
    mw.voice_find = type("F", (), {"stop": lambda self: stopped.append(1)})()
    # "stop auto-organizing" shows its answer on the panel: a search there stops first
    assert c.stop_auto("") == "auto_none" and stopped == [1]
    c.overlay._on_dismiss()
    # a plan open on the Organize page: organize doesn't take the panel, so the search's rows stay
    stopped.clear()
    p.moves = ["x"]
    assert c.start_hands_free("organize it", how="by type") == "page_busy" and stopped == []
    p.moves = []
    # undo puts its answer on the panel: the search stops
    c._undo_auto, c._undo_until, c._undo_info = r"C:\x\Inbox", time.monotonic() + 60, ("Inbox", 0, time.time())
    assert c.undo() and stopped == [1]
    c.overlay._on_dismiss()
    # the organize hotkey still working out the folder: the panel is organize's (a search can't start over it)
    assert not c.panel_busy()
    c._resolve_worker = type("W", (), {"isRunning": lambda self: True})()
    assert c.panel_busy() and not c.busy()
    c._resolve_worker = type("W", (), {"isRunning": lambda self: False})()
    p.working = True
    assert c.panel_busy(), "a plan being made from the hotkey"
    p.working = False
    c._resolve_worker = None
    assert not c.panel_busy()
