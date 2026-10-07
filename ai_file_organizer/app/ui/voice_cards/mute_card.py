"""Voice card: toggle muting system output while recording.

Setting lives in app.core.settings (settings.dictation_mute_while_recording).
When on, the recorder mutes the PC's output while you hold the dictation key so a
background video/music doesn't bleed into the mic, then restores it the instant you
release. This card is display/settings only — the actual muting is handled by the
recorder elsewhere; here we just read/write the toggle."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton

from app.core.settings import settings
from app.ui.theme_manager import get_theme_colors

ACCENT = "#7C4DFF"


class VoiceMuteCard(QFrame):
    """Settings-style card with a single on/off toggle for mute-while-recording."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._c = get_theme_colors()
        self.setObjectName("settingsCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(9)
        from app.ui.icons import line_pixmap
        _ic = QLabel()
        _ic.setPixmap(line_pixmap("mute", 16, ACCENT))
        _ic.setStyleSheet("background: transparent; border: none;")
        row.addWidget(_ic, 0, Qt.AlignVCenter)
        title = QLabel("Mute while recording")
        title.setStyleSheet(f"font-family: 'Sora', 'Segoe UI', sans-serif; font-size: 15px; font-weight: 600; color: {ACCENT}; "
                            "background: transparent; border: none;")
        row.addWidget(title, 0, Qt.AlignVCenter)
        row.addStretch(1)
        self._toggle = QPushButton()
        self._toggle.setCheckable(True)
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.setFixedSize(66, 30)
        self._toggle.setChecked(bool(getattr(settings, 'dictation_mute_while_recording', False)))
        self._toggle.toggled.connect(self._on_toggled)
        self._style_toggle()
        row.addWidget(self._toggle, 0, Qt.AlignVCenter)
        layout.addLayout(row)

        self._hint = QLabel("Mutes your PC's sound while you hold the dictation key, so a video or "
                            "music playing in the background doesn't bleed into the mic — and restores "
                            "it the moment you let go.")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._apply_theme_styles()

    def apply_theme(self, theme=None):
        """Re-read theme colours and re-apply the card frame and hint text."""
        self._c = get_theme_colors(theme)
        self._apply_theme_styles()

    def _apply_theme_styles(self):
        """(Re)apply all theme-colour-dependent inline styles from ``self._c``."""
        c = self._c
        self.setStyleSheet(f"""
            QFrame#settingsCard {{
                background-color: {c.get('surface', '#111119')};
                border: 1px solid {c.get('border', '#1C1C28')};
                border-radius: 16px;
            }}
            QFrame#settingsCard > QLabel {{ border: none; background: transparent; }}
        """)
        self._hint.setStyleSheet(f"color: {c.get('text_secondary', '#B0B0C0')}; font-size: 13px; "
                                 "background: transparent; border: none;")

    def _style_toggle(self):
        on = self._toggle.isChecked()
        self._toggle.setText("On" if on else "Off")
        self._toggle.setStyleSheet(
            f"QPushButton {{ background-color: {ACCENT if on else '#3A3A48'}; color: white; "
            "border: none; border-radius: 15px; font-size: 13px; font-weight: 600; }")

    def _on_toggled(self, checked):
        settings.dictation_mute_while_recording = bool(checked)
        settings._save_config()
        self._style_toggle()
