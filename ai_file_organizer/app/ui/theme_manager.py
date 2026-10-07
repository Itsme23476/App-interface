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
    # Dark-violet-family LIGHT theme: lilac-grey page, white cards, soft lilac borders.
    # Accent stays #7C4DFF (handled outside this dict); purple tints live in purple_light_*.
    "bg":               "#F3F1FB",
    "surface":          "#FFFFFF",
    "card":             "#FFFFFF",
    "border":           "#E2DEF0",
    "border_strong":    "#DCD7ED",
    "text":             "#1B1730",
    "text_secondary":   "#454156",
    "text_muted":       "#6F6B83",
    "text_disabled":    "#A6A2B6",
    "input_bg":         "#F4F2FC",
    "hover":            "#F1EEFA",
    "pressed":          "#EBE8F7",
    "tab_unchecked_bg": "#F4F2FC",
    "tab_unchecked_border": "#E2DEF0",
    "tab_unchecked_text": "#6F6B83",
    "tab_unchecked_hover": "#EBE8F7",
    "danger_bg":        "#FDEEF0",
    "danger_hover":     "#FBE4E7",
    "danger_border":    "#F3C7CD",
    "danger_text":      "#D13A48",
    "purple_light_bg":  "#EDE7FF",
    "purple_light_hover":"#E4DAFF",
    "purple_pressed":   "#DED0FF",
    "scrollbar_bg":     "#ECE9F6",
    "scrollbar_handle": "#C4BEDA",
    "icon_bg":          "#F0EAFE",
    "item_bg":          "#FFFFFF",
    "divider":          "#ECE9F6",
    "dialog_bg":        "#FFFFFF",
    "dialog_border":    "#E2DEF0",
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
        # Cache of the combined (base + tooltip) QSS per theme, so a toggle
        # doesn't re-read ~26 KB from disk every time.
        self._qss_cache: dict = {}
    
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

        # Palette (cheap).
        if theme == 'dark':
            self._apply_dark_palette(app)
        else:
            self._apply_light_palette(app)

        combined_style = self._combined_qss(theme)

        # A toggle used to repaint N times: app.setStyleSheet() re-polishes every
        # widget, then every theme_changed listener restyles its own subtree —
        # each a separate repaint on the UI thread, which froze big windows.
        # Freeze painting on the visible top-level windows, do ALL the restyling,
        # then re-enable and repaint ONCE.
        tops = [w for w in app.topLevelWidgets() if w.isVisible()]
        for w in tops:
            try:
                w.setUpdatesEnabled(False)
            except Exception:
                pass
        try:
            app.setStyleSheet(combined_style)
            # Apply dark/light title bar on Windows
            self._apply_windows_titlebar(theme)
            # Save setting
            if settings.theme != theme:
                settings.set_theme(theme)
            # Listeners restyle their subtrees while painting is frozen.
            self.theme_changed.emit(theme)
        finally:
            for w in tops:
                try:
                    w.setUpdatesEnabled(True)
                    w.update()
                except Exception:
                    pass

    def _combined_qss(self, theme: str) -> str:
        """Combined (base stylesheet + tooltip) QSS for the theme, cached so a
        toggle never re-reads the ~26 KB .qss file from disk."""
        if theme in self._qss_cache:
            return self._qss_cache[theme]
        style_path = self._ui_dir / ('styles.qss' if theme == 'dark' else 'styles_light.qss')
        try:
            base_style = style_path.read_text(encoding='utf-8') if style_path.exists() else ""
        except Exception:
            base_style = ""
        if theme == 'dark':
            tooltip_style = (
                "QToolTip { background-color: #1E1E2E; color: #E8E8F0;"
                " border: 1px solid #7C4DFF; border-radius: 6px; padding: 8px 12px; font-size: 12px; }"
            )
        else:
            tooltip_style = (
                "QToolTip { background-color: #FFFFFF; color: #1A1A1A;"
                " border: 1px solid #7C4DFF; border-radius: 6px; padding: 8px 12px; font-size: 12px; }"
            )
        combined = base_style + tooltip_style
        self._qss_cache[theme] = combined
        return combined
    
    def _apply_windows_titlebar(self, theme: str):
        """Set Windows title bar to dark or light using DwmSetWindowAttribute.
        
        Enhanced for Windows 11 24H2 compatibility with DWMWA_CAPTION_COLOR fallback.
        """
        if sys.platform != 'win32':
            return
        try:
            import ctypes
            import ctypes.wintypes
            dark_value = 1 if theme == 'dark' else 0
            
            # Direct caption color for Windows 11 (COLORREF format: 0x00BBGGRR)
            # Match our dark theme background (#0A0A12 -> BGR: 0x00120A0A)
            # Light theme: white (#FFFFFF -> BGR: 0x00FFFFFF)
            caption_color = 0x00120A0A if theme == 'dark' else 0x00FFFFFF

            app = QApplication.instance()
            if not app:
                return

            # Constants
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19
            DWMWA_CAPTION_COLOR = 35  # Windows 11 only - directly sets title bar color
            SWP_FRAMECHANGED = 0x0020
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            # RedrawWindow flags
            RDW_INVALIDATE = 0x0001
            RDW_FRAME = 0x0400

            for widget in app.topLevelWidgets():
                try:
                    hwnd = int(widget.winId())
                    if not hwnd:
                        continue
                    
                    hwnd_ptr = ctypes.wintypes.HWND(hwnd)
                    
                    # Method 1: Set immersive dark mode (Win10 2004+ and Win11)
                    for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY):
                        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            hwnd_ptr,
                            ctypes.wintypes.DWORD(attr),
                            ctypes.byref(ctypes.c_int(dark_value)),
                            ctypes.sizeof(ctypes.c_int),
                        )
                        if result == 0:  # S_OK
                            break
                    
                    # Method 2: Directly set caption color (Windows 11 only)
                    # This is more reliable on Win11 24H2 and forces the color
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd_ptr,
                        ctypes.wintypes.DWORD(DWMWA_CAPTION_COLOR),
                        ctypes.byref(ctypes.wintypes.DWORD(caption_color)),
                        ctypes.sizeof(ctypes.wintypes.DWORD),
                    )
                    
                    # Force redraw of the non-client area (title bar)
                    ctypes.windll.user32.RedrawWindow(
                        hwnd_ptr, None, None,
                        RDW_INVALIDATE | RDW_FRAME
                    )
                    
                    # Trigger frame change notification
                    ctypes.windll.user32.SetWindowPos(
                        hwnd_ptr, None,
                        0, 0, 0, 0,
                        SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE
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
        palette.setColor(QPalette.Window, QColor(243, 241, 251))     # #F3F1FB lilac-grey page
        palette.setColor(QPalette.WindowText, QColor(27, 23, 48))    # #1B1730
        palette.setColor(QPalette.Base, QColor(255, 255, 255))       # #FFFFFF cards/fields
        palette.setColor(QPalette.AlternateBase, QColor(239, 237, 248))  # #EFEDF8
        palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 255))
        palette.setColor(QPalette.ToolTipText, QColor(27, 23, 48))
        palette.setColor(QPalette.Text, QColor(27, 23, 48))
        palette.setColor(QPalette.Button, QColor(255, 255, 255))
        palette.setColor(QPalette.ButtonText, QColor(27, 23, 48))
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Link, QColor(124, 77, 255))        # #7C4DFF Purple accent
        palette.setColor(QPalette.Highlight, QColor(124, 77, 255))   # #7C4DFF
        palette.setColor(QPalette.HighlightedText, Qt.white)
        app.setPalette(palette)


def apply_titlebar_theme(widget):
    """Apply the current theme's title bar color to a specific widget.
    
    Call this in a dialog's showEvent to ensure dark/light title bar.
    Works only on Windows 10 (2004+) and Windows 11.
    Enhanced for Windows 11 24H2 with DWMWA_CAPTION_COLOR support.
    """
    if sys.platform != 'win32':
        return
    
    try:
        import ctypes
        import ctypes.wintypes
        
        theme = settings.theme
        dark_value = 1 if theme == 'dark' else 0
        # Direct caption color (COLORREF format: 0x00BBGGRR)
        caption_color = 0x00120A0A if theme == 'dark' else 0x00FFFFFF
        
        hwnd = int(widget.winId())
        if not hwnd:
            return
        
        hwnd_ptr = ctypes.wintypes.HWND(hwnd)
        
        # Constants
        DWMWA_CAPTION_COLOR = 35  # Windows 11 only
        SWP_FRAMECHANGED = 0x0020
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        RDW_INVALIDATE = 0x0001
        RDW_FRAME = 0x0400
        
        # Method 1: Set immersive dark mode (Win10 2004+ and Win11)
        for attr in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd_ptr,
                ctypes.wintypes.DWORD(attr),
                ctypes.byref(ctypes.c_int(dark_value)),
                ctypes.sizeof(ctypes.c_int),
            )
            if result == 0:  # S_OK
                break
        
        # Method 2: Directly set caption color (Windows 11 only)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd_ptr,
            ctypes.wintypes.DWORD(DWMWA_CAPTION_COLOR),
            ctypes.byref(ctypes.wintypes.DWORD(caption_color)),
            ctypes.sizeof(ctypes.wintypes.DWORD),
        )
        
        # Force redraw of the non-client area (title bar)
        ctypes.windll.user32.RedrawWindow(
            hwnd_ptr, None, None,
            RDW_INVALIDATE | RDW_FRAME
        )
        
        # Trigger frame change notification
        ctypes.windll.user32.SetWindowPos(
            hwnd_ptr, None,
            0, 0, 0, 0,
            SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE
        )
    except Exception:
        pass


# Global instance
theme_manager = ThemeManager()
