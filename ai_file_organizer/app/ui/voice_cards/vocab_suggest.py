"""Reusable 'suggest Custom Words from your field' panel.

The user types their niche -> we ask the AI (gpt-5-mini, via the proxy, on a background
thread so the UI never freezes) for DISTINCTIVE terms -> they're shown as selectable chips
-> the user picks which to add (plus can type their own). Added terms go to
`settings.dictation_custom_terms` (Grok key-term biasing). Used by the Custom Words settings
card and the onboarding step. Emits `terms_added(list)` after a successful add.
"""

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from app.core.settings import settings
from app.ui.theme_manager import get_theme_colors

ACCENT = "#7C4DFF"


class _FlowLayout(QLayout):
    """Minimal wrapping layout so chips flow onto multiple rows."""

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
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _layout(self, rect, test_only):
        x, y, line_h = rect.x(), rect.y(), 0
        sp = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            nx = x + hint.width() + sp
            if nx - sp > rect.right() and line_h > 0:
                x = rect.x()
                y = y + line_h + sp
                nx = x + hint.width() + sp
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class _ChipButton(QPushButton):
    """A selectable suggestion chip that also emits doubleClicked, for inline editing."""
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(e)


class SuggestTermsWorker(QThread):
    """Runs the AI suggestion OFF the UI thread (network call). Emits the term list (or [])."""
    done = Signal(list)

    def __init__(self, niche: str):
        super().__init__()
        self.niche = niche

    def run(self):
        try:
            from app.core.vocab import suggest_terms_for_niche
            self.done.emit(suggest_terms_for_niche(self.niche))
        except Exception:
            self.done.emit([])


class VocabSuggestPanel(QWidget):
    """Niche field -> Suggest -> selectable chips -> Add selected. Reusable in Settings and
    onboarding. Emits terms_added(list) after terms are committed to settings."""
    terms_added = Signal(list)

    def __init__(self, parent=None, show_manual_add=True):
        super().__init__(parent)
        self._c = get_theme_colors()
        self._chip_buttons = []
        self._worker = None
        self._show_manual = show_manual_add

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self._hint = QLabel("Dictating technical terms? Tell us your field and we'll suggest "
                            "words to spell right.")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.niche_input = QLineEdit()
        self.niche_input.setPlaceholderText("Your field — e.g. software engineering, cardiology…")
        self.niche_input.setMinimumHeight(34)
        self.niche_input.returnPressed.connect(self._on_suggest)
        row.addWidget(self.niche_input, 1)
        self.suggest_btn = QPushButton("Suggest words")
        self.suggest_btn.setCursor(Qt.PointingHandCursor)
        self.suggest_btn.setMinimumHeight(34)
        self.suggest_btn.clicked.connect(self._on_suggest)
        row.addWidget(self.suggest_btn)
        root.addLayout(row)

        # #5 — remember & prefill the last field the user asked about.
        self.niche_input.setText(settings.dictation_last_niche or "")

        self._status = QLabel("")
        self._status.setVisible(False)
        root.addWidget(self._status)

        self._chips_host = QWidget()
        self._chips_host.setStyleSheet("background: transparent;")
        self._chips_host.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self._chips_layout = _FlowLayout(spacing=8)
        self._chips_host.setLayout(self._chips_layout)
        self._chips_host.setVisible(False)       # shown only while it holds chips (see _clear_chips)
        root.addWidget(self._chips_host)

        self.add_btn = QPushButton("Add selected")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setMinimumHeight(34)
        self.add_btn.setVisible(False)
        self.add_btn.clicked.connect(self._on_add_selected)
        root.addWidget(self.add_btn)

        # #4 — a one-line "why these words?" note so the user knows what adding does.
        self._why = QLabel("Added words become spelling hints for the transcriber, so your names "
                           "and jargon come out right every time — even with Polishing off.")
        self._why.setWordWrap(True)
        root.addWidget(self._why)

        # Manual add — shown only when this panel is used standalone (onboarding). In the
        # Settings card the card already has its own add field, so we hide this duplicate.
        if self._show_manual:
            manual_row = QHBoxLayout()
            manual_row.setSpacing(8)
            self.manual_input = QLineEdit()
            self.manual_input.setPlaceholderText("Or add your own word…")
            self.manual_input.setMinimumHeight(34)
            self.manual_input.returnPressed.connect(self._on_manual_add)
            manual_row.addWidget(self.manual_input, 1)
            self.manual_btn = QPushButton("Add")
            self.manual_btn.setCursor(Qt.PointingHandCursor)
            self.manual_btn.setMinimumHeight(34)
            self.manual_btn.clicked.connect(self._on_manual_add)
            manual_row.addWidget(self.manual_btn)
            root.addLayout(manual_row)

        self._apply_theme_styles()

    # --- theme ---------------------------------------------------------------
    def apply_theme(self, theme=None):
        self._c = get_theme_colors(theme)
        self._apply_theme_styles()
        for b in self._chip_buttons:
            b.setStyleSheet(self._chip_style())

    def _apply_theme_styles(self):
        c = self._c
        self._hint.setStyleSheet(f"color: {c['text_secondary']}; font-size: 11px; "
                                 "background: transparent; border: none;")
        self._status.setStyleSheet(f"color: {c['text_secondary']}; font-size: 12px; "
                                   "background: transparent; border: none;")
        self._why.setStyleSheet(f"color: {c['text_muted']}; font-size: 11px; "
                                "background: transparent; border: none;")
        self.niche_input.setStyleSheet(f"""
            QLineEdit {{ background-color: {c['input_bg']}; border: 1px solid {c['border']};
                border-radius: 10px; padding: 0 12px; color: {c['text']}; font-size: 13px; }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}""")
        btn_css = (f"QPushButton {{ background-color: {ACCENT}; color: white; border: none; "
                   f"border-radius: 10px; padding: 0 16px; font-size: 13px; font-weight: 600; }} "
                   f"QPushButton:disabled {{ background-color: #9e9e9e; }}")
        self.suggest_btn.setStyleSheet(btn_css)
        self.add_btn.setStyleSheet(btn_css)
        if self._show_manual:
            self.manual_input.setStyleSheet(self.niche_input.styleSheet())
            self.manual_btn.setStyleSheet(btn_css)

    def _chip_style(self):
        c = self._c
        return (f"QPushButton {{ background-color: {c['hover']}; color: {c['text']}; "
                f"border: 1px solid {c['border_strong']}; border-radius: 12px; padding: 4px 12px; "
                f"font-size: 12px; }} "
                f"QPushButton:checked {{ background-color: {ACCENT}; color: white; "
                f"border-color: {ACCENT}; }}")

    # --- logic ---------------------------------------------------------------
    def _on_suggest(self):
        niche = self.niche_input.text().strip()
        if not niche:
            return
        settings.dictation_last_niche = niche   # #5 remember for next time
        try:
            settings._save_config()
        except Exception:
            pass
        if self._worker and self._worker.isRunning():
            return
        self.suggest_btn.setEnabled(False)
        self._status.setText("Thinking…")
        self._status.setVisible(True)
        self._clear_chips()
        self.add_btn.setVisible(False)
        self._worker = SuggestTermsWorker(niche)
        self._worker.done.connect(self._on_suggested)
        self._worker.start()

    def _on_suggested(self, terms):
        self.suggest_btn.setEnabled(True)
        existing = {t.lower() for t in settings.dictation_custom_terms}
        fresh = [t for t in terms if t.lower() not in existing]
        if not fresh:
            self._status.setText("No new suggestions — add your own below, or try another field.")
            return
        self._status.setVisible(False)
        for term in fresh:
            self._add_chip(term)
        self.add_btn.setVisible(True)
        self._update_add_count()

    def _add_chip(self, term, checked=True):
        b = _ChipButton(term)
        b.setCheckable(True)
        b.setChecked(checked)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip("Double-click to edit")
        b.setStyleSheet(self._chip_style())
        b.toggled.connect(self._update_add_count)
        b.doubleClicked.connect(lambda c=b: self._edit_chip(c))
        self._chip_buttons.append(b)
        self._chips_layout.addWidget(b)
        self._chips_host.setVisible(True)
        return b

    def _edit_chip(self, chip):
        """Double-click a chip to fix its text (casing/typo) before adding."""
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Edit word", "Edit this term:", text=chip.text())
        if not ok or not text.strip():
            return
        text = text.strip()
        others = {b.text().lower() for b in self._chip_buttons if b is not chip} | \
                 {t.lower() for t in settings.dictation_custom_terms}
        if text.lower() in others:
            self._status.setText(f"“{text}” is already added.")
            self._status.setVisible(True)
            return
        chip.setText(text)
        chip.setChecked(True)   # the double-click toggled it once; keep it selected
        self._update_add_count()

    def _update_add_count(self):
        n = sum(1 for b in self._chip_buttons if b.isChecked())
        self.add_btn.setText(f"Add selected ({n})")
        self.add_btn.setEnabled(n > 0)

    def _on_manual_add(self):
        """In the onboarding panel, a manually-typed word joins the selected chips above (so
        it's previewed + included in the 'Add selected' count), rather than committing on its
        own. 'Add selected' then saves everything together."""
        term = self.manual_input.text().strip()
        if not term:
            return
        existing = {t.lower() for t in settings.dictation_custom_terms} | \
                   {b.text().lower() for b in self._chip_buttons}
        if term.lower() in existing:
            self._status.setText(f"“{term}” is already added.")
            self._status.setVisible(True)
            self.manual_input.clear()
            return
        self._status.setVisible(False)
        self._add_chip(term)
        self.manual_input.clear()
        self.add_btn.setVisible(True)
        self._update_add_count()

    def _on_add_selected(self):
        chosen = [b.text() for b in self._chip_buttons if b.isChecked()]
        if not chosen:
            return
        added = settings.add_custom_terms(chosen)
        self._clear_chips()
        self.add_btn.setVisible(False)
        self._status.setText(f"Added {added} word{'s' if added != 1 else ''} to your Custom Words.")
        self._status.setVisible(True)
        if added:
            self.terms_added.emit(chosen)

    def _clear_chips(self):
        while self._chips_layout.count():
            item = self._chips_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._chip_buttons = []
        # An emptied flow layout keeps its old height until the next full relayout — a big blank
        # gap under "Added N words" (seen on the real screen). Hidden, it takes no space.
        self._chips_host.setVisible(False)
