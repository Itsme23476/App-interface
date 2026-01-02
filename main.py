#!/usr/bin/env python3
"""
File Search Assistant - v1.0
A privacy-first desktop application for intelligent file search and quick path autofill.
Instantly find and autofill file paths in any application using global hotkeys.
"""

import sys
import os
from pathlib import Path

# Add the app directory to Python path
app_dir = Path(__file__).parent / "app"
sys.path.insert(0, str(app_dir))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from ui.main_window import MainWindow
from core.logging_config import setup_logging


def main():
    """Main application entry point."""
    # Setup logging
    setup_logging()
    
    # Create Qt application
    app = QApplication(sys.argv)
    app.setApplicationName("File Search Assistant")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("File Search Assistant")
    
    # Apply saved theme (dark/light)
    try:
        from ui.theme_manager import theme_manager
        theme_manager.apply_theme()
    except Exception as e:
        print(f"Failed to apply theme: {e}")
    
    # High DPI handling is enabled by default in Qt6; deprecated attributes removed
    
    # Create and show main window
    window = MainWindow()
    window.show()
    
    # Start event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()


