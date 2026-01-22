"""
Update checker for AI File Organizer.

Checks for new versions on startup and notifies the user.
"""

import json
import logging
import threading
import webbrowser
from typing import Optional, Tuple, Callable

import requests

from version import VERSION, VERSION_TUPLE, UPDATE_CHECK_URL, DOWNLOAD_URL

logger = logging.getLogger(__name__)


def parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse version string to tuple for comparison."""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def check_for_updates() -> Optional[dict]:
    """
    Check for updates by fetching version.json from GitHub.
    
    Returns:
        Dict with update info if update available, None otherwise.
        {
            "available": True,
            "current_version": "1.0.0",
            "latest_version": "1.1.0",
            "download_url": "https://...",
            "release_notes": "What's new..."
        }
    """
    try:
        response = requests.get(UPDATE_CHECK_URL, timeout=5)
        if response.status_code != 200:
            logger.debug(f"Update check failed: HTTP {response.status_code}")
            return None
        
        data = response.json()
        latest_version = data.get("version", "0.0.0")
        latest_tuple = parse_version(latest_version)
        
        if latest_tuple > VERSION_TUPLE:
            return {
                "available": True,
                "current_version": VERSION,
                "latest_version": latest_version,
                "download_url": data.get("download_url", DOWNLOAD_URL),
                "release_notes": data.get("release_notes", ""),
            }
        
        logger.debug(f"App is up to date (v{VERSION})")
        return None
        
    except requests.RequestException as e:
        logger.debug(f"Update check failed: {e}")
        return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.debug(f"Invalid update response: {e}")
        return None


def check_for_updates_async(callback: Callable[[Optional[dict]], None]):
    """
    Check for updates in a background thread.
    
    Args:
        callback: Function to call with the result (on the calling thread)
    """
    def _check():
        result = check_for_updates()
        callback(result)
    
    thread = threading.Thread(target=_check, daemon=True)
    thread.start()


def open_download_page(url: Optional[str] = None):
    """Open the download page in the default browser."""
    webbrowser.open(url or DOWNLOAD_URL)
