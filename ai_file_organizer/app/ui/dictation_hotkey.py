"""
Dictation hotkey — hold-to-talk gesture engine (Windows).

The Mac app captures the Fn key via a CGEventTap. Windows can't intercept Fn,
and — critically on this ARM64 machine — the low-level keyboard HOOK libraries
(`keyboard`, `pynput`, both SetWindowsHookEx-based) don't receive events here.
What DOES work on ARM64 is plain user32 calls (RegisterHotKey works; so does
GetAsyncKeyState). So we poll the trigger key's state via GetAsyncKeyState on a
QTimer on the GUI thread — no hook, no admin, ARM64-safe.

Gesture:
  - Hold the trigger key longer than ``hold_ms`` -> dictate_start(mode).
  - Release -> dictate_stop().
  - A quick tap (< hold_ms) passes through normally and fires nothing.
Modes (read at the moment the hold threshold is crossed):
  - trigger alone        -> 'dictate'
  - trigger + Shift      -> 'search'
  - trigger + Alt        -> 'organize'

The trigger key is configurable (default Right Ctrl) via settings.
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
_SHIFT_VKS = {0x10, 0xA0, 0xA1}
_ALT_VKS = {0x12, 0xA4, 0xA5}

DEFAULT_TRIGGER = "right ctrl"


def _key_down(vk: int) -> bool:
    """True if the virtual key is currently held. GetAsyncKeyState high bit."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


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
        """Set the trigger key. Accepts a named modifier (see _VK) OR a single
        alphanumeric character (e.g. 'a' for testing in environments like a
        Parallels VM where modifier keys are intercepted by the host)."""
        trigger = (trigger or DEFAULT_TRIGGER).lower()
        if trigger in _VK:
            self._trigger_name = trigger
            self._vk = _VK[trigger]
        elif len(trigger) == 1 and trigger.isalnum():
            self._trigger_name = trigger
            self._vk = ord(trigger.upper())   # VK for 'A'..'Z'/'0'..'9'
        else:
            self._trigger_name = DEFAULT_TRIGGER
            self._vk = _VK[DEFAULT_TRIGGER]

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
        # trigger IS Shift, holding it must mean 'dictate', not 'search').
        shift_is_trigger = self._vk in _SHIFT_VKS
        alt_is_trigger = self._vk in _ALT_VKS
        if not shift_is_trigger and _key_down(_VK_SHIFT):
            return "search"
        if not alt_is_trigger and _key_down(_VK_ALT):
            return "organize"
        return "dictate"

    def _poll(self):
        down = _key_down(self._vk)
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
