"""Windows mechanics for Filect's floating panels — the top-centre card, the left rail, the screen-ink layer.

The Windows twin of the Mac's notch_panel.py, with the SAME function names, so claude_overlay.py /
conversation_rail.py / screen_ink.py port unchanged (`from . import win_panel as notch_panel`).

What a panel needs on Windows (guide §2):
  * never steal focus from the app you're in  → WS_EX_NOACTIVATE (+ Qt's WindowDoesNotAcceptFocus /
    WA_ShowWithoutActivating), no taskbar button → WS_EX_TOOLWINDOW, above other windows → WS_EX_TOPMOST;
  * left out of Filect's own screenshots → SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) (Win10 2004+);
  * click-through when it shows nothing → WS_EX_TRANSPARENT (with WS_EX_LAYERED);
  * typing into it (rail search, the card's type field) → drop WS_EX_NOACTIVATE and activate on that USER
    click only; put it back when the field loses focus.
Windows has no notch: notch_width() / notch_inset_top() are 0, so the card falls back to its own strip.

Every call is wrapped: a panel must still construct headless (QT_QPA_PLATFORM=offscreen) and off Windows.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)
IS_WIN = sys.platform.startswith("win")

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0010, 0x0020
HWND_TOPMOST = -1


def _user32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, wintypes.UINT]
    return u


_FG = None


def _fg_api():
    """GetForegroundWindow / SetForegroundWindow / IsWindow with HWND types, on a PRIVATE user32 handle — so
    the restype never changes what other Filect code gets back from the shared ctypes.windll.user32."""
    global _FG
    if _FG is None:
        import ctypes
        from ctypes import wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetForegroundWindow.argtypes = []
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        _FG = u
    return _FG


def _offscreen() -> bool:
    try:
        from PySide6.QtGui import QGuiApplication
        return QGuiApplication.platformName() in ("offscreen", "minimal")
    except Exception:
        return True


def _widget(win_title: str):
    """The visible top-level Qt widget with this window title (titles are unique per panel)."""
    try:
        from PySide6.QtWidgets import QApplication
        for w in QApplication.topLevelWidgets():
            if w.windowTitle() == win_title:
                return w
    except Exception:
        pass
    return None


def hwnd_of(widget) -> int:
    try:
        return int(widget.winId()) if widget is not None else 0
    except Exception:
        return 0


# ---- the Mac API (same names) -------------------------------------------------------------------
def _ns_window(win_title: str):
    """Mac: the NSWindow for a title. Windows: there's none — callers branch on None."""
    return None


def notch_screen_geo():
    return None


def notch_inset_top() -> int:
    return 0


def notch_width() -> int:
    return 0


def screen_geo():
    """The display the panel anchors to: the one under the cursor, else the primary (available area,
    so the card never hides behind a top taskbar)."""
    try:
        from PySide6.QtGui import QGuiApplication, QCursor
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        return screen.geometry() if screen is not None else None
    except Exception:
        return None


_FS = None


def fullscreen_app_in_front(widget=None) -> bool:
    """Is the window in front a FULLSCREEN app (a video, a game, a slideshow) on its screen — so the resting pill
    shouldn't sit on top of it? Fullscreen = it covers its whole monitor, taskbar included (a maximized window
    leaves the taskbar out, so it doesn't count). The desktop and the taskbar never count, nor Filect itself."""
    global _FS
    try:
        import ctypes
        from ctypes import wintypes
        if _FS is None:
            u = ctypes.WinDLL("user32")
            u.GetForegroundWindow.restype = wintypes.HWND
            u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            u.MonitorFromWindow.restype = wintypes.HMONITOR
            u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

            class MI(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD)]
            u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MI)]
            _FS = (u, MI)
        u, MI = _FS
        h = u.GetForegroundWindow()
        if not h:
            return False
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        import os
        if pid.value == os.getpid():
            return False
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(h, cls, 64)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False
        r = wintypes.RECT()
        if not u.GetWindowRect(h, ctypes.byref(r)):
            return False
        mi = MI()
        mi.cbSize = ctypes.sizeof(MI)
        if not u.GetMonitorInfoW(u.MonitorFromWindow(h, 2), ctypes.byref(mi)):
            return False
        m = mi.rcMonitor
        return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom
    except Exception:
        return False


def configure_macos_panel(win_title: str) -> None:
    """Make the titled window a non-activating, topmost tool window left out of screen captures."""
    configure_panel(_widget(win_title))


def place_native(win_title: str, x: int, y: int, w: int, h: int) -> None:
    """Mac re-places the NSWindow over the menu bar. Windows: Qt's own geometry is right — nothing to do."""
    return None


def reassert_panel_style(win_title: str) -> None:
    """Qt can rebuild a window's ex-style on some changes; put the panel bits back (idempotent)."""
    w = _widget(win_title)
    if w is not None and getattr(w, "_win_panel", False):
        _apply(w)


# ---- Windows specifics ----------------------------------------------------------------------------
def configure_panel(widget, exclude_from_capture: bool = True) -> None:
    """Non-activating + topmost + no taskbar button (+ left out of Filect's screenshots). Call after show()."""
    if widget is None or not IS_WIN or _offscreen():
        return
    widget._win_panel = True
    widget._win_panel_capture = bool(exclude_from_capture)
    _apply(widget)


def _apply(widget) -> None:
    hwnd = hwnd_of(widget)
    if not hwnd:
        return
    try:
        u = _user32()
        ex = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        want = ex | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        if not getattr(widget, "_win_typing", False):
            want |= WS_EX_NOACTIVATE
        if getattr(widget, "_win_click_through", False):
            want |= WS_EX_TRANSPARENT | WS_EX_LAYERED
        else:
            want &= ~WS_EX_TRANSPARENT
        if want != ex:
            u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want)
            u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                           SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        # (FILECT_PANELS_VISIBLE_IN_SCREENSHOTS=1: testing only — lets a screenshot tool see the panels)
        if getattr(widget, "_win_panel_capture", True) and not getattr(widget, "_win_affinity_done", False) \
                and os.environ.get("FILECT_PANELS_VISIBLE_IN_SCREENSHOTS") != "1":
            if u.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
                widget._win_affinity_done = True
    except Exception as e:
        logger.debug(f"[win_panel] style failed: {e}")


def set_click_through(widget, on: bool) -> None:
    """Clicks go to whatever is beneath (the ink layer between drawings)."""
    if widget is None:
        return
    widget._win_click_through = bool(on)
    if IS_WIN and not _offscreen():
        _apply(widget)


def allow_typing(widget, on: bool) -> None:
    """The user clicked a text field in the panel: let it take the keyboard (activate on THIS click only).
    off → back to never stealing focus."""
    if widget is None:
        return
    was = bool(getattr(widget, "_win_typing", False))
    widget._win_typing = bool(on)
    if not IS_WIN or _offscreen():
        return
    u = _fg_api()
    if on and not was:
        try:                                        # remember the app you were in, to hand it back after
            fg = u.GetForegroundWindow() or 0
            widget._win_prev_fg = fg if fg and fg != hwnd_of(widget) else 0
        except Exception:
            widget._win_prev_fg = 0
    _apply(widget)
    if on:
        try:
            widget.activateWindow()
            u.SetForegroundWindow(hwnd_of(widget))
        except Exception:
            pass
    elif was:
        # Done typing: give the keyboard back to the app you were in. Otherwise Windows activates "the next
        # window" of Filect once the panel turns click-through again — Filect's main window jumped in front
        # of your app after you sent a task from the card and closed it (2026-11-08).
        prev, widget._win_prev_fg = getattr(widget, "_win_prev_fg", 0), 0
        try:
            if prev and u.IsWindow(prev) and (u.GetForegroundWindow() or 0) == hwnd_of(widget):
                u.SetForegroundWindow(prev)
        except Exception:
            pass


def excluded_from_capture(widget) -> bool:
    """Is this panel left out of screen captures (WDA_EXCLUDEFROMCAPTURE took)?"""
    return bool(getattr(widget, "_win_affinity_done", False))
