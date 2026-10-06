"""
Dictation overlay — the floating "pill" shown while dictating, ported to match
the macOS pack pixel-for-pixel (240×68 rounded pill + soft glow):

  - dictate  : 9 reactive gradient bars, centred.
  - search   : magnifier glyph (left) + bars.
  - organize : folder glyph (left) + bars.
  - transcribing (after release, until text appears): 3 pulsing dots.

Critical requirement: it must NOT take keyboard focus — otherwise the
synthesized Ctrl+V paste would land in the overlay instead of the user's app.
On Windows we combine Qt's non-activating flags with the Win32 ex-styles
WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW (the analogue of the Mac NSPanel
preventsActivation). Pure-Qt painting (see icons.AnimatedWaveform / VoiceDots).
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                               QFrame, QGraphicsDropShadowEffect)
from PySide6.QtGui import QColor

from app.ui.icons import AnimatedWaveform, VoiceDots, line_pixmap, ACCENT

logger = logging.getLogger(__name__)

_GLYPH = "#C9B8FF"           # soft lilac for the mode glyph
_PILL_W, _PILL_H = 240, 68
_MARGIN = 14                 # space around the pill for the glow


class DictationOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(_PILL_W + 2 * _MARGIN, _PILL_H + 2 * _MARGIN)

        self._mode = "dictate"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)

        self._card = QFrame()
        self._card.setObjectName("dictationPill")
        self._card.setFixedSize(_PILL_W, _PILL_H)
        # Exact spec: 240×68, corner 20, bg rgba(10,10,18,.922), border rgba(255,255,255,.11).
        self._card.setStyleSheet("""
            QFrame#dictationPill {
                background-color: rgba(10, 10, 18, 235);
                border: 1px solid rgba(255, 255, 255, 28);
                border-radius: 20px;
            }
        """)
        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(26)
        glow.setColor(QColor(124, 77, 255, 90))
        glow.setOffset(0, 0)
        self._card.setGraphicsEffect(glow)
        outer.addWidget(self._card)

        row = QHBoxLayout(self._card)
        row.setContentsMargins(22, 0, 22, 0)
        row.setSpacing(12)

        self._glyph = QLabel()
        self._glyph.setFixedWidth(20)
        self._glyph.setAlignment(Qt.AlignCenter)
        row.addWidget(self._glyph)

        # 9 bars, fixed 5px wide / 8px gap / 6–40px tall (the exact reference).
        self._bars = AnimatedWaveform(color=ACCENT, bars=9, width=130, height=48,
                                      bar_width=5.0, bar_gap=8.0, min_h=6.0, max_h=40.0)
        self._dots = VoiceDots(color=ACCENT, width=130, height=48)
        row.addWidget(self._bars, 1)
        row.addWidget(self._dots, 1)
        self._dots.setVisible(False)

    # ----- Win32 non-activation (applied once the HWND exists) -----
    def showEvent(self, event):
        super().showEvent(event)
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception as e:
            logger.debug(f"[OVERLAY] ex-style apply failed: {e}")

    def _set_glyph(self, name):
        if name:
            self._glyph.setPixmap(line_pixmap(name, 18, _GLYPH))
            self._glyph.setVisible(True)
        else:
            self._glyph.clear()
            self._glyph.setVisible(False)

    # ----- public API driven by the controller -----
    def show_listening(self, mode: str = "dictate"):
        self._mode = mode
        self._set_glyph({"search": "search", "organize": "folder"}.get(mode, ""))
        self._dots.setVisible(False)
        self._bars.setVisible(True)
        self._position_bottom_center()
        self.show()
        self.raise_()

    def show_transcribing(self):
        # Keep the mode glyph; swap bars → pulsing dots.
        self._bars.setVisible(False)
        self._dots.setVisible(True)

    def set_level(self, level: float):
        self._bars.set_level(level)

    def hide_pill(self):
        self.hide()

    def _position_bottom_center(self):
        try:
            from PySide6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen().availableGeometry()
            x = screen.center().x() - self.width() // 2
            y = screen.bottom() - self.height() - 80
            self.move(x, y)
        except Exception:
            pass
