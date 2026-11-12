"""Ambient "Assistant" notch overlay.

A calm, VoiceOS-style surface for the Claude agent. It RESTS as a small, state-coloured handle
tucked under the notch and only reveals its content when you HOVER it — it never drops down on
its own. The handle itself carries the signal:

    idle      dim grey        (no task)
    working   purple, breathing
    needs_you AMBER, breathing + a dot   ← an Approve/Deny is pending (it blocks the task)
    done      green           (fades back after a few seconds)
    error     red

Hovering expands the panel to show the current step, or — the point of the whole thing — a
compact Approve/Deny card so you can clear a blocked tool call without leaving the app you're in.
The full in-tab Activity feed and approval card stay as the detailed view; this is the ambient,
glance-and-approve layer. Mouse-only + non-activating, so it never steals focus.

Driven by the AssistantPage's existing worker signals; see wiring in assistant_page.py.

macOS panel mechanics are shared with the organize overlay via notch_panel.py.

Headless self-check:
    cd /Users/damianosmalliaros/Developer/Mac_app && \
      QT_QPA_PLATFORM=offscreen venv311/bin/python \
      ai_file_organizer/app/ui/claude_overlay.py
"""
import html
import logging
import math
import time
import sys

logger = logging.getLogger(__name__)

from PySide6.QtCore import (
    Qt, QTimer, QPointF, QRectF, QSize, QPropertyAnimation, QEasingCurve, Signal, QUrl,
)
from PySide6.QtGui import (QColor, QPainter, QPen, QGuiApplication, QCursor, QDesktopServices, QIcon,
                           QFont, QFontMetrics)
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QSizePolicy, QGraphicsDropShadowEffect, QTextBrowser, QSlider, QLineEdit, QCheckBox,
)
IS_WIN = sys.platform.startswith("win")
try:
    from . import details_view
    if IS_WIN:
        from . import win_panel as notch_panel      # Windows twin of the Mac panel mechanics (same names)
    else:
        from . import notch_panel
except ImportError:                      # running this file directly (self-check)
    import details_view
    import win_panel as notch_panel


def _voice_key_label() -> str:
    """The agent chord as the user presses it ("Ctrl+Alt" on Windows; fn ⌃ on the Mac)."""
    if not IS_WIN:
        return "fn ⌃"
    try:
        from app.core.settings import settings
        from app.ui.agent_hotkey import label, DEFAULT_SHORTCUT
        return label(getattr(settings, "assistant_voice_shortcut", "") or DEFAULT_SHORTCUT)
    except Exception:
        return "Ctrl+Alt"

# --- palette (VoiceOS-style: soft near-black card, muted greys, Claude-orange accent) ----
C = {
    "text":       "#F2F2F4",
    "text_2":     "#C2C2CA",
    "text_muted": "#8A8A92",
    "surface":    "#1C1C1F",   # pill / control background (VoiceOS-dark)
    "border":     "#2A2A2E",
}
CARD_BG = "#000000"            # pure black, like the hover bar, so the card MERGES with the notch
ACCENT = "#A78BFA"            # lavender — the hover bar's orb; focus, send, effort, the "working" accent
ACCENT_LIGHT = "#C4B5FD"
AMBER = "#E0A23C"           # "your turn" — an approval is waiting
GREEN = "#3FB860"
RED = "#F2645A"

CARD_W = 450               # fits the widest model pill ("GPT-5.6-Terra · Extra high") beside "Open in Terminal →"
SHADOW_MARGIN = 22
WIDGET_W = CARD_W + SHADOW_MARGIN * 2
TOP_MARGIN = 0
STRIP_W = 150               # width of the resting handle
TOPBAR_H = 24
FOOTER_H = 22               # keycap shortcut hint row
TYPEFIELD_H = 40            # the "type a task" input row at the bottom
PEEK_H = 34                 # strip below the notch band: invisible at rest, shows the hover tab (click target)
WIN_TITLE = "Filect Assistant"   # unique — notch_panel finds the NSWindow by this

# Deterministic content heights per state (no runtime layout measuring — that raced in organize).
_STACK_H = {"working": 22, "needs_you": 116, "done": 118, "error": 56, "idle": 34}
DETAILS_H = 166      # height of the live "Details" feed when expanded
DONE_MAX_H = 280     # the finished answer shows in full up to this height, then scrolls
DONE_TEXT_W = CARD_W - 32 - 26   # its width: card minus the column margins and the ✓ column
TRANSCRIPT_H = 56    # the "what you said" bubble at the top of the card
TITLE_H = 20         # the task's short bold name under it (VoiceOS: "Build coffee brand website")
CTRL_H = 32          # persistent control row (model pill · Details · primary action)
MODELPANEL_H = 170   # the inline "Model & effort" panel (model dropdown closed)
OPENPANEL_H = 46     # the ⌄ menu: one row — the other place to open it
MODEL_ROW_H = 31     # one row of the open model dropdown (28px + spacing; the selected one is hidden)
_MODELS = ("opus", "sonnet", "haiku", "fable")
# Display names carry the version (the alias still resolves to Claude Code's latest of each family).
_MODEL_NAMES = {"opus": "Opus 5.5", "sonnet": "Sonnet 5.5", "haiku": "Haiku 5.5", "fable": "Fable 5.1"}
_AGENT_NAMES = {"claude": "Claude", "codex": "Codex"}            # the card's "Open in …" / answered-by
_SETUP_NAMES = {"claude": "Claude Code", "codex": "Codex"}
_EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]       # slider index 0..4 ("" = Auto, off-slider)
_EFFORT_NAMES = {"": "Auto", "low": "Low", "medium": "Medium",
                 "high": "High", "xhigh": "Extra high", "max": "Max"}

# Dark scrollbar: unstyled, its track fell back to the app's LIGHT palette (a white strip).
_SCROLLBAR_CSS = (
    "QScrollBar:vertical { background: transparent; width: 6px; margin: 2px; }"
    "QScrollBar::handle:vertical { background: rgba(255,255,255,0.18); border-radius: 3px; min-height: 24px; }"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
    "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }")

FLASH_MS = 3500      # how long the pop-up stays open; each new step re-arms it (follows activity)


def _load_logo(px: int, agent: str = "claude"):
    """The agent's official logo IF it's in app/ui/assets/ (claude-logo.png: the Claude spark; codex-logo.png:
    the Codex mark from the Codex app). We don't redraw a brand's mark in code — just load the real file when
    it's there; otherwise the header shows a neutral placeholder glyph."""
    import os
    from PySide6.QtGui import QPixmap
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    names = ("codex-logo.png",) if agent == "codex" else ("claude-logo.png", "claude-logo.svg", "claude-logo.jpg",
                                                         "claude.png")
    for name in names:
        p = os.path.join(base, name)
        if os.path.isfile(p):
            pm = QPixmap(p)
            if not pm.isNull():
                return pm.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return None


def _glyph(kind: str, px: int = 16, color=None):
    """Small line glyphs painted in code (the bundle has no icon assets): 'search', 'plus', 'close'."""
    from PySide6.QtGui import QPixmap
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(color or QColor(150, 150, 160), 1.5)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    s = float(px)
    if kind == "search":
        p.drawEllipse(QRectF(s * .18, s * .18, s * .5, s * .5))
        p.drawLine(QPointF(s * .6, s * .6), QPointF(s * .84, s * .84))
    elif kind == "plus":
        p.drawLine(QPointF(s * .5, s * .22), QPointF(s * .5, s * .78))
        p.drawLine(QPointF(s * .22, s * .5), QPointF(s * .78, s * .5))
    elif kind == "close":
        p.drawLine(QPointF(s * .28, s * .28), QPointF(s * .72, s * .72))
        p.drawLine(QPointF(s * .72, s * .28), QPointF(s * .28, s * .72))
    p.end()
    return pm


class _Handle(QWidget):
    """The notch hotspot. INVISIBLE at rest when idle. While a task is live or finished it shows a gray
    lip hanging flush from the notch (amber dot = an approval waits); hover grows it. Click opens."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "idle"
        self._listening = ""          # Filect Assistant: "" (off) | "listening" | "hearing" | "paused"
        self._note, self._note_color = "", None   # a short note under the notch ("Cancelled"), for ~2.5 s
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(lambda: self.flash_note(""))
        self._hovered = False
        self._phase = 0.0
        self._rest_pill = IS_WIN                  # Windows: the black pill at rest (see WIN_PILL_W)
        self._pill_hidden = False                 # …not over a fullscreen app
        # Always tall enough to draw the hover tab below the notch — the window never resizes on hover
        # (resizing a non-activating panel mid-hover drops its style and the tab never renders). The
        # strip is invisible at rest; the tab is painted only while hovered.
        self.setFixedHeight(PEEK_H)
        self.setCursor(Qt.PointingHandCursor)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._reveal = 0.0            # hover "relax": the pill grows/brightens in
        self._reveal_target = 0.0
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setInterval(16)
        self._reveal_timer.timeout.connect(self._reveal_step)

    def set_state(self, state: str) -> None:
        self._state = state
        self._run_timer()
        self.update()

    def set_listening(self, mode: str) -> None:
        """Filect Assistant's light under the notch: a slow violet pulse while it listens, bright while
        it hears you, dim gray while paused; nothing when it's off."""
        mode = mode if mode in ("listening", "hearing", "paused") else ""
        if mode != self._listening:
            self._listening = mode
            self._run_timer()
            self.update()

    def flash_note(self, text: str, color: str = "#B8B8C4", ms: int = 2500) -> None:
        """Filect Assistant feedback right under the notch — where you're looking, not the tab: e.g.
        "Cancelled" after Esc, "Not a request" for ignored speech. "" clears it."""
        self._note, self._note_color = text, QColor(color)
        if text:
            self._note_timer.start(ms)
        else:
            self._note_timer.stop()
        self.update()

    def _paint_note(self, p, y: float) -> bool:
        if not self._note or self._reveal > 0.3:
            return False
        f = QFont(self.font())
        f.setPixelSize(11)
        f.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetrics(f)
        text = fm.elidedText(self._note, Qt.ElideRight, max(20, self.width() - 22))
        w, h = fm.horizontalAdvance(text) + 18, 19.0
        x = (self.width() - w) / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(24, 24, 30, 235))
        p.drawRoundedRect(QRectF(x, y, w, h), h / 2.0, h / 2.0)
        p.setFont(f)
        p.setPen(self._note_color or QColor("#B8B8C4"))
        p.drawText(QRectF(x, y, w, h), Qt.AlignCenter, text)
        return True

    def _run_timer(self) -> None:
        # Breathe only while a task is live (working) or blocked on you (needs_you), or the hands-free
        # light pulses. At idle the notch is fully hidden (nothing drawn, no timer) until you hover it.
        if self._state in ("working", "needs_you") or self._listening in ("listening", "hearing"):
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def set_pill_hidden(self, hidden: bool) -> None:
        if hidden != self._pill_hidden:
            self._pill_hidden = hidden
            self.update()

    def _paint_rest_pill(self, p) -> None:
        """The resting pill: black, flush with the top edge (flat top, rounded bottom), a hairline edge so it shows
        on dark windows too."""
        w = min(float(WIN_PILL_W), self.width() - 8.0)
        x = (self.width() - w) / 2.0
        h = float(WIN_PILL_H)
        p.setPen(QPen(QColor(255, 255, 255, 46), 1.0))
        p.setBrush(QColor(10, 10, 12))
        p.drawRoundedRect(QRectF(x, -h, w, 2 * h - 0.5), h / 2.0 + 1, h / 2.0 + 1)   # top half off-widget → flat

    def set_hovered(self, hovered: bool) -> None:
        if hovered != self._hovered:
            self._hovered = hovered
            self.update()

    def reveal(self, on: bool) -> None:
        """Grow the hover tab in (on) / fade it out (off). Driven by the overlay's cursor poll — not by
        this widget's own enter/leave: the handle sits BELOW the notch band, so hovering the notch
        itself never entered it, and the tab never showed."""
        self.set_hovered(on)
        self._reveal_target = 1.0 if on else 0.0
        if not self._reveal_timer.isActive():
            self._reveal_timer.start()

    def _reveal_step(self) -> None:
        d = self._reveal_target - self._reveal
        if abs(d) < 0.02:
            self._reveal = self._reveal_target
            self._reveal_timer.stop()
        else:
            self._reveal += d * 0.28
        self.update()

    def _tick(self) -> None:
        self._phase += 0.14
        self.update()

    def _shows_at_rest(self) -> bool:
        # Visible unless truly IDLE: a running task (working, breathing purple), a blocked approval
        # (amber), a finished task (green), or an error (red). Only idle is invisible.
        return self._state in ("working", "needs_you", "done", "error")

    def _paint_listening(self, p, y: float) -> None:
        if not self._listening or self._reveal > 0.3:
            return
        cx = self.width() / 2.0
        if self._listening == "paused":
            color, r = QColor(150, 150, 160, 150), 3.0
        elif self._listening == "hearing":
            color, r = QColor(ACCENT), 4.5
            glow = QColor(ACCENT)
            glow.setAlpha(70)
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QPointF(cx, y), 8.0, 8.0)
        else:
            color, r = QColor(ACCENT), 3.5
            color.setAlpha(110 + int(120 * (0.5 + 0.5 * math.sin(self._phase * 0.6))))
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(cx, y), r, r)

    def paintEvent(self, _e) -> None:
        base = self._shows_at_rest()
        # At rest the notch is COMPLETELY hidden — nothing is drawn until you hover it (reveal > 0),
        # or a task is live/blocked/finished (base). No idle hint, no resting pill. (Filect Assistant
        # adds only its small light while it's on.)
        if not base and self._reveal < 0.02:
            pill = self._rest_pill and not self._pill_hidden
            if self._listening or self._note or pill:
                p = QPainter(self)
                p.setRenderHint(QPainter.Antialiasing, True)
                if pill:
                    self._paint_rest_pill(p)
                y0 = WIN_PILL_H + 4.0 if pill else 4.0
                if not self._paint_note(p, y0):
                    self._paint_listening(p, y0 + 3.0)
                p.end()
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if not base:
            # IDLE hover → a clearly VISIBLE pull-tab that grows in, so you can see there's something
            # to click. Nothing at rest; on hover a solid rounded bar + a downward chevron appear just
            # below the notch, in the strip that actually receives the click.
            r = self._reveal
            tab_w = 56.0 + 86.0 * r
            tab_h = 6.0 + 10.0 * r
            x = (self.width() - tab_w) / 2.0
            y = 4.0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(236, 236, 240, int(70 + 185 * r)))
            p.drawRoundedRect(QRectF(x, y, tab_w, tab_h), tab_h / 2.0, tab_h / 2.0)
            if r > 0.45:                             # chevron: "click to open"
                cx = self.width() / 2.0
                cy = y + tab_h + 7.0
                pen = QPen(QColor(236, 236, 240, int(210 * r)))
                pen.setWidthF(2.0); pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(cx - 5, cy - 3), QPointF(cx, cy + 2))
                p.drawLine(QPointF(cx, cy + 2), QPointF(cx + 5, cy - 3))
            p.end()
            return
        # Live states (working / needs_you / done / error): a gray lip hanging FLUSH from the notch (like
        # the left list's edge tab, attached to the edge), that grows on hover. Solid gray, not
        # translucent black — black vanished over dark windows. Working breathes; an approval adds amber.
        r = self._reveal
        lip_w = self.width() * (0.5 + 0.12 * r)
        t = 4.0 + 4.0 * r
        a = 150 + int(70 * r)
        if self._state == "working" and r < 0.02:
            a = 90 + int(90 * (0.5 + 0.5 * math.sin(self._phase)))
        x = (self.width() - lip_w) / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(160, 160, 168, a))
        p.drawRoundedRect(QRectF(x, -6.0, lip_w, t + 6.0), t / 2.0 + 1, t / 2.0 + 1)   # top is off-widget → flat, attached
        if self._state == "needs_you":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(AMBER))
            p.drawEllipse(QPointF(x + lip_w + 9, t / 2.0 + 1), 3.5, 3.5)
        if not self._paint_note(p, t + 4.0):
            self._paint_listening(p, t + 9.0)
        p.end()

    def mousePressEvent(self, e) -> None:
        self.clicked.emit()
        e.accept()


class _Spinner(QWidget):
    """Small rotating arc for the working page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0.0
        self.setFixedSize(26, 26)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle + 6.0) % 360.0
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
        pen = QPen(QColor(ACCENT), 3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, int(-self._angle * 16), 270 * 16)
        p.end()


_CHIP = "background-color:#2A2A2E; color:#A3A3AB; font-size:11px;"
_SPEAK_HINT = ("".join(f"<span style='{_CHIP}'>&nbsp;{k}&nbsp;</span> " for k in _voice_key_label().split("+"))
               .join(("Type or hold ", "to speak")))


class _TypeField(QLineEdit):
    """Text input on the notch. On click it asks the panel to take keyboard focus — allowed because the
    panel is a floating, non-activating panel with becomesKeyOnlyIfNeeded: a USER click can key it (the
    old 'dead key window' problem was only for key-grabs with no user click)."""
    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self._hint = None

    def set_hint(self, html: str) -> None:
        """A rich placeholder (keycap chips — a QLineEdit placeholder is plain text only), shown while
        the field is empty. Clicks go through it to the field."""
        self._hint = QLabel(self)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._hint.setTextFormat(Qt.RichText)
        self._hint.setText(html)
        self._hint.setStyleSheet("background:transparent;color:#8E8E96;font-size:13px;")
        self.textChanged.connect(lambda s: self._hint.setVisible(not s))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._hint is not None:
            self._hint.setGeometry(15, 0, max(0, self.width() - 20), self.height())

    def mousePressEvent(self, e):
        try:
            self._on_click()
        except Exception:
            pass
        super().mousePressEvent(e)


class _SendButton(QPushButton):
    """Send button with a PAINTED up-arrow. A text glyph ('↑') doesn't render in the brand font — it
    came out as a blank orange square — so we draw the arrow ourselves: always crisp, centered, and
    visible in both the enabled (white on accent) and disabled (grey) states."""
    def paintEvent(self, e):
        super().paintEvent(e)                 # styled background (accent / disabled / hover)
        from PySide6.QtGui import QPainter, QPen, QColor
        from PySide6.QtCore import QPointF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor("#FFFFFF") if self.isEnabled() else QColor("#8A8A92"))
        pen.setWidthF(2.3)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        w, h = self.width(), self.height()
        cx, top, bot, head = w / 2.0, h * 0.30, h * 0.72, w * 0.18
        p.drawLine(QPointF(cx, bot), QPointF(cx, top))                 # shaft
        p.drawLine(QPointF(cx, top), QPointF(cx - head, top + head))   # left wing
        p.drawLine(QPointF(cx, top), QPointF(cx + head, top + head))   # right wing
        p.end()


HOVER_BODY_H = 56       # idle hover bar: height below the notch band (the input row + the fade)
HOVER_SHOULDER = 12     # its top corners curve into the screen edge
HOVER_DWELL_MS = 220    # rest the cursor on the notch this long before it grows (VoiceOS ~0.3s)
HOVER_GRACE_MS = 280    # after the cursor leaves, wait this long before tucking back
WIN_HOT_H = 12          # Windows: at rest only the resting pill (the screen's top 12 px over it) wakes the strip (§8r)
# Windows has no notch to aim for, so at rest a small black pill sits at the top centre where it would be (the guide:
# "show the resting strip as a small black pill"; 2026-11-08 — "I'm not seeing anything that I can open"). Solid and
# still (no pulsing hint — rejected before), hidden over a fullscreen app, never in screenshots (capture-excluded).
WIN_PILL_W = 128
WIN_PILL_H = 10
# Windows has no notch; the hover bar is laid out around one (orb IN the band beside the notch, the input
# under it — with a 0 band they overlapped). Use the MacBook's: a 32 px band, a 190 px notch → the same
# 88 px-tall, 399 px-wide bar as on the Mac (guide 02-hover-bar).
WIN_HOVER_BAND = 32
WIN_NOTCH_W = 190
# Glyphs Windows' UI font would draw as colour emoji (⚠) or a grey box (◼): drawn with Segoe UI Symbol,
# which has them as plain text glyphs that take the label's colour — like the Mac.
_SYM = "font-family: 'Segoe UI Symbol', 'Segoe UI', sans-serif; " if IS_WIN else ""
_STOP_SQ = "◼"


def _orb_pixmap(px: int):
    """The small lavender orb VoiceOS shows beside the notch (painted, no asset)."""
    from PySide6.QtGui import QPixmap, QRadialGradient
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    g = QRadialGradient(px * .35, px * .3, px * .85)
    g.setColorAt(0, QColor(248, 244, 255))
    g.setColorAt(.55, QColor(214, 200, 255))
    g.setColorAt(1, QColor(170, 150, 235))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(QRectF(.5, .5, px - 1, px - 1))
    p.end()
    return pm


class _HoverBar(QWidget):
    """Idle hover, VoiceOS-style: the notch grows into a wider black bar — a lavender orb beside the
    notch and a "Type or hold fn ⌃ to speak" input under it — fading to see-through at the bottom.
    Enter submits like the card's type field; a click anywhere on it puts the cursor in the input."""
    submitted = Signal(str)
    close_requested = Signal()               # clicked again while empty → tuck the bar away

    def __init__(self, on_field_click, parent=None):
        super().__init__(parent)
        self.field_clicked = False           # clicked into the input → stay a bit longer on leave

        self._on_field_click = on_field_click  # keys the panel so it can take typing

        def _click():
            self._toggle()
        self._orb = QLabel(self)
        self._orb.setFixedSize(16, 16)
        self._orb.setPixmap(_orb_pixmap(16))
        self.field = _TypeField(_click, self)
        self.field.setStyleSheet("QLineEdit{background:transparent;border:none;color:#F2F2F4;"
                                 "font-size:14px;padding:0;}")
        self.field.returnPressed.connect(self._submit)
        self._hint = QLabel(self)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._hint.setTextFormat(Qt.RichText)
        self._hint.setText(_SPEAK_HINT)
        self._hint.setStyleSheet("background:transparent;color:#8E8E96;font-size:14px;")
        self.field.textChanged.connect(lambda t: self._hint.setVisible(not t))

    def place(self, notch_band: int) -> None:
        """Orb in the notch band beside the notch (left); the input row just below the band."""
        s = HOVER_SHOULDER
        self._orb.move(s + 16, max(4, (notch_band - 16) // 2))
        row = (s + 16, notch_band + 8, max(40, self.width() - 2 * s - 32), 30)
        self.field.setGeometry(*row)
        self._hint.setGeometry(*row)

    def reset(self) -> None:
        self.field.clear()
        self.field.clearFocus()
        self.field_clicked = False

    def _submit(self) -> None:
        text = self.field.text().strip()
        if text:
            self.field.clear()
            self.submitted.emit(text)

    def paintEvent(self, _e) -> None:
        from PySide6.QtGui import QPainterPath, QLinearGradient
        w, h, s, r = self.width(), self.height(), HOVER_SHOULDER, 18
        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(w, 0)
        path.cubicTo(w - s * .45, 0, w - s, s * .45, w - s, s)     # top-right: curves into the edge
        path.lineTo(w - s, h - r)
        path.quadTo(w - s, h, w - s - r, h)
        path.lineTo(s + r, h)
        path.quadTo(s, h, s, h - r)
        path.lineTo(s, s)
        path.cubicTo(s, s * .45, s * .45, 0, 0, 0)                 # top-left: curves into the edge
        path.closeSubpath()
        g = QLinearGradient(0, 0, 0, h)                            # solid black, fading at the bottom
        g.setColorAt(0, QColor(0, 0, 0, 255))
        g.setColorAt(.6, QColor(0, 0, 0, 255))
        g.setColorAt(1, QColor(0, 0, 0, 95))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillPath(path, g)
        p.end()

    def _toggle(self) -> None:
        """Click = toggle, like the old card: the first click puts the cursor in the input (same look,
        no switch to a different card); clicking again closes it — unless you've typed something, then
        a click just places the cursor (Esc still closes)."""
        if self.field_clicked and not self.field.text():
            self.close_requested.emit()
            return
        self.field_clicked = True
        self._on_field_click()
        self.field.setFocus()

    def mousePressEvent(self, e) -> None:
        self._toggle()
        e.accept()


class ClaudeOverlay(QWidget):
    """Ambient Assistant notch. Rests as a handle; hover expands; never auto-drops."""

    approve_clicked = Signal()
    deny_clicked = Signal()
    open_in = Signal(str)         # "Open in …" / the ⌄ row: "app" (desktop app) or "terminal"; the pick becomes the default
    dismissed = Signal()
    model_changed = Signal(str, str)   # (model, effort) picked on the card's model pill
    stop_clicked = Signal()            # "Stop" pressed while a task is running
    submit_text = Signal(str)          # a typed task submitted from the bottom input field
    typing_started = Signal()          # something typed in the card's field or the hover bar (the app warms up)
    collapsed = Signal()               # the open card tucked back into the notch
    setup_requested = Signal(str, str)  # (agent, "install"/"signin") → open that agent's setup in Terminal
    update_requested = Signal()        # "Update Codex" in the model panel (a newer Codex has newer models)
    codex_choice = Signal(str, bool)   # a chat the Codex app holds: ("close" | "new", always close Codex)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(WIN_TITLE)
        # Qt keeps a window's layout out of the notch ("safe area") by default — +32px at the top once
        # the window really sits over the notch. This one is DESIGNED around the notch, so opt out.
        self.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)
        self.setFixedWidth(WIDGET_W)
        # System font (like organize) — hand-tuned heights must not shift under the app font.
        self.setStyleSheet("QWidget { font-family: '-apple-system', 'SF Pro Display', "
                           "'Helvetica Neue', 'Segoe UI', sans-serif; }")

        self._state = "idle"
        self._minimized = True
        self._pinned = False          # click the handle to keep it open
        self._anim = None
        self._task_text = ""          # the prompt we're running — shown on the done page
        self._result_text = ""        # what Claude generated — shown on the done page
        self._details_open = False    # is the live "Details" feed expanded?
        self._detail_last = ""        # last line appended to the feed (dedup)
        self._entries = []            # rich Details: the conversation's entries (see details_view)
        self._rich = False            # True once entries drive Details (plain status lines stop)
        self._setup_kind = ""         # the error is a setup step ("install"/"signin"/"usage"), not a failure
        self._setup_agent = "claude"  # …for which agent
        self._agent = "claude"        # the agent this card shows ("claude" / "codex"): models, Open in …
        self._model_names = dict(_MODEL_NAMES)   # the picker's models for that agent
        self._expanded = set()        # entry ids whose "Show full" is open
        self._stopped = False         # the done card is a Stop (its last reply stays in Details)
        self._stopping = False        # Stop clicked, the turn is winding down — keep "Stopping…" (not Stop again)
        self._model = "opus"          # a real model is always chosen: opus/sonnet/haiku
        self._effort = "high"         # a real effort is always chosen: low/medium/high/xhigh/max
        self._model_open = False      # is the inline "Model & effort" panel expanded?
        self._model_list_open = False # is the model dropdown list expanded?

        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(self._maybe_collapse)
        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(self.dismiss)
        self._flash_timer = QTimer(self)      # briefly-open "receipt", then tuck away
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self._elapsed_timer = QTimer(self)    # "Working 2:26" counter in the header
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._task_started = None
        self._hover_in = False                # is the cursor over the resting notch strip?
        self._hover_poll = QTimer(self)       # resting hover = cursor poll (see _poll_hover)
        self._hover_poll.setInterval(60)
        self._hover_poll.timeout.connect(self._poll_hover)
        self._hover_bar_on = False            # idle hover bar showing (see _show_hover_bar)

        self._build_ui()
        self._hover_bar = _HoverBar(self._key_for_typing, self)
        self._hover_bar.field.textChanged.connect(lambda t: t.strip() and self.typing_started.emit())
        self._hover_bar.hide()
        self._hover_bar.submitted.connect(self._on_hover_submit)
        self._hover_bar.close_requested.connect(self._hide_hover_bar)
        self._dwell_timer = QTimer(self)      # cursor must rest on the notch briefly before it grows
        self._dwell_timer.setSingleShot(True)
        self._dwell_timer.setInterval(HOVER_DWELL_MS)
        self._dwell_timer.timeout.connect(self._show_hover_bar)
        self._bar_grace = QTimer(self)        # after the cursor leaves the bar
        self._bar_grace.setSingleShot(True)
        self._bar_grace.timeout.connect(self._maybe_hide_hover_bar)

    # -- construction ----------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, 0, SHADOW_MARGIN, SHADOW_MARGIN)

        self._card = QFrame()
        self._card.setObjectName("claudeCard")
        # Rounded on all corners: it hangs just BELOW the notch now (not flush to the top edge),
        # so a square top read as a detached slab. A full rounded card looks intentional/centred.
        # Square top (flush with the notch, merges with it), rounded bottom — it drops FROM the notch.
        # The open card's black shape is PAINTED (paintEvent) like the hover bar's: flush with the
        # notch, top corners curving into the screen edge, rounded bottom, a soft fade. Flat, no shadow.
        self._card_style = "QFrame#claudeCard { background: transparent; border: none; }"
        self._card.setStyleSheet(self._card_style)
        outer.addWidget(self._card)

        col = QVBoxLayout(self._card)
        col.setContentsMargins(16, 12, 16, 12)
        col.setSpacing(9)

        # Transcript bubble (VoiceOS-style): "what you said", a lighter rounded box above the card.
        self._transcript = QLabel("")
        self._transcript.setWordWrap(True)
        self._transcript.setMaximumHeight(TRANSCRIPT_H)
        self._transcript.setStyleSheet(
            f"QLabel {{ background: {C['surface']}; color: {C['text']}; font-size: 13px; "
            "border-radius: 16px; padding: 9px 15px; }")
        self._transcript.setVisible(False)
        col.addWidget(self._transcript)
        self._prompt_shown_in_full = False   # the bubble holds the whole prompt → Details skips it
        self._title_label = QLabel("")
        self._title_label.setFixedHeight(TITLE_H)
        self._title_label.setStyleSheet(
            f"QLabel {{ color: {C['text']}; font-size: 14px; font-weight: 600; background: transparent; "
            "border: none; padding-left: 2px; }")
        self._title_label.setVisible(False)
        col.addWidget(self._title_label)

        # The "Claude Code" card sits on the black backing — give it a subtly lighter inset look.
        # Header (VoiceOS-style): [icon] Claude Code  ···  <status> <timer>  [⇣] [✕]
        self._topbar = QWidget()
        self._topbar.setFixedHeight(TOPBAR_H)
        self._topbar.setStyleSheet("background: transparent;")
        top = QHBoxLayout(self._topbar)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self._icon = QLabel()
        self._icon.setFixedSize(20, 20)
        self._icon.setAlignment(Qt.AlignCenter)
        _logo = _load_logo(20)
        if _logo is not None:
            self._icon.setPixmap(_logo)
            self._icon.setStyleSheet("background: transparent; border: none;")
        else:
            self._icon.setPixmap(_orb_pixmap(16))   # the hover bar's orb until a real logo is in assets/
            self._icon.setStyleSheet("background: transparent; border: none;")
        top.addWidget(self._icon, 0, Qt.AlignVCenter)
        self._brand = QLabel("Claude Code")     # kept for reference; the header now sits beside the
        self._brand.setVisible(False)            # notch (VoiceOS) and has no room for a title
        self._brand.setStyleSheet(
            f"color: {C['text_2']}; font-size: 12.5px; font-weight: 500; background: transparent; border: none;")
        self._status_label = QLabel("")        # Working / Needs you / Done / Error
        self._status_label.setStyleSheet(
            f"color: {C['text_muted']}; font-size: 12px; font-weight: 600; background: transparent; border: none;")
        top.addWidget(self._status_label, 0, Qt.AlignVCenter)
        self._timer_label = QLabel("")         # elapsed, e.g. 2:26
        self._timer_label.setStyleSheet(
            f"color: {C['text_2']}; font-size: 12px; background: transparent; border: none;")
        top.addWidget(self._timer_label, 0, Qt.AlignVCenter)
        top.addStretch(1)                       # the physical notch sits in this gap
        close = QPushButton()
        close.setIcon(QIcon(_glyph("close", 13, color=QColor(C["text_2"]))))
        close.setIconSize(QSize(13, 13))
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("Close")
        close.setAccessibleName("Close")
        close.setFixedSize(22, 22)
        close.setStyleSheet(self._icon_btn_style())
        close.clicked.connect(self._on_dismiss)
        top.addWidget(close, 0, Qt.AlignVCenter)
        col.insertWidget(0, self._topbar)       # first: it lives in the notch band, above the bubble

        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        self._page_working = self._make_working_page()    # 0
        self._page_needs = self._make_needs_page()         # 1
        self._page_done = self._make_message_page("done")  # 2
        self._page_error = self._make_message_page("error")  # 3
        self._page_idle = self._make_idle_page()            # 4
        for pg in (self._page_working, self._page_needs, self._page_done, self._page_error, self._page_idle):
            self._stack.addWidget(pg)
        col.addWidget(self._stack)

        # Collapsible live feed — card-level so Details works in any state. A QTextBrowser so the rich
        # feed's links work: Open a file / page, Show full on a preview (handled in _on_details_link).
        self._details_feed = QTextBrowser()
        self._details_feed.setReadOnly(True)
        self._details_feed.setOpenLinks(False)
        self._details_feed.anchorClicked.connect(self._on_details_link)
        self._details_feed.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # content wraps instead
        # The markdown fragments rely on Qt's own "p, li { white-space: pre-wrap }" (kept spaces in code).
        self._details_feed.document().setDefaultStyleSheet("p, li { white-space: pre-wrap; }")
        self._details_feed.setFixedHeight(DETAILS_H - 6)
        self._details_feed.setVisible(False)
        self._details_feed.setStyleSheet(
            f"QTextEdit {{ background: #111113; border: none; border-radius: 12px; "
            f"color: {C['text_2']}; font-size: 11.5px; padding: 8px; }}" + _SCROLLBAR_CSS)
        col.addWidget(self._details_feed)

        # Persistent control row: [model·effort pill]   [Details]  [primary action]
        self._ctrl_row = QWidget()
        self._ctrl_row.setFixedHeight(CTRL_H)
        self._ctrl_row.setStyleSheet("background: transparent;")
        ctrl = QHBoxLayout(self._ctrl_row)
        ctrl.setContentsMargins(0, 0, 0, 0)
        ctrl.setSpacing(8)
        self._model_btn = QPushButton("Auto  ⌄")     # model · thinking-effort picker
        self._model_btn.setCursor(Qt.PointingHandCursor)
        self._model_btn.setFixedHeight(26)
        self._model_btn.setStyleSheet(
            f"QPushButton {{ background: {C['surface']}; border: none; "
            f"color: {C['text_2']}; font-size: 11.5px; font-weight: 600; border-radius: 13px; padding: 4px 12px; }}"
            f"QPushButton:hover {{ background: #26262A; color: {C['text']}; }}")
        self._model_btn.clicked.connect(self._toggle_model_panel)
        ctrl.addWidget(self._model_btn)
        self._details_btn = QPushButton("Details  ⌄")
        self._details_btn.setCursor(Qt.PointingHandCursor)
        self._details_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; "
            f"color: {C['text_muted']}; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {C['text']}; }}")
        self._details_btn.clicked.connect(self._toggle_details)
        ctrl.addWidget(self._details_btn, 1, Qt.AlignRight)   # takes the slack itself (a stretch cost a gap)
        self._primary_btn = QPushButton("")          # Stop (working) / Open in Claude (done/error)
        self._primary_btn.setCursor(Qt.PointingHandCursor)
        self._primary_btn.setFixedHeight(26)
        self._primary_btn.clicked.connect(self._on_primary)
        # ⌄ beside "Open in …": the one other place to open it (Terminal, or back to the desktop app).
        self._open_more = QPushButton("⌄")
        self._open_more.setCursor(Qt.PointingHandCursor)
        self._open_more.setToolTip("Open it somewhere else")
        self._open_more.setFixedSize(26, 26)
        self._open_more.setStyleSheet(
            f"QPushButton {{ background: {C['surface']}; color: {C['text_2']}; border: none; "
            "border-top-right-radius: 13px; border-bottom-right-radius: 13px; "
            "border-top-left-radius: 0; border-bottom-left-radius: 0; font-size: 12px; font-weight: 700; "
            "padding: 0 0 3px 0; }"
            f"QPushButton:hover {{ background: {ACCENT}; color: #0B0B0E; }}")
        self._open_more.clicked.connect(self._toggle_open_panel)
        self._open_more.setVisible(False)
        opn = QHBoxLayout()
        opn.setSpacing(1)
        opn.addWidget(self._primary_btn)
        opn.addWidget(self._open_more)
        ctrl.addLayout(opn)
        col.addWidget(self._ctrl_row)

        # Inline "Model & effort" panel — opens below the controls when the model pill is tapped.
        # Inline (not a QMenu/popup) so it RELIABLY receives clicks from the non-activating notch
        # panel (a popup can't become key there, which is why the old dropdown did nothing).
        self._model_panel = QFrame()
        self._model_panel.setObjectName("modelPanel")      # targeted bg so it reads as a dark box
        self._model_panel.setStyleSheet(
            f"QFrame#modelPanel {{ background:#111113; border:1px solid {C['border']}; border-radius:14px; }}")
        self._model_panel.setVisible(False)
        mp = QVBoxLayout(self._model_panel)
        mp.setContentsMargins(14, 12, 14, 12)
        mp.setSpacing(6)
        _title = QLabel("Model & effort")
        _title.setStyleSheet(f"color:{C['text']}; font-size:12.5px; font-weight:700; background:transparent; border:none;")
        mp.addWidget(_title)
        _mlbl = QLabel("Model")
        _mlbl.setStyleSheet(f"color:{C['text_muted']}; font-size:10.5px; font-weight:600; background:transparent; border:none;")
        mp.addWidget(_mlbl)
        # Model DROPDOWN (box + inline list) — like VoiceOS. Inline so it works on the notch panel.
        self._model_select = QPushButton("Opus")
        self._model_select.setCursor(Qt.PointingHandCursor)
        self._model_select.setFixedHeight(32)
        self._model_select.setStyleSheet(
            f"QPushButton {{ background:#202026; color:{C['text']}; border:1px solid {C['border']}; "
            "border-radius:9px; font-size:12.5px; font-weight:600; text-align:left; padding:0 12px; }"
            f"QPushButton:hover {{ border-color:{ACCENT}; }}")
        self._model_select.clicked.connect(self._toggle_model_list)
        mp.addWidget(self._model_select)
        self._model_list = QWidget()
        self._model_list.setStyleSheet("background:transparent;")
        self._model_list.setVisible(False)
        ml = QVBoxLayout(self._model_list)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(3)
        self._model_rows = {}
        self._build_model_rows()
        mp.addWidget(self._model_list)
        # A newer Codex exists: its newer models only show once Codex is updated (one click, ~5s).
        self._update_btn = QPushButton("")
        self._update_btn.setCursor(Qt.PointingHandCursor)
        self._update_btn.setFixedHeight(28)
        self._update_btn.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{ACCENT}; border:1px dashed {ACCENT}; "
            "border-radius:8px; font-size:11.5px; font-weight:600; text-align:left; padding:0 12px; }"
            f"QPushButton:hover {{ background:{C['surface']}; }}"
            f"QPushButton:disabled {{ color:{C['text_muted']}; border-color:{C['border']}; }}")
        self._update_btn.clicked.connect(self._on_update_clicked)
        self._update_btn.setVisible(False)
        mp.addWidget(self._update_btn)
        mp.addSpacing(10)          # clear gap so the model dropdown and the effort controls read as
        erow = QHBoxLayout()       # two separate groups, not one cramped block
        _elbl = QLabel("Effort")
        _elbl.setStyleSheet(f"color:{C['text_muted']}; font-size:10.5px; font-weight:600; background:transparent; border:none;")
        erow.addWidget(_elbl)
        self._effort_name = QLabel("High")
        self._effort_name.setStyleSheet(f"color:{ACCENT}; font-size:11.5px; font-weight:700; background:transparent; border:none;")
        erow.addSpacing(8)
        erow.addWidget(self._effort_name)
        erow.addStretch(1)
        self._auto_btn = QPushButton("Auto")
        self._auto_btn.setCursor(Qt.PointingHandCursor)
        self._auto_btn.setFixedHeight(22)
        self._auto_btn.clicked.connect(self._toggle_auto_effort)
        erow.addWidget(self._auto_btn)
        mp.addLayout(erow)
        self._effort_slider = QSlider(Qt.Horizontal)
        self._effort_slider.setRange(0, 4)
        self._effort_slider.setPageStep(1)
        self._effort_slider.setFixedHeight(18)
        # No tick marks: enabling them made Qt draw a white groove frame (the rendering bug).
        self._effort_slider.valueChanged.connect(self._on_effort_slider)
        mp.addWidget(self._effort_slider)
        ends = QHBoxLayout()
        _f = QLabel("Faster")
        _s = QLabel("Smarter")
        for _e in (_f, _s):
            _e.setStyleSheet(f"color:{C['text_muted']}; font-size:9.5px; background:transparent; border:none;")
        ends.addWidget(_f)
        ends.addStretch(1)
        ends.addWidget(_s)
        mp.addLayout(ends)
        col.addWidget(self._model_panel)

        # The ⌄ menu: ONE row — the other place ("Open in Terminal" / "Open in Claude app"); picking it
        # makes it the default. Inline (not a popup), like the model panel, so it gets clicks on the
        # non-activating notch panel.
        self._open_panel = QFrame()
        self._open_panel.setObjectName("openPanel")
        self._open_panel.setStyleSheet(
            f"QFrame#openPanel {{ background:#111113; border:1px solid {C['border']}; border-radius:14px; }}")
        self._open_panel.setVisible(False)
        self._open_open = False
        self._open_where = {"claude": "app", "codex": "app"}   # per agent; the page seeds it from settings
        op = QVBoxLayout(self._open_panel)
        op.setContentsMargins(8, 8, 8, 8)
        self._open_alt = QPushButton("")
        self._open_alt.setCursor(Qt.PointingHandCursor)
        self._open_alt.setFixedHeight(28)
        self._open_alt.setStyleSheet(
            f"QPushButton {{ background:#202026; color:{C['text_2']}; border:1px solid {C['border']}; "
            "border-radius:8px; font-size:12px; text-align:left; padding:0 12px; }"
            f"QPushButton:hover {{ background:{C['surface']}; color:{C['text']}; border-color:{ACCENT}; }}")
        self._open_alt.clicked.connect(self._pick_open)
        op.addWidget(self._open_alt)
        col.addWidget(self._open_panel)

        # Type a task instead of speaking (VoiceOS-style). Enter routes it through the SAME intent
        # router as voice (new / follow-up / resume). Precise fallback when the mic mishears.
        self._type_field = _TypeField(self._key_for_typing)
        self._type_field.textChanged.connect(lambda t: t.strip() and self.typing_started.emit())
        self._type_field.setObjectName("typeField")
        self._type_field.setFixedHeight(TYPEFIELD_H - 8)
        self._type_field.setStyleSheet(
            f"QLineEdit#typeField{{background:{C['surface']}; color:{C['text']}; border:1px solid transparent;"
            f" border-radius:16px; padding:4px 14px; font-size:13px;}}"
            "QLineEdit#typeField:focus{border-color:rgba(167,139,250,0.45);}")
        self._type_field.set_hint(_SPEAK_HINT)
        self._type_field.returnPressed.connect(self._on_type_submit)
        # Chat-style SEND button (painted ↑) — Enter still works; enabled only when there's text.
        self._send_btn = _SendButton()
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.setFixedSize(TYPEFIELD_H - 8, TYPEFIELD_H - 8)
        self._send_btn.setCursor(Qt.PointingHandCursor)
        self._send_btn.setEnabled(False)
        self._send_btn.setStyleSheet(
            f"QPushButton#sendBtn{{background:{ACCENT}; border:none; border-radius:16px;}}"
            f"QPushButton#sendBtn:disabled{{background:{C['surface']}; border:none; border-radius:16px;}}"
            f"QPushButton#sendBtn:hover{{background:{ACCENT_LIGHT};}}")
        self._send_btn.clicked.connect(self._on_type_submit)
        self._type_field.textChanged.connect(lambda s: self._send_btn.setEnabled(bool(s.strip())))
        trow = QHBoxLayout()
        trow.setContentsMargins(0, 0, 0, 0)
        trow.setSpacing(7)
        trow.addWidget(self._type_field, 1)
        trow.addWidget(self._send_btn)
        col.addLayout(trow)

        # Footer: keycap shortcut hint (VoiceOS-style), subtle, below the content.
        self._footer = QLabel()
        self._footer.setFixedHeight(FOOTER_H)
        self._footer.setTextFormat(Qt.RichText)
        self._footer.setText(self._hint_html())
        self._footer.setStyleSheet(
            f"color: {C['text_muted']}; font-size: 11px; background: transparent; border: none;")
        col.addWidget(self._footer)

        # Resting handle (shown when minimized; the card is hidden then).
        self._handle = _Handle()
        self._handle.clicked.connect(self._on_handle_clicked)
        col.addWidget(self._handle)

        self._sync_model_panel()        # initial model-button highlight + slider position
        self._apply_minimized_chrome(True)

    def paintEvent(self, _e) -> None:
        """The OPEN card, drawn like the hover bar (VoiceOS): one black shape flush with the notch, its
        top corners curving into the screen edge, rounded bottom corners, a soft fade at the very
        bottom. Resting / hover-bar states draw nothing here (the handle / bar paint themselves)."""
        if self._minimized:
            return
        from PySide6.QtGui import QPainterPath, QLinearGradient
        r = self._card.geometry()
        x0, x1, h = r.left(), r.right() + 1, r.bottom() + 1
        s, rad = HOVER_SHOULDER, 22
        path = QPainterPath()
        path.moveTo(x0 - s, 0)
        path.lineTo(x1 + s, 0)
        path.cubicTo(x1 + s * .55, 0, x1, s * .45, x1, s)        # top-right: curves into the edge
        path.lineTo(x1, h - rad)
        path.quadTo(x1, h, x1 - rad, h)
        path.lineTo(x0 + rad, h)
        path.quadTo(x0, h, x0, h - rad)
        path.lineTo(x0, s)
        path.cubicTo(x0, s * .45, x0 - s * .55, 0, x0 - s, 0)    # top-left: curves into the edge
        path.closeSubpath()
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, QColor(0, 0, 0, 255))
        g.setColorAt(max(0.0, (h - 22) / max(h, 1)), QColor(0, 0, 0, 255))
        g.setColorAt(1, QColor(0, 0, 0, 205))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillPath(path, g)
        p.end()

    def _hint_html(self) -> str:
        chip = (f"background:{C['surface']}; border:1px solid {C['border']}; border-radius:4px; "
                "padding:1px 5px; font-size:10px;")
        return ("Hold " + " ".join(f"<span style='{chip}'>{k}</span>" for k in _voice_key_label().split("+"))
                + " to speak")

    def _key_for_typing(self):
        """User clicked the type field — let the non-activating panel take keyboard focus for it.
        A user click is allowed to key a floating becomesKeyOnlyIfNeeded panel; a no-click grab is not.
        Windows: drop WS_EX_NOACTIVATE and activate on THIS click only (put back when you're done)."""
        if IS_WIN:
            notch_panel.allow_typing(self, True)
            return
        if sys.platform != "darwin":
            return
        try:
            ns = notch_panel._ns_window(WIN_TITLE)
            if ns is not None and hasattr(ns, "makeKeyWindow"):
                ns.makeKeyWindow()
        except Exception:
            pass

    def _on_type_submit(self):
        text = (self._type_field.text() or "").strip()
        if not text:
            return
        self._type_field.clear()
        self._type_field.clearFocus()
        self._end_typing()
        self.submit_text.emit(text)       # -> AssistantPage.run_voice_task (same routing as voice)

    def _icon_btn_style(self) -> str:
        return (
            "QPushButton { background: #2C2C31; border: none; border-radius: 11px; padding: 0; }"
            "QPushButton:hover { background: #3A3A41; }")

    def _make_idle_page(self) -> QWidget:
        """Shown when the notch is opened at idle (no task): a prompt to type or speak."""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 2, 0, 0)
        lbl = QLabel(f"Ready — type a task below, or hold {_voice_key_label()} to speak.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{C['text_muted']}; font-size:12px; background:transparent; border:none;")
        lay.addWidget(lbl)
        lay.addStretch(1)
        return w

    def _make_working_page(self) -> QWidget:
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(pg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._work_spinner = _Spinner()          # kept (hidden) — the header status conveys activity
        self._work_spinner.setVisible(False)
        self._work_label = QLabel("")            # kept for compatibility; NOT shown (the transcript
        self._work_label.setVisible(False)       # bubble at the top is the task — no duplicate title)
        # Current ACTION (muted), one line — the only thing the working page shows; the task is the
        # bubble, the full activity is in Details. No stretch here (it caused a big empty gap).
        self._tool_chip = QLabel("")
        self._tool_chip.setWordWrap(False)
        self._tool_chip.setStyleSheet(
            f"color: {C['text_muted']}; font-size: 12.5px; background: transparent; border: none;")
        lay.addWidget(self._tool_chip, 0, Qt.AlignVCenter)
        return pg

    def _make_needs_page(self) -> QWidget:
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(pg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._needs_title = QLabel("Claude wants to run a tool")
        self._needs_title.setWordWrap(True)
        self._needs_title.setStyleSheet(
            f"color: {C['text']}; font-size: 14px; font-weight: 700; background: transparent; border: none;")
        lay.addWidget(self._needs_title)
        self._needs_detail = QLabel("")
        self._needs_detail.setWordWrap(True)
        self._needs_detail.setMaximumHeight(60)         # cap — full args live in the Activity feed
        self._needs_detail.setStyleSheet(
            f"color: {C['text_2']}; font-size: 11px; font-family: monospace; "
            "background: transparent; border: none;")
        lay.addWidget(self._needs_detail)
        lay.addStretch(1)
        btns = QHBoxLayout()
        btns.setSpacing(10)
        deny = QPushButton("Deny")
        deny.setCursor(Qt.PointingHandCursor)
        deny.setFixedHeight(34)
        deny.setStyleSheet(
            f"QPushButton {{ background: {C['surface']}; color: {RED}; border: none; "
            "border-radius: 17px; font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: rgba(242,100,90,0.16); }")
        deny.clicked.connect(self._on_deny)
        btns.addWidget(deny, 1)
        approve = QPushButton("Approve")
        approve.setCursor(Qt.PointingHandCursor)
        approve.setFixedHeight(34)
        approve.setStyleSheet(
            f"QPushButton {{ background: {GREEN}; color: white; border: none; "
            "border-radius: 17px; font-size: 13px; font-weight: 700; }"
            "QPushButton:hover { background: #278238; }")
        approve.clicked.connect(self._on_approve)
        btns.addWidget(approve, 1)
        lay.addLayout(btns)
        return pg

    def _make_message_page(self, kind: str) -> QWidget:
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(pg)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(10)
        glyph = QLabel("✓" if kind == "done" else "⚠")
        glyph.setFixedWidth(16)                  # fixed, so the answer's width (DONE_TEXT_W) is exact
        glyph.setStyleSheet(
            f"color: {GREEN if kind == 'done' else RED}; font-size: 16px; font-weight: 700; {_SYM}"
            "background: transparent; border: none;")
        row.addWidget(glyph, 0, Qt.AlignTop)
        if kind == "done":
            self._done_glyph = glyph             # ✓ done / ◼ stopped (show_done)
            # The ANSWER itself — Claude's whole final reply, formatted (lists, bold) — not a one-line
            # summary (its first paragraph was often just "Here are the files:"). Scrolls past DONE_MAX_H.
            view = QTextBrowser()
            view.setReadOnly(True)
            view.setOpenLinks(False)
            view.anchorClicked.connect(self._on_details_link)   # the deliverable's Open link
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            view.document().setDefaultStyleSheet("p, li { white-space: pre-wrap; }")
            view.document().setDocumentMargin(0)
            view.document().setIndentWidth(14)   # Qt's default 40px list indent wastes a narrow card
            view.setStyleSheet(
                f"QTextEdit {{ background: transparent; border: none; color: {C['text']}; "
                f"font-size: 13px; padding: 0; }}" + _SCROLLBAR_CSS)
            view.setFixedHeight(_STACK_H["done"] - 4)
            row.addWidget(view, 1)
            self._done_view = view
        else:
            label = QLabel("")
            label.setWordWrap(True)
            label.setStyleSheet(
                f"color: {C['text']}; font-size: 13px; background: transparent; border: none;")
            row.addWidget(label, 1)
            self._error_label = label
        lay.addLayout(row)
        if kind != "done":
            # Claude Code missing / signed out: one click opens Terminal with the official setup.
            self._setup_btn = QPushButton("")
            self._setup_btn.setCursor(Qt.PointingHandCursor)
            self._setup_btn.setFixedHeight(32)
            self._setup_btn.setStyleSheet(
                f"QPushButton {{ background: {ACCENT}; color: #0B0B0E; border: none; border-radius: 16px; "
                "font-size: 12.5px; font-weight: 700; padding: 0 18px; }"
                f"QPushButton:hover {{ background: {ACCENT_LIGHT}; }}")
            self._setup_btn.clicked.connect(self._on_setup_btn)
            self._setup_btn.setVisible(False)
            # A chat the Codex app holds: "Close Codex and continue here" (above) or a new chat (this).
            self._choice_btn = QPushButton("New chat instead")      # (the card is narrow; Segoe UI is wider)
            self._choice_btn.setCursor(Qt.PointingHandCursor)
            self._choice_btn.setFixedHeight(32)
            self._choice_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {C['text_2']}; border: 1px solid {C['border']}; "
                "border-radius: 16px; font-size: 12.5px; font-weight: 600; padding: 0 16px; }"
                f"QPushButton:hover {{ color: {C['text']}; border-color: {ACCENT}; }}")
            self._choice_btn.clicked.connect(lambda: self.codex_choice.emit("new", False))
            self._choice_btn.setVisible(False)
            brow = QHBoxLayout()
            brow.setSpacing(8)
            brow.addWidget(self._setup_btn)
            brow.addWidget(self._choice_btn)
            brow.addStretch(1)
            lay.addLayout(brow)
            self._always_box = QCheckBox("Always close Codex for this")
            self._always_box.setCursor(Qt.PointingHandCursor)
            self._always_box.setStyleSheet(
                f"QCheckBox {{ color: {C['text_muted']}; font-size: 11.5px; background: transparent; spacing: 8px; }}"
                f"QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid {C['text_muted']};"
                " border-radius: 4px; background: transparent; }"
                f"QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}"
                f"QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}")
            self._always_box.setVisible(False)
            lay.addWidget(self._always_box, 0, Qt.AlignLeft)
        lay.addStretch(1)
        return pg

    # -- public API (called from the UI thread via AssistantPage slots) --------
    def present(self, pin: bool = True) -> None:
        """Start a task: rest the panel tucked into the physical notch (a slim status nub), peek it
        open briefly via flash, then tuck back. HOVER the notch to slide it open (VoiceOS-style, like
        the left rail); click the ⇣ pin to keep it open while you watch a task. Dismiss with ✕.
        `pin=False` ("Open answers automatically" off): the task's card stays tucked until you open it."""
        self._done_timer.stop()
        self._stopping = False
        # A task keeps the card OPEN — while it runs and once it's done — until YOU close it (click
        # the card, ✕ or Esc). 2026-11-05: the auto-tuck hid the answer before it arrived.
        if pin:
            self._pinned = True
        self._set_task_title("")             # set_task / load_state fill it for this task
        self.set_state("working")
        self._set_status("Working", C["text_muted"])
        self._start_elapsed()        # reset the "Working m:ss" counter for this task
        self._reset_details()        # fresh live feed, collapsed, for the new task
        # Already up (e.g. the "searching" card -> the task starting): keep it up, just re-centre —
        # don't collapse to a pill and re-expand, which flickers between routing and the task.
        if self.isVisible() and not self._minimized:
            self._reanchor()
            self._reassert()
            return
        self._minimized = True
        self._end_typing()
        self._apply_minimized_chrome(True)
        sw = self._strip_w()                 # rest as a strip spanning the whole notch, not full card
        self.setFixedWidth(sw)
        geo = notch_panel.screen_geo()
        if geo is None:
            if not self.isVisible():
                self.show()
            self._configure_macos_deferred()
            return
        self.resize(sw, self._min_h())
        x = geo.x() + (geo.width() - sw) // 2
        self.move(x, geo.y() - self._min_h())    # start just above the top edge, then slide down
        if not self.isVisible():
            self.show()
        self._configure_macos_deferred()
        self._slide_to(x, self._rest_y(geo))

    def rest_idle(self, force: bool = False) -> None:
        """Keep the notch present at idle (no task) as a hoverable nub, so you can hover/click to open
        it and type or speak a task without having run anything first. Shows only a faint hint at the
        notch until you hover. No-op while a task is live or blocked — unless `force` (✕ / Esc, or the
        page letting go of a chat the card can't follow): then the card closes back to the resting
        strip even mid-task, and the chat keeps running in the drawer. (Windows 2026-11-08: without
        this, ✕ while working left the card frozen on "Working" for a chat it no longer followed —
        Stop did nothing, the answer never arrived, and hovering never offered the input bar again.)"""
        if self._state in ("working", "needs_you") and not force:
            return
        if force:
            self._stopping = False
            self._stop_elapsed()
            self._timer_label.setText("")
            self._work_spinner.stop()
            self._flash_timer.stop()
            self._collapse_timer.stop()
            if self._open_open:
                self._set_open_panel(False)
            if self.isVisible() and not self._minimized:
                self._pinned = False
                self.minimize_panel()            # tuck first, so it rests as the strip (not a blank open card)
        self._done_timer.stop()
        self.set_state("idle")
        self._set_status("", C["text_muted"])
        # CLEARED, not just hidden: opening the card again shows the bubble whenever it has text
        # (_apply_minimized_chrome), so a hidden old question came back on the next blank card (Mac 331b229).
        self._transcript.setText("")
        self._transcript.setVisible(False)
        self._set_task_title("")
        self._reset_details()
        self._stack.setCurrentWidget(self._page_idle)
        self._pinned = False
        if self.isVisible() and not self._minimized:
            return                               # already open (you're hovering) — leave it be
        self._minimized = True
        self._apply_minimized_chrome(True)
        sw = self._strip_w()
        self.setFixedWidth(sw)
        geo = notch_panel.screen_geo()
        if geo is None:
            if not self.isVisible():
                self.show()
            self._configure_macos_deferred()
            return
        self.resize(sw, self._min_h())
        x = geo.x() + (geo.width() - sw) // 2
        self.move(x, self._rest_y(geo))
        if not self.isVisible():
            self.show()
        self._configure_macos_deferred()

    def show_new_chat(self) -> None:
        """Rail "New chat": the notch opens on a blank Ready card with the type box focused. Unlike
        rest_idle this also leaves a working chat's view — that chat keeps running in the rail.
        The old question and timer are CLEARED (they came back on the blank card: Mac 331b229, the same on Windows)."""
        self._done_timer.stop()
        self.set_state("idle")
        self._set_status("", C["text_muted"])
        self._stop_elapsed()
        self._timer_label.setText("")
        self._transcript.setText("")
        self._transcript.setVisible(False)
        self._set_task_title("")
        self._reset_details()
        self._stack.setCurrentWidget(self._page_idle)
        if not self.isVisible():
            self.rest_idle()                     # create/position the resting nub first
        self.open_full()
        self._type_field.setFocus()
        self._key_for_typing()

    def set_title(self, title: str) -> None:
        """A short cue in the bubble ("Routing…", "↳ continuing …") before the task is known."""
        title = (title or "").strip()
        if title:
            self._set_bubble(title)
            if not self._minimized:
                self._transcript.setVisible(True)
        if title and not self._details_feed.toPlainText().strip():
            self._append_detail(f"▸ {title}")      # seed the Details feed with the task
        if not self._minimized:
            self._fit()

    def set_task(self, prompt: str, title: str = "") -> None:
        """VoiceOS layout: the bubble shows what you asked (its first two lines; hover for all of it),
        the bold line under it the task's short name."""
        prompt = (prompt or "").strip()
        if prompt and not self._details_feed.toPlainText().strip():
            self._append_detail(f"▸ {prompt}")     # seed the plain feed (rich Details replace it)
        self._set_bubble(prompt or title, is_prompt=bool(prompt))
        self._set_task_title(title if prompt else "")
        if not self._minimized:
            self._transcript.setVisible(bool(self._transcript.text()))
            self._fit()

    def _set_bubble(self, text: str, is_prompt: bool = False) -> None:
        """Cut to the bubble's two lines at a word, with an ellipsis; the full text is the tooltip."""
        text = " ".join((text or "").split())
        f = QFont()
        f.setPixelSize(13)
        fm = QFontMetrics(f)
        w = CARD_W - 32 - 30                      # card minus the column margins and the bubble padding

        def fits(t):
            return fm.boundingRect(0, 0, w, 10000, Qt.TextWordWrap, t).height() <= 2 * fm.lineSpacing()

        full = fits(text)
        shown = text
        if not full:
            lo, hi = 0, len(text)
            while lo < hi:                        # longest prefix that still fits with the "…"
                mid = (lo + hi + 1) // 2
                lo, hi = (mid, hi) if fits(text[:mid].rstrip() + "…") else (lo, mid - 1)
            shown = text[:lo].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
        self._transcript.setText(shown)
        self._transcript.setToolTip("" if full else text)
        self._prompt_shown_in_full = is_prompt and full

    def _set_task_title(self, title: str) -> None:
        f = QFont()
        f.setPixelSize(14)
        f.setWeight(QFont.DemiBold)
        title = QFontMetrics(f).elidedText(" ".join((title or "").split()), Qt.ElideRight, CARD_W - 40)
        self._title_label.setText(title)
        self._title_label.setVisible(bool(title) and not self._minimized)

    def show_working(self, text: str = "", tool: str = "") -> None:
        self.set_state("working")
        # After Stop, the steps Claude was already sending keep arriving for a second or two — they must not
        # flip the card back to "Working" with a live ■ Stop (it read as "my click didn't register").
        self._set_status("Stopping…" if self._stopping else "Working", C["text_muted"])
        if not self._elapsed_timer.isActive():
            self._start_elapsed()
        action = text.strip() if text else (f"Using {tool}…" if tool else "")
        self._tool_chip.setText(action[:160])
        self._tool_chip.setVisible(bool(action))
        if action:
            self._append_detail(action)            # accumulate into the live Details feed
        self._stack.setCurrentWidget(self._page_working)
        if not self._minimized:
            self._fit()
        if self._flash_timer.isActive():
            self._flash_timer.start(FLASH_MS)

    # -- live "Details" feed (collapsible) -------------------------------------
    def _append_detail(self, line: str) -> None:
        if self._rich:                             # entries drive Details — status lines stay in the chip
            return
        line = (line or "").strip()
        if not line or line == self._detail_last:
            return
        self._detail_last = line
        self._details_feed.append(line)
        sb = self._details_feed.verticalScrollBar()
        sb.setValue(sb.maximum())                  # keep the newest line in view

    def _reset_details(self) -> None:
        self._details_open = False
        self._detail_last = ""
        self._entries, self._rich, self._expanded = [], False, set()
        self._details_feed.clear()
        self._details_feed.setVisible(False)
        self._details_btn.setText("Details  ⌄")

    def set_details(self, entries) -> None:
        """Show the conversation's structured entries as the rich Details feed (see details_view):
        replies formatted, and every action with its substance — previews, Show full, Open, diffs,
        command output, failures."""
        self._entries, self._rich = entries, True
        self._render_details(follow=True)

    def _render_details(self, follow: bool) -> None:
        sb = self._details_feed.verticalScrollBar()
        at_bottom, pos = sb.value() >= sb.maximum() - 4, sb.value()
        shown = details_view.visible_entries(self._entries, hide_final=self._state == "done" and not self._stopped,
                                             hide_prompt=self._prompt_shown_in_full)
        self._details_feed.setHtml(details_view.render_html(shown, self._expanded) or (
            f'<p style="color:{C["text_muted"]};">{_AGENT_NAMES.get(self._agent, "Claude")} '
            'answered directly — no other steps.</p>'))
        # Live updates follow the newest activity only if you were already at the bottom; a Show-full
        # toggle keeps your place.
        sb.setValue(sb.maximum() if (follow and at_bottom) else pos)

    def _on_details_link(self, url) -> None:
        """A link in Details or on the done card: Show full/less on a preview, or open a file/page."""
        url = QUrl(url) if isinstance(url, str) else url
        if url.scheme() == "full":
            self._expanded ^= {url.path()}
            self._render_details(follow=False)
        elif url.isLocalFile() or url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def _toggle_details(self) -> None:
        self._details_open = not self._details_open
        if self._details_open and self._rich:
            self._render_details(follow=True)      # (closed, it skips live-reply repaints — catch up now)
        self._details_feed.setVisible(self._details_open)
        self._details_btn.setText("Show less  ⌃" if self._details_open else "Details  ⌄")
        sb = self._details_feed.verticalScrollBar()
        sb.setValue(sb.maximum())
        if not self._minimized:
            self._fit()

    # -- model + thinking-effort picker (inline; reliable on the non-activating panel) ----
    def _build_model_rows(self) -> None:
        """One row per model of the current agent (rebuilt when the card switches agent)."""
        lay = self._model_list.layout()
        for r in self._model_rows.values():
            lay.removeWidget(r)
            r.deleteLater()
        self._model_rows = {}
        for k, name in self._model_names.items():
            r = QPushButton(name)
            r.setCursor(Qt.PointingHandCursor)
            r.setFixedHeight(28)
            r.setStyleSheet(
                f"QPushButton {{ background:#202026; color:{C['text_2']}; border:1px solid {C['border']}; "
                "border-radius:8px; font-size:12px; text-align:left; padding:0 12px; }"
                f"QPushButton:hover {{ background:{C['surface']}; color:{C['text']}; border-color:{ACCENT}; }}")
            r.clicked.connect(lambda _=False, kk=k: self._pick_model(kk))
            self._model_rows[k] = r
            lay.addWidget(r)

    def set_agent(self, agent: str, models=None, update: str = "") -> None:
        """Which agent this card shows: its models in the picker, its name on "Open in …". `models` =
        [(key, display name)]; omitted → Claude's. `update`: a newer Codex version (offers "Update Codex").
        Call set_model_effort after, with that agent's pick."""
        names = dict(models) if models else dict(_MODEL_NAMES)
        self._agent = agent if agent in _AGENT_NAMES else "claude"
        if names != self._model_names:
            self._model_names = names
            self._build_model_rows()
        self.set_update(update if self._agent == "codex" else "")
        _logo = _load_logo(20, self._agent)
        if _logo is not None:
            self._icon.setPixmap(_logo)
        self._update_primary()

    def set_update(self, version: str, busy: bool = False) -> None:
        """Show/hide the "Update Codex" row; `busy` while the update runs."""
        self._update_btn.setText("Updating Codex…" if busy else f"↑  Update Codex to {version} for the newest models")
        self._update_btn.setEnabled(not busy)
        self._update_btn.setVisible(bool(version) or busy)
        if not self._minimized:
            self._fit()

    def _on_update_clicked(self) -> None:
        self.set_update("", busy=True)
        self.update_requested.emit()

    def set_model_effort(self, model: str, effort: str) -> None:
        first = next(iter(self._model_names), "opus")
        model = (model or "").strip()
        self._model = model if model in self._model_names else first   # always a real model
        self._effort = (effort or "").strip()               # '' = Auto; else a slider level
        if self._effort and self._effort not in _EFFORT_ORDER:
            self._effort = "high"
        self._sync_model_panel()
        self._update_model_pill()

    def _update_model_pill(self) -> None:
        ml = self._model_names.get(self._model, self._model or "Opus")
        el = _EFFORT_NAMES.get(self._effort, "High")
        self._model_btn.setText(f"{ml} · {el}  ⌄")

    def _toggle_open_panel(self) -> None:
        self._set_open_panel(not self._open_open)

    def _set_open_panel(self, on: bool) -> None:
        self._open_open = on
        self._open_panel.setVisible(on)
        if on and self._model_open:                 # one menu at a time
            self._toggle_model_panel()
        if not self._minimized:
            self._fit()

    def set_listening(self, mode: str) -> None:
        """Filect Assistant (hands-free) state for the light under the notch: "" | listening | hearing | paused."""
        self._handle.set_listening(mode)

    def flash_note(self, text: str, color: str = "#B8B8C4", ms: int = 2500) -> None:
        """Filect Assistant: a short note under the notch for a moment ("Cancelled", "Not a request")."""
        self._handle.flash_note(text, color, ms)

    def set_open_defaults(self, where: dict) -> None:
        """Where "Open in …" opens, per agent ("app" / "terminal") — the page's saved choice."""
        self._open_where.update({k: v for k, v in where.items() if v in ("app", "terminal")})
        self._update_primary()

    def _pick_open(self) -> None:
        """The ⌄ row: open it in the OTHER place — which becomes the default from now on."""
        self._set_open_panel(False)
        where = "app" if self._open_where.get(self._agent, "app") == "terminal" else "terminal"
        self._open_where[self._agent] = where
        self._update_primary()
        self.open_in.emit(where)

    def _toggle_model_panel(self) -> None:
        self._model_open = not self._model_open
        if self._model_open and self._open_open:     # one menu at a time
            self._set_open_panel(False)
        if not self._model_open:
            self._model_list_open = False
            self._model_list.setVisible(False)
        self._model_panel.setVisible(self._model_open)
        if not self._minimized:
            self._fit()

    def _toggle_model_list(self) -> None:
        self._model_list_open = not self._model_list_open
        self._model_list.setVisible(self._model_list_open)
        self._sync_model_panel()
        if not self._minimized:
            self._fit()

    def _pick_model(self, k: str) -> None:
        self._model = k
        self._model_list_open = False
        self._model_list.setVisible(False)
        self._sync_model_panel()
        self._update_model_pill()
        self.model_changed.emit(self._model, self._effort)
        if not self._minimized:
            self._fit()

    def _sync_model_panel(self) -> None:
        """Reflect the current model/effort on the dropdown, slider, and Auto button."""
        chev = "  ⌃" if self._model_list_open else "  ⌄"
        self._model_select.setText(self._model_names.get(self._model, self._model or "Opus") + chev)
        for k, r in getattr(self, "_model_rows", {}).items():
            r.setVisible(k != self._model)        # don't list the already-selected model
        auto = (self._effort == "")
        idx = _EFFORT_ORDER.index(self._effort) if self._effort in _EFFORT_ORDER else 2
        self._effort_slider.blockSignals(True)
        self._effort_slider.setValue(idx)
        self._effort_slider.blockSignals(False)
        fill = "#4A4A52" if auto else ACCENT              # grey fill when Auto (not manually set)
        self._effort_slider.setStyleSheet(
            "QSlider { background:transparent; border:none; }"
            "QSlider::groove:horizontal { border:none; height:4px; background:#2A2A30; border-radius:2px; }"
            f"QSlider::sub-page:horizontal {{ background:{fill}; border-radius:2px; }}"
            "QSlider::add-page:horizontal { border:none; background:#2A2A30; border-radius:2px; }"
            "QSlider::handle:horizontal { background:white; border:none; width:14px; height:14px; "
            "margin:-6px 0; border-radius:7px; }")
        self._effort_name.setText(_EFFORT_NAMES.get(self._effort, "High"))
        if auto:
            self._auto_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:white; border:none; border-radius:11px; "
                f"font-size:10.5px; font-weight:600; padding:0 10px; }}")
        else:
            self._auto_btn.setStyleSheet(
                f"QPushButton {{ background:transparent; color:{C['text_muted']}; "
                f"border:1px solid {C['border']}; border-radius:11px; font-size:10.5px; font-weight:600; padding:0 10px; }}"
                f"QPushButton:hover {{ border-color:{ACCENT}; color:{C['text']}; }}")

    def _set_model(self, k: str) -> None:
        self._model = k
        self._sync_model_panel()
        self._update_model_pill()
        self.model_changed.emit(self._model, self._effort)

    def _toggle_auto_effort(self) -> None:
        """Auto toggles: on -> off switches back to the slider's current level (so you can uncheck it)."""
        if self._effort == "":
            idx = self._effort_slider.value()
            self._set_effort(_EFFORT_ORDER[max(0, min(len(_EFFORT_ORDER) - 1, idx))])
        else:
            self._set_effort("")

    def _on_effort_slider(self, v: int) -> None:
        self._set_effort(_EFFORT_ORDER[max(0, min(len(_EFFORT_ORDER) - 1, v))])

    def _set_effort(self, k: str) -> None:
        self._effort = k
        self._sync_model_panel()
        self._update_model_pill()
        self.model_changed.emit(self._model, self._effort)

    def show_needs_you(self, tool: str, tool_input: dict | None = None) -> None:
        """A tool call is gated — the task is BLOCKED until Approve/Deny."""
        self.set_state("needs_you")
        self._set_status("Needs you", AMBER)
        self._needs_title.setText(f"Run  {tool}?")
        detail = ", ".join(f"{k}={str(v)[:80]}" for k, v in (tool_input or {}).items())
        self._needs_detail.setText(detail or "(no details)")
        self._stack.setCurrentWidget(self._page_needs)
        if not self._minimized:
            self._fit()

    def show_done(self, session_id: str = "", cost: float = 0.0, task: str = "", result: str = "",
                  stopped: bool = False) -> None:
        """The finished card. `stopped`: you pressed Stop — say so plainly, not the agent's last line +
        "Finished" (that read like it had completed); that line stays in Details."""
        self.set_state("done")
        self._stopped = stopped
        self._set_status("Stopped" if stopped else "Done", C["text_muted"] if stopped else GREEN)
        self._done_glyph.setText("◼" if stopped else "✓")
        self._done_glyph.setStyleSheet(
            f"color: {C['text_muted'] if stopped else GREEN}; font-size: {12 if stopped else 16}px; "
            f"font-weight: 700; {_SYM}background: transparent; border: none;")
        self._stop_elapsed()
        self._work_spinner.stop()
        if task:
            self._task_text = task
        if result:
            self._result_text = result
        sid = (session_id or "")[:8]
        meta = ("\u25A0 Stopped" if IS_WIN else "◼ Stopped") if stopped else "✓ Finished"
        if sid:
            meta += f"  ·  saved {sid}…"
        # No $ figure: on the Claude subscription you're not charged per task — a dollar amount would
        # falsely imply a bill. (The token value is still in the [asst] trace if we need it.)
        # Lead with what Claude GENERATED: its whole final reply, formatted, then the file the turn
        # produced, one click from opening. The step-by-step log stays in Details. Falls back to the
        # prompt if there's no text.
        reply = details_view.final_reply(self._entries) if self._rich and not stopped else ""
        body = reply or self._result_text or self._task_text
        parts = [details_view._md(body)] if body else []
        made = details_view.deliverable_html(self._entries) if self._rich else ""
        if made:
            parts.append(f"<p>{made}</p>")
        parts.append(f'<p style="color:{C["text_muted"]}; font-size:11.5px;">{html.escape(meta)}</p>')
        view = self._done_view
        view.setHtml("".join(parts))
        view.ensurePolished()                     # the 13px stylesheet font must apply before measuring
        doc = view.document()
        doc.setTextWidth(DONE_TEXT_W)
        view.setFixedHeight(max(24, min(DONE_MAX_H, int(doc.size().height()) + 2)))
        view.verticalScrollBar().setValue(0)
        self._stack.setCurrentWidget(self._page_done)
        if self._rich:
            self._render_details(follow=False)   # the answer is on the card now — not in Details too
        # Stays up showing the answer until you close it (present() pinned it).
        if not self._minimized:
            self._fit()

    def _size_setup_btn(self) -> None:
        """The stylesheet paints this button bold; Qt measured its regular-weight text — the first letter got
        clipped ("lose Codex and continue here"). Make room for the bold text + its padding."""
        from PySide6.QtGui import QFont, QFontMetrics
        f = QFont(self._setup_btn.font())
        f.setPixelSize(13)
        f.setWeight(QFont.Bold)
        self._setup_btn.setMinimumWidth(QFontMetrics(f).horizontalAdvance(self._setup_btn.text()) + 44)

    def _on_setup_btn(self) -> None:
        if self._setup_kind == "choice":
            self.codex_choice.emit("close", self._always_box.isChecked())
        else:
            self.setup_requested.emit(self._setup_agent, self._setup_kind)

    def show_codex_choice(self, msg: str, busy: str = "") -> None:
        """A chat the Codex app holds: close Codex and continue the SAME chat, or a new chat. `busy`: what
        the Codex app is working on right now (closing would stop it) — warned, and no "Always"."""
        self.set_state("error")
        self._setup_agent, self._setup_kind = "codex", "choice"
        self._set_status("Open in Codex", AMBER)
        self._stop_elapsed()
        self._work_spinner.stop()
        self._error_label.setText(msg + (f" Codex is working on “{busy}” right now — closing it will stop that."
                                         if busy else ""))
        self._setup_btn.setText("Close Codex anyway  →" if busy else "Close Codex and continue here  →")
        self._size_setup_btn()
        self._setup_btn.setVisible(True)
        self._choice_btn.setVisible(True)
        self._always_box.setChecked(False)
        self._always_box.setVisible(not busy)
        self._update_primary()
        self._stack.setCurrentWidget(self._page_error)
        if not self._minimized:
            self._fit()

    def show_error(self, msg: str) -> None:
        try:
            from app.core.agent_runner import setup_info
        except Exception:                        # the standalone self-check has no app package
            setup_info = lambda _m: None         # noqa: E731
        self._setup_agent, self._setup_kind = setup_info(msg) or ("claude", "")
        self.set_state("error")
        name = _SETUP_NAMES.get(self._setup_agent, "Claude Code")
        if self._setup_kind == "usage":          # out of usage: nothing to click — it says when it resets
            self._set_status("Out of usage", AMBER)
        elif self._setup_kind:                   # a setup step, not a failure: amber, with the fix
            self._set_status("Setup needed", AMBER)
            self._setup_btn.setText(f"Set up {name}  →" if self._setup_kind == "install"
                                    else f"Sign in to {name}  →")
            self._size_setup_btn()
        else:
            self._set_status("Error", RED)
        self._setup_btn.setVisible(self._setup_kind in ("install", "signin"))
        self._choice_btn.setVisible(False)
        self._always_box.setVisible(False)
        self._stop_elapsed()
        self._work_spinner.stop()
        self._done_timer.stop()
        self._error_label.setText((msg or "Something went wrong.")[:200])
        self._stack.setCurrentWidget(self._page_error)
        if not self._minimized:
            self._fit()

    def load_state(self, title: str, feed_lines=None, status: str = "working", action: str = "",
                   approval_req=None, result: str = "", cost: float = 0.0,
                   session_id: str = "", task: str = "", started_at: float = 0.0,
                   duration=None, entries=None) -> None:
        """Repaint the notch to show a DIFFERENT conversation's stored state — the left rail swapping
        focus between concurrent agents. Rebuilds the bubble + Details feed from the snapshot, then
        sets the status page. ponytail: reuses the existing show_* methods, no parallel render path.
        (_append_detail dedupes, so re-seeding the last action line is a no-op.)"""
        self._reset_details()
        self._stopping = False                     # another chat's state (the page re-marks it if it's stopping)
        title = (title or "").strip()
        self._set_bubble(task or title, is_prompt=bool(task))
        self._set_task_title(title if task else "")
        self._transcript.setVisible(bool(self._transcript.text()) and not self._minimized)
        if entries:
            self.set_details(entries)              # rich Details (turns + actions with substance)
        else:
            for ln in (feed_lines or []):
                self._append_detail(ln)
        if status == "needs_you":
            tool = getattr(approval_req, "tool", "") if approval_req else ""
            tin = getattr(approval_req, "tool_input", None) if approval_req else None
            self.show_needs_you(tool, tin)
        elif status == "done":
            self.show_done(session_id=session_id, cost=cost, task=task, result=result or "")
        elif status == "stopped":
            self.show_done(task=task, result="Stopped by you.", stopped=True)
        elif status == "error":
            self.show_error(result or "Something went wrong.")
        else:
            self.show_working(action or "Working…", "")
        # The header timer: present() restarted it at 0:00, which made every re-focused finished chat
        # read "Done · 0:00". Live → count from when this turn started; finished → its real duration,
        # or nothing if unknown.
        import time
        if status in ("working", "needs_you") and started_at:
            self._task_started = time.monotonic() - max(0.0, time.time() - started_at)
            self._tick_elapsed()
        else:
            self._stop_elapsed()
            s = int(duration) if duration else None
            self._timer_label.setText(f"{s // 60}:{s % 60:02d}" if s is not None else "")

    def set_state(self, state: str) -> None:
        if state != "idle" and self._hover_bar_on:
            self._hide_hover_bar()            # a task started (e.g. by voice) while hovering
        if state != "working":
            self._stopping = False            # the stop finished (done / error / idle)
        self._state = state
        self._handle.set_state(state)
        self._update_primary()

    # -- primary action: Stop while working / Open in Claude when done ----------
    def _on_primary(self) -> None:
        if self._state == "working":
            self.stop_clicked.emit()
        else:
            self.open_in.emit(self._open_where.get(self._agent, "app"))

    def hide_primary(self) -> None:
        """No "Open in …" / Stop for this card (a request stopped before it became a chat)."""
        self._primary_btn.setVisible(False)
        self._open_more.setVisible(False)
        if self._open_open:
            self._set_open_panel(False)

    def mark_stopping(self) -> None:
        """Called the instant Stop is clicked: disable the button + show it registered, so there's
        no reason to click it twice. It stays that way until the turn ends (see `_stopping`)."""
        self._stopping = True
        self._set_status("Stopping…", C["text_muted"])
        self._primary_btn.setVisible(True)
        self._primary_btn.setText("Stopping…")
        self._primary_btn.setEnabled(False)
        self._primary_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {C['text_muted']}; "
            f"border: 1px solid {C['border']}; border-radius: 8px; font-size: 12px; font-weight: 600; padding: 0 12px; }}")

    def _update_primary(self) -> None:
        if self._state == "working" and self._stopping:
            self.mark_stopping()                    # a late step while stopping: keep "Stopping…", disabled
            self._open_more.setVisible(False)
            return
        self._primary_btn.setEnabled(True)          # re-enable after a stop/state change
        if self._state == "working":
            self._primary_btn.setText(("\u25A0" if IS_WIN else "◼") + "  Stop")
            self._primary_btn.setVisible(True)
            self._primary_btn.setStyleSheet(
                f"QPushButton {{ background: {C['surface']}; color: {RED}; border: none; "
                "border-radius: 13px; font-size: 12px; font-weight: 600; padding: 0 14px; }"
                "QPushButton:hover { background: rgba(242,100,90,0.16); }")
        elif self._state == "error" and self._setup_kind:
            self._primary_btn.setVisible(False)  # nothing to open yet — the setup button is the action
        elif self._state in ("done", "error"):
            name = _AGENT_NAMES.get(self._agent, "Claude")
            in_terminal = self._open_where.get(self._agent, "app") == "terminal"
            self._primary_btn.setText(f"Open in {'Terminal' if in_terminal else name}  →")
            self._primary_btn.setVisible(True)
            self._primary_btn.setStyleSheet(       # left half of a split button; the ⌄ is the right half
                f"QPushButton {{ background: {C['surface']}; color: {C['text']}; border: none; "
                "border-top-left-radius: 13px; border-bottom-left-radius: 13px; "
                "border-top-right-radius: 0; border-bottom-right-radius: 0; "
                "font-size: 12px; font-weight: 600; padding: 0 12px 0 14px; }"
                f"QPushButton:hover {{ background: {ACCENT}; color: #0B0B0E; }}")
            self._open_alt.setText(f"Open in {name} app" if in_terminal else "Open in Terminal")
            # Qt sizes the button with the regular-weight font but paints it semibold (~4px wider), which
            # clipped "Open in Terminal →" at both ends — reserve the painted width (+ the 26px padding).
            f = QFont(self._primary_btn.font())
            f.setPixelSize(12)
            f.setWeight(QFont.Weight.DemiBold)
            self._primary_btn.setMinimumWidth(QFontMetrics(f).horizontalAdvance(self._primary_btn.text()) + 30)
            self._open_more.setVisible(True)
            return
        else:                                   # needs_you — Approve/Deny live in the page
            self._primary_btn.setVisible(False)
        self._open_more.setVisible(False)          # only beside "Open in …"
        if self._open_open:
            self._set_open_panel(False)

    # -- header status + elapsed timer -----------------------------------------
    def _set_status(self, text: str, color: str) -> None:
        self._status_label.setText(text)
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 600; background: transparent; border: none;")

    def _start_elapsed(self) -> None:
        import time
        self._task_started = time.monotonic()
        self._tick_elapsed()
        if not self._elapsed_timer.isActive():
            self._elapsed_timer.start()

    def _stop_elapsed(self) -> None:
        self._elapsed_timer.stop()      # keep the final time shown

    def _tick_elapsed(self) -> None:
        if self._task_started is None:
            self._timer_label.setText("")
            return
        import time
        s = int(max(0, time.monotonic() - self._task_started))
        self._timer_label.setText(f"{s // 60}:{s % 60:02d}")

    def flash(self, ms: int = FLASH_MS) -> None:
        """Briefly drop the panel open — a 'receipt' that a (voice) task started, or that one is
        already running — then tuck back to the invisible handle unless you hovered or pinned it."""
        self._collapse_timer.stop()
        if self._minimized:
            self.restore_panel()
        self._flash_timer.start(ms)

    def _end_flash(self) -> None:
        if self._pinned:
            return
        try:
            if self.frameGeometry().contains(QCursor.pos()):
                self._flash_timer.start(700)      # still hovering — keep it open
                return
        except Exception:
            pass
        if not self._minimized:
            self.minimize_panel()

    def dismiss(self) -> None:
        """Slide up and hide."""
        self._done_timer.stop()
        self._flash_timer.stop()
        self._collapse_timer.stop()
        self._stop_anim()
        self._work_spinner.stop()
        geo = notch_panel.screen_geo()
        if geo is None or not self.isVisible():
            self.hide()
            return
        self._slide_to(self.x(), geo.y() - self.height(), on_done=self.hide)

    # -- hover / expand / collapse --------------------------------------------
    def enterEvent(self, e) -> None:
        # Resting: hovering the notch reveals a visible tab below it (a click target). The cursor poll is
        # the source of truth; enter/leave only trigger an immediate check so it's instant when macOS
        # does deliver them. A CLICK opens the full card.
        self._collapse_timer.stop()
        if self._minimized:
            self._poll_hover()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        if self._minimized:
            self._poll_hover()                    # still "inside" if the cursor just crossed onto a child
        elif not self._pinned:
            self._collapse_timer.start(400)       # expanded: collapse shortly after you leave
        super().leaveEvent(e)

    def _poll_hover(self) -> None:
        """Resting-hover source of truth: is the global cursor anywhere over the notch strip, including
        the notch band itself? Enter/leave alone can't do this — macOS doesn't reliably deliver them to
        a non-activating accessory panel, and Qt sends the parent a leave when the cursor crosses onto
        a child. QCursor.pos() works whether or not Filect is the active app.
        ponytail: 60ms poll while resting; swap for an NSEvent mouse-moved monitor if CPU ever matters."""
        if not self._minimized or not self.isVisible():
            return
        g, pos = self.frameGeometry(), QCursor.pos()
        inside = g.contains(pos)
        if IS_WIN:
            self._check_fullscreen()
        if IS_WIN and inside and not self._hover_in and not self._hover_bar_on:
            # Windows (§8r): at rest only the black pill shows; the rest of the strip sits over whatever is at the
            # top centre (a maximized browser's tabs) — so only the pill (the screen's top edge) wakes it; once the
            # tab / bar shows, the visible part takes the clicks. Not over a fullscreen app (no pill there).
            inside = pos.y() - g.top() < WIN_HOT_H and not self._handle._pill_hidden
        if inside != self._hover_in:
            self._hover_in = inside
            logger.info("[notch] hover %s", "in" if inside else "out")
            if self._state != "idle":
                self._handle.reveal(inside)       # a task is live: the status pill, as before
            elif inside:
                self._bar_grace.stop()
                if not self._hover_bar_on:
                    self._dwell_timer.start()     # idle: grow into the VoiceOS-style bar
            else:
                self._dwell_timer.stop()
                if self._hover_bar_on:            # clicked into the input → give it longer
                    self._bar_grace.start(4000 if self._hover_bar.field_clicked else HOVER_GRACE_MS)
        self._win_hit()

    def _check_fullscreen(self) -> None:
        """Windows, twice a second at most: hide the resting pill while a fullscreen app is in front."""
        now = time.monotonic()
        if now - getattr(self, "_fs_at", 0.0) < 0.5:
            return
        self._fs_at = now
        fs = notch_panel.fullscreen_app_in_front() if hasattr(notch_panel, "fullscreen_app_in_front") else False
        self._handle.set_pill_hidden(fs and self._state == "idle")

    def _win_hit(self) -> None:
        """Windows: the resting strip lets clicks through to the app beneath unless it's showing something
        (you're on it, its idle bar is up, or the card is open)."""
        if not IS_WIN:
            return
        through = bool(self._minimized and not self._hover_in and not self._hover_bar_on)
        if through != getattr(self, "_win_click_through", None):
            notch_panel.set_click_through(self, through)

    # -- idle hover bar (VoiceOS-style) ------------------------------------------
    def _hover_w(self) -> int:
        nw = notch_panel.notch_width() or (WIN_NOTCH_W if IS_WIN else STRIP_W)
        return max(330, min(420, int(nw * 2.1)))

    def _show_hover_bar(self) -> None:
        if (self._state != "idle" or not self._minimized or not self.isVisible()
                or not self._hover_in or self._hover_bar_on):
            return
        self._hover_bar_on = True
        nb = notch_panel.notch_inset_top() or (WIN_HOVER_BAND if IS_WIN else 0)
        w, h = self._hover_w(), nb + HOVER_BODY_H
        self._handle.setVisible(False)
        self.setFixedWidth(w)
        self.setMinimumHeight(0)
        self.resize(w, h)
        self._hover_bar.setGeometry(0, 0, w, h)
        self._hover_bar.place(nb)
        self._hover_bar.show()
        self._hover_bar.raise_()
        self._reanchor()
        self._reassert()
        self._win_hit()
        logger.info("[notch] hover bar shown")

    def _maybe_hide_hover_bar(self) -> None:
        if self._hover_in or self._hover_bar.field.text():
            return                                # back on it, or mid-typing: keep it
        self._hide_hover_bar()

    def _hide_hover_bar(self, resize: bool = True) -> None:
        self._dwell_timer.stop()
        self._bar_grace.stop()
        if not self._hover_bar_on:
            return
        self._hover_bar_on = False
        self._hover_bar.hide()
        self._hover_bar.reset()
        self._handle.setVisible(self._minimized)
        if resize and self._minimized:            # back to the resting notch-sized strip
            sw = self._strip_w()
            self.setFixedWidth(sw)
            self.setMinimumHeight(0)
            self.resize(sw, self._min_h())
            self._reanchor()
            self._reassert()
        self._end_typing()
        self._win_hit()
        logger.info("[notch] hover bar hidden")

    def _on_hover_submit(self, text: str) -> None:
        self._hide_hover_bar()
        self._end_typing()
        self.submit_text.emit(text)               # same as the card's type field → run_voice_task

    def mousePressEvent(self, e) -> None:
        # Resting hotspot click → open (and pin). Click on the OPEN card's body → close it again
        # (toggle), so a second click on the notch tucks it away. An approval (needs_you) is kept so
        # you don't dismiss it by accident — use Approve/Deny or ✕. Clicks on the input/buttons are
        # consumed by them first, so this only fires on the card body / top bar.
        if self._minimized:
            self._on_handle_clicked()
            e.accept()
            return
        if self._state != "needs_you":
            self._pinned = False
            self.minimize_panel()
            e.accept()
            return
        super().mousePressEvent(e)

    def _maybe_collapse(self) -> None:
        if self._pinned or self._minimized:
            return
        # Don't tuck away mid-interaction: while you're typing a task, or while an approval is pending.
        if self._type_field.hasFocus() or self._state == "needs_you":
            self._collapse_timer.start(500)
            return
        try:
            if self.frameGeometry().contains(QCursor.pos()):
                # Still inside (likely over a child widget — Qt leaveEvent gotcha). Re-check soon.
                self._collapse_timer.start(300)
                return
        except Exception:
            pass
        self.minimize_panel()

    def _on_handle_clicked(self) -> None:
        # Clicking the hidden notch hotspot opens the notch ONLY (and pins it so it stays while you
        # read/decide). The left rail is independent — it opens on its own click.
        self.open_full()

    def open_full(self) -> None:
        """Expand the card and pin it (the notch's own click action)."""
        self._pinned = True
        if self._minimized:
            self.restore_panel()

    def restore_panel(self) -> None:
        """Expand from the handle to the full card."""
        self._minimized = False
        self._apply_minimized_chrome(False)
        self.setFixedWidth(WIDGET_W)
        self._fit()
        self._reanchor()
        self._reassert()

    def minimize_panel(self) -> None:
        """Collapse the card back to the resting handle."""
        if self._minimized:
            return
        self._minimized = True
        self._type_field.clearFocus()
        self._end_typing()                       # Windows: tucked = the keyboard goes back to your app
        self._apply_minimized_chrome(True)
        sw = self._strip_w()
        self.setFixedWidth(sw)
        self.setMinimumHeight(0)
        self.resize(sw, self._min_h())
        self._reanchor()
        self._reassert()
        self.collapsed.emit()

    def _apply_minimized_chrome(self, minimized: bool) -> None:
        """Show just the handle (minimized) or the full card (expanded). When minimized we also
        collapse the margins so the resting window is ~20px tall — a tiny transparent strip that
        hugs the notch, instead of a tall invisible box that would eat clicks at top-centre."""
        if getattr(self, "_hover_bar_on", False):
            self._hide_hover_bar(resize=False)    # the caller sizes the window for the new state
        self._topbar.setVisible(not minimized)
        self._stack.setVisible(not minimized)
        self._ctrl_row.setVisible(not minimized)
        self._footer.setVisible(False)            # the hint lives in the input now
        self._type_field.setVisible(not minimized)
        self._send_btn.setVisible(not minimized)
        self._transcript.setVisible((not minimized) and bool(self._transcript.text()))
        self._title_label.setVisible((not minimized) and bool(self._title_label.text()))
        self._details_feed.setVisible((not minimized) and self._details_open)
        if minimized:
            self._model_open = False
            self._model_list_open = False
            self._model_list.setVisible(False)
        self._model_panel.setVisible((not minimized) and self._model_open)
        self._handle.setVisible(minimized)
        col = self._card.layout()
        eff = self._card.graphicsEffect()
        nb = notch_panel.notch_inset_top()      # the notch/menu-bar band the card top sits behind
        if minimized:
            # Full-width, invisible hit strip spanning the WHOLE notch (0 side margins) so the entire
            # notch is hoverable/clickable — not a narrow sliver. The col top margin = the notch band,
            # so the hover peek hangs just below the notch's bottom edge.
            self.layout().setContentsMargins(0, 0, 0, 2)
            col.setContentsMargins(16, nb, 16, 1)
            self._card.setStyleSheet(self._nub_style())
            if eff is not None:
                eff.setEnabled(False)
            self._hover_in = False                # resting: watch the cursor for the hover tab
            self._handle.reveal(False)
            self._hover_poll.start()
            self._win_hit()
        else:
            self._hover_poll.stop()               # open card: normal enter/leave handles collapse
            self._hover_in = False
            self._win_hit()
            self.layout().setContentsMargins(SHADOW_MARGIN, 0, SHADOW_MARGIN, SHADOW_MARGIN)
            # the header row sits IN the notch band (beside the notch); the rest starts just below it
            col.setContentsMargins(16, max(4, (nb - TOPBAR_H) // 2), 16, 12)
            self._card.setStyleSheet(self._card_style)
            if eff is not None:
                eff.setEnabled(False)

    def _nub_style(self) -> str:
        # At rest the nub is visually NOTHING, but it must stay a CLICK TARGET. A non-activating
        # translucent panel only receives mouse events where it has painted pixels — a fully
        # transparent nub is click-through, which is why the idle notch couldn't be opened until a
        # task had painted it once. An alpha≈2/255 fill is imperceptible yet gives the whole
        # notch-wide strip a hit region, so hover and click work at idle too.
        return "QFrame#claudeCard { background-color: rgba(0,0,0,0.008); border: none; }"

    # -- sizing / anchoring ----------------------------------------------------
    def _content_height(self) -> int:
        if self._state == "done":                 # the answer's measured height (see show_done)
            return self._done_view.maximumHeight() + 4
        if self._state == "error" and self._setup_kind == "choice":
            lab = max(22, self._error_label.heightForWidth(CARD_W - 32 - 26))
            box = (8 + self._always_box.sizeHint().height()) if not self._always_box.isHidden() else 0
            return lab + 12 + 32 + box + 10
        if self._state == "error" and self._setup_kind in ("install", "signin"):
            return _STACK_H["error"] + 46          # + the setup button
        return _STACK_H.get(self._state, 90)

    def _fit(self) -> None:
        col = self._card.layout()
        cm = col.contentsMargins()
        om = self.layout().contentsMargins()
        sp = col.spacing()
        h = om.top() + cm.top()
        if self._transcript.text():
            h += TRANSCRIPT_H + sp            # the "what you said" bubble
        if self._title_label.text():
            h += TITLE_H + sp                 # the task's short bold name
        self._stack.setFixedHeight(self._content_height())   # pin it so the page doesn't expand
        h += TOPBAR_H + sp + self._content_height() + sp + CTRL_H
        if self._model_open:
            h += sp + MODELPANEL_H            # the inline model & effort panel
            if not self._update_btn.isHidden():
                h += 28 + 6                   # the "Update Codex" row
            if self._model_list_open:         # model dropdown list expanded (the selected one is hidden)
                h += MODEL_ROW_H * max(0, len(self._model_rows) - 1) + 3
        if self._open_open:
            h += sp + OPENPANEL_H             # the ⌄ menu
        if self._details_open:
            h += sp + (DETAILS_H - 6)         # the expanded live feed
        h += sp + TYPEFIELD_H + cm.bottom() + om.bottom()
        col.invalidate()
        col.activate()
        self.layout().invalidate()
        self.layout().activate()
        self.setMinimumHeight(0)
        self.resize(self.width(), max(60, int(h)))
        self._reassert()

    def _rest_y(self, geo) -> int:
        # Anchor the window AT the top edge so the card's black top band merges with the physical
        # notch (VoiceOS-style: it looks like it lives in/drops from the notch). Content is pushed
        # below the notch via the card's top margin (= the notch band), so nothing hides behind it.
        return geo.y() + TOP_MARGIN

    def _min_h(self) -> int:
        # Minimized window height: the notch band + the (invisible) hover-tab strip. Kept permanent so
        # the hover tab has room to paint WITHOUT a resize (the resize-on-hover broke the tab).
        return notch_panel.notch_inset_top() + PEEK_H

    def _strip_w(self) -> int:
        # The resting hotspot spans the WHOLE physical notch so the entire notch is hoverable/clickable
        # (not a 1-2px sliver). +6 for a comfortable edge; falls back to STRIP_W on non-notch displays.
        nw = notch_panel.notch_width()
        return max(STRIP_W, nw + 6) if nw else STRIP_W

    def _reanchor(self) -> None:
        # Cancel any in-flight present() slide first — it ends at the pill-width centre and would
        # otherwise override this move, leaving an expanded card off-centre (the "searching card
        # not centred" bug). Use the exact width for the state so centring is deterministic.
        self._stop_anim()
        if not self.isVisible():
            return
        geo = notch_panel.screen_geo()
        if geo is None:
            return
        if self._hover_bar_on:
            w = self._hover_w()
        else:
            w = self._strip_w() if self._minimized else WIDGET_W
        x = geo.x() + (geo.width() - w) // 2
        self.move(x, self._rest_y(geo))
        self._place_native()

    def _place_native(self) -> None:
        """Make the real (native) window sit where Qt thinks it is — at the very top, over the menu
        bar, so the hover area and the card cover the physical notch (see notch_panel.place_native)."""
        g = self.frameGeometry()
        notch_panel.place_native(WIN_TITLE, g.x(), g.y(), g.width(), g.height())

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._reassert()

    def _reassert(self) -> None:
        notch_panel.reassert_panel_style(WIN_TITLE)

    def _slide_to(self, x: int, y: int, on_done=None) -> None:
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(170)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setEndValue(QPointF(x, y).toPoint())
        anim.finished.connect(self._place_native)
        if on_done is not None:
            anim.finished.connect(on_done)
        self._anim = anim
        anim.start()

    def _stop_anim(self) -> None:
        if self._anim is not None:
            try:
                self._anim.stop()
            except Exception:
                pass

    def _end_typing(self) -> None:
        """Windows: back to never taking focus from the app you're in (after you've typed)."""
        if IS_WIN and getattr(self, "_win_typing", False):
            notch_panel.allow_typing(self, False)

    def _configure_macos_deferred(self) -> None:
        if IS_WIN:
            notch_panel.configure_panel(self)               # non-activating, topmost, out of screenshots
            QTimer.singleShot(10, lambda: notch_panel.configure_panel(self))
            QTimer.singleShot(60, lambda: notch_panel.configure_panel(self))
            return
        if sys.platform != "darwin":
            return
        def configure():
            notch_panel.configure_macos_panel(WIN_TITLE)
            self._place_native()          # level is high now → the window may sit over the menu bar
        configure()
        QTimer.singleShot(10, configure)
        QTimer.singleShot(60, configure)

    # -- decisions / dismiss ---------------------------------------------------
    def _on_approve(self) -> None:
        self.approve_clicked.emit()
        self.show_working("Approved — continuing…")

    def _on_deny(self) -> None:
        self.deny_clicked.emit()
        self.show_working("Denied.")

    def _on_dismiss(self) -> None:
        # ✕ closes the card back to the resting strip (hover → the input bar) — also mid-task: the chat keeps
        # running in the drawer. The page lets go of the chat first (dismissed), so nothing re-opens it.
        self.dismissed.emit()
        self.rest_idle(force=True)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            if self._hover_bar_on:                # Esc in the hover bar just tucks it away
                self._hide_hover_bar()
                return
            self._on_dismiss()
            return
        super().keyPressEvent(event)


# --- headless self-check -------------------------------------------------------
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    ov = ClaudeOverlay()

    fired = {"approve": 0, "deny": 0, "open": 0, "dismiss": 0}
    ov.approve_clicked.connect(lambda: fired.__setitem__("approve", fired["approve"] + 1))
    ov.deny_clicked.connect(lambda: fired.__setitem__("deny", fired["deny"] + 1))
    ov.open_in.connect(lambda w: fired.__setitem__("open", fired["open"] + 1))
    ov.dismissed.connect(lambda: fired.__setitem__("dismiss", fired["dismiss"] + 1))

    ov.present()
    assert ov._minimized is True, "should rest minimized, not expand on present"
    assert ov._state == "working"

    # Drive every state; handle colour tracks state; content pages switch.
    ov.set_title("Build a site")
    ov.show_working("Reading files…", tool="Read")
    assert ov._stack.currentWidget() is ov._page_working
    assert ov._handle._state == "working"
    assert ov._handle._shows_at_rest(), "working must show at rest — no dead zone while a task runs"
    # Collapsible live "Details" feed: toggling it expands the card and accumulates activity.
    ov._toggle_details()   # isHidden() reflects the explicit setVisible flag; isVisible() needs
    assert ov._details_open and not ov._details_feed.isHidden()   # shown ancestors (false offscreen)
    assert "Reading files" in ov._details_feed.toPlainText(), "Details feed must accumulate actions"
    ov._toggle_details()
    assert not ov._details_open and ov._details_feed.isHidden()
    # Model + effort picker: setting updates the pill label and emits model_changed.
    picked = {}
    ov.model_changed.connect(lambda m, e: picked.update(model=m, effort=e))
    ov.set_model_effort("opus", "high")
    assert "Opus" in ov._model_btn.text() and "High" in ov._model_btn.text(), ov._model_btn.text()
    ov._set_model("sonnet")
    assert picked.get("model") == "sonnet" and "Sonnet" in ov._model_btn.text()
    ov._set_effort("max")
    assert picked.get("effort") == "max" and "Max" in ov._model_btn.text()

    ov.show_needs_you("Bash", {"command": "git push origin main", "timeout": 120000})
    assert ov._stack.currentWidget() is ov._page_needs
    assert ov._state == "needs_you" and ov._handle._state == "needs_you"

    # Hover expands; leaving schedules collapse; pin keeps it open.
    ov.restore_panel()
    assert ov._minimized is False
    ov.minimize_panel()
    assert ov._minimized is True
    ov._on_handle_clicked()
    assert ov._pinned is True and ov._minimized is False

    # Approve resolves via signal and flips back to working.
    ov._on_approve()
    assert fired["approve"] == 1
    assert ov._stack.currentWidget() is ov._page_working

    ov.show_needs_you("Write", {"file_path": "/tmp/x.txt"})
    ov._on_deny()
    assert fired["deny"] == 1

    # A task keeps the card open until the user closes it (2026-11-05): present() pins.
    ov._pinned = False
    ov.present()
    assert ov._pinned is True, "a task must keep the card open until you close it"
    ov.restore_panel()                      # what a hover does
    ov.show_done("cc7618c9abcd", 0.1234, task="Build a site",
                 result="Subject: Partnership — Here is a short intro email you can send to brands.")
    assert ov._stack.currentWidget() is ov._page_done
    assert not ov._done_timer.isActive(), "done must NOT auto-hide — it stays reopenable"
    assert ov._handle._state == "done"
    assert ov._handle._shows_at_rest(), "done nub visible at rest (the status sliver / hover target)"
    assert "intro email" in ov._done_view.toPlainText(), "done card must show what Claude GENERATED"
    # Click the nub still pins it open (watch-this-task mode).
    ov.minimize_panel()
    assert ov._minimized is True
    ov._on_handle_clicked()
    assert ov._minimized is False and ov._pinned is True and ov._stack.currentWidget() is ov._page_done

    ov.show_error("Couldn't reach the agent.")
    assert ov._stack.currentWidget() is ov._page_error
    assert ov._handle._state == "error"

    # open + dismiss signals.
    ov.open_in.emit("app")
    assert fired["open"] == 1
    ov._on_dismiss()
    assert fired["dismiss"] == 1

    # Idempotent / no-raise edge calls.
    ov.minimize_panel()
    ov.minimize_panel()
    ov.show_needs_you("Edit", None)       # None tool_input must not raise
    ov.show_working()                      # empty text/tool must not raise

    # Transcript bubble + primary action (Stop while working / Open in Claude when done).
    ov.present()
    ov.set_title("Make me a landing page for a coffee brand")
    assert "landing page" in ov._transcript.text(), "transcript bubble shows what you said"
    assert "Stop" in ov._primary_btn.text(), "primary is Stop while working"
    stops = []
    ov.stop_clicked.connect(lambda: stops.append(1))
    ov._on_primary()
    assert len(stops) == 1, "Stop fires stop_clicked"
    _o = fired["open"]
    ov.show_done("zz99", 0.0, result="built the landing page")
    assert "Open in Claude" in ov._primary_btn.text(), "primary becomes Open in Claude when done"
    ov._on_primary()
    assert fired["open"] == _o + 1, "primary on done opens Claude"

    ov.show_done("zz99", 0.0, result="Stopped by you.", stopped=True)
    assert ov._status_label.text() == "Stopped" and "Stopped by you" in ov._done_view.toPlainText()
    assert "Finished" not in ov._done_view.toPlainText(), "a Stop doesn't read as finished"

    # A Codex chat: its models in the picker, "Open in Codex"; back to Claude restores Claude's.
    ov.set_agent("codex", [("gpt-a", "GPT-A"), ("gpt-b", "GPT-B")])
    ov.set_model_effort("", "high")
    assert "GPT-A" in ov._model_btn.text() and set(ov._model_rows) == {"gpt-a", "gpt-b"}
    assert "Open in Codex" in ov._primary_btn.text()
    ov._set_model("gpt-b")
    assert picked.get("model") == "gpt-b"
    ov.set_agent("claude")
    ov.set_model_effort("sonnet", "high")
    assert "Sonnet" in ov._model_btn.text() and "Open in Claude" in ov._primary_btn.text()

    print("claude_overlay self-check OK")
