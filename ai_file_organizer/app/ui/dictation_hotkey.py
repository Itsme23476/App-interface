"""
Dictation hotkey — hold-to-talk gesture engine (Windows).

The Mac app captures the Fn key via a CGEventTap. Windows can't intercept Fn,
and — critically on this ARM64 machine — the low-level keyboard HOOK libraries
(`keyboard`, `pynput`, both SetWindowsHookEx-based) don't receive events here.
What DOES work on ARM64 is plain user32 calls (RegisterHotKey works; so does
GetAsyncKeyState). So we poll the trigger key's state via GetAsyncKeyState on a
QTimer on the GUI thread — no hook, no admin, ARM64-safe.

Gesture:
  - Hold the trigger longer than ``hold_ms`` -> dictate_start(mode).
  - Release -> dictate_stop().
  - A quick tap (< hold_ms) passes through normally and fires nothing.
Modes (read at the moment the hold threshold is crossed):
  - trigger alone        -> 'dictate'
  - trigger + Shift      -> 'search'
  - trigger + Alt        -> 'organize'

Default trigger is ``ctrl+win`` (hold Ctrl **and** the Windows key) — the de-facto
standard push-to-talk hotkey for Windows dictation apps (Wispr Flow, Typeless,
etc.), chosen because it doesn't collide with common Windows system shortcuts.
The trigger is configurable via settings (a single named modifier, the ctrl+win
chord, or a single alphanumeric key for testing).
"""
import logging
from PySide6.QtCore import QObject, Signal, QTimer

logger = logging.getLogger(__name__)

# Virtual-key codes (user32). https://learn.microsoft.com/windows/win32/inputdev/virtual-key-codes
_VK = {
    "right ctrl": 0xA3,   # VK_RCONTROL
    "left ctrl":  0xA2,   # VK_LCONTROL
    "shift":      0x10,   # VK_SHIFT (either)
    "left shift": 0xA0,   # VK_LSHIFT
    "right shift": 0xA1,  # VK_RSHIFT
    "right alt":  0xA5,   # VK_RMENU
    "caps lock":  0x14,   # VK_CAPITAL
}
_VK_SHIFT = 0x10          # VK_SHIFT (either)
_VK_ALT = 0x12            # VK_MENU (either)
_VK_CTRL = 0x11           # VK_CONTROL (either)
_VK_LWIN = 0x5B           # VK_LWIN
_VK_RWIN = 0x5C           # VK_RWIN
_SHIFT_VKS = {0x10, 0xA0, 0xA1}
_ALT_VKS = {0x12, 0xA4, 0xA5}

DEFAULT_TRIGGER = "ctrl+win"


def _key_down(vk: int) -> bool:
    """True if the virtual key is currently held. GetAsyncKeyState high bit."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def _ctrl_down() -> bool:
    return _key_down(_VK_CTRL) or _key_down(0xA2) or _key_down(0xA3)


def _win_down() -> bool:
    return _key_down(_VK_LWIN) or _key_down(_VK_RWIN)


class DictationHotkey(QObject):
    """Polls the trigger key and emits start/stop for hold-to-talk dictation."""

    dictate_start = Signal(str)   # mode: 'dictate' | 'search' | 'organize'
    dictate_stop = Signal()

    def __init__(self, trigger: str = DEFAULT_TRIGGER, hold_ms: int = 180,
                 poll_ms: int = 25, parent=None):
        super().__init__(parent)
        self._poll_ms = max(10, int(poll_ms))
        self._hold_frames = max(1, int(hold_ms) // self._poll_ms)
        self._down_frames = 0
        self._active = False
        self._mode = "dictate"
        self.set_trigger(trigger)
        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_ms)
        self._timer.timeout.connect(self._poll)

    def set_trigger(self, trigger: str):
        """Set the trigger. Accepts the ``ctrl+win`` chord (default), a named
        modifier (see _VK), OR a single alphanumeric character (e.g. 'a' for
        testing in environments where modifier keys are intercepted by a host)."""
        trigger = (trigger or DEFAULT_TRIGGER).lower().strip()
        self._chord = False
        if trigger in ("ctrl+win", "win+ctrl", "ctrl win"):
            self._trigger_name = "ctrl+win"
            self._chord = True
            self._vk = None
        elif trigger in _VK:
            self._trigger_name = trigger
            self._vk = _VK[trigger]
        elif len(trigger) == 1 and trigger.isalnum():
            self._trigger_name = trigger
            self._vk = ord(trigger.upper())   # VK for 'A'..'Z'/'0'..'9'
        else:
            self._trigger_name = "ctrl+win"
            self._chord = True
            self._vk = None

    def _trigger_down(self) -> bool:
        """True while the trigger is held — the Ctrl+Win chord, or a single key."""
        if self._chord:
            return _ctrl_down() and _win_down()
        return _key_down(self._vk)

    @property
    def trigger_name(self) -> str:
        return self._trigger_name

    @property
    def is_active(self) -> bool:
        return self._active

    def start(self):
        if not self._timer.isActive():
            self._down_frames = 0
            self._active = False
            self._timer.start()
            logger.info(f"[DICTATION HOTKEY] polling '{self._trigger_name}' "
                        f"(hold {self._hold_frames * self._poll_ms}ms)")

    def stop(self):
        self._timer.stop()
        if self._active:
            self._active = False
            self.dictate_stop.emit()
        self._down_frames = 0

    def _current_mode(self) -> str:
        # Don't treat the trigger key itself as a mode modifier (e.g. if the
        # trigger IS Shift, holding it must mean 'dictate', not 'search'). For
        # the ctrl+win chord, Ctrl/Win are the trigger and Shift/Alt are free.
        shift_is_trigger = (not self._chord) and self._vk in _SHIFT_VKS
        alt_is_trigger = (not self._chord) and self._vk in _ALT_VKS
        if not shift_is_trigger and _key_down(_VK_SHIFT):
            return "search"
        if not alt_is_trigger and _key_down(_VK_ALT):
            return "organize"
        return "dictate"

    def _poll(self):
        down = self._trigger_down()
        if down:
            self._down_frames += 1
            if self._down_frames == self._hold_frames and not self._active:
                self._active = True
                self._mode = self._current_mode()
                logger.info(f"[DICTATION HOTKEY] start mode={self._mode}")
                self.dictate_start.emit(self._mode)
        else:
            if self._active:
                self._active = False
                logger.info("[DICTATION HOTKEY] stop")
                self.dictate_stop.emit()
            self._down_frames = 0
