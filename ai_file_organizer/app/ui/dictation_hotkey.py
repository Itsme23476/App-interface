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
Shift held down FIRST (at least SHIFT_FIRST_SEC before the trigger goes down), then the trigger -> mute_toggle:
mutes or unmutes the hands-free Assistant (the Mac's Shift, then Fn). Decided the moment the trigger goes down;
holding them toggles once and records nothing. Shift and the trigger pressed together, or the trigger first and
then Shift, stay a search. With Alt held too it isn't mute (a normal press).

Default trigger is ``ctrl+win`` (hold Ctrl **and** the Windows key) — the de-facto
standard push-to-talk hotkey for Windows dictation apps (Wispr Flow, Typeless,
etc.), chosen because it doesn't collide with common Windows system shortcuts.
The trigger is configurable via settings (a single named modifier, the ctrl+win
chord, or a single alphanumeric key for testing).
"""
import logging
import time
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
SHIFT_FIRST_SEC = 0.1     # Shift down this long before the shortcut = mute / unmute the Assistant (the Mac's ⇧ then fn)


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


_VK_MASK = 0xE8   # an unassigned key: "something else went down with Alt / Win" (AutoHotkey's menu mask key)


def _mask_menu_keys() -> None:
    """Press and let go of an unassigned key while Win / Alt are held for the shortcut. Without it, letting go of
    Alt counts as a lone Alt tap in the app in front: it opens that app's menu (Word: the ribbon's key tips), and the
    menu swallows the next keys you type — live test 2026-11-12, with Filect closed too: ' eggs, milk' typed after
    Ctrl+Win+Alt arrived as 'ggs, milk'; with this key sent while the chord was held, nothing was lost (3 of 3).
    (A lone Win release opening Start is stopped the same way.)"""
    try:
        import ctypes
        u = ctypes.windll.user32
        u.keybd_event(_VK_MASK, 0, 0, 0)
        u.keybd_event(_VK_MASK, 0, 0x0002, 0)        # KEYEVENTF_KEYUP
    except Exception:
        pass


class DictationHotkey(QObject):
    """Polls the trigger key and emits start/stop for hold-to-talk dictation."""

    dictate_start = Signal(str)   # mode: 'dictate' | 'search' | 'organize'
    dictate_stop = Signal()
    mute_toggle = Signal()        # Shift first, then the shortcut: mute / unmute the hands-free Assistant

    def __init__(self, trigger: str = DEFAULT_TRIGGER, hold_ms: int = 180,
                 poll_ms: int = 25, parent=None):
        super().__init__(parent)
        self._poll_ms = max(10, int(poll_ms))
        self._hold_frames = max(1, int(hold_ms) // self._poll_ms)
        self._down_frames = 0
        self._active = False
        self._mode = "dictate"
        self._masked_win = self._masked_alt = False   # (the mask key was sent for this press's Win / Alt)
        self._shift_since = None      # when Shift (either one) went down; None while both are up
        self._mute_press = False      # this press was Shift-first: it toggled mute, it records nothing
        self._clock = time.monotonic
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
            self._shift_since, self._mute_press = None, False
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

    def _watch_shift(self, now: float) -> None:
        """Remember when Shift went down (the first of the two Shift keys counts; both up clears it). Read only —
        Shift is never swallowed."""
        if (not self._chord) and self._vk in _SHIFT_VKS:
            self._shift_since = None                      # (Shift IS the trigger: there's no "Shift first")
            return
        if _key_down(_VK_SHIFT):
            if self._shift_since is None:
                self._shift_since = now
        else:
            self._shift_since = None

    def _shift_first(self, now: float) -> bool:
        """The trigger just went down: was Shift already held for SHIFT_FIRST_SEC (and no Alt — that's another
        shortcut)?"""
        return (self._shift_since is not None and now - self._shift_since >= SHIFT_FIRST_SEC - 1e-6
                and not _key_down(_VK_ALT))

    def _mask_if_needed(self):
        """While the shortcut is active: once Win, and once Alt, is part of it, send the mask key (see
        _mask_menu_keys) — so letting go opens neither Start nor the front app's menu. A quick tap never gets here."""
        win, alt = _win_down(), _key_down(_VK_ALT)
        if (win and not self._masked_win) or (alt and not self._masked_alt):
            self._masked_win = self._masked_win or win
            self._masked_alt = self._masked_alt or alt
            _mask_menu_keys()

    def _poll(self):
        now = self._clock()
        down = self._trigger_down()
        if not down or self._down_frames == 0:
            self._watch_shift(now)                        # (while the trigger is held, keep when Shift went down)
        if down:
            self._down_frames += 1
            if self._down_frames == 1 and self._shift_first(now):
                self._mute_press = True
                self._masked_win = self._masked_alt = False
                self._mask_if_needed()                    # (letting go of Win mustn't open Start)
                logger.info("[DICTATION HOTKEY] Shift first, then the shortcut: mute / unmute")
                self.mute_toggle.emit()
                return
            if self._mute_press:
                return                                    # (held: it toggled once, it records nothing)
            if self._down_frames == self._hold_frames and not self._active:
                self._active = True
                self._masked_win = self._masked_alt = False
                self._mode = self._current_mode()
                logger.info(f"[DICTATION HOTKEY] start mode={self._mode}")
                self._mask_if_needed()
                self.dictate_start.emit(self._mode)
            elif self._active:
                self._mask_if_needed()                  # (Alt pressed a moment after the start)
        else:
            self._mute_press = False
            if self._active:
                self._active = False
                logger.info("[DICTATION HOTKEY] stop")
                self.dictate_stop.emit()
            self._down_frames = 0
