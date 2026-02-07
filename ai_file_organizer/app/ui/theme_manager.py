"""
Theme manager for switching between dark and light modes.
"""

import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor
from PySide6.QtCore import Qt, QObject, Signal

from app.core.settings import settings


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
                app.setStyleSheet(f.read())
        
        # Save setting
        if settings.theme != theme:
            settings.set_theme(theme)
        
        # Emit signal for any listeners
        self.theme_changed.emit(theme)
    
    def toggle_theme(self):
        """Toggle between dark and light themes."""
        new_theme = 'light' if settings.theme == 'dark' else 'dark'
        self.apply_theme(new_theme)
        return new_theme
    
    def _apply_dark_palette(self, app: QApplication):
        """Apply dark color palette with purple accent."""
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(15, 15, 15))        # #0F0F0F
        palette.setColor(QPalette.WindowText, QColor(224, 224, 224)) # #E0E0E0
        palette.setColor(QPalette.Base, QColor(26, 26, 26))          # #1A1A1A
        palette.setColor(QPalette.AlternateBase, QColor(15, 15, 15)) # #0F0F0F
        palette.setColor(QPalette.ToolTipBase, QColor(30, 30, 30))
        palette.setColor(QPalette.ToolTipText, QColor(224, 224, 224))
        palette.setColor(QPalette.Text, QColor(224, 224, 224))
        palette.setColor(QPalette.Button, QColor(30, 30, 30))
        palette.setColor(QPalette.ButtonText, QColor(224, 224, 224))
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Link, QColor(124, 77, 255))        # #7C4DFF Purple accent
        palette.setColor(QPalette.Highlight, QColor(124, 77, 255))   # #7C4DFF
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


# Global instance
theme_manager = ThemeManager()

