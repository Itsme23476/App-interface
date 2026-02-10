"""
Theme manager for switching between dark and light modes.
"""

import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor
from PySide6.QtCore import Qt, QObject, Signal

from app.core.settings import settings


# ---------------------------------------------------------------------------
# Theme color palettes - centralised so every widget can stay in sync
# ---------------------------------------------------------------------------
_DARK_COLORS = {
    "bg":               "#0A0A12",
    "surface":          "#111119",
    "card":             "#16161F",
    "border":           "#1C1C28",
    "border_strong":    "#252535",
    "text":             "#E8E8F0",
    "text_secondary":   "#B0B0C0",
    "text_muted":       "#7A7A90",
    "text_disabled":    "#4A4A5A",
    "input_bg":         "#16161F",
    "hover":            "#1C1C28",
    "pressed":          "#252535",
    "tab_unchecked_bg": "#16161F",
    "tab_unchecked_border": "#252535",
    "tab_unchecked_text": "#B0B0C0",
    "tab_unchecked_hover": "#1C1C28",
    "danger_bg":        "rgba(211, 47, 47, 0.12)",
    "danger_hover":     "rgba(211, 47, 47, 0.10)",
    "danger_border":    "rgba(211, 47, 47, 0.30)",
    "danger_text":      "#FF6B6B",
    "purple_light_bg":  "rgba(124, 77, 255, 0.12)",
    "purple_light_hover":"rgba(124, 77, 255, 0.10)",
    "purple_pressed":   "#1C1C28",
    "scrollbar_bg":     "#252535",
    "scrollbar_handle": "#7A7A90",
    "icon_bg":          "#1A1A2E",
    "item_bg":          "#111119",
    "divider":          "#1C1C28",
    "dialog_bg":        "#111119",
    "dialog_border":    "#1C1C28",
}

_LIGHT_COLORS = {
    "bg":               "#FAFBFC",
    "surface":          "#FFFFFF",
    "card":             "#FFFFFF",
    "border":           "#E8E8E8",
    "border_strong":    "#D0D0D0",
    "text":             "#1A1A1A",
    "text_secondary":   "#666666",
    "text_muted":       "#888888",
    "text_disabled":    "#888888",
    "input_bg":         "#FFFFFF",
    "hover":            "#F5F5F5",
    "pressed":          "#E8E8E8",
    "tab_unchecked_bg": "#FFFFFF",
    "tab_unchecked_border": "#E0E0E0",
    "tab_unchecked_text": "#666666",
    "tab_unchecked_hover": "#F5F5F5",
    "danger_bg":        "#FFF0F0",
    "danger_hover":     "#FFEBEE",
    "danger_border":    "#FFCCCC",
    "danger_text":      "#CC6666",
    "purple_light_bg":  "#E8DFFF",
    "purple_light_hover":"#EDE7FF",
    "purple_pressed":   "#E8E0FF",
    "scrollbar_bg":     "#F0F0F0",
    "scrollbar_handle": "#AAAAAA",
    "icon_bg":          "#F3EEFF",
    "item_bg":          "#FAFAFA",
    "divider":          "#EEEEEE",
    "dialog_bg":        "#FFFFFF",
    "dialog_border":    "#E0E0E0",
}


def get_theme_colors(theme: str = None) -> dict:
    """Return the colour palette dict for the given (or current) theme."""
    if theme is None:
        theme = settings.theme
    return dict(_DARK_COLORS) if theme == "dark" else dict(_LIGHT_COLORS)


class ThemeManager(QObject):
    """Manages application theme switching."""
    
    # Signal emitted when theme changes
    theme_changed = Signal(str)
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        super().__init__()
        self._initialized = True
        
        # Handle PyInstaller bundled path
        if hasattr(sys, '_MEIPASS'):
            # Running as bundled exe - files are in temp extraction folder
            self._ui_dir = Path(sys._MEIPASS) / 'app' / 'ui'
        else:
            # Running from source
        self._ui_dir = Path(__file__).parent
    
    @property
    def current_theme(self) -> str:
        """Get current theme from settings."""
        return settings.theme
    
    def get_colors(self) -> dict:
        """Convenience: return colours for the *current* theme."""
        return get_theme_colors(self.current_theme)
    
    def apply_theme(self, theme: str = None):
        """Apply theme to the application.
        
        Args:
            theme: 'dark' or 'light'. If None, uses current setting.
        """
        if theme is None:
            theme = settings.theme
        
        if theme not in ('dark', 'light'):
            theme = 'dark'
        
        app = QApplication.instance()
        if not app:
            return
        
        # Load appropriate stylesheet
        if theme == 'dark':
            style_path = self._ui_dir / 'styles.qss'
            self._apply_dark_palette(app)
        else:
            style_path = self._ui_dir / 'styles_light.qss'
            self._apply_light_palette(app)
        
        # Load and apply stylesheet
        if style_path.exists():
            with open(style_path, 'r', encoding='utf-8') as f:
                base_style = f.read()
        else:
            base_style = ""
        
        # Add explicit tooltip styling to ensure it's applied globally
        if theme == 'dark':
            tooltip_style = """
                QToolTip {
                    background-color: #1E1E2E;
                    color: #E8E8F0;
                    border: 1px solid #7C4DFF;
                    border-radius: 6px;
                    padding: 8px 12px;
                    font-size: 12px;
                }
            """
        else:
            tooltip_style = """
                QToolTip {
                    background-color: #FFFFFF;
                    color: #1A1A1A;
                    border: 1px solid #7C4DFF;
                    border-radius: 6px;
                    padding: 8px 12px;
                    font-size: 12px;
                }
            """
        
        # Combine and apply
        app.setStyleSheet(base_style + tooltip_style)
        
        # Apply dark/light title bar on Windows
        self._apply_windows_titlebar(theme)
        
        # Save setting
        if settings.theme != theme:
            settings.set_theme(theme)
        
        # Emit signal for any listeners
        self.theme_changed.emit(theme)
    
    def _apply_windows_titlebar(self, theme: str):
        """Set Windows title bar to dark or light using DwmSetWindowAttribute."""
        if sys.platform != 'win32':
            return
        try:
            import ctypes
            import ctypes.wintypes
            dark_value = 1 if theme == 'dark' else 0

            app = QApplication.instance()
            if not app:
                return

            # Constants
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19
            SWP_FRAMECHANGED = 0x0020
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010

            for widget in app.topLevelWidgets():
                try:
                    hwnd = int(widget.winId())
                    if not hwnd:
                        continue
                    
                    hwnd_ptr = ctypes.wintypes.HWND(hwnd)
                    
                    # Try attribute 20 first (Win11/Win10 2004+), then 19 (older)
                    for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY):
                        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            hwnd_ptr,
                            ctypes.wintypes.DWORD(attr),
                            ctypes.byref(ctypes.c_int(dark_value)),
                            ctypes.sizeof(ctypes.c_int),
                        )
                        if result == 0:  # S_OK
                            break
                    
                    # Force title bar redraw - aggressive approach for Win11 24H2
                    # Get current window rect
                    rect = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd_ptr, ctypes.byref(rect))
                    width = rect.right - rect.left
                    height = rect.bottom - rect.top
                    
                    # Briefly resize by 1px then restore (forces frame redraw)
                    ctypes.windll.user32.SetWindowPos(
                        hwnd_ptr, None,
                        rect.left, rect.top, width + 1, height,
                        SWP_NOZORDER | SWP_NOACTIVATE
                    )
                    ctypes.windll.user32.SetWindowPos(
                        hwnd_ptr, None,
                        rect.left, rect.top, width, height,
                        SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOACTIVATE
                    )
                    
                except Exception:
                    continue
        except Exception:
            pass  # Silently fail on non-Windows or older versions
    
    def toggle_theme(self):
        """Toggle between dark and light themes."""
        new_theme = 'light' if settings.theme == 'dark' else 'dark'
        self.apply_theme(new_theme)
        return new_theme
    
    def _apply_dark_palette(self, app: QApplication):
        """Apply dark color palette with purple accent."""
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(10, 10, 18))        # #0A0A12
        palette.setColor(QPalette.WindowText, QColor(232, 232, 240)) # #E8E8F0
        palette.setColor(QPalette.Base, QColor(17, 17, 25))           # #111119
        palette.setColor(QPalette.AlternateBase, QColor(14, 14, 22))  # #0E0E16
        palette.setColor(QPalette.ToolTipBase, QColor(22, 22, 31))    # #16161F
        palette.setColor(QPalette.ToolTipText, QColor(232, 232, 240)) # #E8E8F0
        palette.setColor(QPalette.Text, QColor(232, 232, 240))        # #E8E8F0
        palette.setColor(QPalette.Button, QColor(22, 22, 31))         # #16161F
        palette.setColor(QPalette.ButtonText, QColor(232, 232, 240))  # #E8E8F0
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Link, QColor(124, 77, 255))         # #7C4DFF
        palette.setColor(QPalette.Highlight, QColor(124, 77, 255))    # #7C4DFF
        palette.setColor(QPalette.HighlightedText, Qt.white)
        app.setPalette(palette)
    
    def _apply_light_palette(self, app: QApplication):
        """Apply light color palette with purple accent."""
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(250, 251, 252))     # #FAFBFC
        palette.setColor(QPalette.WindowText, QColor(26, 26, 26))    # #1A1A1A
        palette.setColor(QPalette.Base, QColor(255, 255, 255))       # #FFFFFF
        palette.setColor(QPalette.AlternateBase, QColor(248, 248, 248))
        palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 255))
        palette.setColor(QPalette.ToolTipText, QColor(26, 26, 26))
        palette.setColor(QPalette.Text, QColor(26, 26, 26))
        palette.setColor(QPalette.Button, QColor(255, 255, 255))
        palette.setColor(QPalette.ButtonText, QColor(26, 26, 26))
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Link, QColor(124, 77, 255))        # #7C4DFF Purple accent
        palette.setColor(QPalette.Highlight, QColor(124, 77, 255))   # #7C4DFF
        palette.setColor(QPalette.HighlightedText, Qt.white)
        app.setPalette(palette)


def apply_titlebar_theme(widget):
    """Apply the current theme's title bar color to a specific widget.
    
    Call this in a dialog's showEvent to ensure dark/light title bar.
    Works only on Windows 10 (2004+) and Windows 11.
    """
    if sys.platform != 'win32':
        return
    
    try:
        import ctypes
        import ctypes.wintypes
        
        dark_value = 1 if settings.theme == 'dark' else 0
        hwnd = int(widget.winId())
        if not hwnd:
            return
        
        hwnd_ptr = ctypes.wintypes.HWND(hwnd)
        
        # Constants
        SWP_FRAMECHANGED = 0x0020
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        
        # Try attribute 20 first (Win11/Win10 2004+), then 19
        for attr in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd_ptr,
                ctypes.wintypes.DWORD(attr),
                ctypes.byref(ctypes.c_int(dark_value)),
                ctypes.sizeof(ctypes.c_int),
            )
            if result == 0:  # S_OK
                break
        
        # Force title bar redraw - aggressive approach for Win11 24H2
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd_ptr, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        
        # Briefly resize by 1px then restore (forces frame redraw)
        ctypes.windll.user32.SetWindowPos(
            hwnd_ptr, None,
            rect.left, rect.top, width + 1, height,
            SWP_NOZORDER | SWP_NOACTIVATE
        )
        ctypes.windll.user32.SetWindowPos(
            hwnd_ptr, None,
            rect.left, rect.top, width, height,
            SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOACTIVATE
        )
    except Exception:
        pass


# Global instance
theme_manager = ThemeManager()

