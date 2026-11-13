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
        self.organize_controller = None   # injected by main_window (VoiceOrganizeController)
        self._recorder = None
        self._mode = "dictate"
        self._overlay = DictationOverlay()

        trigger = getattr(settings, "dictation_trigger", None) or DEFAULT_TRIGGER
        self.hotkey = DictationHotkey(trigger=trigger)
        self.hotkey.dictate_start.connect(self._on_dictate_start)
        self.hotkey.dictate_stop.connect(self._on_dictate_stop)
        self.hotkey.mute_toggle.connect(self._on_mute_keys)
        logger.info(f"[DICTATION] controller ready (trigger='{self.hotkey.trigger_name}')")
        try:                                  # so the first dictation already reads the caret context
            from app.ui.win_hotkey import warm_text_before_caret
            warm_text_before_caret()
        except Exception:
            pass

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
    def _on_mute_keys(self):
        """Shift, then Ctrl + Win: mute or unmute the hands-free Assistant (the page says "The Assistant isn't on"
        when it's off). Never in the middle of a capture: that press does nothing."""
        from app.core import mic_helper
        if self._recorder is not None or mic_helper.busy():
            logger.info("[DICTATION] Shift, then the shortcut, during a capture — ignored")
            return
        page = getattr(self.main_window, "live_page", None)
        if hasattr(page, "toggle_mute"):
            page.toggle_mute()

    def _on_dictate_start(self, mode: str):
        if self._recorder is not None:
            logger.warning("[DICTATION] start ignored — a recording is already active")
            return
        self._mode = mode
        self._caret = None                    # (the caret read of an earlier dictation never carries over)
        logger.info(f"[DICTATION] ▶ start mode={mode}")

        # A new organize request: clear any leftover organize popup NOW (before
        # recording), so the old 'pick a folder' card doesn't linger over the new
        # one. The fresh overlay is presented later from the transcript.
        if mode == "organize" and self.organize_controller is not None:
            try:
                self.organize_controller.prepare_new()
            except Exception:
                pass

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
        if self._recorder is None:            # (it already ended: an error, e.g. the daily limit, said so)
            logger.info("[DICTATION] ■ stop — nothing is recording")
            return
        logger.info("[DICTATION] ■ stop (awaiting transcript)")
        self._overlay.show_transcribing()
        # Read what's before the cursor NOW, while the words are being finished: it can't change meanwhile, and read
        # when the text arrived it sometimes ran out of time (live 2026-11-13: 0.405 s → "unreadable", so no space
        # and no casing for that dictation, and 0.4 s slower).
        try:
            from app.ui.win_hotkey import start_text_before_caret
            self._caret = start_text_before_caret()
        except Exception:
            self._caret = None
        if self._recorder:
            self._recorder.stop_recording()

    # ----- transcript result -----
    def _on_finished(self, text: str):
        text = (text or "").strip()

        logger.info(f"[DICTATION] transcript (mode={self._mode}, {len(text)} chars): {text[:120]!r}")
        self._overlay.hide_pill()
        self.state_changed.emit("idle")

        # Optional polishing (Voice "AI Cleanup" setting).
        level = getattr(settings, "dictation_polish_level", "none")
        if text and level in ("light", "polished"):
            try:
                from app.core.transcription import clean_transcript
                before = text
                text = clean_transcript(text, level=level, terms=self._custom_words())
                if text != before:
                    logger.info(f"[DICTATION] polished ({level}): {text[:120]!r}")
            except Exception as e:
                logger.warning(f"[DICTATION] polish failed: {e}")

        # Deterministic formatting — runs on the FINAL text (raw OR polished) so
        # output is identical regardless of the AI Cleanup setting: spoken
        # numbers/currency/percent -> written form, whitespace/punctuation tidy,
        # and context-aware sentence casing. apply_sentence_case safely no-ops
        # when the caret context can't be read (text_before_caret() -> None).
        before = None
        if text:
            try:
                from app.core.dictation_format import (format_spoken, tidy_text, apply_sentence_case,
                                                       fix_known_addresses)
                before = None
                import time as _time
                job, self._caret = getattr(self, "_caret", None), None
                try:
                    if job is not None:               # (started at key release: 0.8 s from then in all)
                        before = job(0.8 - (_time.perf_counter() - job.started))
                        took = round((_time.perf_counter() - job.started) * 1000)
                    else:
                        from app.ui.win_hotkey import text_before_caret
                        t0 = _time.perf_counter()
                        before = text_before_caret()
                        took = round((_time.perf_counter() - t0) * 1000)
                except Exception:
                    before, took = None, -1
                _ctx = before.rstrip(" \t") if before is not None else None
                logger.info("[DICTATION] caret context: " + (
                    "unreadable" if _ctx is None else
                    "sentence start" if (not _ctx or _ctx[-1] in ".!?\n") else "mid-sentence")
                    + f" ({took} ms{' since the key release' if job is not None else ''})")
                terms = settings.live_dictation_custom_terms()
                text = apply_sentence_case(
                    fix_known_addresses(tidy_text(format_spoken(text)), terms),   # (your saved addresses, exactly)
                    before,
                    terms,
                )
            except Exception as e:
                logger.warning(f"[DICTATION] deterministic formatting skipped: {e}")

        self._recorder = None
        # Notify listeners (UI) with the final text + mode before routing.
        try:
            self.transcript_ready.emit(text, self._mode)
        except Exception:
            pass
        if not text:
            logger.info("[DICTATION] empty transcript — nothing to do")
            return

        try:                                       # which voice mode (counts only - never the words)
            from app.core.supabase_client import track
            track("voice_used", mode=str(self._mode or "dictate"), words=len(text.split()), screenshots=0)
        except Exception:
            pass

        if self._mode == "search":
            self._route_search(text)
        elif self._mode == "organize":
            self._route_organize(text)
        else:
            from app.core.dictation_format import join_space
            self._paste(join_space(before, text))  # (a space when it would stick to the text before the cursor)

        self._save_history(text, self._mode)

    def _on_error(self, message: str):
        logger.error(f"[DICTATION] error: {message}")
        if message:                           # say it (the pill used to just vanish: no word about the limit)
            self._overlay.show_message(message)
        else:
            self._overlay.hide_pill()
        self.state_changed.emit("idle")
        self._recorder = None

    # ----- routing -----
    def _paste(self, text: str):
        """Put text on the clipboard and synth Ctrl+V into the focused app, then put back what was on the clipboard -
        all of it (text, a picture, files: it used to keep only the text, so a copied picture became empty text), and
        never over something copied after the paste."""
        logger.info(f"[DICTATION] pasting {len(text)} chars into focused app")
        try:
            from app.ui import clipboard_guard
            cb = QApplication.clipboard()
            prev = clipboard_guard.snapshot()
            cb.setText(text)
            # Small delay so the overlay is fully hidden + focus is stable.
            QTimer.singleShot(90, lambda: self._do_paste_and_restore(cb, prev, text))
        except Exception as e:
            logger.error(f"[DICTATION] clipboard paste failed: {e}")

    def _do_paste_and_restore(self, cb, prev, text):
        from app.ui import clipboard_guard
        # Something else may have written to the clipboard in those 90 ms (a clipboard manager, a phone's clipboard
        # syncing over, a copy): then THAT was pasted - anything private. Put the words back first; what the other
        # program wrote is the newest copy, so that's what goes back afterwards. (Mac c143812.)
        newer = clipboard_guard.put_back_if_changed(text)
        if newer is not None:
            prev = newer
        _send_ctrl_v()
        QTimer.singleShot(400, lambda: self._restore_clipboard(cb, prev, text))

    def _restore_clipboard(self, cb, prev, text):
        from app.ui import clipboard_guard
        if clipboard_guard.restore_if_still(text, prev):
            logger.debug("[DICTATION] clipboard restored")

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
        if self.organize_controller is not None:
            self.organize_controller.start_from_transcript(text)
        else:
            logger.warning("[DICTATION] no organize_controller injected — organize skipped")

    # ----- helpers -----
    def _custom_words(self):
        try:
            words = settings.live_dictation_custom_terms() or []
            return list(words) if words else None
        except Exception:
            return None

    def _save_history(self, text: str, mode: str):
        try:
            from app.core import dictation_history
            dictation_history.add(text)
            # Refresh the Voice page's History card if it's live.
            mw = self.main_window
            if mw is not None and hasattr(mw, "_on_dictation_saved"):
                mw._on_dictation_saved()
        except Exception as e:
            logger.debug(f"[DICTATION] history save skipped: {e}")
