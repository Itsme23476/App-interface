"""
EXIF metadata extraction utilities.
Extracts original creation dates from images that preserve this information.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# File extensions that typically contain EXIF data
EXIF_SUPPORTED_EXTENSIONS = {
    '.jpg', '.jpeg', '.tiff', '.tif', '.heic', '.heif',
    '.png', '.webp', '.raw', '.cr2', '.nef', '.arw', '.dng'
}


def get_exif_date(file_path: str) -> Optional[datetime]:
    """
    Extract the original creation date from an image's EXIF metadata.
    
    Args:
        file_path: Path to the image file
        
    Returns:
        datetime of the original creation date, or None if not available
    """
    try:
        path = Path(file_path)
        
        # Check if file type supports EXIF
        if path.suffix.lower() not in EXIF_SUPPORTED_EXTENSIONS:
            return None
        
        if not path.exists():
            return None
        
        from PIL import Image
        from PIL.ExifTags import TAGS
        
        with Image.open(file_path) as img:
            exif_data = img._getexif()
            
            if not exif_data:
                logger.debug(f"No EXIF data found in {file_path}")
                return None
            
            # Look for date tags in order of preference
            date_tags = [
                36867,  # DateTimeOriginal - when photo was taken
                36868,  # DateTimeDigitized - when digitized
                306,    # DateTime - modification time
            ]
            
            for tag_id in date_tags:
                if tag_id in exif_data:
                    date_str = exif_data[tag_id]
                    if date_str:
                        # EXIF dates are typically in format: "YYYY:MM:DD HH:MM:SS"
                        try:
                            parsed_date = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                            logger.debug(f"Extracted EXIF date from {path.name}: {parsed_date}")
                            return parsed_date
                        except ValueError:
                            # Try alternative formats
                            try:
                                parsed_date = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                                return parsed_date
                            except ValueError:
                                continue
            
            logger.debug(f"No date found in EXIF for {file_path}")
            return None
            
    except Exception as e:
        logger.debug(f"Could not extract EXIF from {file_path}: {e}")
        return None


def get_best_date(file_path: str, created_date: Optional[str] = None, 
                  modified_date: Optional[str] = None) -> Optional[str]:
    """
    Get the best available date for a file, prioritizing:
    1. EXIF DateTimeOriginal (for images)
    2. File modification date
    3. File creation date (least reliable on Windows)
    
    Args:
        file_path: Path to the file
        created_date: ISO format created date from filesystem
        modified_date: ISO format modified date from filesystem
        
    Returns:
        ISO format date string representing the best date
    """
    # Try EXIF first for images
    exif_date = get_exif_date(file_path)
    if exif_date:
        return exif_date.isoformat()
    
    # Fall back to modification date (more reliable than creation)
    if modified_date:
        return modified_date
    
    # Last resort: creation date
    return created_date

