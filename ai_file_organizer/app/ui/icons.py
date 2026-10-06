"""Thin line-icons drawn with QPainter (no QtSvg / no bundled assets, so they
render identically in dev and in the frozen app). Ported from the Mac pack.

Also contains AnimatedWaveform — the voice motif shown while dictating. The
Mac version is a phase-only idle shimmer; this Windows version is
voice-REACTIVE: call set_level(0..1) from the recorder's `level` signal and the
bars grow with your speaking volume (like Wispr Flow), with a smooth decay.
"""
import math
from PySide6.QtCore import Qt, QRectF, QLineF, QTimer, QPointF
from PySide6.QtGui import (QPixmap, QPainter, QPen, QColor, QPainterPath, QIcon,
                           QLinearGradient, QBrush)
from PySide6.QtWidgets import QWidget

ACCENT = "#7C4DFF"
ACCENT_LIGHT = "#B39DFF"   # top of the waveform gradient (Mac)


# --- individual icon drawers (24x24 logical coordinate box) ---------------
def _search(p):
    p.drawEllipse(QRectF(4, 4, 12, 12)); p.drawLine(QLineF(14.6, 14.6, 20, 20))

def _folder(p):
    path = QPainterPath()
    path.moveTo(3, 18.5); path.lineTo(3, 7); path.lineTo(9, 7)
    path.lineTo(11, 9); path.lineTo(21, 9); path.lineTo(21, 18.5); path.closeSubpath()
    p.drawPath(path)

def _layers(p):
    p.drawRoundedRect(QRectF(4, 3, 16, 18), 2, 2)
    p.drawLine(QLineF(7.5, 8, 16.5, 8)); p.drawLine(QLineF(7.5, 12, 16.5, 12))
    p.drawLine(QLineF(7.5, 16, 13, 16))

def _mic(p):
    p.drawRoundedRect(QRectF(9, 3, 6, 11), 3, 3)
    p.drawArc(QRectF(5, 4, 14, 14), 180 * 16, 180 * 16)
    p.drawLine(QLineF(12, 18, 12, 21.2)); p.drawLine(QLineF(8.5, 21.2, 15.5, 21.2))

def _gear(p):
    p.drawEllipse(QRectF(8.7, 8.7, 6.6, 6.6))
    for i in range(8):
        a = math.radians(i * 45); c, s = math.cos(a), math.sin(a)
        p.drawLine(QLineF(12 + 5 * c, 12 + 5 * s, 12 + 8 * c, 12 + 8 * s))

def _sparkle(p):
    path = QPainterPath()
    path.moveTo(12, 3); path.lineTo(13.6, 10.4); path.lineTo(21, 12)
    path.lineTo(13.6, 13.6); path.lineTo(12, 21); path.lineTo(10.4, 13.6)
    path.lineTo(3, 12); path.lineTo(10.4, 10.4); path.closeSubpath()
    p.drawPath(path)

def _globe(p):
    p.drawEllipse(QRectF(3, 3, 18, 18)); p.drawLine(QLineF(3, 12, 21, 12))
    p.drawEllipse(QRectF(8, 3, 8, 18))

def _mute(p):
    sp = QPainterPath()
    sp.moveTo(4, 9.5); sp.lineTo(7, 9.5); sp.lineTo(11, 6)
    sp.lineTo(11, 18); sp.lineTo(7, 14.5); sp.lineTo(4, 14.5); sp.closeSubpath()
    p.drawPath(sp)
    p.drawLine(QLineF(15, 9, 20, 15)); p.drawLine(QLineF(20, 9, 15, 15))

def _clock(p):
    p.drawEllipse(QRectF(3, 3, 18, 18)); p.drawLine(QLineF(12, 7.5, 12, 12))
    p.drawLine(QLineF(12, 12, 15.5, 14))

def _type(p):
    p.drawLine(QLineF(6, 6.5, 18, 6.5)); p.drawLine(QLineF(12, 6.5, 12, 18))
    p.drawLine(QLineF(9.5, 18, 14.5, 18))

def _waveform(p):
    for x, h in ((5, 9), (8.5, 15), (12, 20), (15.5, 13), (19, 7)):
        top = 12 - h / 2.0
        p.drawLine(QLineF(x, top, x, top + h))

def _x(p):
    p.drawLine(QLineF(6, 6, 18, 18)); p.drawLine(QLineF(18, 6, 6, 18))


_DRAW = {
    "search": _search, "folder": _folder, "layers": _layers, "mic": _mic,
    "gear": _gear, "sparkle": _sparkle, "globe": _globe, "mute": _mute,
    "clock": _clock, "type": _type, "waveform": _waveform, "x": _x,
}

_cache = {}


def line_pixmap(name: str, size: int = 16, color: str = ACCENT) -> QPixmap:
    key = (name, size, color)
    if key in _cache:
        return _cache[key]
    drawer = _DRAW.get(name)
    ratio = 2
    px = max(2, int(round(size * ratio)))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    if drawer is not None:
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.scale(px / 24.0, px / 24.0)
        pen = QPen(QColor(color)); pen.setWidthF(2.0)
        pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        try:
            drawer(p)
        finally:
            p.end()
    pm.setDevicePixelRatio(ratio)
    _cache[key] = pm
    return pm


def line_icon(name: str, size: int = 18, on_color: str = ACCENT,
              off_color: str = None) -> QIcon:
    icon = QIcon()
    off = off_color or on_color
    icon.addPixmap(line_pixmap(name, size, off), QIcon.Normal, QIcon.Off)
    icon.addPixmap(line_pixmap(name, size, on_color), QIcon.Normal, QIcon.On)
    return icon


class AnimatedWaveform(QWidget):
    """Voice-reactive waveform — exact port of the macOS `dictation_overlay.py`
    `_paint_bars` (see the Filect dictation-animation reference spec):

      phase += 0.16                           # 16 ms timer (~60 fps)
      level += (level_target - level)·0.25    # ease the live mic level
      center = (N-1)/2
      dist = |i-center|/center                # 0 middle → 1 edges
      bell = 1 - dist²·0.55                    # centre bars taller
      wave = 0.5 + 0.5·sin(phase + i·0.7)      # travelling ripple
      idle = 0.10·(0.5 + 0.5·sin(phase·0.6 + i·0.9))  # bob at silence
      target = clamp(0.12 + level·bell·wave + idle, 0, 1)
      heights[i] += (target - heights[i])·0.25
      h_px = BAR_MIN_H + heights[i]·(BAR_MAX_H - BAR_MIN_H)

    Bars are FIXED width/gap, centred horizontally, each centred vertically;
    vertical gradient #B39DFF → #7C4DFF, round caps. Three things that must stay
    exact: the bell weighting, easing BOTH level and heights at 0.25 (never snap),
    and the idle bob so it never goes fully flat."""

    EASE = 0.25

    def __init__(self, parent=None, color: str = ACCENT, bars: int = 9,
                 width: int = 180, height: int = 48, interval: int = 16,
                 bar_width: float = 5.0, bar_gap: float = 8.0,
                 min_h: float = 6.0, max_h: float = 40.0, wave_amp: float = 0.34,
                 level_gain: float = 0.4):
        super().__init__(parent)
        self._c_top = QColor(ACCENT_LIGHT)
        self._c_bot = QColor(color)
        self._n = max(3, bars)
        self._bw = bar_width
        self._gap = bar_gap
        self._min_h = min_h
        self._max_h = max_h
        # How much each bar oscillates. Spec is 0.5 (wave = 0.5 + 0.5·sin);
        # lowered so the bars breathe instead of launching up and down.
        self._wave_amp = max(0.0, min(0.5, wave_amp))
        # How strongly voice VOLUME raises the bars. Spec is 1.0; lowered so
        # speaking louder lifts the bars only a little instead of shooting up.
        self._level_gain = max(0.0, min(1.0, level_gain))
        self._phase = 0.0
        self._level = 0.0
        self._level_target = 0.0
        self._vals = [0.0] * self._n
        self._interval = interval
        self.setMinimumSize(width, height)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent; border: none;")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set_level(self, level: float):
        """Feed the recorder's 0..1 RMS. This sets the TARGET; the bars ease
        toward it each frame (do not snap) — that's what makes it fluid."""
        self._level_target = max(0.0, min(1.0, float(level)))

    def _tick(self):
        self._phase += 0.16
        self._level += (self._level_target - self._level) * self.EASE
        n = self._n
        center = (n - 1) / 2.0 if n > 1 else 0.5
        for i in range(n):
            dist = abs(i - center) / center if center else 0.0
            bell = 1.0 - dist * dist * 0.55
            wave = (1.0 - self._wave_amp) + self._wave_amp * math.sin(self._phase + i * 0.7)
            idle = 0.10 * (0.5 + 0.5 * math.sin(self._phase * 0.6 + i * 0.9))
            target = 0.12 + (self._level * self._level_gain) * bell * wave + idle
            target = 0.0 if target < 0.0 else (1.0 if target > 1.0 else target)
            self._vals[i] += (target - self._vals[i]) * self.EASE
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self._phase = 0.0
        self._level = 0.0
        self._level_target = 0.0
        self._vals = [0.0] * self._n
        if not self._timer.isActive():
            self._timer.start(self._interval)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        total = self._n * self._bw + (self._n - 1) * self._gap
        x0 = cx - total / 2.0
        span = self._max_h - self._min_h
        for i in range(self._n):
            bh = self._min_h + self._vals[i] * span
            x = x0 + i * (self._bw + self._gap) + self._bw / 2.0
            top = cy - bh / 2.0
            grad = QLinearGradient(QPointF(x, top), QPointF(x, top + bh))
            grad.setColorAt(0.0, self._c_top)
            grad.setColorAt(1.0, self._c_bot)
            pen = QPen(QBrush(grad), self._bw)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QLineF(x, top, x, top + bh))


class Spinner(QWidget):
    """A rotating 270° arc (macOS `_Spinner`), used for thinking/applying."""

    def __init__(self, parent=None, color: str = ACCENT, size: int = 22,
                 thickness: float = 3.0, interval: int = 16):
        super().__init__(parent)
        self._color = QColor(color)
        self._t = thickness
        self._angle = 0.0
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent; border: none;")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _tick(self):
        self._angle = (self._angle + 7.0) % 360.0
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        if not self._timer.isActive():
            self._timer.start(16)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(self._color)
        pen.setWidthF(self._t)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        m = self._t / 2.0 + 1.0
        rect = QRectF(m, m, self.width() - 2 * m, self.height() - 2 * m)
        start = int(-self._angle * 16)
        p.drawArc(rect, start, int(270 * 16))


class VoiceDots(QWidget):
    """Three pulsing dots (macOS `_paint_dots`), shown while transcribing.
    pulse = 0.5 + 0.5·sin(phase·1.6 − i·0.9); alpha = 90 + 150·pulse;
    radius = r·(0.7 + 0.5·pulse)."""

    def __init__(self, parent=None, color: str = ACCENT, width: int = 80,
                 height: int = 48, dot_r: float = 5.0, gap: float = 22.0,
                 interval: int = 16):
        super().__init__(parent)
        self._color = QColor(color)
        self._r = dot_r
        self._gap = gap
        self._phase = 0.0
        self._interval = interval
        self.setMinimumSize(width, height)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent; border: none;")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _tick(self):
        self._phase += 0.16
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self._phase = 0.0
        if not self._timer.isActive():
            self._timer.start(self._interval)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        for i in range(3):
            pulse = 0.5 + 0.5 * math.sin(self._phase * 1.6 - i * 0.9)
            col = QColor(self._color)
            col.setAlpha(int(90 + 150 * pulse))
            p.setBrush(col)
            rr = self._r * (0.7 + 0.5 * pulse)
            cx_i = cx + (i - 1) * self._gap
            p.drawEllipse(QPointF(cx_i, cy), rr, rr)
