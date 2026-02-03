import ctypes
from ctypes import wintypes
from typing import Optional, Tuple, Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication
from PySide6.QtWidgets import QWidget


WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004


class _WinHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, hotkey_id: int, on_activated: Callable[[], None]):
        super().__init__()
        self._id = hotkey_id
        self._on_activated = on_activated

    def nativeEventFilter(self, eventType, message):
        try:
            if eventType != 'windows_generic_MSG':
                return False, 0
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self._id:
                try:
                    self._on_activated()
                except Exception:
                    pass
                return True, 0
            return False, 0
        except Exception:
            return False, 0


def _parse_hotkey(sequence: str) -> Tuple[int, int]:
    seq = (sequence or '').lower().replace(' ', '')
    parts = seq.split('+')
    mods = 0
    vk = 0
    # common virtual keys
    vk_map = {
        'space': 0x20,
        'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75,
        'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
        'f13': 0x7C, 'f14': 0x7D, 'f15': 0x7E, 'f16': 0x7F, 'f17': 0x80, 'f18': 0x81,
        'f19': 0x82, 'f20': 0x83, 'f21': 0x84, 'f22': 0x85, 'f23': 0x86, 'f24': 0x87,
    }
    for p in parts:
        if p == 'ctrl' or p == 'control':
            mods |= MOD_CONTROL
        elif p == 'alt':
            mods |= MOD_ALT
        elif p == 'shift':
            mods |= MOD_SHIFT
        elif p:
            if p in vk_map:
                vk = vk_map[p]
            elif len(p) == 1:
                ch = p
                vk = ord(ch.upper())
            elif p.startswith('f') and p[1:].isdigit():
                n = int(p[1:])
                if 1 <= n <= 24:
                    vk = 0x6F + n  # 0x70 is F1
    return mods, vk


def register_global_hotkey(parent: Optional[QWidget], sequence: str, on_activated: Callable[[], None]) -> Optional[Tuple[int, _WinHotkeyFilter, int]]:
    try:
        user32 = ctypes.windll.user32
        mods, vk = _parse_hotkey(sequence)
        if not vk:
            return None
        # Pick an arbitrary id (avoid collisions). In a real app we'd track and reuse.
        hotkey_id = 0xA11D  # arbitrary unique id
        hwnd = None
        if parent is not None:
            try:
                hwnd = int(parent.winId())
            except Exception:
                hwnd = None
        if not user32.RegisterHotKey(hwnd, hotkey_id, mods, vk):
            return None
        filt = _WinHotkeyFilter(hotkey_id, on_activated)
        app = QCoreApplication.instance()
        if app:
            app.installNativeEventFilter(filt)
        return hotkey_id, filt, (hwnd or 0)
    except Exception:
        return None


def unregister_global_hotkey(hotkey_id: int, filt: _WinHotkeyFilter) -> None:
    try:
        app = QCoreApplication.instance()
        if app and filt:
            app.removeNativeEventFilter(filt)
        ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)
    except Exception:
        pass


# Foreground window helpers
def get_foreground_hwnd() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def set_foreground_hwnd(hwnd: int) -> bool:
    try:
        user32 = ctypes.windll.user32
        SW_SHOW = 5
        if hwnd:
            user32.ShowWindow(hwnd, SW_SHOW)
            ok = user32.SetForegroundWindow(hwnd)
            return bool(ok)
        return False
    except Exception:
        return False


def set_foreground_hwnd_robust(hwnd: int) -> bool:
    """Stronger attempt: attach input to target thread, allow foreground, then set."""
    try:
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # Allow any process to set foreground (best effort)
        try:
            ASFW_ANY = -1
            user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:
            pass
        # Get thread ids
        pid = wintypes.DWORD()
        target_tid = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        current_tid = kernel32.GetCurrentThreadId()
        # Attach thread input
        attached = user32.AttachThreadInput(current_tid, target_tid, True)
        # Bring to front
        SW_SHOW = 5
        user32.ShowWindow(hwnd, SW_SHOW)
        ok = user32.SetForegroundWindow(hwnd)
        # Detach
        if attached:
            user32.AttachThreadInput(current_tid, target_tid, False)
        return bool(ok)
    except Exception:
        return False


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | tuple[()]:
    """Return (left, top, right, bottom) for a window handle, or empty tuple on failure."""
    try:
        rect = wintypes.RECT()
        ok = ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
        if ok:
            return rect.left, rect.top, rect.right, rect.bottom
        return ()
    except Exception:
        return ()


# Phase 1: State Capture Functions
def get_cursor_pos() -> tuple[int, int] | tuple[()]:
    """Get current mouse cursor position as (x, y), or empty tuple on failure."""
    try:
        point = wintypes.POINT()
        ok = ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        if ok:
            return point.x, point.y
        return ()
    except Exception:
        return ()


def set_cursor_pos(x: int, y: int) -> bool:
    """Set mouse cursor to specific position."""
    try:
        return bool(ctypes.windll.user32.SetCursorPos(x, y))
    except Exception:
        return False


def is_file_dialog(hwnd: int) -> bool:
    """Check if a window handle appears to be a file dialog."""
    try:
        user32 = ctypes.windll.user32
        
        # Get window class name
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), class_name, 256)
        class_str = class_name.value.lower()
        
        # Get window title
        title_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(wintypes.HWND(hwnd), title_buf, 512)
        title_str = title_buf.value.lower()
        
        # Common file dialog indicators
        dialog_classes = ['#32770', 'opendialog', 'savedialog', 'explorerframe']
        dialog_keywords = ['open', 'save', 'browse', 'select', 'choose', 'file']
        
        # Check class name
        if any(cls in class_str for cls in dialog_classes):
            return True
            
        # Check title for file dialog keywords
        if any(keyword in title_str for keyword in dialog_keywords):
            return True
            
        return False
    except Exception:
        return False


def get_window_title(hwnd: int) -> str:
    """Get the title of a window."""
    try:
        title_buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(wintypes.HWND(hwnd), title_buf, 512)
        return title_buf.value
    except Exception:
        return ""


def get_window_class(hwnd: int) -> str:
    """Get the class name of a window."""
    try:
        class_name = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(wintypes.HWND(hwnd), class_name, 256)
        return class_name.value
    except Exception:
        return ""


def click_at_position(x: int, y: int) -> bool:
    """Perform a mouse click at the specified screen coordinates."""
    try:
        user32 = ctypes.windll.user32
        
        # Mouse event constants
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        
        # Save current cursor position
        old_pos = get_cursor_pos()
        
        # Move to target position
        if not set_cursor_pos(x, y):
            return False
            
        # Perform click (down then up)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        
        # Restore cursor position if we had one
        if old_pos:
            set_cursor_pos(old_pos[0], old_pos[1])
            
        return True
    except Exception:
        return False


# Phase 2: Enhanced Focus Restoration Functions
def window_still_exists(hwnd: int) -> bool:
    """Check if a window handle is still valid."""
    try:
        return bool(ctypes.windll.user32.IsWindow(wintypes.HWND(hwnd)))
    except Exception:
        return False


def is_window_visible(hwnd: int) -> bool:
    """Check if a window is visible."""
    try:
        return bool(ctypes.windll.user32.IsWindowVisible(wintypes.HWND(hwnd)))
    except Exception:
        return False


def restore_window_focus_method1(hwnd: int) -> bool:
    """Method 1: Simple SetForegroundWindow approach."""
    try:
        if not window_still_exists(hwnd):
            return False
        return set_foreground_hwnd(hwnd)
    except Exception:
        return False


def restore_window_focus_method2(hwnd: int) -> bool:
    """Method 2: AttachThreadInput + SetForegroundWindow approach."""
    try:
        if not window_still_exists(hwnd):
            return False
        return set_foreground_hwnd_robust(hwnd)
    except Exception:
        return False


def restore_window_focus_method3(hwnd: int) -> bool:
    """Method 3: AllowSetForegroundWindow + SetForegroundWindow approach."""
    try:
        if not window_still_exists(hwnd):
            return False
            
        user32 = ctypes.windll.user32
        
        # Allow any process to set foreground
        ASFW_ANY = -1
        user32.AllowSetForegroundWindow(ASFW_ANY)
        
        # Show and bring to front
        SW_SHOW = 5
        SW_RESTORE = 9
        user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOW)
        
        # Set as foreground
        return bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))
    except Exception:
        return False


def restore_focus_by_mouse_click(cursor_pos: tuple[int, int], window_rect: tuple[int, int, int, int]) -> bool:
    """Method 4: Click at saved cursor position to restore focus."""
    try:
        if not cursor_pos or len(cursor_pos) != 2:
            return False
        if not window_rect or len(window_rect) != 4:
            return False
            
        x, y = cursor_pos
        left, top, right, bottom = window_rect
        
        # Verify the cursor position is within the window bounds
        if not (left <= x <= right and top <= y <= bottom):
            # If cursor was outside window, click center of window instead
            x = (left + right) // 2
            y = (top + bottom) // 2
            
        return click_at_position(x, y)
    except Exception:
        return False


def restore_dialog_focus_hybrid(hwnd: int, cursor_pos: tuple[int, int], window_rect: tuple[int, int, int, int], 
                               delay_ms: int = 500) -> tuple[bool, str]:
    """
    Phase 2: Hybrid approach to restore focus to a file dialog.
    
    Returns: (success: bool, method_used: str)
    """
    import time
    
    try:
        if not hwnd:
            return False, "no_hwnd"
            
        # Check if window still exists
        if not window_still_exists(hwnd):
            return False, "window_gone"
            
        # Add delay for Windows to settle
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        
        # Method 1: Simple SetForegroundWindow
        if restore_window_focus_method1(hwnd):
            time.sleep(0.1)  # Brief pause to verify
            current_fg = get_foreground_hwnd()
            if current_fg == hwnd:
                return True, "method1_simple"
        
        # Method 2: AttachThreadInput + SetForegroundWindow  
        if restore_window_focus_method2(hwnd):
            time.sleep(0.1)
            current_fg = get_foreground_hwnd()
            if current_fg == hwnd:
                return True, "method2_robust"
        
        # Method 3: AllowSetForegroundWindow + SetForegroundWindow
        if restore_window_focus_method3(hwnd):
            time.sleep(0.1)
            current_fg = get_foreground_hwnd()
            if current_fg == hwnd:
                return True, "method3_allow"
        
        # Method 4: Mouse click fallback
        if restore_focus_by_mouse_click(cursor_pos, window_rect):
            time.sleep(0.2)  # Longer pause for mouse click to register
            current_fg = get_foreground_hwnd()
            if current_fg == hwnd:
                return True, "method4_click"
                
        # All methods failed
        return False, "all_failed"
        
    except Exception as e:
        return False, f"exception_{str(e)[:20]}"


# Phase 4: Extensive Logging & Debugging Functions
def enumerate_windows_detailed() -> list[dict]:
    """Get detailed information about all visible windows."""
    try:
        import ctypes
        from ctypes import wintypes
        
        windows = []
        
        def enum_windows_proc(hwnd, lParam):
            try:
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    return True
                
                # Get window info
                title = get_window_title(hwnd)
                class_name = get_window_class(hwnd)
                rect = get_window_rect(hwnd)
                is_dialog = is_file_dialog(hwnd)
                
                # Get process info
                pid = wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                
                windows.append({
                    'hwnd': hwnd,
                    'title': title,
                    'class': class_name,
                    'rect': rect,
                    'is_dialog': is_dialog,
                    'pid': pid.value
                })
                
            except Exception:
                pass
            return True
        
        # Define the callback type
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        callback = EnumWindowsProc(enum_windows_proc)
        
        ctypes.windll.user32.EnumWindows(callback, 0)
        return windows
        
    except Exception:
        return []


def log_system_state(logger, prefix="[QS]"):
    """Log comprehensive system state for debugging."""
    try:
        logger.info(f"{prefix} === SYSTEM STATE DUMP ===")
        
        # Current foreground window
        fg_hwnd = get_foreground_hwnd()
        if fg_hwnd:
            fg_title = get_window_title(fg_hwnd)
            fg_class = get_window_class(fg_hwnd)
            fg_rect = get_window_rect(fg_hwnd)
            fg_is_dialog = is_file_dialog(fg_hwnd)
            logger.info(f"{prefix} Foreground: hwnd={fg_hwnd}, title='{fg_title}', class='{fg_class}', rect={fg_rect}, is_dialog={fg_is_dialog}")
        else:
            logger.info(f"{prefix} Foreground: None")
        
        # Cursor position
        cursor_pos = get_cursor_pos()
        logger.info(f"{prefix} Cursor position: {cursor_pos}")
        
        # All visible windows
        windows = enumerate_windows_detailed()
        logger.info(f"{prefix} Total visible windows: {len(windows)}")
        
        # Log potential file dialogs
        dialogs = [w for w in windows if w['is_dialog']]
        logger.info(f"{prefix} Potential file dialogs: {len(dialogs)}")
        for i, dialog in enumerate(dialogs[:5]):  # Limit to first 5
            logger.info(f"{prefix} Dialog {i+1}: hwnd={dialog['hwnd']}, title='{dialog['title']}', class='{dialog['class']}'")
        
        logger.info(f"{prefix} === END SYSTEM STATE ===")
        
    except Exception as e:
        logger.error(f"{prefix} Error logging system state: {e}")


def log_window_hierarchy(hwnd: int, logger, prefix="[QS]", max_depth: int = 3):
    """Log the UI hierarchy of a specific window for debugging."""
    try:
        logger.info(f"{prefix} === WINDOW HIERARCHY: {hwnd} ===")
        
        if not window_still_exists(hwnd):
            logger.warning(f"{prefix} Window {hwnd} no longer exists")
            return
        
        title = get_window_title(hwnd)
        class_name = get_window_class(hwnd)
        rect = get_window_rect(hwnd)
        
        logger.info(f"{prefix} Root: hwnd={hwnd}, title='{title}', class='{class_name}', rect={rect}")
        
        # Try to get child windows using Win32 API
        try:
            import ctypes
            from ctypes import wintypes
            
            children = []
            
            def enum_child_proc(child_hwnd, lParam):
                try:
                    child_title = get_window_title(child_hwnd)
                    child_class = get_window_class(child_hwnd)
                    child_rect = get_window_rect(child_hwnd)
                    
                    # Check if it's visible
                    is_visible = ctypes.windll.user32.IsWindowVisible(child_hwnd)
                    
                    children.append({
                        'hwnd': child_hwnd,
                        'title': child_title,
                        'class': child_class,
                        'rect': child_rect,
                        'visible': bool(is_visible)
                    })
                except Exception:
                    pass
                return True
            
            EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            callback = EnumChildProc(enum_child_proc)
            
            ctypes.windll.user32.EnumChildWindows(wintypes.HWND(hwnd), callback, 0)
            
            logger.info(f"{prefix} Child windows: {len(children)}")
            for i, child in enumerate(children[:10]):  # Limit to first 10
                visible_str = "visible" if child['visible'] else "hidden"
                logger.info(f"{prefix}   Child {i+1}: hwnd={child['hwnd']}, class='{child['class']}', title='{child['title']}', {visible_str}")
                
                # Look for Edit controls specifically
                if 'edit' in child['class'].lower():
                    logger.info(f"{prefix}     ^ EDIT CONTROL FOUND")
                elif 'combo' in child['class'].lower():
                    logger.info(f"{prefix}     ^ COMBO CONTROL FOUND")
                elif 'button' in child['class'].lower():
                    logger.info(f"{prefix}     ^ BUTTON CONTROL FOUND")
        
        except Exception as e:
            logger.error(f"{prefix} Error enumerating child windows: {e}")
        
        logger.info(f"{prefix} === END HIERARCHY ===")
        
    except Exception as e:
        logger.error(f"{prefix} Error logging window hierarchy: {e}")


def create_autofill_debug_report(hwnd: int, cursor_pos: tuple, window_rect: tuple, logger, prefix="[QS]"):
    """Create a comprehensive debug report for autofill troubleshooting."""
    try:
        logger.info(f"{prefix} === AUTOFILL DEBUG REPORT ===")
        
        # Basic window info
        if hwnd:
            title = get_window_title(hwnd)
            class_name = get_window_class(hwnd)
            exists = window_still_exists(hwnd)
            visible = is_window_visible(hwnd)
            is_dialog = is_file_dialog(hwnd)
            current_rect = get_window_rect(hwnd)
            
            logger.info(f"{prefix} Target Window:")
            logger.info(f"{prefix}   HWND: {hwnd}")
            logger.info(f"{prefix}   Title: '{title}'")
            logger.info(f"{prefix}   Class: '{class_name}'")
            logger.info(f"{prefix}   Exists: {exists}")
            logger.info(f"{prefix}   Visible: {visible}")
            logger.info(f"{prefix}   Is Dialog: {is_dialog}")
            logger.info(f"{prefix}   Saved Rect: {window_rect}")
            logger.info(f"{prefix}   Current Rect: {current_rect}")
            
            # Check if window moved/resized
            if window_rect and current_rect:
                if window_rect != current_rect:
                    logger.warning(f"{prefix}   WINDOW MOVED/RESIZED!")
        else:
            logger.warning(f"{prefix} No target window HWND")
        
        # Cursor info
        current_cursor = get_cursor_pos()
        logger.info(f"{prefix} Cursor:")
        logger.info(f"{prefix}   Saved Position: {cursor_pos}")
        logger.info(f"{prefix}   Current Position: {current_cursor}")
        
        # Current foreground window
        fg_hwnd = get_foreground_hwnd()
        if fg_hwnd != hwnd:
            fg_title = get_window_title(fg_hwnd)
            logger.warning(f"{prefix} FOCUS MISMATCH!")
            logger.warning(f"{prefix}   Expected: {hwnd}")
            logger.warning(f"{prefix}   Actual: {fg_hwnd} ('{fg_title}')")
        else:
            logger.info(f"{prefix} Focus: Correct (target window is foreground)")
        
        # Window hierarchy
        if hwnd and window_still_exists(hwnd):
            log_window_hierarchy(hwnd, logger, prefix)
        
        logger.info(f"{prefix} === END DEBUG REPORT ===")
        
    except Exception as e:
        logger.error(f"{prefix} Error creating debug report: {e}")


