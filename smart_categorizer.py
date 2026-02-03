"""
Smart file categorization engine.

Provides rule-based and AI-enhanced categorization for automatic file organization.
"""

import os
import re
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Default category rules (file extension -> category folder)
DEFAULT_CATEGORIES = {
    # Images
    ".jpg": "Images",
    ".jpeg": "Images",
    ".png": "Images",
    ".gif": "Images",
    ".bmp": "Images",
    ".webp": "Images",
    ".svg": "Images",
    ".ico": "Images",
    ".tiff": "Images",
    ".heic": "Images",
    
    # Screenshots (detected by name pattern, not extension)
    # Handled separately in categorize_file()
    
    # Documents
    ".pdf": "Documents",
    ".doc": "Documents",
    ".docx": "Documents",
    ".txt": "Documents",
    ".rtf": "Documents",
    ".odt": "Documents",
    ".xls": "Documents",
    ".xlsx": "Documents",
    ".ppt": "Documents",
    ".pptx": "Documents",
    ".csv": "Documents",
    
    # Videos
    ".mp4": "Videos",
    ".mov": "Videos",
    ".avi": "Videos",
    ".mkv": "Videos",
    ".wmv": "Videos",
    ".flv": "Videos",
    ".webm": "Videos",
    ".m4v": "Videos",
    
    # Audio
    ".mp3": "Audio",
    ".wav": "Audio",
    ".flac": "Audio",
    ".aac": "Audio",
    ".ogg": "Audio",
    ".wma": "Audio",
    ".m4a": "Audio",
    
    # Archives
    ".zip": "Archives",
    ".rar": "Archives",
    ".7z": "Archives",
    ".tar": "Archives",
    ".gz": "Archives",
    
    # Code
    ".py": "Code",
    ".js": "Code",
    ".ts": "Code",
    ".html": "Code",
    ".css": "Code",
    ".java": "Code",
    ".cpp": "Code",
    ".c": "Code",
    ".h": "Code",
    ".json": "Code",
    ".xml": "Code",
    ".yaml": "Code",
    ".yml": "Code",
    
    # Executables/Installers
    ".exe": "Programs",
    ".msi": "Programs",
    ".dmg": "Programs",
    ".app": "Programs",
    
    # Fonts
    ".ttf": "Fonts",
    ".otf": "Fonts",
    ".woff": "Fonts",
    ".woff2": "Fonts",
}

# Name patterns for special detection
SCREENSHOT_PATTERNS = [
    r"screenshot",
    r"screen.?shot",
    r"snip",
    r"capture",
    r"screen.?cap",
    r"^ss_",
    r"^sc_",
]

# Files/folders to ignore
IGNORE_PATTERNS = [
    r"^\..*",  # Hidden files (start with .)
    r".*\.tmp$",
    r".*\.temp$",
    r"^~.*",  # Temp files starting with ~
    r"^thumbs\.db$",
    r"^desktop\.ini$",
    r"^\.ds_store$",
    r"^ntuser\..*",
]

# System folders to never touch
SYSTEM_FOLDERS = [
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "$Recycle.Bin",
    "System Volume Information",
    "AppData",
]


class SmartCategorizer:
    """
    Categorizes files into folders based on rules and AI.
    
    Priority:
    1. Name patterns (e.g., screenshot detection)
    2. File extension rules
    3. AI analysis (if enabled and ambiguous)
    4. Fallback to "Other"
    """
    
    def __init__(self, custom_rules: Optional[Dict[str, str]] = None):
        """
        Initialize categorizer with optional custom rules.
        
        Args:
            custom_rules: Dict mapping extensions/patterns to category names
        """
        self.rules = DEFAULT_CATEGORIES.copy()
        if custom_rules:
            self.rules.update(custom_rules)
        
        # Compile patterns for efficiency
        self.screenshot_patterns = [re.compile(p, re.IGNORECASE) for p in SCREENSHOT_PATTERNS]
        self.ignore_patterns = [re.compile(p, re.IGNORECASE) for p in IGNORE_PATTERNS]
    
    def should_ignore(self, file_path: str) -> bool:
        """Check if file should be ignored."""
        filename = os.path.basename(file_path)
        
        for pattern in self.ignore_patterns:
            if pattern.match(filename):
                return True
        
        # Check if in system folder
        path_parts = Path(file_path).parts
        for part in path_parts:
            if part in SYSTEM_FOLDERS:
                return True
        
        return False
    
    def is_screenshot(self, filename: str) -> bool:
        """Check if filename indicates a screenshot."""
        name_lower = filename.lower()
        for pattern in self.screenshot_patterns:
            if pattern.search(name_lower):
                return True
        return False
    
    def categorize_file(self, file_path: str, use_ai: bool = False) -> Tuple[str, str]:
        """
        Categorize a single file.
        
        Args:
            file_path: Path to the file
            use_ai: Whether to use AI for ambiguous files
            
        Returns:
            Tuple of (category_name, confidence) where confidence is:
            - "rule" for rule-based match
            - "pattern" for name pattern match
            - "ai" for AI-determined
            - "fallback" for default category
        """
        filename = os.path.basename(file_path)
        extension = os.path.splitext(filename)[1].lower()
        
        # Check for screenshots first (name-based)
        if self.is_screenshot(filename):
            return ("Screenshots", "pattern")
        
        # Check extension rules
        if extension in self.rules:
            return (self.rules[extension], "rule")
        
        # AI analysis for ambiguous files (if enabled)
        if use_ai:
            category = self._categorize_with_ai(file_path)
            if category:
                return (category, "ai")
        
        # Fallback
        return ("Other", "fallback")
    
    def categorize_batch(self, file_paths: List[str], use_ai: bool = False) -> Dict[str, List[str]]:
        """
        Categorize multiple files.
        
        Args:
            file_paths: List of file paths
            use_ai: Whether to use AI for ambiguous files
            
        Returns:
            Dict mapping category names to lists of file paths
        """
        categories = {}
        
        for file_path in file_paths:
            if self.should_ignore(file_path):
                continue
            
            category, _ = self.categorize_file(file_path, use_ai=use_ai)
            
            if category not in categories:
                categories[category] = []
            categories[category].append(file_path)
        
        return categories
    
    def _categorize_with_ai(self, file_path: str) -> Optional[str]:
        """
        Use AI to categorize an ambiguous file.
        
        This is called when rule-based categorization fails.
        """
        try:
            from app.core.settings import settings
            from openai import OpenAI
            
            if not settings.openai_api_key:
                return None
            
            filename = os.path.basename(file_path)
            extension = os.path.splitext(filename)[1].lower()
            
            # Get available categories
            available_categories = list(set(self.rules.values())) + ["Screenshots", "Other"]
            
            client = OpenAI(api_key=settings.openai_api_key)
            
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": f"""You are a file categorization assistant. 
Given a filename, determine the most appropriate category from this list:
{', '.join(available_categories)}

Respond with ONLY the category name, nothing else."""
                    },
                    {
                        "role": "user",
                        "content": f"Categorize this file: {filename}"
                    }
                ],
                max_tokens=20,
                temperature=0
            )
            
            category = response.choices[0].message.content.strip()
            
            # Validate category
            if category in available_categories:
                logger.info(f"AI categorized {filename} as {category}")
                return category
            
            return None
            
        except Exception as e:
            logger.warning(f"AI categorization failed for {file_path}: {e}")
            return None
    
    def get_destination_path(self, file_path: str, base_folder: str, use_ai: bool = False) -> str:
        """
        Get the full destination path for a file.
        
        Args:
            file_path: Original file path
            base_folder: Base folder where organized files go
            use_ai: Whether to use AI for categorization
            
        Returns:
            Full destination path
        """
        filename = os.path.basename(file_path)
        category, _ = self.categorize_file(file_path, use_ai=use_ai)
        
        dest_folder = os.path.join(base_folder, category)
        dest_path = os.path.join(dest_folder, filename)
        
        # Handle name collisions
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(dest_path):
                new_name = f"{base} ({counter}){ext}"
                dest_path = os.path.join(dest_folder, new_name)
                counter += 1
        
        return dest_path


# Global instance
smart_categorizer = SmartCategorizer()
