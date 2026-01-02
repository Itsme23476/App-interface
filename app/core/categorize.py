"""
File categorization logic using heuristics (extension and MIME type).
"""

import filetype
from pathlib import Path
from typing import Optional
from .settings import settings
from .ocr import extract_text_from_file, get_supported_formats


def categorize_file(file_path: Path) -> str:
    """
    Categorize a file based on its extension and MIME type.
    
    Args:
        file_path: Path to the file to categorize
        
    Returns:
        Category string (e.g., "Documents/PDFs", "Images/Photos")
    """
    # First, try extension-based categorization
    extension = file_path.suffix.lower()
    category = _categorize_by_extension(extension)
    
    if category and category != "Misc":
        return category
    
    # Fallback to MIME type detection
    category = _categorize_by_mime(file_path)
    
    return category or "Misc"


def _categorize_by_extension(extension: str) -> Optional[str]:
    """
    Categorize file by its extension.
    
    Args:
        extension: File extension (e.g., ".pdf")
        
    Returns:
        Category string or None if not found
    """
    for category, extensions in settings.category_map.items():
        if extension in extensions:
            return category
    
    return None


def _categorize_by_mime(file_path: Path) -> Optional[str]:
    """
    Categorize file by its MIME type.
    
    Args:
        file_path: Path to the file
        
    Returns:
        Category string or None if not found
    """
    try:
        # Use filetype library to detect MIME type
        kind = filetype.guess(str(file_path))
        if kind is None:
            return None
        
        mime_type = kind.mime
        
        # Check MIME type fallbacks
        for mime_prefix, category in settings.mime_fallbacks.items():
            if mime_type.startswith(mime_prefix):
                return category
        
        return None
        
    except Exception:
        # If MIME detection fails, return None
        return None


def get_file_metadata(file_path: Path) -> dict:
    """
    Get comprehensive file metadata for categorization.
    
    Args:
        file_path: Path to the file
        
    Returns:
        Dictionary with file metadata
    """
    try:
        stat = file_path.stat()
        kind = filetype.guess(str(file_path))
        
        # Basic metadata
        metadata = {
            "name": file_path.name,
            "extension": file_path.suffix.lower(),
            "size": stat.st_size,
            "mime_type": kind.mime if kind else None,
            "category": categorize_file(file_path),
            "is_file": file_path.is_file(),
            "is_dir": file_path.is_dir()
        }
        
        # Add OCR text if supported
        if file_path.suffix.lower() in get_supported_formats():
            ocr_text = extract_text_from_file(file_path)
            if ocr_text:
                metadata["ocr_text"] = ocr_text
                metadata["has_ocr"] = True
            else:
                metadata["has_ocr"] = False
        else:
            metadata["has_ocr"] = False
        
        return metadata
        
    except Exception as e:
        return {
            "name": file_path.name,
            "extension": file_path.suffix.lower(),
            "size": 0,
            "mime_type": None,
            "category": "Misc",
            "is_file": False,
            "is_dir": False,
            "has_ocr": False,
            "error": str(e)
        }


