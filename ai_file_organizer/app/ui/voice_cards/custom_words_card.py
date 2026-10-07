"""Voice > Custom Words card — user-supplied key terms for transcription biasing.

Terms live in `settings.dictation_custom_terms` (list[str], persisted via
`settings._save_config()`). They are sent to the transcribe edge function, which
forwards them to Grok Voice Transcribe 2.0 as `keyterm` biasing so names/jargon
are spelled right.
"""

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.settings import settings
from app.ui.theme_manager import get_theme_colors

ACCENT = "#7C4DFF"
MAX_TERMS = 200  # mirrors settings cap; xAI accepts up to 100 per request (server trims)


class _FlowLayout(QLayout):
    """Minimal wrapping layout so chips flow onto multiple rows (Qt ships no built-in)."""

    def __init__(self, spacing=8):
        super().__init__()
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _layout(self, rect, test_only):
        x, y, line_h = rect.x(), rect.y(), 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > rect.right() and line_h > 0:
                x = rect.x()
                y = y + line_h + spacing
                next_x = x + hint.width() + spacing
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class VoiceCustomWordsCard(QFrame):
    """Settings-style card: add/remove custom words biased into transcription."""

    def __init__(self, parent=None):
        super().__init__(parent)
        c = get_theme_colors()
        self._c = c

        self.setObjectName("customWordsCard")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        from app.ui.icons import line_pixmap
        _trow = QHBoxLayout(); _trow.setSpacing(9)
        _ic = QLabel(); _ic.setPixmap(line_pixmap("type", 16, ACCENT))
        _ic.setStyleSheet("background: transparent; border: none;")
        _trow.addWidget(_ic, 0, Qt.AlignVCenter)
        title = QLabel("Custom Words")
        title.setStyleSheet(
            f"font-family: 'Sora', 'Segoe UI', sans-serif; font-size: 15px; font-weight: 600; color: {ACCENT}; "
            "background: transparent; border: none;"
        )
        _trow.addWidget(title, 0, Qt.AlignVCenter)
        _trow.addStretch(1)
        root.addLayout(_trow)

        self._hint = QLabel(
            "Add names, jargon, or brand terms you say often so they're spelled "
            "right — e.g. Filect, PySide6, a client's name."
        )
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        # Input row: field + Add button (Enter in the field also adds).
        row = QHBoxLayout()
        row.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Add a word or phrase…")
        self.input.setMinimumHeight(34)
        self.input.returnPressed.connect(self._on_add_clicked)
        row.addWidget(self.input, 1)

        add_btn = QPushButton("Add")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setMinimumHeight(34)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {ACCENT};
                border: none;
                border-radius: 10px;
                color: white;
                font-weight: 600;
                padding: 0 18px;
            }}
            QPushButton:hover {{ background-color: #6A3DF0; }}
        """)
        add_btn.clicked.connect(self._on_add_clicked)
        row.addWidget(add_btn)
        root.addLayout(row)

        # Chips area.
        self._chips_host = QWidget()
        self._chips_host.setStyleSheet("background: transparent;")
        self._chips_host.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self._chips_layout = _FlowLayout(spacing=8)
        self._chips_host.setLayout(self._chips_layout)
        root.addWidget(self._chips_host)

        self._empty_label = QLabel("No custom words yet.")
        root.addWidget(self._empty_label)

        self._apply_theme_styles()
        self._refresh_chips()

    # --- theme ----------------------------------------------------------------

    def apply_theme(self, theme=None):
        """Re-read theme colours and re-apply every themed stylesheet (chips included)."""
        self._c = get_theme_colors(theme)
        self._apply_theme_styles()
        self._refresh_chips()

    def _apply_theme_styles(self):
        """(Re)apply all theme-colour-dependent inline styles from ``self._c``."""
        c = self._c
        self.setStyleSheet(f"""
            QFrame#customWordsCard {{
                background-color: {c['card']};
                border: 1px solid {c['border']};
                border-radius: 16px;
            }}
            QFrame#customWordsCard > QLabel {{ border: none; background: transparent; }}
        """)
        self._hint.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; "
            "background: transparent; border: none;"
        )
        self.input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {c['input_bg']};
                border: 1px solid {c['border']};
                border-radius: 10px;
                padding: 0 12px;
                color: {c['text']};
                font-size: 13px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}
        """)
        self._empty_label.setStyleSheet(
            f"color: {c['text_muted']}; font-size: 12px; "
            "background: transparent; border: none;"
        )

    # --- logic (tests call these directly) ------------------------------------

    def _add_term(self, text: str) -> bool:
        """Trim, reject empties/dupes (case-insensitive) and overflow; persist. True if added."""
        term = (text or "").strip()
        if not term:
            return False
        existing = settings.dictation_custom_terms
        if any(term.lower() == t.lower() for t in existing):
            return False
        if len(existing) >= MAX_TERMS:
            return False
        existing.append(term)
        settings._save_config()
        self._refresh_chips()
        return True

    def _remove_term(self, term: str) -> None:
        terms = settings.dictation_custom_terms
        settings.dictation_custom_terms = [t for t in terms if t != term]
        settings._save_config()
        self._refresh_chips()

    # --- ui helpers -----------------------------------------------------------

    def _on_add_clicked(self):
        if self._add_term(self.input.text()):
            self.input.clear()
        self.input.setFocus()

    def _refresh_chips(self):
        # Clear existing chip widgets.
        while self._chips_layout.count():
            item = self._chips_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        terms = settings.dictation_custom_terms
        self._empty_label.setVisible(not terms)
        for term in terms:
            self._chips_layout.addWidget(self._make_chip(term))

    def _make_chip(self, term: str) -> QWidget:
        c = self._c
        chip = QFrame()
        chip.setStyleSheet(f"""
            QFrame {{
                background-color: {c['hover']};
                border: 1px solid {c['border_strong']};
                border-radius: 12px;
            }}
        """)
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(10, 4, 6, 4)
        lay.setSpacing(6)

        label = QLabel(term)
        label.setStyleSheet(
            f"color: {c['text']}; font-size: 12px; background: transparent; border: none;"
        )
        lay.addWidget(label)

        x_btn = QPushButton("×")
        x_btn.setCursor(Qt.PointingHandCursor)
        x_btn.setFixedSize(18, 18)
        x_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                color: {c['text_muted']};
                font-size: 15px;
                font-weight: 600;
                padding: 0;
            }}
            QPushButton:hover {{ color: {c['danger_text']}; }}
        """)
        x_btn.clicked.connect(lambda _=False, t=term: self._remove_term(t))
        lay.addWidget(x_btn)
        return chip
