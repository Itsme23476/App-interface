"""The History tab on the Assistant page: what the voice assistant did, newest first.

Chats open in Claude or Codex (their app or a terminal), organizing opens its details (with Revert). The rows come from
core/assistant_history.py; anything that could have been changed elsewhere (a revert, a deleted chat) is checked when
it's shown. Ported from the Mac (assistant_history_view.py, v14.15); Windows: day and month names from QLocale (the
system's language), a fixed 1970 fallback for a damaged time, the app's own icon for Filect rows.
"""
import html
import json
import os
from datetime import date, datetime, timedelta

from PySide6.QtCore import QLocale, QPoint, QTime, QTimer, Qt
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from app.core import assistant_history

ACCENT = "#7C4DFF"
_FILTERS = (("all", "All"), ("claude", "Claude Code"), ("codex", "Codex"), ("filect", "Filect"))
_AGENT = {"claude": "Claude Code", "codex": "Codex", "filect": "Filect"}
_KIND = {"organize": "Filect Organize", "auto": "Filect Auto-organize", "find": "Filect Find"}
_PAGE = 60                      # rows built at a time


def _when(ts):
    try:
        return datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError, TypeError):   # (a damaged row must not take the tab down with it)
        return datetime(1970, 1, 1)


def _day_label(d: date) -> str:
    today = date.today()
    if d == today:
        return "TODAY"
    if d == today - timedelta(days=1):
        return "YESTERDAY"
    loc = QLocale.system()
    if today - timedelta(days=7) < d < today:
        return loc.dayName(d.isoweekday(), QLocale.LongFormat).upper()
    month = loc.monthName(d.month, QLocale.ShortFormat).upper().rstrip(".")
    return f"{month} {d.day}" + ("" if d.year == today.year else f", {d.year}")


def _tooltip(text: str) -> str:
    return f"<span>{html.escape(text)}</span>"     # (a tooltip would otherwise render a title's <tags> as HTML)


class _Elided(QLabel):
    """One line that ends in "…" when there's no room (and can shrink, so it never pushes its row wider). With `short`:
    when the whole text doesn't fit, that complete shorter sentence is shown instead of a word cut in half."""
    def __init__(self, text="", short=None):
        super().__init__()
        self._full = text
        self._short = short
        self.setTextFormat(Qt.PlainText)           # (what you said, and a chat's name, are never markup)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(10)
        self._fit()

    def setFullText(self, text):
        self._full = text or ""
        self._fit()

    def fullText(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        fm = self.fontMetrics()
        room = max(10, self.width() - 2)
        text = self._full
        if self._short and fm.horizontalAdvance(text) > room:
            text = self._short
        QLabel.setText(self, fm.elidedText(text, Qt.ElideRight, room))


def _elided(text: str, css: str) -> _Elided:
    w = _Elided(text)
    w.setStyleSheet(css)
    w.setToolTip(_tooltip(text))
    return w


def _filect_pixmap(px):
    ic = QApplication.windowIcon() if QApplication.instance() else None
    if ic is not None and not ic.isNull():
        return ic.pixmap(px, px)
    try:
        from app.ui.icons import line_pixmap
        return line_pixmap("sparkle", px, ACCENT)
    except Exception:
        return None


class HistoryView(QWidget):
    def __init__(self, main_window, colors: dict, parent=None):
        super().__init__(parent)
        self._mw = main_window
        self._c = colors
        self._filter = "all"
        self._shown = _PAGE
        self._problem = {}                         # row id -> what's wrong with opening it ("gone" | "failed")
        self._log_cache = {}                       # (path, mtime) -> was it reverted
        self._queued = False
        self._day = date.today()
        self._rows = []
        v = QVBoxLayout(self)
        v.setContentsMargins(30, 18, 30, 0)
        v.setSpacing(14)
        self._segs = QFrame()
        self._segs.setObjectName("histSegs")
        sh = QHBoxLayout(self._segs)
        sh.setContentsMargins(3, 3, 3, 3)
        sh.setSpacing(3)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns = {}
        for key, label in _FILTERS:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(28)
            b.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self._group.addButton(b)
            self._btns[key] = b
            sh.addWidget(b)
        self._btns["all"].setChecked(True)
        top = QHBoxLayout()
        top.addWidget(self._segs)
        top.addStretch(1)
        v.addLayout(top)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body = QWidget()
        self._body.setObjectName("histBody")
        self._list = QVBoxLayout(self._body)
        self._list.setContentsMargins(0, 0, 8, 16)
        self._list.setSpacing(8)
        self._list.addStretch(1)
        self._scroll.setWidget(self._body)
        v.addWidget(self._scroll, 1)
        self._style()
        assistant_history.subscribe(self.changed)

    # ---- keeping it current ---------------------------------------------------------------------------------
    def changed(self):
        """A row was written. (Off screen it's read again when you come back.)"""
        if self.isVisible() and not self._queued:
            self._queued = True
            QTimer.singleShot(50, self._flush)

    def _flush(self):
        self._queued = False
        if self.isVisible():
            self.refresh()

    def tick(self):
        """Left open past midnight, "today" would turn into a lie."""
        if self.isVisible() and date.today() != self._day:
            self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def apply_theme(self, c):
        self._c = c
        self._style()
        if self.isVisible():
            self.refresh()

    def _style(self):
        c = self._c
        self.setStyleSheet(f"QWidget#histBody {{ background:{c['bg']}; }} "
                           f"QFrame#histSegs {{ background:{c['input_bg']}; border:1px solid {c['border']}; "
                           "border-radius:9px; }")
        self._scroll.viewport().setStyleSheet(f"background:{c['bg']};")
        for b in self._btns.values():
            b.setStyleSheet(
                f"QPushButton {{ background:transparent; color:{c['text_secondary']}; border:none; border-radius:7px; "
                "padding:0 12px; font-size:12px; } "
                f"QPushButton:checked {{ background:{c['surface']}; color:{ACCENT}; font-weight:600; "
                f"border:1px solid {c['border']}; }}")

    def _set_filter(self, key):
        self._filter, self._shown = key, _PAGE
        self.refresh()

    # ---- the list -----------------------------------------------------------------------------------------
    def refresh(self):
        self._day = date.today()
        bar = self._scroll.verticalScrollBar()
        at = bar.value()
        for w in self._rows:
            w.setParent(None)
            w.deleteLater()
        self._rows = []
        items = [e for e in assistant_history.get_all() if self._filter in ("all", e.get("agent"))]
        day = None
        for e in items[:self._shown]:
            d = _when(e["ts"]).date()
            if d != day:
                day = d
                self._add(self._heading(_day_label(d)))
            self._add(self._row(e))
        if len(items) > self._shown:
            more = QPushButton("Show more")
            more.setCursor(Qt.PointingHandCursor)
            more.setFixedHeight(32)
            more.setStyleSheet(self._button_css())
            more.clicked.connect(self._more)
            self._add(more)
        if not items:
            self._add(self._empty_label())
        QTimer.singleShot(0, lambda: bar.setValue(at))

    def _add(self, w):
        self._rows.append(w)
        self._list.insertWidget(self._list.count() - 1, w)   # (above the stretch)

    def _more(self):
        self._shown += _PAGE
        self.refresh()

    def _heading(self, text):
        w = QLabel(text)
        w.setStyleSheet(f"color:{self._c['text_muted']}; font-size:11px; font-weight:700; letter-spacing:0.5px; "
                        "padding:8px 2px 0 2px; background:transparent;")
        return w

    def _empty_label(self):
        text = ("Nothing here yet. Chats, organizing and searches you ask for by voice will show up here."
                if self._filter == "all" else f"Nothing from {_AGENT[self._filter]} yet.")
        w = QLabel(text)
        w.setAlignment(Qt.AlignCenter)
        w.setWordWrap(True)
        w.setStyleSheet(f"color:{self._c['text_muted']}; font-size:13px; padding:48px 20px; background:transparent;")
        return w

    def _button_css(self):
        c = self._c
        return (f"QPushButton {{ background:{c['surface']}; color:{c['text_secondary']}; border:1px solid {c['border']}; "
                "border-radius:9px; padding:0 11px; font-weight:600; font-size:12px; } "
                f"QPushButton:hover {{ color:{ACCENT}; border-color:{ACCENT}; }} "
                f"QPushButton:disabled {{ color:{c['text_muted']}; background:transparent; border-color:{c['border']}; }}")

    def _logo(self, agent):
        w = QLabel()
        w.setFixedSize(20, 20)
        w.setAlignment(Qt.AlignCenter)
        pm = None
        if agent in ("claude", "codex"):
            try:
                from app.ui.agents_view import agent_logo
                pm = agent_logo(agent, 18)
            except Exception:
                pm = None
        else:
            pm = _filect_pixmap(18)
        if pm is not None and not pm.isNull():
            w.setPixmap(pm)
        return w

    def _log_reverted(self, path):
        """Was this organize put back, from anywhere? "partly", True/False - None if its record is gone."""
        try:
            mtime = os.path.getmtime(path)
        except (OSError, TypeError, ValueError):
            return None
        key = (path, mtime)
        if key not in self._log_cache:
            try:
                with open(path, encoding="utf-8") as f:
                    log = json.load(f)
                self._log_cache[key] = "partly" if log.get("partly_reverted") else bool(log.get("reverted"))
            except (OSError, ValueError, AttributeError):
                return None
        return self._log_cache[key]

    def _row(self, e):
        c = self._c
        agent = e.get("agent") if e.get("agent") in _AGENT else "filect"
        kind = e.get("kind", "")
        row = QFrame()
        row.setObjectName("histRow")
        row.setStyleSheet(f"QFrame#histRow {{ background:{c['surface']}; border:1px solid {c['border']}; "
                          "border-radius:12px; } QLabel { background:transparent; border:none; }")
        h = QHBoxLayout(row)
        h.setContentsMargins(13, 10, 11, 10)
        h.setSpacing(11)
        h.addWidget(self._logo(agent), 0, Qt.AlignVCenter)
        reverted = self._log_reverted(e.get("log")) if e.get("log") else None
        said = str(e.get("said") or "")
        parts = [_AGENT[agent] if kind == "chat" else _KIND.get(kind, "Filect")]
        if reverted:
            parts.append("Partly put back" if reverted == "partly" else "Put back")
        elif e.get("status"):
            parts.append(str(e["status"]))
        if said:
            parts.append(f"“{said}”")
        col = QVBoxLayout()
        col.setSpacing(3)
        col.addWidget(_elided(str(e.get("title") or said or "Untitled"),
                              f"color:{c['text']}; font-size:13.5px; font-weight:600;"))
        col.addWidget(_elided("  ·  ".join(parts), f"color:{c['text_muted']}; font-size:12px;"))
        h.addLayout(col, 1)
        dt = _when(e["ts"])
        t = QLabel(QLocale.system().toString(QTime(dt.hour, dt.minute), QLocale.ShortFormat))
        t.setStyleSheet(f"color:{c['text_muted']}; font-size:11.5px;")
        h.addWidget(t, 0, Qt.AlignVCenter)
        btn = self._action(e, agent, kind, reverted)
        if btn is not None:
            h.addWidget(btn, 0, Qt.AlignVCenter)
        row.setProperty("history_id", e.get("id"))
        return row

    def _action(self, e, agent, kind, reverted):
        if kind == "chat":
            problem = self._problem.get(e["id"])
            if not e.get("session_id"):
                # (its id never arrived: only this run can still give it one - after a restart it never will)
                b = QPushButton("Starting…" if e["id"] in assistant_history._live.values() else "Not saved")
                b.setEnabled(False)
                if b.text() == "Not saved":
                    b.setToolTip("Filect closed before this chat was saved, so it can't be opened.")
            elif problem == "gone":
                b = QPushButton("Chat deleted")
                b.setEnabled(False)
                b.setToolTip("This chat was deleted or archived, so it can't be opened.")
            else:
                name = "Codex" if agent == "codex" else "Claude"
                b = QPushButton("Couldn't open, try again  ⌄" if problem else f"Open in {name}  ⌄")
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, x=e, w=b: self._open_menu(x, w))
        elif kind in ("organize", "auto") and reverted is not None:
            b = QPushButton("View")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, x=e: self._view(x))
        else:
            return None
        b.setFixedHeight(30)
        b.setStyleSheet(self._button_css())
        return b

    # ---- the buttons ---------------------------------------------------------------------------------------
    def _open_menu(self, e, btn):
        m = QMenu(self)                            # (not the row's: a new row arriving rebuilds the list under it)
        app = "Codex app" if e.get("agent") == "codex" else "Claude app"
        m.addAction(app).triggered.connect(lambda: self._open(e, "app"))
        m.addAction("Terminal").triggered.connect(lambda: self._open(e, "terminal"))
        m.exec(btn.mapToGlobal(QPoint(0, btn.height() + 4)))
        m.deleteLater()

    def _open(self, e, where):
        ap = getattr(self._mw, "assistant_page", None)
        result = ap.open_saved_chat(e.get("agent"), e.get("session_id"), e.get("cwd") or "", where) if ap else "failed"
        if result == "ok":
            self._problem.pop(e["id"], None)
        else:
            self._problem[e["id"]] = result
        self.refresh()

    def _view(self, e):
        from app.ui.organize_page import HistoryDialog
        d = HistoryDialog(getattr(self._mw, "organize_page", None))
        d._view_details(e["log"])                  # (its own window, with Revert)
        d.deleteLater()
        self.refresh()                             # (it may have just been put back)
