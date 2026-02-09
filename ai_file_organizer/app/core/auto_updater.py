"""
Auto-updater for the AI File Organizer app.
Downloads updates from GitHub Releases and applies them.
"""

import logging
import os
import sys
import shutil
import tempfile
import zipfile
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
    update_dir = Path(tempfile.gettempdir()) / "ai_file_organizer_update"
    update_dir.mkdir(exist_ok=True)
    return update_dir


def download_update(
    download_url: str,
    progress_callback: Optional[Callable[[int, int], None]] = None
) -> Optional[Path]:
    """
    Download update zip from URL.
    
    Args:
        download_url: URL to download from (GitHub Release asset)
        progress_callback: Optional callback(downloaded_bytes, total_bytes)
        
    Returns:
        Path to downloaded zip file, or None on failure
    """
    try:
        update_dir = get_update_dir()
        zip_path = update_dir / "update.zip"
        
        # Clean up any previous download
        if zip_path.exists():
            zip_path.unlink()
        
        logger.info(f"Downloading update from: {download_url}")
        
        # Create request with headers
        request = urllib.request.Request(
            download_url,
            headers={'User-Agent': 'AIFileOrganizer-Updater'}
        )
        
        with urllib.request.urlopen(request, timeout=60) as response:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            chunk_size = 8192
            
            with open(zip_path, 'wb') as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if progress_callback and total_size > 0:
                        progress_callback(downloaded, total_size)
        
        logger.info(f"Download complete: {zip_path}")
        return zip_path
        
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None


def extract_update(zip_path: Path) -> Optional[Path]:
    """
    Extract update zip to temporary folder.
    
    Args:
        zip_path: Path to downloaded zip file
        
    Returns:
        Path to extracted folder, or None on failure
    """
    try:
        update_dir = get_update_dir()
        extract_path = update_dir / "extracted"
        
        # Clean up any previous extraction
        if extract_path.exists():
            shutil.rmtree(extract_path)
        extract_path.mkdir()
        
        logger.info(f"Extracting update to: {extract_path}")
        
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_path)
        
        # Find the actual app folder (might be nested)
        contents = list(extract_path.iterdir())
        if len(contents) == 1 and contents[0].is_dir():
            # Zip contained a single folder - use that
            return contents[0]
        else:
            return extract_path
            
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return None


def create_updater_script(extracted_path: Path, app_dir: Path) -> Optional[Path]:
    """
    Create a batch script that will apply the update after app closes.
    
    Args:
        extracted_path: Path to extracted update files
        app_dir: Path to current app installation
        
    Returns:
        Path to updater script, or None on failure
    """
    try:
        update_dir = get_update_dir()
        script_path = update_dir / "apply_update.bat"
        
        # Get the main executable name
        if getattr(sys, 'frozen', False):
            exe_name = Path(sys.executable).name
        else:
            exe_name = "python.exe"
            
        # Create batch script
        script_content = f'''@echo off
echo Applying update, please wait...

:: Wait for the app to close
timeout /t 2 /nobreak > nul

:: Copy new files over old ones
xcopy /E /Y /Q "{extracted_path}\\*" "{app_dir}\\"

:: Clean up update files
rmdir /S /Q "{update_dir}"

:: Relaunch the app
start "" "{app_dir}\\{exe_name}"

:: Delete this script
del "%~f0"
'''
        
        with open(script_path, 'w') as f:
            f.write(script_content)
        
        logger.info(f"Created updater script: {script_path}")
        return script_path
        
    except Exception as e:
        logger.error(f"Failed to create updater script: {e}")
        return None


def apply_update_and_restart(extracted_path: Path) -> bool:
    """
    Apply update and restart the app.
    
    This creates a batch script, launches it, and exits the current app.
    The script will copy new files and relaunch.
    
    Args:
        extracted_path: Path to extracted update files
        
    Returns:
        True if update process started successfully
    """
    try:
        app_dir = get_app_dir()
        
        # Create the updater script
        script_path = create_updater_script(extracted_path, app_dir)
        if not script_path:
            return False
        
        logger.info("Starting update process...")
        
        # Launch the updater script (detached from this process)
        if sys.platform == 'win32':
            # Use subprocess with CREATE_NEW_CONSOLE to detach
            subprocess.Popen(
                ['cmd', '/c', str(script_path)],
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS,
                close_fds=True
            )
        else:
            # Unix-like systems
            subprocess.Popen(
                ['bash', str(script_path)],
                start_new_session=True
            )
        
        logger.info("Update script launched, app will close now...")
        return True
        
    except Exception as e:
        logger.error(f"Failed to start update process: {e}")
        return False


def cleanup_update_files():
    """Clean up any leftover update files."""
    try:
        update_dir = get_update_dir()
        if update_dir.exists():
            shutil.rmtree(update_dir)
            logger.info("Cleaned up update files")
    except Exception as e:
        logger.debug(f"Could not clean up update files: {e}")
