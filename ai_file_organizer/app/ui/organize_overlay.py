"""
Voice Organize overlay — the floating card that slides down from the top when you voice-organize, or when the
voice assistant organizes a folder / sets up auto-organize. Ported to match the macOS pack pixel-for-pixel
(464-wide card, persistent "+ Organize" header, arc spinner, gradient waveform):

  thinking  → spinner + message ("Finding the folder…")
  listening → 11-bar waveform + "Listening… tap Stop when you're done" + Stop
  plan      → "Move N files into M folders" + target (~ for home, middle shortened; + Change) + folder groups
              + "Change by voice" mic  (hands-free: "Say “yes”, “no”, or what to change" instead)
              + Cancel / ✓ Organize  (≥30 → two-click; `action` renames it: "Turn on and organize 12 files")
  applying  → spinner + "Organizing…"
  done      → ✓ + message (auto-dismiss; with Undo: "↩  Undo" and 20 s to reach for it)
  error     → ⚠ (an accent "?" when it's asking, not failing) + message + full-width "Choose folder…" / Close
  confirm   → a question + Cancel / one main button                     (voice assistant: "ask first")
  choice    → a question + [a text box] + full-width answers + Cancel    (voice assistant: "which way?")

Every page is sized from its text: a wrapping label gets the height its text needs at the width it's given, so
a long question or message is never cut off (Windows has no notch inset; the Mac's fixed heights don't apply).

Non-activating (WS_EX_NOACTIVATE) so it floats over other apps and takes mouse clicks WITHOUT stealing keyboard
focus. The one exception is the choice page's text box: a click there takes the keyboard (like the Agents card's
type field, via win_panel.allow_typing), and it's handed back to your app when the page changes, the panel tucks
or it closes. Anchored top-centre of the work area. Pure-Qt painting; no AppKit/Spaces code.

A click on the card body tucks it into the small pill at the top edge (it never cancels: a stray click during
the voice assistant's long "Reading…" must not stop the job). ✕ / Cancel / Esc close it.

Signals (consumed by VoiceOrganizeController):
  refine_requested, organize_clicked, change_folder_requested, dismissed,
  undo_clicked, confirm_clicked, choice_made(key), text_submitted(text)
"""
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer, QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QLineEdit,
    QScrollArea, QGraphicsDropShadowEffect,
)

from app.ui.icons import AnimatedWaveform, Spinner, line_pixmap, ACCENT

logger = logging.getLogger(__name__)

IS_WIN = sys.platform.startswith("win")

_CARD_W = 464
_INNER_W = _CARD_W - 44          # the card's 22 px side margins: the width every full-width row gets
_GLYPH_W = 22                    # the ✓ / ⚠ / ? column on the done and error pages
_GLYPH_GAP = 10
_ROW_TEXT_W = _INNER_W - _GLYPH_W - _GLYPH_GAP   # the message beside that glyph

# palette (macOS pack)
_BG        = "#0D0D13"
_BORDER    = "#23232E"
_CARD2     = "#16161E"   # inset rows / folder cards / answers
_CARD2_BRD = "#24242F"
_TITLE     = "#F2F2F7"
_TEXT      = "#D6D6E0"   # messages
_QUESTION  = "#E8E8F0"   # the confirm / choice question
_MUTED     = "#9A9AA8"
_WARN      = "#F2A33C"
_ACCENT    = ACCENT
_ACCENT_HD = "#8B6FFF"   # "+ Organize" header; the "?" of a question


def display_path(path: str, home: str = None) -> str:
    """The folder as the preview shows it: the home folder as ~ (C:\\Users\\you\\Downloads → ~\\Downloads).
    Windows paths are case-insensitive, so the home prefix is compared with normcase."""
    p = str(path or "").strip()
    if not p:
        return "—"
    home = str(home if home is not None else Path.home())
    norm_p, norm_h = os.path.normpath(p), os.path.normpath(home).rstrip("\\/")
    if not norm_h:
        return p
    if os.path.normcase(norm_p) == os.path.normcase(norm_h):
        return "~"
    if os.path.normcase(norm_p).startswith(os.path.normcase(norm_h) + os.sep):
        return "~" + norm_p[len(norm_h):]
    return p


def _undo_pixmap(color: str, size: int = 16) -> QPixmap:
    """A painted ↩ for the Undo button (the font's ↩ came out tiny), in the icons.line_pixmap style."""
    ratio = 2
    pm = QPixmap(size * ratio, size * ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size * ratio / 24.0, size * ratio / 24.0)
    pen = QPen(QColor(color))
    pen.setWidthF(2.2)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(9, 5)
    path.lineTo(4, 10)
    path.lineTo(9, 15)
    path.moveTo(4, 10)
    path.lineTo(14.5, 10)
    path.cubicTo(17.8, 10, 20, 12.3, 20, 15)
    path.cubicTo(20, 17.7, 17.8, 20, 14.5, 20)
    path.lineTo(11, 20)
    p.drawPath(path)
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def short_path(text: str, fm, room: int) -> str:
    """Fit a path into `room` px by shortening its MIDDLE. The folder's own name (the last part) stays whole when it
    can: "~\\Documents\\Cli…\\Archive\\Second Thoughts"."""
    room = max(40, int(room))
    if fm.horizontalAdvance(text) <= room:
        return text
    head, sep, tail = text.rpartition(os.sep) if os.sep in text else text.rpartition("/")
    if sep and head:
        head_room = room - fm.horizontalAdvance(sep + tail)
        if head_room >= fm.horizontalAdvance("~…") + 4:
            return fm.elidedText(head, Qt.ElideMiddle, head_room) + sep + tail
    return fm.elidedText(text, Qt.ElideMiddle, room)


class _TypeBox(QLineEdit):
    """The choice page's text box. The panel never takes the keyboard by itself, so a click asks it to (the Agents
    card's _TypeField does the same), then focuses the box."""

    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click = on_click

    def mousePressEvent(self, e):
        try:
            self._on_click()
        except Exception:
            pass
        self.setFocus(Qt.MouseFocusReason)
        super().mousePressEvent(e)


# ---- finding a file by voice (the design pack: filect-search-panel-design-for-windows) ----------------------------
_F_ROW_BG = "#111119"
_F_ROW_BRD = "#1C1C28"
_F_TEXT = "#E8E8F0"
_F_SUB = "#B0B0C0"
_F_MUTED = "#7A7A90"
_F_PURPLE = "#7C4DFF"
_F_LIGHT = "#B39DFF"
_F_ROW_H = 58
_F_GAP = 8
_F_LIST_H = 362                  # five and a half rows: plain that the list goes on
_F_SCROLL_ROOM = 12              # kept free for the scroll bar, so it never sits on a row


class _ResultRow(QFrame):
    """One file the voice search found: number, picture (or its type), name, where and when, and a folder icon.
    Click it to open it; the folder icon shows where it is. Hover: purple border."""

    def __init__(self, n: int, info: dict, opened, revealed, width: int):
        super().__init__()
        self._n, self._opened = n, opened
        self.setObjectName("ovResultRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(_F_ROW_H)
        self.setStyleSheet(f"QFrame#ovResultRow{{background:{_F_ROW_BG}; border:1px solid {_F_ROW_BRD}; "
                           f"border-radius:12px;}} QFrame#ovResultRow:hover{{border-color:{_F_PURPLE};}}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 8, 7)
        lay.setSpacing(10)
        num = QLabel(str(n))
        num_w = 22 if n > 9 else 10
        num.setFixedWidth(num_w)
        num.setStyleSheet(f"color:{_F_LIGHT}; font-size:12px; background:transparent; border:none;")
        lay.addWidget(num)
        self.thumb = QLabel(str(info.get("ext") or "")[:4])
        self.thumb.setFixedSize(34, 42)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet(f"background:{_F_TEXT}; color:#0B0B10; font-size:9px; font-weight:700; "
                                 "border-radius:5px; border:none;")
        lay.addWidget(self.thumb)
        self._text_w = max(60, width - 2 - 10 - 8 - num_w - 34 - 28 - 3 * 10)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self.name = self._line(info.get("name") or "", f"color:{_F_TEXT}; font-size:13px; font-weight:600;",
                               Qt.ElideMiddle)
        self.sub = self._line(info.get("sub") or "", f"color:{_F_SUB}; font-size:12px;", Qt.ElideRight)
        col.addStretch(1)
        col.addWidget(self.name)
        col.addWidget(self.sub)
        col.addStretch(1)
        lay.addLayout(col, 1)
        self.folder = QPushButton()
        self.folder.setObjectName("ovResultFolder")
        self.folder.setFixedSize(28, 28)
        self.folder.setCursor(Qt.PointingHandCursor)
        self.folder.setToolTip("Show where it is")
        self._icon_off = QIcon(line_pixmap("folder", 16, _F_SUB))
        self._icon_on = QIcon(line_pixmap("folder", 16, _F_LIGHT))
        self.folder.setIcon(self._icon_off)
        self.folder.setIconSize(QSize(16, 16))
        self.folder.setStyleSheet("QPushButton#ovResultFolder{background:transparent; border:none; border-radius:8px;}"
                                  f"QPushButton#ovResultFolder:hover{{background:{_F_ROW_BRD};}}")
        self.folder.installEventFilter(self)
        self.folder.clicked.connect(lambda: revealed.emit(n - 1))
        lay.addWidget(self.folder)

    def eventFilter(self, obj, e):
        from PySide6.QtCore import QEvent
        if obj is self.folder and e.type() in (QEvent.Enter, QEvent.Leave):
            self.folder.setIcon(self._icon_on if e.type() == QEvent.Enter else self._icon_off)
        return False

    def _line(self, text: str, style: str, elide) -> QLabel:
        lab = QLabel()
        lab.setTextFormat(Qt.PlainText)      # (a file's name is never markup)
        lab.setStyleSheet(style + " background:transparent; border:none;")
        lab.ensurePolished()
        lab.setText(lab.fontMetrics().elidedText(str(text), elide, self._text_w))
        lab.setToolTip(str(text))
        lab.setFixedWidth(self._text_w)
        return lab

    def mousePressEvent(self, e):
        e.accept()                           # (a click on a row never tucks the panel away)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self._opened.emit(self._n - 1)
        e.accept()


class OrganizeOverlay(QWidget):
    refine_requested = Signal()
    organize_clicked = Signal()
    change_folder_requested = Signal()
    dismissed = Signal()
    undo_clicked = Signal()          # the done page's ↩ Undo
    confirm_clicked = Signal()       # the confirm page's main button
    choice_made = Signal(str)        # one of the choice page's answers (its key)
    text_submitted = Signal(str)     # typed into the choice page's box, Return pressed
    result_opened = Signal(int)      # voice search: a result row clicked (its index)
    result_revealed = Signal(int)    # voice search: a row's folder icon
    result_chip = Signal(str)        # voice search: a quick answer to its question
    results_all = Signal()           # voice search: Show all

    _BIG_PLAN = 30                   # moving this many files needs a 2nd click
    _MAX_NAMES = 200                 # file names listed per folder in the preview

    def __init__(self, parent=None):
        super().__init__(parent)
        # (No Qt.WindowDoesNotAcceptFocus: Qt then refuses to activate the window even for the text box. Not taking
        # focus is WS_EX_NOACTIVATE's job (showEvent) + WA_ShowWithoutActivating — the Agents card works the same.)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(_CARD_W + 28)   # +room for the glow

        self._state = "idle"     # idle|listening|thinking|plan|applying|done|error|confirm|choice
        self._hands_free = False # voice assistant: you just talk (no mic button on the plan)
        self._refining = False
        self._organize_armed = False
        self._collapsed = False
        self._tab = None
        self._dismiss_timer = None
        self._current = None     # the page in the card now
        self._page_plan = None   # rebuilt per plan; kept while another page shows (show_plan_page)
        self._organize_btn = None
        self._refine_btn = None
        self.choice_field = None
        self.plan_field = None
        self.choice_buttons = {}
        self.result_rows = []
        self.result_list = None
        self.result_chips = {}
        self.result_field = None
        self.result_all_btn = None

        outer = QVBoxLayout(self)
        # Small top margin so the card sits flush against the top of the screen
        # (Windows has no notch to clear, unlike the Mac anchor).
        outer.setContentsMargins(14, 3, 14, 16)

        self._card = QFrame()
        self._card.setObjectName("organizeOverlayCard")
        self._card.setFixedWidth(_CARD_W)
        self._card.setStyleSheet(
            f"QFrame#organizeOverlayCard {{ background-color: {_BG}; "
            f"border: 1px solid {_BORDER}; border-radius: 18px; }}")
        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(34)
        glow.setColor(QColor(0, 0, 0, 170))
        glow.setOffset(0, 6)
        self._card.setGraphicsEffect(glow)
        outer.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(22, 16, 22, 18)
        card_lay.setSpacing(12)

        # ---- persistent header: "+ Organize"  …  × ----
        header = QHBoxLayout()
        htitle = self._brand = QLabel("+ Organize")     # (voice search swaps it to "+ Find": take_over)
        htitle.setStyleSheet(f"color:{_ACCENT_HD}; font-size:14px; font-weight:700; background:transparent;")
        header.addWidget(htitle)
        header.addStretch()
        # Painted × icon (a QIcon can't be recolored/hidden by the app's global
        # QPushButton style the way a text glyph can). objectName-scoped style so
        # the global button background doesn't paint a dark circle over it.
        close = QPushButton()
        close.setObjectName("ovCloseBtn")
        close.setCursor(Qt.PointingHandCursor)
        close.setFixedSize(28, 28)
        close.setToolTip("Close")
        self._close_icon = line_pixmap("x", 15, "#C9C9D4")
        self._close_icon_hover = line_pixmap("x", 15, "#FFFFFF")
        close.setIcon(QIcon(self._close_icon))
        close.setIconSize(QSize(15, 15))
        close.setStyleSheet(
            "QPushButton#ovCloseBtn{background:transparent; border:none; border-radius:14px;}"
            "QPushButton#ovCloseBtn:hover{background:#2A2A36;}")
        close.clicked.connect(self.dismiss)
        header.addWidget(close)
        card_lay.addLayout(header)

        # ---- body: holds exactly one page at a time ----
        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(0)
        card_lay.addLayout(self._body)

        self._page_thinking = self._make_thinking_page()
        self._page_listening = self._make_listening_page()
        self._page_done = self._make_done_page()
        self._page_error = self._make_error_page()
        self._page_confirm = self._make_confirm_page()
        self._page_choice = self._new_page(spacing=8)      # refilled per question
        self._page_results = self._new_page(spacing=_F_GAP)  # voice search: refilled per answer

    # ----- Win32 non-activation -----
    def showEvent(self, e):
        super().showEvent(e)
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.dismiss()
        else:
            super().keyPressEvent(e)

    # ----- typing (the choice page's box) -----
    def _key_for_typing(self):
        """You clicked the text box: let this non-activating panel take the keyboard, on THIS click only (Windows:
        win_panel.allow_typing, exactly like the Agents card). Given back by _end_typing."""
        if not IS_WIN:
            return
        try:
            from app.ui import win_panel
            self._win_panel_capture = False     # (only the typing bit: don't hide this panel from screenshots)
            win_panel.allow_typing(self, True)
        except Exception as e:
            logger.debug(f"[organize overlay] allow typing failed: {e}")

    def _end_typing(self):
        """Back to never taking the keyboard: it returns to the app you were in. Called BEFORE hiding, or Windows
        hands the focus to Filect's main window instead."""
        if not getattr(self, "_win_typing", False):
            return
        try:
            from app.ui import win_panel
            win_panel.allow_typing(self, False)
        except Exception:
            self._win_typing = False

    # ----- lifecycle -----
    def set_hands_free(self, on: bool):
        """Voice assistant: the mic is already listening, so the plan says what to say instead of a mic button.
        It also follows the Mac's rule for opening: new pages don't pop a tucked panel open by themselves (the
        controller calls ensure_open() when a page needs you)."""
        self._hands_free = bool(on)

    def ensure_open(self):
        """It needs you now: open it, whether it was closed or tucked into the pill."""
        if self._collapsed:
            self._expand()
        elif not self.isVisible():
            self.present()

    def present(self, tucked: bool = False):
        """Open the card. `tucked`: start as the small pill at the top edge instead ("Open answers
        automatically" off); a click on the pill, or ensure_open(), brings the card down."""
        # A fresh request: cancel a pending auto-dismiss from a previous 'done' so it can't close the new flow.
        self._cancel_dismiss_timer()
        if tucked:
            self._position_top_center()
            self._collapse()
            return
        self._collapsed = False
        if self._tab is not None:
            self._tab.hide()
        self._position_top_center()
        self.show()
        self.raise_()

    def dismiss(self):
        self._cancel_dismiss_timer()
        self._end_typing()
        self.hide()
        if self._tab is not None:
            self._tab.hide()
        self._collapsed = False
        self.dismissed.emit()

    def _on_dismiss(self):
        """✕ / Cancel (the Mac name: the controller's cancel() calls it). Same as dismiss(): emits `dismissed`."""
        self.dismiss()

    def _cancel_dismiss_timer(self):
        if self._dismiss_timer is not None:
            self._dismiss_timer.stop()
            self._dismiss_timer = None

    def _start_dismiss_timer(self, ms):
        self._cancel_dismiss_timer()
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.dismiss)
        self._dismiss_timer.start(int(ms))

    # ----- collapse / expand (tuck up to the top edge, like the Mac notch) -----
    def mousePressEvent(self, e):
        # A click on the card body (not a button/child that handled it) tucks the overlay back up to the top of
        # the screen. It never closes or cancels: a stray click while the voice assistant reads a big folder
        # mustn't stop the job (✕ / Cancel do that).
        if not self._collapsed:
            self._collapse()
        super().mousePressEvent(e)

    @property
    def _minimized(self) -> bool:          # (the Mac's name for tucked)
        return self._collapsed

    def _ensure_tab(self):
        if self._tab is None:
            self._tab = _CollapsedTab()
            self._tab.clicked.connect(self._expand)
        self._tab.set_text(self._brand.text())

    def _collapse(self):
        self._end_typing()
        self._collapsed = True
        self._ensure_tab()
        self._tab.present()
        self.hide()

    def _expand(self):
        self._collapsed = False
        if self._tab is not None:
            self._tab.hide()
        self._position_top_center()
        self.show()
        self.raise_()

    def _ensure_expanded(self):
        """Surface the card if the user had it collapsed (used when a new plan or result arrives, so important
        updates pop back down). Not for the voice assistant: there the controller decides (ensure_open), so with
        "Open answers automatically" off its work stays tucked."""
        if self._collapsed and not self._hands_free:
            self._expand()

    def set_level(self, level: float):
        if self._waveform is not None:
            self._waveform.set_level(level)

    def set_refining(self, on: bool):
        self._refining = on

    # ----- layout helpers -----
    def _new_page(self, spacing=10):
        pg = QWidget(self._card)
        pg.setObjectName("ovPage")
        pg.setStyleSheet("QWidget#ovPage{background:transparent;}")
        lay = QVBoxLayout(pg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(spacing)
        pg.hide()
        return pg

    def _show_page(self, page, state):
        """Put `page` in the card (the one before is hidden, and kept: the plan comes back with show_plan_page)."""
        self._cancel_dismiss_timer()
        self._end_typing()
        cur = self._current
        if cur is not page:
            if cur is not None:
                self._body.removeWidget(cur)
                cur.hide()
            self._body.addWidget(page)
            self._current = page
        page.show()
        self._state = state
        self._resize()

    def _clear_layout(self, lay):
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()          # remove from view NOW, not whenever deleteLater runs
                w.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _label(self, text, color=_TITLE, size=15, weight=400, wrap=True):
        lbl = QLabel(text)
        lbl.setWordWrap(wrap)
        if wrap:
            lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lbl.setStyleSheet(
            f"color:{color}; font-size:{size}px; font-weight:{weight}; background:transparent;")
        return lbl

    @staticmethod
    def _fit_text(label, width):
        """Give a wrapping label the height its text needs at `width` (the width the layout gives it), so the page
        — and the panel — grow with a long message instead of cutting it off."""
        label.ensurePolished()
        label.setMinimumHeight(0)        # (QLabel.heightForWidth never answers less than the minimum height)
        h = label.heightForWidth(int(width))
        if h <= 0:
            h = label.sizeHint().height()
        label.setMinimumHeight(h)
        return h

    def _resize(self):
        self._position_top_center()

    def _relayout(self):
        """Drop every layout's cached size before measuring. A nested layout keeps its height-for-width until Qt's
        queued LayoutRequest runs, so measuring right after a page or text change got the OLD page's height (a
        one-line question in a panel sized for three, the gap spread above and below it)."""
        try:
            self._invalidate_tree(self.layout())
        except RuntimeError:
            pass

    @classmethod
    def _invalidate_tree(cls, lay):
        """invalidate() clears a layout's own cache; a container widget's cached size in its parent layout needs
        updateGeometry()."""
        for i in range(lay.count()):
            it = lay.itemAt(i)
            w = it.widget()
            if w is not None:
                if w.isHidden():
                    continue
                w.updateGeometry()
                if isinstance(w, QScrollArea) and w.widget() is not None:
                    w = w.widget()
                    w.updateGeometry()
                if w.layout() is not None:
                    cls._invalidate_tree(w.layout())
            elif it.layout() is not None:
                cls._invalidate_tree(it.layout())
        lay.invalidate()

    _LIST_MAX_H = 300

    def _fit_list(self, scroll):
        """The folder list is as tall as its groups (open or closed), up to 300 px, then it scrolls. Set here, not
        left to QScrollArea: it caches its content's size, so opening a group never grew the list."""
        inner = scroll.widget()
        inner.ensurePolished()               # (the groups' styled borders count toward their height)
        self._invalidate_tree(inner.layout())
        inner.updateGeometry()
        scroll.setFixedHeight(min(self._LIST_MAX_H, max(0, inner.sizeHint().height())))

    def _position_top_center(self):
        try:
            from PySide6.QtGui import QGuiApplication
            scr = QGuiApplication.primaryScreen().availableGeometry()
            self._relayout()
            self.adjustSize()
            x = scr.center().x() - self.width() // 2
            y = scr.top()   # flush with the very top of the screen
            self.move(x, y)
        except Exception:
            pass

    # ----- persistent pages -----
    def _make_thinking_page(self):
        pg = self._new_page()
        row = QHBoxLayout()
        row.setContentsMargins(0, 18, 0, 18)
        row.setSpacing(12)
        row.addStretch()
        self._spinner = Spinner(color=_ACCENT, size=22)
        row.addWidget(self._spinner, 0, Qt.AlignVCenter)
        self._thinking_lbl = self._label("", color=_TEXT, size=15)
        row.addWidget(self._thinking_lbl, 0, Qt.AlignVCenter)
        row.addStretch()
        pg.layout().addLayout(row)
        return pg

    def _fit_thinking(self):
        """One line, centred beside the spinner; a long one ("Reading <a long folder name>, 120 of 300") wraps
        instead of running off the card."""
        lbl = self._thinking_lbl
        lbl.ensurePolished()
        widest = _INNER_W - 22 - 12            # minus the spinner and the gap
        w = min(widest, lbl.fontMetrics().horizontalAdvance(lbl.text()) + 4)
        lbl.setFixedWidth(max(20, w))
        self._fit_text(lbl, lbl.width())

    def _make_listening_page(self):
        pg = self._new_page()
        lay = pg.layout()
        self._waveform = AnimatedWaveform(color=_ACCENT, bars=11, width=_CARD_W - 120,
                                          height=56, bar_width=5.0)
        wrow = QHBoxLayout()
        wrow.addStretch()
        wrow.addWidget(self._waveform)
        wrow.addStretch()
        lay.addLayout(wrow)

        self._listen_caption = self._label("", color=_MUTED, size=13)
        self._listen_caption.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._listen_caption)

        stop = QPushButton("■  Stop")
        stop.setCursor(Qt.PointingHandCursor)
        stop.setMinimumHeight(46)
        stop.setStyleSheet(self._primary_btn())
        stop.clicked.connect(self.refine_requested)
        lay.addWidget(stop)
        return pg

    def _glyph_row(self, lay):
        """[glyph | wrapping message] — the glyph column is fixed, so the message's width (and so its height) is
        known before the page is laid out. The glyph is one text line tall, centred on the message's first line."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(_GLYPH_GAP)
        msg = self._label("", color=_TEXT, size=14)
        msg.ensurePolished()
        glyph = QLabel()
        glyph.setFixedSize(_GLYPH_W, max(20, msg.fontMetrics().lineSpacing()))
        glyph.setAlignment(Qt.AlignCenter)
        row.addWidget(glyph, 0, Qt.AlignTop)
        row.addWidget(msg, 1)
        lay.addLayout(row)
        return glyph, msg

    @staticmethod
    def _set_glyph(glyph, kind):
        """✓ (painted: the font's ✓ came out as a thin √), ⚠ (Segoe UI Symbol: a flat glyph, not the colour
        emoji), or an accent "?" (it's asking, not failing)."""
        if kind == "check":
            glyph.setText("")
            glyph.setPixmap(line_pixmap("check", 18, _ACCENT_HD))
            glyph.setStyleSheet("background:transparent;")
            return
        glyph.setPixmap(QPixmap())
        if kind == "question":
            glyph.setText("?")
            glyph.setStyleSheet(f"color:{_ACCENT_HD}; font-size:17px; font-weight:800; background:transparent;")
        else:
            glyph.setText("⚠")
            glyph.setStyleSheet(f"color:{_WARN}; font-size:16px; background:transparent; "
                                "font-family:'Segoe UI Symbol','Segoe UI';")

    def _make_done_page(self):
        pg = self._new_page(spacing=14)
        lay = pg.layout()
        self._done_glyph, self._done_label = self._glyph_row(lay)
        self._set_glyph(self._done_glyph, "check")
        self._done_undo_btn = QPushButton("  Undo")
        self._done_undo_btn.setIcon(QIcon(_undo_pixmap("#C3C3CE")))
        self._done_undo_btn.setIconSize(QSize(16, 16))
        self._done_undo_btn.setObjectName("ovUndoBtn")
        self._done_undo_btn.setCursor(Qt.PointingHandCursor)
        self._done_undo_btn.setFixedHeight(42)
        self._done_undo_btn.setStyleSheet(self._ghost_btn("ovUndoBtn"))
        self._done_undo_btn.clicked.connect(self.undo_clicked.emit)
        self._done_undo_btn.setVisible(False)
        lay.addWidget(self._done_undo_btn)
        return pg

    def _make_error_page(self):
        pg = self._new_page(spacing=14)
        lay = pg.layout()
        self._error_glyph, self._error_label = self._glyph_row(lay)
        self._set_glyph(self._error_glyph, "warn")
        self._error_pick_btn = QPushButton("📁  Choose folder…")
        self._error_pick_btn.setCursor(Qt.PointingHandCursor)
        self._error_pick_btn.setMinimumHeight(46)
        self._error_pick_btn.setStyleSheet(self._primary_btn())
        self._error_pick_btn.clicked.connect(self.change_folder_requested)
        lay.addWidget(self._error_pick_btn)
        self._error_close_btn = QPushButton("Close")
        self._error_close_btn.setObjectName("ovErrClose")
        self._error_close_btn.setCursor(Qt.PointingHandCursor)
        self._error_close_btn.setMinimumHeight(44)
        self._error_close_btn.setStyleSheet(self._ghost_btn("ovErrClose"))
        self._error_close_btn.clicked.connect(self.dismiss)
        lay.addWidget(self._error_close_btn)
        return pg

    def _make_confirm_page(self):
        """Ask before something big: a question, Cancel, and one main button."""
        pg = self._new_page(spacing=16)
        lay = pg.layout()
        self._confirm_label = self._label("", color=_QUESTION, size=14)
        lay.addWidget(self._confirm_label)
        row = QHBoxLayout()
        row.setSpacing(10)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("ovConfirmCancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setFixedHeight(44)
        cancel.setMinimumWidth(104)
        cancel.setStyleSheet(self._ghost_btn("ovConfirmCancel"))
        cancel.clicked.connect(self._on_dismiss)
        row.addWidget(cancel, 0)
        self._confirm_cancel_btn = cancel
        self._confirm_btn = QPushButton("Continue")
        self._confirm_btn.setCursor(Qt.PointingHandCursor)
        self._confirm_btn.setFixedHeight(44)
        self._confirm_btn.setStyleSheet(self._primary_btn())
        self._confirm_btn.clicked.connect(self.confirm_clicked.emit)
        row.addWidget(self._confirm_btn, 1)
        lay.addLayout(row)
        return pg

    # ================= STATES =================
    def show_thinking(self, msg="Analyzing your files…"):
        # One persistent spinner page: rapid status updates just change the TEXT (recreating the Spinner each
        # time left ghost spinners overlapping and reset the animation every frame).
        self._thinking_lbl.setText(msg or "")
        self._fit_thinking()
        self._show_page(self._page_thinking, "thinking")

    def show_applying(self):
        self.show_thinking("Organizing…")
        self._state = "applying"

    def show_listening(self, prompt="Listening… tap Stop when you're done"):
        self._listen_caption.setText(prompt or "")
        self._fit_text(self._listen_caption, _INNER_W)
        self._show_page(self._page_listening, "listening")

    def show_plan(self, summary, folders, target_folder, file_count, folder_count,
                  move_count=None, action="Organize"):
        """`folders`: {name: [(filename, is_moving), ...]} (bare names count as moving). `move_count`: how many
        actually move (defaults to file_count). `action`: the main button's verb — "Turn on and organize"."""
        self._ensure_expanded()
        self._organize_armed = False
        if move_count is None:
            move_count = file_count
        old = self._page_plan
        self._plan_action = action or "Organize"
        self._page_plan = self._build_plan_page(summary, folders or {}, target_folder, int(move_count or 0),
                                                self._plan_action)
        self._show_page(self._page_plan, "plan")
        if old is not None:
            old.hide()
            old.deleteLater()

    def show_plan_page(self):
        """Re-show the plan already built, without rebuilding it — e.g. after a refine recording that caught
        nothing, so the panel doesn't get stuck on 'Listening…'."""
        if self._page_plan is None:
            return
        self._ensure_expanded()
        self._show_page(self._page_plan, "plan")

    def _build_plan_page(self, summary, folders, target_folder, move_count, action):
        pg = self._new_page(spacing=10)
        lay = pg.layout()

        title = self._label(summary, color=_TITLE, size=19, weight=700)
        self._fit_text(title, _INNER_W)
        lay.addWidget(title)

        # Target folder card: glyph + path + Change (purple text link)
        tcard = QFrame()
        tcard.setObjectName("ovTarget")
        tcard.setStyleSheet(
            f"QFrame#ovTarget{{background:{_CARD2}; border:1px solid {_CARD2_BRD}; border-radius:10px;}}")
        tl = QHBoxLayout(tcard)
        tl.setContentsMargins(14, 11, 12, 11)
        tl.setSpacing(10)
        gl = QLabel()
        gl.setPixmap(line_pixmap("folder", 17, "#C9C9D4"))
        gl.setFixedWidth(20)
        gl.setStyleSheet("background:transparent;")
        tl.addWidget(gl)
        tpath = QLabel()
        tpath.setStyleSheet("color:#C9C9D4; font-size:13px; background:transparent;")
        change = QPushButton("Change")
        change.setCursor(Qt.PointingHandCursor)
        change.setStyleSheet(
            f"QPushButton{{background:transparent; color:{_ACCENT}; border:none; "
            f"font-size:13px; font-weight:600; padding:0 2px;}} QPushButton:hover{{color:#A58BFF;}}")
        change.clicked.connect(self.change_folder_requested)
        # A long path used to lose its END — the part that says which folder. ~ for home, shorten the middle.
        tpath.ensurePolished()
        change.ensurePolished()
        room = _INNER_W - 2 - 14 - 12 - 20 - 2 * 10 - change.sizeHint().width()
        tpath.setText(short_path(display_path(target_folder), tpath.fontMetrics(), room))
        tpath.setToolTip(target_folder or "")
        self._target_label = tpath
        tl.addWidget(tpath, 1)
        tl.addWidget(change)
        lay.addWidget(tcard)

        # Collapsible folder groups (scroll if many)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{border:none; background:transparent;}")
        inner = QWidget()
        inner.setObjectName("ovGroups")
        inner.setStyleSheet("QWidget#ovGroups{background:transparent;}")
        ilay = QVBoxLayout(inner)
        ilay.setContentsMargins(0, 0, 0, 0)
        ilay.setSpacing(8)
        for name, filenames in folders.items():
            ilay.addWidget(self._folder_group(name, filenames or [], scroll))
        ilay.addStretch()
        scroll.setWidget(inner)
        self._plan_list = scroll
        self._fit_list(scroll)
        lay.addWidget(scroll)

        # "Change by voice": the mic (hotkey), or what to say (voice assistant — its mic is already on)
        hint = QHBoxLayout()
        hint.setSpacing(10)
        self._refine_btn = QPushButton(pg)
        self._refine_btn.setCursor(Qt.PointingHandCursor)
        self._refine_btn.setFixedSize(34, 34)
        self._refine_btn.setIcon(QIcon(line_pixmap("mic", 16, "#FFFFFF")))
        self._refine_btn.setIconSize(QSize(16, 16))
        self._refine_btn.setStyleSheet(
            f"QPushButton{{background:{_ACCENT}; border:none; border-radius:17px;}}"
            f"QPushButton:hover{{background:#8F63FF;}}")
        self._refine_btn.clicked.connect(self.refine_requested)
        if self._hands_free:
            self._refine_btn.setVisible(False)
            text = "Say “yes”, “no”, or what to change"
        else:
            hint.addWidget(self._refine_btn)
            text = "Change by voice — tap the mic"
        self._refine_hint = self._label(text, color=_MUTED, size=13, wrap=False)
        hint.addWidget(self._refine_hint)
        hint.addStretch()
        lay.addLayout(hint)
        self.plan_field = None
        if self._hands_free:                 # (speech mishears names: you can type the change too)
            self.plan_field = self._typing_box("Or type what to change and press Enter")
            lay.addWidget(self.plan_field)

        # Cancel / Organize — the button reflects the ACTUAL number of files that will move (not the total shown
        # above), and the big-batch two-click confirm triggers on the real move count.
        big = move_count >= self._BIG_PLAN
        footer = QHBoxLayout()
        footer.setSpacing(10)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("ovPlanCancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setMinimumHeight(46)
        cancel.setStyleSheet(self._ghost_btn("ovPlanCancel"))
        cancel.clicked.connect(self.dismiss)
        footer.addWidget(cancel)

        self._organize_btn = QPushButton()
        self._organize_btn.setMinimumHeight(46)
        if move_count == 0:
            # Already arranged this way — not a dead-end. The user organized on purpose, so invite a new
            # arrangement (by the refine mic, or just by saying it).
            self._organize_btn.setText("Already arranged — say what to change" if self._hands_free
                                       else "Already arranged — tap the mic to change")
            self._organize_btn.setEnabled(False)
            self._organize_btn.setStyleSheet(
                "QPushButton{background:#1E1E28; color:#7E7E8C; border:1px solid #2C2C38; "
                "border-radius:12px; font-size:14px; font-weight:600;}")
        else:
            if action == "Organize":
                self._organize_btn.setText(f"✓  Organize — {move_count} moving")
            else:
                self._organize_btn.setText(f"✓  {action} {move_count} file{'s' if move_count != 1 else ''}")
            self._organize_btn.setCursor(Qt.PointingHandCursor)
            self._organize_btn.setStyleSheet(self._primary_btn())
            self._organize_btn.clicked.connect(lambda: self._on_organize(big, move_count))
        footer.addWidget(self._organize_btn, 1)
        lay.addLayout(footer)
        return pg

    def _on_organize(self, big, move_count):
        if big and not self._organize_armed:
            self._organize_armed = True
            self._organize_btn.setText(f"Move {move_count} files? Click to confirm")
            self._organize_btn.setStyleSheet(self._primary_btn(confirm=True))
            return
        self.organize_clicked.emit()

    def show_done(self, revert_hint="Revert anytime in History.", undo=False):
        """`undo`: an "↩ Undo" button (emits undo_clicked), and 20 s before it closes by itself."""
        self._ensure_expanded()
        msg = revert_hint or "Done."
        self._done_label.setText(msg)
        self._fit_text(self._done_label, _ROW_TEXT_W)
        self._done_undo_btn.setVisible(bool(undo))
        self._show_page(self._page_done, "done")
        # Auto-dismiss, but keep it up long enough to READ a long message (scaled with length) — or to reach
        # for Undo. Cancellable so a new request within the window isn't closed by this stale timer.
        self._start_dismiss_timer(20000 if undo else max(4000, min(9000, 2500 + len(msg) * 55)))

    def show_error(self, msg, allow_pick_folder=False, question=False, buttons=True):
        """`question`: it's asking you something (an accent "?"), not failing (the ⚠). `buttons` False: just the message
        (voice search's "I couldn't find that.", which closes by itself)."""
        self._ensure_expanded()
        self._set_glyph(self._error_glyph, "question" if question else "warn")
        self._error_label.setText(msg or "Something went wrong.")
        self._fit_text(self._error_label, _ROW_TEXT_W)
        self._error_pick_btn.setVisible(bool(allow_pick_folder) and buttons)
        self._error_close_btn.setVisible(not allow_pick_folder and buttons)
        self._error_close_btn.setText("Cancel" if question else "Close")
        self._show_page(self._page_error, "error")

    # ----- voice search: the same panel, "+ Find" -----
    def take_over(self, brand: str) -> None:
        """Another job is using the panel: its name at the top (and on the tucked handle), and nothing left counting
        down to close it."""
        self._cancel_dismiss_timer()
        self._brand.setText(brand)
        if self._tab is not None:
            self._tab.set_text(brand)

    def minimize(self) -> None:
        """Tuck it away into the handle at the top edge (the Mac's name)."""
        if self.isVisible() and not self._collapsed:
            self._collapse()

    def show_results(self, message, rows, chips=(), show_all=False, scroll=False, field=""):
        """Files it found: rows = [{"name", "sub", "ext"}, ...]. `chips`: short answers to the question in `message`
        (at most 3, never wider than the panel). `scroll`: the whole list ("Show all"), scrolling after five and a half
        rows. `field`: a typing box (only when it asked something in your own words)."""
        self._ensure_expanded()
        self._end_typing()
        pg = self._page_results
        lay = pg.layout()
        self._clear_layout(lay)
        q = self._label(message or "", color=_QUESTION, size=14)
        q.setTextFormat(Qt.PlainText)
        self._fit_text(q, _INNER_W)
        lay.addWidget(q)
        lay.addSpacing(4)
        self._results_message = q
        self.result_rows, self.result_list, self.result_chips = [], None, {}
        self.result_field = self.result_all_btn = None
        rows = list(rows or [])
        target, width = lay, _INNER_W
        if scroll and len(rows) * (_F_ROW_H + _F_GAP) - _F_GAP > _F_LIST_H:
            area = self.result_list = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            area.setFixedHeight(_F_LIST_H)
            area.setStyleSheet(
                "QScrollArea{border:none; background:transparent;}"
                "QScrollBar:vertical{background:transparent; width:6px; margin:0;}"
                f"QScrollBar::handle:vertical{{background:{_F_MUTED}; border-radius:3px; min-height:30px;}}"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical{height:0;}"
                "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical{background:transparent;}")
            host = QWidget()
            host.setObjectName("ovResultHost")
            host.setStyleSheet("QWidget#ovResultHost{background:transparent;}")
            target = QVBoxLayout(host)
            target.setContentsMargins(0, 0, _F_SCROLL_ROOM, 0)
            target.setSpacing(_F_GAP)
            area.setWidget(host)
            lay.addWidget(area)
            width = _INNER_W - _F_SCROLL_ROOM
        for n, info in enumerate(rows, 1):
            row = _ResultRow(n, info, self.result_opened, self.result_revealed, width)
            target.addWidget(row)
            self.result_rows.append(row)
        if self.result_list is not None:
            target.addStretch(1)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 2, 0, 0)
        foot.setSpacing(8)
        room = _INNER_W - ((self._foot_button("Show all").sizeHint().width() + 8) if show_all else 0)
        for label in list(chips or ())[:3]:
            b = self._foot_button(str(label), chip=True)
            need = b.sizeHint().width() + 8
            if need > room:                  # (never wider than the panel)
                b.deleteLater()
                break
            room -= need
            b.clicked.connect(lambda _=False, t=str(label): self.result_chip.emit(t))
            foot.addWidget(b, 0)
            self.result_chips[str(label)] = b
        if self.result_chips:
            foot.addStretch(1)
        if show_all:
            self.result_all_btn = self._foot_button("Show all")
            self.result_all_btn.clicked.connect(self.results_all.emit)
            foot.addWidget(self.result_all_btn, 0 if self.result_chips else 1)
        if not self.result_chips:
            close = self._foot_button("Close")
            close.clicked.connect(self._on_dismiss)
            foot.addWidget(close, 1)
        lay.addLayout(foot)
        if field:
            self.result_field = self._typing_box(field)
            lay.addWidget(self.result_field)
        self._show_page(pg, "results")

    def _foot_button(self, text, chip=False):
        b = QPushButton(text)
        b.setObjectName("ovFindChip" if chip else "ovFindFoot")
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(34)
        color, border = (_F_LIGHT, _F_PURPLE) if chip else (_F_TEXT, _F_ROW_BRD)
        sel = f"QPushButton#{b.objectName()}"
        b.setStyleSheet(f"{sel}{{background:{_F_ROW_BG}; color:{color}; border:1px solid {border}; border-radius:10px; "
                        f"font-size:12px; font-weight:600; padding:0 14px;}}"
                        f"{sel}:hover{{border-color:{_F_LIGHT};}}")
        b.ensurePolished()
        return b

    def set_result_thumb(self, i, path):
        """A row's small picture, once it's made (a background thread makes them)."""
        rows = self.result_rows or []
        try:
            if 0 <= i < len(rows):
                pm = QPixmap(path)
                if not pm.isNull():
                    dpr = max(1.0, float(self.devicePixelRatioF()))
                    target = QSize(int(34 * dpr), int(42 * dpr))
                    pm = pm.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    pm.setDevicePixelRatio(dpr)
                    th = rows[i].thumb
                    th.setText("")
                    th.setStyleSheet(f"background:{_F_ROW_BRD}; border-radius:5px; border:none;")
                    th.setPixmap(pm)
        except RuntimeError:                 # (the panel moved on)
            pass

    def show_confirm(self, message, button):
        """A question, Cancel (closes it, like ✕) and one main button (emits confirm_clicked)."""
        self._ensure_expanded()
        self._confirm_label.setText(message or "")
        self._fit_text(self._confirm_label, _INNER_W)
        self._confirm_btn.setText(button or "Continue")
        self._show_page(self._page_confirm, "confirm")

    def show_choice(self, question, options, field=""):
        """A question with a few answers: options = [(key, label), ...], each a full-width button emitting
        choice_made(key). `field`: a text box with that placeholder above them; Return emits text_submitted.
        A Cancel link closes it."""
        self._ensure_expanded()
        self._end_typing()
        pg = self._page_choice
        lay = pg.layout()
        self._clear_layout(lay)
        q = self._label(question or "", color=_QUESTION, size=14)
        self._fit_text(q, _INNER_W)
        lay.addWidget(q)
        lay.addSpacing(6)
        self._choice_question = q
        self.choice_field = None
        if field:
            self.choice_field = self._typing_box(field)
            lay.addWidget(self.choice_field)
        self.choice_buttons = {}
        for key, label in options or []:
            b = QPushButton(label)
            b.setObjectName("ovChoiceBtn")
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(44)
            b.setStyleSheet(
                f"QPushButton#ovChoiceBtn{{text-align:left; padding:0 16px; background:{_CARD2}; color:{_QUESTION}; "
                f"border:1px solid {_CARD2_BRD}; border-radius:10px; font-size:14px; font-weight:600;}}"
                f"QPushButton#ovChoiceBtn:hover{{border-color:{_ACCENT}; color:#FFFFFF;}}")
            b.clicked.connect(lambda _=False, k=key: self.choice_made.emit(str(k)))
            lay.addWidget(b)
            self.choice_buttons[key] = b
        cancel = QPushButton("Cancel")
        cancel.setObjectName("ovChoiceCancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setFixedHeight(34)
        cancel.setStyleSheet(
            f"QPushButton#ovChoiceCancel{{background:transparent; border:none; color:{_MUTED}; "
            f"font-size:13px; font-weight:600; padding:0 16px;}}"
            f"QPushButton#ovChoiceCancel:hover{{color:#FFFFFF;}}")
        cancel.clicked.connect(self._on_dismiss)
        lay.addWidget(cancel, 0, Qt.AlignHCenter)
        self._choice_cancel_btn = cancel
        self._show_page(pg, "choice")

    def _typing_box(self, placeholder):
        """For anything it asks in your own words, in case your voice wasn't understood (Mac bug 46). Enter sends
        it (text_submitted)."""
        f = _TypeBox(self._key_for_typing)
        f.setObjectName("ovChoiceField")
        f.setPlaceholderText(placeholder)
        f.setFixedHeight(44)
        f.setStyleSheet(
            f"QLineEdit#ovChoiceField{{background:{_CARD2}; color:{_TITLE}; border:1px solid {_CARD2_BRD}; "
            f"border-radius:10px; padding:0 14px; font-size:14px; "
            f"selection-background-color:{_ACCENT};}}"
            f"QLineEdit#ovChoiceField:focus{{border-color:{_ACCENT};}}")
        f.returnPressed.connect(lambda f=f: self._on_field_submit(f))
        return f

    def _on_field_submit(self, f):
        text = (f.text() or "").strip()
        f.clear()                            # (before sending: pressing Enter twice mustn't send it twice)
        if not text:
            return
        self._end_typing()
        self.text_submitted.emit(text)

    # ----- pieces -----
    def _folder_group(self, name, entries, scroll=None):
        # Each entry is (filename, is_moving); accept bare strings too (treated as moving), as the Mac sends.
        norm = [(e if isinstance(e, (tuple, list)) else (e, True)) for e in entries]
        total = len(norm)
        wrap = QFrame()
        wrap.setObjectName("ovGroup")
        wrap.setStyleSheet(
            f"QFrame#ovGroup{{background:{_CARD2}; border:1px solid {_CARD2_BRD}; border-radius:10px;}}")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        header = QPushButton()
        header.setObjectName("ovGroupHeader")
        header.setCursor(Qt.PointingHandCursor)
        header.setStyleSheet(
            "QPushButton#ovGroupHeader{background:transparent; color:#E8E8F0; border:none; "
            "font-size:14px; font-weight:600; text-align:left; padding:0;}")
        # A long folder name made the header wider than the list, which then cut off the right edge of EVERY
        # group card: shorten the name to fit (the full name is the tooltip).
        header.ensurePolished()
        hfm = header.fontMetrics()
        room = _INNER_W - 2 - 28 - 12 - hfm.horizontalAdvance(f"▾    ({total})")   # border, margins, scrollbar
        shown = hfm.elidedText(str(name), Qt.ElideMiddle, max(60, room))
        header.setText(f"▸  {shown}  ({total})")
        if shown != str(name):
            header.setToolTip(str(name))
        body = QWidget()
        body.setObjectName("ovGroupBody")
        body.setVisible(False)
        body.setStyleSheet("QWidget#ovGroupBody{background:transparent;}")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(14, 4, 0, 2)
        blay.setSpacing(1)
        built = []

        def build():
            # (Built on first open: a big plan has thousands of names, and most groups are never opened.)
            room = _INNER_W - 2 - 28 - 14 - 12            # card border, group margins, indent, scrollbar
            for fn, moving in norm[:self._MAX_NAMES]:
                fl = QLabel()
                fl.setToolTip(str(fn))
                if moving:
                    fl.setStyleSheet("color:#9A9AA8; font-size:12px; background:transparent;")
                    tail = ""
                else:
                    # Already in this folder — dimmed, tagged, so you see the full end-state but know it
                    # won't move.
                    fl.setStyleSheet("color:#5E5E6B; font-size:12px; background:transparent;")
                    tail = "   · already here"
                fl.ensurePolished()
                fm = fl.fontMetrics()
                fl.setText("• " + fm.elidedText(str(fn), Qt.ElideMiddle,
                                                 room - fm.horizontalAdvance("• " + tail)) + tail)
                blay.addWidget(fl)
            if total > self._MAX_NAMES:
                more = QLabel(f"… and {total - self._MAX_NAMES} more")
                more.setStyleSheet("color:#7A7A90; font-size:12px; background:transparent;")
                blay.addWidget(more)
            built.append(True)

        def toggle():
            if not built:
                build()
            vis = not body.isVisible()
            body.setVisible(vis)
            header.setText(f"{'▾' if vis else '▸'}  {shown}  ({total})")
            if scroll is not None:
                self._fit_list(scroll)
            self._resize()
        header.clicked.connect(toggle)
        lay.addWidget(header)
        lay.addWidget(body)
        return wrap

    def _ghost_btn(self, name=None):
        sel = f"QPushButton#{name}" if name else "QPushButton"
        return (f"{sel}{{background:transparent; color:#C3C3CE; border:1px solid #33333F; "
                "border-radius:11px; font-size:14px; font-weight:600; padding:0 22px;}"
                f"{sel}:hover{{border-color:#7C4DFF; color:#FFFFFF;}}")

    def _primary_btn(self, confirm=False):
        c0, c1 = ("#6E3BEF", "#8257FF") if confirm else ("#7C4DFF", "#9165FF")
        return (f"QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0, "
                f"stop:0 {c0}, stop:1 {c1}); color:white; border:none; border-radius:12px; "
                f"font-size:15px; font-weight:700; padding:0 20px;}}"
                f"QPushButton:hover{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0, "
                f"stop:0 {c1}, stop:1 {c1});}}")


class _CollapsedTab(QWidget):
    """The small pill the overlay tucks into at the very top of the screen when
    collapsed. Clicking it (i.e. clicking the top edge) pops the overlay back
    down. Non-activating, always-on-top, mouse-driven."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(188, 28)
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("collTab")
        # Flat top, rounded bottom — reads as "tucked into the top edge".
        card.setStyleSheet(
            f"QFrame#collTab{{background:{_BG}; border:1px solid {_BORDER}; "
            f"border-top:none; border-top-left-radius:0; border-top-right-radius:0; "
            f"border-bottom-left-radius:12px; border-bottom-right-radius:12px;}}")
        lay.addWidget(card)
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 2, 12, 5)
        row.setSpacing(6)
        row.addStretch()
        t = self._text = QLabel("+ Organize")
        t.setStyleSheet(f"color:{_ACCENT_HD}; font-size:12px; font-weight:700; background:transparent;")
        row.addWidget(t)
        cv = QLabel("▾")
        cv.setStyleSheet(f"color:{_ACCENT_HD}; font-size:12px; background:transparent;")
        row.addWidget(cv)
        row.addStretch()

    def showEvent(self, e):
        super().showEvent(e)
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass

    def set_text(self, text):
        """The job using the panel ("+ Organize" / "+ Find")."""
        self._text.setText(text)

    def present(self):
        try:
            from PySide6.QtGui import QGuiApplication
            scr = QGuiApplication.primaryScreen().availableGeometry()
            x = scr.center().x() - self.width() // 2
            self.move(x, scr.top())
        except Exception:
            pass
        self.show()
        self.raise_()

    def mousePressEvent(self, e):
        self.clicked.emit()
        super().mousePressEvent(e)
