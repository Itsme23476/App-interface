"""
Smart file categorizer for fallback organization.

Used when AI is not available or for quick categorization.
"""

import os
import mimetypes
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Extension to category mapping
EXTENSION_CATEGORIES: Dict[str, str] = {
    # Images
    '.jpg': 'images',
    '.jpeg': 'images',
    '.png': 'images',
    '.gif': 'images',
    '.bmp': 'images',
    '.webp': 'images',
    '.svg': 'images',
    '.ico': 'images',
    '.tiff': 'images',
    '.tif': 'images',
    '.heic': 'images',
    '.heif': 'images',
    '.raw': 'images',
    '.cr2': 'images',
    '.nef': 'images',
    '.psd': 'images',
    
    # Videos
    '.mp4': 'videos',
    '.mkv': 'videos',
    '.avi': 'videos',
    '.mov': 'videos',
    '.wmv': 'videos',
    '.flv': 'videos',
    '.webm': 'videos',
    '.m4v': 'videos',
    '.mpeg': 'videos',
    '.mpg': 'videos',
    '.3gp': 'videos',
    
    # Audio
    '.mp3': 'audio',
    '.wav': 'audio',
    '.flac': 'audio',
    '.aac': 'audio',
    '.ogg': 'audio',
    '.wma': 'audio',
    '.m4a': 'audio',
    '.aiff': 'audio',
    '.opus': 'audio',
    
    # Documents
    '.pdf': 'documents',
    '.doc': 'documents',
    '.docx': 'documents',
    '.txt': 'documents',
    '.rtf': 'documents',
    '.odt': 'documents',
    '.pages': 'documents',
    '.md': 'documents',
    '.tex': 'documents',
    
    # Spreadsheets
    '.xls': 'spreadsheets',
    '.xlsx': 'spreadsheets',
    '.csv': 'spreadsheets',
    '.ods': 'spreadsheets',
    '.numbers': 'spreadsheets',
    
    # Presentations
    '.ppt': 'presentations',
    '.pptx': 'presentations',
    '.key': 'presentations',
    '.odp': 'presentations',
    
    # Archives
    '.zip': 'archives',
    '.rar': 'archives',
    '.7z': 'archives',
    '.tar': 'archives',
    '.gz': 'archives',
    '.bz2': 'archives',
    '.xz': 'archives',
    '.tgz': 'archives',
    
    # Code
    '.py': 'code',
    '.js': 'code',
    '.ts': 'code',
    '.jsx': 'code',
    '.tsx': 'code',
    '.html': 'code',
    '.css': 'code',
    '.scss': 'code',
    '.less': 'code',
    '.java': 'code',
    '.c': 'code',
    '.cpp': 'code',
    '.h': 'code',
    '.hpp': 'code',
    '.cs': 'code',
    '.go': 'code',
    '.rs': 'code',
    '.rb': 'code',
    '.php': 'code',
    '.swift': 'code',
    '.kt': 'code',
    '.scala': 'code',
    '.r': 'code',
    '.sql': 'code',
    '.sh': 'code',
    '.bat': 'code',
    '.ps1': 'code',
    
    # Data
    '.json': 'data',
    '.xml': 'data',
    '.yaml': 'data',
    '.yml': 'data',
    '.toml': 'data',
    '.ini': 'data',
    '.cfg': 'data',
    '.conf': 'data',
    
    # Executables/Installers
    '.exe': 'installers',
    '.msi': 'installers',
    '.dmg': 'installers',
    '.pkg': 'installers',
    '.deb': 'installers',
    '.rpm': 'installers',
    '.app': 'installers',
    '.apk': 'installers',
    
    # Fonts
    '.ttf': 'fonts',
    '.otf': 'fonts',
    '.woff': 'fonts',
    '.woff2': 'fonts',
    '.eot': 'fonts',
    
    # 3D/CAD
    '.obj': '3d-models',
    '.stl': '3d-models',
    '.fbx': '3d-models',
    '.blend': '3d-models',
    '.dae': '3d-models',
    '.3ds': '3d-models',
    
    # eBooks
    '.epub': 'ebooks',
    '.mobi': 'ebooks',
    '.azw': 'ebooks',
    '.azw3': 'ebooks',
}

# MIME type fallbacks
MIME_CATEGORIES: Dict[str, str] = {
    'image/': 'images',
    'video/': 'videos',
    'audio/': 'audio',
    'text/': 'documents',
    'application/pdf': 'documents',
    'application/zip': 'archives',
    'application/x-rar': 'archives',
    'application/x-7z': 'archives',
}


class SmartCategorizer:
    """
    Smart file categorizer that determines where files should be placed.
    
    Uses extension mapping, MIME types, and optionally AI-generated tags.
    """
    
    def __init__(self):
        self.extension_map = EXTENSION_CATEGORIES.copy()
        self.mime_map = MIME_CATEGORIES.copy()
    
    def get_category(self, file_path: str, tags: Optional[List[str]] = None) -> str:
        """
        Get the category for a file.
        
        Args:
            file_path: Path to the file
            tags: Optional list of AI-generated tags
            
        Returns:
            Category name (folder name)
        """
        file_path = Path(file_path)
        extension = file_path.suffix.lower()
        
        # First, check extension mapping
        if extension in self.extension_map:
            return self.extension_map[extension]
        
        # Try MIME type
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if mime_type:
            for mime_prefix, category in self.mime_map.items():
                if mime_type.startswith(mime_prefix):
                    return category
        
        # Use tags if available
        if tags:
            # Look for category-like tags
            category_keywords = {
                'screenshot': 'screenshots',
                'photo': 'images',
                'picture': 'images',
                'video': 'videos',
                'music': 'audio',
                'song': 'audio',
                'document': 'documents',
                'invoice': 'documents',
                'receipt': 'documents',
                'code': 'code',
                'script': 'code',
            }
            for tag in tags:
                tag_lower = tag.lower()
                for keyword, category in category_keywords.items():
                    if keyword in tag_lower:
                        return category
        
        # Default category
        return 'misc'
    
    def get_destination_path(self, file_path: str, base_folder: str, 
                              tags: Optional[List[str]] = None) -> str:
        """
        Get the full destination path for a file.
        
        Args:
            file_path: Path to the source file
            base_folder: Base folder for organization
            tags: Optional list of AI-generated tags
            
        Returns:
            Full path to destination (including filename)
        """
        category = self.get_category(file_path, tags)
        file_name = os.path.basename(file_path)
        return os.path.join(base_folder, category, file_name)
    
    def should_ignore(self, file_path: str) -> bool:
        """
        Check if a file should be ignored during organization.
        
        Args:
            file_path: Path to the file
            
        Returns:
            True if file should be ignored
        """
        file_name = os.path.basename(file_path)
        
        # System files
        ignore_names = {
            '.DS_Store', 'Thumbs.db', 'desktop.ini', '.gitignore',
            '.git', '__pycache__', 'node_modules',
        }
        
        if file_name in ignore_names:
            return True
        
        # Hidden files
        if file_name.startswith('.'):
            return True
        
        # Temp files
        ignore_extensions = {'.tmp', '.temp', '.crdownload', '.part', '.partial'}
        _, ext = os.path.splitext(file_name.lower())
        if ext in ignore_extensions:
            return True
        
        # Office temp files (start with ~$)
        if file_name.startswith('~$'):
            return True
        
        return False
    
    def categorize_files(self, file_paths: List[str], 
                          file_tags: Optional[Dict[str, List[str]]] = None) -> Dict[str, List[str]]:
        """
        Categorize multiple files into categories.
        
        Args:
            file_paths: List of file paths to categorize
            file_tags: Optional dict of {file_path: [tags]} for AI tags
            
        Returns:
            Dict of {category: [file_paths]}
        """
        file_tags = file_tags or {}
        categories: Dict[str, List[str]] = {}
        
        for file_path in file_paths:
            if self.should_ignore(file_path):
                continue
            
            tags = file_tags.get(file_path, [])
            category = self.get_category(file_path, tags)
            
            if category not in categories:
                categories[category] = []
            categories[category].append(file_path)
        
        return categories


# Global instance
smart_categorizer = SmartCategorizer()
