"""
Organize by voice: the controller that sequences the feature (Windows port of the Mac controller).

Wiring only: it owns the OrganizeOverlay (the top pop-up) and delegates the planning / moving to the
OrganizePage engine (its voice_* methods and signals). All heavy work runs off the UI thread in workers.

Two ways in:
  * the organize hotkey (dictation.py): prepare_new() as recording starts, start_from_transcript(text) at the end;
  * the voice assistant (hands-free, assistant_page.py): start_hands_free(sentence, how, ...) when the decider says
    ORGANIZE / ORGANIZE_AUTO, then answer / refine / choose / cancel / undo / stop_auto as you talk to it.
    decider_state() tells the decider what the panel is doing, so "yes", "the second one" and "forget it" work.

Hands-free stages: finding, ask_folder, ask_guess, scanning, ask_read, reading, planning, preview, applying, done,
failed — and for auto-organize ask_mode, ask_rules, confirm_auto (and ask_stop when turning it off).

Windows notes: folders are compared with os.path.normcase; the candidate list leaves out hidden / system folders and
junctions ("My Music" inside Documents); the folder picker is a normal QFileDialog.
"""
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QFileDialog

logger = logging.getLogger(__name__)


def _base(path) -> str:
    """A folder's name (Windows: either slash)."""
    p = str(path or "").rstrip("\\/")
    return os.path.basename(p) or p


def _same_path(a, b) -> bool:
    return os.path.normcase(os.path.normpath(str(a or ""))) == os.path.normcase(os.path.normpath(str(b or "")))


# Typed into the panel's box (not heard: "ok" / "yeah" aren't room filler here, so they count).
_TYPED_YES = {"y", "yes", "yeah", "yea", "ya", "yep", "yup", "ok", "okay", "k", "kk", "sure", "go", "yes please",
              "ok please", "approve", "confirm", "yes do it", "ok do it", "okay do it", "do it", "go ahead"}
_TYPED_NO = {"n", "no", "nah", "nope", "nvm", "no thanks", "no thank you", "cancel", "stop", "x", "none"}


def _typed_folder(text: str, candidates) -> str:
    """The folder typed into the box, or "": a full path (Explorer's "Copy as path" wraps it in quotes; %USERPROFILE%
    and ~ work), or exactly the name of ONE folder Filect offers ("demo inbox", "the Demo Inbox folder"). Two folders
    of that name: "" (the usual question says which)."""
    t = (text or "").strip().strip('"\'').strip()
    p = os.path.expanduser(os.path.expandvars(t))
    if os.path.isabs(p) and os.path.isdir(p):
        return os.path.normpath(p)
    from app.core.ai_organizer import _fold, _name_key
    said = re.sub(r"^(?:the|my|our|in|on)\s+", "", " ".join(re.findall(r"[a-z0-9'&]+", _fold(t))))
    key = _name_key(said)
    if not key:
        return ""
    cands = list(candidates or ())
    keys = {os.path.normcase(os.path.normpath(c)) for c in cands}
    # Never Desktop / Downloads / Documents themselves by name: "downloads" answers "there's more than one “test”
    # folder, in Desktop and Downloads. Which one?" — it says where, not "organize my whole Downloads".
    subs = [c for c in cands if os.path.normcase(os.path.dirname(os.path.normpath(c))) in keys]
    hits = [c for c in subs if _name_key(_base(c)) == key]
    return hits[0] if len(hits) == 1 else ""


def _spoken(hint: str) -> str:
    """The done message without the History pointer (it's on the panel; not worth saying)."""
    for cut in (" Revert", " Undo from"):
        hint = hint.split(cut)[0]
    return hint.strip()


def _hidden_or_link(path: str) -> bool:
    """Hidden / system folders and junctions are never offered (Documents\\My Music is a junction you can't open)."""
    try:
        if os.path.islink(path) or os.path.isjunction(path):
            return True
        attrs = getattr(os.stat(path, follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & 0x2 or attrs & 0x4)      # FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM
    except OSError:
        return True


class _FolderResolveWorker(QThread):
    """Ask the AI which real folder the spoken instruction means, OFF the UI thread (a network call). Emits the chosen
    path, or "" if none matched. `want_how`: also work out what the sentence says about how to sort (read `.how`)."""
    done = Signal(str)

    def __init__(self, instruction, candidates, tries=1, want_how=False):
        super().__init__()
        self._instruction = instruction
        self._candidates = candidates
        self._tries = tries
        self._want_how = want_how
        self.how = instruction        # what the sentence says about how to sort; read once `done` fires

    def run(self):
        path = None
        try:
            from app.core.ai_organizer import resolve_folder_with_ai
            for _ in range(self._tries):
                path = resolve_folder_with_ai(self._instruction, self._candidates)
                if path:
                    if self._want_how:
                        from app.core.ai_organizer import organize_how
                        self.how = organize_how(self._instruction, path)
                    break
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[VOICE ORGANIZE] folder resolve worker failed: {e}")
        self.done.emit(path or "")


class _ScanWorker(QThread):
    """What's in the folder, and what still needs reading."""
    done = Signal(object, int)

    def __init__(self, folder, rid):
        super().__init__()
        self._folder, self._rid = folder, rid

    def run(self):
        try:
            from app.core.organize_reader import scan
            result = scan(self._folder)
        except Exception as e:
            logger.warning(f"[VOICE ORGANIZE] scan failed: {e}")
            result = {"error": "failed"}
        self.done.emit(result, self._rid)


class _ReadWorker(QThread):
    """Reads the unread files on the person's Claude / Codex subscription."""
    progress = Signal(int, int, int)
    done = Signal(object, int)

    def __init__(self, paths, agents, rid):
        super().__init__()
        self._paths, self._agents, self._rid = paths, agents, rid
        self.cancelled = threading.Event()

    def run(self):
        try:
            from app.core.organize_reader import read_files
            result = read_files(self._paths, self._agents, cancelled=self.cancelled,
                                progress=lambda d, t: self.progress.emit(d, t, self._rid))
        except Exception as e:
            logger.warning(f"[VOICE ORGANIZE] reading failed: {e}")
            result = {"read": 0, "by": {}, "cancelled": False, "error": str(e)}
        self.done.emit(result, self._rid)


class _UndoWorker(QThread):
    """Puts one organize back, off the UI thread."""
    done = Signal(object)

    def __init__(self, log_file):
        super().__init__()
        self._log = log_file

    def run(self):
        try:
            from app.core.apply import undo_log
            result = undo_log(self._log)
        except Exception as e:
            logger.warning(f"[VOICE ORGANIZE] undo failed: {e}")
            result = {"reverted": 0, "skipped": 0, "errors": [str(e)]}
        self.done.emit(result)


try:
    from app.ui.organize_overlay import OrganizeOverlay
except Exception:  # pragma: no cover
    OrganizeOverlay = None


class VoiceOrganizeController(QObject):
    """Sequences organize by voice: find the folder -> (read) -> plan -> preview -> move -> Undo."""
    UNDO_SECS = 3600             # "undo" by voice stays on offer this long
    TIDY_SECS = 600              # emptied folders go after this (see apply.tidy_emptied_sources)
    # The auto-organize list's saved action. "everything" is sorted once here, so from then on it's new files only
    # (saving Re-organize All would flatten the folder the next time Start is pressed).
    AUTO_ACTIONS = {"everything": 3, "existing": 2, "new": 3}
    AUTO_CHOICES = [("everything", "Sort everything now, plus new files"),
                    ("existing", "Only use the folders already there"),
                    ("new", "Only new files from now on")]

    def __init__(self, organize_page, main_window=None):
        super().__init__()
        self.organize_page = organize_page
        self.main_window = main_window
        self.overlay = OrganizeOverlay()

        self._recorder = None        # the refine recorder while it records
        self._graveyard = None       # the just-finished recorder, held until GC-safe
        self._refining = False
        self._plan_active = False    # True while the panel shows a plan (a re-spoken sentence then changes it)
        self._last_instruction = ""
        self._last_how = ""          # the part of it that says how to sort ("" = Filect decides)
        self._folder = ""
        self._resolve_worker = None  # live _FolderResolveWorker (hotkey path), kept referenced while it runs
        self._req_id = 0             # bumped per utterance / on dismiss; stale results are dropped

        # The voice assistant's run (None when it isn't organizing).
        self._hf = None
        self._hf_threads = []        # QThreads kept alive until they finish
        self._reader = None          # the running _ReadWorker (Cancel stops it)
        self._hf_dead_plans = 0      # plans still arriving for a run you cancelled
        self._undo_log = ""          # Undo: the last hands-free organize's move log, until _undo_until
        self._undo_until = 0.0
        self._undo_info = ("", 0, 0.0)  # what Undo would put back: (folder name, files moved, when)
        self._undo_auto = ""         # the folder whose auto-organize was just turned on (Undo turns it off)
        self._undone_auto = False
        self._last_folder = ""       # the folder last organized ("put these in Billing" right after means that one)
        self._hf_next = None         # (sentence, how, kind, mode): a new request waiting for the plan it replaced
        self._tidy_pending = ("", "")  # (log, folder) whose emptied folders go once Undo is no longer offered

        ov = self.overlay
        ov.refine_requested.connect(self._on_refine_toggle)
        ov.organize_clicked.connect(self._on_organize_clicked)
        ov.change_folder_requested.connect(self._on_change_folder)
        ov.dismissed.connect(self._cleanup)
        for name, slot in (("undo_clicked", self.undo), ("confirm_clicked", lambda: self.answer(True)),
                           ("choice_made", self.choose), ("text_submitted", self.typed)):
            sig = getattr(ov, name, None)
            if sig is not None:
                sig.connect(slot)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_quit)

        # Engine signals — queued (they fire from worker threads).
        op = self.organize_page
        op.voice_plan_ready.connect(self._on_plan_ready, Qt.QueuedConnection)
        op.voice_plan_error.connect(self._on_plan_error, Qt.QueuedConnection)
        if hasattr(op, "voice_plan_replaced"):
            op.voice_plan_replaced.connect(self._on_plan_replaced)
        op.voice_apply_done.connect(self._on_apply_done, Qt.QueuedConnection)
        op.voice_apply_error.connect(self._on_apply_error, Qt.QueuedConnection)
        op.voice_status.connect(self._on_status, Qt.QueuedConnection)

    # ------------------------------------------------------------------ folder candidates
    def _folder_candidates(self):
        """Real folders the AI may choose from: Desktop / Downloads / Documents and their immediate subfolders
        (not hidden / system ones or junctions). Bounded so the prompt stays small."""
        from PySide6.QtCore import QStandardPaths as S
        roots = []
        for loc in (S.DesktopLocation, S.DownloadLocation, S.DocumentsLocation):
            p = S.writableLocation(loc)
            if p:
                roots.append(os.path.normpath(p))
        out, seen = [], set()

        def add(p):
            k = os.path.normcase(p)
            if k not in seen:
                seen.add(k)
                out.append(p)

        for root in roots:
            if not os.path.isdir(root):
                continue
            add(root)
            try:
                names = sorted(os.listdir(root), key=str.lower)
            except OSError:
                continue
            for name in names:
                if name.startswith("."):
                    continue
                sub = os.path.join(root, name)
                try:
                    if os.path.isdir(sub) and not _hidden_or_link(sub):
                        add(sub)
                except OSError:
                    continue
                if len(out) >= 400:
                    return out
        logger.info(f"[VOICE ORGANIZE] {len(out)} folder candidates")
        return out

    # ------------------------------------------------------------------ hotkey
    def prepare_new(self):
        """A NEW organize recording just started (before the transcript exists): clear the previous panel now, so
        the old 'pick a folder' card doesn't linger, and drop any resolve still in flight."""
        try:
            self._stop_finding()
            self._req_id += 1
            self._hf_stop()
            self._plan_active = False
            self._refining = False
            self.overlay.dismiss()
            logger.info("[VOICE ORGANIZE] prepare_new: cleared previous overlay")
        except Exception as e:
            logger.debug(f"[VOICE ORGANIZE] prepare_new failed: {e}")

    def start_from_transcript(self, text):
        """The organize hotkey's transcript: open the panel, let the AI pick the folder from what was said, then
        build the plan."""
        try:
            self._stop_finding()
            self._hf_stop()
            text = (text or "").strip()
            logger.info(f"[VOICE ORGANIZE] start_from_transcript: {text!r}")
            self.overlay.present()
            if not text:
                self.overlay.show_error("I didn't catch that — try again.")
                return
            if not self._plan_active:
                self._folder, self._last_how = "", ""
            self._last_instruction = text
            self.overlay.show_thinking("Finding the folder…")
            self._req_id += 1
            rid = self._req_id
            # (It says "none" about 1 time in 5 for an exact name when the sentence mentions a second folder, so it
            # asks again. Not while a plan is up: there "none" is normal, it means you're changing the plan.)
            worker = _FolderResolveWorker(text, self._folder_candidates(), tries=1 if self._plan_active else 3,
                                          want_how=True)
            self._resolve_worker = worker
            self._keep(worker)
            worker.done.connect(lambda path, t=text, r=rid, w=worker: self._on_folder_resolved(path, t, r, w.how),
                                Qt.QueuedConnection)
            worker.start()
        except Exception as e:
            logger.error(f"[VOICE ORGANIZE] start_from_transcript failed: {e}", exc_info=True)
            try:
                self.overlay.show_error(f"Something went wrong: {e}", allow_pick_folder=True)
            except Exception:
                pass

    def _on_folder_resolved(self, path, text, rid, how=None):
        if rid != self._req_id:
            logger.info(f"[VOICE ORGANIZE] ignoring stale folder resolve (#{rid} != #{self._req_id})")
            return
        path = (path or "").strip()
        if path and self._plan_active and not (
                self._said_folder(path, text) and re.match(r"\W*(?:organi[sz]e|sort|clean|tidy)\b", text, re.I)):
            path = ""                        # a plan is up and you didn't ask to organize another folder: a change
        if path:
            self._folder = path
            self._last_instruction = text
            self._last_how = text if how is None else how
            logger.info(f"[VOICE ORGANIZE] target folder = {path}, how = {self._last_how!r}")
            self.overlay.show_thinking("Analyzing your files…")
            # (The planner gets only how to sort: given the whole sentence, "organize the Reports folder" read as
            # "put everything in a folder called Reports" and nothing moved.)
            self.organize_page.voice_generate(self._folder, self._last_how, how_only=True)
            return
        if self._plan_active:
            logger.info("[VOICE ORGANIZE] no folder matched; refining the plan on screen")
            self.overlay.show_thinking("Updating the plan…")
            self.organize_page.voice_refine(text)
            return
        self.overlay.show_error("I couldn't tell which folder you meant. Say it again with its name, or pick one.",
                                allow_pick_folder=True)

    def _on_status(self, msg):
        """Transient status from the engine ('Indexing new files…', 'Planning 12 files…')."""
        if not self._plan_active:
            self.overlay.show_thinking(msg)

    # ------------------------------------------------------------------ recorder (refine by voice)
    def _start_recorder(self, on_text):
        from app.core.settings import settings
        from app.core.transcription import StreamingTranscriber
        try:
            terms = settings.live_dictation_custom_terms() or None
        except Exception:
            terms = None
        rec = StreamingTranscriber(language=getattr(settings, "dictation_language", None) or None, terms=terms)
        self._recorder = rec
        rec.level.connect(self.overlay.set_level, Qt.QueuedConnection)
        rec.finished.connect(lambda text, r=rec: self._recorder_done(r, on_text, text), Qt.QueuedConnection)
        rec.error.connect(lambda msg, r=rec: self._recorder_failed(r, msg), Qt.QueuedConnection)
        rec.start()

    def _recorder_done(self, rec, on_text, text):
        self._release_recorder(rec)
        on_text(text)

    def _recorder_failed(self, rec, msg):
        self._release_recorder(rec)
        self.overlay.show_error(msg)

    def _release_recorder(self, rec):
        """Disconnect a finished recorder and keep it referenced one slot longer (its signals fire from inside run()
        just before it returns; dropping the only reference could free the QThread mid-run)."""
        for sig in ("level", "finished", "error"):
            try:
                getattr(rec, sig).disconnect()
            except Exception:
                pass
        if self._recorder is rec:
            self._recorder = None
        self._graveyard = rec

    def _on_refine_toggle(self):
        if not self._refining:
            self._refining = True
            self.overlay.set_refining(True)
            self.overlay.show_listening("Listening… how should I change it?")
            self._start_recorder(self._on_refine_text)
        else:
            self._refining = False
            self.overlay.set_refining(False)
            self.overlay.show_thinking("Transcribing…")
            rec = self._recorder
            if rec is not None:
                rec.stop_recording()

    def _on_refine_text(self, text):
        self._refining = False
        self.overlay.set_refining(False)
        text = (text or "").strip()
        if text:
            if self._hf is not None and self._hf["stage"] == "preview":
                self.refine(text)
                return
            self.overlay.show_thinking("Updating the plan…")
            self.organize_page.voice_refine(text)
        else:
            logger.info("[VOICE ORGANIZE] refine captured no speech; back to the plan")
            show = getattr(self.overlay, "show_plan_page", None)
            if show is not None:
                show()

    # ------------------------------------------------------------------ engine results
    def _on_plan_replaced(self):
        """A newer plan took over from one still being worked out, so that one's result will never arrive (the Mac's
        trap: counted as "still coming", the NEXT real plan was taken for it and thrown away)."""
        self._hf_dead_plans = max(0, self._hf_dead_plans - 1)

    def _on_plan_ready(self, payload):
        if self._hf_dead_plans:              # for a voice-assistant run you cancelled while it was planning
            self._hf_dead_plans -= 1
            self._start_next()
            return
        if self._hf_next is not None and self._hf is None:    # (a voice request took over while this was planning)
            self._start_next()
            return
        if self._hf is not None:
            self._hf_on_plan(payload)
            return
        self._plan_active = True
        self.overlay.show_plan(payload.get("summary", ""), payload.get("folders", {}),
                               payload.get("target_folder", self._folder or ""), payload.get("file_count", 0),
                               payload.get("folder_count", 0), payload.get("move_count", None))

    def _on_plan_error(self, msg):
        if self._hf_dead_plans:
            self._hf_dead_plans -= 1
            self._start_next()
            return
        if self._hf_next is not None and self._hf is None:    # (the plan it waited for failed: it starts anyway)
            self._start_next()
            return
        self._plan_active = False
        if self._hf is not None:
            if self._hf.get("kind") == "auto" and ("Nothing needs moving" in msg or "nothing I can organize" in msg):
                self._auto_confirm()         # (already sorted: new files will be)
                return
            self._hf_fail(msg)
            return
        logger.info(f"[VOICE ORGANIZE] plan error: {msg}")
        self.overlay.show_error(msg, allow_pick_folder=True)

    def _on_organize_clicked(self):
        if self._hf is not None:
            self._hf["stage"] = "applying"
        self.overlay.show_applying()
        # Hands-free leaves emptied folders while Undo is on offer (see apply.tidy_emptied_sources).
        self.organize_page.voice_apply(keep_empty=self._hf is not None)

    def _on_apply_done(self, hint):
        self._plan_active = False
        logger.info(f"[VOICE ORGANIZE] apply done: {hint}")
        if self._hf is not None:
            self._hf_finished(hint)
            return
        self.overlay.show_done(hint)

    def _on_apply_error(self, msg):
        logger.info(f"[VOICE ORGANIZE] apply error: {msg}")
        if self._hf is not None:
            self._remember_undo()
            self._hf_fail(msg + (" Say “undo” to put them back." if self.can_undo() else ""))
            return
        self.overlay.show_error(msg)

    # ------------------------------------------------------------------ change folder
    def _on_change_folder(self):
        start = self._folder or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(None, "Choose folder to organize", start)
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        if self._hf is not None:
            self._hf["confirmed"] = True     # (you picked it: no "did you mean…?")
            self.overlay.present()
            self._hf_begin(chosen)
            return
        resolved_before = bool(self._folder)
        self._folder = chosen
        self.overlay.present()
        self.overlay.show_thinking("Analyzing your files…")
        if resolved_before:                  # (how to sort was already worked out from what you said)
            logger.info(f"[VOICE ORGANIZE] change-folder organizing with: {self._last_how!r}")
            self.organize_page.voice_generate(chosen, self._last_how, how_only=True)
            return
        instr = (self._last_instruction or "").strip() or "organize these files"
        logger.info(f"[VOICE ORGANIZE] change-folder organizing with: {instr!r}")
        self.organize_page.voice_generate(chosen, instr)

    # ------------------------------------------------------------------ teardown
    def _cleanup(self):
        rec = self._recorder
        if rec is not None:
            try:
                rec.stop_recording()
            except Exception:
                pass
        self._refining = False
        self._plan_active = False
        self._req_id += 1                    # any folder resolve in flight can't re-open a plan
        self._hf_next = None
        self._hf_stop()

    # ================================================================== voice assistant (hands-free)
    def _stop_finding(self):
        """Organizing takes the shared panel: a search on it stops (voice_find_controller)."""
        finder = getattr(self.main_window, "voice_find", None) if self.main_window is not None else None
        if finder is not None:
            try:
                finder.stop()
            except Exception as e:
                logger.info(f"[VOICE ORGANIZE] couldn't stop the search: {e}")

    def busy(self) -> bool:
        """Something is using the panel: this flow, or a plan open from the organize hotkey."""
        return bool((self._hf is not None and self._hf["stage"] not in ("done", "failed"))
                    or self._plan_active or self._recorder is not None)

    def panel_busy(self) -> bool:
        """The panel is organize's: busy(), or the organize hotkey still working out the folder or the plan (a search
        started then would be drawn over when the plan arrives)."""
        w = self._resolve_worker
        try:
            resolving = w is not None and w.isRunning()
        except RuntimeError:                 # (its C++ side is gone: it's done)
            resolving = False
        return self.busy() or resolving or self._page_working()

    def _page_working(self) -> bool:
        page = self.organize_page
        check = getattr(page, "voice_busy", None)
        return bool(check()) if callable(check) else False

    def start_hands_free(self, text, how: str = "", replace: bool = False, kind: str = "organize", mode=None) -> str:
        """The voice assistant heard "organize my Downloads". `text` is the whole sentence (it says which folder),
        `how` only how to sort it, if they said ("" = Filect decides). Returns "started", "busy" (one is running) or
        "page_busy" (a plan is open on the Organize page). `replace`: it takes over from one that's waiting or being
        read; only files physically moving make it wait. `kind` "auto": set up auto-organize (`mode` if they said
        which way)."""
        page = self.organize_page
        just_organized = self.can_undo() and self._last_folder
        active = self._hf is not None and self._hf["stage"] not in ("done", "failed")
        if replace and active:
            if self._hf["stage"] == "applying":
                return "busy"
            self._hf_stop()
            self._plan_active = False
            self.overlay.set_hands_free(True)
        if replace and (self._page_working() or self._hf_dead_plans):
            # A plan is still being worked out. Replacing its worker mid-run would crash, so this starts when it ends.
            self._stop_finding()             # (organizing takes the panel from a search)
            self._hf_next = (text, how, kind, mode)
            self.overlay.show_thinking("One moment…")
            return "started"
        if self.busy() or self._page_working() or self._hf_dead_plans:
            return "busy"                    # (a search on the panel stays: organize isn't taking it)
        has_plan = getattr(page, "has_plan", None)
        if callable(has_plan) and has_plan() and not getattr(page, "_voice_plan_owned", False):
            return "page_busy"
        self._stop_finding()                 # (organizing takes the panel from a search)
        from app.core.settings import settings
        tucked = not getattr(settings, "assistant_auto_open", True)
        self._hf = {"stage": "finding", "text": (text or "").strip(), "how": (how or "").strip(), "folder": "",
                    "tucked": tucked, "asked": "", "kind": kind, "mode": mode}
        logger.info(f"[VOICE ORGANIZE] hands-free ({kind}): {text!r} how={how!r} mode={mode!r}")
        self.overlay.set_hands_free(True)
        if not self.overlay.isVisible():     # (taking over from another: the panel's already there)
            self.overlay.present(tucked=tucked)
        from app.core.ai_organizer import sentence_names_a_folder
        if just_organized and how and kind == "organize" and not sentence_names_a_folder(text):
            self._hf["confirmed"] = True     # "put the invoices in Billing", right after: that same folder
            self._hf["only_these"] = True    # (…and only those files: the rest stay where it just put them)
            self._hf_begin(just_organized)
            return "started"
        self._hf_find_folder(self._hf["text"])
        return "started"

    def _start_next(self, tries: int = 20):
        """The voice request that waited for a plan to end starts now (Mac fix 5: before, it waited only for a
        CANCELLED hands-free plan — after any other it said "started" and never ran). A plan's result arrives a
        moment before its thread ends: while it's still running it tries again every 100 ms (up to 2 s)."""
        if self._hf_next is None or self._hf_dead_plans:
            return
        if self._page_working() and tries:
            QTimer.singleShot(100, lambda: self._start_next(tries - 1))
            return
        text, how, kind, mode = self._hf_next
        self._hf_next = None
        self._plan_active = False            # (it took over from whatever plan was up)
        self.start_hands_free(text, how=how, kind=kind, mode=mode)

    def _keep(self, thread):
        """A QThread destroyed while running aborts the app: hold it until it's finished."""
        self._hf_threads = [t for t in self._hf_threads if t.isRunning()] + [thread]

    def _hf_find_folder(self, text):
        self._hf["stage"] = "finding"
        self.overlay.show_thinking("Finding the folder…")
        self._req_id += 1
        rid = self._req_id
        cands = self._folder_candidates()
        # (It answers "none" about 1 time in 5 even for an exact folder name when the sentence mentions a second
        # folder. Hands-free can't re-press a key, so it asks again before asking you.)
        worker = _FolderResolveWorker(text, cands, tries=3)
        self._keep(worker)
        worker.done.connect(lambda path, r=rid, c=cands, t=text: self._hf_folder_found(path, r, c, t),
                            Qt.QueuedConnection)
        worker.start()

    def _hf_folder_found(self, path, rid, candidates, text):
        if rid != self._req_id or self._hf is None:
            return
        path = (path or "").strip()
        if not path:
            self._hf_ask_folder("Which folder should I organize? Say its name, or choose it below.")
            return
        same = [c for c in candidates if _base(c).lower() == _base(path).lower()]
        if len(same) > 1:                    # two "test" folders: only go on if you said which
            # (Compared without spaces: speech-to-text wrote "on my desk top" — seen in the live test.)
            squash = lambda s: re.sub(r"[\W_]+", "", s.lower())
            named = [c for c in same if squash(_base(os.path.dirname(c))) in squash(text)]
            if len(named) == 1:
                path = named[0]
            elif not named:
                places = " and ".join(_base(os.path.dirname(c)) for c in same)
                self._hf_ask_folder(f"There's more than one “{_base(path)}” folder, in {places}. Which one?")
                return
        if not self._said_folder(path, text):   # the AI guessed from a name that isn't there: nothing is read yet
            question = f"Did you mean the “{_base(path)}” folder?"
            self._hf.update(stage="ask_guess", asked=question, guess=path)
            self.overlay.ensure_open()
            self.overlay.show_choice(question, [("guess_yes", "Yes, organize it" if self._hf.get("kind") != "auto"
                                                 else "Yes, that one")], field=self._TYPE_FOLDER)
            self._say(question)
            return
        self._hf_begin(path)

    _TYPE_FOLDER = "Or type the folder's name and press Enter"

    @staticmethod
    def _said_folder(path, text):
        from app.core.ai_organizer import folder_is_named
        return folder_is_named(_base(path), text)

    def _hf_ask_folder(self, question):
        self._hf.update(stage="ask_folder", asked=question)
        self.overlay.ensure_open()
        self.overlay.show_choice(question, [("pick_folder", "📁  Choose folder…")], field=self._TYPE_FOLDER)
        self._say(question)

    def _hf_begin(self, folder):
        self._req_id += 1
        self._plan_active = False
        self._hf["folder"] = self._folder = os.path.normpath(str(folder))
        if self._hf.get("kind") == "auto":
            self._auto_begin()
            return
        self._hf_scan()

    def _hf_scan(self):
        folder = self._hf["folder"]
        self._req_id += 1
        self._hf["stage"] = "scanning"
        self.overlay.show_thinking(f"Looking through {self._hf_name()}…")
        worker = _ScanWorker(str(folder), self._req_id)
        self._keep(worker)
        worker.done.connect(self._hf_on_scan, Qt.QueuedConnection)
        worker.start()

    def _hf_name(self):
        return _base(self._hf["folder"]) if self._hf and self._hf.get("folder") else ""

    def _hf_on_scan(self, result, rid):
        if rid != self._req_id or self._hf is None:
            return
        from app.core.organize_reader import ASK_ABOVE, MAX_FILES
        err = result.get("error")
        if err:
            self._hf_fail({"project": "That folder is a code project, so I won't reorganize it.",
                           "too_many": f"That folder has more than {MAX_FILES:,} files. Try a smaller folder inside it.",
                           "missing": "That folder doesn't exist anymore."}.get(err, "I couldn't look through that folder."))
            return
        if not result["files"] and self._hf.get("kind") == "auto":
            self._auto_confirm()             # (nothing to sort yet: new files will be)
            return
        if not result["files"]:
            self._hf_fail("Everything in that folder is inside apps or code projects, so there's nothing I can move."
                          if result.get("skipped") else "That folder is empty, so there's nothing to organize.")
            return
        self._hf["unread"] = result["unread"]
        n = len(result["unread"])
        if n > ASK_ABOVE:
            question = (f"{n:,} files in {self._hf_name()} haven't been read yet. "
                        "Reading them could use a lot of your Claude limit.")
            self._hf.update(stage="ask_read", asked=question + " Should I go ahead?")
            self.overlay.ensure_open()
            self.overlay.show_confirm(question, f"Read {n:,} files")
            self._say(question + " Should I go ahead?")
        elif n:
            self._hf_read()
        else:
            self._hf_plan()

    def _agent_order(self):
        ap = getattr(self.main_window, "assistant_page", None)
        try:
            first = ap._pick_agent()
            return [a for a in (first, "codex" if first == "claude" else "claude") if ap._agent_ok(a)]
        except Exception:
            return ["claude", "codex"]

    def _hf_read(self):
        paths = self._hf["unread"]
        self._hf["stage"] = "reading"
        self.overlay.show_thinking(f"Reading {self._hf_name()}, 0 of {len(paths)}")
        worker = _ReadWorker(paths, self._agent_order(), self._req_id)
        self._reader = worker
        self._keep(worker)
        worker.progress.connect(self._hf_on_progress, Qt.QueuedConnection)
        worker.done.connect(self._hf_on_read, Qt.QueuedConnection)
        worker.start()

    def _hf_on_progress(self, done, total, rid):
        if rid == self._req_id and self._hf is not None and self._hf["stage"] == "reading":
            self.overlay.show_thinking(f"Reading {self._hf_name()}, {done} of {total}")

    def _hf_on_read(self, result, rid):
        if rid != self._req_id or self._hf is None or result.get("cancelled"):
            return
        self._reader = None
        logger.info(f"[VOICE ORGANIZE] read: {result}")
        self._hf_plan()

    def _hf_plan(self):
        from app.core.organize_reader import MAX_FILES
        self._hf["stage"] = "planning"
        self.overlay.show_thinking("Planning the folders…")
        how = self._hf["how"]
        if self._hf.get("only_these") and how:
            # (Seen live: "Now put the bills in a folder called Money" right after an organize moved ALL 4 files
            # into Money — the planner sorts whatever an instruction doesn't cover. Here it's about those files.)
            how = (f"{how.rstrip(' .')}. Only move the files this is about. Every other file stays in the folder it is in now: "
                   "give it its current folder name exactly.")
        if self._hf.get("kind") == "auto" and self._hf.get("mode") == "existing":
            from app.core.auto_watcher import existing_folders_instruction
            how = existing_folders_instruction(how, self._subfolders())
        self.organize_page.voice_generate(self._hf["folder"], how, _after_index=True, max_files=MAX_FILES,
                                          how_only=True)

    def _hf_on_plan(self, p):
        from app.core.settings import settings
        if self._hf["stage"] != "planning":
            return
        page = self.organize_page
        auto = self._hf.get("kind") == "auto"
        action = "Turn on and organize" if auto else "Organize"
        n = p.get("move_count")
        n = p.get("file_count", 0) if n is None else n
        m = p.get("folder_count", 0)
        before = self._hf.pop("before", None)
        if before is not None and before == (page.current_plan or {}).get("folders"):    # (asked again: same plan)
            self._plan_active = True
            self._hf.update(stage="preview", tucked=False,
                            asked="I couldn't make that change. Should I go ahead with this plan, or say it another way?")
            self.overlay.ensure_open()
            self._show_plan(p, action)
            self._say(self._hf["asked"])
            return
        if not n:                            # (Windows' planner reports this as a plan with nothing moving)
            self._on_plan_error("Nothing needs moving — those files look sorted already.")
            return
        sure = self._hf.get("confirmed") or self._said_folder(self._hf["folder"], self._hf["text"])
        if not getattr(settings, "live_organize_preview", True) and sure and not auto:   # (auto-organize always asks)
            self._on_organize_clicked()      # Preview off: straight to moving (Undo and History still work)
            return
        # (Preview off, but you never named or confirmed this folder: never move those files without showing you.)
        self._plan_active = True
        question = (f"I'd move {n} file{'s' if n != 1 else ''} into {m} folder{'s' if m != 1 else ''}"
                    + (", then keep this folder organized automatically" if auto else "") + ". Should I go ahead?")
        self._hf.update(stage="preview", asked=question, tucked=False)
        self.overlay.ensure_open()           # overrides "Open answers automatically": this needs you
        self._show_plan(p, action)
        self._say(question)

    def _show_plan(self, p, action):
        self.overlay.show_plan(p.get("summary", ""), p.get("folders", {}), p.get("target_folder", self._folder),
                               p.get("file_count", 0), p.get("folder_count", 0), p.get("move_count", None),
                               action=action)

    def _remember_undo(self):
        log = getattr(self.organize_page, "last_voice_log", "")
        if log:
            self._tidy()                     # (the one before: Undo moves on to this one)
            self._undo_log, self._undo_until = log, time.monotonic() + self.UNDO_SECS
            try:
                with open(log, encoding="utf-8") as fh:
                    moved = len(json.load(fh).get("moves", []))
            except (OSError, ValueError):
                moved = 0
            self._undo_info = (self._hf_name(), moved, time.time())
            self._undo_auto, self._last_folder = "", self._hf["folder"]
            self._tidy_pending = (log, self._hf["folder"])
            QTimer.singleShot(int(self.TIDY_SECS * 1000), lambda l=log: self._tidy(l))

    def _tidy(self, only=None):
        """Undo is no longer on offer: remove the folders that organize emptied."""
        log, root = self._tidy_pending
        if not log or (only and only != log):
            return
        self._tidy_pending = ("", "")
        try:
            from app.core.apply import tidy_emptied_sources
            tidy_emptied_sources(log, root)
        except Exception as e:
            logger.info(f"[VOICE ORGANIZE] couldn't tidy up: {e}")

    def _hf_finished(self, hint):
        self._remember_undo()
        self._hf["stage"] = "done"
        if self._hf.get("kind") == "auto":
            self._auto_turn_on(hint)
            return
        self.overlay.ensure_open()
        self.overlay.show_done(hint, undo=self.can_undo())
        self._say(_spoken(hint))

    def _hf_fail(self, msg):
        self._plan_active = False
        self._hf["stage"] = "failed"
        self.overlay.ensure_open()
        self.overlay.show_error(msg)
        self._say(msg)

    def _say(self, line):
        page = getattr(self.main_window, "live_page", None)
        if hasattr(page, "say_line"):
            try:
                page.say_line(line)
            except Exception as e:
                logger.info(f"[VOICE ORGANIZE] couldn't say it: {e}")

    # ------------------------------------------------------------------ auto-organize, set up by voice
    def _subfolders(self):
        from app.core.organize_reader import _dir_is_off_limits
        root = self._hf["folder"]
        try:
            return sorted((d for d in os.listdir(root)
                           if not d.startswith(".") and os.path.isdir(os.path.join(root, d))
                           and not _hidden_or_link(os.path.join(root, d))
                           and not _dir_is_off_limits(os.path.join(root, d))), key=str.lower)
        except OSError:
            return []

    def _auto_begin(self):
        from app.core.organize_reader import is_code_project
        if not os.path.isdir(self._hf["folder"]):
            self._hf_fail("That folder doesn't exist anymore.")
        elif is_code_project(self._hf["folder"]):
            self._hf_fail("That folder is a code project, so I won't auto-organize it.")
        elif self._hf.get("mode"):
            self._auto_mode_chosen(self._hf["mode"])
        else:
            self._auto_ask_mode()

    def _auto_ask_mode(self, first=""):
        name = self._hf_name()
        question = f"{first}How should I keep {name} organized?".strip()
        self._hf.update(stage="ask_mode", tucked=False, asked=(
            f"{question} Sort everything now plus new files, only use the folders already there, or only new files?"))
        self.overlay.ensure_open()
        self.overlay.show_choice(question, self.AUTO_CHOICES)
        self._say(self._hf["asked"])

    def choose(self, mode) -> bool:
        """One of the three ways, by button or by voice."""
        mode = str(mode or "").strip().lower()
        if mode == "none":                   # (the "no instructions" button)
            return self.no_rules()
        if mode == "guess_yes":              # ("did you mean…?")
            return self._hf is not None and self._hf["stage"] == "ask_guess" and self.answer(True)
        if mode == "pick_folder":            # ("which folder?")
            if self._hf is None or self._hf["stage"] != "ask_folder":
                return False
            self._on_change_folder()
            return True
        if self._hf is None or self._hf.get("kind") != "auto" or mode not in self.AUTO_ACTIONS or (
                self._hf["stage"] not in ("ask_mode", "ask_rules", "confirm_auto", "preview")):
            return False
        self._auto_mode_chosen(mode)
        return True

    def _auto_mode_chosen(self, mode):
        self._plan_active = False
        self._hf["mode"] = mode
        if mode == "existing" and not self._subfolders():
            self._auto_ask_mode(f"{self._hf_name()} doesn't have any folders in it yet. ")
        elif self._hf["how"] or self._hf.get("rules_answered"):
            self._auto_go()
        else:
            self._auto_ask_rules()

    def _auto_go(self):
        if self._hf["mode"] == "new":
            self._auto_confirm()
        else:
            self._hf_scan()                  # read, plan and show what would move

    def _auto_ask_rules(self):
        """They didn't say how to sort: ask, the way the Organize tab has a box for it. Saying nothing is fine."""
        question = f"Any instructions for how to sort {self._hf_name()}? Tell me, or say no and I'll decide."
        self._hf.update(stage="ask_rules", tucked=False, asked=question)
        self.overlay.ensure_open()
        self.overlay.show_choice(question, [("none", "No instructions, you decide")],
                                 field="Or type them here and press Enter")
        self._say(question)

    def _auto_rules_given(self, text=""):
        self._hf.update(how=(text or "").strip(), rules_answered=True)
        self._auto_go()

    def no_rules(self) -> bool:
        if self._hf is None or self._hf["stage"] != "ask_rules":
            return False
        self._auto_rules_given()
        return True

    def takes_yes_no(self) -> bool:
        """A bare "no" answers this question directly. (Not "any instructions?": there "no" means carry on.)"""
        return self.waiting_for_answer() and self._hf["stage"] != "ask_rules"

    def _auto_confirm(self):
        name, mode = self._hf_name(), self._hf.get("mode")
        keep = {"new": f"Files already in {name} stay where they are.",
                "existing": f"New files in {name} will only go into the folders already there."}.get(mode, "")
        how = self._hf["how"]
        rules = f'Your instructions: "{how[:60]}{"…" if len(how) > 60 else ""}". ' if how else ""
        question = f"From now on, new files in {name} will be sorted automatically. {rules}{keep}".strip()
        self._hf.update(stage="confirm_auto", tucked=False, asked=question + " Should I turn it on?")
        self.overlay.ensure_open()           # (always asks: this keeps running without you)
        self.overlay.show_confirm(question, "Turn on auto-organize")
        self._say(self._hf["asked"])

    def _auto_turn_on(self, moved_hint=""):
        hf, name = self._hf, self._hf_name()
        self.organize_page.voice_enable_watch(hf["folder"], hf["how"], self.AUTO_ACTIONS[hf["mode"] or "new"])
        if not moved_hint:
            self._undo_log, self._undo_info = "", (name, 0, time.time())
        self._undo_auto, self._undo_until = hf["folder"], time.monotonic() + self.UNDO_SECS
        self._last_folder = hf["folder"]
        hf["stage"] = "done"
        msg = (_spoken(moved_hint) + " " if moved_hint else "") + f"{name} will stay organized from now on."
        self.overlay.ensure_open()
        self.overlay.show_done(msg, undo=True)
        self._say(msg)

    def _notice(self, msg):
        self.overlay.set_hands_free(True)
        if not self.overlay.isVisible():
            self.overlay.present()
        self.overlay.ensure_open()
        self.overlay.show_done(msg)
        self._say(msg)

    _ALL_OF_THEM = frozenset({"both", "all", "every", "everything", "each", "them", "everyone", "everywhere"})

    def stop_auto(self, named="", asking=False) -> str:
        """Turn auto-organize off. `named` is the folder as they said it, or "" (`asking`: it's their answer to which
        one). Returns "auto_off", "auto_asked", "auto_none" or "organize_busy"."""
        from app.core.ai_organizer import _NOT_A_FOLDER_NAME, _fold
        from app.core.settings import settings
        if self._hf is not None and self._hf["stage"] == "applying":
            return "organize_busy"
        self._stop_finding()                 # (whatever it says next goes on the panel: a search there stops)
        watcher = getattr(self.organize_page, "auto_watcher", None)
        entries = [] if watcher is not None and not watcher.is_running else [     # (only what's actually running)
            f["path"] for f in settings.auto_organize_folders if f.get("path") and not f.get("paused")]
        if not entries:
            self._hf_stop()
            self._notice("Auto-organize isn't on for any folder.")
            return "auto_none"
        noise = set(_NOT_A_FOLDER_NAME) | {"folder", "folders", "one", "yes", "yeah", "it"}   # ("that folder")
        words = lambda t: {w[:-1] if w.endswith("s") else w for w in set(re.findall(r"[a-z0-9]+", _fold(t))) - noise}
        said = words(named or "")
        names = {p: words(_base(p)) for p in entries}
        if said and said <= self._ALL_OF_THEM and not any(w == said for w in names.values()):
            # "both" / "all of them" / "everything" (the 2026-11-10 hunt: "both" only asked again)
            self._hf_stop()
            for p in entries:
                self.organize_page.voice_disable_watch(p)
                if self._undo_auto and _same_path(self._undo_auto, p):
                    self._undo_auto = ""
            self._notice(f"Auto-organize is off for {' and '.join(_base(p) for p in entries)}. "
                         "Your files stay where they are.")
            return "auto_off"
        if said:
            hits = [p for p, w in names.items() if w == said] or [p for p, w in names.items() if said < w or w < said]
            if hits:                         # ("Inbox" is Filect Test Inbox; "Old Stuff" isn't Alpha Stuff)
                entries = hits
            elif not asking:                 # (asking: it didn't match, so ask again)
                self._hf_stop()
                self._notice(f"Auto-organize isn't on for {named}.")
                return "auto_none"
        if len(entries) != 1:
            self._hf_stop()
            names = " and ".join(_base(p) for p in entries)
            question = f"Which folder should I stop? {names}."
            self._hf = {"stage": "ask_stop", "kind": "stop", "text": named, "how": "", "folder": "", "tucked": False,
                        "asked": question, "mode": None}
            self.overlay.set_hands_free(True)
            if not self.overlay.isVisible():
                self.overlay.present()
            self.overlay.ensure_open()
            self.overlay.show_choice(question, [], field=self._TYPE_FOLDER)
            self._say(question)
            return "auto_asked"
        target = entries[0]
        self._hf_stop()
        self.organize_page.voice_disable_watch(target)
        if self._undo_auto and _same_path(self._undo_auto, target):
            self._undo_auto = ""
        self._notice(f"Auto-organize is off for {_base(target)}. Your files stay where they are.")
        return "auto_off"

    # ------------------------------------------------------------------ what the decider is told
    def decider_state(self):
        """What organizing is doing right now, for the decider: {"text", "allowed", "stage", "kind"}, or None if
        it's not in play. ("stage" / "kind" are for intent_router.organize_quick / organize_guard, which decide the
        plainly-said answers without the AI and catch the decider's misreads; the decider itself reads text and
        allowed.)"""
        st = self._decider_state()
        if st is not None:
            hf = self._hf
            live = hf is not None and hf["stage"] not in ("done", "failed")
            st["stage"] = hf["stage"] if live else "after"
            st["kind"] = hf.get("kind") if live else None
        return st

    def _decider_state(self):
        hf = self._hf
        if hf is None or hf["stage"] in ("done", "failed"):
            if not self.can_undo():
                return None
            name, n, when = self._undo_info
            about = (" Moving or re-sorting particular files ('put the invoice and the receipt in Billing') without naming "
                     "another folder means this same folder: that is ORGANIZE, with what they said as task, never a new chat.")
            if self._undo_auto:
                did = f" and moved {n} file{'s' if n != 1 else ''}" if n else ""
                return {"text": f"Filect turned on auto-organize for the {name} folder{did} {self._ago(when)} ago. "
                                f"It can be undone.{about}", "allowed": ["ORGANIZE_UNDO", "ORGANIZE"]}
            return {"text": f"Filect finished organizing the {name} folder ({n} file{'s' if n != 1 else ''} moved) "
                            f"{self._ago(when)} ago. It can be undone.{about}",
                    "allowed": ["ORGANIZE_UNDO", "ORGANIZE"]}
        stage = hf["stage"]
        name = f"the {self._hf_name()} folder" if hf["folder"] else "a folder"
        if stage == "ask_mode":
            return {"text": f"Filect asked how to set up auto-organize for {name}: everything (sort what's there now and "
                            "new files), existing (only into the folders already there), or new (only new files from now on).",
                    "allowed": ["ORGANIZE_CHOICE", "ORGANIZE_CANCEL", "ORGANIZE"]}
        if stage == "ask_rules":
            return {"text": f"Filect asked the user whether they have any instructions for how to sort {name}. Instructions they "
                            "give ('put the bills in a folder called Money', 'by year') are ORGANIZE_CHANGE, with the instructions as "
                            "task. A bare 'yes' or 'I do' without any instructions is IGNORE: they're about to say them. Switching which files it "
                            "covers (everything, existing = only the folders already there, new = only new files from now on) is ORGANIZE_CHOICE, "
                            "not instructions.",
                    "allowed": ["ORGANIZE_CHANGE", "ORGANIZE_NO_RULES", "ORGANIZE_CHOICE", "ORGANIZE_CANCEL", "ORGANIZE"]}
        if stage == "confirm_auto":
            return {"text": f"Filect is asking the user to confirm turning on auto-organize for {name}, which keeps running "
                            "on its own.", "allowed": ["ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE_CHOICE", "ORGANIZE"]}
        if stage == "ask_stop":
            return {"text": f"Filect asked the user: {hf['asked']}", "allowed": ["ORGANIZE_CANCEL", "ORGANIZE_CHANGE"]}
        if stage == "preview" and hf.get("kind") == "auto":
            return {"text": f"A preview of organizing {name}, after which auto-organize keeps running on it, is on screen, "
                            "waiting for the user's answer. Switching which files it covers (everything, existing = only the folders already "
                            "there, new = only new files from now on) is ORGANIZE_CHOICE, not a change to the plan.",
                    "allowed": ["ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE_CHOICE", "ORGANIZE"]}
        if stage == "preview":
            return {"text": f"A preview of organizing {name} is on screen, waiting for the user's answer.",
                    "allowed": ["ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"]}
        if stage == "ask_read":
            return {"text": f"Filect is asking whether to read the unread files in {name} before organizing it.",
                    "allowed": ["ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE"]}
        if stage == "ask_guess":
            return {"text": f"Filect asked the user: {hf['asked']}",
                    "allowed": ["ORGANIZE_GO", "ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"]}
        if stage == "ask_folder":
            return {"text": f"Filect asked the user: {hf['asked']}",
                    "allowed": ["ORGANIZE_CANCEL", "ORGANIZE_CHANGE", "ORGANIZE"]}
        if stage == "applying":
            return None                      # (moving files: a moment, and there's nothing to decide)
        return {"text": f"Filect is working on organizing {name} right now and hasn't moved anything yet.",
                "allowed": ["ORGANIZE_CANCEL", "ORGANIZE"]}

    @staticmethod
    def _ago(when):
        secs = max(0, time.time() - when)
        if secs < 60:
            return "less than a minute"
        minutes = int(secs // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"

    @property
    def finished_at(self):
        return self._undo_info[2]

    # ------------------------------------------------------------------ answers
    def cancel(self) -> bool:
        """Close whatever it's doing, at any stage except while files are physically moving."""
        if self._hf is None or self._hf["stage"] in ("done", "failed", "applying"):
            return False
        moved_nothing = "Okay, I left everything where it was."
        close = getattr(self.overlay, "_on_dismiss", None) or self.overlay.dismiss
        close()
        self._say(moved_nothing)
        return True

    def waiting_for_answer(self) -> bool:
        return self._hf is not None and self._hf["stage"] in (
            "preview", "ask_read", "ask_folder", "ask_guess", "ask_mode", "ask_rules", "confirm_auto", "ask_stop")

    def question(self) -> str:
        return self._hf["asked"] if self.waiting_for_answer() else ""

    def answer(self, yes: bool) -> bool:
        """A spoken yes / no, or the panel's button. False if that doesn't answer it ("yes" to "which folder?")."""
        if not self.waiting_for_answer():
            return False
        if not yes:
            return self.cancel()
        stage = self._hf["stage"]
        if stage == "preview":
            self._on_organize_clicked()
        elif stage == "ask_read":
            self.overlay.show_thinking(f"Reading {self._hf_name()}…")
            self._hf_read()
        elif stage == "ask_guess":
            self._hf["confirmed"] = True
            self._hf_begin(self._hf["guess"])
        elif stage == "confirm_auto":
            self._auto_turn_on()
        else:
            return False
        return True

    def refine_fits(self, sentence: str) -> bool:
        """The decider said "organize a different folder", but this is really an answer to what's on screen: a
        change to the plan / the instructions that names no folder, or an answer to "which folder?" (which may name
        a place: refine reads it together with the first sentence)."""
        if not self.waiting_for_answer():
            return False
        stage = self._hf["stage"]
        if stage in ("ask_folder", "ask_guess"):
            return True
        if stage in ("preview", "ask_rules"):
            from app.core.ai_organizer import sentence_names_a_folder
            return not sentence_names_a_folder(sentence or "")
        return False

    def typed(self, text: str) -> bool:
        """Typed into the panel's box and Enter pressed. Read the way a spoken answer is, without the AI: "yes",
        "ok", "cancel", "nvm" do what they say (never looked up as a folder called "yes", or sent as a change to the
        plan, or saved as auto-organize rules); a folder's exact name or its full path (Explorer's "Copy as path"
        puts quotes round it) is that folder. Anything else is the answer in your own words (refine)."""
        text = (text or "").strip()
        if not text or not self.waiting_for_answer():
            return False
        stage = self._hf["stage"]
        logger.info(f"[VOICE ORGANIZE] typed at {stage}: {text!r}")
        if stage in ("ask_folder", "ask_guess", "ask_stop"):
            path = _typed_folder(text, self._folder_candidates() if stage != "ask_stop" else ())
            if path and stage == "ask_stop":
                return self.stop_auto(_base(path), asking=True) is not None
            if path:
                self._hf["confirmed"] = True     # (you typed it: no "did you mean…?")
                self._hf_begin(path)
                return True
        from app.core.intent_router import organize_quick
        d = organize_quick(text, self.decider_state())
        if d is not None:
            intent = d["intent"]
            if intent == "ORGANIZE_CANCEL":
                return self.cancel()
            if intent == "ORGANIZE_GO":
                return self.answer(True) or True
            if intent == "ORGANIZE_CHOICE":
                return self.choose(d.get("task"))
            if intent == "ORGANIZE_NO_RULES":
                return self.no_rules()
            if intent == "IGNORE":
                return True
        t = " ".join(re.sub(r"[^a-z' ]", " ", text.lower()).split())
        if t in _TYPED_YES:
            return self.answer(True) or True     # ("yes" to "which folder?" answers nothing: wait for the name)
        if t in _TYPED_NO:
            return self.no_rules() if stage == "ask_rules" else self.cancel()
        return self.refine(text)

    def refine(self, text: str) -> bool:
        """Something other than yes / no while it's waiting: a change to the plan, or which folder. False if there's
        nothing to change (it's only asking whether to read a big folder)."""
        if not self.waiting_for_answer():
            return False
        text = (text or "").strip()
        stage = self._hf["stage"]
        if stage == "ask_rules":
            self._auto_rules_given(text)
            return True
        if not text:
            return False
        if stage == "preview":
            if self._hf.get("kind") == "auto":   # (it keeps running: new files should follow this too)
                self._hf["how"] = ". ".join(x for x in (self._hf["how"], text) if x)
            self._plan_active = False
            page = self.organize_page
            self._hf.update(stage="planning", before=(page.current_plan or {}).get("folders"))
            self.overlay.show_thinking("Updating the plan…")
            page.voice_refine(text)
            return True
        if stage in ("ask_folder", "ask_guess"):
            self._hf_find_folder(f"{self._hf['text']}. {text}")
            return True
        if stage == "ask_stop":
            self.stop_auto(text, asking=True)
            return True
        return False

    # ------------------------------------------------------------------ undo
    def can_undo(self) -> bool:
        return bool(self._undo_log or self._undo_auto) and time.monotonic() < self._undo_until and not (
            self._hf is not None and self._hf["stage"] not in ("done", "failed"))

    def undo(self) -> bool:
        """Put the last hands-free organize back: the Undo button, or saying "undo"."""
        if not self.can_undo():
            return False
        self._stop_finding()                 # (the panel shows the undo now)
        log, self._undo_log = self._undo_log, ""
        auto, self._undo_auto = self._undo_auto, ""
        self._undone_auto = bool(auto)
        if auto:                             # (off first, or the watcher would sort them straight back)
            self.organize_page.voice_disable_watch(auto)
        self._tidy_pending = ("", "")
        if not self.overlay.isVisible():
            self.overlay.present()
        self.overlay.ensure_open()
        if not log:
            msg = f"Okay, auto-organize is off for {self._undo_info[0]}."
            self.overlay.show_done(msg)
            self._say(msg)
            return True
        self.overlay.show_thinking("Putting your files back…")
        worker = _UndoWorker(log)
        self._keep(worker)
        worker.done.connect(self._on_undone, Qt.QueuedConnection)
        worker.start()
        return True

    def _on_undone(self, r):
        n, skipped = r["reverted"], r["skipped"] + len(r["errors"])
        msg = f"Put {n} file{'s' if n != 1 else ''} back where {'they were' if n != 1 else 'it was'}."
        if skipped:
            msg += f" {skipped} couldn't go back because {'they' if skipped != 1 else 'it'} moved or changed."
        if self._undone_auto:
            msg += " Auto-organize is off again."
        try:
            refresh = getattr(self.organize_page, "_refresh_history", None)
            if callable(refresh):
                refresh()
        except Exception:
            pass
        self.overlay.show_done(msg)
        self._say(msg)

    def _hf_stop(self):
        """Closed, or the hotkey took over: stop reading now. A move already under way finishes (and is undoable)."""
        hf, reader = self._hf, self._reader
        if reader is not None:
            reader.cancelled.set()
            self._reader = None
        if hf is not None and hf["stage"] == "applying":
            return
        if hf is not None and hf["stage"] == "planning":
            self._hf_dead_plans += 1
        self._hf = None
        self._hf_next = None                 # (the hotkey / a close is newer than whatever was waiting)
        try:
            self.overlay.set_hands_free(False)
        except Exception:
            pass

    def _on_quit(self):
        self._tidy()
        reader = self._reader
        if reader is not None:
            reader.cancelled.set()
            reader.wait(4000)
