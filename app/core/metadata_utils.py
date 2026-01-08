"""
File metadata extraction utilities.
Extracts original creation dates from various file types.
"""
import logging
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


def get_file_original_date(file_path: str) -> Optional[datetime]:
    """
    Extract the original creation date from a file's metadata.
    Supports multiple file types.
    
    Args:
        file_path: Path to the file
        
    Returns:
        datetime of the original creation date, or None if not available
    """
    path = Path(file_path)
    if not path.exists():
        return None
    
    ext = path.suffix.lower()
    
    try:
        # Route to appropriate extractor based on file type
        if ext in {'.jpg', '.jpeg', '.tiff', '.tif', '.heic', '.heif'}:
            return _get_exif_date(file_path)
        elif ext == '.png':
            return _get_png_date(file_path)
        elif ext == '.webp':
            return _get_webp_date(file_path)
        elif ext in {'.docx', '.xlsx', '.pptx'}:
            return _get_office_date(file_path)
        elif ext == '.pdf':
            return _get_pdf_date(file_path)
        elif ext in {'.mp4', '.mov', '.m4v', '.avi', '.mkv'}:
            return _get_video_date(file_path)
        else:
            # Try to parse date from filename as last resort
            return _get_filename_date(file_path)
    except Exception as e:
        logger.debug(f"Could not extract date from {file_path}: {e}")
        return None


def _get_exif_date(file_path: str) -> Optional[datetime]:
    """Extract date from EXIF data (JPEG, TIFF, etc.)"""
    try:
        from PIL import Image
        
        with Image.open(file_path) as img:
            exif_data = img._getexif()
            
            if not exif_data:
                return None
            
            # EXIF date tags in order of preference
            date_tags = [
                36867,  # DateTimeOriginal
                36868,  # DateTimeDigitized
                306,    # DateTime
            ]
            
            for tag_id in date_tags:
                if tag_id in exif_data:
                    date_str = exif_data[tag_id]
                    if date_str:
                        parsed = _parse_exif_date(date_str)
                        if parsed:
                            logger.debug(f"EXIF date from {Path(file_path).name}: {parsed}")
                            return parsed
            
            return None
    except Exception as e:
        logger.debug(f"EXIF extraction failed for {file_path}: {e}")
        return None


def _get_png_date(file_path: str) -> Optional[datetime]:
    """Extract date from PNG metadata (tEXt chunks, XMP)."""
    try:
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo
        
        with Image.open(file_path) as img:
            # Check PNG text chunks
            info = img.info
            
            # Common date keys in PNG metadata
            date_keys = ['Creation Time', 'create-date', 'DateTimeOriginal', 
                        'xmp:CreateDate', 'photoshop:DateCreated', 'date:create']
            
            for key in date_keys:
                if key in info:
                    date_str = info[key]
                    parsed = _parse_flexible_date(date_str)
                    if parsed:
                        logger.debug(f"PNG date from {Path(file_path).name}: {parsed}")
                        return parsed
            
            # Check for XMP data
            if 'XML:com.adobe.xmp' in info:
                xmp_date = _parse_xmp_date(info['XML:com.adobe.xmp'])
                if xmp_date:
                    logger.debug(f"PNG XMP date from {Path(file_path).name}: {xmp_date}")
                    return xmp_date
            
            return None
    except Exception as e:
        logger.debug(f"PNG date extraction failed for {file_path}: {e}")
        return None


def _get_webp_date(file_path: str) -> Optional[datetime]:
    """Extract date from WebP metadata."""
    try:
        from PIL import Image
        
        with Image.open(file_path) as img:
            # WebP can contain EXIF data
            exif = img.getexif()
            if exif:
                # DateTimeOriginal
                if 36867 in exif:
                    parsed = _parse_exif_date(exif[36867])
                    if parsed:
                        return parsed
                # DateTime
                if 306 in exif:
                    parsed = _parse_exif_date(exif[306])
                    if parsed:
                        return parsed
            
            return None
    except Exception as e:
        logger.debug(f"WebP date extraction failed for {file_path}: {e}")
        return None


def _get_office_date(file_path: str) -> Optional[datetime]:
    """Extract creation date from Office documents (docx, xlsx, pptx)."""
    try:
        # Office files are ZIP archives with XML metadata
        with zipfile.ZipFile(file_path, 'r') as zf:
            # Core properties are in docProps/core.xml
            if 'docProps/core.xml' in zf.namelist():
                with zf.open('docProps/core.xml') as f:
                    tree = ET.parse(f)
                    root = tree.getroot()
                    
                    # Namespace for Dublin Core
                    namespaces = {
                        'dcterms': 'http://purl.org/dc/terms/',
                        'xsi': 'http://www.w3.org/2001/XMLSchema-instance'
                    }
                    
                    # Try dcterms:created
                    created = root.find('.//dcterms:created', namespaces)
                    if created is not None and created.text:
                        parsed = _parse_iso_date(created.text)
                        if parsed:
                            logger.debug(f"Office date from {Path(file_path).name}: {parsed}")
                            return parsed
            
            return None
    except Exception as e:
        logger.debug(f"Office date extraction failed for {file_path}: {e}")
        return None


def _get_pdf_date(file_path: str) -> Optional[datetime]:
    """Extract creation date from PDF files."""
    try:
        from PyPDF2 import PdfReader
        
        reader = PdfReader(file_path)
        info = reader.metadata
        
        if info:
            # Try CreationDate first, then ModDate
            for key in ['/CreationDate', '/ModDate']:
                if key in info:
                    date_str = info[key]
                    if date_str:
                        parsed = _parse_pdf_date(date_str)
                        if parsed:
                            logger.debug(f"PDF date from {Path(file_path).name}: {parsed}")
                            return parsed
        
        return None
    except ImportError:
        logger.debug("PyPDF2 not installed, skipping PDF date extraction")
        return None
    except Exception as e:
        logger.debug(f"PDF date extraction failed for {file_path}: {e}")
        return None


def _get_video_date(file_path: str) -> Optional[datetime]:
    """Extract creation date from video files using file metadata."""
    try:
        from PIL import Image
        
        # Some video formats store creation date in a way Pillow can read
        # This is limited but catches some cases
        
        # Try to get date from filename as fallback for videos
        return _get_filename_date(file_path)
    except Exception as e:
        logger.debug(f"Video date extraction failed for {file_path}: {e}")
        return None


def _get_filename_date(file_path: str) -> Optional[datetime]:
    """
    Extract date from filename patterns like:
    - Screenshot 2024-12-29 143022.png
    - 2024-12-29_14-30-22.png
    - IMG_20241229_143022.jpg
    - VID_20241229_143022.mp4
    """
    filename = Path(file_path).stem
    
    patterns = [
        # 2024-12-29 or 2024_12_29
        r'(\d{4})[-_](\d{2})[-_](\d{2})',
        # 20241229 (8 digits)
        r'(\d{4})(\d{2})(\d{2})',
        # Dec 29, 2024
        r'([A-Za-z]{3})\s+(\d{1,2}),?\s+(\d{4})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, filename)
        if match:
            groups = match.groups()
            try:
                if len(groups) == 3 and groups[0].isdigit():
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                    if 1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                        parsed = datetime(year, month, day)
                        logger.debug(f"Filename date from {Path(file_path).name}: {parsed}")
                        return parsed
            except (ValueError, TypeError):
                continue
    
    return None


def _parse_exif_date(date_str: str) -> Optional[datetime]:
    """Parse EXIF date format: YYYY:MM:DD HH:MM:SS"""
    formats = [
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y:%m:%d",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    """Parse ISO format date."""
    try:
        # Handle various ISO formats
        date_str = date_str.strip()
        # Remove timezone info for simplicity
        if 'Z' in date_str:
            date_str = date_str.replace('Z', '')
        if '+' in date_str:
            date_str = date_str.split('+')[0]
        
        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _parse_pdf_date(date_str: str) -> Optional[datetime]:
    """Parse PDF date format: D:YYYYMMDDHHmmSS or similar."""
    try:
        # Remove 'D:' prefix if present
        if date_str.startswith('D:'):
            date_str = date_str[2:]
        
        # Remove timezone info
        date_str = re.sub(r"[Z+']\d*'?", '', date_str)
        
        # Try various lengths
        if len(date_str) >= 14:
            return datetime.strptime(date_str[:14], "%Y%m%d%H%M%S")
        elif len(date_str) >= 8:
            return datetime.strptime(date_str[:8], "%Y%m%d")
    except Exception:
        pass
    return None


def _parse_xmp_date(xmp_data: str) -> Optional[datetime]:
    """Parse date from XMP XML data."""
    try:
        # Look for common date fields in XMP
        patterns = [
            r'<xmp:CreateDate>([^<]+)</xmp:CreateDate>',
            r'<photoshop:DateCreated>([^<]+)</photoshop:DateCreated>',
            r'<exif:DateTimeOriginal>([^<]+)</exif:DateTimeOriginal>',
        ]
        for pattern in patterns:
            match = re.search(pattern, xmp_data)
            if match:
                parsed = _parse_iso_date(match.group(1))
                if parsed:
                    return parsed
    except Exception:
        pass
    return None


def _parse_flexible_date(date_str: str) -> Optional[datetime]:
    """Try multiple date parsing strategies."""
    # Try ISO format first
    parsed = _parse_iso_date(date_str)
    if parsed:
        return parsed
    
    # Try EXIF format
    parsed = _parse_exif_date(date_str)
    if parsed:
        return parsed
    
    # Try common formats
    formats = [
        "%a %b %d %H:%M:%S %Y",  # "Thu Dec 29 14:30:22 2024"
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%B %d, %Y",  # "December 29, 2024"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    
    return None


# Legacy function for backwards compatibility
def get_exif_date(file_path: str) -> Optional[datetime]:
    """Legacy function - use get_file_original_date instead."""
    return get_file_original_date(file_path)

