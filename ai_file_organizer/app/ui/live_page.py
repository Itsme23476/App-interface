"""The Assistant tab — Filect Assistant, hands-free (Windows port of the Mac page, feat/live-assistant e677e8e).

Start → Filect listens (app/core/live_listener.py). Each thing you say, once you've stopped talking for
"send after" seconds, runs exactly like a voice-chord request: AssistantPage.run_voice_task(text, source="live") →
router (which may IGNORE what isn't a request) → Claude Code / Codex → the card at the top. This page shows what it's
doing — the orb in the middle (state, mic level, the ring that fills before it sends), "What it heard" (each request,
where it went, working / done), the Ctrl Ctrl tray — and its Settings panel on the right. Stop (here) ends it; it never
starts by itself, and quitting Filect stops it.

Your screen, two ways:
  · "Look at my screen…" — the display under the mouse is captured (in memory, off the UI thread) the moment it's
    clear you're speaking; it's written to disk and attached ONLY if the request is about your screen (the router's
    "screen" flag, or the word check); otherwise it's simply dropped.
  · Draw: press Shift + Ctrl together (watched only while Filect Assistant is on — the listener's observe-only key
    hook, so no key is ever blocked) → the red-ink layer the moment both are down; press them again → a screenshot
    with your marks. (Ctrl+Shift+Tab and other Ctrl+Shift+key shortcuts: the drawing that started is quietly taken
    back.) Up to 5 drawings ALL go with the next thing you say. Talking while you draw? That request waits for the
    drawing and goes with it. Not used (ignored / cancelled / a yes-no answer) → they keep waiting; dropped after
    60 s or with Esc. Esc while drawing cancels that drawing and what you said during it.

Collect, then send: double-tap Ctrl → "Collecting": everything you say (and draw with Shift + Ctrl) is kept, not
sent — pause as long as you like; tap Ctrl once → it ALL goes as ONE request (routed once); Esc throws it away.

Speak replies (a setting, OFF by default): a hands-free task's moments that matter — done (what the agent wrote to be
said: a sentence or two, more when you asked), needs your OK, failed — are said out loud (app/core/speech_out.py). It
waits while you're talking or using a Filect shortcut, and stops when you press Esc or click Stop. Windows has no echo
cancellation for the mic, so the mic is ignored while Filect speaks (+300 ms) — as the Mac does with its filter off.

Windows vs the Mac: the keys are Shift + Ctrl / Ctrl Ctrl … Ctrl / Esc; chimes are Windows' own short sounds; the
"Filter background noise" switch is hidden (no voice processing here); there's no menu-bar / tray item."""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSlider, QVBoxLayout, QWidget)

from app.core.settings import settings
from app.ui.theme_manager import get_theme_colors

logger = logging.getLogger("ai_file_organizer")

ACCENT = "#7C4DFF"
AMBER = "#E0A33A"
RED = "#E5534B"
GREEN = "#3FB97F"
MAX_ROWS = 100
SAY_EXAMPLE = "“make a landing page for my coffee shop”"
DRAWN_KEEP_MS = 60000           # drawings wait this long (after the last one) for what you say about them
MAX_CAPTURES = 3                # screens kept in memory at once (one per request being heard / checked)
MAX_DRAWINGS = 5                # drawings that can go with one request
DRAW_KEY = "Shift + Ctrl"        # press Shift + Ctrl together (live_listener.ChordTap)
COLLECT_TAP_SEC = 1.0           # two Ctrl taps this close together = start collecting
COLLECT_WAIT_MS = 8000          # tapping Ctrl to send waits this long at most for what's still being transcribed

# ("See what it can do": this page's tour is page_tours.py's "assistant", in the first-run guide's style)

# listener state → the notch light
_NOTCH = {"starting": "listening", "listening": "listening", "countdown": "listening",
          "hearing": "hearing", "paused": "paused"}


def _theme_name(theme=None) -> str:
    if theme in ("light", "dark"):
        return theme
    try:
        from app.ui.theme_manager import theme_manager
        return "dark" if theme_manager.current_theme == "dark" else "light"
    except Exception:
        return "dark" if getattr(settings, "theme", "light") == "dark" else "light"


def _colors(theme=None) -> dict:
    """What the Mac page calls agents_view.colors(): theme_manager's palette (bg, text_secondary, input_bg…) + dark."""
    name = _theme_name(theme)
    c = dict(get_theme_colors(name))
    c.setdefault("purple_light_bg", "#EDE7FF")
    c["dark"] = name == "dark"
    return c


def _av_colors(theme=None) -> dict:
    """The Agents page's own palette — for its widgets used here (SetupCard)."""
    from app.ui.agents_view import colors
    return colors(_theme_name(theme))


def style_tour_button(btn: QPushButton, seen: bool, c) -> None:
    """"See what it can do": until the tour was opened once, a filled pill with a soft pulsing glow ("New
    here? …"); after that a quiet link. (The Mac's agents_view.style_tour_button.)"""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    from app.ui.icons import line_icon
    if seen:
        btn.setText(" See what it can do")
        btn.setStyleSheet(f"QPushButton {{ background:transparent; color:{ACCENT}; border:none; border-radius:10px; "
                          f"padding:0 10px; font-size:12px; font-weight:600; }} "
                          f"QPushButton:hover {{ background:{c['purple_light_bg']}; }}")
        glow = getattr(btn, "_glow", None)
        if glow is not None:
            glow.stop()
            btn._glow = None
            btn.setGraphicsEffect(None)
    else:
        btn.setText(" New here? See what it can do")
        bg = "rgba(124,77,255,0.18)" if c["dark"] else c["purple_light_bg"]
        btn.setStyleSheet(f"QPushButton {{ background:{bg}; color:{ACCENT}; border:1px solid {ACCENT}; "
                          "border-radius:14px; padding:0 14px 0 11px; font-size:12.5px; font-weight:700; } "
                          f"QPushButton:hover {{ background:{ACCENT}; color:white; }}")
        if getattr(btn, "_glow", None) is None:
            # Windows: the Mac's 60-fps animated BLUR re-rendered the shadow non-stop — measured 20 % of a core while
            # the page is open (0 % without it). As the Agents tab's glow (agents_view.Composer.set_glow): a fixed
            # blur whose colour breathes ~10×/s, and only while the button is on screen.
            import math
            fx = QGraphicsDropShadowEffect(btn)
            fx.setOffset(0, 0)
            fx.setBlurRadius(18)
            fx.setColor(QColor(124, 77, 255, 150))
            btn.setGraphicsEffect(fx)
            a = QTimer(btn)
            a.setInterval(100)
            t0 = time.monotonic()

            def breathe():
                if not btn.isVisible() or btn.window().isMinimized():
                    return
                k = 0.5 - 0.5 * math.cos(2 * math.pi * ((time.monotonic() - t0) % 1.6) / 1.6)
                fx.setColor(QColor(124, 77, 255, int(40 + 140 * k)))
            a.timeout.connect(breathe)
            a.start()
            btn._glow = a
    btn.setIcon(line_icon("sparkle", 13, ACCENT))


def _track(event: str, **props) -> None:
    try:
        from app.core.supabase_client import track
        track(event, **props)
    except Exception:
        pass


# The Mac's NSSound names → Windows' own short sounds (C:\Windows\Media): a light tick when a request goes through,
# a soft click for a cancel / throw-away, "Speech On" when collecting starts.
_CHIMES = {"Tink": "Windows Feed Discovered.wav", "Pop": "Windows Menu Command.wav", "Morse": "Speech On.wav"}


def _chime(name: str = "Tink") -> None:
    """Async (never blocks the UI); silent in tests (offscreen) and if the sound file is missing."""
    try:
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.platformName() == "offscreen":
            return
        import os
        import winsound
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Media", _CHIMES.get(name, _CHIMES["Tink"]))
        if os.path.exists(path):
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass


def _ago(t0: float) -> str:
    d = time.time() - t0
    if d < 60:
        return "now"
    if d < 3600:
        return f"{int(d // 60)} min"
    return time.strftime("%H:%M", time.localtime(t0))


class _Orb(QWidget):
    """The big button in the middle of the page (2026-11-07 redesign, mockups/assistant-page*.png): click it to
    start / stop. It shows the state by itself — off · listening (rings follow your voice) · a ring that fills
    while it waits to send · collecting (a count) · paused (amber) · error (red). Keeps the old meter's API
    (set_level / set_on) so the listener's level signal drives it.

    Smooth while the mic + speech detection share the UI thread (his "it lags", 2026-11-07): a precise 60-fps
    clock moves it by elapsed TIME (a late frame catches up instead of stuttering) and only that clock repaints;
    the orb itself (glow, gradient, icon) is drawn once per state and reused, so a frame is just the rings; it
    paints its own background (Qt needn't repaint what's behind it) and skips frames while the window is covered."""
    clicked = Signal()
    LIVE = ("listen", "hear", "count", "collect", "starting")
    BREATH_SEC = 5.0            # one slow breath of the rings
    FOLLOW_SEC = 0.093          # how fast the rings follow your voice (time constant)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._state, self._v, self._shown, self._prog, self._badge, self._phase = "off", 0.0, 0.0, 0.0, 0, 0.0
        self._dark, self._bg = False, QColor("#F3F1FB")
        self._count = None                                # (left, when, total): the countdown, between reports
        self._body = {}                                   # (state kind, size, dark, dpr) -> the drawn orb
        self._last = time.monotonic()
        self._t = QTimer(self)
        self._t.setTimerType(Qt.PreciseTimer)
        self._t.setInterval(16)
        self._t.timeout.connect(self._tick)
        self.set_big(True)

    def set_big(self, big: bool):
        s = 236 if big else 176
        if self.width() != s:
            self.setFixedSize(s, s)
            self.update()

    def set_dark(self, dark: bool):
        self._dark = dark
        self._body.clear()
        self.update()

    def set_bg(self, color: str):
        self._bg = QColor(color)
        self.update()

    def set_state(self, state: str):
        if state == self._state:
            return
        self._state = state
        if state in self.LIVE:
            self._last = time.monotonic()
            if self.isVisible():                          # (hidden: showEvent starts it)
                self._t.start()
        else:
            self._t.stop()
            self._v = self._shown = 0.0
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        if self._state in self.LIVE and not self._t.isActive():
            self._last = time.monotonic()
            self._t.start()

    def hideEvent(self, e):
        super().hideEvent(e)                              # (another page / minimised: no 60-Hz wake-ups)
        self._t.stop()

    def set_level(self, v: float) -> None:
        v = max(0.0, min(1.0, v))
        self._v = v if v > self._v else self._v * 0.75 + v * 0.25     # fast attack, smooth release

    def set_on(self, on: bool) -> None:
        if not on:
            self._v = 0.0

    def set_progress(self, f: float):
        self._prog = max(0.0, min(1.0, f))
        self._count = None
        if not self._t.isActive():                        # (while animating, the clock repaints it)
            self.update()

    def set_countdown(self, left: float, total: float):
        """The listener reports the time left only every 0.4 s (his "it jumps 0.4, 0.8"): remember when, and
        let every frame count down from there — the ring fills continuously."""
        total = max(0.5, float(total))
        left = max(0.0, float(left))
        if self._count is not None and abs(self._count[2] - total) < 0.01:   # (only ever forward — a report
            left = min(left, self.left_now())             #  behind the clock doesn't pull it back — unless the wait
                                                          #  itself changed: it sounded unfinished → it waits longer)
        self._count = (left, time.monotonic(), total)
        self._prog = 1.0 - self._count[0] / total

    def left_now(self):
        """Seconds left right now (between the listener's reports), or None outside a countdown."""
        if self._count is None:
            return None
        left, t0, _total = self._count
        return max(0.0, left - (time.monotonic() - t0))

    def _prog_now(self) -> float:
        if self._count is None:
            return self._prog
        return 1.0 - self.left_now() / self._count[2]

    def set_badge(self, n: int):
        if n != self._badge:
            self._badge = n
            if not self._t.isActive():
                self.update()

    def _tick(self):
        import math
        now = time.monotonic()
        dt, self._last = min(0.25, now - self._last), now
        self._phase = (self._phase + dt * 2 * math.pi / self.BREATH_SEC) % (2 * math.pi)
        self._shown += (self._v - self._shown) * (1.0 - math.exp(-dt / self.FOLLOW_SEC))
        # Windows: a frame costs ~2 ms to draw here (measured; ~12 % of a core at 60 fps, more with the flush) —
        # so 60 fps only while there's real motion (the rings following your voice); the countdown ring at 30 fps; the
        # quiet breath (±1.6 px over 5 s: at most ~0.16 px per frame) at 12.5 fps — the same picture. Motion goes by elapsed time,
        # so the speed never changes with the frame rate.
        moving = self._v > 0.02 or self._shown > 0.02
        want = 16 if moving else (33 if self._state == "count" else 80)
        if self._t.interval() != want:
            self._t.setInterval(want)
        if not self.isVisible():
            return
        wh = self.window().windowHandle()
        if wh is not None and not wh.isExposed():         # covered / minimised: no frames
            return
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            c = self.width() / 2.0
            r = self._r() + 12
            d = e.position() - QPointF(c, c)
            if d.x() * d.x() + d.y() * d.y() <= r * r:
                self.clicked.emit()
        super().mousePressEvent(e)

    def _r(self) -> float:
        return (140 if self.width() >= 236 else 112) / 2.0

    def _kind(self) -> str:
        return {"off": "off", "paused": "paused", "error": "error"}.get(self._state, "live")

    def _orb_pixmap(self, dpr: float) -> QPixmap:
        """The orb itself — glow, gradient, shading, its icon — drawn once per state / size / theme."""
        kind, w = self._kind(), self.width()
        key = (kind, w, self._dark, dpr)
        pm = self._body.get(key)
        if pm is not None:
            return pm
        pm = QPixmap(int(w * dpr), int(w * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        c, R, k = w / 2.0, self._r(), w / 236.0
        ctr = QPointF(c, c)
        glow = {"paused": QColor(60, 40, 110, 45), "error": QColor(217, 68, 58, 80)}.get(kind, QColor(108, 61, 232, 95))
        g = QRadialGradient(QPointF(c, c + R * 0.32), R * 1.18)
        g.setColorAt(0.55, glow)
        gt = QColor(glow)
        gt.setAlpha(0)
        g.setColorAt(1.0, gt)
        p.setBrush(g)
        p.drawEllipse(QPointF(c, c + R * 0.32), R * 1.18, R * 1.18)
        stops = {"off": ("#D6CBFF", "#A487FF", "#7C4DFF", "#6337E8"),
                 "paused": ("#F3F0FA", "#E3DEF0", "#CFC8E2", "#B8B0CF"),
                 "error": ("#FFC9C4", "#F49A91", "#F07A70", "#D9443A")}.get(kind, ("#C9B8FF", "#9670FF", "#6B3FEA", "#4F25D3"))
        og = QRadialGradient(QPointF(c - R * 0.32, c - R * 0.46), R * 1.9)
        for pos, col in zip((0.0, 0.3, 0.62, 1.0), stops):
            og.setColorAt(pos, QColor(col))
        p.setBrush(og)
        p.drawEllipse(ctr, R, R)
        shade = QRadialGradient(QPointF(c, c + R * 0.9), R * 1.1)
        shade.setColorAt(0.0, QColor(40, 10, 120, 0 if kind in ("paused", "error") else 70))
        shade.setColorAt(1.0, QColor(40, 10, 120, 0))
        p.setBrush(shade)
        p.drawEllipse(ctr, R, R)
        if kind == "paused":
            p.setBrush(QColor("#8A8399"))
            bw, bh = 7 * k, 26 * k
            p.drawRoundedRect(QRectF(c - bw - 3.5 * k, c - bh / 2, bw, bh), 2 * k, 2 * k)
            p.drawRoundedRect(QRectF(c + 3.5 * k, c - bh / 2, bw, bh), 2 * k, 2 * k)
        elif kind == "error":
            f = QFont()
            f.setPixelSize(int(40 * k))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(QRectF(c - R, c - R, 2 * R, 2 * R), Qt.AlignCenter, "!")
        else:
            from app.ui.icons import line_pixmap
            size = int(44 * k)
            ic = line_pixmap("mic" if kind == "off" else "waveform", size, "#FFFFFF")
            p.drawPixmap(QRectF(c - size / 2, c - size / 2, size, size), ic, QRectF(ic.rect()))
        p.end()
        self._body[key] = pm
        return pm

    def paintEvent(self, _e):
        import math
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg)                 # (opaque: nothing behind it needs repainting)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = float(self.width())
        c, R, k = w / 2.0, self._r(), w / 236.0
        st = self._state
        ctr = QPointF(c, c)
        p.setPen(Qt.NoPen)
        if st in ("listen", "hear", "collect", "starting"):           # rings: breathe + follow your voice
            grow = (self._shown * 14 + math.sin(self._phase) * 1.6) * k
            for rad, a in ((118, 12), (104, 23), (88, 41)):
                p.setBrush(QColor(124, 77, 255, a if st != "starting" else a // 2))
                rr = rad * k + grow * (rad / 118.0)
                p.drawEllipse(ctr, rr, rr)
        elif st == "count":
            p.setBrush(QColor(124, 77, 255, 12))
            p.drawEllipse(ctr, 118 * k, 118 * k)
            rr = 80 * k
            pen = QPen(QColor(124, 77, 255, 36), 4.5 * k)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(ctr, rr, rr)
            pen.setColor(QColor(ACCENT))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(QRectF(c - rr, c - rr, 2 * rr, 2 * rr), 90 * 16, int(-self._prog_now() * 360 * 16))
            p.setPen(Qt.NoPen)
        elif st in ("paused", "error"):
            p.setPen(QPen(QColor(224, 163, 58, 190) if st == "paused" else QColor(229, 83, 75, 150), 3))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(ctr, R + 10 * k, R + 10 * k)
            p.setPen(Qt.NoPen)
        p.drawPixmap(QRectF(0, 0, w, w), self._orb_pixmap(self.devicePixelRatioF()), QRectF(0, 0, w * self.devicePixelRatioF(), w * self.devicePixelRatioF()))
        if self._badge and st in ("collect", "count", "hear", "listen"):
            bx, by, br = c + R * 0.62, c - R * 0.74, 13 * k
            p.setPen(QPen(QColor(ACCENT), 2))
            p.setBrush(QColor("#111119" if self._dark else "#FFFFFF"))
            p.drawEllipse(QPointF(bx, by), br, br)
            f = QFont()
            f.setPixelSize(int(13 * k))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(ACCENT))
            p.drawText(QRectF(bx - br, by - br, 2 * br, 2 * br), Qt.AlignCenter, str(self._badge))
        p.end()


class _Switch(QAbstractButton):
    """An on/off switch (its text is still "On" / "Off" — for screen readers and the tests)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(34, 20)
        self.setCursor(Qt.PointingHandCursor)
        self._off = QColor("#DCD7ED")
        self._knob = QColor("#FFFFFF")

    def set_colors(self, off: str, knob: str):
        self._off, self._knob = QColor(off), QColor(knob)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if not self.isEnabled():
            p.setOpacity(0.45)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ACCENT) if self.isChecked() else self._off)
        p.drawRoundedRect(QRectF(0, 0, 34, 20), 10, 10)
        x = 16 if self.isChecked() else 2
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(QRectF(x, 2.6, 16, 16))
        p.setBrush(self._knob)
        p.drawEllipse(QRectF(x, 2, 16, 16))
        p.end()


class _PlayBtn(QPushButton):
    """▶ next to a voice — painted (a ▶ glyph in a 24-pt button gets clipped by the app's button padding).
    Its text stays "▶" / "…" (loading)."""

    def __init__(self, parent=None):
        super().__init__("▶", parent)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.PointingHandCursor)
        self._c = None

    def paintEvent(self, _e):
        c = self._c or {}
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(ACCENT if self.underMouse() else c.get("border_strong", "#DCD7ED")), 1))
        p.setBrush(QColor(c.get("surface", "#FFFFFF")))
        p.drawEllipse(QRectF(0.5, 0.5, 23, 23))
        col = QColor(ACCENT if self.isEnabled() else c.get("text_muted", "#888"))
        if self.text() == "▶":
            path = QPainterPath()
            path.moveTo(9.5, 7.5)
            path.lineTo(17, 12)
            path.lineTo(9.5, 16.5)
            path.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawPath(path)
        else:
            p.setPen(col)
            p.drawText(self.rect(), Qt.AlignCenter, self.text())
        p.end()


class _VoiceButton(QAbstractButton):
    """One voice in the list: ◉ Name  description  (▶). Checkable; the ▶ is its own button."""

    def __init__(self, name: str, desc: str, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedHeight(36)
        self.setCursor(Qt.PointingHandCursor)
        self._name, self._desc, self._c = name, desc, None
        self.play = _PlayBtn(self)

    def resizeEvent(self, e):
        self.play.move(self.width() - 30, (self.height() - 24) // 2)
        super().resizeEvent(e)

    def paintEvent(self, _e):
        c = self._c or {}
        on = self.isChecked()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(ACCENT if on else c.get("border", "#E2DEF0")), 1))
        p.setBrush(QColor(c.get("purple_light_bg", "#EDE7FF")) if on else QColor(c.get("surface", "#FFFFFF")))
        if on and c.get("dark"):
            p.setBrush(QColor(124, 77, 255, 31))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 10, 10)
        cy = self.height() / 2.0
        if on:
            p.setPen(QPen(QColor(ACCENT), 4.5))
            p.setBrush(QColor(c.get("surface", "#FFFFFF")))
            p.drawEllipse(QPointF(17, cy), 4.75, 4.75)
        else:
            p.setPen(QPen(QColor(c.get("border_strong", "#DCD7ED")), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(17, cy), 6.25, 6.25)
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(c.get("text", "#1B1730")))
        nw = QFontMetrics(f).horizontalAdvance(self._name)
        p.drawText(QRectF(32, 0, nw + 2, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self._name)
        f.setBold(False)
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(QColor(c.get("text_muted", "#6F6B83")))
        x = 32 + nw + 9
        room = max(0, self.width() - 36 - x)
        p.drawText(QRectF(x, 0, room, self.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(f).elidedText(self._desc, Qt.ElideRight, room))
        p.end()


class _UsageBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(90, 5)
        self._f, self._track = 0.0, QColor("#DCD7ED")

    def set_fraction(self, f: float):
        self._f = max(0.0, min(1.0, f))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self._track)
        p.drawRoundedRect(QRectF(0, 0, 90, 5), 2.5, 2.5)
        if self._f > 0:
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(QRectF(0, 0, max(5.0, 90 * self._f), 5), 2.5, 2.5)
        p.end()


_ROUTE = re.compile(r"^(Started|Continued) “(.*)”(.*?)\s*→$")
_DECIDE = re.compile(r"^(Approved|Denied) — “(.*)”\s*→$")


class _Row(QFrame):
    """One thing it heard ("What it heard"): when · your drawing · what you said · where it went (the agent's
    logo, the chat, working / done, if it spoke the answer) · Open. `outcome` keeps the plain outcome text
    (the page and the tests read it); `line` is how it's shown."""

    def __init__(self, page, uid):
        super().__init__()
        self._page, self.uid, self.key = page, uid, ""
        self.t0 = time.time()
        self.state, self.spoke = "", False
        self.setObjectName("liveRow")
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 11, 14, 11)
        h.setSpacing(12)
        self.when = QLabel("now")
        self.when.setFixedWidth(40)
        h.addWidget(self.when, 0, Qt.AlignTop)
        self.thumb = QLabel()
        self.thumb.setFixedSize(74, 46)
        self.thumb.setVisible(False)
        h.addWidget(self.thumb, 0, Qt.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(7)
        self.text = QLabel("…")
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.addWidget(self.text)
        ln = QHBoxLayout()
        ln.setSpacing(7)
        self.icon = QLabel()
        self.icon.setFixedSize(15, 15)
        ln.addWidget(self.icon, 0, Qt.AlignVCenter)
        self.line = QLabel("")
        self.line.setTextFormat(Qt.RichText)
        ln.addWidget(self.line, 0, Qt.AlignVCenter)
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        ln.addWidget(self.dot, 0, Qt.AlignVCenter)
        self.state_lbl = QLabel("")
        ln.addWidget(self.state_lbl, 0, Qt.AlignVCenter)
        self.spk_icon = QLabel()
        self.spk_icon.setFixedSize(13, 13)
        ln.addWidget(self.spk_icon, 0, Qt.AlignVCenter)
        self.spk = QLabel("said it")
        ln.addWidget(self.spk, 0, Qt.AlignVCenter)
        ln.addStretch(1)
        self.open = QLabel("Open ↗")
        self.open.setCursor(Qt.PointingHandCursor)
        self.open.setVisible(False)
        ln.addWidget(self.open, 0, Qt.AlignVCenter)
        body.addLayout(ln)
        h.addLayout(body, 1)
        self.outcome = QLabel("Transcribing…")          # the plain outcome (not shown — `line` is)
        self.outcome.setVisible(False)
        self._color = None
        self.restyle()

    def set_text(self, text: str) -> None:
        self.text.setText(f"“{text}”" if text else "…")

    def set_outcome(self, text: str, color: str, key: str = "") -> None:
        self.outcome.setText(text)
        self._color = color
        self.key = key or ""
        self.setCursor(Qt.PointingHandCursor if self.key else Qt.ArrowCursor)
        self.setToolTip("Open this chat" if self.key else "")
        m = _ROUTE.match(text)
        self.state = "run" if m else ("" if not self.key else self.state)
        shots = self._page._drawn_for.get(self.uid) or []
        if shots and not self.thumb.isVisible():
            pm = QPixmap(shots[0])
            if not pm.isNull():
                pm = pm.scaled(148, 92, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                pm = pm.copy((pm.width() - 148) // 2, 0, 148, 92)
                pm.setDevicePixelRatio(2)
                self.thumb.setPixmap(pm)
                self.thumb.setVisible(True)
        self.restyle()

    def set_state(self, state: str):
        self.state = state
        self.restyle()

    def set_spoke(self):
        self.spoke = True
        self.restyle()

    def _agent(self) -> str:
        ap = getattr(self._page._mw, "assistant_page", None)
        s = getattr(ap, "_sessions", {}).get(self.key) if ap is not None else None
        return getattr(s, "agent", "claude") or "claude"

    def restyle(self):
        from app.ui.icons import line_pixmap
        c = self._page._c
        text = self.outcome.text()
        col = self._color or c["text_muted"]
        gone = text.startswith(("Cancelled", "Not sent", "Thrown away", "Stopped talking"))
        self.setStyleSheet(f"QFrame#liveRow {{ background:{c['surface']}; border:1px solid {c['border']}; "
                           "border-radius:14px; } QLabel { background:transparent; border:none; }")
        self.when.setText(_ago(self.t0))
        self.when.setStyleSheet(f"color:{c['text_muted']}; font-size:11px; padding-top:2px;")
        self.text.setStyleSheet(f"color:{c['text_muted'] if gone else c['text']}; font-size:13.5px;"
                                + (" text-decoration: line-through;" if gone else ""))
        self.thumb.setStyleSheet(f"border:1px solid {c['border']}; border-radius:8px;")
        m, d = _ROUTE.match(text), _DECIDE.match(text)
        pm = None
        if m or d:
            from app.ui.agents_view import agent_logo
            pm = agent_logo(self._agent(), 15)
            if m:
                verb = "New chat" if m.group(1) == "Started" else "Continued"
                extra = " · with your screen" if "screen" in m.group(3) else ""
                html = (f'<span style="color:{c["text_secondary"]}">{verb}</span>&nbsp; '
                        f'<b style="color:{c["text"]}">{_esc(m.group(2))}</b>'
                        f'<span style="color:{c["text_muted"]}">{extra}</span>')
            else:
                html = (f'<span style="color:{col}">{d.group(1)}</span>&nbsp; '
                        f'<b style="color:{c["text"]}">{_esc(d.group(2))}</b>')
        else:
            if gone:
                pm = line_pixmap("x", 12, c["text_muted"])
                html = f'<span style="color:{c["text_muted"]}">{_esc(text)}</span>'
            else:
                html = f'<span style="color:{col}">{_esc(text)}</span>'
        self.icon.setVisible(pm is not None)
        if pm is not None:
            self.icon.setPixmap(pm)
        self.line.setText(f'<span style="font-size:12px;">{html}</span>')
        st = self.state if (m or d) else ""
        self.dot.setVisible(bool(st))
        self.state_lbl.setVisible(bool(st))
        if st:
            dcol = {"run": ACCENT, "done": GREEN, "need": AMBER, "error": RED}[st]
            self.dot.setStyleSheet(f"background:{dcol}; border-radius:4px; margin:1px;")
            self.state_lbl.setText({"run": "working", "done": "done", "need": "needs your OK", "error": "failed"}[st])
            self.state_lbl.setStyleSheet(f"color:{c['text_secondary']}; font-size:12px;")
        self.spk.setVisible(self.spoke)
        self.spk_icon.setVisible(self.spoke)
        self.spk_icon.setPixmap(line_pixmap("speaker", 13, c["text_muted"]))
        self.spk.setStyleSheet(f"color:{c['text_muted']}; font-size:12px;")
        self.open.setVisible(bool(self.key))
        self.open.setStyleSheet(f"color:{ACCENT}; font-size:12px; font-weight:600;")

    def mousePressEvent(self, e):
        if self.key:
            self._page._open_chat(self.key)
        super().mousePressEvent(e)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class LivePage(QScrollArea):
    _preview_sig = Signal(str, object)    # (voice, audio | None) — a voice preview fetched off the UI thread

    def __init__(self, main_window, listener=None, capture=None, speaker=None):
        super().__init__()
        self._mw = main_window
        self._speaker_obj = None      # speech_out.Speaker, made when first needed (injectable for tests)
        self._speaker_given = speaker
        self._spoke_until = 0.0       # when Filect last stopped talking
        self._talked_over = set()     # uids you started saying while it talked (or just after)
        # your screen + drawing (see the module docstring); capture is injectable for tests
        self._capture_fn = capture
        self._captures = {}           # uid → {"img": QImage|None, "thread": Thread}: your screen as you began
        self._screen_used = set()     # uids whose capture was attached
        self._ink = None
        self._drawing = False
        self._drawings = []           # drawn screenshots (paths) waiting for what you say, in order
        self._drawn_for = {}          # uid → the drawings sent with it (back to waiting if it isn't used)
        self._waiting = None          # {"uids": [...], "text": str}: what you said WHILE drawing
        self._also = {}               # uid → other uids folded into its request (said during the same drawing)
        self._drawn_timer = QTimer(self)
        self._drawn_timer.setSingleShot(True)
        self._drawn_timer.timeout.connect(lambda: self._drop_drawings("expired"))
        # collect, then send (⌃⌃ … ⌃)
        self._collect = None          # {"uids": [...], "text": str, "sending": bool} while collecting
        self._ctrl_at = 0.0           # the last lone ⌃ tap (a 2nd within COLLECT_TAP_SEC = ⌃⌃)
        self._transcribing = set()    # uids you finished saying that aren't heard / dropped yet
        self._collect_timer = QTimer(self)
        self._collect_timer.setSingleShot(True)
        self._collect_timer.timeout.connect(lambda: self._send_collection(force=True))
        self._c = get_theme_colors()
        self.setObjectName("livePage")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.NoFrame)
        if listener is None:
            from app.core.live_listener import LiveListener
            listener = LiveListener(self)
        self.listener = listener
        self._rows = {}                 # uid → _Row
        self._sent_text = {}            # uid → what was sent (a request you go on talking after is pulled back)
        self._parked = {}               # uid → its text (None until transcribed): pulled back, joins the next part
        self._joins = {}                # new uid → the pulled-back uid whose words go in front of it
        self._wait_join = {}            # pulled-back uid → (new uid, its text): the next part came first
        self._applied = {}              # widget → the style it has (state flips restyle only what changed)
        self._started_at = None
        self._state = "off"
        self._tray_action = None
        self._build()
        self._preview_sig.connect(self._on_preview_ready)
        L = self.listener
        L.state_changed.connect(self._on_state)
        L.level.connect(self._meter.set_level)
        L.countdown.connect(self._on_countdown)
        L.utterance_ended.connect(self._on_ended)
        L.heard.connect(self._on_heard)
        L.dropped.connect(self._on_dropped)
        L.usage.connect(self._on_usage)
        L.esc_late.connect(self._on_esc_late)
        L.state_changed.connect(self._on_listener_state)   # off → any drawing ends
        if hasattr(L, "draw_tap"):
            L.draw_tap.connect(self._on_draw_key)
        if hasattr(L, "draw_spoiled"):
            L.draw_spoiled.connect(self._on_draw_spoiled)
        if hasattr(L, "ctrl_tap"):
            L.ctrl_tap.connect(self._on_ctrl_tap)
        if hasattr(L, "early_text"):
            L.early_text.connect(self._on_early_text)
        if hasattr(L, "quick_sent"):
            L.quick_sent.connect(self._on_quick_sent)
        if hasattr(L, "resumed"):
            L.resumed.connect(self._on_resumed)
        if hasattr(L, "speech_confirmed"):
            L.speech_confirmed.connect(self._on_speech_confirmed)
        L.utterance_started.connect(self._on_utterance_started)
        if hasattr(L, "set_esc_filter"):
            L.set_esc_filter(self._esc_filter)
        self._on_usage(L.used_today())
        self._noted = set()             # uids whose "Cancelled" note was already shown
        self._hooked_ap = None
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.listener.stop)     # quitting Filect stops listening (mic + key hook)
        try:                                               # (Windows: the theme toggle reaches pages by this signal)
            from app.ui.theme_manager import theme_manager
            theme_manager.theme_changed.connect(self.apply_theme)
        except Exception:
            pass

    # ---- layout (2026-11-07 redesign — mockups/assistant-page*.png) ------------------------------------------
    #   [header: Filect Assistant · status chip ················ Settings]  [Settings panel (hide →)]
    #   [the orb · what it's doing · Start / Stop · Try saying · tour     ]  [ Listening · Answers ·   ]
    #   [collecting tray (⌃⌃) · What it heard                            ]  [ Sounds & keys           ]
    #   [privacy · voice used today                                       ]
    def _build(self):
        self._c = _colors()
        page = QWidget()
        page.setObjectName("liveContent")
        outer = QHBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        main = QWidget()
        mv = QVBoxLayout(main)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(0)
        # header
        self._head = QFrame()
        self._head.setObjectName("liveHead")
        self._head.setFixedHeight(62)
        hh = QHBoxLayout(self._head)
        hh.setContentsMargins(26, 0, 26, 0)
        hh.setSpacing(12)
        self._title = QLabel("Filect Assistant")
        hh.addWidget(self._title)
        self._chip = QLabel("")
        self._chip.setFixedHeight(24)
        hh.addWidget(self._chip, 0, Qt.AlignVCenter)
        hh.addStretch(1)
        self._show_set_btn = QPushButton(" Settings")
        self._show_set_btn.setCursor(Qt.PointingHandCursor)
        self._show_set_btn.setFixedHeight(32)
        self._show_set_btn.clicked.connect(lambda: self._set_settings_collapsed(False))
        hh.addWidget(self._show_set_btn)
        mv.addWidget(self._head)
        # the stage
        self._stage_scroll = QScrollArea()
        self._stage_scroll.setWidgetResizable(True)
        self._stage_scroll.setFrameShape(QFrame.NoFrame)
        self._stage_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        stage = QWidget()
        stage.setObjectName("liveStage")
        self._stage = sv = QVBoxLayout(stage)
        sv.setContentsMargins(30, 22, 30, 16)
        sv.setSpacing(0)
        sv.addStretch(1)                                  # (index 0: centres the page while it's empty)
        col = QWidget()
        col.setMaximumWidth(620)
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        from app.ui.agents_view import SetupCard
        self._setup_wrap = QWidget()                  # neither Claude Code nor Codex ready: how to connect one
        sw = QVBoxLayout(self._setup_wrap)
        sw.setContentsMargins(0, 0, 0, 22)
        self._setup_card = SetupCard(_av_colors(), compact=True)
        self._setup_card.setup.connect(self._on_setup_clicked)
        sw.addWidget(self._setup_card)
        self._setup_wrap.setVisible(False)
        self._setup_hooked = False
        cv.addWidget(self._setup_wrap)
        self._orb = _Orb()
        self._orb.clicked.connect(self.toggle)
        self._meter = self._orb                           # (the listener's level signal drives it)
        cv.addWidget(self._orb, 0, Qt.AlignHCenter)
        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        cv.addWidget(self.status)
        cv.addSpacing(7)
        self._sub = QLabel("")
        self._sub.setAlignment(Qt.AlignCenter)
        self._sub.setWordWrap(True)
        self._sub.setTextFormat(Qt.RichText)
        self._sub.setFixedWidth(480)                  # (a centred word-wrapped label sized itself for 2 lines of 3 → the
                                                      #  last line of the "off" text was cut: a fixed width wraps it right)
        cv.addWidget(self._sub, 0, Qt.AlignHCenter)
        self._heard = QLabel("")
        self._heard.setAlignment(Qt.AlignCenter)
        self._heard.setWordWrap(True)
        self._heard.setVisible(False)
        cv.addWidget(self._heard)
        self._heard_timer = QTimer(self)
        self._heard_timer.setSingleShot(True)
        self._heard_timer.timeout.connect(lambda: self._heard.setVisible(False))
        cv.addSpacing(16)
        self.start_btn = QPushButton("Start listening")
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.setIconSize(QSize(17, 17))
        self.start_btn.clicked.connect(self.toggle)
        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setCursor(Qt.PointingHandCursor)
        self.mute_btn.setIconSize(QSize(15, 15))
        self.mute_btn.setToolTip("Closes the microphone until you unmute. Shortcut: Shift, then Ctrl + Win")
        self.mute_btn.clicked.connect(self.toggle_mute)
        self.mute_btn.setVisible(False)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        buttons.addWidget(self.mute_btn)
        buttons.addWidget(self.start_btn)
        buttons.addStretch(1)
        cv.addLayout(buttons)
        # "Try saying" (while nothing's been heard yet — the old empty-activity line)
        self._empty = QWidget()
        ev = QHBoxLayout(self._empty)
        ev.setContentsMargins(0, 24, 0, 0)
        ev.setSpacing(8)
        ev.addStretch(1)
        self._try_lbl = QLabel("Try saying")
        ev.addWidget(self._try_lbl)
        self._quotes = [QLabel(q) for q in ("“Make a landing page”", "“What's in my Downloads?”", "“Tell me more”")]
        for q in self._quotes:
            ev.addWidget(q)
        ev.addStretch(1)
        cv.addWidget(self._empty)
        self.tour_btn = QPushButton(" See what it can do")
        self.tour_btn.setIconSize(QSize(13, 13))
        self.tour_btn.setCursor(Qt.PointingHandCursor)
        self.tour_btn.setFixedHeight(30)
        self.tour_btn.clicked.connect(self._show_tour)
        self._tour_wrap = QWidget()
        tw = QVBoxLayout(self._tour_wrap)
        tw.setContentsMargins(0, 22, 0, 0)
        tw.addWidget(self.tour_btn, 0, Qt.AlignHCenter)
        cv.addWidget(self._tour_wrap)
        # collecting (⌃⌃): what goes as ONE message
        self._tray = QFrame()
        self._tray.setObjectName("liveTray")
        tv = QVBoxLayout(self._tray)
        tv.setContentsMargins(16, 14, 16, 14)
        tv.setSpacing(0)
        th = QHBoxLayout()
        self._tray_title = QLabel("GOES AS ONE MESSAGE")
        th.addWidget(self._tray_title)
        th.addStretch(1)
        self._tray_hint = QLabel("Esc throws it all away")
        th.addWidget(self._tray_hint)
        tv.addLayout(th)
        tv.addSpacing(4)
        self._tray_items = QVBoxLayout()
        self._tray_items.setSpacing(0)
        tv.addLayout(self._tray_items)
        ta = QHBoxLayout()
        ta.setSpacing(8)
        ta.addStretch(1)
        self._tray_drop = QPushButton("Throw away  Esc")
        self._tray_drop.setCursor(Qt.PointingHandCursor)
        self._tray_drop.setFixedHeight(36)
        self._tray_drop.clicked.connect(lambda: self._drop_collection("Thrown away — not sent"))
        ta.addWidget(self._tray_drop)
        self._tray_send = QPushButton("Send all  Ctrl")
        self._tray_send.setCursor(Qt.PointingHandCursor)
        self._tray_send.setFixedHeight(36)
        self._tray_send.clicked.connect(lambda: self._send_collection())
        ta.addWidget(self._tray_send)
        tv.addSpacing(10)
        tv.addLayout(ta)
        self._tray_wrap = QWidget()
        trw = QVBoxLayout(self._tray_wrap)
        trw.setContentsMargins(30, 20, 30, 0)
        trw.addWidget(self._tray)
        self._tray_wrap.setVisible(False)
        cv.addWidget(self._tray_wrap)
        # what it heard
        self._feed = QWidget()
        fv = QVBoxLayout(self._feed)
        fv.setContentsMargins(0, 22, 0, 0)
        fv.setSpacing(8)
        fh = QHBoxLayout()
        fh.setContentsMargins(2, 0, 2, 0)
        self._act_title = QLabel("WHAT IT HEARD")
        fh.addWidget(self._act_title)
        fh.addStretch(1)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_activity)
        fh.addWidget(self.clear_btn)
        fv.addLayout(fh)
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(8)
        fv.addLayout(self._rows_box)
        self._feed.setVisible(False)
        cv.addWidget(self._feed)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(col, 100)
        row.addStretch(1)
        sv.addLayout(row)
        sv.addStretch(1)
        self._stage_scroll.setWidget(stage)
        from PySide6.QtWidgets import QStackedWidget
        from app.ui.assistant_history_view import HistoryView, _Elided
        self._pages = QStackedWidget()                    # the Assistant / History switch at the bottom
        self._history = HistoryView(self._mw, self._c)
        self._pages.addWidget(self._stage_scroll)
        self._pages.addWidget(self._history)
        mv.addWidget(self._pages, 1)
        # footer: Assistant | History · privacy · voice used today
        self._foot = QFrame()
        self._foot.setObjectName("liveFoot")
        fo = QHBoxLayout(self._foot)
        fo.setContentsMargins(26, 11, 26, 11)
        fo.setSpacing(10)
        self._tabs = QFrame()
        self._tabs.setObjectName("liveSegs")
        tab_row = QHBoxLayout(self._tabs)
        tab_row.setContentsMargins(3, 3, 3, 3)
        tab_row.setSpacing(3)
        tab_group = QButtonGroup(self)
        self.tab_btns = []
        for i, label in enumerate(("Assistant", "History")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(28)
            b.clicked.connect(lambda _=False, n=i: self._pages.setCurrentIndex(n))
            tab_group.addButton(b)
            self.tab_btns.append(b)
            tab_row.addWidget(b)
        fo.addWidget(self._tabs)
        fo.addSpacing(8)
        self._lock = QLabel()
        fo.addWidget(self._lock)
        # (shortens itself: a footer that won't shrink pushes Settings off the window - Mac issue 13; Segoe UI is wider)
        self._hint = _Elided("Only your speech is sent — never the silence.", short="Only your speech is sent.")
        self._hint.setToolTip("Only your speech is sent — never the silence. It steps aside while another app uses the "
                              "mic, while you use a Filect shortcut, and while your PC is locked.")
        fo.addWidget(self._hint, 1)
        self.usage_label = QLabel("")
        fo.addWidget(self.usage_label)
        self._usage_bar = _UsageBar()
        fo.addWidget(self._usage_bar, 0, Qt.AlignVCenter)
        mv.addWidget(self._foot)
        outer.addWidget(main, 1)
        outer.addWidget(self._build_settings())
        self.setWidget(page)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._count_timer = QTimer(self)                  # the countdown text between the listener's reports
        self._count_timer.setInterval(50)
        self._count_timer.timeout.connect(self._count_text)
        self._age_timer = QTimer(self)                    # "now" → "2 min" on the rows
        self._age_timer.setInterval(30000)
        # Only the age changes with time (everything else is restyled when it changes): a full restyle of 100 rows
        # froze the window ~0.8 s every 30 s (measured, 2026-11-10).
        self._age_timer.timeout.connect(lambda: ([r.when.setText(_ago(r.t0)) for r in self._rows.values()],
                                                 self._history.tick()))
        self._age_timer.start()
        self._set_settings_collapsed(bool(getattr(settings, "live_settings_collapsed", False)), save=False)
        self.apply_theme()
        self._on_state("off", "")

    def _build_settings(self):
        """The Settings panel (right; hide it with its button — remembered): Listening · Answers · Sounds & keys."""
        from app.core.speech_out import VOICES, current_voice
        self._set_card = QFrame()
        self._set_card.setObjectName("liveSettings")
        self._set_card.setFixedWidth(306)
        pv = QVBoxLayout(self._set_card)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(0)
        top = QFrame()
        top.setObjectName("liveSetTop")
        top.setFixedHeight(62)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(20, 0, 14, 0)
        self._set_title = QLabel("Settings")
        tl.addWidget(self._set_title, 1)
        self._hide_set_btn = QPushButton()
        self._hide_set_btn.setToolTip("Hide settings")
        self._hide_set_btn.setFixedSize(38, 38)
        self._hide_set_btn.setIconSize(QSize(17, 17))
        self._hide_set_btn.setCursor(Qt.PointingHandCursor)
        self._hide_set_btn.clicked.connect(lambda: self._set_settings_collapsed(True))
        tl.addWidget(self._hide_set_btn)
        pv.addWidget(top)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._set_scroll = scroll
        body = QWidget()
        body.setObjectName("liveSetBody")
        sc = QVBoxLayout(body)
        sc.setContentsMargins(20, 2, 20, 16)
        sc.setSpacing(0)
        self._sec_lbls = []

        def sec(text):
            lab = QLabel(text.upper())
            lab.setContentsMargins(0, 16, 0, 6)
            self._sec_lbls.append(lab)
            sc.addWidget(lab)

        # -- Agent (Windows addition, 2026-11-09: "how do I switch my agent? there should be the option in here")
        sec("Agent")
        self._agent_lbl = QLabel("New requests go to")
        self._agent_lbl.setContentsMargins(0, 7, 0, 4)
        sc.addWidget(self._agent_lbl)
        self._agent_segs = QFrame()
        self._agent_segs.setObjectName("liveSegs")
        ag = QHBoxLayout(self._agent_segs)
        ag.setContentsMargins(2, 2, 2, 2)
        ag.setSpacing(0)
        self.agent_group = QButtonGroup(self)
        self.agent_group.setExclusive(True)
        self.agent_btns = {}
        from app.ui.agents_view import AGENT_NAMES, agent_logo
        from PySide6.QtGui import QIcon
        for key in ("claude", "codex"):
            b = QPushButton(f" {AGENT_NAMES[key]}")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            b.setIcon(QIcon(agent_logo(key, 16)))
            b.setIconSize(QSize(15, 15))
            b.clicked.connect(lambda _=False, k=key: self._on_agent_pick(k))
            self.agent_group.addButton(b)
            self.agent_btns[key] = b
            ag.addWidget(b, 1)
        sc.addWidget(self._agent_segs)
        self._agent_hint = QLabel("")
        self._agent_hint.setWordWrap(True)
        self._agent_hint.setContentsMargins(0, 5, 0, 0)
        sc.addWidget(self._agent_hint)
        self._show_agent()
        # -- Listening
        sec("Listening")
        self._wait_lbl = QLabel("Send after I stop talking")
        self._wait_val = QLabel("")
        r = QHBoxLayout()
        r.setContentsMargins(0, 7, 0, 4)
        r.addWidget(self._wait_lbl, 1)
        r.addWidget(self._wait_val, 0)
        sc.addLayout(r)
        self.wait_slider = QSlider(Qt.Horizontal)
        self.wait_slider.setRange(2, 20)                       # half-seconds: 1.0 – 10.0 s
        self.wait_slider.setSingleStep(1)
        self.wait_slider.setPageStep(2)
        self.wait_slider.setValue(int(round(float(settings.live_send_after_sec) * 2)))
        self.wait_slider.valueChanged.connect(self._on_wait)
        sc.addWidget(self.wait_slider)
        ends = QHBoxLayout()
        self._ends = (QLabel("1 s · quick"), QLabel("10 s · room to think"))
        ends.addWidget(self._ends[0])
        ends.addStretch(1)
        ends.addWidget(self._ends[1])
        sc.addLayout(ends)
        self._on_wait(self.wait_slider.value(), save=False)
        self._sens_lbl = QLabel("Picks up")
        self._sens_lbl.setContentsMargins(0, 10, 0, 4)
        sc.addWidget(self._sens_lbl)
        self._segs = QFrame()
        self._segs.setObjectName("liveSegs")
        sg = QHBoxLayout(self._segs)
        sg.setContentsMargins(2, 2, 2, 2)
        sg.setSpacing(0)
        self.sens_group = QButtonGroup(self)
        self.sens_group.setExclusive(True)
        self.sens_btns = {}
        for key, label in (("low", "Close voices"), ("medium", "Normal"), ("high", "Soft voices too")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(28)
            b.setChecked(settings.live_sensitivity == key)
            b.setToolTip({"low": "Only voices close to the mic — if it picks up people around you",
                          "medium": "", "high": "If it misses you"}[key])
            b.clicked.connect(lambda _=False, k=key: self._on_sens(k))
            self.sens_group.addButton(b)
            self.sens_btns[key] = b
            sg.addWidget(b, 1)
        sc.addWidget(self._segs)
        self._sens_hint = None
        self.noise_toggle, self._noise_lbl, self._noise_hint = self._toggle_row(
            sc, "Filter background noise", settings.live_noise_filter, self._on_noise,
            "Ignores noise and what your PC plays")
        self.noise_toggle._row.setVisible(False)          # Windows: no echo cancellation / noise filter to switch
        # -- Answers
        sec("Answers")
        self.speak_toggle, self._speak_lbl, self._speak_hint = self._toggle_row(
            sc, "Speak replies", settings.live_speak, self._on_speak,
            "A sentence or two — say “tell me more” for the rest")
        self.voice_group = QButtonGroup(self)
        self.voice_group.setExclusive(True)
        self.voice_btns, self.voice_play = {}, {}
        cur = current_voice()
        vbox = QVBoxLayout()
        vbox.setContentsMargins(0, 4, 0, 4)
        vbox.setSpacing(5)
        for vid, name, desc, _gender in VOICES:
            b = _VoiceButton(name, desc)
            b.setChecked(vid == cur)
            b.clicked.connect(lambda _=False, v=vid: self._on_voice(v))
            self.voice_group.addButton(b)
            self.voice_btns[vid] = b
            b.play.setToolTip(f"Hear {name}")
            b.play.clicked.connect(lambda _=False, v=vid: self._preview_voice(v))
            self.voice_play[vid] = b.play
            vbox.addWidget(b)
        sc.addLayout(vbox)
        self._voice_lbl = QLabel("")                    # (only when a sample couldn't load)
        self._voice_lbl.setWordWrap(True)
        self._voice_lbl.setVisible(False)
        sc.addWidget(self._voice_lbl)
        self.open_toggle, self._open_lbl, self._open_hint = self._toggle_row(
            sc, "Open answers automatically", settings.assistant_auto_open, self._on_auto_open,
            "Off: they wait at the top of your screen")
        self.organize_toggle, self._organize_lbl, self._organize_hint = self._toggle_row(
            sc, "Preview before organizing", settings.live_organize_preview, self._on_organize_preview,
            "Off: files move right away. Undo anytime in History")
        # -- Sounds & keys
        sec("Sounds & keys")
        self.chime_toggle, self._chime_lbl, _ = self._toggle_row(
            sc, "Chime when it starts", settings.live_chime, self._on_chime, "")
        drow = QHBoxLayout()
        drow.setContentsMargins(0, 7, 0, 7)
        self._draw_lbl = QLabel("Draw on your screen")
        drow.addWidget(self._draw_lbl, 1)
        self.draw_key = QLabel("")
        self.draw_key.setToolTip("Press Shift and Ctrl together, draw, press them again to capture. Esc cancels.")
        drow.addWidget(self.draw_key, 0)
        sc.addLayout(drow)
        crow = QHBoxLayout()
        crow.setContentsMargins(0, 7, 0, 7)
        self._many_lbl = QLabel("Several things at once")
        crow.addWidget(self._many_lbl, 1)
        self._many_key = QLabel("")
        self._many_key.setToolTip("Double-tap Ctrl, say / draw it all, tap Ctrl once to send it together.")
        crow.addWidget(self._many_key, 0)
        sc.addLayout(crow)
        self._draw_hint = None
        sc.addStretch(1)
        scroll.setWidget(body)
        pv.addWidget(scroll, 1)
        self._act_card = self._feed
        self._hero = self._stage_scroll
        return self._set_card

    def _toggle_row(self, parent_layout, label, on, handler, hint):
        roww = QWidget()                                  # (a widget, so a row can be hidden — the noise filter)
        row = QHBoxLayout(roww)
        row.setContentsMargins(0, 7, 0, 7)
        row.setSpacing(12)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        lbl = QLabel(label)
        txt.addWidget(lbl)
        h = None
        if hint:
            h = QLabel(hint)
            h.setWordWrap(True)
            txt.addWidget(h)
        row.addLayout(txt, 1)
        t = _Switch()
        t.setChecked(bool(on))
        t.toggled.connect(lambda v, b=t: (self._style_toggle(b), handler(v)))
        row.addWidget(t, 0, Qt.AlignVCenter)
        parent_layout.addWidget(roww)
        t._row = roww
        self._style_toggle(t)
        return t, lbl, h

    def _style_toggle(self, t):
        t.setText("On" if t.isChecked() else "Off")
        c = getattr(self, "_c", None)
        if c is not None:
            t.set_colors(c["border_strong"], "#E8E8F0" if c.get("dark") else "#FFFFFF")
        t.update()

    def _keys(self, keys) -> str:
        c = self._c
        k = (f'<span style="background:{c["surface"]}; color:{c["text_secondary"]}; border:1px solid '
             f'{c["border_strong"]}; border-radius:5px; font-weight:600;">&nbsp;{{}}&nbsp;</span>')
        out = []
        for t in keys:
            out.append(f'<span style="color:{c["text_muted"]};">&nbsp;{t}&nbsp;</span>' if t in ("+", "…")
                       else k.format(t))
        return "".join(out)

    def showEvent(self, e):
        super().showEvent(e)
        ap = self._hook_setup()
        if ap is not None and (getattr(ap, "_agents_check", None) is not None or getattr(ap, "_speedups", False)):
            ap.refresh_agents_setup()                 # (you may have set one up outside Filect)

    def _hook_setup(self):
        """Follow the Agents page's "is Claude Code / Codex ready?" — the banner shows only when neither is."""
        ap = getattr(self._mw, "assistant_page", None)
        if ap is None or not hasattr(ap, "agents_setup_changed"):
            return None
        if not self._setup_hooked:
            self._setup_hooked = True
            ap.agents_setup_changed.connect(self._on_agents_setup)
            if hasattr(ap, "primary_changed"):
                ap.primary_changed.connect(lambda _a: self._show_agent())
            self._on_agents_setup(ap._agents_state)
        self._show_agent()
        return ap

    def _on_agents_setup(self, state):
        from app.ui.agents_view import needs_setup
        self._setup_card.set_state(state)
        self._setup_wrap.setVisible(needs_setup(state))
        self._show_agent()

    # ---- the agent new requests go to (the same pick as the Agents page's agent menu) ------------------------
    def _current_agent(self) -> str:
        ap = getattr(self._mw, "assistant_page", None)
        a = getattr(ap, "_primary", None) or getattr(settings, "assistant_agent", "claude")
        return a if a in ("claude", "codex") else "claude"

    def _show_agent(self):
        """✓ on the picked agent; under it, what it means — or that the picked one isn't set up (another takes over)."""
        btns = getattr(self, "agent_btns", None)
        if not btns:
            return
        from app.ui.agents_view import AGENT_NAMES
        cur = self._current_agent()
        for k, b in btns.items():
            if b.isChecked() != (k == cur):
                b.setChecked(k == cur)
        ap = getattr(self._mw, "assistant_page", None)
        st = getattr(ap, "_agents_state", None) or {}
        other = "codex" if cur == "claude" else "claude"
        if st.get(cur) and st.get(cur) != "ready":
            hint = (f"{AGENT_NAMES[cur]} isn't set up yet — set it up in Agents"
                    + (f". {AGENT_NAMES[other]} answers meanwhile." if st.get(other) == "ready" else "."))
            warn = True
        else:
            hint, warn = "Follow-ups stay with their chat's agent. Switching starts a new chat.", False
        if self._agent_hint.text() != hint:
            self._agent_hint.setText(hint)
        c = getattr(self, "_c", None)
        if c is not None:
            self._set_style(self._agent_hint, f"color:{AMBER if warn else c['text_muted']}; font-size:11.5px;")

    def _on_agent_pick(self, agent: str):
        ap = getattr(self._mw, "assistant_page", None)
        if ap is not None and hasattr(ap, "_set_primary"):
            ap._set_primary(agent)                     # (saves it, updates the Agents page + card, readies Claude Code)
        else:
            settings.assistant_agent = agent
            try:
                settings._save_config()
            except Exception:
                pass
        self._show_agent()
        _track("live_agent", agent=agent)

    def _on_setup_clicked(self, agent: str):
        ap = getattr(self._mw, "assistant_page", None)
        if ap is not None:
            ap.setup_agent(agent)

    def apply_theme(self, theme=None):
        from app.ui.icons import line_icon, line_pixmap
        self._c = c = _colors(theme)
        if hasattr(self, "_setup_card"):
            self._setup_card.apply_theme(_av_colors(theme))
        dark = c["dark"]
        self.setStyleSheet(
            f"QScrollArea#livePage, QWidget#liveContent, QWidget#liveStage, QWidget#liveSetBody {{ background:{c['bg']}; "
            "border:none; } QWidget#liveContent QLabel { background:transparent; border:none; } "
            f"QFrame#liveHead {{ background:transparent; border:none; border-bottom:1px solid {c['border']}; }} "
            f"QFrame#liveFoot {{ background:transparent; border:none; border-top:1px solid {c['border']}; }} "
            f"QFrame#liveSettings {{ background:{c['bg']}; border:none; border-left:1px solid {c['border']}; }} "
            f"QFrame#liveSetTop {{ background:transparent; border:none; border-bottom:1px solid {c['border']}; }} "
            f"QFrame#liveTray {{ background:{c['surface']}; border:1.5px dashed rgba(124,77,255,0.55); border-radius:16px; }} "
            f"QFrame#liveSegs {{ background:{c['input_bg']}; border:1px solid {c['border']}; border-radius:9px; }} "
            "QScrollBar:vertical { width:8px; background:transparent; } "
            f"QScrollBar::handle:vertical {{ background:{c['border_strong']}; border-radius:4px; min-height:30px; }} "
            "QScrollBar::add-line, QScrollBar::sub-line { height:0; }")
        for sa in (self._stage_scroll, self._set_scroll):
            sa.viewport().setStyleSheet(f"background:{c['bg']};")
        self._title.setStyleSheet(f"color:{c['text']}; font-family:'Sora','SF Pro Display',sans-serif; font-size:16.5px; "
                                  "font-weight:600;")
        self._show_set_btn.setIcon(line_icon("gear", 13, c["text_secondary"]))
        self._show_set_btn.setStyleSheet(
            f"QPushButton {{ background:{c['surface']}; color:{c['text_secondary']}; border:1px solid {c['border']}; "
            "border-radius:9px; padding:0 12px; font-size:12.5px; font-weight:600; } "
            f"QPushButton:hover {{ color:{ACCENT}; border-color:{ACCENT}; }}")
        self._orb.set_dark(dark)
        self._orb.set_bg(c["bg"])
        self._applied = {}                              # (theme changed: restyle everything once)
        self._sub.setStyleSheet(f"color:{c['text_muted']}; font-size:14px;")
        self._heard.setStyleSheet(f"color:{c['text_secondary']}; font-size:14.5px; font-style:italic; padding-top:6px;")
        self._try_lbl.setStyleSheet(f"color:{c['text_muted']}; font-size:12px; font-weight:600;")
        for q in self._quotes:
            q.setStyleSheet(f"background:{c['surface']}; border:1px solid {c['border']}; border-radius:15px; "
                            f"color:{c['text_secondary']}; font-size:12.5px; padding:6px 12px;")
        style_tour_button(self.tour_btn, bool(getattr(settings, "live_tour_seen", False)), c)
        self._tray_title.setStyleSheet(f"color:{ACCENT}; font-size:12px; font-weight:700; letter-spacing:0.3px;")
        self._tray_hint.setStyleSheet(f"color:{c['text_muted']}; font-size:12px;")
        self._tray_drop.setStyleSheet(
            f"QPushButton {{ background:{c['surface']}; color:{c['text_secondary']}; border:1px solid {c['border_strong']}; "
            "border-radius:11px; padding:0 16px; font-size:13px; font-weight:600; }")
        self._tray_send.setStyleSheet(
            f"QPushButton {{ background:{ACCENT}; color:white; border:none; border-radius:12px; padding:0 16px; "
            "font-size:13px; font-weight:600; } QPushButton:hover { background:#8E63FF; }")
        self._act_title.setStyleSheet(f"color:{c['text_muted']}; font-size:11px; font-weight:700; letter-spacing:0.5px;")
        self.clear_btn.setStyleSheet(f"QPushButton {{ background:transparent; color:{c['text_muted']}; border:none; "
                                     f"padding:0; font-size:12px; }} QPushButton:hover {{ color:{c['text']}; }}")
        self._lock.setPixmap(line_pixmap("lock", 14, c["text_muted"]))
        for w in (self._hint, self.usage_label):
            w.setStyleSheet(f"color:{c['text_muted']}; font-size:12px;")
        self._usage_bar._track = QColor(c["border_strong"])
        self._usage_bar.update()
        # settings panel
        self._set_title.setStyleSheet(f"color:{c['text']}; font-family:'Sora','SF Pro Display',sans-serif; font-size:15px; "
                                      "font-weight:600;")
        self._hide_set_btn.setIcon(line_icon("sidebar", 17, ACCENT, c["text_muted"]))
        self._hide_set_btn.setStyleSheet(
            f"QPushButton {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:11px; padding:0; }} "
            f"QPushButton:hover {{ border-color:{ACCENT}; }}")
        for lab in self._sec_lbls:
            lab.setStyleSheet(f"color:{c['text_muted']}; font-size:11px; font-weight:700; letter-spacing:0.5px;")
        body = f"color:{c['text']}; font-size:13px; font-weight:500;"
        for w in (self._wait_lbl, self._sens_lbl, self._noise_lbl, self._chime_lbl, self._speak_lbl, self._draw_lbl,
                  self._open_lbl, self._organize_lbl, self._many_lbl, self._agent_lbl):
            w.setStyleSheet(body)
        for w in (self._noise_hint, self._speak_hint, self._open_hint, self._organize_hint, self._voice_lbl):
            if w is not None:
                w.setStyleSheet(f"color:{c['text_muted']}; font-size:11.5px;")
        for w in self._ends:
            w.setStyleSheet(f"color:{c['text_muted']}; font-size:10.5px;")
        self._wait_val.setStyleSheet(f"color:{ACCENT}; font-size:12.5px; font-weight:700;")
        self.wait_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height:4px; background:{c['border_strong']}; border-radius:2px; }}"
            f"QSlider::sub-page:horizontal {{ background:{ACCENT}; border-radius:2px; }}"
            f"QSlider::handle:horizontal {{ background:{'#E8E8F0' if dark else '#FFFFFF'}; border:2px solid {ACCENT}; "
            "width:12px; height:12px; margin:-6px 0; border-radius:8px; }")
        self._history.apply_theme(c)
        for b in list(self.sens_btns.values()) + list(getattr(self, "agent_btns", {}).values()) + self.tab_btns:
            b.setStyleSheet(
                f"QPushButton {{ background:transparent; color:{c['text_secondary']}; border:none; border-radius:7px; "
                "padding:0 4px; font-size:12px; } "
                f"QPushButton:checked {{ background:{c['surface']}; color:{ACCENT}; font-weight:600; "
                f"border:1px solid {c['border']}; }}")
        for b in self.voice_btns.values():
            b._c = c
            b.play._c = c
            b.update()
        self.draw_key.setText(self._keys(["Shift", "+", "Ctrl"]))
        self._many_key.setText(self._keys(["Ctrl", "Ctrl", "…", "Ctrl"]))
        for t in (self.noise_toggle, self.chime_toggle, self.speak_toggle, self.open_toggle, self.organize_toggle):
            self._style_toggle(t)
        for row in self._rows.values():
            row.restyle()
        self._style_start()
        self._on_state(self._state, getattr(self, "_detail", ""))
        self._show_agent()

    def _style_start(self):
        from app.ui.icons import line_icon
        c = self._c
        if self.listener.on:
            self.start_btn.setText("  Stop listening")
            self.start_btn.setIcon(line_icon("stop", 13, c["text_secondary"]))
            self.start_btn.setFixedHeight(38)
            self.start_btn.setGraphicsEffect(None)
            self.start_btn.setStyleSheet(
                f"QPushButton {{ background:{c['surface']}; color:{c['text_secondary']}; border:1px solid "
                f"{c['border_strong']}; border-radius:11px; padding:0 16px; font-size:13px; font-weight:600; }} "
                f"QPushButton:hover {{ border-color:{ACCENT}; color:{ACCENT}; }}")
        else:
            self.start_btn.setText("  Start listening")
            self.start_btn.setIcon(line_icon("mic", 17, "#FFFFFF"))
            self.start_btn.setFixedHeight(44)
            self.start_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:white; border:none; border-radius:12px; "
                "padding:0 22px; font-size:14px; font-weight:600; } QPushButton:hover { background:#8E63FF; }")
            if self.start_btn.graphicsEffect() is None:
                from PySide6.QtWidgets import QGraphicsDropShadowEffect
                fx = QGraphicsDropShadowEffect(self.start_btn)
                fx.setBlurRadius(22)
                fx.setOffset(0, 8)
                fx.setColor(QColor(124, 77, 255, 70))
                self.start_btn.setGraphicsEffect(fx)
        self._applied.pop("mute", None)       # (the theme may have changed)
        self._style_mute()
        self._update_layout()

    def _style_mute(self):
        from app.ui.icons import line_icon
        on, muted = self.listener.on, bool(getattr(self.listener, "user_muted", False))
        if self._applied.get("mute") == (on, muted):
            return
        self._applied["mute"] = (on, muted)
        c = self._c
        amber = "#E0B060" if c.get("dark") else "#B9861F"
        col = amber if muted else c["text_secondary"]
        self.mute_btn.setVisible(on)
        self.mute_btn.setText("  Unmute" if muted else "  Mute")
        self.mute_btn.setIcon(line_icon("mic" if muted else "mute", 15, col))
        self.mute_btn.setFixedHeight(38)
        self.mute_btn.setStyleSheet(
            f"QPushButton {{ background:{c['surface']}; color:{col}; border:1px solid "
            f"{amber if muted else c['border_strong']}; border-radius:11px; padding:0 16px; font-size:13px; "
            f"font-weight:600; }} QPushButton:hover {{ border-color:{ACCENT}; color:{ACCENT}; }}")

    def _update_layout(self):
        """Empty & off: everything centred, the big orb. Otherwise the orb is smaller and the page starts at
        the top (what it heard grows below)."""
        busy = bool(self._rows) or self.listener.on or self._collect is not None
        self._orb.set_big(not busy)
        self._stage.setStretch(0, 0 if busy else 1)
        self._tour_wrap.setVisible(not self.listener.on)
        self._feed.setVisible(bool(self._rows))

    def _set_settings_collapsed(self, collapsed: bool, save: bool = True):
        self._set_card.setVisible(not collapsed)
        self._show_set_btn.setVisible(collapsed)
        if save:
            settings.live_settings_collapsed = bool(collapsed)
            try:
                settings._save_config()
            except Exception:
                pass

    def _show_tour(self):
        """"See what it can do": this page's tour (page_tours.py), in the first-run guide's style."""
        from app.ui import page_tours
        page_tours.mark_seen("assistant")
        style_tour_button(self.tour_btn, True, self._c)
        self._tour = page_tours.open_tour(self.window(), "assistant")

    def _refresh_tray(self):
        """Collecting (⌃⌃): the numbered list of what goes as ONE message — screenshots and words in order."""
        col = self._collect
        self._tray_wrap.setVisible(col is not None)
        if col is None:
            self._orb.set_badge(0)
            return
        c = self._c
        while self._tray_items.count():
            it = self._tray_items.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        bits = list(col.get("bits") or [])
        if self._state == "hearing":
            bits.append(("live", ""))
        for i, (kind, val) in enumerate(bits, 1):
            row = QWidget()
            row.setObjectName("trayBit")
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 8, 0, 8)
            h.setSpacing(10)
            n = QLabel(str(i))
            n.setFixedSize(20, 20)
            n.setAlignment(Qt.AlignCenter)
            n.setStyleSheet(f"background:{c['purple_light_bg'] if not c['dark'] else 'rgba(124,77,255,0.16)'}; "
                            f"color:{ACCENT}; border-radius:6px; font-size:11px; font-weight:700;")
            h.addWidget(n)
            if kind == "shot":
                th = QLabel()
                th.setFixedSize(64, 40)
                pm = QPixmap(val)
                if not pm.isNull():
                    pm = pm.scaled(128, 80, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    pm = pm.copy((pm.width() - 128) // 2, 0, 128, 80)
                    pm.setDevicePixelRatio(2)
                    th.setPixmap(pm)
                th.setStyleSheet(f"border:1px solid {c['border']}; border-radius:7px;")
                h.addWidget(th)
                t = QLabel("Screenshot with your drawing")
                t.setStyleSheet(f"color:{c['text']}; font-size:13.5px;")
            elif kind == "live":
                t = QLabel("…hearing you")
                t.setStyleSheet(f"color:{c['text_muted']}; font-size:13.5px; font-style:italic;")
            else:
                t = QLabel(f"“{val}”")
                t.setWordWrap(True)
                t.setStyleSheet(f"color:{c['text']}; font-size:13.5px;")
            h.addWidget(t, 1)
            if i < len(bits):
                row.setStyleSheet(f"QWidget#trayBit {{ border-bottom:1px solid {c['border']}; }}")
            self._tray_items.addWidget(row)
        self._orb.set_badge(len(col.get("bits") or ()))

    def _show_heard(self, text: str):
        self._heard.setText(f"“{text}”")
        self._heard.setVisible(True)
        self._heard_timer.start(8000)

    # ---- start / stop --------------------------------------------------------------------------------
    def toggle(self):
        if self.listener.on:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.listener.on:
            return
        ap = getattr(self._mw, "assistant_page", None)
        self._hook_ap(ap)
        if ap is not None and hasattr(ap, "live_answer_context") and hasattr(self.listener, "set_answer_probe"):
            # a short "yes" counts while the agent is waiting for your answer (else: noise, never sent)
            self.listener.set_answer_probe(lambda: ap.live_answer_context() is not None)
        self._started_at = time.time()
        self.listener.start()
        self._hands_free(True)
        self._style_start()
        _track("live_started", send_after=float(settings.live_send_after_sec),
               sensitivity=settings.live_sensitivity, noise_filter=bool(settings.live_noise_filter))

    def stop(self):
        if not self.listener.on:
            self._style_start()
            return
        self.listener.stop()
        self._style_start()
        if self._started_at:
            _track("live_stopped", minutes=round((time.time() - self._started_at) / 60.0, 1))
            self._started_at = None

    # ---- listener → UI ---------------------------------------------------------------------------------
    def _on_state(self, state: str, detail: str):
        self._state, self._detail = state, detail
        c = self._c
        amber_t = "#E0B060" if c.get("dark") else "#B9861F"
        keys = (f"Point at something with {self._keys(['Shift', '+', 'Ctrl'])} · several things at once with "
                f"{self._keys(['Ctrl', 'Ctrl'])}")
        text, sub, color = {
            "off": ("Talk to your agents — hands-free", "Turn it on and just say what you want. Claude Code or Codex "
                    "does it, and the card at the top shows the work.", c["text"]),
            "starting": (detail or "Starting the microphone…", "", c["text_muted"]),
            "listening": ("Listening — just talk", keys, ACCENT),
            "hearing": ("Hearing you…", "Esc throws away what you're saying", ACCENT),
            "countdown": ("Sending soon", "Esc cancels", ACCENT),
            "paused": (detail or "Paused", "It carries on by itself", amber_t),
            "error": (detail or "Something went wrong.", "", RED),
        }.get(state, (detail or state, "", c["text_muted"]))
        muted = state == "paused" and bool(getattr(self.listener, "user_muted", False))
        if muted:
            text, sub = ("Muted", f"It can't hear anything. Press {self._keys(['Shift'])} then "
                                  f"{self._keys(['Ctrl', '+', 'Win'])} to unmute.")
        collecting = self._collect is not None and state in ("listening", "hearing", "countdown")
        if collecting:
            n = len(self._collect.get("bits") or ())
            text = {"listening": (f"Collecting — {n} thing{'s' if n != 1 else ''} so far" if n
                                  else "Collecting — nothing yet"),
                    "hearing": "Hearing you… (collecting)", "countdown": "Adding it…"}[state]
            sub = f"Keep talking, scrolling and snapping. Tap {self._keys(['Ctrl'])} to send it all as one message."
        self._set_text(self.status, text)
        self._set_style(self.status, f"color:{color}; font-family:'Sora','SF Pro Display',sans-serif; font-size:21px; "
                                     "font-weight:700; padding-top:4px;")
        self._set_text(self._sub, sub)
        self._sub.setVisible(bool(sub))
        if state == "off":
            self._heard.setVisible(False)
        chip, ccol, cbg, cbd = {
            "off": ("Off", c["text_muted"], c["surface"], c["border"]),
            "starting": ("Starting…", c["text_muted"], c["surface"], c["border"]),
            "paused": ("Muted" if muted else "Paused", amber_t, "rgba(224,163,58,0.08)", "rgba(224,163,58,0.55)"),
            "error": ("Can't listen", RED, "rgba(229,83,75,0.07)", "rgba(229,83,75,0.45)"),
        }.get(state, ("Collecting" if collecting else "Listening", ACCENT,
                      "rgba(124,77,255,0.16)" if c.get("dark") else c["purple_light_bg"], "rgba(124,77,255,0.35)"))
        self._set_text(self._chip, f'<span style="color:{ccol};">●</span>&nbsp; {chip}')
        self._set_style(self._chip, f"color:{ccol}; background:{cbg}; border:1px solid {cbd}; border-radius:12px; "
                                    "padding:0 10px; font-size:12px; font-weight:600;")
        self._orb.set_state("collect" if collecting and state != "countdown" else
                            {"off": "off", "starting": "starting", "listening": "listen", "hearing": "hear",
                             "countdown": "count", "paused": "paused", "error": "error"}.get(state, "off"))
        if state == "countdown":
            self._orb.set_progress(0.0)
        self._meter.set_on(state in ("listening", "hearing", "countdown"))
        if self._applied.get("on") != self.listener.on:  # (Start / Stop only changes when listening does)
            self._applied["on"] = self.listener.on
            self._style_start()
        self._style_mute()
        if self._collect is not None or self._tray_wrap.isVisibleTo(self):
            self._refresh_tray()
        self._notch_light(_NOTCH.get(state, ""))
        if self._tray_action is not None:
            self._tray_action.setText("Stop Filect Assistant" if self.listener.on else "Start Filect Assistant")
        if state == "error":
            _track("live_error", kind=(detail or "")[:40])

    def _on_countdown(self, left: float):
        if self._state == "countdown":
            f = getattr(self.listener, "send_after_now", None)
            total = f() if callable(f) else settings.live_send_after_sec
            self._orb.set_countdown(left, max(float(total), float(left)))
            self._count_text()
            self._count_timer.start()

    def _count_text(self):
        """"Sending in 1.4 s", counting down in 0.1 s steps between the listener's 0.4-s reports."""
        if self._state != "countdown":
            self._count_timer.stop()
            return
        left = self._orb.left_now()
        if left is not None and self._collect is None:
            self._set_text(self.status, f"Sending in {left:.1f} s" if left >= 0.05 else "Sending…")

    def _set_text(self, label, text: str):
        if label.text() != text:                      # (a label that's set again re-lays-out the page)
            label.setText(text)

    def _set_style(self, w, css: str):
        if self._applied.get(id(w)) != css:           # (re-applying a style sheet is slow: only when it changes)
            self._applied[id(w)] = css
            w.setStyleSheet(css)

    def _on_usage(self, secs: float):
        mins = secs / 60.0
        used = f"{mins:.0f} min" if mins >= 1 else f"{secs:.0f} s"
        self.usage_label.setText(f"Voice today: {used} of 3 h")
        self._usage_bar.set_fraction(secs / (3 * 3600.0))

    def _row(self, uid) -> "_Row":
        row = self._rows.get(uid)
        if row is None:
            row = _Row(self, uid)
            self._rows[uid] = row
            self._rows_box.insertWidget(0, row)
            self._empty.setVisible(False)
            self._update_layout()
            while len(self._rows) > MAX_ROWS:
                old = min(self._rows)
                w = self._rows.pop(old)
                w.setParent(None)
                w.deleteLater()
        return row

    def _on_ended(self, uid: int):
        self._transcribing.add(uid)
        self._row(uid).set_outcome("Transcribing…", self._c["text_muted"])

    def _on_heard(self, uid: int, text: str):
        self._transcribing.discard(uid)
        row = self._row(uid)
        if uid in self._parked:                        # pulled back (you went on talking): it waits for the rest
            self._parked[uid] = text
            row.set_text(text)
            row.set_outcome("Joined with what you said next", self._c["text_muted"])
            w = self._wait_join.pop(uid, None)
            if w is not None:                          # the next part was heard first: send them together now
                self._on_heard(*w)
            return
        prev = self._joins.get(uid)
        if prev is not None:                           # what you said right after a sentence that went early
            if prev in self._parked and self._parked[prev] is None:
                self._wait_join[prev] = (uid, text)    # (its first part is still being transcribed)
                row.set_outcome("Transcribing…", self._c["text_muted"])
                return
            self._joins.pop(uid, None)
            first = self._parked.pop(prev, None)
            if first:
                text = f"{first} {text}"
            self._remove_row(prev)
            logger.info("[live] request %s = %s + %s, joined", uid, prev, uid)
        row.set_text(text)
        self._show_heard(text)
        if uid in self._talked_over:
            self._talked_over.discard(uid)
            from app.core.speech_out import is_stop_phrase
            if is_stop_phrase(text):                    # "stop talking, please" — for Filect, not the agent
                row.set_outcome("Stopped talking — not sent", self._c["text_muted"])
                logger.info("[live] request %s was just 'stop' (said over Filect) — not sent", uid)
                self._maybe_finish_send()
                return
        if self._collect is not None:                  # collecting (⌃⌃): kept, sent with the rest on ⌃
            self._add_part(uid, text)
            return
        if self._drawing:                              # said while drawing: it goes WITH that drawing
            w = self._waiting
            if w is None:
                self._waiting = {"uids": [uid], "text": text}
                row.set_outcome(f"Waiting for your drawing — press {DRAW_KEY} to send it", ACCENT)
            else:                                      # a 2nd sentence during the same drawing: one request
                w["uids"].append(uid)
                w["text"] = f"{w['text']} {text}"
                self._row(w["uids"][0]).set_text(w["text"])
                self._remove_row(uid)
            logger.info("[live] request %s waits for the drawing", uid)
            return
        self._send(uid, text)

    def _send(self, uid: int, text: str, screen_uid=None):
        row = self._row(uid)
        row.set_outcome("Checking…", self._c["text_muted"])
        self._sent_text[uid] = text
        while len(self._sent_text) > 20:
            self._sent_text.pop(next(iter(self._sent_text)))
        ap = getattr(self._mw, "assistant_page", None)
        if ap is None:
            row.set_outcome("The agents aren't ready yet", AMBER)
            return
        self._hook_ap(ap)
        images, screen = [], None
        if self._drawings:                             # your drawings go with this one (all of them)
            images, self._drawings = list(self._drawings), []
            self._drawn_for[uid] = images
            self._drawn_timer.stop()
        elif (screen_uid or uid) in self._captures:    # your screen, if it turns out to be about it
            screen = self._screen_for(screen_uid or uid)
        ap.run_voice_task(text, source="live", tag=uid, images=images, screen=screen)

    def _remove_row(self, uid):
        w = self._rows.pop(uid, None)
        if w is not None:
            w.setParent(None)
            w.deleteLater()

    def _hook_ap(self, ap):
        if ap is None or self._hooked_ap is ap:
            return
        ap.live_routed.connect(self._on_routed)
        if hasattr(ap, "live_event"):
            ap.live_event.connect(self._on_live_event)
        if hasattr(ap, "live_screen_used"):
            ap.live_screen_used.connect(self._on_screen_used)
        self._hooked_ap = ap

    def _on_screen_used(self, tag):
        self._screen_used.add(tag)

    def _shot_suffix(self, tag) -> str:
        n = len(self._drawn_for.get(tag) or ())
        if n:
            return "  · ✏️ with your drawing" if n == 1 else f"  · ✏️ with your {n} drawings"
        if tag in self._screen_used:
            return "  · 📸 with your screen"
        return ""

    def _on_routed(self, tag, outcome: str, title: str, key: str = ""):
        row = self._rows.get(tag)
        self._captures.pop(tag, None)                  # (its screen capture is no longer needed in memory)
        sent = self._drawn_for.get(tag)
        also = self._also.pop(tag, [])
        if sent and outcome not in ("new", "continued"):
            self._drawn_for.pop(tag, None)
            if outcome == "cancelled":
                # Esc: thrown away WITH the request. (His test: a cancelled "what is this?" left its
                # drawing waiting, and it went with his next, unrelated request — "tell me a poem".)
                logger.info("[live] %d drawing(s) dropped with the request you cancelled", len(sent))
            else:
                # not used (ignored / a yes-no answer / failed): they're still waiting for you
                self._drawings = (sent + self._drawings)[:MAX_DRAWINGS]
                self._keep_drawings()
        if outcome in ("new", "continued"):
            self.listener.learn(tag)
            for u in also:
                self.listener.learn(u)
            if settings.live_chime:
                _chime()
            if row is not None:
                verb = "Started" if outcome == "new" else "Continued"
                row.set_outcome(f"{verb} “{title}”{self._shot_suffix(tag)}  →", ACCENT, key)
            n = len(self._drawn_for.get(tag) or ())
            if n:
                self._note("📸  Your drawing went with it" if n == 1 else f"📸  Your {n} drawings went with it",
                           "#FFFFFF")
            elif tag in self._screen_used:
                self._note("📸  Screen attached", "#FFFFFF")
            _track("live_task", outcome=outcome, screen=tag in self._screen_used, drawings=n)
        elif outcome in ("approved", "denied"):        # "yes" / "no" to a waiting approval
            self.listener.learn(tag)
            if settings.live_chime and outcome == "approved":
                _chime()
            if row is not None:
                row.set_outcome(f"{'Approved' if outcome == 'approved' else 'Denied'} — “{title}”  →",
                                ACCENT if outcome == "approved" else AMBER, key)
            self._note("✓  Approved" if outcome == "approved" else "✕  Denied", "#FFFFFF")
            _track("live_task", outcome=outcome)
        elif outcome in ("organize", "organize_changed", "undone", "auto_setup", "auto_off", "auto_asked",
                         "auto_none", "find", "find_opened", "find_shown", "find_which", "find_all", "find_more",
                         "find_closed"):            # organizing / finding by voice: the top panel shows the rest
            if tag is not None:                        # (typed into the panel: no row of its own)
                self.listener.learn(tag)
            if settings.live_chime:
                _chime()
            if row is not None:
                row.set_outcome({"organize": "Organizing your files", "organize_changed": "Updating the plan",
                                 "undone": "Putting your files back", "auto_setup": "Setting up auto-organize",
                                 "auto_off": "Auto-organize turned off", "auto_asked": "Asking which folder",
                                 "auto_none": "Auto-organize isn't on", "find": "Looking for your file",
                                 "find_opened": "Opening it", "find_shown": "Showing where it is",
                                 "find_which": "Asking which one", "find_all": "Showing all of them",
                                 "find_more": "Looking again", "find_closed": "Closed"}[outcome], ACCENT)
            _track("live_task", outcome=outcome)
        elif outcome in ("organize_busy", "organize_page_busy"):
            if row is not None:
                row.set_outcome("Already organizing. Finish that one first" if outcome == "organize_busy"
                                else "Finish the plan on the Organize page first", AMBER)
            self._note("Already organizing" if outcome == "organize_busy" else "Finish the Organize page first", AMBER)
        elif outcome == "ignored":
            if row is not None:
                row.set_outcome("Ignored — not a request", self._c["text_muted"])
            self._note("Not a request")
            _track("live_ignored")
        elif outcome == "cancelled":                 # Esc while the router was still checking it
            if row is not None:
                row.set_outcome("Cancelled (Esc) — not sent", self._c["text_muted"])
            self._cancel_feedback(tag)
        else:
            if row is not None:
                row.set_outcome("Couldn't check this one — say it again, or check Claude Code / Codex in Agents",
                                AMBER)
            self._note("Couldn't check that", AMBER)
            _track("live_error", kind="route_failed")

    def _on_dropped(self, uid: int, text: str, why: str):
        self._transcribing.discard(uid)
        if uid in self._parked:                        # the pulled-back part had no words after all
            self._parked[uid] = ""
            self._remove_row(uid)
            w = self._wait_join.pop(uid, None)
            if w is not None:
                self._on_heard(*w)
            return
        prev = self._joins.pop(uid, None)
        if prev is not None:                           # what came next was only a cough: the first part goes alone
            first = self._parked.pop(prev, None)
            if first:
                self._send(prev, first)
        self._maybe_finish_send()
        self._captures.pop(uid, None)
        row = self._rows.get(uid)
        if row is None and why not in ("cancelled",) and not text:
            return                          # a cough / a paused half-sentence: not worth a row
        row = self._row(uid)
        if text:
            row.set_text(text)
        label = {"cancelled": "Cancelled (Esc) — not sent",
                 "stopped": "Not sent — you clicked Stop"}.get(why, f"Not sent — {why}")
        row.set_outcome(label, self._c["text_muted"])
        if why == "cancelled":
            self._cancel_feedback(uid)

    def _on_esc_late(self):
        """Esc after it stopped listening to that request: cancel the newest one still being checked."""
        ap = getattr(self._mw, "assistant_page", None)
        checking = [u for u, r in self._rows.items() if r.outcome.text() == "Checking…"]
        if ap is not None and checking and hasattr(ap, "cancel_live"):
            ap.cancel_live(max(checking))

    def _cancel_feedback(self, uid):
        """Make a cancel unmistakable where you're looking: a note under the notch (+ a short sound)."""
        if uid in self._noted:
            return
        self._noted.add(uid)
        self._note("✕  Cancelled", "#FFFFFF")
        if settings.live_chime:
            _chime("Pop")
        _track("live_cancelled")

    def toggle_mute(self):
        """Shift, then Ctrl + Win (or the Mute button). For recording a video or a private call: the mic stays
        closed until they're pressed again."""
        if not self.listener.on:
            self._note("The Assistant isn't on")
            return
        on = not self.listener.user_muted
        self.listener.set_user_muted(on)
        self._note("Muted" if on else "Listening again", "#E0B060" if on else "#B8B8C4")

    def _note(self, text: str, color: str = "#B8B8C4", ms: int = 2500):
        ap = getattr(self._mw, "assistant_page", None)
        try:
            ov = ap._ensure_notch() if ap is not None else None
            if ov is not None and hasattr(ov, "flash_note"):
                ov.flash_note(text, color, ms)
        except Exception:
            pass
        if self._collect is not None and ms < 60000:   # collecting: its note comes back after this one
            QTimer.singleShot(ms + 100, self._show_collect_note)

    def clear_activity(self):
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._empty.setVisible(True)
        self._update_layout()

    def _open_chat(self, key: str):
        ap = getattr(self._mw, "assistant_page", None)
        if ap is None:
            return
        try:
            self._mw._on_nav_clicked(5)
            btn = self._mw.nav_button_group.button(5)
            if btn is not None:
                btn.setChecked(True)
            if key in getattr(ap, "_sessions", {}):
                ap._focus(key)
        except Exception as e:
            logger.info("[live] open chat failed: %s", e)

    # ---- settings -------------------------------------------------------------------------------------
    def _save(self):
        try:
            settings._save_config()
        except Exception as e:
            logger.warning("[live] couldn't save settings: %s", e)
        self.listener.apply_settings()

    def _on_wait(self, v: int, save: bool = True):
        secs = v / 2.0
        self._wait_val.setText(f"{secs:.1f} s")
        if save:
            settings.live_send_after_sec = secs
            self._save()

    def _on_sens(self, key: str):
        settings.live_sensitivity = key
        self._save()

    def _on_noise(self, on: bool):
        settings.live_noise_filter = bool(on)
        self._save()

    def _on_speak(self, on: bool):
        settings.live_speak = bool(on)
        self._save()
        if not on and self._speaker_obj is not None:
            self._speaker_obj.stop()
        _track("live_speak", on=bool(on))

    def _on_voice(self, v: str):
        settings.live_voice = v                       # read by speech_out.fetch_speech for every line
        self._save()
        _track("live_voice", voice=v)

    def _preview_voice(self, v: str):
        """▶ next to a voice: its sample, fetched once and cached (instant after that, no cost)."""
        pb = self.voice_play.get(v)
        if pb is not None:
            pb.setText("…")
            pb.setEnabled(False)

        def work():
            data = None
            try:
                from app.core.speech_out import preview_audio
                data = preview_audio(v)
            except Exception as e:
                logger.info("[live] voice preview failed: %s", e)
            self._preview_sig.emit(v, data)
        threading.Thread(target=work, daemon=True, name="filect-voice-preview").start()
        _track("live_voice_preview", voice=v)

    def _on_preview_ready(self, v: str, data):
        pb = self.voice_play.get(v)
        if pb is not None:
            pb.setText("▶")
            pb.setEnabled(True)
        if not data:
            self._voice_lbl.setText("That sample couldn't load — check your internet / sign-in")
            self._voice_lbl.setVisible(True)
            QTimer.singleShot(4000, lambda: self._voice_lbl.setVisible(False))
            return
        from app.core.speech_out import PREVIEW_LINE, VOICES
        sp = self._speaker()
        sp.stop()                                     # (one at a time — and over whatever it was saying)
        sp.hold(False)
        sp.say(PREVIEW_LINE.format(name=next((x[1] for x in VOICES if x[0] == v), v)), data=data)

    def _on_auto_open(self, on: bool):
        settings.assistant_auto_open = bool(on)       # read by the Agents page at each task / answer
        self._save()
        _track("assistant_auto_open", on=bool(on))

    def _on_organize_preview(self, on: bool):
        settings.live_organize_preview = bool(on)     # read each time you ask it to organize a folder
        self._save()
        _track("live_organize_preview", on=bool(on))

    def say_line(self, line: str):
        """Organizing by voice: the panel's question, the result. Only with Speak replies, and only while on."""
        if not line or not settings.live_speak or not self.listener.on:
            return
        sp = self._speaker()
        sp.hold(self._state in ("hearing", "countdown", "paused"))
        sp.say(line)

    # ---- Speak replies ----------------------------------------------------------------------------------
    def _speaker(self):
        if self._speaker_obj is None:
            sp = self._speaker_given
            if sp is None:
                from app.core.speech_out import Speaker
                sp = Speaker(self)
            sp.started.connect(self._on_speak_started)
            sp.finished.connect(self._on_speak_finished)
            self._speaker_obj = sp
        return self._speaker_obj

    def _on_live_event(self, key, kind: str, payload):
        """A hands-free task finished / needs your OK / failed: say it, if Speak replies is on."""
        st = {"done": "done", "approval": "need", "error": "error"}.get(kind)
        for r in self._rows.values():                 # "What it heard": working → done / needs your OK / failed
            if st and r.key == key and r.state in ("run", "need"):
                r.set_state(st)
        if not settings.live_speak or not self.listener.on:
            return
        from app.core.speech_out import spoken_line
        line = spoken_line(kind, payload)
        if not line:
            return
        logger.info("[live] speak (%s, %s): %r", kind, key, line)
        _track("live_spoke", kind=kind)             # (which moment only — never the words)
        sp = self._speaker()
        sp.hold(self._state in ("hearing", "countdown", "paused"))   # not over you / your shortcut
        sp.say(line)
        if kind == "done":
            for r in self._rows.values():
                if r.key == key and r.state == "done" and not r.spoke:
                    r.set_spoke()

    def _on_speak_started(self):
        if not getattr(self.listener, "echo_cancelled", False) and hasattr(self.listener, "set_muted"):
            self.listener.set_muted(True)           # no echo cancellation: the mic would hear Filect

    def _on_speak_finished(self):
        self._spoke_until = time.monotonic()
        QTimer.singleShot(300, self._unmute_if_quiet)   # (the speakers' tail)

    def _unmute_if_quiet(self):
        sp = self._speaker_obj
        if (sp is None or not sp.speaking) and hasattr(self.listener, "set_muted"):
            self.listener.set_muted(False)

    def _on_chime(self, on: bool):
        settings.live_chime = bool(on)
        self._save()

    # ---- your screen, as you started talking ------------------------------------------------------------
    def _on_utterance_started(self, uid: int):
        """You started talking. While Filect talks (echo cancellation on — measured: the mic doesn't hear
        Filect's own voice) it stops at once. A plain "stop" said over it / just after isn't a task."""
        sp = self._speaker_obj
        if sp is None:
            return
        if sp.speaking or time.monotonic() - self._spoke_until < 2.5:
            self._talked_over.add(uid)
            while len(self._talked_over) > 20:
                self._talked_over.discard(min(self._talked_over))
        if sp.speaking and getattr(self.listener, "echo_cancelled", False):
            logger.info("[live] you started talking — stopped speaking")
            sp.stop()

    def _on_speech_confirmed(self, uid: int):
        """It's clearly speech: snap the display under the mouse NOW (off the UI thread, in memory only) —
        if the request turns out to be about your screen, this is what you were looking at."""
        if settings.live_speak:                        # the voice service wakes while you talk (~0.15 s sooner)
            try:
                from app.core.speech_out import wake
                wake()
            except Exception:
                pass
        if self._drawings or self._drawing:
            return                                     # your drawing is the screenshot for this one
        try:
            from PySide6.QtGui import QCursor, QGuiApplication
            scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            rect = scr.geometry()
        except Exception:
            return
        capture = self._capture_fn
        if capture is None:
            # Windows: where on the screen is placed here (QScreen belongs to the UI thread); the copy itself (GDI,
            # ~0.2 s for a 4K screen) runs in the thread — Qt's grab would stall the UI while you talk.
            from app.ui.screen_ink import grab_px, screen_px_rect
            px = screen_px_rect(rect)
            if px is None:
                return
            capture = lambda _r, _px=px: grab_px(_px)    # noqa: E731
        entry = {"img": None}

        def grab():
            try:
                entry["img"] = capture(rect)
            except Exception as e:
                logger.info("[live] screen capture failed: %s", e)

        t = threading.Thread(target=grab, daemon=True, name="filect-live-screen")
        entry["thread"] = t
        self._captures[uid] = entry
        t.start()
        while len(self._captures) > MAX_CAPTURES:      # bounded memory
            self._captures.pop(min(self._captures))

    def _screen_for(self, uid: int):
        """A callable for the Agents page: called ONLY if the request is about your screen — writes the
        capture to disk then (nothing is written otherwise) and returns its path."""
        def get():
            e = self._captures.get(uid)
            if not e:
                return None
            t = e.get("thread")
            if t is not None:
                t.join(2.0)
            img = e.get("img")
            if img is None:
                return None
            from app.ui.screen_ink import SCREENSHOT_DIR, _save
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = str(SCREENSHOT_DIR / f"screen-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png")
            threading.Thread(target=_save, args=(img, path), daemon=True, name="filect-live-screen-save").start()
            e["path"] = path
            logger.info("[live] request %s is about your screen → attached %s", uid, path)
            return path
        return get

    # ---- drawing (press ⇧⌃, while Filect Assistant is on) ----------------------------------------------
    def _hands_free(self, on: bool):
        """Tell the Agents page we're listening (it keeps Claude Code running ahead of time) / stopped."""
        ap = getattr(self._mw, "assistant_page", None)
        if ap is not None and hasattr(ap, "set_hands_free") and getattr(ap, "_hands_free", None) != on:
            try:
                ap.set_hands_free(on)
            except Exception as e:
                logger.info("[live] hands-free speed-ups unavailable: %s", e)

    def _on_quick_sent(self, uid: int, left: float):
        """A sentence went early: the Agents page doesn't START it before the normal "send after" would have
        passed — so if you go on talking, it can still be joined with the rest."""
        ap = getattr(self._mw, "assistant_page", None)
        if ap is not None and hasattr(ap, "hold_live"):
            ap.hold_live(uid, left)

    def _on_resumed(self, prev: int, new: int):
        """You went on talking right after a sentence went early ("March, April… and May"): pull it back and join it
        with what you're saying now — exactly as if it had waited out "send after"."""
        if self._collect is not None or self._drawing or self._waiting is not None:
            return                                     # (those put the parts together already)
        if prev in self._transcribing:
            parked = True                              # not even transcribed yet: it simply waits
        elif prev in self._sent_text:
            ap = getattr(self._mw, "assistant_page", None)
            parked = bool(ap is not None and hasattr(ap, "cancel_live") and ap.cancel_live(prev, quiet=True))
        else:
            parked = False
        if not parked:                                 # it had already started: what you say now is a follow-up
            return
        self._parked[prev] = self._sent_text.pop(prev, None)
        self._joins[new] = prev
        row = self._rows.get(prev)
        if row is not None:
            row.set_outcome("Joined with what you said next", self._c["text_muted"])
        logger.info("[live] request %s pulled back — you went on talking (joins %s)", prev, new)

    def _on_early_text(self, uid: int, text: str):
        """What you said is already transcribed as it ends: let the Agents page route it NOW (used when the
        final words match). Not for what's being collected or drawn, or said over Filect talking."""
        if self._collect is not None or self._drawing or uid in self._talked_over:
            return
        ap = getattr(self._mw, "assistant_page", None)
        if ap is not None and hasattr(ap, "prepare_live_route"):
            try:
                ap.prepare_live_route(uid, text)
            except Exception as e:
                logger.info("[live] early routing unavailable: %s", e)

    def _on_listener_state(self, state: str = "", *_):
        if not self.listener.on:
            self._hands_free(False)
        if not self.listener.on and self._collect is not None:
            self._drop_collection("Not sent — you clicked Stop")
        if not self.listener.on:
            self._transcribing.clear()
        if not self.listener.on and (self._drawing or self._drawings or self._waiting):
            self._end_drawing(drop=True)
        sp = self._speaker_obj
        if sp is not None:
            if not self.listener.on or state in ("off", "error", "paused"):
                sp.stop()                           # you pressed fn / Stop: quiet at once
            sp.hold(state in ("hearing", "countdown", "paused"))   # …and what's next waits for you

    def _on_draw_key(self):
        if not self.listener.on:
            return
        if self._drawing:
            self._capture_drawing()
        elif len(self._drawings) >= MAX_DRAWINGS:
            self._note(f"{MAX_DRAWINGS} drawings is the most — say what to do", AMBER)
        else:
            self._start_drawing()

    def _on_draw_spoiled(self):
        """The ⇧⌃ that just started a drawing was part of a shortcut (⌃⇧Tab…): quietly take it back —
        unless you've already drawn something."""
        if self._drawing and not getattr(self._ink, "_strokes", None):
            self._end_drawing()
            logger.info("[live] drawing taken back (a ⌃⇧ shortcut)")
            if self._drawings:
                self._keep_drawings()

    def _ensure_ink(self):
        if self._ink is None:
            from app.ui.screen_ink import ScreenInk
            self._ink = ScreenInk(title="Filect Assistant Ink")   # its own window (not fn + ⌃'s)
        return self._ink

    def _start_drawing(self):
        ink = self._ensure_ink()
        ink.begin()
        ink.set_drawing(True, f"press {DRAW_KEY} to capture · Esc cancels")
        self._drawing = True
        self._drawn_timer.stop()                       # earlier drawings wait while you draw this one
        n = len(self._drawings) + 1
        self._note(f"✏️  Drawing{f' {n}' if n > 1 else ''} — press {DRAW_KEY} to capture", "#FFFFFF")
        logger.info("[live] drawing %d on", n)
        _track("live_draw")

    def _capture_drawing(self):
        ink = self._ink
        shot = ink.snapshot() if ink is not None else None
        if ink is not None:
            ink.end()
        self._drawing = False
        if shot is None:
            self._note("Couldn't capture the screen", AMBER)
        else:
            self._drawings.append(shot[0])
            logger.info("[live] drawing %d captured (marks=%s): %s", len(self._drawings), shot[1], shot[0])
        if self._collect is not None:                  # collecting: it waits for ⌃ with the rest
            if shot is not None:
                self._collect.setdefault("bits", []).append(("shot", shot[0]))
            self._show_collect_note()
            self._on_state(self._state, getattr(self, "_detail", ""))
            return
        w, self._waiting = self._waiting, None
        if w is not None:                              # you already said what to do: send it now, with them
            uid = w["uids"][0]
            if len(w["uids"]) > 1:
                self._also[uid] = w["uids"][1:]
            self._send(uid, w["text"])
            return
        if self._drawings:
            self._keep_drawings()
            n = len(self._drawings)
            self._note("📸  Drawing ready — say what to do" if n == 1 else f"📸  {n} drawings ready — say what to do",
                       "#FFFFFF")

    def _drop_drawings(self, why: str):
        if self._drawings:
            n = len(self._drawings)
            logger.info("[live] %d drawing(s) dropped (%s)", n, why)
            self._drawings = []
            self._note("Drawing dropped" if n == 1 else f"{n} drawings dropped")
        self._drawn_timer.stop()

    def _end_drawing(self, drop: bool = False):
        if self._drawing and self._ink is not None:
            self._ink.end()
        self._drawing = False
        if drop:
            self._drawings = []
            self._drawn_timer.stop()
            w, self._waiting = self._waiting, None
            for u in (w or {}).get("uids", []):
                row = self._rows.get(u)
                if row is not None:
                    row.set_outcome("Not sent — you clicked Stop", self._c["text_muted"])

    def _esc_filter(self):
        """Esc, in order: while drawing → cancel the drawing (+ what you said during it); while Filect is
        talking → just stop talking; collecting (⌃⌃) → throw the collection away; with drawings waiting →
        drop them (and Esc still cancels a request in flight, as usual)."""
        used = self._esc_drawing()
        if used:
            return used
        sp = self._speaker_obj
        if sp is not None and sp.speaking:          # Esc while it's talking: just be quiet
            sp.stop()
            logger.info("[live] stopped speaking (Esc)")
            if self._collect is not None:           # (what you collected stays: say so, or Esc looks like it did nothing)
                self._note("Stopped talking. Press Esc again to throw away what you collected.", ms=4000)
            return True
        if self._collect is not None:               # collecting: throw it ALL away (+ what you're saying)
            self._drop_collection("Thrown away (Esc) — not sent")
            return "speech"
        if self._drawings:
            self._drop_drawings("Esc")
        return False

    def _esc_drawing(self):
        """Esc while drawing cancels that drawing AND what you said during it ("speech": the listener also
        drops what you're saying right now) — drawings you'd already captured keep waiting."""
        if self._drawing:
            self._end_drawing()
            w, self._waiting = self._waiting, None
            for u in (w or {}).get("uids", []):
                row = self._rows.get(u)
                if row is not None:
                    row.set_outcome("Cancelled (Esc) — not sent", self._c["text_muted"])
            self._note("Drawing cancelled")
            logger.info("[live] drawing cancelled (Esc)%s", " with what you said" if w else "")
            if self._drawings:
                self._keep_drawings()
            return "speech"
        return False

    # ---- collect, then send (⌃⌃ … ⌃) -------------------------------------------------------------------
    def _on_ctrl_tap(self):
        """A lone ⌃ tap (the listener filters out ⌃C, ⌃-click, ⇧⌃, fn + ⌃…). Collecting → send it all;
        else two within COLLECT_TAP_SEC → start collecting (one on its own does nothing)."""
        if not self.listener.on:
            return
        now = time.monotonic()
        if self._collect is not None:
            self._ctrl_at = 0.0                        # (this tap is used: the next one starts fresh)
            self._send_collection()
        elif now - self._ctrl_at <= COLLECT_TAP_SEC:
            self._ctrl_at = 0.0
            self._start_collecting()
        else:
            self._ctrl_at = now

    def _start_collecting(self):
        self._collect = {"uids": [], "text": "", "sending": False,
                         "bits": [("shot", p) for p in self._drawings]}     # (the tray: in order)
        self._drawn_timer.stop()                       # drawings wait for ⌃ now, not 60 s
        logger.info("[live] collecting (⌃⌃) — tap ⌃ to send")
        if settings.live_chime:
            _chime("Morse")
        self._show_collect_note()
        self._on_state(self._state, getattr(self, "_detail", ""))
        _track("live_collect")

    def _collect_summary(self) -> str:
        c = self._collect or {}
        n, d = len(c.get("uids") or ()), len(self._drawings) + (1 if self._drawing else 0)
        parts = [f"{n} part{'s' if n != 1 else ''}"] if n else ["nothing yet"]
        if d:
            parts.append(f"{d} drawing{'s' if d != 1 else ''}")
        return " · ".join(parts)

    def _show_collect_note(self):
        if self._collect is not None:
            self._note(f"📥  Collecting · {self._collect_summary()} — tap Ctrl to send", "#FFFFFF", ms=600000)

    def _add_part(self, uid: int, text: str):
        c = self._collect
        c["uids"].append(uid)
        c["text"] = f"{c['text']} {text}".strip()
        c.setdefault("bits", []).append(("said", text))
        first = c["uids"][0]
        if uid != first:                               # one row for the whole collection
            self._row(first).set_text(c["text"])
            self._remove_row(uid)
        self._row(first).set_outcome(f"Collecting · {self._collect_summary()} — tap Ctrl to send", ACCENT)
        logger.info("[live] collected part %d (%s)", len(c["uids"]), uid)
        self._show_collect_note()
        self._on_state(self._state, getattr(self, "_detail", ""))
        self._maybe_finish_send()

    def _maybe_finish_send(self):
        c = self._collect
        if c is not None and c["sending"] and not self._transcribing:
            self._send_collection(force=True)

    def _send_collection(self, force: bool = False):
        """⌃ while collecting: what you're saying right now is finished at once (no "send after" wait),
        and once everything is transcribed it ALL goes as one request — drawings too."""
        c = self._collect
        if c is None:
            return
        if self._drawing:                              # a drawing still open goes with it
            self._capture_drawing()
        if not c["sending"]:
            c["sending"] = True
            if hasattr(self.listener, "finish_now"):
                self.listener.finish_now()
            if self._collect is not c:                 # (finishing it — too short → dropped — sent it already)
                return
            if self._transcribing and not force:       # wait for it (heard / dropped → _maybe_finish_send)
                self._note("Sending…", "#FFFFFF", ms=600000)
                self._collect_timer.start(COLLECT_WAIT_MS)
                return
        elif self._transcribing and not force:
            return
        self._collect, c["sending"] = None, False
        self._collect_timer.stop()
        uids = c["uids"]
        if not uids:
            self._note("Nothing collected — say something first" if not self._drawings
                       else "Say what to do with your drawing", AMBER)
            if self._drawings:
                self._keep_drawings()
            logger.info("[live] collecting ended — nothing to send")
            self._on_state(self._state, getattr(self, "_detail", ""))
            return
        uid = uids[0]
        if len(uids) > 1:
            self._also[uid] = uids[1:]
        self._row(uid).set_text(c["text"])
        # ponytail: the screen of the LATEST part that captured one — per-part screens if that's ever wrong
        screen_uid = max((u for u in uids if u in self._captures), default=uid)
        n_draw = len(self._drawings)
        logger.info("[live] sending the collection: %d part(s), %d drawing(s)", len(uids), n_draw)
        self._note("")                                 # (clears the collecting note)
        self._send(uid, c["text"], screen_uid=screen_uid)
        self._on_state(self._state, getattr(self, "_detail", ""))
        _track("live_collect_sent", parts=len(uids), drawings=n_draw)

    def _drop_collection(self, label: str):
        c, self._collect = self._collect, None
        self._collect_timer.stop()
        if c is None:
            return
        for u in c["uids"][:1]:
            row = self._rows.get(u)
            if row is not None:
                row.set_outcome(label, self._c["text_muted"])
        if self._drawing:
            self._end_drawing()
        n_draw = len(self._drawings)
        self._drawings = []
        self._drawn_timer.stop()
        logger.info("[live] collection thrown away (%d part(s), %d drawing(s)): %s", len(c["uids"]), n_draw, label)
        self._note("✕  Collection thrown away", "#FFFFFF")
        if settings.live_chime:
            _chime("Pop")
        self._on_state(self._state, getattr(self, "_detail", ""))

    def _keep_drawings(self):
        """Drawings wait DRAWN_KEEP_MS for what you say — but not while collecting (they wait for ⌃)."""
        if self._collect is None:
            self._drawn_timer.start(DRAWN_KEEP_MS)

    # ---- notch light + menu bar ---------------------------------------------------------------------------
    def _notch_light(self, mode: str):
        ap = getattr(self._mw, "assistant_page", None)
        if ap is None:
            return
        try:
            ov = ap._ensure_notch() if mode else getattr(ap, "_overlay", None)
            if ov is not None:
                ov.set_listening(mode)
        except Exception:
            pass

    def _add_tray_item(self):
        """The Mac adds "Start Filect Assistant" to its menu-bar menu. Windows: Filect has no tray icon (X quits)."""
        return
