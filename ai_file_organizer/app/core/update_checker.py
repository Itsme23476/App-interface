"""
Update checker for Lumina - File Search Assistant.
Uses GitHub Releases API to automatically detect new versions.
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
        # Strip 'v' prefix if present (GitHub tags often use v1.0.0)
        current_clean = current.lstrip('v')
        latest_clean = latest.lstrip('v')
        return version.parse(latest_clean) > version.parse(current_clean)
    except Exception:
        # Fallback to string comparison
        return latest.lstrip('v') > current.lstrip('v')


def check_for_updates_github(current_version: str, github_url: str) -> Optional[Dict[str, Any]]:
    """
    Check for updates using GitHub Releases API.
    
    Args:
        current_version: Current app version (e.g., "1.0.0")
        github_url: GitHub API URL for latest release
        
    Returns:
        Dict with update info if available, None otherwise
    """
    try:
        import urllib.request
        import json
        
        logger.info(f"Checking for updates via GitHub Releases...")
        
        # GitHub API request with proper headers
        request = urllib.request.Request(
            github_url,
            headers={
                'User-Agent': f'AIFileOrganizer/{current_version}',
                'Accept': 'application/vnd.github.v3+json'
            }
        )
        
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
        
        # Extract version from tag_name (e.g., "v1.1.0" -> "1.1.0")
        latest_version = data.get('tag_name', '').lstrip('v')
        
        if not latest_version:
            logger.debug("No version tag found in release")
            return None
        
        if compare_versions(current_version, latest_version):
            logger.info(f"Update available: {current_version} -> {latest_version}")
            
            # Get download URL - prefer the release page, or first asset if available
            download_url = data.get('html_url', '')  # Release page URL
            
            # If there are downloadable assets, get the first one (usually the installer)
            assets = data.get('assets', [])
            if assets:
                # Look for .exe or .zip files first
                for asset in assets:
                    name = asset.get('name', '').lower()
                    if name.endswith('.exe') or name.endswith('.zip') or name.endswith('.msi'):
                        download_url = asset.get('browser_download_url', download_url)
                        break
            
            return {
                'current_version': current_version,
                'latest_version': latest_version,
                'download_url': download_url,
                'release_notes': data.get('body', ''),
                'release_name': data.get('name', f'Version {latest_version}'),
                'published_at': data.get('published_at', ''),
                'required': False
            }
        else:
            logger.info(f"App is up to date (v{current_version})")
            return None
            
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.debug("No releases found on GitHub yet")
        else:
            logger.debug(f"GitHub API error: {e}")
        return None
    except Exception as e:
        logger.debug(f"Could not check for updates: {e}")
        return None


def check_for_updates(current_version: str, check_url: str) -> Optional[Dict[str, Any]]:
    """
    Check for updates - wrapper that uses GitHub Releases API.
    
    Args:
        current_version: Current app version
        check_url: GitHub Releases API URL
        
    Returns:
        Dict with update info if available, None otherwise
    """
    return check_for_updates_github(current_version, check_url)


def open_download_page(url: str) -> bool:
    """Open the download page in the default browser."""
    try:
        webbrowser.open(url)
        return True
    except Exception as e:
        logger.error(f"Could not open download page: {e}")
        return False
