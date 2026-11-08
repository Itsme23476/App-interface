"""The Agents page — a chat app, like Claude's own (Windows port of the Mac page, built from its spec §5).

A PURE VIEW: the engine (assistant_page.AssistantPage) pushes state in with set_* / show_*, and the
widgets only emit what you clicked. Layout:

    [ Header · stacked( ChatView | EmptyState ) · Composer ]  |  ChatList (right, collapsible)

* ChatView: per turn — your message as a bubble (with screenshots) → the agent's row: its steps FOLDED
  into one line ("6 steps · created 2 files, ran 3 commands" → click for the full rich Details), the
  answer (markdown), chips for files it wrote → and on the last turn the tail: Approve / Deny inline,
  an error (with a setup button when Claude Code needs it), "Working · …" + a queued follow-up, or
  "Stopped by you". Re-renders only what changed (a signature per turn).
* ChatList: + New chat · hide · search · Today / Yesterday / Earlier · rows = status dot + title + agent
  logo + time. Search also reaches your other Claude Code chats. Rows are built once and reused.
* Composer: Enter sends, Shift+Enter = new line, ↑ becomes ■ (Stop) while it works, a mic; under the box
  the agent + model pill and the access pill (Ask first / Full access); under that "See what it can do".

Qt traps from the Mac port, all handled here: the app's global stylesheet pads every QPushButton
(10px 20px) and turns it violet on hover, and styles QTextEdit/QLineEdit — so every widget here carries
its own full style; children added to a visible parent are show()n; layouts are cleared by hiding
before deleteLater; nothing re-styles unless its state changed.
"""
from __future__ import annotations

import math
import os
import time
from pathlib import Path

from PySide6.QtCore import (Qt, Signal, QTimer, QSize, QUrl, QPoint, QRect, QRectF, QEvent,
                            QPropertyAnimation, QEasingCurve, QVariantAnimation)
from PySide6.QtGui import (QColor, QPixmap, QPainter, QFontMetrics, QDesktopServices, QImage, QIcon,
                           QPainterPath, QKeyEvent)
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
                               QStackedWidget, QScrollArea, QLineEdit, QPlainTextEdit, QTextBrowser,
                               QSizePolicy, QMenu, QWidgetAction, QGraphicsScene, QGraphicsPixmapItem,
                               QGraphicsBlurEffect, QGraphicsDropShadowEffect, QApplication)

from app.ui import details_view
from app.ui.icons import line_pixmap

ASSETS = Path(__file__).resolve().parent / "assets"
LIST_W = 272
HEADER_H = 62
MAX_TURNS = 40
BUBBLE_MAX = 560
BUBBLE_MIN_CAP = 500
THUMB_W = 220

# ── colours ──────────────────────────────────────────────────────────────────────────────────────
_LIGHT = {
    "page": "#F7F6FC", "surface": "#FFFFFF", "panel": "#FBFAFE", "border": "#E5E1F2", "border_strong": "#D9D3EC",
    "text": "#1B1730", "muted": "#6F6B83", "faint": "#A6A2B6", "accent": "#7C4DFF", "accent_soft": "#EFE9FF",
    "bubble": "#EEEAFB", "hover": "#F1EEFA", "selected": "#EAE4FC", "code": "#F4F2FA",
    "amber": "#B7791F", "amber_soft": "#FFF6E5", "green": "#2F9E55", "red": "#D13A48", "red_soft": "#FDEEF0",
    "on_accent": "#FFFFFF", "shadow": "rgba(27,23,48,0.10)",
}
_DARK = {
    "page": "#0C0C14", "surface": "#13131C", "panel": "#101018", "border": "#22222F", "border_strong": "#2D2D3D",
    "text": "#E8E8F0", "muted": "#8A8AA0", "faint": "#55556A", "accent": "#8F67FF", "accent_soft": "#221A3D",
    "bubble": "#1E1A33", "hover": "#1A1A26", "selected": "#231D3B", "code": "#181824",
    "amber": "#E0A23C", "amber_soft": "#2A2213", "green": "#4FC774", "red": "#F2645A", "red_soft": "#2C1517",
    "on_accent": "#FFFFFF", "shadow": "rgba(0,0,0,0.45)",
}


def colors(theme: str) -> dict:
    return dict(_DARK if theme == "dark" else _LIGHT)


STATUS_COLOR = {"working": "accent", "needs_you": "amber", "done": "green", "error": "red", "stopped": "faint"}
STATUS_WORDS = {"working": "Working", "needs_you": "Needs your OK", "done": "Done", "error": "Failed",
                "stopped": "Stopped"}
EFFORT_NAMES = {"": "Auto", "low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max",
                "ultra": "Ultra"}
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}
APP_NAMES = {"claude": "Claude", "codex": "Codex"}           # "Open in Claude" / "Open in Codex"
AGENT_SUBS = {"claude": "Uses your Claude subscription", "codex": "Uses your ChatGPT subscription"}

IDEAS = (
    "Find my 10 biggest downloads",
    "Build a one-page website",
    "Summarize my Desktop PDFs",
)
IDEA_PROMPTS = {
    "Find my 10 biggest downloads": "Find the 10 biggest files in my Downloads folder and list them with their sizes.",
    "Build a one-page website": "Build a simple one-page website about ",
    "Summarize my Desktop PDFs": "Summarize the PDFs on my Desktop in one short list.",
}

# The tour (Windows: only what works on Windows today — the voice chord and screen drawing join it when
# they're ported). Each step: (title, body).
TOUR = [
    ("Just say what you want",
     "Type a task in the box at the bottom — or tap the mic and say it. Claude Code (or Codex) does the work on "
     "your PC, in the “Filect Assistant” folder in your user folder, and every chat is saved."),
    ("Several things at once",
     "Start a new chat while another one is still working. Each keeps going on its own — the list on "
     "the right shows which are working, done, or waiting for you."),
    ("Pick the agent, model + access",
     "Under the box: the model pill picks the agent (Claude Code or Codex), its model and how hard it thinks. ""If one isn't set up or runs out, Filect switches to the other on its own. The access pill decides "
     "whether it asks you first before it changes files or runs commands, or just does the work."),
    ("Your chats",
     "Open the list (the sidebar button, top right) to pick up any chat again — type in an open chat to "
     "continue it. Search also finds your other Claude Code and Codex chats. “Open in Claude / Codex” opens it in ""that app."),
]


# ── small helpers ───────────────────────────────────────────────────────────────────────────────
_logo_cache: dict = {}


def agent_logo(agent: str = "claude", px: int = 18) -> QPixmap:
    """The real agent logo (the Claude spark), scaled sharply; a plain dot if the asset is missing."""
    key = (agent, px)
    if key in _logo_cache:
        return _logo_cache[key]
    ratio = 2
    pm = QPixmap(px * ratio, px * ratio)
    pm.fill(Qt.transparent)
    src = QPixmap(str(ASSETS / f"{agent}-logo.png"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    if not src.isNull():
        p.drawPixmap(QRect(0, 0, px * ratio, px * ratio), src)
    else:
        p.setBrush(QColor("#D97757"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(0, 0, px * ratio, px * ratio)
    p.end()
    pm.setDevicePixelRatio(ratio)
    _logo_cache[key] = pm
    return pm


def _btn(text="", obj="", tip="") -> QPushButton:
    b = QPushButton(text)
    if obj:
        b.setObjectName(obj)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    if tip:
        b.setToolTip(tip)
    return b


class _ClickRow(QWidget):
    """A menu row made of widgets (a QWidgetAction's widget gets the clicks, so the action itself never
    triggers): runs `on_click` and closes the menu."""

    def __init__(self, menu, on_click, parent=None):
        super().__init__(parent)
        self._menu, self._cb = menu, on_click
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self._menu.close()
            self._cb()


def _clear(layout):
    """Remove every item; hide widgets BEFORE deleteLater, or the old ones paint under the new for a frame."""
    while layout.count():
        it = layout.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif it.layout() is not None:
            _clear(it.layout())


def ago(ts: float, now: float | None = None) -> str:
    """now / 5m / 3h (today) / Fri (this week) / Nov 3."""
    now = now or time.time()
    d = max(0, now - (ts or 0))
    if d < 60:
        return "now"
    lt, ln = time.localtime(ts), time.localtime(now)
    if d < 3600:
        return f"{int(d // 60)}m"
    if (lt.tm_year, lt.tm_yday) == (ln.tm_year, ln.tm_yday):
        return f"{int(d // 3600)}h"
    if d < 6 * 86400:
        return time.strftime("%a", lt)
    return time.strftime("%b ", lt) + str(lt.tm_mday)


def group_of(ts: float, now: float | None = None) -> str:
    """Today / Yesterday / Earlier — calendar days, not rolling 24h."""
    now = now or time.time()
    day = lambda t: time.localtime(t)[:3]
    if day(ts) == day(now):
        return "Today"
    if day(ts) == day(now - 86400):
        return "Yesterday"
    return "Earlier"


def matches(query: str, *fields) -> bool:
    """Case-insensitive; every word must appear in the fields."""
    hay = " ".join(f or "" for f in fields).lower()
    return all(w in hay for w in (query or "").lower().split())


def split_turns(entries):
    """Entries → turns: {"user": entry|None, "items": [...]} (a turn starts at each thing you said)."""
    turns, cur = [], None
    for e in entries or []:
        if e.get("kind") == "user":
            cur = {"user": e, "items": []}
            turns.append(cur)
        else:
            if cur is None:
                cur = {"user": None, "items": []}
                turns.append(cur)
            cur["items"].append(e)
    return turns


def split_reply(items):
    """(steps, reply texts): the answer is the text AFTER the agent's last tool call; everything before it
    (tool calls + the in-between "I'll create the file…" lines) folds into the steps."""
    last = max((i for i, e in enumerate(items) if e.get("kind") == "tool"), default=-1)
    steps = items[:last + 1]
    reply = [e for e in items[last + 1:] if e.get("kind") == "text" and (e.get("text") or "").strip()]
    return steps, reply


_EDITS = ("Edit", "MultiEdit", "NotebookEdit")
_COMMANDS = ("Bash", "PowerShell")


def fold_summary(steps) -> str:
    """'6 steps · created 2 files, edited 1, ran 3 commands'."""
    tools = [e for e in steps if e.get("kind") == "tool"]
    n = len(tools)
    if not n:
        return ""
    created = len({(e.get("input") or {}).get("file_path") for e in tools if e.get("tool") == "Write"
                   and not ((e.get("result") or {}).get("error"))})
    edited = len({(e.get("input") or {}).get("file_path") or (e.get("input") or {}).get("notebook_path")
                  for e in tools if e.get("tool") in _EDITS and not ((e.get("result") or {}).get("error"))})
    ran = sum(1 for e in tools if e.get("tool") in _COMMANDS)
    parts = []
    if created:
        parts.append(f"created {created} file" + ("" if created == 1 else "s"))
    if edited:
        parts.append(f"edited {edited}" + ("" if parts else (" file" if edited == 1 else " files")))
    if ran:
        parts.append(f"ran {ran} command" + ("" if ran == 1 else "s"))
    head = f"{n} step" + ("" if n == 1 else "s")
    return head + (" · " + ", ".join(parts) if parts else "")


def written_files(items):
    """Files the turn wrote that still exist (chips), newest last, no repeats."""
    out = []
    for e in items:
        if e.get("kind") == "tool" and e.get("tool") == "Write" and not ((e.get("result") or {}).get("error")):
            p = (e.get("input") or {}).get("file_path") or ""
            if p and os.path.exists(p) and p not in out:
                out.append(p)
    return out


def approval_what(req) -> tuple:
    """(what it wants to do, the detail in mono) for the "needs your OK" card."""
    tool = getattr(req, "tool", "") or ""
    inp = dict(getattr(req, "tool_input", None) or {})
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    name = os.path.basename(path)
    if tool == "Write":
        return (f"Create {name}" if name else "Create a file"), path
    if tool in _EDITS:
        return (f"Edit {name}" if name else "Edit a file"), path
    if tool in _COMMANDS:
        desc = (inp.get("description") or "").strip()
        return (desc or "Run a command"), str(inp.get("command") or "")
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1] if len(parts) > 2 else ""
        action = parts[-1].replace("_", " ")
        return f"Use {server}: {action}" if server else f"Use {action}", \
            ", ".join(f"{k}={str(v)[:80]}" for k, v in list(inp.items())[:4])
    return f"Use {tool}", ", ".join(f"{k}={str(v)[:80]}" for k, v in list(inp.items())[:4])


def open_path(url_or_path: str):
    if url_or_path.startswith(("http://", "https://", "file:")):
        QDesktopServices.openUrl(QUrl(url_or_path))
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(url_or_path))


# ── building blocks ─────────────────────────────────────────────────────────────────────────────
class _AutoBrowser(QTextBrowser):
    """Rich text that is exactly as tall as its content (no inner scrollbars); the outer chat scrolls.
    Links are handed to `on_link` (files/pages open; full:<id> toggles a Details entry)."""

    def __init__(self, c, on_link=None, parent=None):
        super().__init__(parent)
        self.setObjectName("autoBrowser")
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.document().setDocumentMargin(0)
        self.document().setIndentWidth(14)          # Qt's 40px list indent wastes the column
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self.anchorClicked.connect(lambda u: (on_link or (lambda s: open_path(s)))(u.toString()))
        self.restyle(c)

    def restyle(self, c):
        self.setStyleSheet(
            f"QTextBrowser#autoBrowser {{ background: transparent; border: none; padding: 0; margin: 0;"
            f" color: {c['text']}; font-size: 13px; selection-background-color: {c['accent']}; }}")

    def _fit(self, *_):
        h = int(self.document().size().height() + 0.99) + 2
        if h != self.height():
            self.setFixedHeight(h)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.document().setTextWidth(self.viewport().width())
        self._fit()

    def wheelEvent(self, e):
        e.ignore()                                   # let the chat scroll


class _Dot(QWidget):
    def __init__(self, px=8, parent=None):
        super().__init__(parent)
        self._c = QColor("#999")
        self.setFixedSize(px, px)

    def set_color(self, color: str):
        if QColor(color) != self._c:
            self._c = QColor(color)
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._c)
        p.drawEllipse(self.rect().adjusted(0, 0, -1, -1))


class _Thumb(QLabel):
    def __init__(self, path, c, parent=None):
        super().__init__(parent)
        self.path = path
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(path)
        t = path[:-4] + ".thumb.png" if path.lower().endswith(".png") else path
        pm = QPixmap(t if os.path.exists(t) else path)
        if pm.isNull():
            self.setText("screenshot moved or deleted")
            self.setStyleSheet(f"color:{c['muted']}; font-size:11px; background:transparent; border:none;")
        else:
            self.setPixmap(pm.scaledToWidth(THUMB_W, Qt.SmoothTransformation))
            self.setStyleSheet(f"border:1px solid {c['border']}; border-radius:8px; background:transparent;")

    def mousePressEvent(self, e):
        if os.path.exists(self.path):
            open_path(self.path)


# ── the conversation ────────────────────────────────────────────────────────────────────────────
class _Bubble(QFrame):
    """Your message, right-aligned. Its width = its longest line (up to 500), never a narrow column
    (a wrapping QLabel otherwise shrinks to its minimum)."""

    def __init__(self, text, images, c, parent=None):
        super().__init__(parent)
        self.setObjectName("bubble")
        self.setStyleSheet(f"QFrame#bubble {{ background:{c['bubble']}; border:none; border-radius:16px; }}"
                           f" QLabel {{ background:transparent; border:none; color:{c['text']}; font-size:13px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 9, 14, 9)
        lay.setSpacing(6)
        words, shots = details_view.split_screenshots(text or "")
        shots = list(images or []) + [s for s in shots if s not in (images or [])]
        if words:
            lab = QLabel(words)
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            fm = QFontMetrics(lab.font())
            longest = max((fm.horizontalAdvance(ln) for ln in words.splitlines() or [""]), default=0)
            lab.setMinimumWidth(min(BUBBLE_MIN_CAP, longest + 4))
            lay.addWidget(lab)
        if shots:
            row = QHBoxLayout()
            row.setSpacing(6)
            for p in shots[:4]:
                row.addWidget(_Thumb(p, c))
            row.addStretch(1)
            lay.addLayout(row)
        self.setMaximumWidth(BUBBLE_MAX)


class _Turn(QWidget):
    """One turn: your bubble → the agent row (fold · reply · file chips) → [tail on the last turn]."""

    def __init__(self, view, turn, opened, c, agent="claude", parent=None):
        super().__init__(parent)
        self.view = view
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        u = turn.get("user")
        if u is not None:
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(_Bubble(u.get("text", ""), u.get("images"), c))
            lay.addLayout(row)
        steps, reply = split_reply(turn["items"])
        if not steps and not reply:
            return
        agent_row = QHBoxLayout()
        agent_row.setSpacing(10)
        av = QLabel()
        av.setFixedSize(26, 26)
        av.setAlignment(Qt.AlignCenter)
        av.setPixmap(agent_logo(agent, 16))
        av.setStyleSheet(f"background:{c['surface']}; border:1px solid {c['border']}; border-radius:13px;")
        agent_row.addWidget(av, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        col.setContentsMargins(0, 2, 0, 0)
        summary = fold_summary(steps)
        tid = (u or {}).get("id") or "t0"
        if summary:
            is_open = tid in opened
            fb = _btn("", "foldBtn")
            fb.setText(("▾  " if is_open else "▸  ") + summary)
            fb.setStyleSheet(
                f"QPushButton#foldBtn {{ background:transparent; border:none; padding:2px 0; text-align:left;"
                f" color:{c['muted']}; font-size:12px; font-weight:600; }}"
                f" QPushButton#foldBtn:hover {{ color:{c['text']}; background:transparent; border:none; }}")
            fb.clicked.connect(lambda _=False, t=tid: view._toggle_fold(t))
            col.addWidget(fb, 0, Qt.AlignLeft)
            if is_open:
                box = QFrame()
                box.setObjectName("foldBox")
                box.setStyleSheet(f"QFrame#foldBox {{ background:{c['panel']}; border:1px solid {c['border']};"
                                  f" border-radius:10px; }}")
                bl = QVBoxLayout(box)
                bl.setContentsMargins(12, 6, 12, 8)
                br = _AutoBrowser(c, on_link=view._on_detail_link)
                with details_view.palette(text=c["text"], muted=c["muted"], accent=c["accent"],
                                          green=c["green"], red=c["red"], user=c["text"]):
                    br.setHtml(details_view.render_html(steps, frozenset(view._expanded)))
                bl.addWidget(br)
                col.addWidget(box)
        self.reply_browser = None     # the reply's column (pieces of a long reply; one for a normal one)
        self._c = c
        self._on_link = view._on_detail_link
        self._pieces, self._piece_src = [], []
        if reply:
            box = QWidget()
            box.setStyleSheet("background: transparent;")
            self._reply_col = QVBoxLayout(box)
            self._reply_col.setContentsMargins(0, 0, 0, 0)
            self._reply_col.setSpacing(8)
            self.reply_browser = box
            self.set_reply(reply)
            col.addWidget(box)
        files = written_files(turn["items"])
        if files:
            chips = QHBoxLayout()
            chips.setSpacing(6)
            for p in files[-4:]:
                chip = _btn("  " + os.path.basename(p), "fileChip", p)
                chip.setFixedHeight(28)
                chip.setIcon(QIcon(line_pixmap("file", 14, c["accent"])))
                chip.setIconSize(QSize(14, 14))
                chip.setStyleSheet(
                    f"QPushButton#fileChip {{ background:{c['surface']}; border:1px solid {c['border']};"
                    f" border-radius:13px; padding:4px 12px 4px 8px; color:{c['text']}; font-size:12px; }}"
                    f" QPushButton#fileChip:hover {{ background:{c['hover']}; border-color:{c['accent']};"
                    f" color:{c['text']}; }}")
                chip.clicked.connect(lambda _=False, path=p: open_path(path))
                chips.addWidget(chip)
            chips.addStretch(1)
            col.addLayout(chips)
        agent_row.addLayout(col, 1)
        lay.addLayout(agent_row)

    def set_reply(self, reply) -> None:
        """The answer text — also to update a reply that's still streaming in, in place. Laid out in pieces
        (details_view.reply_chunks) and only the pieces that changed are redone: while it streams that's just
        the last one, so an update costs the same at 2 KB or 80 KB."""
        txt = "\n\n".join(e["text"] for e in reply)
        chunks = details_view.reply_chunks(txt)
        for i, src in enumerate(chunks):
            if i < len(self._pieces):
                if self._piece_src[i] == src:
                    continue
                b = self._pieces[i]
            else:
                b = _AutoBrowser(self._c, on_link=self._on_link)
                self._reply_col.addWidget(b)
                self._pieces.append(b)
                self._piece_src.append(None)
            b.setHtml(f'<div style="color:{self._c["text"]};">{details_view._md(src)}</div>')
            self._piece_src[i] = src
        for b in self._pieces[len(chunks):]:
            self._reply_col.removeWidget(b)
            b.hide()
            b.deleteLater()
        del self._pieces[len(chunks):], self._piece_src[len(chunks):]

    def reply_text(self) -> str:
        """(tests) the reply as shown, all pieces."""
        return "\n".join(b.toPlainText() for b in self._pieces)


class ChatView(QScrollArea):
    """The open conversation. render(state) rebuilds only the turns whose signature changed."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.setObjectName("chatScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._inner = QWidget()
        self._inner.setObjectName("chatInner")
        self._col = QVBoxLayout(self._inner)
        self._col.setContentsMargins(32, 22, 32, 18)
        self._col.setSpacing(18)
        self._col.addStretch(1)
        self.setWidget(self._inner)
        self._sig = None
        self._live_len = -1           # length of the reply streaming in, as last shown
        self._live_hold = 0.0         # no live repaint before this (monotonic) — set after a slow one
        self._turn_sigs = []          # per shown turn: its signature
        self._turn_widgets = []
        self._tail = None
        self._more = None
        self._key = None
        self._shown_limit = MAX_TURNS
        self.c = None

    def restyle(self, c):
        self.c = c
        self.setStyleSheet(
            f"QScrollArea#chatScroll {{ background:{c['page']}; border:none; }}"
            f" QWidget#chatInner {{ background:{c['page']}; }}"
            f" QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}"
            f" QScrollBar::handle:vertical {{ background:{c['border_strong']}; border-radius:4px; min-height:30px; }}"
            f" QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}"
            f" QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}")
        self._sig = None
        self._turn_sigs = []

    def _at_bottom(self) -> bool:
        sb = self.verticalScrollBar()
        return sb.value() >= sb.maximum() - 40

    def _turn_sig(self, t, i, opened, expanded):
        """A turn's "did it change" signature (it's rebuilt when this differs)."""
        tid = (t.get("user") or {}).get("id") or f"t{i}"
        items = t["items"]
        return (tid, len(items), sum(1 for e in items if e.get("kind") == "tool" and e.get("result") is not None),
                tid in opened, frozenset(e for e in expanded), id(self.c),
                tuple(len(e.get("text") or "") for e in items if e.get("kind") == "text"),   # (a reply that
                # finished streaming changes its text, not the row count)
                tuple(os.path.exists((e.get("input") or {}).get("file_path") or "") for e in items
                      if e.get("kind") == "tool" and e.get("tool") == "Write"))

    def render(self, st: dict, opened: set, expanded: set):
        """st: key, entries, status, running, approval, last_action, queued, bypass, error, setup_kind,
        agent. Cheap when nothing changed."""
        c = self.c
        entries = st.get("entries") or []
        n_res = sum(1 for e in entries if e.get("kind") == "tool" and e.get("result") is not None)
        sig = (st.get("key"), len(entries), n_res, st.get("status"), bool(st.get("running")),
               st.get("approval") is not None, st.get("last_action"), st.get("queued"), bool(st.get("bypass")),
               frozenset(opened), frozenset(expanded), self._shown_limit, st.get("error"), id(c))
        live = details_view.live_len(entries)
        if sig == self._sig:
            if live == self._live_len:
                return                            # nothing changed
            # Same structure — only the reply streaming in grew (or just finished): update its text in place
            # (no rebuild, no flicker), and keep following the bottom if you were there.
            if live >= 0 and time.monotonic() < self._live_hold:
                return                            # (backing off: the last live repaint was slow — see below)
            w = self._turn_widgets[-1] if self._turn_widgets else None
            turns = split_turns(entries)
            reply = split_reply(turns[-1]["items"])[1] if turns else []
            if w is not None and getattr(w, "reply_browser", None) is not None and reply:
                follow = self._at_bottom()
                t0 = time.monotonic()
                w.set_reply(reply)
                cost = time.monotonic() - t0
                # Safety net: never let live repaints take more than ~1/4 of the UI thread — a slow one (a huge
                # code block in one piece) spaces the next ones out. The finished text always paints (live < 0).
                self._live_hold = time.monotonic() + (3 * cost if cost > 0.04 else 0)
                self._live_len = live
                if self._turn_sigs:               # this turn is up to date now — don't rebuild it later
                    self._turn_sigs[-1] = self._turn_sig(turns[-1], len(turns) - 1, opened, expanded)
                if follow:
                    QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(
                        self.verticalScrollBar().maximum()))
                return
            self._sig = None                      # (no reply box yet: rebuild the last turn below)
            self._turn_sigs = self._turn_sigs[:-1]
        self._live_len = live
        new_chat = st.get("key") != self._key
        if new_chat:
            self._key = st.get("key")
            self._shown_limit = MAX_TURNS
            sig = sig[:11] + (self._shown_limit,) + sig[12:]
        follow = new_chat or self._at_bottom()
        self._sig = sig
        turns = split_turns(entries)
        hidden = max(0, len(turns) - self._shown_limit)
        shown = turns[hidden:]
        # "Show N earlier"
        if self._more is not None:
            self._more.hide()
            self._more.deleteLater()
            self._more = None
        if hidden:
            self._more = _btn(f"Show {min(hidden, MAX_TURNS)} earlier", "moreBtn")
            self._more.setFixedHeight(26)
            self._more.setStyleSheet(
                f"QPushButton#moreBtn {{ background:transparent; border:1px solid {c['border']}; border-radius:12px;"
                f" padding:4px 14px; color:{c['muted']}; font-size:12px; }}"
                f" QPushButton#moreBtn:hover {{ background:{c['hover']}; color:{c['text']}; border-color:{c['border']}; }}")
            self._more.clicked.connect(self._show_more)
            self._col.insertWidget(0, self._more, 0, Qt.AlignHCenter)
        base = 1 if hidden else 0
        agent = st.get("agent") or "claude"
        new_sigs = [self._turn_sig(t, i, opened, expanded) for i, t in enumerate(shown)]
        # rebuild from the first turn that differs (usually only the last one)
        first = next((i for i, (a, b) in enumerate(zip(self._turn_sigs, new_sigs)) if a != b),
                     min(len(self._turn_sigs), len(new_sigs)))
        if new_chat:
            first = 0
        for w in self._turn_widgets[first:]:
            w.hide()
            self._col.removeWidget(w)
            w.deleteLater()
        self._turn_widgets = self._turn_widgets[:first]
        for i in range(first, len(shown)):
            w = _Turn(self.owner, shown[i], opened, c, agent)
            self._col.insertWidget(base + i, w)
            w.show()
            self._turn_widgets.append(w)
        self._turn_sigs = new_sigs
        # the tail (approval / error / working / stopped)
        if self._tail is not None:
            self._tail.hide()
            self._col.removeWidget(self._tail)
            self._tail.deleteLater()
            self._tail = None
        tail = self.owner._make_tail(st, c)
        if tail is not None:
            self._col.insertWidget(base + len(shown), tail)
            tail.show()
            self._tail = tail
        if follow:
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))
            QTimer.singleShot(60, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def _show_more(self):
        self._shown_limit += MAX_TURNS
        self._sig = None
        self._turn_sigs = []
        for w in self._turn_widgets:
            w.hide()
            self._col.removeWidget(w)
            w.deleteLater()
        self._turn_widgets = []
        self.owner._rerender()

    def reset(self):
        self._sig = None
        self._key = None
        self._turn_sigs = []
        for w in self._turn_widgets:
            w.hide()
            self._col.removeWidget(w)
            w.deleteLater()
        self._turn_widgets = []


SETUP_WORDS = {"install": "Set up", "signin": "Sign in to"}


def needs_setup(state) -> bool:
    """Neither Claude Code nor Codex is ready (installed AND signed in) — `state` from AssistantPage; None = not
    checked yet (show nothing rather than flash the card)."""
    return bool(state) and state.get("claude") != "ready" and state.get("codex") != "ready"


_SETUP_LINE1 = "Filect's agents run on Claude Code (your Claude plan) or Codex (your ChatGPT plan)."
_SETUP_LINE2 = "Set up either one — it takes about a minute."


class SetupCard(QFrame):
    """"Connect Claude Code or Codex" (Mac §8v): shown on the Agents tab ONLY when neither agent is ready. One click
    opens a PowerShell window with the official installer / sign-in (the same as a task's setup card); the page
    updates by itself when it's done. `state`: {"claude"/"codex": ready|install|signin, "waiting": agent|None}."""
    setup = Signal(str)                      # "claude" | "codex"

    def __init__(self, c, parent=None):
        super().__init__(parent)
        self.setObjectName("agentSetupCard")
        self.setMaximumWidth(620)
        self._c, self._state = c, None
        self._lay = QVBoxLayout(self)
        self.apply_theme(c)

    def set_state(self, state):
        self._state = state
        self.apply_theme(self._c)

    def _fit_desc(self):
        """Like the Mac: the first sentence on its own line, the second under it — when the first fits. In a
        narrower window the two just flow (a forced break there left a third, one-word-ish line)."""
        d = getattr(self, "desc", None)
        if d is None:
            return
        room = self.width() - 56 - 8
        fits = room > 0 and d.fontMetrics().horizontalAdvance(_SETUP_LINE1) <= room
        text = f"{_SETUP_LINE1}\n{_SETUP_LINE2}" if fits else f"{_SETUP_LINE1} {_SETUP_LINE2}"
        if d.text() != text:
            d.setText(text)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_desc()

    def apply_theme(self, c):
        self._c = c
        _clear(self._lay)
        st = self._state or {}
        waiting = st.get("waiting")
        self._lay.setContentsMargins(28, 24, 28, 22)
        self._lay.setSpacing(10)
        self.setStyleSheet(f"QFrame#agentSetupCard {{ background:{c['surface']}; border:1px solid {c['border']};"
                           f" border-radius:16px; }} QFrame#agentSetupCard QLabel {{ background:transparent; border:none; }}")
        logos = QHBoxLayout()
        logos.setSpacing(12)
        logos.addStretch(1)
        for agent in ("claude", "codex"):
            lg = QLabel()
            lg.setPixmap(agent_logo(agent, 40))
            logos.addWidget(lg)
        logos.addStretch(1)
        self._lay.addLayout(logos)
        self._lay.addSpacing(2)
        t = QLabel("Connect Claude Code or Codex")
        t.setAlignment(Qt.AlignCenter)
        t.setStyleSheet(f"color:{c['text']}; font-size:19px; font-weight:700;")
        self._lay.addWidget(t)
        d = QLabel(f"{_SETUP_LINE1} {_SETUP_LINE2}")
        d.setWordWrap(True)
        d.setAlignment(Qt.AlignCenter)
        d.setStyleSheet(f"color:{c['muted']}; font-size:13px;")
        self._lay.addWidget(d)
        self.desc = d
        self._fit_desc()
        self._lay.addSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        self.buttons = {}
        for agent, primary in (("claude", True), ("codex", False)):
            kind = st.get(agent) or "install"
            label = ("Finish in PowerShell…" if waiting == agent
                     else f"{SETUP_WORDS.get(kind, 'Set up')} {AGENT_NAMES[agent]}")
            b = _btn(f"  {label}", f"setup_{agent}")
            b.setIcon(QIcon(agent_logo(agent, 16)))
            b.setIconSize(QSize(16, 16))
            b.setFixedHeight(38)
            if primary:
                css = (f"QPushButton#setup_{agent} {{ background:{c['accent']}; color:{c['on_accent']}; border:1px solid {c['accent']};"
                       f" border-radius:10px; padding:0 16px 0 12px; font-size:13px; font-weight:600; }}"
                       f" QPushButton#setup_{agent}:hover {{ background:{c['accent']}; color:{c['on_accent']}; border-color:{c['text']}; }}")
            else:
                css = (f"QPushButton#setup_{agent} {{ background:{c['page']}; color:{c['text']}; border:1px solid {c['border']};"
                       f" border-radius:10px; padding:0 16px 0 12px; font-size:13px; font-weight:600; }}"
                       f" QPushButton#setup_{agent}:hover {{ background:{c['hover']}; color:{c['text']}; border-color:{c['accent']}; }}")
            b.setStyleSheet(css)
            b.setAccessibleName(label)
            b.clicked.connect(lambda _=False, a=agent: self.setup.emit(a))
            self.buttons[agent] = b
            row.addWidget(b)
        row.addStretch(1)
        self._lay.addLayout(row)
        n = QLabel("Waiting for you to finish in the PowerShell window — this updates by itself." if waiting else
                   "Opens a PowerShell window with the official installer and sign-in. This updates by itself "
                   "when you're done.")
        n.setWordWrap(True)
        n.setAlignment(Qt.AlignCenter)
        n.setStyleSheet(f"color:{c['accent'] if waiting else c['muted']}; font-size:12px;")
        self._lay.addWidget(n)
        self.note = n


class EmptyState(QWidget):
    """New chat: "What should I do?", one line, three small idea chips. Nothing else — unless neither agent can run:
    then the "Connect Claude Code or Codex" card instead (the ideas would only fail)."""
    idea = Signal(str)
    setup = Signal(str)                      # the setup card's buttons: "claude" | "codex"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("emptyState")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 40, 40, 40)
        lay.addStretch(3)
        self.logo = QLabel()
        self.logo.setAlignment(Qt.AlignCenter)
        self.logo.setPixmap(agent_logo("claude", 34))
        lay.addWidget(self.logo)
        lay.addSpacing(12)
        self.title = QLabel("What should I do?")
        self.title.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title)
        self.line = QLabel("Type it below, or tap the mic and just say it.")
        self.line.setAlignment(Qt.AlignCenter)
        self.line.setWordWrap(True)
        lay.addWidget(self.line)
        lay.addSpacing(18)
        row = QHBoxLayout()
        row.addStretch(1)
        self.chips = []
        for t in IDEAS:
            b = _btn(t, "ideaChip")
            b.setFixedHeight(32)
            b.clicked.connect(lambda _=False, s=IDEA_PROMPTS.get(t, t): self.idea.emit(s))
            row.addWidget(b)
            self.chips.append(b)
        row.addStretch(1)
        self._chips_row = row
        lay.addLayout(row)
        self._setup_state = None
        self._c = None
        self.card = None                     # the setup card (built when it's needed)
        self._card_row = QHBoxLayout()
        self._card_row.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self._card_row)
        lay.addStretch(4)

    def set_setup(self, state):
        """Neither agent ready → the setup card instead of the greeting + ideas; one ready → the normal screen."""
        if state == self._setup_state:
            return
        self._setup_state = dict(state) if state else state
        show = needs_setup(state)
        for w in [self.logo, self.title, self.line] + self.chips:
            w.setVisible(not show)
        if show:
            if self.card is None:
                self.card = SetupCard(self._c or colors("light"))
                self.card.setup.connect(self.setup.emit)
                self._card_row.addStretch(1)
                self._card_row.addWidget(self.card, 10)
                self._card_row.addStretch(1)
            self.card.set_state(state)
            self.card.show()
        elif self.card is not None:
            self.card.hide()

    def set_agent(self, agent: str):
        if getattr(self, "_agent", "claude") != agent:
            self._agent = agent
            self.logo.setPixmap(agent_logo(agent, 34))

    def restyle(self, c):
        self._c = c
        if self.card is not None:
            self.card.apply_theme(c)
        self.setStyleSheet(f"QWidget#emptyState {{ background:{c['page']}; }}")
        self.title.setStyleSheet(f"color:{c['text']}; font-size:22px; font-weight:700; background:transparent; border:none;")
        self.line.setStyleSheet(f"color:{c['muted']}; font-size:13px; background:transparent; border:none;")
        for b in self.chips:
            b.setStyleSheet(
                f"QPushButton#ideaChip {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:14px;"
                f" padding:6px 14px; color:{c['text']}; font-size:12px; }}"
                f" QPushButton#ideaChip:hover {{ background:{c['hover']}; border-color:{c['accent']}; color:{c['text']}; }}")


# ── the input ───────────────────────────────────────────────────────────────────────────────────
class _Input(QPlainTextEdit):
    """Enter sends; Shift+Enter = new line; grows from 1 to 6 lines."""
    submit = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composerInput")
        self.setFrameShape(QFrame.NoFrame)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(2)
        self.textChanged.connect(self._grow)
        self._grow()

    def keyPressEvent(self, e: QKeyEvent):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.submit.emit()
            return
        super().keyPressEvent(e)

    def _grow(self):
        fm = QFontMetrics(self.font())
        lines = max(1, min(6, int(self.document().size().height())))
        self.setFixedHeight(lines * fm.lineSpacing() + 12)

    # compat with the Mac engine, which used a QTextEdit (toPlainText / setPlainText / clear)


class Composer(QWidget):
    send = Signal(str)
    stop = Signal()
    mic = Signal()
    agent_menu = Signal(QWidget)      # the pill that was clicked (menu anchors to it)
    access_menu = Signal(QWidget)
    tour = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composerWrap")
        self._running = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 6, 28, 14)
        outer.setSpacing(8)
        self.box = QFrame()
        self.box.setObjectName("composerBox")
        bl = QVBoxLayout(self.box)
        bl.setContentsMargins(14, 10, 10, 8)
        bl.setSpacing(6)
        self.input = _Input()
        self.input.setAccessibleName("Message Claude Code")
        self.input.submit.connect(self._on_submit)
        bl.addWidget(self.input)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.agent_pill = _btn("", "agentPill", "Agent, model and thinking")
        self.agent_pill.setIconSize(QSize(15, 15))
        self.agent_pill.setFixedHeight(28)
        self.agent_pill.clicked.connect(lambda: self.agent_menu.emit(self.agent_pill))
        row.addWidget(self.agent_pill)
        self.access_pill = _btn("", "accessPill", "Ask first, or full access")
        self.access_pill.setIconSize(QSize(14, 14))
        self.access_pill.setFixedHeight(28)
        self.access_pill.clicked.connect(lambda: self.access_menu.emit(self.access_pill))
        row.addWidget(self.access_pill)
        row.addStretch(1)
        self.mic_btn = _btn("", "micBtn", "Say it — tap to start, tap again to stop")
        self.mic_btn.setAccessibleName("Voice input")
        self.mic_btn.setFixedSize(34, 34)
        self.mic_btn.setIconSize(QSize(17, 17))
        self.mic_btn.clicked.connect(self.mic.emit)
        row.addWidget(self.mic_btn)
        self.send_btn = _btn("", "sendBtn", "Send (Enter)")
        self.send_btn.setAccessibleName("Send")
        self.send_btn.setFixedSize(34, 34)
        self.send_btn.setIconSize(QSize(17, 17))
        self.send_btn.clicked.connect(self._on_send_btn)
        row.addWidget(self.send_btn)
        bl.addLayout(row)
        outer.addWidget(self.box)
        trow = QHBoxLayout()
        trow.setContentsMargins(4, 0, 0, 0)
        self.tour_btn = _btn("See what it can do", "tourBtn")
        self.tour_btn.clicked.connect(self.tour.emit)
        trow.addWidget(self.tour_btn)
        self.new_badge = QLabel("New here?")
        self.new_badge.setObjectName("newBadge")
        trow.addWidget(self.new_badge)
        trow.addStretch(1)
        self.hint = QLabel("")
        self.hint.setObjectName("composerHint")
        trow.addWidget(self.hint)
        outer.addLayout(trow)
        self._glow = None
        self.c = None
        self._mic_state = "idle"

    def restyle(self, c):
        self.c = c
        self.setStyleSheet(f"QWidget#composerWrap {{ background:{c['page']}; }}")
        self.box.setStyleSheet(f"QFrame#composerBox {{ background:{c['surface']}; border:1px solid {c['border_strong']};"
                               f" border-radius:18px; }}")
        self.input.setStyleSheet(
            f"QPlainTextEdit#composerInput {{ background:transparent; border:none; padding:0; margin:0;"
            f" color:{c['text']}; font-size:13.5px; selection-background-color:{c['accent']}; }}")
        pill = (f"background:transparent; border:1px solid {c['border']}; border-radius:13px; padding:0 11px 0 9px;"
                f" font-size:12px; font-weight:600;")
        self.agent_pill.setStyleSheet(
            f"QPushButton#agentPill {{ {pill} color:{c['text']}; }}"
            f" QPushButton#agentPill:hover {{ background:{c['hover']}; border-color:{c['border_strong']}; color:{c['text']}; }}")
        self._style_access(getattr(self, "_full", False))
        self._style_mic(self._mic_state)
        self._style_send()
        self.tour_btn.setFixedHeight(26)
        self.tour_btn.setStyleSheet(
            f"QPushButton#tourBtn {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:13px;"
            f" padding:0 12px; color:{c['muted']}; font-size:12px; font-weight:600; }}"
            f" QPushButton#tourBtn:hover {{ color:{c['accent']}; background:{c['surface']}; border-color:{c['accent']}; }}")
        self.new_badge.setStyleSheet(f"QLabel#newBadge {{ background:{c['accent_soft']}; color:{c['accent']};"
                                     f" border-radius:8px; padding:1px 7px; font-size:10.5px; font-weight:700; }}")
        self.hint.setStyleSheet(f"QLabel#composerHint {{ color:{c['muted']}; font-size:11.5px; background:transparent; }}")

    def _style_access(self, full: bool):
        c = self.c
        self._full = full
        col = c["text"] if full else c["amber"]
        bg = "transparent" if full else c["amber_soft"]
        self.access_pill.setText("Full access" if full else "Ask first")
        self.access_pill.setIcon(QIcon(line_pixmap("bolt" if full else "shield", 14, c["accent"] if full else c["amber"])))
        self.access_pill.setStyleSheet(
            f"QPushButton#accessPill {{ background:{bg}; border:1px solid {c['border'] if full else c['amber']};"
            f" border-radius:13px; padding:0 11px 0 9px; font-size:12px; font-weight:600; color:{col}; }}"
            f" QPushButton#accessPill:hover {{ background:{c['hover']}; color:{col};"
            f" border-color:{c['border_strong'] if full else c['amber']}; }}")

    def _style_mic(self, state):
        c = self.c
        self._mic_state = state
        rec = state == "recording"
        self.mic_btn.setIcon(QIcon(line_pixmap("mic", 17, "#FFFFFF" if rec else c["muted"])))
        self.mic_btn.setEnabled(state != "transcribing")
        self.mic_btn.setToolTip({"recording": "Listening — tap to stop", "transcribing": "Writing down what you said…"}
                                .get(state, "Say it — tap to start, tap again to stop"))
        bg = c["red"] if rec else "transparent"
        self.mic_btn.setStyleSheet(
            f"QPushButton#micBtn {{ background:{bg}; border:none; border-radius:17px; padding:0; }}"
            f" QPushButton#micBtn:hover {{ background:{c['red'] if rec else c['hover']}; border:none; }}"
            f" QPushButton#micBtn:disabled {{ background:transparent; border:none; }}")

    def _style_send(self):
        c = self.c
        has = bool(self.input.toPlainText().strip())
        on = self._running or has
        self.send_btn.setIcon(QIcon(line_pixmap("stop" if self._running else "arrow_up", 17,
                                                c["on_accent"] if on else c["faint"])))
        self.send_btn.setToolTip("Stop" if self._running else "Send (Enter)")
        self.send_btn.setAccessibleName("Stop" if self._running else "Send")
        bg = c["accent"] if on else c["hover"]
        self.send_btn.setStyleSheet(
            f"QPushButton#sendBtn {{ background:{bg}; border:none; border-radius:17px; padding:0; }}"
            f" QPushButton#sendBtn:hover {{ background:{c['accent'] if on else c['hover']}; border:none; }}")

    def set_running(self, running: bool):
        if running != self._running:
            self._running = running
            self._style_send()

    def set_glow(self, on: bool):
        self.new_badge.setVisible(on)
        if on and self._glow is None:
            eff = QGraphicsDropShadowEffect(self.tour_btn)
            eff.setOffset(0, 0)
            eff.setColor(QColor(self.c["accent"]))
            self.tour_btn.setGraphicsEffect(eff)
            base = QColor(self.c["accent"])
            eff.setBlurRadius(18)
            # A slow breathe (2.4 s), drawn ~10×/s and ONLY while the button is on screen. (2026-11-08: a 60 fps
            # QVariantAnimation re-blurred the shadow non-stop — ~17% of a core while visible, ~3% minimized —
            # part of the "everything lags" the user saw.)
            anim = QTimer(self)
            anim.setInterval(100)
            t0 = time.monotonic()

            def _breathe():
                b = self.tour_btn
                if not b.isVisible() or b.window().isMinimized():
                    return
                a = 170.0 * (0.5 - 0.5 * math.cos(2 * math.pi * ((time.monotonic() - t0) % 2.4) / 2.4))
                eff.setColor(QColor(base.red(), base.green(), base.blue(), int(a)))
            anim.timeout.connect(_breathe)
            anim.start()
            self._glow = (eff, anim)
        elif not on and self._glow is not None:
            self._glow[1].stop()
            self.tour_btn.setGraphicsEffect(None)
            self._glow = None

    def _on_submit(self):
        text = self.input.toPlainText().strip()
        if not text:
            if self._running:
                self.stop.emit()          # empty Enter while it works = Stop
            return
        self.send.emit(text)

    def _on_send_btn(self):
        if self._running and not self.input.toPlainText().strip():
            self.stop.emit()
        elif self._running:
            self._on_submit()             # text typed while it works → a queued follow-up
        else:
            self._on_submit()


# ── the chat list (right) ───────────────────────────────────────────────────────────────────────
class _ChatRow(QFrame):
    clicked = Signal(str)

    def __init__(self, rid, parent=None):
        super().__init__(parent)
        self.rid = rid
        self.setObjectName("chatRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(34)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(8)
        self.dot = _Dot(8)
        lay.addWidget(self.dot, 0, Qt.AlignVCenter)
        self.title = QLabel()
        self.title.setObjectName("rowTitle")
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.title, 1)
        self.logo = QLabel()
        self.logo.setFixedSize(14, 14)
        lay.addWidget(self.logo, 0, Qt.AlignVCenter)
        self.when = QLabel()
        self.when.setObjectName("rowWhen")
        lay.addWidget(self.when, 0, Qt.AlignVCenter)
        self._state = None
        self._full = ""

    def set_state(self, row: dict, c, now):
        st = (row.get("title"), row.get("status"), ago(row.get("ts") or 0, now), bool(row.get("focused")),
              row.get("agent"), id(c), row.get("sub"))
        if st == self._state:
            return
        focus_changed = self._state is None or self._state[3] != st[3] or self._state[5] != st[5]
        self._state = st
        self._full = row.get("title") or "Untitled"
        self._set_access_name()
        self._elide()
        self.dot.set_color(c[STATUS_COLOR.get(row.get("status"), "faint")])
        self.logo.setPixmap(agent_logo(row.get("agent") or "claude", 14))
        self.when.setText(st[2])
        tip = f"{STATUS_WORDS.get(row.get('status'), '')} · {row.get('sub') or ''}".strip(" ·")
        self.setToolTip(f"{self._full}\n{tip}" if tip else self._full)
        if focus_changed:                  # (re-parsing a stylesheet is the costly part — only when it changed)
            bg = c["selected"] if st[3] else "transparent"
            self.setStyleSheet(
                f"QFrame#chatRow {{ background:{bg}; border:none; border-radius:9px; }}"
                f" QFrame#chatRow:hover {{ background:{c['selected'] if st[3] else c['hover']}; }}"
                f" QLabel#rowTitle {{ color:{c['text']}; font-size:12.5px; background:transparent; border:none;"
                f" font-weight:{'600' if st[3] else '400'}; }}"
                f" QLabel#rowWhen {{ color:{c['faint']}; font-size:11px; background:transparent; border:none; }}"
                f" QLabel {{ background:transparent; border:none; }}")

    def _elide(self):
        fm = QFontMetrics(self.title.font())
        self.title.setText(fm.elidedText(self._full, Qt.ElideRight, max(40, self.title.width())))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.rid)

    def _set_access_name(self):
        self.setAccessibleName(f"Chat: {self._full}")


class ChatList(QFrame):
    """Right column: + New chat · hide · search · groups · rows (reused, never rebuilt per update)."""
    new_chat = Signal()
    hide_list = Signal()
    picked = Signal(str)
    search = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatList")
        self.setFixedWidth(LIST_W)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 14, 12, 10)
        lay.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.new_btn = _btn("  New chat", "listNew")
        self.new_btn.setIconSize(QSize(14, 14))
        self.new_btn.clicked.connect(self.new_chat.emit)
        top.addWidget(self.new_btn, 1)
        self.hide_btn = _btn("", "listHide", "Hide the chat list")
        self.hide_btn.setAccessibleName("Hide chats")
        self.hide_btn.setFixedSize(30, 30)
        self.hide_btn.setIconSize(QSize(16, 16))
        self.hide_btn.clicked.connect(self.hide_list.emit)
        top.addWidget(self.hide_btn)
        lay.addLayout(top)
        self.search_box = QLineEdit()
        self.search_box.setObjectName("listSearch")
        self.search_box.setPlaceholderText("Search chats")
        self.search_box.setAccessibleName("Search chats")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedHeight(32)
        self._search_icon = self.search_box.addAction(QIcon(), QLineEdit.LeadingPosition)
        self.search_box.textChanged.connect(self._on_search)
        lay.addWidget(self.search_box)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("listScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.inner.setObjectName("listInner")
        self.col = QVBoxLayout(self.inner)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(1)
        self.col.addStretch(1)
        self.scroll.setWidget(self.inner)
        lay.addWidget(self.scroll, 1)
        self.empty = QLabel("No chats yet")
        self.empty.setObjectName("listEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.empty)
        self._rows: dict = {}
        self._hdrs: dict = {}
        self._order = []
        self._data = []
        self._other = []
        self._sig = None
        self.c = None

    def restyle(self, c):
        self.c = c
        self.setStyleSheet(f"QFrame#chatList {{ background:{c['panel']}; border:none; border-left:1px solid {c['border']}; }}"
                           f" QWidget#listInner {{ background:transparent; }}"
                           f" QScrollArea#listScroll {{ background:transparent; border:none; }}"
                           f" QScrollBar:vertical {{ background:transparent; width:8px; margin:0; }}"
                           f" QScrollBar::handle:vertical {{ background:{c['border_strong']}; border-radius:4px; min-height:24px; }}"
                           f" QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}"
                           f" QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}")
        self.new_btn.setIcon(QIcon(line_pixmap("plus", 14, c["text"])))
        self.new_btn.setStyleSheet(
            f"QPushButton#listNew {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:9px;"
            f" padding:6px 10px; text-align:left; color:{c['text']}; font-size:12.5px; font-weight:600; }}"
            f" QPushButton#listNew:hover {{ background:{c['hover']}; border-color:{c['border_strong']}; color:{c['text']}; }}")
        self.hide_btn.setIcon(QIcon(line_pixmap("sidebar", 16, c["muted"])))
        self.hide_btn.setStyleSheet(
            f"QPushButton#listHide {{ background:transparent; border:none; border-radius:8px; padding:0; }}"
            f" QPushButton#listHide:hover {{ background:{c['hover']}; border:none; }}")
        self._search_icon.setIcon(QIcon(line_pixmap("search", 14, c["faint"])))
        self.search_box.setStyleSheet(
            f"QLineEdit#listSearch {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:16px;"
            f" padding:0 8px 0 2px; color:{c['text']}; font-size:12px; }}"
            f" QLineEdit#listSearch:focus {{ border-color:{c['accent']}; background:{c['surface']}; }}")
        self.empty.setStyleSheet(f"QLabel#listEmpty {{ color:{c['faint']}; font-size:12px; background:transparent; padding:8px; }}")
        for h in self._hdrs.values():
            self._style_hdr(h)
        self._sig = None
        self._render()

    def _style_hdr(self, h):
        h.setStyleSheet(f"color:{self.c['faint']}; font-size:11px; font-weight:600; background:transparent;"
                        f" border:none; padding:10px 10px 3px 10px;")

    def set_rows(self, rows, other=None):
        """rows: [{id, title, status, agent, ts, focused, preview, sub}] — this list's chats.
        other: the user's other Claude Code chats (search only)."""
        self._data = list(rows or [])
        if other is not None:
            self._other = list(other)
        self._render()

    def _on_search(self, text):
        self.search.emit(text)
        self._render()

    def _header(self, text):
        h = self._hdrs.get(text)
        if h is None:
            h = QLabel(text)
            self._hdrs[text] = h
            self._style_hdr(h)
        return h

    def _row(self, rid):
        r = self._rows.get(rid)
        if r is None:
            r = _ChatRow(rid)
            r.clicked.connect(self.picked.emit)
            self._rows[rid] = r
        return r

    def _render(self):
        if self.c is None:
            return
        q = self.search_box.text().strip()
        now = time.time()
        sig = (q, tuple((r["id"], r.get("title"), r.get("status"), bool(r.get("focused")), ago(r.get("ts") or 0, now))
                        for r in self._data), len(self._other) if q else 0, id(self.c))
        if sig == self._sig:
            return
        self._sig = sig
        order = []                    # [(widget, row|None)]
        if q:
            hits = [r for r in self._data if matches(q, r.get("title"), r.get("preview"))]
            for r in hits:
                order.append((self._row(r["id"]), r))
            mine = {r["id"] for r in self._data}
            others = [r for r in self._other if r["id"] not in mine
                      and matches(q, r.get("title"), r.get("preview"), r.get("sub"))][:30]
            if others:
                order.append((self._header("Other Claude Code & Codex chats"), None))
                order += [(self._row(r["id"]), r) for r in others]
            self.empty.setText("No matching chats")
        else:
            groups = {}
            for r in self._data:
                groups.setdefault(group_of(r.get("ts") or 0, now), []).append(r)
            for g in ("Today", "Yesterday", "Earlier"):
                if groups.get(g):
                    order.append((self._header(g), None))
                    order += [(self._row(r["id"]), r) for r in groups[g]]
            self.empty.setText("No chats yet")
        for w, r in order:
            if r is not None:
                w.set_state(r, self.c, now)
        widgets = [w for w, _ in order]
        self.empty.setVisible(not widgets)
        if widgets != self._order:                 # re-order only when the order changed
            for w in self._order:
                if w not in widgets:
                    w.hide()
            for i, w in enumerate(widgets):
                if self.col.indexOf(w) != i:
                    self.col.insertWidget(i, w)
                w.show()
            self._order = widgets
        # free rows whose chat left the data (rows hidden by a search are kept)
        alive = {r["id"] for r in self._data} | {r["id"] for r in self._other}
        for rid in [k for k in self._rows if k not in alive]:
            w = self._rows.pop(rid)
            w.hide()
            self.col.removeWidget(w)
            w.deleteLater()


# ── the tour ────────────────────────────────────────────────────────────────────────────────────
def _blurred(pm: QPixmap, radius: float = 18.0) -> QPixmap:
    """A soft blur of a window snapshot (done at quarter size — cheap)."""
    w, h = max(1, pm.width() // 4), max(1, pm.height() // 4)
    small = pm.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(small)
    eff = QGraphicsBlurEffect()
    eff.setBlurRadius(radius / 4 + 2)
    item.setGraphicsEffect(eff)
    scene.addItem(item)
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    scene.render(p, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
    p.end()
    return QPixmap.fromImage(img).scaled(pm.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


class Tour(QWidget):
    """A layer INSIDE the window: a blurred + dimmed snapshot behind one card that rises in. Back / Next,
    ← →, dots; Esc or a click outside closes."""
    closed = Signal()

    def __init__(self, c, window: QWidget, steps=TOUR):
        super().__init__(window)
        self.c, self.steps, self.i = c, list(steps), 0
        self.hide()                                   # (a child painting during grab() would be in the snapshot)
        snap = window.grab()
        self._bg = _blurred(snap) if not snap.isNull() else QPixmap()
        self.setGeometry(window.rect())
        self.setFocusPolicy(Qt.StrongFocus)
        self.card = QFrame(self)
        self.card.setObjectName("tourCard")
        self.card.setFixedWidth(420)
        self.card.setStyleSheet(
            f"QFrame#tourCard {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:18px; }}"
            f" QLabel {{ background:transparent; border:none; }}")
        sh = QGraphicsDropShadowEffect(self.card)
        sh.setBlurRadius(40)
        sh.setOffset(0, 10)
        sh.setColor(QColor(0, 0, 0, 80))
        self.card.setGraphicsEffect(sh)
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(26, 22, 26, 18)
        cl.setSpacing(10)
        self.step_lbl = QLabel()
        self.step_lbl.setStyleSheet(f"color:{c['accent']}; font-size:11px; font-weight:700;")
        cl.addWidget(self.step_lbl)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setStyleSheet(f"color:{c['text']}; font-size:18px; font-weight:700;")
        cl.addWidget(self.title)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setStyleSheet(f"color:{c['muted']}; font-size:13px;")
        cl.addWidget(self.body)
        cl.addSpacing(6)
        row = QHBoxLayout()
        self.dots = QLabel()
        row.addWidget(self.dots)
        row.addStretch(1)
        self.back = _btn("Back", "tourBack")
        self.back.setStyleSheet(
            f"QPushButton#tourBack {{ background:transparent; border:1px solid {c['border']}; border-radius:10px;"
            f" padding:6px 14px; color:{c['text']}; font-size:12.5px; }}"
            f" QPushButton#tourBack:hover {{ background:{c['hover']}; color:{c['text']}; border-color:{c['border_strong']}; }}")
        self.back.clicked.connect(lambda: self.go(self.i - 1))
        row.addWidget(self.back)
        self.next = _btn("Next", "tourNext")
        self.next.setStyleSheet(
            f"QPushButton#tourNext {{ background:{c['accent']}; border:none; border-radius:10px;"
            f" padding:6px 18px; color:{c['on_accent']}; font-size:12.5px; font-weight:600; }}"
            f" QPushButton#tourNext:hover {{ background:{c['accent']}; color:{c['on_accent']}; border:none; }}")
        self.next.clicked.connect(lambda: self.go(self.i + 1))
        row.addWidget(self.next)
        cl.addLayout(row)
        self._anim = None
        window.installEventFilter(self)
        self.go(0, animate=False)

    def eventFilter(self, obj, e):
        if obj is self.parent() and e.type() == QEvent.Resize:
            self.setGeometry(self.parent().rect())
            self._place()
        return False

    def open(self):
        self.show()
        self.raise_()
        self.setFocus()
        self._place(rise=True)

    def _place(self, rise=False):
        self.card.adjustSize()
        x = (self.width() - self.card.width()) // 2
        y = (self.height() - self.card.height()) // 2
        if rise:
            a = QPropertyAnimation(self.card, b"pos", self)
            a.setDuration(240)
            a.setStartValue(QPoint(x, y + 28))
            a.setEndValue(QPoint(x, y))
            a.setEasingCurve(QEasingCurve.OutCubic)
            a.start()
            self._anim = a
        else:
            self.card.move(x, y)

    def go(self, i, animate=True):
        if i >= len(self.steps):
            self.close_tour()
            return
        self.i = max(0, i)
        t, b = self.steps[self.i]
        self.step_lbl.setText(f"STEP {self.i + 1} OF {len(self.steps)}")
        self.title.setText(t)
        self.body.setText(b)
        self.back.setVisible(self.i > 0)
        self.next.setText("Done" if self.i == len(self.steps) - 1 else "Next")
        dots = "".join(f'<span style="color:{self.c["accent"] if k == self.i else self.c["border_strong"]};'
                       f' font-size:14px;">●</span>&nbsp;' for k in range(len(self.steps)))
        self.dots.setText(dots)
        if self.isVisible():
            self._place()

    def close_tour(self):
        try:
            self.parent().removeEventFilter(self)
        except Exception:
            pass
        self.hide()
        self.closed.emit()
        self.deleteLater()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close_tour()
        elif e.key() in (Qt.Key_Right, Qt.Key_Return, Qt.Key_Enter):
            self.go(self.i + 1)
        elif e.key() == Qt.Key_Left:
            self.go(self.i - 1)
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if not self.card.geometry().contains(e.position().toPoint()):
            self.close_tour()

    def paintEvent(self, _):
        p = QPainter(self)
        if not self._bg.isNull():
            p.drawPixmap(self.rect(), self._bg)
        p.fillRect(self.rect(), QColor(10, 8, 20, 120))


# ── the page ────────────────────────────────────────────────────────────────────────────────────
class AgentsView(QWidget):
    """The whole Agents page. Signals = what you did; the engine answers with set_* / show_*."""
    send_requested = Signal(str)
    stop_requested = Signal()
    new_chat_requested = Signal()
    chat_picked = Signal(str)            # "s:<key>" a chat from this run · "r:<session id>" a saved one
    approve = Signal(bool)
    approve_always = Signal()
    access_changed = Signal(bool)        # True = Full access
    model_changed = Signal(str, str)     # model value, effort ('' = Auto)
    open_in = Signal(str)                # 'app' | 'terminal'
    mic_clicked = Signal()
    list_collapsed_changed = Signal(bool)
    tour_opened = Signal()
    setup_requested = Signal(str, str)   # agent, kind
    search_changed = Signal(str)
    agent_changed = Signal(str)          # the agent new chats use ("claude" | "codex")
    agent_setup_requested = Signal(str)  # the "Connect Claude Code or Codex" card: set up "claude" | "codex"
    codex_choice = Signal(str, bool)     # a chat the Codex app holds: ("close" | "new", always close Codex)

    def __init__(self, theme="light", parent=None):
        super().__init__(parent)
        self.setObjectName("agentsView")
        self.c = colors(theme)
        self._models = []
        self._model = ""
        self._effort = ""
        self._full = False
        self._agent = "claude"           # the agent the composer shows (the open chat's, else new chats')
        self._primary = "claude"         # the agent new chats use
        self._chat_agent = None          # the open chat's agent (None = a new chat)
        self._open_default = "app"
        self._open_has_app = True
        self._open_enabled = False
        self._opened = set()             # folds opened (turn ids)
        self._expanded = set()           # Details entries with "Show full" open
        self._state = None               # the open chat's state (for re-render)
        self._list_collapsed = True
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        main = QWidget()
        main.setObjectName("agentsMain")
        self.main = main
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        # header
        self.header = QFrame()
        self.header.setObjectName("agentsHeader")
        self.header.setFixedHeight(HEADER_H)
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(28, 0, 16, 0)
        hl.setSpacing(10)
        self.title = QLabel("Agents")
        self.title.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        hl.addWidget(self.title, 0, Qt.AlignVCenter)
        self.chip = QLabel()
        self.chip.setObjectName("agentChip")
        self.chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.chip.setFixedHeight(26)
        hl.addWidget(self.chip, 0, Qt.AlignVCenter)
        hl.addStretch(1)
        self.open_btn = _btn("Open in Claude", "openBtn", "Open this chat in the Claude app")
        self.open_btn.clicked.connect(lambda: self.open_in.emit(self._open_default if self._open_has_app else "terminal"))
        self.open_more = _btn("", "openMore", "Other places to open it")
        self.open_more.setAccessibleName("More ways to open")
        self.open_more.setFixedSize(26, 30)
        self.open_more.setIconSize(QSize(13, 13))
        self.open_more.clicked.connect(self._open_menu)
        og = QHBoxLayout()
        og.setSpacing(0)
        og.addWidget(self.open_btn)
        og.addWidget(self.open_more)
        hl.addLayout(og)
        self.new_btn = _btn("  New", "hdrNew", "New chat")
        self.new_btn.setIconSize(QSize(13, 13))
        self.new_btn.clicked.connect(self.new_chat_requested.emit)
        hl.addWidget(self.new_btn)
        self.show_list_btn = _btn("", "hdrList", "Show your chats")
        self.show_list_btn.setAccessibleName("Show chats")
        self.show_list_btn.setFixedSize(32, 32)
        self.show_list_btn.setIconSize(QSize(17, 17))
        self.show_list_btn.clicked.connect(lambda: self.set_list_collapsed(False, emit=True))
        hl.addWidget(self.show_list_btn)
        ml.addWidget(self.header)
        # body
        self.stack = QStackedWidget()
        self.chat = ChatView(self)
        self.empty = EmptyState()
        self.empty.idea.connect(self._use_idea)
        self.empty.setup.connect(self.agent_setup_requested.emit)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.chat)
        ml.addWidget(self.stack, 1)
        self.composer = Composer()
        self.composer.send.connect(self.send_requested.emit)
        self.composer.stop.connect(self.stop_requested.emit)
        self.composer.mic.connect(self.mic_clicked.emit)
        self.composer.agent_menu.connect(self._agent_menu)
        self.composer.access_menu.connect(self._access_menu)
        self.composer.tour.connect(self.open_tour)
        self.composer.input.textChanged.connect(self.composer._style_send)
        ml.addWidget(self.composer)
        root.addWidget(main, 1)
        self.list = ChatList()
        self.list.new_chat.connect(self.new_chat_requested.emit)
        self.list.hide_list.connect(lambda: self.set_list_collapsed(True, emit=True))
        self.list.picked.connect(self.chat_picked.emit)
        self.list.search.connect(self.search_changed.emit)
        root.addWidget(self.list)
        self.tour = None
        self.apply_theme(theme)
        self.set_list_collapsed(True)
        self.set_open_state(False, "app", True)
        self.show_empty()

    # ---- theme ----
    def apply_theme(self, theme: str):
        self.c = colors(theme)
        c = self.c
        self.setStyleSheet(f"QWidget#agentsView {{ background:{c['page']}; }} QWidget#agentsMain {{ background:{c['page']}; }}")
        self.header.setStyleSheet(f"QFrame#agentsHeader {{ background:{c['page']}; border:none; }}")
        self.title.setStyleSheet(f"color:{c['text']}; font-size:20px; font-weight:700; background:transparent; border:none;")
        self.chip.setStyleSheet(f"QLabel#agentChip {{ background:{c['surface']}; border:1px solid {c['border']};"
                                f" border-radius:12px; padding:3px 10px; color:{c['muted']}; font-size:12px; }}")
        self.open_btn.setStyleSheet(
            f"QPushButton#openBtn {{ background:{c['surface']}; border:1px solid {c['border']}; border-right:none;"
            f" border-top-left-radius:10px; border-bottom-left-radius:10px; border-top-right-radius:0;"
            f" border-bottom-right-radius:0; padding:5px 12px; color:{c['text']}; font-size:12.5px; font-weight:600; }}"
            f" QPushButton#openBtn:hover {{ background:{c['hover']}; color:{c['text']}; border-color:{c['border_strong']}; }}"
            f" QPushButton#openBtn:disabled {{ color:{c['faint']}; background:{c['surface']}; border-color:{c['border']}; }}")
        self.open_more.setIcon(QIcon(line_pixmap("chevron", 13, c["muted"])))
        self.open_more.setStyleSheet(
            f"QPushButton#openMore {{ background:{c['surface']}; border:1px solid {c['border']};"
            f" border-top-right-radius:10px; border-bottom-right-radius:10px; border-top-left-radius:0;"
            f" border-bottom-left-radius:0; padding:0; }}"
            f" QPushButton#openMore:hover {{ background:{c['hover']}; border-color:{c['border_strong']}; }}"
            f" QPushButton#openMore:disabled {{ background:{c['surface']}; border-color:{c['border']}; }}")
        self.new_btn.setIcon(QIcon(line_pixmap("plus", 13, c["text"])))
        self.new_btn.setStyleSheet(
            f"QPushButton#hdrNew {{ background:transparent; border:1px solid {c['border']}; border-radius:10px;"
            f" padding:5px 12px 5px 9px; color:{c['text']}; font-size:12.5px; font-weight:600; }}"
            f" QPushButton#hdrNew:hover {{ background:{c['hover']}; color:{c['text']}; border-color:{c['border_strong']}; }}")
        self.show_list_btn.setIcon(QIcon(line_pixmap("sidebar", 17, c["muted"])))
        self.show_list_btn.setStyleSheet(
            f"QPushButton#hdrList {{ background:transparent; border:none; border-radius:8px; padding:0; }}"
            f" QPushButton#hdrList:hover {{ background:{c['hover']}; border:none; }}")
        self.chat.restyle(c)
        self.empty.restyle(c)
        self.composer.restyle(c)
        self.list.restyle(c)
        self._sync_pills()
        self._rerender()

    # ---- the engine's API ----
    def show_empty(self):
        self._state = None
        self.chat.reset()
        self.stack.setCurrentWidget(self.empty)
        self.composer.input.setPlaceholderText(f"Ask {AGENT_NAMES.get(self._agent, 'Claude Code')} to do something…")

    def show_chat(self, state: dict):
        self._state = state
        if self.stack.currentWidget() is not self.chat:
            self.stack.setCurrentWidget(self.chat)
        self.composer.input.setPlaceholderText("Reply…  (Enter to send, Shift+Enter for a new line)")
        self.chat.render(state, self._opened, self._expanded)

    def _rerender(self):
        if self._state is not None:
            self.chat.render(self._state, self._opened, self._expanded)

    def set_running(self, running: bool):
        self.composer.set_running(running)

    def set_chats(self, rows, other=None):
        self.list.set_rows(rows, other)

    def set_list_collapsed(self, collapsed: bool, emit=False):
        self._list_collapsed = collapsed
        self.list.setVisible(not collapsed)
        self.show_list_btn.setVisible(collapsed)
        self.new_btn.setVisible(collapsed)
        if emit:
            self.list_collapsed_changed.emit(collapsed)
        if not collapsed:
            QTimer.singleShot(0, self.list._render)

    def set_models(self, models, model: str, effort: str):
        """models: [{value, name, efforts}] from Claude Code. A thinking level the model lacks → Auto."""
        self._models = list(models or [])
        self._model = model or ""
        m = self._model_info(self._model)
        self._effort = effort if (effort and m and effort in (m.get("efforts") or [])) else ""
        self._sync_pills()

    def set_setup(self, state):
        """The agents' readiness ({"claude", "codex", "waiting"}; None = not checked yet): the new-chat screen
        shows "Connect Claude Code or Codex" when neither can run."""
        self.empty.set_setup(state)

    def set_agent(self, agent: str, primary: str = None, chat_agent=None):
        """The agent the composer shows (`agent`: the open chat's, else new chats'), the one new chats use
        (`primary`) and the open chat's (`chat_agent`, None = a new chat). Call set_models after, with its list."""
        agent = agent if agent in AGENT_NAMES else "claude"
        sig = (agent, primary or self._primary, chat_agent)
        if sig == getattr(self, "_agent_sig", None):
            return
        self._agent_sig = sig
        self._agent, self._primary, self._chat_agent = sig
        self.empty.set_agent(self._primary)
        self.composer.input.setAccessibleName(f"Message {AGENT_NAMES[agent]}")
        if self._state is None:
            self.composer.input.setPlaceholderText(f"Ask {AGENT_NAMES[agent]} to do something…")
        self._open_sig = None
        self.set_open_state(self._open_enabled, self._open_default, self._open_has_app)
        self._sync_pills()

    def set_access(self, full: bool):
        self._full = bool(full)
        self.composer._style_access(self._full)

    def set_open_state(self, enabled: bool, default: str, has_app: bool):
        if getattr(self, "_open_sig", None) == (enabled, default, has_app):
            return
        self._open_sig = (enabled, default, has_app)
        self._open_enabled, self._open_default, self._open_has_app = enabled, default, has_app
        where = default if has_app else "terminal"
        app = APP_NAMES.get(self._agent, "Claude")
        self.open_btn.setText(f"Open in {app}" if where == "app" else "Open in Terminal")
        self.open_btn.setToolTip(f"Open this chat in the {app} app" if where == "app"
                                 else f"Open this chat in {AGENT_NAMES.get(self._agent, 'Claude Code')}, in a terminal window")
        self.open_btn.setEnabled(enabled)
        self.open_more.setEnabled(enabled and has_app)
        self.open_btn.setVisible(True)

    def set_mic_state(self, state: str):
        self.composer._style_mic(state)

    def set_hint(self, text: str):
        self.composer.hint.setText(text or "")

    def set_tour_seen(self, seen: bool):
        self.composer.set_glow(not seen)

    def input_text(self) -> str:
        return self.composer.input.toPlainText()

    def set_input_text(self, text: str):
        self.composer.input.setPlainText(text)
        cur = self.composer.input.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.composer.input.setTextCursor(cur)

    def focus_input(self):
        self.composer.input.setFocus()

    # ---- the tail of the open chat ----
    def _make_tail(self, st, c):
        status, running = st.get("status"), bool(st.get("running"))
        req = st.get("approval")
        if req is not None:
            return self._approval_card(req, c, bool(st.get("bypass")))
        if st.get("codex_choice") and not running:
            return self._codex_choice_card(st["codex_choice"], c)
        if status == "error" and not running:
            return self._error_card(st.get("error") or "Something went wrong.", st.get("setup_kind") or "", c,
                                    st.get("setup_agent") or st.get("agent") or "claude")
        if status == "stopped" and not running:
            return self._note("◼  Stopped by you", c["muted"], c)
        if running:
            w = QWidget()
            w.setStyleSheet("background:transparent;")
            v = QVBoxLayout(w)
            v.setContentsMargins(36, 0, 0, 0)
            v.setSpacing(4)
            row = QHBoxLayout()
            row.setSpacing(8)
            d = _Dot(7)
            d.set_color(c["accent"])
            row.addWidget(d, 0, Qt.AlignVCenter)
            act = (st.get("last_action") or "").strip().replace("\n", " ")
            lab = QLabel(f"Working · {act[:120]}" if act else "Working…")
            lab.setStyleSheet(f"color:{c['muted']}; font-size:12.5px; background:transparent; border:none;")
            row.addWidget(lab, 1)
            v.addLayout(row)
            if st.get("queued"):
                q = QLabel(f"Next: “{st['queued'][:140]}” — sends when this step finishes")
                q.setWordWrap(True)
                q.setStyleSheet(f"color:{c['faint']}; font-size:12px; background:transparent; border:none;")
                v.addWidget(q)
            return w
        return None

    def _note(self, text, color, c):
        lab = QLabel(text)
        lab.setContentsMargins(36, 0, 0, 0)
        lab.setStyleSheet(f"color:{color}; font-size:12.5px; background:transparent; border:none;")
        return lab

    def _approval_card(self, req, c, full):
        card = QFrame()
        card.setObjectName("approvalCard")
        card.setStyleSheet(f"QFrame#approvalCard {{ background:{c['surface']}; border:1px solid {c['amber']};"
                           f" border-radius:14px; }} QLabel {{ background:transparent; border:none; }}")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(6)
        hd = QLabel("●  NEEDS YOUR OK")
        hd.setStyleSheet(f"color:{c['amber']}; font-size:11px; font-weight:700;")
        v.addWidget(hd)
        what, detail = approval_what(req)
        wl = QLabel(what)
        wl.setWordWrap(True)
        wl.setStyleSheet(f"color:{c['text']}; font-size:13.5px; font-weight:600;")
        v.addWidget(wl)
        if detail:
            dl = QLabel(detail[:600])
            dl.setWordWrap(True)
            dl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            dl.setStyleSheet(f"color:{c['muted']}; font-family:Consolas,monospace; font-size:11.5px;"
                             f" background:{c['code']}; border-radius:6px; padding:6px 8px;")
            v.addWidget(dl)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        deny = _btn("Deny", "denyBtn")
        deny.setFixedHeight(32)
        deny.setStyleSheet(
            f"QPushButton#denyBtn {{ background:{c['red_soft']}; border:none; border-radius:15px; padding:6px 18px;"
            f" color:{c['red']}; font-size:12.5px; font-weight:600; }}"
            f" QPushButton#denyBtn:hover {{ background:{c['red_soft']}; color:{c['red']}; border:1px solid {c['red']}; }}")
        deny.clicked.connect(lambda: self.approve.emit(False))
        row.addWidget(deny)
        ok = _btn("Approve", "approveBtn")
        ok.setFixedHeight(32)
        ok.setStyleSheet(
            f"QPushButton#approveBtn {{ background:{c['accent']}; border:none; border-radius:15px; padding:6px 18px;"
            f" color:{c['on_accent']}; font-size:12.5px; font-weight:600; }}"
            f" QPushButton#approveBtn:hover {{ background:{c['accent']}; color:{c['on_accent']}; border:none; }}")
        ok.clicked.connect(lambda: self.approve.emit(True))
        row.addWidget(ok)
        v.addLayout(row)
        if not full:
            nudge = QFrame()
            nudge.setObjectName("nudge")
            nudge.setStyleSheet(f"QFrame#nudge {{ background:{c['accent_soft']}; border:none; border-radius:10px; }}")
            nl = QHBoxLayout(nudge)
            nl.setContentsMargins(10, 7, 8, 7)
            nl.setSpacing(8)
            ic = QLabel()
            ic.setPixmap(line_pixmap("bolt", 14, c["accent"]))
            nl.addWidget(ic, 0, Qt.AlignTop)
            tx = QLabel("Asking you every time? With <b>Full access</b> it just does the work — like Claude's "
                        "bypass mode.")
            tx.setWordWrap(True)
            tx.setStyleSheet(f"color:{c['text']}; font-size:12px;")
            nl.addWidget(tx, 1)
            always = _btn("Approve && don't ask again", "alwaysBtn")
            always.setFixedHeight(28)
            always.setStyleSheet(
                f"QPushButton#alwaysBtn {{ background:transparent; border:1px solid {c['accent']}; border-radius:13px;"
                f" padding:4px 12px; color:{c['accent']}; font-size:12px; font-weight:600; }}"
                f" QPushButton#alwaysBtn:hover {{ background:{c['surface']}; color:{c['accent']}; border-color:{c['accent']}; }}")
            always.clicked.connect(self.approve_always.emit)
            nl.addWidget(always, 0, Qt.AlignVCenter)
            v.addWidget(nudge)
        wrap = QWidget()
        wrap.setStyleSheet("background:transparent;")
        wl2 = QHBoxLayout(wrap)
        wl2.setContentsMargins(36, 0, 0, 0)
        wl2.addWidget(card)
        return wrap

    def _codex_choice_card(self, choice, c):
        """A chat the Codex app (or Codex in a terminal) holds: close it and continue the SAME chat here, or carry
        on as a new chat with a recap. `choice`: {msg, busy}."""
        card = QFrame()
        card.setObjectName("choiceCard")
        card.setStyleSheet(f"QFrame#choiceCard {{ background:{c['surface']}; border:1px solid {c['amber']};"
                           f" border-radius:14px; }} QLabel {{ background:transparent; border:none; }}"
                           f" QCheckBox {{ color:{c['muted']}; font-size:12px; background:transparent; }}")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(6)
        hd = QLabel("●  OPEN IN CODEX")
        hd.setStyleSheet(f"color:{c['amber']}; font-size:11px; font-weight:700;")
        v.addWidget(hd)
        busy = choice.get("busy") or ""
        msg = choice.get("msg") or "This chat is open in the Codex app."
        if busy:
            msg += f" Codex is working on “{busy}” right now — closing it will stop that."
        else:
            msg += " Close Codex to continue it here — your Codex chats are saved."
        ml = QLabel(msg)
        ml.setWordWrap(True)
        ml.setStyleSheet(f"color:{c['text']}; font-size:13px;")
        v.addWidget(ml)
        row = QHBoxLayout()
        row.setSpacing(8)
        go = _btn("Close Codex anyway  →" if busy else "Close Codex and continue here  →", "closeCodexBtn")
        go.setFixedHeight(32)
        go.setStyleSheet(
            f"QPushButton#closeCodexBtn {{ background:{c['accent']}; border:none; border-radius:15px; padding:6px 16px;"
            f" color:{c['on_accent']}; font-size:12.5px; font-weight:600; }}"
            f" QPushButton#closeCodexBtn:hover {{ background:{c['accent']}; color:{c['on_accent']}; border:none; }}")
        new = _btn("Continue as a new chat", "newChatBtn")
        new.setFixedHeight(32)
        new.setStyleSheet(
            f"QPushButton#newChatBtn {{ background:transparent; border:1px solid {c['border']}; border-radius:15px;"
            f" padding:6px 16px; color:{c['text']}; font-size:12.5px; font-weight:600; }}"
            f" QPushButton#newChatBtn:hover {{ background:{c['hover']}; color:{c['text']}; border-color:{c['border_strong']}; }}")
        always = None
        if not busy:
            from PySide6.QtWidgets import QCheckBox
            always = QCheckBox("Always close Codex for this")
        go.clicked.connect(lambda: self.codex_choice.emit("close", bool(always is not None and always.isChecked())))
        new.clicked.connect(lambda: self.codex_choice.emit("new", False))
        row.addWidget(go)
        row.addWidget(new)
        row.addStretch(1)
        v.addLayout(row)
        if always is not None:
            v.addWidget(always)
        wrap = QWidget()
        wrap.setStyleSheet("background:transparent;")
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(36, 0, 0, 0)
        wl.addWidget(card)
        return wrap

    def _error_card(self, msg, kind, c, agent="claude"):
        card = QFrame()
        card.setObjectName("errorCard")
        setup = kind in ("install", "signin")
        border = c["amber"] if (setup or kind == "usage") else c["red"]
        card.setStyleSheet(f"QFrame#errorCard {{ background:{c['surface']}; border:1px solid {border}; border-radius:14px; }}"
                           f" QLabel {{ background:transparent; border:none; }}")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(6)
        head = {"install": "SETUP NEEDED", "signin": "SIGN-IN NEEDED", "usage": "OUT OF USAGE"}.get(kind, "IT DIDN'T WORK")
        hd = QLabel(f"●  {head}")
        hd.setStyleSheet(f"color:{border}; font-size:11px; font-weight:700;")
        v.addWidget(hd)
        ml = QLabel(msg[:900])
        ml.setWordWrap(True)
        ml.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ml.setStyleSheet(f"color:{c['text']}; font-size:13px;")
        v.addWidget(ml)
        if setup:
            row = QHBoxLayout()
            name = AGENT_NAMES.get(agent, "Claude Code")
            b = _btn(f"Set up {name} →" if kind == "install" else f"Sign in to {name} →", "setupBtn")
            b.setFixedHeight(32)
            b.setStyleSheet(
                f"QPushButton#setupBtn {{ background:{c['accent']}; border:none; border-radius:15px; padding:6px 16px;"
                f" color:{c['on_accent']}; font-size:12.5px; font-weight:600; }}"
                f" QPushButton#setupBtn:hover {{ background:{c['accent']}; color:{c['on_accent']}; border:none; }}")
            b.clicked.connect(lambda: self.setup_requested.emit(agent, kind))
            row.addWidget(b)
            row.addStretch(1)
            v.addLayout(row)
        wrap = QWidget()
        wrap.setStyleSheet("background:transparent;")
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(36, 0, 0, 0)
        wl.addWidget(card)
        return wrap

    # ---- folds + links ----
    def _toggle_fold(self, tid):
        self._opened ^= {tid}
        self._rerender()

    def _on_detail_link(self, url: str):
        if url.startswith("full:"):
            self._expanded ^= {url[5:]}
            self._rerender()
        else:
            open_path(url)

    def _use_idea(self, text):
        self.set_input_text(text)
        self.focus_input()

    # ---- pills + menus ----
    def _model_info(self, value):
        return next((m for m in self._models if m.get("value") == value), None)

    def model_name(self, value=None) -> str:
        v = self._model if value is None else value
        m = self._model_info(v)
        return (m or {}).get("name") or (v.capitalize() if v else "Default")

    def _sync_pills(self):
        c = self.c
        name = self.model_name()
        eff = EFFORT_NAMES.get(self._effort, "")
        txt = f"  {name}" + (f" · {eff}" if self._effort else "")
        self.composer.agent_pill.setText(txt)
        self.composer.agent_pill.setIcon(QIcon(agent_logo(self._agent, 15)))
        self.chip.setText(f"{AGENT_NAMES.get(self._agent, 'Claude Code')} · {name}")

    def _menu_css(self):
        c = self.c
        return (f"QMenu {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:10px; padding:6px; }}"
                f" QMenu::item {{ padding:6px 26px 6px 12px; border-radius:6px; color:{c['text']}; font-size:12.5px; }}"
                f" QMenu::item:selected {{ background:{c['hover']}; color:{c['text']}; }}"
                f" QMenu::item:disabled {{ color:{c['faint']}; }}"
                f" QMenu::separator {{ height:1px; background:{c['border']}; margin:5px 6px; }}"
                f" QMenu::indicator {{ width:14px; height:14px; }}")

    def _menu_header(self, menu, text):
        lab = QLabel(text)
        lab.setStyleSheet(f"color:{self.c['faint']}; font-size:10.5px; font-weight:700; padding:6px 12px 2px 12px;"
                          f" background:transparent;")
        act = QWidgetAction(menu)
        act.setDefaultWidget(lab)
        act.setEnabled(False)
        menu.addAction(act)

    def _agent_row(self, menu, agent):
        c = self.c
        sel = agent == self._primary
        w = _ClickRow(menu, lambda a=agent: self._pick_agent(a))
        w.setObjectName("agentRow")
        w.setStyleSheet(f"QWidget#agentRow {{ background:{c['hover'] if sel else 'transparent'}; border-radius:8px; }}"
                        f" QLabel {{ background:transparent; }}")
        h = QHBoxLayout(w)
        h.setContentsMargins(12, 6, 14, 6)
        h.setSpacing(10)
        lg = QLabel()
        lg.setPixmap(agent_logo(agent, 20))
        h.addWidget(lg)
        col = QVBoxLayout()
        col.setSpacing(0)
        a = QLabel(AGENT_NAMES[agent])
        a.setStyleSheet(f"color:{c['text']}; font-size:12.5px; font-weight:600; background:transparent;")
        b = QLabel(AGENT_SUBS[agent])
        b.setStyleSheet(f"color:{c['muted']}; font-size:11px; background:transparent;")
        col.addWidget(a)
        col.addWidget(b)
        h.addLayout(col, 1)
        ck = QLabel()
        ck.setFixedSize(14, 14)
        if sel:
            ck.setPixmap(line_pixmap("check", 14, c["accent"]))
        h.addWidget(ck)
        act = QWidgetAction(menu)
        act.setDefaultWidget(w)
        act.setText(f"{AGENT_NAMES[agent]} — {AGENT_SUBS[agent].lower()}")   # (screen readers / UI Automation)
        act.triggered.connect(lambda _=False, a=agent: self._pick_agent(a))     # keyboard: arrows + Enter
        menu.addAction(act)

    def _pick_agent(self, agent):
        """The agent row in the menu: it becomes the agent NEW chats use (an open chat keeps its own)."""
        for m in self.findChildren(QMenu):
            if m.isVisible():
                m.close()
        if agent != self._primary:
            self.agent_changed.emit(agent)

    def _agent_menu(self, anchor):
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_css())
        self._menu_header(menu, "AGENT" if self._chat_agent is None else "AGENT FOR NEW CHATS")
        for agent in ("claude", "codex"):
            self._agent_row(menu, agent)
        menu.addSeparator()
        self._menu_header(menu, "MODEL" if self._chat_agent is None
                          else f"MODEL — {AGENT_NAMES.get(self._agent, 'Claude Code').upper()}")
        claude = self._agent != "codex"
        current = [m for m in self._models if not (claude and str(m.get("value", "")).startswith("claude-"))]
        older = [m for m in self._models if claude and str(m.get("value", "")).startswith("claude-")]
        for m in current:
            a = menu.addAction(m["name"])
            a.setCheckable(True)
            a.setChecked(m["value"] == self._model)
            a.triggered.connect(lambda _=False, v=m["value"]: self._pick_model(v))
        if older:
            sub = menu.addMenu("Older versions")
            sub.setStyleSheet(self._menu_css())
            for m in older:
                a = sub.addAction(m["name"])
                a.setCheckable(True)
                a.setChecked(m["value"] == self._model)
                a.triggered.connect(lambda _=False, v=m["value"]: self._pick_model(v))
        info = self._model_info(self._model)
        levels = (info or {}).get("efforts") or []
        if levels:
            menu.addSeparator()
            self._menu_header(menu, "THINKING")
            for k in [""] + levels:
                a = menu.addAction(EFFORT_NAMES.get(k, k.capitalize()))
                a.setCheckable(True)
                a.setChecked(k == self._effort)
                a.triggered.connect(lambda _=False, e=k: self._pick_effort(e))
        menu.addSeparator()
        foot = QLabel("If one isn't set up or runs out of usage, Filect switches to the other on its own."
                      + (" A chat keeps the agent it started with." if self._chat_agent else ""))
        foot.setWordWrap(True)
        foot.setFixedWidth(280)
        foot.setStyleSheet(f"color:{self.c['muted']}; font-size:11px; padding:4px 12px 6px 12px; background:transparent;")
        fa = QWidgetAction(menu)
        fa.setDefaultWidget(foot)
        fa.setEnabled(False)
        menu.addAction(fa)
        menu.exec(anchor.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 6)))

    def _pick_model(self, value):
        self._model = value
        info = self._model_info(value)
        if self._effort and self._effort not in ((info or {}).get("efforts") or []):
            self._effort = ""
        self._sync_pills()
        self.model_changed.emit(self._model, self._effort)

    def _pick_effort(self, effort):
        self._effort = effort
        self._sync_pills()
        self.model_changed.emit(self._model, self._effort)

    def _access_row(self, menu, full, title, line):
        c = self.c
        w = _ClickRow(menu, lambda f=full: self._pick_access(f))
        w.setObjectName("accessRow")
        sel = full == self._full
        w.setStyleSheet(f"QWidget#accessRow {{ background:{c['hover'] if sel else 'transparent'}; border-radius:8px; }}"
                        f" QLabel {{ background:transparent; }}")
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 7, 12, 7)
        h.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(line_pixmap("bolt" if full else "shield", 16, c["accent"] if full else c["amber"]))
        h.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        a = QLabel(title)
        a.setStyleSheet(f"color:{c['text']}; font-size:12.5px; font-weight:600;")
        b = QLabel(line)
        b.setWordWrap(True)
        b.setFixedWidth(250)
        b.setStyleSheet(f"color:{c['muted']}; font-size:11px;")
        col.addWidget(a)
        col.addWidget(b)
        h.addLayout(col, 1)
        if sel:
            ck = QLabel()
            ck.setPixmap(line_pixmap("check", 14, c["accent"]))
            h.addWidget(ck, 0, Qt.AlignTop)
        act = QWidgetAction(menu)
        act.setDefaultWidget(w)
        act.setText(title)                       # its name for screen readers / UI Automation
        act.triggered.connect(lambda _=False, f=full: self._pick_access(f))   # keyboard: arrows + Enter
        menu.addAction(act)

    def _access_menu(self, anchor):
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_css())
        self._access_row(menu, False, "Ask first", "Asks you before it changes files or runs commands. Codex "
                                                   "can't ask — it works only in the chat's folder, without internet.")
        self._access_row(menu, True, "Full access", "Does the work without asking — like Claude's bypass mode. "
                                                    "Your MCP tools included.")
        menu.exec(anchor.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 6)))

    def _pick_access(self, full):
        if full != self._full:
            self.set_access(full)
            self.access_changed.emit(full)

    def _open_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_css())
        other = "terminal" if self._open_default == "app" else "app"
        a = menu.addAction("Open in Terminal" if other == "terminal" else f"Open in {APP_NAMES.get(self._agent, 'Claude')} app")
        a.triggered.connect(lambda: self.open_in.emit(other))
        menu.exec(self.open_more.mapToGlobal(QPoint(self.open_more.width() - menu.sizeHint().width(),
                                                    self.open_more.height() + 4)))

    # ---- tour ----
    def open_tour(self):
        win = self.window()
        self.tour = Tour(self.c, win)
        self.tour.closed.connect(self._tour_closed)
        self.tour.open()
        self.tour_opened.emit()
        self.composer.set_glow(False)

    def _tour_closed(self):
        self.tour = None
