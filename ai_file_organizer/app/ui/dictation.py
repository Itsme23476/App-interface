"""
Dictation controller — ties the hold-to-talk hotkey to the transcriber and
routes the final transcript by mode.

Flow:
  hotkey.dictate_start(mode) -> start StreamingTranscriber + show overlay
  (while held)               -> recorder.level -> overlay waveform
  hotkey.dictate_stop        -> recorder.stop_recording() + "Transcribing…"
  recorder.finished(text)    -> route by mode:
        dictate  -> paste into the focused app (clipboard + synth Ctrl+V)
        search   -> (C6) run in-app search with the text
        organize -> (C6) run voice-organize with the text

Everything logs under [DICTATION] so the whole flow is traceable from the app
log without the user narrating.
"""
import logging

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from app.core.settings import settings
from app.ui.dictation_hotkey import DictationHotkey, DEFAULT_TRIGGER
from app.ui.dictation_overlay import DictationOverlay

logger = logging.getLogger(__name__)


def _send_ctrl_v():
    """Synthesize Ctrl+V via user32.keybd_event (ARM64-safe; works where the
    low-level HOOK libs don't, same as GetAsyncKeyState/RegisterHotKey)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        VK_CONTROL, VK_V, KEYEVENTF_KEYUP = 0x11, 0x56, 0x0002
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        logger.info("[DICTATION] synthesized Ctrl+V")
        return True
    except Exception as e:
        logger.error(f"[DICTATION] Ctrl+V synth failed: {e}")
        return False


class DictationController(QObject):
    # Emitted with (text, mode) the moment a transcript is ready, before
    # routing (paste/search/organize). Lets the UI show what was heard and
    # makes the pipeline testable via a button (no global hotkey needed).
    transcript_ready = Signal(str, str)
    # Live mic amplitude 0..1 (re-emitted from the recorder) for the waveform.
    level = Signal(float)
    # State changes so the UI can show/hide the waveform: 'listening' | 'idle'.
    state_changed = Signal(str)

    def __init__(self, main_window=None):
        super().__init__()
        self.main_window = main_window
        self._recorder = None
        self._mode = "dictate"
        self._overlay = DictationOverlay()

        trigger = getattr(settings, "dictation_trigger", None) or DEFAULT_TRIGGER
        self.hotkey = DictationHotkey(trigger=trigger)
        self.hotkey.dictate_start.connect(self._on_dictate_start)
        self.hotkey.dictate_stop.connect(self._on_dictate_stop)
        logger.info(f"[DICTATION] controller ready (trigger='{self.hotkey.trigger_name}')")

    def start(self):
        self.hotkey.start()
        logger.info("[DICTATION] listening for hotkey")

    def stop(self):
        self.hotkey.stop()
        if self._recorder:
            self._recorder.stop_recording()

    def set_trigger(self, trigger: str):
        self.hotkey.set_trigger(trigger)
        logger.info(f"[DICTATION] trigger changed to '{self.hotkey.trigger_name}'")

    # ----- manual push-to-talk (button-driven; no global hotkey) -----
    def begin_dictation(self, mode: str = "dictate"):
        """Start dictation from a UI button (press). Same path as the hotkey."""
        self._on_dictate_start(mode)

    def end_dictation(self):
        """Stop dictation from a UI button (release)."""
        self._on_dictate_stop()

    # ----- hotkey gesture -----
    def _on_dictate_start(self, mode: str):
        if self._recorder is not None:
            logger.warning("[DICTATION] start ignored — a recording is already active")
            return
        self._mode = mode
        logger.info(f"[DICTATION] ▶ start mode={mode}")

        from app.core.transcription import StreamingTranscriber
        terms = self._custom_words()
        language = getattr(settings, "dictation_language", None) or None
        self._recorder = StreamingTranscriber(language=language, terms=terms)
        self._recorder.level.connect(self._overlay.set_level)
        self._recorder.level.connect(self.level)   # re-emit for the Voice page waveform
        self._recorder.recording_stopped.connect(
            lambda: logger.info("[DICTATION] recorder: recording_stopped"))
        self._recorder.finished.connect(self._on_finished)
        self._recorder.error.connect(self._on_error)
        self._recorder.capped.connect(
            lambda: logger.warning("[DICTATION] recorder: hit 10-min cap"))
        # Start the MIC FIRST — before any UI work — so not one opening word is
        # lost to the overlay/waveform show. The visuals come immediately after;
        # the mic is already capturing by then.
        self._recorder.start()
        self._overlay.show_listening(mode)
        self.state_changed.emit("listening")

    def _on_dictate_stop(self):
        logger.info("[DICTATION] ■ stop (awaiting transcript)")
        self._overlay.show_transcribing()
        if self._recorder:
            self._recorder.stop_recording()

    # ----- transcript result -----
    def _on_finished(self, text: str):
        text = (text or "").strip()
        logger.info(f"[DICTATION] transcript (mode={self._mode}, {len(text)} chars): {text[:120]!r}")
        self._overlay.hide_pill()
        self.state_changed.emit("idle")

        # Optional polishing (Voice "Polishing" setting).
        level = getattr(settings, "dictation_cleanup_level", "off")
        if text and level in ("light", "polished"):
            try:
                from app.core.transcription import clean_transcript
                before = text
                text = clean_transcript(text, level=level, terms=self._custom_words())
                if text != before:
                    logger.info(f"[DICTATION] polished ({level}): {text[:120]!r}")
            except Exception as e:
                logger.warning(f"[DICTATION] polish failed: {e}")

        self._recorder = None
        # Notify listeners (UI) with the final text + mode before routing.
        try:
            self.transcript_ready.emit(text, self._mode)
        except Exception:
            pass
        if not text:
            logger.info("[DICTATION] empty transcript — nothing to do")
            return

        if self._mode == "search":
            self._route_search(text)
        elif self._mode == "organize":
            self._route_organize(text)
        else:
            self._paste(text)

        self._save_history(text, self._mode)

    def _on_error(self, message: str):
        logger.error(f"[DICTATION] error: {message}")
        self._overlay.hide_pill()
        self.state_changed.emit("idle")
        self._recorder = None

    # ----- routing -----
    def _paste(self, text: str):
        """Put text on the clipboard and synth Ctrl+V into the focused app,
        restoring the previous clipboard afterward."""
        logger.info(f"[DICTATION] pasting {len(text)} chars into focused app")
        try:
            cb = QApplication.clipboard()
            prev = cb.text()
            cb.setText(text)
            # Small delay so the overlay is fully hidden + focus is stable.
            QTimer.singleShot(90, lambda: self._do_paste_and_restore(cb, prev))
        except Exception as e:
            logger.error(f"[DICTATION] clipboard paste failed: {e}")

    def _do_paste_and_restore(self, cb, prev):
        _send_ctrl_v()
        QTimer.singleShot(400, lambda: self._restore_clipboard(cb, prev))

    def _restore_clipboard(self, cb, prev):
        try:
            cb.setText(prev)
            logger.debug("[DICTATION] clipboard restored")
        except Exception:
            pass

    def _route_search(self, text: str):
        """Voice search: distill the spoken sentence into keywords and run it in
        the Quick Search popup overlay (the floating preview, more practical
        than the full Search tab). Falls back to the Search page if the overlay
        isn't available."""
        mw = self.main_window
        if mw is None:
            logger.warning("[DICTATION] no main_window — search route skipped")
            return
        try:
            from app.core.transcription import distill_search_query
            query = distill_search_query(text) or text
            logger.info(f"[DICTATION] route SEARCH: spoken={text[:60]!r} -> query={query!r}")

            overlay = getattr(mw, "quick_overlay", None)
            if overlay is not None and hasattr(overlay, "run_voice_query"):
                # Mac-parity: show the spoken sentence, search the distilled
                # keywords, and make picking a result OPEN the file.
                overlay.run_voice_query(text, query)
                logger.info("[DICTATION] search shown in Quick Search popup (voice)")
                return

            # Fallback: full Search page.
            logger.info("[DICTATION] quick_overlay unavailable — using Search page")
            try:
                mw._on_nav_clicked(0)
                if hasattr(mw, "nav_buttons") and mw.nav_buttons:
                    mw.nav_buttons[0].setChecked(True)
            except Exception:
                pass
            if hasattr(mw, "search_input"):
                mw.search_input.setText(query)
            if hasattr(mw, "search_files"):
                mw.search_files()
        except Exception as e:
            logger.error(f"[DICTATION] search route failed: {e}")

    def _route_organize(self, text: str):
        logger.info(f"[DICTATION] route ORGANIZE: {text[:80]!r}")
        # Wired fully in C6 (voice-organize controller).

    # ----- helpers -----
    def _custom_words(self):
        try:
            words = getattr(settings, "dictation_custom_words", None) or []
            return list(words) if words else None
        except Exception:
            return None

    def _save_history(self, text: str, mode: str):
        try:
            hist = list(getattr(settings, "dictation_history", []) or [])
            hist.insert(0, {"text": text, "mode": mode})
            if hasattr(settings, "set_dictation_history"):
                settings.set_dictation_history(hist[:100])
        except Exception as e:
            logger.debug(f"[DICTATION] history save skipped: {e}")
