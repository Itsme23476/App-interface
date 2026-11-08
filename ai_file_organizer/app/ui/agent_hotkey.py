"""The "talk to the agent" key gesture (Windows) — the twin of the Mac's fn + ⌃ tap, built on what WORKS on
every Windows box including the ARM64/Parallels dev machine: RegisterHotKey (claims a key so it never types
into the app in front) + GetAsyncKeyState (sees when it's let go). Verified there with synthetic input.

  * press the chord (setting `assistant_voice_shortcut`, TEST default "shift+z") → `pressed`
  * while it's held, Filect claims — only then — the chord's letter (so a key-repeat after Shift is let go
    doesn't type "zzz"), Space (+Shift+Space) and Esc (+Shift+Esc), all swallowed:
        Space down → space(True) · Space up → space(False)   (draw on the screen / snapshot it)
        Esc → escape()                                        (cancel)
  * let go of the chord's letter → `released`; the transient claims are dropped at once. The voice controller
    then keeps Esc claimed a few seconds (hold_escape / drop_escape) so Esc still cancels while your words are
    written down and routed — you often let go of the letter to reach Esc.
  * right-click while held → right() (drawing on / snapshot). A low-level MOUSE hook (WH_MOUSE_LL) catches
    it — installed only while the chord is held, on its own thread — and swallows the right button (no
    context menu); every other mouse event passes through untouched. (Verified working in Parallels too.)

All signals are emitted on the UI thread (WM_HOTKEY arrives through Qt's native event filter, the release
comes from a QTimer poll) — so handlers may touch widgets.
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal, QTimer, QAbstractNativeEventFilter, QCoreApplication

logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
VK_SPACE, VK_ESCAPE = 0x20, 0x1B
DEFAULT_SHORTCUT = "shift+z"          # TEST keybind (Parallels) — swap for the real one before release
MAX_HOLD_SEC = 600                    # same 10-minute cap as the recorder

_ID_FIRST, _ID_LAST, _BLOCK = 0xB100, 0xBEFF, 8
_next_block = [_ID_FIRST]


def _id_block() -> list[int]:
    """8 hotkey ids for ONE AgentHotkey (the chord, its 5 transient claims, 2 post-release Esc claims), reused
    on every press. (2026-11-08: each press used to take 5 NEW ids from a 255-id pool — after ~50 presses
    next() raised inside the native filter and the chord stayed "held" forever = Shift+Z dead until restart.)
    Blocks wrap around the app range, so tests that build many hotkeys never run out either."""
    start = _next_block[0]
    _next_block[0] = _ID_FIRST if start + 2 * _BLOCK > _ID_LAST else start + _BLOCK
    return list(range(start, start + _BLOCK))


POST_ESC_SEC = 6.0    # after you let go: Esc still cancels while your words are written down + routed


def parse(seq: str):
    """'shift+z' → (MOD_SHIFT, VK 'Z'). Letters, digits and F1–F24; modifiers ctrl/alt/shift/win."""
    mods, vk = 0, 0
    for p in (seq or "").lower().replace(" ", "").split("+"):
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p == "win":
            mods |= MOD_WIN
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
        elif p.startswith("f") and p[1:].isdigit() and 1 <= int(p[1:]) <= 24:
            vk = 0x6F + int(p[1:])
    return mods, vk


def label(seq: str) -> str:
    """'shift+z' → 'Shift+Z' (for hints)."""
    names = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    return "+".join(names.get(p, p.upper()) for p in (seq or "").lower().replace(" ", "").split("+") if p)


def _down(vk: int) -> bool:
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type, message):
        try:
            if event_type != "windows_generic_MSG":
                return False, 0
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam in self._owner._known:
                self._owner._on_hotkey(int(msg.wParam))
                return True, 0
        except Exception:
            pass
        return False, 0


class _RightClickHook:
    """WH_MOUSE_LL on its own thread (a message loop of its own, so a busy UI thread can never stall the
    mouse): right-button down → on_right() + swallowed; right-button up → swallowed; all else passes."""
    WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205

    def __init__(self, on_right):
        self._on_right = on_right
        self._thread = None
        self._tid = 0
        self._proc = None
        self.installed = False

    def start(self):
        import threading
        if self._thread is not None:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(ready,), daemon=True, name="filect-agent-mouse")
        self._thread.start()
        ready.wait(1.0)

    def _loop(self, ready):
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        u.SetWindowsHookExW.restype = wintypes.HHOOK
        u.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u.CallNextHookEx.restype = LRESULT
        u.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HMODULE        # (a 32-bit default here = a failed hook)

        def proc(n, w, l):
            if n == 0 and w in (self.WM_RBUTTONDOWN, self.WM_RBUTTONUP):
                if w == self.WM_RBUTTONDOWN:
                    try:
                        self._on_right()
                    except Exception:
                        pass
                return 1                                      # swallowed: no context menu
            return u.CallNextHookEx(None, n, w, l)

        self._proc = HOOKPROC(proc)
        self._tid = k.GetCurrentThreadId()
        hook = u.SetWindowsHookExW(14, self._proc, k.GetModuleHandleW(None), 0)   # WH_MOUSE_LL
        self.installed = bool(hook)
        ready.set()
        if not hook:
            logger.warning("[agent-key] right-click hook unavailable")
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        u.UnhookWindowsHookEx(hook)

    def stop(self):
        t, self._thread = self._thread, None
        if t is not None and self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)      # WM_QUIT
            t.join(1.0)
        self._tid, self.installed = 0, False


class AgentHotkey(QObject):
    pressed = Signal()
    released = Signal()
    space = Signal(bool)
    escape = Signal()
    right = Signal()                     # right-click while the chord is held (emitted from the hook thread,
                                         # queued to the receiver's thread automatically)

    def __init__(self, hwnd: int = 0, shortcut: str = DEFAULT_SHORTCUT, poll_ms: int = 20, parent=None):
        super().__init__(parent)
        self._hwnd = int(hwnd or 0)
        self._shortcut = shortcut or DEFAULT_SHORTCUT
        self._mods, self._vk = parse(self._shortcut)
        self._block = _id_block()
        self._main_id = None
        self._transient = {}            # id -> role ("letter" | "space" | "esc")
        self._post_esc = set()          # Esc claims kept a few seconds after the chord is let go
        self._post_timer = QTimer(self)
        self._post_timer.setSingleShot(True)
        self._post_timer.timeout.connect(self.drop_escape)
        self._known = set()
        self._active = False
        self._space_down = False
        self._since = 0.0
        self._filter = _Filter(self)
        self._timer = QTimer(self)
        self._timer.setInterval(max(10, int(poll_ms)))
        self._timer.timeout.connect(self._poll)
        self._mouse = _RightClickHook(self.right.emit)
        self.ok = False

    @property
    def shortcut(self) -> str:
        return self._shortcut

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> bool:
        """Claim the chord. False if it's taken / invalid (another app owns it)."""
        if self.ok:
            return True
        if not self._vk:
            logger.warning(f"[agent-key] can't parse shortcut {self._shortcut!r}")
            return False
        app = QCoreApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._filter)
        self._main_id = self._block[0]
        self._known.add(self._main_id)
        self.ok = bool(ctypes.windll.user32.RegisterHotKey(self._hwnd or None, self._main_id, self._mods, self._vk))
        logger.info(f"[agent-key] {label(self._shortcut)} {'registered' if self.ok else 'NOT available (taken?)'}")
        return self.ok

    def stop(self) -> None:
        self._end(emit=False)
        self.drop_escape()
        if self._main_id is not None:
            ctypes.windll.user32.UnregisterHotKey(self._hwnd or None, self._main_id)
            self._known.discard(self._main_id)
            self._main_id = None
        try:
            QCoreApplication.instance().removeNativeEventFilter(self._filter)
        except Exception:
            pass
        self.ok = False

    # -- events --------------------------------------------------------------------------------------
    def _on_hotkey(self, hid: int) -> None:
        if hid == self._main_id:
            if self._active:
                return                          # key-repeat of the held chord
            self.drop_escape()                  # (a new press: the transient claims take over Esc)
            self._active, self._space_down, self._since = True, False, time.monotonic()
            try:
                self._claim()
                self._mouse.start()
            finally:
                self._timer.start()             # always watch for the release — never stuck "held"
            logger.info(f"[agent-key] {label(self._shortcut)} down")
            self.pressed.emit()
            return
        if hid in self._post_esc:
            logger.info("[agent-key] Esc (just after letting go)")
            self.escape.emit()
            return
        role = self._transient.get(hid)
        if not self._active or role is None:
            return
        if role == "space" and not self._space_down:
            self._space_down = True
            self.space.emit(True)
        elif role == "esc":
            self.escape.emit()

    def _claim(self) -> None:
        """While held: the letter (no Shift), Space and Esc — with and without Shift — swallowed."""
        u = ctypes.windll.user32
        for hid, (role, mods, vk) in zip(self._block[1:6], (
                ("letter", MOD_NOREPEAT, self._vk),
                ("space", MOD_NOREPEAT, VK_SPACE), ("space", MOD_SHIFT | MOD_NOREPEAT, VK_SPACE),
                ("esc", MOD_NOREPEAT, VK_ESCAPE), ("esc", MOD_SHIFT | MOD_NOREPEAT, VK_ESCAPE))):
            if role == "letter" and mods & ~MOD_NOREPEAT == self._mods:
                continue                        # (a plain-letter chord is already claimed)
            if u.RegisterHotKey(self._hwnd or None, hid, mods, vk):
                self._transient[hid] = role
                self._known.add(hid)
            else:
                logger.warning(f"[agent-key] couldn't claim {role} (mods={mods & ~MOD_NOREPEAT:#x}) — "
                               "another app has it")

    def hold_escape(self, seconds: float = POST_ESC_SEC) -> None:
        """After the chord is let go: keep Esc (and Shift+Esc) claimed a little longer, so Esc still cancels
        while your words are being written down and routed — letting go of the letter to reach Esc is natural.
        Swallowed while held, like during the chord. drop_escape() (the request was sent / cancelled) ends it."""
        if self._post_esc or not self.ok:
            return
        u = ctypes.windll.user32
        for hid, mods in zip(self._block[6:8], (MOD_NOREPEAT, MOD_SHIFT | MOD_NOREPEAT)):
            if u.RegisterHotKey(self._hwnd or None, hid, mods, VK_ESCAPE):
                self._post_esc.add(hid)
                self._known.add(hid)
        self._post_timer.start(int(max(0.1, seconds) * 1000))

    def drop_escape(self) -> None:
        self._post_timer.stop()
        if not self._post_esc:
            return
        u = ctypes.windll.user32
        for hid in list(self._post_esc):
            u.UnregisterHotKey(self._hwnd or None, hid)
            self._known.discard(hid)
        self._post_esc.clear()

    @property
    def holding_escape(self) -> bool:
        return bool(self._post_esc)

    def _release_claims(self) -> None:
        u = ctypes.windll.user32
        for hid in list(self._transient):
            u.UnregisterHotKey(self._hwnd or None, hid)
            self._known.discard(hid)
        self._transient.clear()

    def _poll(self) -> None:
        if not self._active:
            self._timer.stop()
            return
        if self._space_down and not _down(VK_SPACE):
            self._space_down = False
            self.space.emit(False)
        if not _down(self._vk) or time.monotonic() - self._since > MAX_HOLD_SEC:
            self._end(emit=True)

    def _end(self, emit: bool) -> None:
        if not self._active:
            return
        self._timer.stop()
        self._release_claims()
        self._mouse.stop()
        self._active = False
        was_space, self._space_down = self._space_down, False
        logger.info(f"[agent-key] {label(self._shortcut)} up")
        if emit:
            if was_space:
                self.space.emit(False)
            self.released.emit()
