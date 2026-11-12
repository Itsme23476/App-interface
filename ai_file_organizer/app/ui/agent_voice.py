"""Talk to the agent from anywhere (Windows) — hold the agent chord (Ctrl + Alt), speak, let go → the words
become a task for the Agents tab (it routes them: new chat / continue the one in focus / "use my … chat").

While holding the chord (ported from the Mac's dictation.py, same timings):
  * TAP Space  → drawing turns ON (red ink on the live screen); tap Space again → snapshot (screen + marks)
  * HOLD Space → draw while held; let go of Space → snapshot
  * let go of the chord while drawing → that's a snapshot too
  * RIGHT-click → drawing on (draw with the LEFT button, as many marks as you like); right-click again →
    snapshot. Esc on a right-click drawing cancels just that drawing (keep talking); Esc again drops it all
  * Esc → cancel everything (nothing is sent) — also just AFTER you let go, while your words are written down
    and the card says "Routing…" (until it becomes a chat). Reaching for Esc often lifts the letter first.
Snapshots ride along with your words; with several, "[screenshot N]" marks where you took each. Said nothing
but snapped something → "Look at what I've marked in red." / "Take a look at this screenshot."

The mic is the same StreamingTranscriber dictation uses (Grok), with your language + Custom Words. It never
starts while a dictation is recording (one mic at a time).
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QTimer

logger = logging.getLogger(__name__)

TAP_SEC = 0.35        # Space released sooner than this = a TAP: drawing stays on
REGRIP_SEC = 0.3      # pressed again this soon after that tap = a re-grip (a hold), not "snapshot"
RIGHT_HINT = "draw with left-click · right-click to capture · Esc cancels the drawing"
MIN_HOLD_SEC = 0.25   # a chord tap shorter than this, with no screenshot, is an accident → dropped


def with_markers(text: str, shots) -> str:
    """'change this [screenshot 1] … also this [screenshot 2]': each screenshot goes after the words you'd said
    when you took it (shots = (path, had_marks, words_so_far|None); None → the end). (Mac, verbatim.)"""
    words = text.split()
    at = {}
    for i, s in enumerate(shots, 1):
        n = s[2] if len(s) > 2 and s[2] is not None else len(words)
        at.setdefault(min(n, len(words)), []).append(f"[screenshot {i}]")
    out = []
    for k in range(len(words) + 1):
        out += at.get(k, [])
        if k < len(words):
            out.append(words[k])
    return " ".join(out)


class AgentVoiceController(QObject):
    def __init__(self, main_window, hotkey=None):
        super().__init__(main_window)
        self.mw = main_window
        self._recorder = None
        self._state = "idle"              # idle | recording | transcribing
        self._pressed_at = 0.0
        self._shots = []                  # [(png, had_marks, words_so_far)]
        self._ink = None
        self._inking = False
        self._ink_down_at = 0.0
        self._ink_tapped_at = None
        self._ink_by_right = False        # this drawing was started with a right-click (Esc = undo just it)
        self._watchdog = None
        self._pill = None
        self._route_rid = None            # the request we handed the page, while it's still being routed
        self._route_watch = QTimer(self)  # …watched so Esc stops being claimed the moment it becomes a chat
        self._route_watch.setInterval(100)
        self._route_watch.timeout.connect(self._check_routed)
        if hotkey is None:
            from app.ui.agent_hotkey import AgentHotkey, DEFAULT_SHORTCUT
            from app.core.settings import settings
            hwnd = 0
            try:
                hwnd = int(main_window.winId())
            except Exception:
                pass
            hotkey = AgentHotkey(hwnd, getattr(settings, "assistant_voice_shortcut", "") or DEFAULT_SHORTCUT)
        self.hotkey = hotkey
        hotkey.pressed.connect(self._on_press)
        hotkey.released.connect(self._on_release)
        hotkey.space.connect(self._on_space)
        hotkey.escape.connect(self._on_escape)
        if hasattr(hotkey, "right"):
            hotkey.right.connect(self._on_right_click)

    def start(self) -> bool:
        return self.hotkey.start()

    def stop(self) -> None:
        self.hotkey.stop()
        self._cancel()

    # -- the chord --------------------------------------------------------------------------------------
    def _dictation_busy(self) -> bool:
        dc = getattr(self.mw, "dictation_controller", None)
        return bool(dc is not None and getattr(dc, "_recorder", None) is not None)

    def _on_press(self):
        if self._state != "idle":
            logger.info(f"[agent-voice] chord ignored (state={self._state})")
            return
        if self._dictation_busy():
            logger.info("[agent-voice] chord ignored — a dictation is recording")
            return
        try:
            from app.core.transcription import StreamingTranscriber
            from app.core.settings import settings
            self._recorder = StreamingTranscriber(language=(settings.dictation_language or None),
                                                  terms=settings.live_dictation_custom_terms())
        except Exception as e:
            logger.error(f"[agent-voice] mic unavailable: {e}")
            return
        self._recorder.finished.connect(self._on_text)
        self._recorder.error.connect(self._on_error)
        if hasattr(self._recorder, "capped"):
            self._recorder.capped.connect(self._on_release)
        self._shots = []
        self._state = "recording"
        self._pressed_at = time.monotonic()
        self._recorder.start()                # the mic first — not one word lost to the pill showing
        self._route_tag = None
        pill = self._get_pill()
        if pill is not None:
            try:
                self._recorder.level.connect(pill.set_level)
                pill.show_listening("assistant")
            except Exception:
                pass
        logger.info("[agent-voice] ▶ listening")
        try:                                  # Claude Code + the decider start while you talk (warm on intent)
            page = getattr(self.mw, "assistant_page", None)
            if page is not None and hasattr(page, "agent_intent"):
                page.agent_intent("voice")
        except Exception as e:
            logger.info(f"[agent-voice] warm-up skipped: {e}")

    def _on_release(self):
        if self._state != "recording":
            return
        if self._inking:                       # let go of the chord while drawing → a snapshot too
            self._take_shot()
        self._end_ink()
        held = time.monotonic() - self._pressed_at
        if held < MIN_HOLD_SEC and not self._shots:
            logger.info(f"[agent-voice] chord tap ({held:.2f}s) — dropped")
            self._cancel()
            return
        self._state = "transcribing"
        self._hold_escape()                    # Esc still cancels while it's written down + routed
        if not self._shots:
            # What you said is already transcribed live: start deciding where it goes NOW, while the final transcript
            # arrives (read-only — the transcription is untouched). Used only if the final words match
            # (AssistantPage._route_sig); screenshots change the words, so those are decided at the end.
            try:
                early = self._recorder.text_so_far() if hasattr(self._recorder, "text_so_far") else ""
                page = getattr(self.mw, "assistant_page", None)
                if early and page is not None and hasattr(page, "prepare_live_route"):
                    self._route_tag = ("shiftz", time.monotonic())
                    page.prepare_live_route(self._route_tag, early, live=False)
            except Exception as e:
                self._route_tag = None
                logger.info(f"[agent-voice] early routing skipped: {e}")
        pill = self._get_pill()
        if pill is not None:
            pill.show_transcribing()
        try:
            self._recorder.stop_recording()
        except Exception as e:
            self._on_error(str(e))
            return
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_timeout)
        self._watchdog.start(25000)
        logger.info(f"[agent-voice] ■ released after {held:.1f}s, {len(self._shots)} screenshot(s) — transcribing")

    def _on_text(self, text: str):
        if self._watchdog is not None:
            self._watchdog.stop()
        if self._state != "transcribing":
            return
        text = (text or "").strip()
        shots, self._shots = self._shots, []
        self._state, self._recorder = "idle", None
        pill = self._get_pill()
        if pill is not None:
            pill.hide_pill()
        if not text and shots:                  # drew / snapped but said nothing
            text = ("Look at what I've marked in red." if any(s[1] for s in shots)
                    else "Take a look at this screenshot.")
        elif len(shots) > 1:
            text = with_markers(text, shots)
        if not text:
            logger.info("[agent-voice] no speech — nothing sent")
            self._drop_escape()
            return
        logger.info(f"[agent-voice] heard {text[:120]!r} + {len(shots)} screenshot(s) → Agents")
        page = getattr(self.mw, "assistant_page", None)
        if page is None:
            logger.error("[agent-voice] Agents page unavailable")
            self._drop_escape()
            return
        try:
            tag, self._route_tag = getattr(self, "_route_tag", None), None
            kw = {"tag": tag} if tag is not None else {}      # (only with an early decision to match)
            rid = page.run_voice_task(text, images=[s[0] for s in shots], **kw)
        except Exception as e:
            logger.error(f"[agent-voice] couldn't start the task: {e}", exc_info=True)
            rid = None
        if rid is not None and getattr(page, "is_routing", lambda _r: False)(rid):
            self._route_rid = rid              # Esc now cancels the routing request (until it becomes a chat)
            self._route_watch.start()
        else:
            self._drop_escape()

    def _check_routed(self):
        page = getattr(self.mw, "assistant_page", None)
        if self._route_rid is None or page is None or not page.is_routing(self._route_rid) \
                or not getattr(self.hotkey, "holding_escape", False):
            self._route_rid = None
            self._route_watch.stop()
            self._drop_escape()

    def _hold_escape(self):
        fn = getattr(self.hotkey, "hold_escape", None)
        if fn is not None:
            try:
                fn()
            except Exception:
                pass

    def _drop_escape(self):
        self._route_rid = None
        self._route_watch.stop()
        fn = getattr(self.hotkey, "drop_escape", None)
        if fn is not None:
            try:
                fn()
            except Exception:
                pass

    def _on_error(self, msg: str):
        logger.error(f"[agent-voice] mic/transcription error: {msg}")
        self._cancel()

    def _on_timeout(self):
        if self._state == "transcribing":
            logger.warning("[agent-voice] transcription timed out — dropped")
            self._cancel()

    # -- Space: draw / snapshot ---------------------------------------------------------------------------
    def _on_space(self, down: bool):
        now = time.monotonic()
        if down:
            if self._state != "recording":
                return
            if self._inking:
                if self._ink_tapped_at is not None and now - self._ink_tapped_at < REGRIP_SEC:
                    self._ink_tapped_at, self._ink_down_at = None, now     # a re-grip: this press is a hold
                    return
                self._take_shot()                                           # the second tap: snapshot
                return
            ink = self._ensure_ink()
            if ink is None:
                return
            if not ink.isVisible():
                ink.begin()
            ink.set_drawing(True)
            self._inking, self._ink_down_at, self._ink_tapped_at = True, now, None
        elif self._inking and self._ink_tapped_at is None:
            if now - self._ink_down_at < TAP_SEC:
                self._ink_tapped_at = now                                   # a tap: keep drawing
                self._ink.set_drawing(True, "tap Space again to snapshot · Esc cancels")
            else:
                self._take_shot()                                           # a hold: release = snapshot

    def _on_right_click(self):
        """Right-click while holding the chord: drawing on (left button draws); right-click again → snapshot.
        The right button only toggles — a trackpad can't comfortably right-drag. (Mac §8j, as-is.)"""
        if self._state != "recording":
            return
        if self._inking:
            self._take_shot()                          # the second right-click (or after a Space tap)
            return
        ink = self._ensure_ink()
        if ink is None:
            return
        if not ink.isVisible():
            ink.begin()
        ink.set_drawing(True, RIGHT_HINT)
        now = time.monotonic()
        self._inking, self._ink_down_at, self._ink_tapped_at = True, now, now - 1   # latched, past re-grip
        self._ink_by_right = True

    def _on_escape(self):
        if self._state == "recording" and self._inking and self._ink_by_right and self._ink is not None:
            logger.info("[agent-voice] right-click drawing cancelled with Esc")
            self._inking, self._ink_tapped_at, self._ink_by_right = False, None, False
            self._ink.clear()
            self._ink.set_drawing(False)
            return
        if self._state == "recording":
            logger.info("[agent-voice] cancelled with Esc")
            self._cancel()
        elif self._state == "transcribing":       # let go, then Esc: drop it before it's sent
            logger.info("[agent-voice] cancelled with Esc (after letting go, while writing it down)")
            self._cancel()
        elif self._route_rid is not None:         # …or while the card says "Routing…"
            rid, page = self._route_rid, getattr(self.mw, "assistant_page", None)
            self._drop_escape()
            if page is not None and page.cancel_routing(rid):
                logger.info("[agent-voice] cancelled with Esc (while routing)")

    def _ensure_ink(self):
        if self._ink is None:
            try:
                from app.ui.screen_ink import ScreenInk
                self._ink = ScreenInk()
            except Exception as e:
                logger.error(f"[agent-voice] screen ink unavailable: {e}")
                return None
        return self._ink

    def _take_shot(self):
        self._inking, self._ink_tapped_at, self._ink_by_right = False, None, False
        ink = self._ink
        if ink is None:
            return
        shot = ink.snapshot()
        ink.set_drawing(False)
        if shot:
            at = getattr(self._recorder, "words_so_far", lambda: None)()
            self._shots.append((*shot, at))

    def _end_ink(self):
        self._inking, self._ink_tapped_at, self._ink_by_right = False, None, False
        if self._ink is not None:
            self._ink.end()

    # -- helpers -------------------------------------------------------------------------------------------
    def _cancel(self):
        self._end_ink()
        self._shots = []
        if self._state != "recording":         # (while the chord is held, its own claims own Esc)
            self._drop_escape()
        if self._watchdog is not None:
            self._watchdog.stop()
        r, self._recorder = self._recorder, None
        self._state = "idle"
        pill = self._get_pill()
        if pill is not None:
            try:
                pill.hide_pill()
            except Exception:
                pass
        if r is not None:
            for sig in ("finished", "error"):
                try:
                    getattr(r, sig).disconnect()
                except Exception:
                    pass
            try:
                # cancel(), not stop_recording(): nobody wants this text (a tap, Esc, a timeout), so an empty stream
                # must not be re-uploaded for a batch transcription — 2026-11-09 log: five quick Shift+Z taps in 3 s
                # each uploaded ~1.3 s of audio (voice usage for nothing).
                (r.cancel if hasattr(r, "cancel") else r.stop_recording)()
            except Exception:
                pass

    def _get_pill(self):
        """The same floating pill dictation shows (an "assistant" sparkle), shared, never two."""
        if self._pill is None:
            dc = getattr(self.mw, "dictation_controller", None)
            self._pill = getattr(dc, "_overlay", None)
            if self._pill is None:
                try:
                    from app.ui.dictation_overlay import DictationOverlay
                    self._pill = DictationOverlay()
                except Exception:
                    self._pill = None
        return self._pill
