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
from pathlib import Path
from typing import Optional, Callable
import urllib.request

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
    progress_callback: Optional[Callable[[int, int], None]] = None
) -> Optional[Path]:
    """
    Download update installer from URL.
    
    Args:
        download_url: URL to download from (GitHub Release asset)
        progress_callback: Optional callback(downloaded_bytes, total_bytes)
        
    Returns:
        Path to downloaded installer file, or None on failure
    """
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
        
        logger.info(f"Downloading update from: {download_url}")
        
        # Create request with headers
        request = urllib.request.Request(
            download_url,
            headers={
                'User-Agent': 'Lumina-Updater/1.0',
                'Accept': 'application/octet-stream'
            }
        )
        
        with urllib.request.urlopen(request, timeout=120) as response:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            chunk_size = 65536  # 64KB chunks for faster download
            
            logger.info(f"Download size: {total_size / (1024*1024):.2f} MB")
            
            with open(installer_path, 'wb') as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if progress_callback:
                        progress_callback(downloaded, total_size if total_size > 0 else downloaded)
        
        # Verify the file was downloaded
        if installer_path.exists() and installer_path.stat().st_size > 0:
            logger.info(f"Download complete: {installer_path} ({installer_path.stat().st_size / (1024*1024):.2f} MB)")
            return installer_path
        else:
            logger.error("Downloaded file is empty or missing")
            return None
        
    except urllib.error.HTTPError as e:
        logger.error(f"HTTP Error downloading update: {e.code} {e.reason}")
        return None
    except urllib.error.URLError as e:
        logger.error(f"URL Error downloading update: {e.reason}")
        return None
    except Exception as e:
        logger.error(f"Download failed: {e}", exc_info=True)
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
