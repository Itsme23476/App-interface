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

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt
from ui.main_window import MainWindow
from ui.auth_dialog import AuthDialog
from core.logging_config import setup_logging
from core.supabase_client import supabase_auth, SUPABASE_AVAILABLE
from core.settings import settings


def check_existing_session():
    """
    Check if there's a valid stored session with active subscription.
    Returns True if user can skip login, False otherwise.
    """
    if not settings.has_stored_session():
        return False
    
    # Try to restore the session
    result = supabase_auth.restore_session(
        settings.auth_access_token,
        settings.auth_refresh_token
    )
    
    if not result.get('success'):
        # Session invalid, clear tokens
        settings.clear_auth_tokens()
        return False
    
    # Check subscription
    sub_result = supabase_auth.check_subscription()
    if sub_result.get('has_subscription'):
        return True
    
    return False


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
    
    # Check if Supabase is available
    if not SUPABASE_AVAILABLE:
        QMessageBox.warning(
            None,
            "Missing Dependency",
            "The 'supabase' package is required.\n\nPlease run: pip install supabase"
        )
        sys.exit(1)
    
    # Check for existing valid session BEFORE showing auth dialog
    has_valid_session = check_existing_session()
    
    if not has_valid_session:
        # Show auth dialog
        auth_dialog = AuthDialog()
        auth_result = auth_dialog.exec()
        
        # If dialog was rejected (closed without auth), exit
        if auth_result == 0:  # QDialog.Rejected
            sub_check = supabase_auth.check_subscription()
            if not sub_check.get('has_subscription'):
                sys.exit(0)  # Exit cleanly if no subscription
    
    # Create and show main window
    window = MainWindow()
    window.show()
    
    # Start event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
