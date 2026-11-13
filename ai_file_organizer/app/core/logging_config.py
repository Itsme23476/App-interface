"""
Logging configuration for the application.
"""

import logging
import os
import sys
from pathlib import Path
from .settings import settings


def setup_logging():
    """Setup application logging."""
    # Create logs directory
    logs_dir = settings.get_app_data_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Verbose diagnostics only when explicitly opted in (FILECT_DEBUG=1). In
    # production we stay at INFO so the thousands of per-file "Moved …" lines a
    # big organize used to emit never hit the console or the log file.
    debug_mode = os.environ.get('FILECT_DEBUG') == '1'
    base_level = logging.DEBUG if debug_mode else logging.INFO

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(base_level)

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(base_level)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # File handler
    log_file = logs_dir / "ai_file_organizer.log"
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(base_level)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)
    
    # Set specific logger levels
    logging.getLogger('PySide6').setLevel(logging.WARNING)
    
    logging.info("Logging configured successfully")
    logging.info(f"Log file: {log_file}")
    _record_crashes(logs_dir)


_crash_file = None  # (kept open for the whole run: faulthandler writes into it at the moment of a crash)


def _record_crashes(logs_dir: Path):
    """A crash in native code (Qt, an image or PDF decoder, an access violation...) kills Filect before Python can
    log anything. faulthandler writes every thread's Python stack to logs/crash.log at that moment, so a crash
    report says exactly where. Each launch adds one line, so a crash can be matched to its session. (Pack §6.1.)"""
    global _crash_file
    try:
        import faulthandler
        from datetime import datetime
        try:
            from app.version import VERSION
        except Exception:
            VERSION = "?"
        _crash_file = open(logs_dir / "crash.log", "a", encoding="utf-8")
        _crash_file.write(f"--- Filect {VERSION} started {datetime.now():%Y-%m-%d %H:%M:%S} (pid {os.getpid()})\n")
        _crash_file.flush()
        faulthandler.enable(_crash_file, all_threads=True)
    except Exception as e:
        logging.warning(f"Crash recorder unavailable: {e}")


