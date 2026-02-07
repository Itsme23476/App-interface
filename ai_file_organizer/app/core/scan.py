"""
File scanning and discovery logic.
"""

import logging
from pathlib import Path
from typing import List, Dict, Any
from .categorize import get_file_metadata


logger = logging.getLogger(__name__)


def scan_directory(source_path: Path, max_files: int = 1000) -> List[Dict[str, Any]]:
    """
    Recursively scan a directory and collect file information.
    
    Args:
        source_path: Root directory to scan
        max_files: Maximum number of files to process
        
    Returns:
        List of file metadata dictionaries
    """
    files = []
    
    try:
        if not source_path.exists():
            logger.error(f"Source path does not exist: {source_path}")
            return files
        
        if not source_path.is_dir():
            logger.error(f"Source path is not a directory: {source_path}")
            return files
        
        logger.info(f"Starting scan of directory: {source_path}")
        
        # Walk through directory recursively
        for file_path in source_path.rglob('*'):
            if len(files) >= max_files:
                logger.warning(f"Reached maximum file limit ({max_files})")
                break
            
            # Skip directories for now (we only move files)
            if file_path.is_dir():
                continue
            
            # Skip hidden files and system files
            if _should_skip_file(file_path):
                continue
            
            try:
                metadata = get_file_metadata(file_path)
                if metadata:
                    # Include full source path for files in subfolders
                    metadata['source_path'] = str(file_path)
                    files.append(metadata)
                    
            except Exception as e:
                logger.error(f"Error processing file {file_path}: {e}")
                continue
        
        logger.info(f"Scan completed. Found {len(files)} files.")
        return files
        
    except Exception as e:
        logger.error(f"Error scanning directory {source_path}: {e}")
        return files


def _is_onedrive_cloud_file(file_path: Path) -> bool:
    """
    Check if a file is an OneDrive cloud-only (placeholder) file.
    These files appear in directory listings but aren't actually on disk.
    
    Args:
        file_path: Path to check
        
    Returns:
        True if file is a cloud-only placeholder
    """
    import os
    
    # Only check on Windows
    if os.name != 'nt':
        return False
    
    try:
        import ctypes
        
        # Windows file attribute flags for cloud files
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000  # Cloud file - download on access
        FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000  # Cloud file - download on open
        FILE_ATTRIBUTE_OFFLINE = 0x1000  # Offline file
        
        # Get file attributes using Windows API
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(file_path))
        
        if attrs == -1:  # INVALID_FILE_ATTRIBUTES
            return True  # Can't read attributes, skip it
        
        # Check for cloud/offline attributes
        if attrs & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_OFFLINE):
            logger.debug(f"Skipping OneDrive cloud file: {file_path.name}")
            return True
        
        return False
        
    except Exception as e:
        logger.debug(f"Error checking OneDrive status for {file_path}: {e}")
        return False


def _should_skip_file(file_path: Path) -> bool:
    """
    Determine if a file should be skipped during scanning.
    
    Note: Exclusion patterns from settings are NOT checked here.
    Files can be indexed even if they match exclusion patterns.
    Exclusion patterns only prevent files from being MOVED during organization.
    
    Args:
        file_path: Path to the file
        
    Returns:
        True if file should be skipped
    """
    # Skip hidden files
    if file_path.name.startswith('.'):
        return True
    
    # Skip system files
    system_files = {
        'thumbs.db', 'desktop.ini', '.ds_store', 
        'icon\r', 'icon\n', 'icon\r\n'
    }
    if file_path.name.lower() in system_files:
        return True
    
    # Skip temporary files
    temp_extensions = {'.tmp', '.temp', '.bak', '.swp', '.swo'}
    if file_path.suffix.lower() in temp_extensions:
        return True
    
    # Skip OneDrive cloud-only files (not downloaded locally)
    if _is_onedrive_cloud_file(file_path):
        return True
    
    return False


def get_directory_stats(source_path: Path) -> Dict[str, Any]:
    """
    Get basic statistics about a directory.
    
    Args:
        source_path: Directory to analyze
        
    Returns:
        Dictionary with directory statistics
    """
    try:
        if not source_path.exists() or not source_path.is_dir():
            return {"error": "Invalid directory path"}
        
        total_files = 0
        total_dirs = 0
        total_size = 0
        
        for item in source_path.rglob('*'):
            if item.is_file():
                total_files += 1
                try:
                    total_size += item.stat().st_size
                except:
                    pass
            elif item.is_dir():
                total_dirs += 1
        
        return {
            "total_files": total_files,
            "total_directories": total_dirs,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2)
        }
        
    except Exception as e:
        return {"error": str(e)}


