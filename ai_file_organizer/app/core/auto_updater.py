"""
Auto-updater for the Lumina app.
Downloads installer from releases and runs it to apply updates.
"""

import logging
import os
import sys
import shutil
import tempfile
import subprocess
import ssl
import certifi
from pathlib import Path
from typing import Optional, Callable

# Use requests library - much better SSL handling for PyInstaller apps
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    HAS_REQUESTS = False

logger = logging.getLogger(__name__)


def get_app_dir() -> Path:
    """Get the directory where the app is installed."""
    if getattr(sys, 'frozen', False):
        # Running as compiled exe
        return Path(sys.executable).parent
    else:
        # Running as script - use the ai_file_organizer folder
        return Path(__file__).parent.parent.parent


def get_update_dir() -> Path:
    """Get temporary directory for update downloads."""
    update_dir = Path(tempfile.gettempdir()) / "lumina_update"
    update_dir.mkdir(exist_ok=True)
    return update_dir


def download_update(
    download_url: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    status_callback: Optional[Callable[[str], None]] = None
) -> Optional[Path]:
    """
    Download update installer from URL.
    
    Args:
        download_url: URL to download from (GitHub Release asset)
        progress_callback: Optional callback(downloaded_bytes, total_bytes)
        status_callback: Optional callback(status_message) for UI updates
        
    Returns:
        Path to downloaded installer file, or None on failure
    """
    def update_status(msg: str):
        logger.info(msg)
        if status_callback:
            status_callback(msg)
    
    try:
        update_dir = get_update_dir()
        
        # Determine filename from URL
        filename = download_url.split('/')[-1]
        if not filename.endswith('.exe'):
            filename = "Lumina-Setup.exe"
        
        installer_path = update_dir / filename
        
        # Clean up any previous download
        if installer_path.exists():
            installer_path.unlink()
        
        update_status("Connecting to server...")
        logger.info(f"Downloading update from: {download_url}")
        
        if HAS_REQUESTS:
            return _download_with_requests(download_url, installer_path, progress_callback, update_status)
        else:
            return _download_with_urllib(download_url, installer_path, progress_callback, update_status)
        
    except Exception as e:
        logger.error(f"Download failed: {e}", exc_info=True)
        if status_callback:
            status_callback(f"Download failed: {str(e)[:50]}")
        return None


def _download_with_requests(
    download_url: str,
    installer_path: Path,
    progress_callback: Optional[Callable[[int, int], None]],
    update_status: Callable[[str], None]
) -> Optional[Path]:
    """Download using requests library - better SSL handling."""
    try:
        update_status("Establishing secure connection...")
        
        # Use requests with streaming for large files
        # verify=True uses certifi's certificates which work in PyInstaller
        response = requests.get(
            download_url,
            stream=True,
            timeout=(30, 300),  # (connect timeout, read timeout)
            headers={
                'User-Agent': 'Lumina-Updater/2.0',
                'Accept': 'application/octet-stream'
            },
            allow_redirects=True
        )
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        chunk_size = 131072  # 128KB chunks
        
        if total_size > 0:
            update_status(f"Downloading... 0 / {total_size / (1024*1024):.1f} MB")
            logger.info(f"Download size: {total_size / (1024*1024):.2f} MB")
        else:
            update_status("Downloading...")
        
        # Initial progress callback
        if progress_callback:
            progress_callback(0, total_size if total_size > 0 else 1)
        
        with open(installer_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if progress_callback:
                        progress_callback(downloaded, total_size if total_size > 0 else downloaded)
                    
                    # Update status every ~5MB
                    if total_size > 0 and downloaded % (5 * 1024 * 1024) < chunk_size:
                        percent = int((downloaded / total_size) * 100)
                        update_status(f"Downloading... {downloaded / (1024*1024):.1f} / {total_size / (1024*1024):.1f} MB ({percent}%)")
        
        # Verify the file was downloaded
        if installer_path.exists() and installer_path.stat().st_size > 0:
            actual_size = installer_path.stat().st_size
            logger.info(f"Download complete: {installer_path} ({actual_size / (1024*1024):.2f} MB)")
            update_status("Download complete!")
            return installer_path
        else:
            logger.error("Downloaded file is empty or missing")
            update_status("Download failed - file is empty")
            return None
            
    except requests.exceptions.SSLError as e:
        logger.error(f"SSL Error: {e}")
        update_status("SSL certificate error - trying fallback...")
        # Try with SSL verification disabled as fallback (not ideal but works)
        return _download_with_requests_no_verify(download_url, installer_path, progress_callback, update_status)
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection Error: {e}")
        update_status("Connection failed - check internet")
        return None
    except requests.exceptions.Timeout as e:
        logger.error(f"Timeout: {e}")
        update_status("Connection timed out")
        return None
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP Error: {e.response.status_code} {e.response.reason}")
        update_status(f"Server error: {e.response.status_code}")
        return None
    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        update_status(f"Download error: {str(e)[:40]}")
        return None


def _download_with_requests_no_verify(
    download_url: str,
    installer_path: Path,
    progress_callback: Optional[Callable[[int, int], None]],
    update_status: Callable[[str], None]
) -> Optional[Path]:
    """Fallback download without SSL verification."""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        update_status("Retrying download...")
        
        response = requests.get(
            download_url,
            stream=True,
            timeout=(30, 300),
            headers={
                'User-Agent': 'Lumina-Updater/2.0',
                'Accept': 'application/octet-stream'
            },
            allow_redirects=True,
            verify=False  # Disable SSL verification as fallback
        )
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        chunk_size = 131072
        
        if progress_callback:
            progress_callback(0, total_size if total_size > 0 else 1)
        
        with open(installer_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size if total_size > 0 else downloaded)
        
        if installer_path.exists() and installer_path.stat().st_size > 0:
            logger.info(f"Fallback download complete: {installer_path}")
            update_status("Download complete!")
            return installer_path
        return None
        
    except Exception as e:
        logger.error(f"Fallback download also failed: {e}")
        update_status("Download failed")
        return None


def _download_with_urllib(
    download_url: str,
    installer_path: Path,
    progress_callback: Optional[Callable[[int, int], None]],
    update_status: Callable[[str], None]
) -> Optional[Path]:
    """Fallback download using urllib (when requests not available)."""
    import urllib.request
    import urllib.error
    
    try:
        update_status("Establishing connection...")
        
        # Create SSL context with certifi certificates
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        
        request = urllib.request.Request(
            download_url,
            headers={
                'User-Agent': 'Lumina-Updater/2.0',
                'Accept': 'application/octet-stream'
            }
        )
        
        with urllib.request.urlopen(request, timeout=120, context=ssl_context) as response:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            chunk_size = 131072
            
            if total_size > 0:
                update_status(f"Downloading... 0 / {total_size / (1024*1024):.1f} MB")
                logger.info(f"Download size: {total_size / (1024*1024):.2f} MB")
            
            if progress_callback:
                progress_callback(0, total_size if total_size > 0 else 1)
            
            with open(installer_path, 'wb') as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if progress_callback:
                        progress_callback(downloaded, total_size if total_size > 0 else downloaded)
        
        if installer_path.exists() and installer_path.stat().st_size > 0:
            logger.info(f"Download complete: {installer_path}")
            update_status("Download complete!")
            return installer_path
        else:
            logger.error("Downloaded file is empty or missing")
            return None
            
    except urllib.error.HTTPError as e:
        logger.error(f"HTTP Error: {e.code} {e.reason}")
        update_status(f"Server error: {e.code}")
        return None
    except urllib.error.URLError as e:
        logger.error(f"URL Error: {e.reason}")
        update_status("Connection failed")
        return None
    except Exception as e:
        logger.error(f"urllib download failed: {e}", exc_info=True)
        return None


def run_installer_and_exit(installer_path: Path) -> bool:
    """
    Run the installer and exit the current app.
    
    The installer will handle updating the app files.
    
    Args:
        installer_path: Path to the downloaded installer
        
    Returns:
        True if installer was launched successfully
    """
    try:
        if not installer_path.exists():
            logger.error(f"Installer not found: {installer_path}")
            return False
        
        logger.info(f"Launching installer: {installer_path}")
        
        if sys.platform == 'win32':
            # Launch the installer detached from this process
            # /SILENT = install without prompts (can also use /VERYSILENT)
            # The user can still see progress
            subprocess.Popen(
                [str(installer_path), '/SILENT'],
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS,
                close_fds=True
            )
        else:
            # Non-Windows: just open the installer
            subprocess.Popen(
                [str(installer_path)],
                start_new_session=True
            )
        
        logger.info("Installer launched successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to launch installer: {e}", exc_info=True)
        return False


def apply_update_and_restart(installer_path: Path) -> bool:
    """
    Apply update by running the installer and closing the app.
    
    Args:
        installer_path: Path to downloaded installer
        
    Returns:
        True if update process started successfully
    """
    return run_installer_and_exit(installer_path)


def cleanup_update_files():
    """Clean up any leftover update files."""
    try:
        update_dir = get_update_dir()
        if update_dir.exists():
            shutil.rmtree(update_dir)
            logger.info("Cleaned up update files")
    except Exception as e:
        logger.debug(f"Could not clean up update files: {e}")
