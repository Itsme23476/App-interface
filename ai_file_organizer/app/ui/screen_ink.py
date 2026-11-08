"""Draw on your screen in red and snapshot it for Claude — the fn + ⌃ + Space (or Shift) gesture.

While you hold fn + ⌃ (talking to Claude), the draw key turns the screen into a red-ink layer:
click-drag draws. TAP it and drawing stays on until you tap again; HOLD it and drawing lasts while
held. Either way the end of drawing snapshots the screen EXACTLY as you see it at that instant — minus
Filect's own floating windows — with your marks painted on at full resolution, then clears the ink.
Releasing fn sends what you said + every snapshot to Claude (DictationController wires the gesture;
this module is just the ink + capture).

Live, not frozen (Damianos' call): between Space presses the layer is click-through, so you can scroll
or switch to the next thing and mark again — one message can carry several snapshots.

Windows port: the capture is Qt's screen grab; Filect's own floating windows (this layer, the card, the
rail, the voice pill) are left out of it by SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) — see
win_panel. Click-through between drawings = WS_EX_TRANSPARENT. While drawing, the layer is filled with an
alpha-1 (invisible) wash: a fully transparent pixel would let the mouse fall through to the app beneath.
No permission is needed on Windows.
"""
from __future__ import annotations
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPointF, QRect, QRectF
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

IS_WIN = sys.platform.startswith("win")
try:
    if IS_WIN:
        from . import win_panel as notch_panel
    else:
        from . import notch_panel
except ImportError:                      # running this file directly
    import win_panel as notch_panel

logger = logging.getLogger(__name__)

INK_TITLE = "Filect Screen Ink"            # unique — notch_panel finds the NSWindow by this
INK_LEVEL = 1002                            # above the notch/rail panels (1000) and the menu bar
RED = "#FF3B30"
STROKE_W = 4.5                              # logical px; scaled up to the capture's pixel density
SCREENSHOT_DIR = Path.home() / "Filect Assistant" / "Screenshots"   # the assistant's workspace


def thumb_path(path: str) -> str:
    """Where a screenshot's small preview lives (details_view shows it in the notch)."""
    return str(path)[:-4] + ".thumb.png"


def _save(img: QImage, path: str) -> None:
    """Write the screenshot + its thumbnail, each appearing atomically (QImage is fine off the UI thread)."""
    try:
        img.save(path + ".part", "PNG")
        os.replace(path + ".part", path)
        thumb = img.scaledToWidth(min(720, img.width()), Qt.SmoothTransformation)
        thumb.save(thumb_path(path) + ".part", "PNG")
        os.replace(thumb_path(path) + ".part", thumb_path(path))
    except Exception as e:
        logger.warning(f"[screen] saving {path} failed: {e}")


def _capture_win(rect: QRect) -> Optional[QImage]:
    """Windows: the screen under `rect` (global logical px) at full pixel resolution. Our floating windows
    carry WDA_EXCLUDEFROMCAPTURE, so the grab shows what's BENEATH them (the main window stays in)."""
    try:
        scr = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        g = scr.geometry()
        pm = scr.grabWindow(0, rect.x() - g.x(), rect.y() - g.y(), rect.width(), rect.height())
        if pm.isNull():
            return None
        img = pm.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(1.0)                 # pixel coordinates from here on
        return img
    except Exception as e:
        logger.warning(f"[screen] capture failed: {e}")
        return None


def _capture(rect: QRect) -> Optional[QImage]:
    """The screen inside `rect` (global points) exactly as you see it — minus Filect's own floating
    windows (this ink layer, the notch, the rail, the dictation pill; the main window stays) — at full
    pixel resolution. None if macOS Screen Recording isn't allowed yet (asks once)."""
    if IS_WIN:
        return _capture_win(rect)
    if sys.platform != "darwin":
        return None
    try:
        import Quartz as Q
        if not Q.CGPreflightScreenCaptureAccess():
            Q.CGRequestScreenCaptureAccess()          # macOS's one-time permission prompt
            logger.warning("[screen] Screen Recording not allowed yet — asked macOS")
            return None
        pid = os.getpid()
        infos = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID) or []
        ids = [w["kCGWindowNumber"] for w in infos
               if not (w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", 0) != 0)]
        img = Q.CGWindowListCreateImageFromArray(
            Q.CGRectMake(rect.x(), rect.y(), rect.width(), rect.height()), ids, Q.kCGWindowImageDefault)
        if img is None:
            return None
        w, h, bpr = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img), Q.CGImageGetBytesPerRow(img)
        raw = bytes(Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(img)))
        return QImage(raw, w, h, bpr, QImage.Format_ARGB32_Premultiplied).copy()
    except Exception as e:
        logger.warning(f"[screen] capture failed: {e}")
        return None


class ScreenInk(QWidget):
    """A transparent full-screen layer on the display under the cursor. Click-through unless drawing."""

    def __init__(self, title: str = INK_TITLE):
        super().__init__()
        # Each ink layer needs its OWN title: the native window is found by title (fn + ⌃'s layer and
        # Filect Assistant's ⌘⇧D layer must never configure each other).
        self._title = title
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle(title)
        self._strokes: list[list[QPointF]] = []
        self._drawing = False
        self._hint = ""
        self._banner = ""                  # standing hint while NOT drawing (receive mode)
        self._shots = 0                   # snapshots taken this gesture (pill + file names)
        self._flash = 0.0                  # shutter flash after a snapshot (fades out)
        self._toast = ""                   # "✓ Screenshot 2" right after a snapshot
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(16)
        self._flash_timer.timeout.connect(self._flash_step)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._clear_toast)

    # -- gesture lifecycle (driven by DictationController) ---------------------------------------
    def begin(self) -> None:
        """Cover the display under the cursor (first Space press of a gesture)."""
        scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.setGeometry(scr.geometry())
        self._strokes, self._shots, self._toast = [], 0, ""
        self.show()
        self._configure()

    def set_drawing(self, on: bool, hint: str = "release to snapshot") -> None:
        """Drawing → take the mouse and show the red edge + `hint`; off → click-through again."""
        self._drawing, self._hint = on, hint
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        self._apply_native()
        self.update()

    def clear(self) -> None:
        """Drop the marks drawn so far (Esc on a right-click drawing)."""
        self._strokes = []
        self.update()

    def set_banner(self, text: str) -> None:
        """A standing hint shown whenever you're not drawing (e.g. "Receiving · 2 screenshots …")."""
        self._banner = text
        self.update()

    def snapshot(self):
        """The screen right now + your marks → a PNG (and a small .thumb.png). Returns
        (path, had_marks), or None if the screen couldn't be captured. Clears the marks."""
        img = _capture(self.geometry())
        had_marks = bool(self._strokes)
        if img is None:
            self._strokes = []
            self.update()
            return None
        s = img.width() / max(1, self.width())         # logical → pixel (2 on Retina)
        if had_marks:
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(RED), STROKE_W * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            for pts in self._strokes:
                if len(pts) == 1:
                    p.drawPoint(pts[0] * s)
                else:
                    p.drawPolyline(QPolygonF([q * s for q in pts]))
            p.end()
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        self._shots += 1
        # Unique per snapshot: a per-gesture counter repeated across back-to-back gestures in the same
        # second, and two save threads then fought over one file (one screenshot overwrote the other).
        path = SCREENSHOT_DIR / f"screen-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
        # The capture IS the moment, so it's taken right here; writing a ~7-megapixel PNG took ~0.5s,
        # which froze the screen on every Space release — so it's written off the UI thread. Each file
        # appears atomically (".part" + rename): readers (agent_runner) never see a half-written one.
        threading.Thread(target=_save, args=(img, str(path)), daemon=True,
                         name="filect-screenshot-save").start()
        self._strokes = []
        self._toast = f"✓  Screenshot {self._shots}"
        self._toast_timer.start(1200)
        self._flash = 0.35
        self._flash_timer.start()
        logger.info(f"[screen] snapshot {self._shots} ({img.width()}x{img.height()}, marks={had_marks}) → {path}")
        return str(path), had_marks

    def end(self) -> None:
        """The gesture is over (fn released / cancelled): hide and forget the ink."""
        self._drawing, self._strokes, self._banner = False, [], ""
        self._flash_timer.stop()
        self.hide()

    # -- drawing ----------------------------------------------------------------------------------
    # Any button draws: you're holding ⌃ for the gesture, and Qt turns ⌃-click into a RIGHT click on
    # macOS — accepting only the left button silently dropped every stroke on the real gesture.
    def mousePressEvent(self, e) -> None:
        if self._drawing:
            self._strokes.append([e.position()])
            self.update()

    def mouseMoveEvent(self, e) -> None:
        if self._drawing and e.buttons() and self._strokes:
            self._strokes[-1].append(e.position())
            self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._drawing and IS_WIN:
            p.fillRect(self.rect(), QColor(0, 0, 0, 1))     # (catches the mouse; invisible)
        if self._flash > 0:
            p.fillRect(self.rect(), QColor(255, 255, 255, int(255 * self._flash)))
        if self._drawing:                                   # you're in draw mode: a red edge
            p.setPen(QPen(QColor(255, 59, 48, 200), 6))
            p.drawRect(self.rect().adjusted(3, 3, -3, -3))
        p.setPen(QPen(QColor(RED), STROKE_W, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        for pts in self._strokes:
            if len(pts) == 1:
                p.drawPoint(pts[0])
            else:
                p.drawPolyline(QPolygonF(pts))
        label = (f"●  Drawing — {self._hint}" if self._drawing else self._toast or self._banner)
        if label:
            f = p.font()
            f.setPixelSize(13)
            f.setBold(True)
            p.setFont(f)
            w = p.fontMetrics().horizontalAdvance(label) + 32
            r = QRectF((self.width() - w) / 2, self.height() - 96, w, 34)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 20, 24, 235))
            p.drawRoundedRect(r, 17, 17)
            p.setPen(QColor(RED) if self._drawing else QColor("#ECECEC"))
            p.drawText(r, Qt.AlignCenter, label)
        p.end()

    # -- internals ---------------------------------------------------------------------------------
    def _flash_step(self) -> None:
        self._flash = max(0.0, self._flash - 0.05)
        if self._flash <= 0:
            self._flash_timer.stop()
        self.update()

    def _clear_toast(self) -> None:
        self._toast = ""
        self.update()

    def _configure(self) -> None:
        if IS_WIN:
            notch_panel.configure_panel(self)                # non-activating, topmost, out of captures
            notch_panel.set_click_through(self, not self._drawing)
            return
        notch_panel.configure_macos_panel(self._title)      # non-activating, all Spaces, no focus steal
        self._apply_native()
        QTimer.singleShot(10, self._apply_native)
        QTimer.singleShot(60, self._apply_native)

    def _apply_native(self) -> None:
        """Above the other panels, and click-through unless you're drawing."""
        if IS_WIN:
            notch_panel.set_click_through(self, not self._drawing)
            if self.isVisible():
                self.raise_()
            return
        ns = notch_panel._ns_window(self._title)
        if ns is None:
            return
        try:
            ns.setLevel_(INK_LEVEL)
            ns.setIgnoresMouseEvents_(not self._drawing)
        except Exception:
            pass
