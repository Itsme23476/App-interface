"""
Update checker for Lumina - File Search Assistant.
Uses Supabase to check for new versions (works with private GitHub repos).
"""

import logging
import webbrowser
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def compare_versions(current: str, latest: str) -> bool:
    """
    Compare two version strings.
    
    Returns:
        True if latest is newer than current
    """
    try:
        from packaging import version
        # Strip 'v' prefix if present
        current_clean = current.lstrip('v')
        latest_clean = latest.lstrip('v')
        return version.parse(latest_clean) > version.parse(current_clean)
    except Exception:
        # Fallback to string comparison
        return latest.lstrip('v') > current.lstrip('v')


def check_for_updates_supabase(current_version: str) -> Optional[Dict[str, Any]]:
    """
    Check for updates using Supabase app_version table.
    
    This method works even with private GitHub repos since version info
    is stored in Supabase with public read access.
    
    Args:
        current_version: Current app version (e.g., "1.0.0")
        
    Returns:
        Dict with update info if available, None otherwise
    """
    try:
        from app.core.supabase_client import get_latest_app_version
        
        logger.info("Checking for updates via Supabase...")
        
        version_info = get_latest_app_version()
        
        if not version_info:
            logger.info("No version info found in Supabase")
            return None
        
        latest_version = version_info.get('version', '').lstrip('v')
        
        if not latest_version:
            logger.info("No version found in Supabase response")
            return None
        
        if compare_versions(current_version, latest_version):
            logger.info(f"Update available: {current_version} -> {latest_version}")
            
            return {
                'current_version': current_version,
                'latest_version': latest_version,
                'download_url': version_info.get('download_url', ''),
                'release_notes': version_info.get('release_notes', ''),
                'release_name': version_info.get('release_name', f'Version {latest_version}'),
                'published_at': version_info.get('published_at', ''),
                'required': version_info.get('is_required', False)
            }
        else:
            logger.info(f"App is up to date (v{current_version})")
            return None
            
    except Exception as e:
        logger.info(f"Could not check for updates via Supabase: {e}")
        return None


def check_for_updates(current_version: str, check_url: str = None) -> Optional[Dict[str, Any]]:
    """
    Check for updates using Supabase.
    
    Args:
        current_version: Current app version
        check_url: Ignored (kept for backwards compatibility)
        
    Returns:
        Dict with update info if available, None otherwise
    """
    return check_for_updates_supabase(current_version)


def open_download_page(url: str) -> bool:
    """Open the download page in the default browser."""
    try:
        webbrowser.open(url)
        return True
    except Exception as e:
        logger.error(f"Could not open download page: {e}")
        return False
