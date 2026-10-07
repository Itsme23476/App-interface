"""Polishing card for the Voice page — pick how much Filect cleans up dictation.

Three levels (VoiceOS-style), each with a before/after example:
  none     — word-for-word, no corrections
  light    — remove filler/self-corrections, fix punctuation
  polished — light + smooth phrasing for clarity

The setting lives in app.core.settings (settings.dictation_polish_level:
'none'|'light'|'polished', default 'none'). 'none' never calls the server — the
dictation flow skips polishing. Styled from theme_manager so it works in light+dark.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel

from app.core.settings import settings
from app.ui.theme_manager import get_theme_colors

ACCENT = "#7C4DFF"

# (level, name, badge, bullets, example-after-text)
LEVELS = [
    ("none", "None", "Instant",
     ["Word-for-word transcript", "No corrections at all"],
     "Um so the meeting got moved to thursday no wait friday so so we should uh "
     "prep the deck before that."),
    ("light", "Light", "Fast",
     ["Removes filler & self-corrections", "Fixes punctuation"],
     "So the meeting got moved to Friday, so we should prep the deck before that."),
    ("polished", "Polished", "Smart",
     ["Everything in Light", "Smooths phrasing for clarity"],
     "The meeting got moved to Friday, so we should prep the deck beforehand."),
]


class VoiceCleanupCard(QFrame):
    """Settings-style card: a 3-way Polishing selector with example previews."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._c = get_theme_colors()
        self._options = {}  # level -> (frame, name_label)
        self._themed_fns = []  # (widget, build(c)->css) re-applied on theme change

        self.setObjectName("settingsCard")
        self._themed(self, lambda c: f"""
            QFrame#settingsCard {{
                background-color: {c['surface']};
                border: 1px solid {c['border']};
                border-radius: 16px;
            }}
            QFrame#settingsCard > QLabel {{ border: none; background: transparent; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(6)

        from app.ui.icons import line_pixmap
        _trow = QHBoxLayout(); _trow.setSpacing(9)
        _ic = QLabel(); _ic.setPixmap(line_pixmap("sparkle", 16, ACCENT))
        _ic.setStyleSheet("background: transparent; border: none;")
        _trow.addWidget(_ic, 0, Qt.AlignVCenter)
        title = QLabel("Polishing")
        title.setStyleSheet(f"font-family: 'Sora', 'Segoe UI', sans-serif; font-size: 15px; font-weight: 600; color: {ACCENT}; "
                            "background: transparent; border: none;")
        _trow.addWidget(title, 0, Qt.AlignVCenter)
        _trow.addStretch(1)
        layout.addLayout(_trow)

        subtitle = QLabel("Choose how much Filect cleans up your dictation.")
        self._themed(subtitle, lambda c: f"color: {c['text_secondary']}; font-size: 13px; "
                                         "background: transparent; border: none;")
        layout.addWidget(subtitle)

        row = QHBoxLayout()
        row.setSpacing(12)
        row.setContentsMargins(0, 8, 0, 0)
        for level, name, badge, bullets, example in LEVELS:
            row.addWidget(self._build_option(level, name, badge, bullets, example))
        layout.addLayout(row)

        self._select(getattr(settings, 'dictation_polish_level', 'none'), persist=False)

    def _themed(self, widget, build):
        """Register a widget whose stylesheet depends on theme colours, and apply it now."""
        self._themed_fns.append((widget, build))
        widget.setStyleSheet(build(self._c))

    def apply_theme(self, theme=None):
        """Re-read theme colours and re-apply the card, static labels and selection styling."""
        self._c = get_theme_colors(theme)
        for widget, build in self._themed_fns:
            widget.setStyleSheet(build(self._c))
        self._select(getattr(settings, 'dictation_polish_level', 'none'), persist=False)

    def _build_option(self, level, name, badge, bullets, example):
        frame = QFrame()
        frame.setObjectName("polishOption")
        frame.setCursor(Qt.PointingHandCursor)
        v = QVBoxLayout(frame)
        v.setContentsMargins(14, 12, 14, 14)
        v.setSpacing(8)

        head = QHBoxLayout()
        name_lbl = QLabel(name)
        name_lbl.setObjectName("polishName")
        head.addWidget(name_lbl)
        head.addStretch()
        badge_lbl = QLabel(badge)
        self._themed(badge_lbl, lambda c: f"color: {c['text_muted']}; font-size: 11px; font-weight: 600; "
                                          f"background: {c['input_bg']}; border: 1px solid {c['border']}; "
                                          "border-radius: 8px; padding: 2px 8px;")
        head.addWidget(badge_lbl)
        v.addLayout(head)

        for b in bullets:
            bl = QLabel("·  " + b)
            bl.setWordWrap(True)
            self._themed(bl, lambda c: f"color: {c['text_muted']}; font-size: 11px; "
                                       "background: transparent; border: none;")
            v.addWidget(bl)

        ex = QLabel(example)
        ex.setWordWrap(True)
        self._themed(ex, lambda c: f"color: {c['text_secondary']}; font-size: 12px; "
                                   f"background: {c['input_bg']}; border: 1px solid {c['border']}; "
                                   "border-radius: 10px; padding: 10px;")
        v.addWidget(ex)
        v.addStretch()

        frame.mousePressEvent = lambda e, lv=level: self._select(lv)
        self._options[level] = (frame, name_lbl)
        return frame

    def _select(self, level: str, persist: bool = True):
        if level not in ("none", "light", "polished"):
            level = "none"
        c = self._c
        for lv, (frame, name_lbl) in self._options.items():
            selected = (lv == level)
            frame.setStyleSheet(f"""
                QFrame#polishOption {{
                    background-color: {c['card']};
                    border: {'2px' if selected else '1px'} solid {ACCENT if selected else c['border']};
                    border-radius: 12px;
                }}
                QFrame#polishOption QLabel {{ border: none; }}
            """)
            name_lbl.setText(("✓  " if selected else "") + lv.capitalize())
            name_lbl.setStyleSheet(
                f"font-size: 14px; font-weight: 700; "
                f"color: {ACCENT if selected else c['text']}; "
                "background: transparent; border: none;")
        if persist:
            settings.dictation_polish_level = level
            settings._save_config()

    # Back-compat shim for any caller/test that used the old on/off API.
    def _set_enabled(self, enabled: bool):
        self._select("light" if enabled else "none")
