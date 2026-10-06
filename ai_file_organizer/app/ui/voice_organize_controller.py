"""
Voice Organize controller — wiring between the hotkey/voice transcript, the
AI folder resolver, the organize engine (organize_page voice_* methods), and
the floating OrganizeOverlay.

Flow:
  start_from_transcript(text)                         [text already transcribed
    -> resolve target folder (candidate list -> AI)    by the dictation layer]
    -> organize_page.voice_generate(folder, text)      [plan off-thread]
    -> voice_plan_ready -> overlay.show_plan(...)
    -> refine (voice)  -> voice_refine -> plan (loop)
       change folder   -> QFileDialog -> voice_generate
       Organize        -> voice_apply -> done

The target folder is chosen by an LLM from a bounded candidate list (the
Windows known folders + their immediate subdirs) — NOT from Explorer's current
directory. See resolve_folder_with_ai.
"""
import logging

from PySide6.QtCore import QObject, QThread, Signal, Qt

from app.ui.organize_overlay import OrganizeOverlay

logger = logging.getLogger(__name__)


class _FolderResolveWorker(QThread):
    done = Signal(str, str, int)   # (resolved_path_or_empty, text, req_id)

    def __init__(self, text: str, candidates: list, req_id: int):
        super().__init__()
        self.text = text
        self.candidates = candidates
        self.req_id = req_id

    def run(self):
        try:
            from app.core.ai_organizer import resolve_folder_with_ai
            path = resolve_folder_with_ai(self.text, self.candidates) or ""
        except Exception as e:
            logger.error(f"[VOICE ORGANIZE] resolve worker failed: {e}")
            path = ""
        self.done.emit(path, self.text, self.req_id)


class VoiceOrganizeController(QObject):
    def __init__(self, organize_page, main_window=None):
        super().__init__()
        self.organize_page = organize_page
        self.main_window = main_window
        self._folder = None
        self._last_text = ""   # most recent spoken instruction (for change-folder)
        self._req_id = 0
        self._plan_showing = False
        self._refining = False
        self._recorder = None
        self._graveyard = []   # keep finished recorder threads alive for signal delivery

        self.overlay = OrganizeOverlay()
        self.overlay.refine_requested.connect(self._on_refine_toggle)
        self.overlay.organize_clicked.connect(self._on_organize_clicked)
        self.overlay.change_folder_requested.connect(self._on_change_folder)
        self.overlay.dismissed.connect(self._cleanup)

        # Engine signals — queued (they fire from worker threads).
        op = self.organize_page
        op.voice_plan_ready.connect(self._on_plan_ready, Qt.QueuedConnection)
        op.voice_plan_error.connect(self._on_plan_error, Qt.QueuedConnection)
        op.voice_apply_done.connect(self._on_apply_done, Qt.QueuedConnection)
        op.voice_apply_error.connect(self._on_apply_error, Qt.QueuedConnection)
        op.voice_status.connect(self._on_status, Qt.QueuedConnection)

    # ----- folder candidates (Windows known folders + immediate subdirs) -----
    def _folder_candidates(self):
        """Real folders the AI may choose from: Desktop / Downloads / Documents
        (each root) + their non-dot immediate subdirs. Hard cap 400 to keep the
        prompt small. This is the bounded list the LLM matches the request to —
        no Explorer active-folder query."""
        from PySide6.QtCore import QStandardPaths as S
        from pathlib import Path
        roots = []
        for loc in (S.DesktopLocation, S.DownloadLocation, S.DocumentsLocation):
            p = S.writableLocation(loc)
            if p:
                roots.append(Path(p))
        candidates, seen = [], set()
        for root in roots:
            if not root.exists():
                continue
            rp = str(root)
            if rp not in seen:
                candidates.append(rp); seen.add(rp)
            try:
                for child in sorted(root.iterdir()):
                    if len(candidates) >= 400:
                        break
                    if child.is_dir() and not child.name.startswith('.'):
                        cp = str(child)
                        if cp not in seen:
                            candidates.append(cp); seen.add(cp)
            except Exception:
                pass
            if len(candidates) >= 400:
                break
        logger.info(f"[VOICE ORGANIZE] {len(candidates)} folder candidates")
        return candidates

    def prepare_new(self):
        """Called the instant a NEW organize recording starts (before the
        transcript exists), so a leftover popup from the previous request — e.g.
        the 'couldn't find that folder, pick one' error — is cleared right away
        and there's space for the new one. Invalidates any in-flight resolve."""
        try:
            self._req_id += 1          # stale-guard any pending resolve worker
            self._plan_showing = False
            self._refining = False
            self.overlay.dismiss()
            logger.info("[VOICE ORGANIZE] prepare_new: cleared previous overlay")
        except Exception as e:
            logger.debug(f"[VOICE ORGANIZE] prepare_new failed: {e}")

    # ----- entry -----
    def start_from_transcript(self, text: str):
        try:
            text = (text or "").strip()
            logger.info(f"[VOICE ORGANIZE] start_from_transcript: {text!r}")
            self.overlay.present()
            if not text:
                self.overlay.show_error("I didn't catch that — try again.")
                return
            # Remember the spoken instruction so that if the folder can't be
            # resolved and the user picks one by hand, we organize with THEIR
            # instruction — not a generic fallback that invents folders.
            self._last_text = text
            self.overlay.show_thinking("Finding the folder…")
            self._req_id += 1
            rid = self._req_id
            self._resolve_worker = _FolderResolveWorker(text, self._folder_candidates(), rid)
            self._resolve_worker.done.connect(self._on_folder_resolved, Qt.QueuedConnection)
            self._resolve_worker.start()
        except Exception as e:
            logger.error(f"[VOICE ORGANIZE] start_from_transcript failed: {e}", exc_info=True)
            try:
                self.overlay.show_error(f"Something went wrong: {e}", allow_pick_folder=True)
            except Exception:
                pass

    def _on_folder_resolved(self, path: str, text: str, rid: int):
        if rid != self._req_id:
            return  # stale
        if path:
            self._folder = path
            logger.info(f"[VOICE ORGANIZE] resolved folder: {path}")
            self.overlay.show_thinking("Analyzing your files…")
            self.organize_page.voice_generate(path, text)
        elif self._plan_showing:
            # No folder, but a plan is up -> treat the utterance as a refinement.
            logger.info("[VOICE ORGANIZE] no folder but plan showing -> refine")
            self.overlay.show_thinking("Refining the plan…")
            self.organize_page.voice_refine(text)
        else:
            self.overlay.show_error(
                "I couldn't tell which folder you meant. Pick one to organize.",
                allow_pick_folder=True)

    # ----- engine results -----
    def _on_plan_ready(self, payload: dict):
        self._plan_showing = True
        self.overlay.show_plan(
            payload.get("summary", ""),
            payload.get("folders", {}),
            payload.get("target_folder", self._folder or ""),
            payload.get("file_count", 0),
            payload.get("folder_count", 0),
            payload.get("move_count", None),
        )

    def _on_plan_error(self, msg: str):
        self._plan_showing = False
        self.overlay.show_error(msg, allow_pick_folder=True)

    def _on_status(self, msg: str):
        # Only reflect status while not already showing a plan.
        if not self._plan_showing:
            self.overlay.show_thinking(msg)

    def _on_apply_done(self, hint: str):
        self._plan_showing = False
        self.overlay.show_done(hint)

    def _on_apply_error(self, msg: str):
        self.overlay.show_error(msg)

    # ----- user actions -----
    def _on_organize_clicked(self):
        self.overlay.show_applying()
        self.organize_page.voice_apply()

    def _on_change_folder(self):
        from PySide6.QtWidgets import QFileDialog
        from pathlib import Path
        start = self._folder or str(Path.home())
        folder = QFileDialog.getExistingDirectory(None, "Choose Folder to Organize", start)
        if folder:
            self._folder = folder
            self.overlay.present()
            self.overlay.show_thinking("Analyzing your files…")
            # Use the ORIGINAL spoken instruction (stored on the controller),
            # falling back to the engine's last instruction, then a generic one.
            instr = (getattr(self, "_last_text", "") or "").strip() \
                or getattr(self.organize_page, "original_instruction", None) \
                or "organize these files"
            logger.info(f"[VOICE ORGANIZE] change-folder organizing with: {instr!r}")
            self.organize_page.voice_generate(folder, instr)

    # ----- refine (voice re-record) -----
    def _on_refine_toggle(self):
        if self._refining:
            # stop
            self._refining = False
            self.overlay.set_refining(False)
            self.overlay.show_thinking("Transcribing…")
            if self._recorder:
                self._recorder.stop_recording()
        else:
            # start
            self._refining = True
            self.overlay.set_refining(True)
            self.overlay.show_listening("Listening… how should I change it?")
            self._start_recorder(self._on_refine_text)

    def _on_refine_text(self, text: str):
        text = (text or "").strip()
        logger.info(f"[VOICE ORGANIZE] refine text: {text!r}")
        if not text:
            # Nothing said — re-show the existing plan.
            self._plan_showing = True
            # The engine still holds current_plan; just re-render via a no-op refine.
            return
        self.overlay.show_thinking("Refining the plan…")
        self.organize_page.voice_refine(text)

    def _start_recorder(self, on_text):
        from app.core.transcription import StreamingTranscriber
        rec = StreamingTranscriber()
        rec.level.connect(self.overlay.set_level)

        def _finish(t):
            self._graveyard.append(rec)
            on_text(t)
        rec.finished.connect(_finish)
        rec.error.connect(lambda m: self.overlay.show_error(m))
        self._recorder = rec
        rec.start()

    # ----- cleanup -----
    def _cleanup(self):
        self._plan_showing = False
        self._refining = False
        if self._recorder:
            try:
                self._recorder.stop_recording()
            except Exception:
                pass
