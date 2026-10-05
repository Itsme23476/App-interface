"""
Dictation overlay — the small floating "pill" shown while dictating.

Critical requirement: it must NOT take keyboard focus. If it did, the
synthesized Ctrl+V paste would land in the overlay instead of the user's app.
On Windows we combine Qt's non-activating flags with the Win32 ex-styles
WS_EX_NOACTIVATE (never activate on show/click) + WS_EX_TOOLWINDOW (keep it off
the taskbar / alt-tab). This is the Windows analogue of the Mac NSPanel
setPreventsActivation_.

Minimal for now: a state label ("Listening…" / "Transcribing…") + a thin level
bar driven by the recorder's `level` signal. The animated waveform can come
later; this is enough to see what's happening.
"""
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QFrame

logger = logging.getLogger(__name__)


class DictationOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Frameless, always-on-top, tool window, does-not-accept-focus.
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(220, 56)

        card = QFrame(self)
        card.setObjectName("dictationPill")
        card.setGeometry(0, 0, 220, 56)
        card.setStyleSheet("""
            QFrame#dictationPill {
                background-color: #16161F;
                border: 1px solid #2A2A3A;
                border-radius: 28px;
            }
        """)
        row = QHBoxLayout(card)
        row.setContentsMargins(18, 0, 18, 0)
        row.setSpacing(10)

        self._dot = QLabel("●")
        self._dot.setStyleSheet("color:#7C4DFF; font-size:16px;")
        row.addWidget(self._dot)

        self._label = QLabel("Listening…")
        self._label.setStyleSheet("color:#E8E8F0; font-size:14px; font-weight:600; background:transparent;")
        row.addWidget(self._label)
        row.addStretch()

        self._level_bar = QFrame()
        self._level_bar.setFixedSize(6, 24)
        self._level_bar.setStyleSheet("background:#7C4DFF; border-radius:3px;")
        row.addWidget(self._level_bar)

        # Pulse the dot while listening.
        self._pulse_on = True
        self._pulse = QTimer(self)
        self._pulse.timeout.connect(self._tick_pulse)

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
            logger.debug("[OVERLAY] applied WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW")
        except Exception as e:
            logger.debug(f"[OVERLAY] ex-style apply failed: {e}")

    def _tick_pulse(self):
        self._pulse_on = not self._pulse_on
        self._dot.setStyleSheet(
            f"color:{'#7C4DFF' if self._pulse_on else '#3A2A6A'}; font-size:16px;"
        )

    # ----- public API driven by the controller -----
    def show_listening(self, mode: str = "dictate"):
        labels = {"dictate": "Listening…", "search": "Listening (search)…",
                  "organize": "Listening (organize)…"}
        self._label.setText(labels.get(mode, "Listening…"))
        self._position_bottom_center()
        self.show()
        self.raise_()
        self._pulse.start(450)

    def show_transcribing(self):
        self._label.setText("Transcribing…")
        self._pulse.stop()
        self._dot.setStyleSheet("color:#7C4DFF; font-size:16px;")

    def set_level(self, level: float):
        # Map 0..1 -> bar height 4..24 px.
        h = max(4, min(24, int(4 + level * 20)))
        self._level_bar.setFixedSize(6, h)

    def hide_pill(self):
        self._pulse.stop()
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
