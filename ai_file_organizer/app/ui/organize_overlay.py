"""
Voice Organize overlay — the floating card that slides down from the top when
you voice-organize. Ported to match the macOS pack pixel-for-pixel (464-wide
card, persistent "+ Organize" header, arc spinner, gradient waveform):

  thinking  → spinner + message ("Finding the folder…")
  listening → 11-bar waveform + "Listening… tap Stop when you're done" + Stop
  plan      → "Move N files into M folders" + target (+ Change) + folder groups
              + "Change by voice" mic + Cancel / ✓ Organize  (≥30 → two-click)
  applying  → spinner + "Organizing…"
  done      → ✓ + "Organized N files. Revert anytime in History." (auto-dismiss)
  error     → ⚠ + message + full-width "Choose folder…" fallback

Non-activating (WS_EX_NOACTIVATE) so it floats over other apps and takes mouse
clicks WITHOUT stealing keyboard focus. Anchored top-centre of the work area.
Pure-Qt painting; no AppKit/Spaces code.

Signals (consumed by VoiceOrganizeController):
  refine_requested, organize_clicked, change_folder_requested, dismissed
"""
import logging

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QGraphicsDropShadowEffect,
)
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon

from app.ui.icons import AnimatedWaveform, Spinner, line_pixmap, ACCENT

logger = logging.getLogger(__name__)

_CARD_W = 464

# palette (macOS pack)
_BG        = "#0D0D13"
_BORDER    = "#23232E"
_CARD2     = "#16161E"   # inset rows / folder cards
_CARD2_BRD = "#24242F"
_TITLE     = "#F2F2F7"
_MUTED     = "#9A9AA8"
_ACCENT    = ACCENT
_ACCENT_HD = "#8B6FFF"   # "+ Organize" header


class OrganizeOverlay(QWidget):
    refine_requested = Signal()
    organize_clicked = Signal()
    change_folder_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(_CARD_W + 28)   # +room for the glow

        self._refining = False
        self._armed = False
        self._waveform = None
        self._collapsed = False
        self._tab = None
        self._dismiss_timer = None
        self._thinking_lbl = None

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
        htitle = QLabel("+ Organize")
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

        # ---- body (rebuilt per state) ----
        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(10)
        card_lay.addLayout(self._body)

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

    # ----- lifecycle -----
    def present(self):
        # A fresh request: drop any leftover collapsed tab and cancel a pending
        # auto-dismiss from a previous 'done' so it can't close the new flow.
        self._cancel_dismiss_timer()
        self._collapsed = False
        if self._tab is not None:
            self._tab.hide()
        self._position_top_center()
        self.show()
        self.raise_()

    def dismiss(self):
        self._cancel_dismiss_timer()
        self.hide()
        if self._tab is not None:
            self._tab.hide()
        self._collapsed = False
        self.dismissed.emit()

    def _cancel_dismiss_timer(self):
        if self._dismiss_timer is not None:
            self._dismiss_timer.stop()
            self._dismiss_timer = None

    # ----- collapse / expand (tuck up to the top edge, like the Mac notch) -----
    def mousePressEvent(self, e):
        # A click on the card body (not a button/child that handled it) tucks
        # the overlay back up to the top of the screen.
        if not self._collapsed:
            self._collapse()
        super().mousePressEvent(e)

    def _ensure_tab(self):
        if self._tab is None:
            self._tab = _CollapsedTab()
            self._tab.clicked.connect(self._expand)

    def _collapse(self):
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
        """Surface the card if the user had it collapsed (used when a new plan
        or result arrives, so important updates pop back down)."""
        if self._collapsed:
            self._expand()

    def set_level(self, level: float):
        if self._waveform is not None:
            self._waveform.set_level(level)

    def set_refining(self, on: bool):
        self._refining = on

    # ----- layout helpers -----
    def _clear(self):
        self._cancel_dismiss_timer()
        self._waveform = None
        self._thinking_lbl = None
        self._clear_layout(self._body)

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
        lbl.setStyleSheet(
            f"color:{color}; font-size:{size}px; font-weight:{weight}; background:transparent;")
        return lbl

    def _resize(self):
        self.adjustSize()
        self._position_top_center()

    def _position_top_center(self):
        try:
            from PySide6.QtGui import QGuiApplication
            scr = QGuiApplication.primaryScreen().availableGeometry()
            self.adjustSize()
            x = scr.center().x() - self.width() // 2
            y = scr.top()   # flush with the very top of the screen
            self.move(x, y)
        except Exception:
            pass

    # ================= STATES =================
    def show_thinking(self, msg="Analyzing your files…"):
        # If a thinking view is already up (status updates fire rapidly during
        # indexing), just update the TEXT — don't rebuild. Recreating the Spinner
        # each time left ghost spinners overlapping (the "double" artifact) and
        # reset the animation every frame.
        lbl = getattr(self, "_thinking_lbl", None)
        if lbl is not None:
            try:
                lbl.setText(msg)
                self._resize()
                return
            except RuntimeError:
                self._thinking_lbl = None
        self._clear()
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addStretch()
        row.addWidget(Spinner(color=_ACCENT, size=22))
        self._thinking_lbl = self._label(msg, color="#D6D6E0", size=15, wrap=False)
        row.addWidget(self._thinking_lbl)
        row.addStretch()
        self._pad(row, 18)
        self._resize()

    def show_applying(self):
        self.show_thinking("Organizing…")

    def show_listening(self, prompt="Listening… tap Stop when you're done"):
        self._clear()
        self._waveform = AnimatedWaveform(color=_ACCENT, bars=11, width=_CARD_W - 120,
                                          height=56, bar_width=5.0)
        wrow = QHBoxLayout()
        wrow.addStretch()
        wrow.addWidget(self._waveform)
        wrow.addStretch()
        self._body.addLayout(wrow)

        cap = self._label(prompt, color=_MUTED, size=13, wrap=False)
        cap.setAlignment(Qt.AlignCenter)
        self._body.addWidget(cap)

        stop = QPushButton("■  Stop")
        stop.setCursor(Qt.PointingHandCursor)
        stop.setMinimumHeight(46)
        stop.setStyleSheet(self._primary_btn())
        stop.clicked.connect(self.refine_requested)
        self._body.addWidget(stop)
        self._resize()

    def show_plan(self, summary, folders, target_folder, file_count, folder_count,
                  move_count=None):
        self._ensure_expanded()
        self._clear()
        self._armed = False
        if move_count is None:
            move_count = file_count

        self._body.addWidget(self._label(summary, color=_TITLE, size=19, weight=700))

        # Target folder card: glyph + path + Change (purple text link)
        tcard = QFrame()
        tcard.setStyleSheet(
            f"background:{_CARD2}; border:1px solid {_CARD2_BRD}; border-radius:10px;")
        tl = QHBoxLayout(tcard)
        tl.setContentsMargins(14, 11, 12, 11)
        tl.setSpacing(10)
        gl = QLabel()
        gl.setPixmap(line_pixmap("folder", 17, "#C9C9D4"))
        gl.setFixedWidth(20)
        tl.addWidget(gl)
        tpath = QLabel(target_folder)
        tpath.setStyleSheet(f"color:#C9C9D4; font-size:13px; background:transparent;")
        tpath.setToolTip(target_folder)
        tl.addWidget(tpath, 1)
        change = QPushButton("Change")
        change.setCursor(Qt.PointingHandCursor)
        change.setStyleSheet(
            f"QPushButton{{background:transparent; color:{_ACCENT}; border:none; "
            f"font-size:13px; font-weight:600;}} QPushButton:hover{{color:#A58BFF;}}")
        change.clicked.connect(self.change_folder_requested)
        tl.addWidget(change)
        self._body.addWidget(tcard)

        # Collapsible folder groups (scroll if many)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMaximumHeight(300)
        scroll.setStyleSheet("QScrollArea{border:none; background:transparent;}")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        ilay = QVBoxLayout(inner)
        ilay.setContentsMargins(0, 0, 0, 0)
        ilay.setSpacing(8)
        for name, filenames in folders.items():
            ilay.addWidget(self._folder_group(name, filenames))
        ilay.addStretch()
        scroll.setWidget(inner)
        self._body.addWidget(scroll)

        # "Change by voice" mic row
        hint = QHBoxLayout()
        mic = QPushButton()
        mic.setCursor(Qt.PointingHandCursor)
        mic.setFixedSize(34, 34)
        mic.setIcon(QIcon(line_pixmap("mic", 16, "#FFFFFF")))
        mic.setIconSize(QSize(16, 16))
        mic.setStyleSheet(
            f"QPushButton{{background:{_ACCENT}; border:none; border-radius:17px;}}"
            f"QPushButton:hover{{background:#8F63FF;}}")
        mic.clicked.connect(self.refine_requested)
        hint.addWidget(mic)
        hint.addWidget(self._label("Change by voice — tap the mic", color=_MUTED,
                                    size=13, wrap=False))
        hint.addStretch()
        self._body.addLayout(hint)

        # Cancel / Organize — the button reflects the ACTUAL number of files
        # that will move (not the total shown above), and the big-batch two-click
        # confirm triggers on the real move count.
        big = move_count >= 30
        footer = QHBoxLayout()
        footer.setSpacing(10)
        cancel = QPushButton("Cancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setMinimumHeight(46)
        cancel.setStyleSheet(self._ghost_btn())
        cancel.clicked.connect(self.dismiss)
        footer.addWidget(cancel)

        self._organize_btn = QPushButton()
        self._organize_btn.setMinimumHeight(46)
        if move_count == 0:
            # Already arranged this way — not a dead-end. The user organized on
            # purpose, so invite a new arrangement via the refine mic above.
            self._organize_btn.setText("Already arranged — tap the mic to change")
            self._organize_btn.setEnabled(False)
            self._organize_btn.setStyleSheet(
                "QPushButton{background:#1E1E28; color:#7E7E8C; border:1px solid #2C2C38; "
                "border-radius:12px; font-size:14px; font-weight:600;}")
        else:
            self._organize_btn.setText(f"✓  Organize — {move_count} moving")
            self._organize_btn.setCursor(Qt.PointingHandCursor)
            self._organize_btn.setStyleSheet(self._primary_btn())
            self._organize_btn.clicked.connect(lambda: self._on_organize(big, move_count))
        footer.addWidget(self._organize_btn, 1)
        self._body.addLayout(footer)
        self._resize()

    def _on_organize(self, big, move_count):
        if big and not self._armed:
            self._armed = True
            self._organize_btn.setText(f"Move {move_count} files? Click to confirm")
            self._organize_btn.setStyleSheet(self._primary_btn(confirm=True))
            return
        self.organize_clicked.emit()

    def show_done(self, revert_hint="Revert anytime in History."):
        self._ensure_expanded()
        self._clear()
        msg = revert_hint or "Done."
        # One wrapping label (check inline as rich text) added straight to the
        # vertical body — a QHBoxLayout of [check | wrapping label] does NOT
        # propagate heightForWidth, so the 2nd line was getting clipped.
        from PySide6.QtCore import Qt as _Qt
        lbl = QLabel(f'<span style="color:{_ACCENT}; font-weight:800;">✓</span>&nbsp;&nbsp;{msg}')
        lbl.setTextFormat(_Qt.RichText)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#D6D6E0; font-size:14px; background:transparent;")
        self._body.addWidget(lbl)
        self._resize()
        # Auto-dismiss, but keep it up long enough to READ a long message (it was
        # getting cut off by the old fixed 3s). Scale with length; cancellable so
        # a new request within the window isn't closed by this stale timer.
        ms = max(4000, min(9000, 2500 + len(msg) * 55))
        self._cancel_dismiss_timer()
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.dismiss)
        self._dismiss_timer.start(ms)

    def show_error(self, msg, allow_pick_folder=False):
        self._ensure_expanded()
        self._clear()
        row = QHBoxLayout()
        warn = QLabel("⚠")
        warn.setStyleSheet("color:#F2A33C; font-size:18px; background:transparent;")
        warn.setFixedWidth(24)
        warn.setAlignment(Qt.AlignTop)
        row.addWidget(warn)
        row.addWidget(self._label(msg, color="#D6D6E0", size=14), 1)
        self._body.addLayout(row)
        if allow_pick_folder:
            pick = QPushButton("📁  Choose folder…")
            pick.setCursor(Qt.PointingHandCursor)
            pick.setMinimumHeight(46)
            pick.setStyleSheet(self._primary_btn())
            pick.clicked.connect(self.change_folder_requested)
            self._body.addWidget(pick)
        else:
            close = QPushButton("Close")
            close.setCursor(Qt.PointingHandCursor)
            close.setMinimumHeight(44)
            close.setStyleSheet(self._ghost_btn())
            close.clicked.connect(self.dismiss)
            self._body.addWidget(close)
        self._resize()

    # ----- pieces -----
    def _pad(self, inner_layout, v):
        wrap = QVBoxLayout()
        wrap.setContentsMargins(0, v, 0, v)
        wrap.addLayout(inner_layout)
        self._body.addLayout(wrap)

    def _folder_group(self, name, entries):
        # Each entry is (filename, is_moving); accept bare strings too (treated
        # as moving) for backward compatibility.
        norm = [(e if isinstance(e, (tuple, list)) else (e, True)) for e in entries]
        total = len(norm)
        wrap = QFrame()
        wrap.setStyleSheet(
            f"background:{_CARD2}; border:1px solid {_CARD2_BRD}; border-radius:10px;")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        header = QPushButton(f"▸  {name}  ({total})")
        header.setCursor(Qt.PointingHandCursor)
        header.setStyleSheet(
            "QPushButton{background:transparent; color:#E8E8F0; border:none; "
            "font-size:14px; font-weight:600; text-align:left; padding:0;}")
        body = QWidget()
        body.setVisible(False)
        body.setStyleSheet("background:transparent;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(14, 4, 0, 2)
        blay.setSpacing(1)
        for fn, moving in norm[:50]:
            if moving:
                fl = QLabel("• " + str(fn))
                fl.setStyleSheet("color:#9A9AA8; font-size:12px; background:transparent;")
            else:
                # Already in this folder — dimmed, tagged, so you see the full
                # end-state but know it won't move.
                fl = QLabel(f"• {fn}   · already here")
                fl.setStyleSheet("color:#5E5E6B; font-size:12px; background:transparent;")
            blay.addWidget(fl)
        if total > 50:
            more = QLabel(f"…and {total - 50} more")
            more.setStyleSheet("color:#7A7A90; font-size:12px; background:transparent;")
            blay.addWidget(more)

        def toggle():
            vis = not body.isVisible()
            body.setVisible(vis)
            header.setText(f"{'▾' if vis else '▸'}  {name}  ({total})")
            self._resize()
        header.clicked.connect(toggle)
        lay.addWidget(header)
        lay.addWidget(body)
        return wrap

    def _ghost_btn(self):
        return ("QPushButton{background:transparent; color:#C3C3CE; border:1px solid #33333F; "
                "border-radius:11px; font-size:14px; font-weight:600; padding:0 22px;}"
                "QPushButton:hover{border-color:#7C4DFF; color:#FFFFFF;}")

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
        t = QLabel("+ Organize")
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
