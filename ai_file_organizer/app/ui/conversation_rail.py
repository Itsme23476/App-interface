"""Left-edge conversation rail — the cockpit for concurrent agents, styled after VoiceOS's drawer.

At rest: a slim dark see-through sliver against the left screen edge. Hover → the sliver grows (a cue,
never opens). CLICK → a near-black drawer slides out, flush on the edge with corners that curve into it:
pill search, "New chat", then the chats grouped like Claude's sidebar — Running (working or
waiting on you), Today / Previous 7 days / Previous 30 days / one group per month — one line each with a
round icon. It tucks away shortly after the mouse leaves (not while you're searching). Hover is read
by polling the cursor (macOS doesn't deliver enter/leave to an inactive app's non-activating panel).
Selecting a conversation shows it in the notch. Same non-activating, all-Spaces, no-focus-steal panel
mechanics as the notch (`notch_panel.configure_macos_panel`), so it never steals focus from your app.
"""
from __future__ import annotations
import sys
import time
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QPoint, QEasingCurve, QRect
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
                               QScrollArea)

import logging
import sys

IS_WIN = sys.platform.startswith("win")
if IS_WIN:
    from app.ui import win_panel as notch_panel      # Windows twin of the Mac panel mechanics (same names)
else:
    from app.ui import notch_panel
from app.ui.claude_overlay import _TypeField, _glyph   # _TypeField: a QLineEdit that runs a callback on click

logger = logging.getLogger(__name__)


def _cursor_pos():
    from PySide6.QtGui import QCursor
    return QCursor.pos()

RAIL_TITLE = "Filect Conversations"   # unique — notch_panel finds the NSWindow by this
RAIL_W = 240                          # window width: the ~224px drawer (VoiceOS-like proportions) + the tab
EDGE = 7                              # px of the rail left peeking when collapsed (the hover strip)
TAB_W = 16                            # the edge tab: hover slides it out this far (EDGE → TAB_W)
FLARE = 18                            # the drawer's inward-curved corners where it meets the screen edge
PANEL_R = 20                          # its rounded right corners
PANEL_FRAC = 0.5                      # drawer height, as a share of the screen (fixed; the list scrolls)
ANIM_MS = 160
_RUNNING = ("working", "needs_you")

# status -> (dot colour, label). Running states are coloured; finished history is neutral so viewing an
# old chat doesn't change how it looks — only errors keep their colour.
_STATUS = {
    "working":   ("#7C4DFF", "Working"),
    "needs_you": ("#E6A23C", "Needs you"),
    "done":      ("#6E6E78", ""),
    "error":     ("#F2645A", "Error"),
    "stopped":   ("#6E6E78", "Stopped"),
    "recent":    ("#6E6E78", ""),           # a past chat from disk
}
_HDR_CSS = "color:#86868F;font-size:11px;font-weight:500;background:transparent;"
_SCROLL_CSS = (
    "QScrollArea{background:transparent;border:none;}"
    "QScrollBar:vertical{background:transparent;width:6px;margin:0;}"
    "QScrollBar::handle:vertical{background:rgba(255,255,255,0.14);border-radius:3px;min-height:24px;}"
    "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
    "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}")


def _elapsed(since: float) -> str:
    s = max(0, int(time.time() - since))
    return f"{s // 60}:{s % 60:02d}"


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 3600:
        return f"{max(1, s // 60)}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    if s < 172800:
        return "Yesterday"
    return f"{s // 86400}d ago"


def _group(sessions, recent, now=None):
    """[(header, [(kind, obj), ...]), ...] in display order, like Claude's sidebar. RUNNING = this run's
    chats working or waiting on you; everything else — this run's finished chats ("session") and past
    chats from disk ("recent") — newest first, by the calendar day of last activity: TODAY, PREVIOUS
    7 DAYS, PREVIOUS 30 DAYS, then one group per month ("SEPTEMBER"; "DECEMBER 2025" once it's not
    this year). Empty groups are omitted."""
    now = now if now is not None else time.time()
    today = datetime.fromtimestamp(now)
    start = today.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    running = sorted((s for s in sessions if s.status in _RUNNING), key=lambda s: s.started_at, reverse=True)
    rest = [(s.started_at, "session", s) for s in sessions if s.status not in _RUNNING]
    rest += [(r.mtime, "recent", r) for r in recent]
    rest.sort(key=lambda t: t[0], reverse=True)
    grouped = {}                        # insertion order = newest group first (rest is newest-first)
    for t, kind, obj in rest:
        if t >= start:
            name = "TODAY"
        elif t >= start - 7 * 86400:
            name = "PREVIOUS 7 DAYS"
        elif t >= start - 30 * 86400:
            name = "PREVIOUS 30 DAYS"
        else:
            d = datetime.fromtimestamp(t)
            name = d.strftime("%B").upper() + ("" if d.year == today.year else f" {d.year}")
        grouped.setdefault(name, []).append((kind, obj))
    out = [("RUNNING", [("session", s) for s in running])] if running else []
    return out + list(grouped.items())


def _matches(query: str, *fields) -> bool:
    """Every word of the query appears somewhere in the fields (case-insensitive)."""
    hay = " ".join(f or "" for f in fields).lower()
    return all(w in hay for w in query.lower().split())


def _row_icon(status: str, px: int = 18, agent: str = "claude"):
    """A row's round icon (VoiceOS-style): the Claude logo if one is bundled, else a soft lavender
    circle — a Codex chat gets a light terminal mark instead; a small badge marks a chat that's working
    (purple) / waiting on you (amber) / failed (red)."""
    from PySide6.QtGui import QPixmap, QPainter, QColor, QRadialGradient, QFont
    from PySide6.QtCore import QRectF
    from app.ui.claude_overlay import _load_logo
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    logo = _load_logo(px) if agent != "codex" else None
    if agent == "codex":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(236, 236, 240))
        p.drawEllipse(QRectF(0.5, 0.5, px - 1, px - 1))
        f = QFont()
        f.setPixelSize(int(px * 0.5))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(17, 17, 20))
        p.drawText(QRectF(0, 0, px, px - 1), Qt.AlignCenter, ">_")
    elif logo is not None:
        p.drawPixmap(0, 0, px, px, logo)
    else:
        g = QRadialGradient(px * .35, px * .3, px * .8)
        g.setColorAt(0, QColor(236, 228, 255))
        g.setColorAt(1, QColor(196, 178, 255))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QRectF(0.5, 0.5, px - 1, px - 1))
    badge = {"working": QColor(124, 77, 255), "needs_you": QColor(230, 162, 60),
             "error": QColor(242, 100, 90)}.get(status)
    if badge is not None:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 10, 12))
        p.drawEllipse(QRectF(px - 8, px - 8, 8, 8))     # dark ring so the dot reads on the icon
        p.setBrush(badge)
        p.drawEllipse(QRectF(px - 7, px - 7, 6, 6))
    p.end()
    return pm


class _EdgeTab(QWidget):
    """The rail's tab at the screen edge, VoiceOS-style: at rest a slim dark see-through sliver against
    the edge; on hover the rail slides out a little and the sliver grows wider, taller and darker — the
    cue that a click opens it. Clicks fall through to the rail."""
    def paintEvent(self, e):
        from PySide6.QtGui import QPainter, QColor, QPen
        from PySide6.QtCore import QRectF
        rail = self.parent()
        if getattr(rail, "_expanded", False):
            return                                   # open: the drawer is the UI, no tab
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(0, 0, 0, 2))  # alpha 2: invisible, but keeps the strip clickable
        cue = getattr(rail, "_cue", False)
        # Draw from the SCREEN edge: tucked, it's at x = TAB_W - EDGE in here; slid out (cue), at x = 0.
        x0 = 0 if cue else TAB_W - EDGE
        w = 8 if cue else 4
        h = (self.height() - 2 * FLARE) * (0.66 if cue else 0.58)
        p.setPen(QPen(QColor(255, 255, 255, 45 if cue else 28), 1))
        p.setBrush(QColor(0, 0, 0, 150 if cue else 115))
        p.drawRoundedRect(QRectF(x0 - 6, (self.height() - h) / 2, w + 6, h), w / 2 + 1, w / 2 + 1)


class _RailRow(QFrame):
    """One conversation, VoiceOS-style: a round icon + the title on one line (status / when live in the
    icon badge + the tooltip). Clickable."""
    clicked = Signal()

    def __init__(self, title, status, when, focused, agent="claude", parent=None):
        super().__init__(parent)
        self.setObjectName("railRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 7, 8, 7)
        lay.setSpacing(10)
        self._icon = QLabel()
        self._icon.setFixedSize(18, 18)
        lay.addWidget(self._icon, 0, Qt.AlignVCenter)
        self._title = QLabel()
        self._title.setStyleSheet("color:#EDEDED;font-size:12.5px;font-weight:500;")
        lay.addWidget(self._title, 1)
        self._tag = QLabel("Codex")              # which agent runs this chat (Claude = the default, no tag)
        self._tag.setStyleSheet("color:#9A9AA6;font-size:10px;font-weight:600;padding:0 5px;"
                                "border:1px solid rgba(255,255,255,0.16);border-radius:6px;")
        self._tag.setVisible(False)
        lay.addWidget(self._tag, 0, Qt.AlignVCenter)
        self._state = None
        self.set_state(title, status, when, focused, agent)

    def set_state(self, title, status, when, focused, agent="claude"):
        """Update in place — the rail REUSES rows (rebuilding ~70 styled rows took ~60ms of UI freeze on
        every agent step and twice when a task finished). Only what changed is touched."""
        label = _STATUS.get(status, _STATUS["working"])[1]
        if status == "working":
            sub = f"{label} · {_elapsed(when)}"
        elif status == "needs_you":
            sub = label
        elif label:                              # error / stopped: say so, plus when
            sub = f"{label} · {_ago(when)}"
        else:                                    # finished / past: just when
            sub = _ago(when)
        title = title or "Untitled"
        codex = agent == "codex"
        self.setToolTip(f"{title}\n{sub}" + (" · Codex" if codex else ""))
        old = self._state or (None, None, None, None)
        if focused != old[2]:                    # stylesheet parsing is the costly part — only on change
            bg = "rgba(124,77,255,0.20)" if focused else "transparent"
            self.setStyleSheet(
                f"QFrame#railRow{{background:{bg};border:none;border-radius:10px;}}"
                "QFrame#railRow:hover{background:rgba(255,255,255,0.07);} "
                "QFrame#railRow QLabel{background:transparent;border:none;}")
        if status != old[1] or agent != old[3]:
            self._icon.setPixmap(_row_icon(status, agent=agent))
            self._tag.setVisible(codex)
        if title != old[0] or agent != old[3]:
            room = RAIL_W - TAB_W - 66 - (44 if codex else 0)    # the Codex tag takes its share
            self._title.setText(self._title.fontMetrics().elidedText(title, Qt.ElideRight, room))
        self._state = (title, status, focused, agent)

    def mousePressEvent(self, e):
        self.clicked.emit()


class ConversationRail(QWidget):
    session_clicked = Signal(str)     # AgentSession.key — a chat from this run
    new_requested = Signal()
    recent_clicked = Signal(str)      # claude session id — a past chat from disk

    def __init__(self, parent=None):
        super().__init__(parent)
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.setInterval(450)    # grace before the drawer tucks away on leave
        self._close_timer.timeout.connect(self._close_if_left)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(RAIL_TITLE)
        self.setFixedWidth(RAIL_W)
        self._rows = []               # every row widget, in display order
        self._row_cache = {}          # ("session", key) / ("recent", id) -> its _RailRow, reused
        self._hdr_cache = {}          # group header text -> its QLabel, reused
        self._empty = None            # the "No chats yet / No matches" label
        self._groups = []             # [(header, row count)] as last rendered
        self._data = ([], None, [])   # (sessions, focused_key, recent) from the last update_sessions
        self._expanded = False
        self._hovered = False
        self._cue = False             # hover cue: tab slid out a little with a grip (click opens)
        self._x_open = 0
        self._x_closed = 0
        self._x_cue = 0
        self._y = 0
        self._anim = QPropertyAnimation(self, b"pos")
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._on_slid)
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(60)   # ponytail: 60ms cursor poll; an NSEvent mouse monitor if CPU ever matters
        self._hover_timer.timeout.connect(self._poll_hover)
        self._build()

    def _build(self):
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QLineEdit
        outer = QVBoxLayout(self)
        # left=0: flush on the screen edge. top/bottom=FLARE: room for the curved corners (painted in
        # paintEvent). right=TAB_W: the edge tab.
        outer.setContentsMargins(0, FLARE, TAB_W, FLARE)
        self._tab = _EdgeTab(self)
        self._card = QFrame()                  # the drawer's content; its dark shape is painted by us
        self._card.setObjectName("railCard")
        self._card.setStyleSheet("QFrame#railCard{background:transparent;border:none;}")
        outer.addWidget(self._card)
        col = QVBoxLayout(self._card)
        col.setContentsMargins(10, 12, 8, 10)
        col.setSpacing(6)
        # Top bar (VoiceOS): a pill search box. Clicking it keys the panel so it can take typing
        # (see _focus_search).
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self._search = _TypeField(self._focus_search)
        self._search.setObjectName("railSearch")
        self._search.setPlaceholderText("Search")
        self._search.setClearButtonEnabled(True)
        self._search.addAction(QIcon(_glyph("search")), QLineEdit.LeadingPosition)
        self._search.setFixedHeight(30)
        self._search.setStyleSheet(
            "QLineEdit#railSearch{background:#232327;color:#ECECEC;border:1px solid transparent;"
            "border-radius:15px;padding:0 8px 0 2px;font-size:12.5px;}"
            "QLineEdit#railSearch:focus{border-color:rgba(124,77,255,0.7);}")
        self._search.textChanged.connect(lambda _t: self._render())
        self._search.installEventFilter(self)
        top.addWidget(self._search, 1)
        col.addLayout(top)
        self._new_btn = QPushButton("New chat")
        self._new_btn.setIcon(QIcon(_glyph("plus")))
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#CFCFD6;border:none;border-radius:10px;"
            "padding:7px 8px;font-size:12.5px;text-align:left;}"
            "QPushButton:hover{background:rgba(255,255,255,0.07);color:#FFFFFF;}")
        self._new_btn.clicked.connect(self.new_requested.emit)
        col.addWidget(self._new_btn)
        # The grouped list scrolls. Every layer is explicitly transparent: an unstyled viewport falls
        # back to the app's LIGHT palette and paints a white box on the dark card.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(_SCROLL_CSS)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.viewport().setStyleSheet("background:transparent;")
        self._list = QWidget()
        self._list.setObjectName("railList")
        self._list.setStyleSheet("QWidget#railList{background:transparent;}")
        self._list_box = QVBoxLayout(self._list)
        self._list_box.setContentsMargins(0, 0, 4, 0)
        self._list_box.setSpacing(4)
        self._scroll.setWidget(self._list)
        col.addWidget(self._scroll)
        col.addStretch(1)                  # spare height goes BELOW the list — never float items mid-rail

    def update_sessions(self, sessions, focused_key, recent=()):
        """Rebuild the grouped roster (see _group), keeping any search filter. Peeks open briefly while
        chats are running so a new one is noticed, then tucks back; otherwise stays the collapsed strip,
        always summonable."""
        self._data = (list(sessions), focused_key, list(recent))
        first_show = not self.isVisible()
        self._render()
        if first_show:
            self.show()
            self._configure()
            self._hover_timer.start()
            logger.info(f"[rail] shown at idle edge (x={self.x()}, y={self.y()}, h={self.height()})")
        if self._groups and self._groups[0][0] == "RUNNING":
            self._peek()            # something's running → slide open briefly, then auto-tuck

    def _render(self):
        """Rebuild the rows from the last data + the search text, then resize the rail to fit."""
        sessions, focused_key, recent = self._data
        q = self._search.text().strip()
        if q:
            sessions = [s for s in sessions if _matches(q, s.title, getattr(s, "prompt", ""))]
            recent = [r for r in recent if _matches(q, r.title, getattr(r, "preview", ""))]
        # Rows and headers are REUSED (built once per chat, then updated in place): a full rebuild of
        # ~70 styled rows cost ~60ms of frozen UI on every agent step and twice when a task finished.
        groups = _group(sessions, recent)
        want, rows = [], []
        for i, (header, items) in enumerate(groups):
            h = self._hdr_cache.get(header)
            if h is None:
                h = self._hdr_cache[header] = QLabel(header.capitalize())   # "Previous 7 days" (VoiceOS-quiet)
            pad = 8 if i else 2
            if h.property("pad") != pad:
                h.setProperty("pad", pad)
                h.setStyleSheet(_HDR_CSS + f"padding:{pad}px 6px 2px 6px;")
            want.append(h)
            for kind, obj in items:
                k = (kind, obj.key if kind == "session" else obj.id)
                row = self._row_cache.get(k)
                agent = getattr(obj, "agent", "claude")
                if kind == "session":
                    args = (obj.title, obj.status, obj.started_at, obj.key == focused_key, agent)
                else:
                    args = (obj.title, "recent", obj.mtime, False, agent)
                if row is None:
                    row = self._row_cache[k] = _RailRow(*args)
                    if kind == "session":
                        row.clicked.connect(lambda key=obj.key: self.session_clicked.emit(key))
                    else:
                        row.clicked.connect(lambda sid=obj.id: self.recent_clicked.emit(sid))
                else:
                    row.set_state(*args)
                want.append(row)
                rows.append(row)
        if not groups:
            if self._empty is None:
                self._empty = QLabel()
                self._empty.setStyleSheet("color:#6E6E78;font-size:11px;background:transparent;padding:6px 2px;")
            self._empty.setText("No matches" if q else "No chats yet")
            want.append(self._empty)
        self._rows = rows
        self._groups = [(h, len(items)) for h, items in groups]
        have = [self._list_box.itemAt(i).widget() for i in range(self._list_box.count())]
        if have[:-1] == want:                    # same widgets in the same order: nothing to relayout
            return
        while self._list_box.count():            # take items out; the widgets themselves are kept
            w = self._list_box.takeAt(0).widget()
            if w is not None and w not in want:
                w.hide()                         # NOT setParent(None): that made each one a top-level
        for w in want:                           # native window (the qt.qpa.window bursts in the log)
            self._list_box.addWidget(w)
            # Show NOW: on an already-visible rail Qt only shows new children on a QUEUED event, and
            # until then they measure as hidden (0 height) — which sized the list to 40px on every
            # rebuild (the "only one chat left" bug after selecting a conversation).
            w.show()
        self._list_box.addStretch(1)
        known = {("session", x.key) for x in self._data[0]} | {("recent", x.id) for x in self._data[2]}
        for k in [k for k in self._row_cache if k not in known]:
            self._row_cache.pop(k).deleteLater()  # gone from the data (search-hidden rows are kept)
        self._reanchor()

    def _focus_search(self):
        """You clicked the search box: pin the rail open and let the non-activating panel take keyboard
        focus. A USER click may key a floating becomesKeyOnlyIfNeeded panel — same as the notch's type
        field."""
        self.open_full()
        if IS_WIN:
            notch_panel.allow_typing(self, True)       # this click may take the keyboard (only this one)
            logger.info("[rail] search clicked")
            return
        if sys.platform != "darwin":
            return
        try:
            ns = notch_panel._ns_window(RAIL_TITLE)
            if ns is not None and hasattr(ns, "makeKeyWindow"):
                ns.makeKeyWindow()
        except Exception:
            pass
        logger.info("[rail] search clicked")
        QTimer.singleShot(250, self._log_search_focus)

    def _log_search_focus(self):
        """Diagnostic: after a click on the search box, did the panel take the keyboard?"""
        try:
            from AppKit import NSApp
            from PySide6.QtWidgets import QApplication
            ns = notch_panel._ns_window(RAIL_TITLE)
            kw = NSApp.keyWindow()
            fw = QApplication.focusWidget()
            logger.info(f"[rail] search focus: panel_key={bool(ns and ns.isKeyWindow())} "
                        f"nonactivating={bool(ns and ns.styleMask() & 128)} app_active={NSApp.isActive()} "
                        f"key_window={kw.title() if kw else None!r} qt_focus={type(fw).__name__ if fw else None} "
                        f"field_focused={self._search.hasFocus()}")
        except Exception as e:
            logger.info(f"[rail] search focus: n/a ({e})")

    def eventFilter(self, obj, e):
        """Diagnostic: do keystrokes reach the search box, and when/why does it lose focus?"""
        from PySide6.QtCore import QEvent
        if obj is self._search:
            if e.type() == QEvent.KeyPress:
                logger.info(f"[rail] search key: {e.text()!r}")
            elif e.type() == QEvent.FocusOut:
                logger.info(f"[rail] search lost focus (reason={getattr(e.reason(), 'name', e.reason())})")
        return super().eventFilter(obj, e)

    # -- collapse / expand (slide flush against the left edge) -----------------
    def _reanchor(self):
        geo = notch_panel.screen_geo()
        if geo is None:
            return
        # A FIXED-height drawer (PANEL_FRAC of the screen), centered vertically like VoiceOS's; the list
        # scrolls inside it. Fixed, so the search box never jumps and the edge sliver stays put.
        col = self._card.layout()
        chrome = (2 * FLARE + 12 + 10 + 2 * col.spacing()
                  + self._search.height() + self._new_btn.sizeHint().height())
        cap = int(geo.height() * PANEL_FRAC) - chrome
        self._scroll.setFixedHeight(max(40, cap))
        # The outer layout caches the card's size hint and only refreshes it when the CARD reports a
        # change (not when its inner scroll area does) — without this the rail lagged one resize behind.
        self._card.updateGeometry()
        self.layout().activate()
        self.resize(RAIL_W, self.sizeHint().height())   # explicit: adjustSize() never SHRANK the rail
        self._y = geo.y() + (geo.height() - self.height()) // 2
        self._x_open = geo.x()                          # flush on the screen's left edge
        self._x_closed = geo.x() - (RAIL_W - EDGE)      # tucked left, only EDGE px peeking
        self._x_cue = geo.x() - (RAIL_W - TAB_W)        # hovered: the whole tab shows
        self.move(self._x_open if self._expanded else self._x_cue if self._cue else self._x_closed, self._y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._tab.setGeometry(self.width() - TAB_W, 0, TAB_W, self.height())
        self._tab.raise_()
        if self._anim.state() != QPropertyAnimation.Running:
            self._win_mask(self._expanded)              # (the clip follows the new size)

    def paintEvent(self, e):
        """The drawer, VoiceOS-style: near-black, flush on the screen edge with corners that curve
        INTO the edge (top-left / bottom-left) and rounded corners on the right."""
        from PySide6.QtGui import QPainter, QPainterPath, QColor
        r = self._card.geometry()
        top, bot, w, f, R = r.top(), r.bottom() + 1, r.right() + 1, FLARE, PANEL_R
        path = QPainterPath()
        path.moveTo(0, top - f)
        path.cubicTo(0, top - f * .45, f * .45, top, f, top)        # inward curve off the screen edge
        path.lineTo(w - R, top)
        path.quadTo(w, top, w, top + R)
        path.lineTo(w, bot - R)
        path.quadTo(w, bot, w - R, bot)
        path.lineTo(f, bot)
        path.cubicTo(f * .45, bot, 0, bot + f * .45, 0, bot + f)
        path.closeSubpath()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillPath(path, QColor(11, 11, 13, 248))

    def _slide_to(self, x: int):
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(x, self._y))
        self._anim.start()

    def _on_slid(self):
        """A slide ended: tucked → clip to the tab now (after the slide-out, so it still animates)."""
        if not self._expanded:
            self._win_mask(False)

    def _set_expanded(self, on: bool):
        self._expanded = on
        if on:
            self._cue = False
            self._win_mask(True)                       # whole drawer BEFORE it slides in
        self._tab.update()
        self._slide_to(self._x_open if on else self._x_cue if self._cue else self._x_closed)

    def _win_mask(self, expanded: bool):
        """Windows: tucked, the window sits 233 px off the left edge — with a monitor to the LEFT those px
        would show on it (the drawer is painted even when tucked; guide §4 / SPEC §7.2). Clip the window to
        its edge tab while tucked; the whole drawer again while open."""
        if not IS_WIN:
            return
        if expanded:
            self.clearMask()
        else:
            self.setMask(QRegion(QRect(self.width() - TAB_W, 0, TAB_W, self.height())))

    def _set_cue(self, on: bool):
        """Hover cue: the resting tab slides out a little (EDGE → TAB_W) and shows its grip. Only a CLICK
        opens the rail (Damianos' call: hovering must never pop it open)."""
        on = on and not self._expanded
        if on == self._cue:
            return
        self._cue = on
        self._tab.update()
        if not self._expanded:
            self._slide_to(self._x_cue if on else self._x_closed)

    def _peek(self):
        """A new chat started running: show the hover cue for a moment (never opens by itself)."""
        self._set_cue(True)
        QTimer.singleShot(2600, lambda: self._set_cue(False) if not self._hovered else None)

    def _set_hovered(self, inside: bool):
        if inside == self._hovered:
            return
        self._hovered = inside
        logger.info("[rail] hover in" if inside else "[rail] hover out")
        self._set_cue(inside)
        if inside:
            self._close_timer.stop()
        elif self._expanded:
            self._close_timer.start()            # tuck away shortly after you leave

    def _close_if_left(self):
        """Grace period over: tuck the drawer away unless you came back or you're typing in the search
        box."""
        # (search TEXT, not focus: Qt hands the search box focus as soon as the drawer shows)
        if self._expanded and not self._hovered and not self._search.text():
            logger.info("[rail] closed (mouse left)")
            self._set_expanded(False)

    def _poll_hover(self):
        """Hover source of truth: is the global cursor over the rail (incl. the 7 px edge strip)? macOS
        doesn't reliably deliver enter/leave to a non-activating panel of an app that isn't active —
        seen live: clicks arrived, hover never did. Same fix as the notch's _poll_hover."""
        if not self.isVisible():
            return
        g = self.frameGeometry()
        if IS_WIN and not self._expanded:
            g = QRect(g.right() - TAB_W + 1, g.top(), TAB_W, g.height())
        inside = g.contains(_cursor_pos())
        self._set_hovered(inside)
        if not inside and self._expanded and not self._close_timer.isActive():
            self._close_timer.start()    # open but the mouse isn't on it (e.g. opened from a row/notch)

    def enterEvent(self, e):
        self._set_hovered(True)

    def leaveEvent(self, e):
        self._poll_hover()            # Qt also sends a leave when the cursor crosses onto a child

    def mousePressEvent(self, e):
        # A click on the tab / an empty part of the drawer (rows consume their own click) TOGGLES it.
        # Independent of the notch — clicking the rail never opens it.
        logger.info(f"[rail] click → {'close' if self._expanded else 'open'}")
        if self._expanded:
            self.collapse()
        else:
            self.open_full()
        super().mousePressEvent(e)

    def open_full(self):
        """Open the drawer (its own click action — the notch is not affected). It stays open while the
        mouse is on it."""
        self._close_timer.stop()
        self._set_expanded(True)

    def collapse(self):
        if IS_WIN and getattr(self, "_win_typing", False):
            notch_panel.allow_typing(self, False)      # back to never stealing focus
        self._close_timer.stop()
        self._set_expanded(False)

    def _configure(self):
        if IS_WIN:
            notch_panel.configure_panel(self)
            QTimer.singleShot(10, lambda: notch_panel.configure_panel(self))
            QTimer.singleShot(60, lambda: notch_panel.configure_panel(self))
            return
        notch_panel.configure_macos_panel(RAIL_TITLE)
        QTimer.singleShot(10, lambda: notch_panel.configure_macos_panel(RAIL_TITLE))
        QTimer.singleShot(60, lambda: notch_panel.configure_macos_panel(RAIL_TITLE))
