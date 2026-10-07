"""Voice page card: recent dictation transcripts with per-row copy + delete."""
import time
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core import dictation_history
from app.ui.theme_manager import get_theme_colors

_ACCENT = "#7C4DFF"


def _rel_time(ts) -> str:
    """Compact relative age from an ISO-8601 timestamp (or epoch seconds):
    'just now' / 'Xm ago' / 'Xh ago' / 'Xd ago'."""
    if ts is None or ts == "":
        return ""
    try:
        if isinstance(ts, (int, float)):
            then = float(ts)
        else:
            then = datetime.fromisoformat(str(ts)).timestamp()
        delta = time.time() - then
    except Exception:
        return ""
    if delta < 0:
        delta = 0
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


class VoiceHistoryCard(QFrame):
    """Self-contained card showing recent dictations from dictation_history."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("voiceHistoryCard")
        self._theme = None  # overridden by apply_theme(); None = follow settings

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        from app.ui.icons import line_pixmap
        _trow = QHBoxLayout(); _trow.setSpacing(9)
        _ic = QLabel(); _ic.setPixmap(line_pixmap("clock", 16, _ACCENT))
        _ic.setStyleSheet("background: transparent; border: none;")
        _trow.addWidget(_ic, 0, Qt.AlignVCenter)
        self._title = QLabel("History")
        _trow.addWidget(self._title, 0, Qt.AlignVCenter)
        _trow.addStretch(1)
        root.addLayout(_trow)

        # Privacy banner — subtle rounded strip with a lock glyph.
        self._privacy = QFrame()
        self._privacy.setObjectName("voiceHistoryPrivacy")
        pl = QHBoxLayout(self._privacy)
        pl.setContentsMargins(10, 8, 10, 8)
        pl.setSpacing(8)
        self._lock = QLabel("\U0001F512")
        pl.addWidget(self._lock, 0, Qt.AlignTop)
        self._privacy_text = QLabel(
            "Stored on your PC — nothing is uploaded. "
            "History is capped at your most recent dictations."
        )
        self._privacy_text.setWordWrap(True)
        pl.addWidget(self._privacy_text, 1)
        root.addWidget(self._privacy)

        self._hint = QLabel(
            "Your recent dictations — copy any line if a paste didn't land."
        )
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setMaximumHeight(360)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._list_host = QWidget()
        self._list_host.setObjectName("voiceHistoryList")
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(10)
        self._scroll.setWidget(self._list_host)
        root.addWidget(self._scroll)

        self._clear_btn = QPushButton("Clear history")
        self._clear_btn.setObjectName("voiceHistoryClear")
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.clicked.connect(self._on_clear)
        root.addWidget(self._clear_btn, alignment=Qt.AlignRight)

        self.refresh()

    def apply_theme(self, theme=None):
        """Re-read theme colours (honouring an explicit theme) and rebuild the list."""
        self._theme = theme
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the list from the store and restyle for the current theme."""
        c = get_theme_colors(self._theme)
        self._apply_styles(c)

        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        entries = dictation_history.get_all()
        if not entries:
            empty = QLabel("No dictations yet.")
            empty.setStyleSheet(
                f"color: {c['text_muted']}; font-size: 12px; "
                "background: transparent; border: none;"
            )
            self._list_layout.addWidget(empty)
            self._list_layout.addStretch()
            return

        for entry in entries:
            self._list_layout.addWidget(self._make_row(entry, c))
        self._list_layout.addStretch()

    def _make_row(self, entry: dict, c) -> QWidget:
        """One dictation as its own rounded sub-card: timestamp + actions, then full text."""
        full = entry.get("text", "")
        entry_id = entry.get("id")
        card = QFrame()
        card.setObjectName("voiceHistoryRow")
        card.setStyleSheet(
            f"QFrame#voiceHistoryRow {{ background-color: {c['card']}; "
            f"border: 1px solid {c['border']}; border-radius: 12px; }}"
            "QFrame#voiceHistoryRow QLabel { background: transparent; border: none; }"
        )
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 12, 14, 14)
        col.setSpacing(8)

        # Top row: relative timestamp on the left, copy + delete on the right.
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)

        ts_label = QLabel(_rel_time(entry.get("timestamp")))
        ts_label.setStyleSheet(
            f"color: {c['text_muted']}; font-size: 11px;"
        )
        top.addWidget(ts_label, 0, Qt.AlignVCenter)
        top.addStretch(1)

        copy_btn = QPushButton("Copy")
        copy_btn.setObjectName("voiceHistoryCopy")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setToolTip("Copy full text")
        copy_btn.clicked.connect(
            lambda _=False, t=full: QGuiApplication.clipboard().setText(t)
        )
        copy_btn.setStyleSheet(
            "QPushButton#voiceHistoryCopy { background-color: transparent; "
            f"border: 1px solid {c['border']}; border-radius: 8px; color: {_ACCENT}; "
            "font-size: 11px; padding: 3px 10px; }"
            f"QPushButton#voiceHistoryCopy:hover {{ border-color: {_ACCENT}; "
            f"background-color: {c['hover']}; }}"
        )
        top.addWidget(copy_btn, 0, Qt.AlignVCenter)

        del_btn = QPushButton("\U0001F5D1")  # 🗑
        del_btn.setObjectName("voiceHistoryDelete")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setToolTip("Delete this entry")
        del_btn.clicked.connect(lambda _=False, eid=entry_id: self._on_delete(eid))
        del_btn.setStyleSheet(
            "QPushButton#voiceHistoryDelete { background-color: transparent; "
            f"border: 1px solid {c['border']}; border-radius: 8px; color: {c['text_muted']}; "
            "font-size: 11px; padding: 3px 8px; }"
            f"QPushButton#voiceHistoryDelete:hover {{ border-color: {c['danger_border']}; "
            f"color: {c['danger_text']}; background-color: {c['danger_bg']}; }}"
        )
        top.addWidget(del_btn, 0, Qt.AlignVCenter)
        col.addLayout(top)

        # Full dictation text, word-wrapped and readable (never elided to one line).
        text = QLabel(full or "(empty)")
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.setStyleSheet(
            f"color: {c['text']}; font-size: 13px; line-height: 20px;"
        )
        col.addWidget(text)
        return card

    def _on_delete(self, entry_id):
        dictation_history.delete(entry_id)
        self.refresh()

    def _on_clear(self):
        dictation_history.clear()
        self.refresh()

    def _apply_styles(self, c):
        self.setStyleSheet(
            f"QFrame#voiceHistoryCard {{ background-color: {c['card']}; "
            f"border: 1px solid {c['border']}; border-radius: 16px; }}"
        )
        self._title.setStyleSheet(
            f"font-family: 'Sora', 'Segoe UI', sans-serif; font-size: 15px; font-weight: 600; color: {_ACCENT}; "
            "background: transparent; border: none;"
        )
        self._privacy.setStyleSheet(
            f"QFrame#voiceHistoryPrivacy {{ background-color: {c['surface']}; "
            f"border: 1px solid {c['divider']}; border-radius: 10px; }}"
            "QFrame#voiceHistoryPrivacy QLabel { background: transparent; border: none; }"
        )
        self._lock.setStyleSheet(
            "background: transparent; border: none; font-size: 12px;"
        )
        self._privacy_text.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; "
            "background: transparent; border: none;"
        )
        self._hint.setStyleSheet(
            f"color: {c['text_muted']}; font-size: 11px; "
            "background: transparent; border: none;"
        )
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            f"QWidget#voiceHistoryList {{ background: transparent; }}"
        )
        self._clear_btn.setStyleSheet(
            "QPushButton#voiceHistoryClear { "
            f"background-color: {c['danger_bg']}; border: 1px solid {c['danger_border']}; "
            f"border-radius: 10px; color: {c['danger_text']}; font-weight: 600; "
            "padding: 6px 14px; }"
            f"QPushButton#voiceHistoryClear:hover {{ border-color: {c['danger_text']}; }}"
        )
