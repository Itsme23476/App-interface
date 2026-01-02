"""
Application settings and configuration management.
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Any


class Settings:
    """Application settings manager."""
    
    def __init__(self):
        self.app_name = "ai-file-organizer"
        self.category_map = self._load_default_categories()
        self.mime_fallbacks = self._get_mime_fallbacks()
        # AI options
        self.use_openai_fallback: bool = False
        self.openai_api_key: str | None = os.environ.get('OPENAI_API_KEY')
        self.openai_vision_model: str = os.environ.get('OPENAI_VISION_MODEL', 'gpt-4o')
        # Search rerank option (ChatGPT)
        self.use_openai_search_rerank: bool = False
        self.openai_search_model: str = 'gpt-4o-mini'
        # Quick search overlay
        self.use_quick_search: bool = True
        self.quick_search_shortcut: str = 'ctrl+alt+h'
        self.quick_search_autopaste: bool = True
        self.quick_search_auto_confirm: bool = True
        self.quick_search_geometry: Dict[str, int] = {}
        # Theme: 'dark' or 'light'
        self.theme: str = 'dark'
        # Auto-index downloads folder
        self.auto_index_downloads: bool = False
        # Load persisted config if available
        try:
            self._load_config()
        except Exception:
            pass
    
    def _load_default_categories(self) -> Dict[str, List[str]]:
        """Load default category mappings from resources."""
        try:
            # Try to load from resources first
            resource_path = Path(__file__).parent.parent.parent / "resources" / "category_defaults.json"
            if resource_path.exists():
                with open(resource_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        
        # Fallback to hardcoded defaults
        return {
            "Documents/PDFs": [".pdf"],
            "Documents/Word": [".doc", ".docx", ".rtf"],
            "Documents/Text": [".txt", ".md"],
            "Spreadsheets": [".xls", ".xlsx", ".csv"],
            "Presentations": [".ppt", ".pptx"],
            "Images/Photos": [".jpg", ".jpeg"],
            "Images/Screenshots": [".png"],
            "Images/Graphics": [".gif", ".svg", ".webp"],
            "Videos": [".mp4", ".mov"],
            "Audio/Music": [".mp3"],
            "Audio/Recordings": [".wav", ".m4a"],
            "Archives": [".zip", ".rar", ".7z"],
            "Code": [".py", ".js", ".ts"],
            "Misc": []
        }
    
    def _get_mime_fallbacks(self) -> Dict[str, str]:
        """Get MIME type fallback mappings."""
        return {
            "image/": "Images/Photos",
            "video/": "Videos", 
            "audio/": "Audio/Recordings",
            "application/pdf": "Documents/PDFs"
        }
    
    def get_app_data_dir(self) -> Path:
        """Get application data directory."""
        if os.name == 'nt':  # Windows
            app_data = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
        else:  # macOS/Linux
            app_data = Path.home() / '.config'
        
        app_dir = app_data / self.app_name
        app_dir.mkdir(parents=True, exist_ok=True)
        return app_dir
    
    def get_moves_dir(self) -> Path:
        """Get moves log directory."""
        moves_dir = self.get_app_data_dir() / "moves"
        moves_dir.mkdir(parents=True, exist_ok=True)
        return moves_dir

    # Runtime updates from UI
    def set_openai_api_key(self, key: str | None) -> None:
        key = (key or '').strip()
        self.openai_api_key = key if key else None
        if self.openai_api_key:
            os.environ['OPENAI_API_KEY'] = self.openai_api_key
        else:
            try:
                del os.environ['OPENAI_API_KEY']
            except Exception:
                pass
        self._save_config()

    def set_use_openai_fallback(self, use: bool) -> None:
        self.use_openai_fallback = bool(use)
        self._save_config()

    def set_openai_vision_model(self, model: str) -> None:
        model = (model or '').strip() or 'gpt-4o'
        self.openai_vision_model = model
        os.environ['OPENAI_VISION_MODEL'] = model
        self._save_config()

    def delete_openai_api_key(self) -> None:
        self.openai_api_key = None
        try:
            del os.environ['OPENAI_API_KEY']
        except Exception:
            pass
        self._save_config()

    # Persistence helpers
    def _config_file(self) -> Path:
        return self.get_app_data_dir() / 'settings.json'

    def _load_config(self) -> None:
        cfg_file = self._config_file()
        if not cfg_file.exists():
            return
        with open(cfg_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.use_openai_fallback = bool(data.get('use_openai_fallback', self.use_openai_fallback))
        self.use_openai_search_rerank = bool(data.get('use_openai_search_rerank', self.use_openai_search_rerank))
        self.use_quick_search = bool(data.get('use_quick_search', self.use_quick_search))
        k = data.get('openai_api_key')
        if isinstance(k, str) and k.strip():
            self.openai_api_key = k.strip()
            os.environ['OPENAI_API_KEY'] = self.openai_api_key
        m = data.get('openai_vision_model')
        if isinstance(m, str) and m.strip():
            self.openai_vision_model = m.strip()
            os.environ['OPENAI_VISION_MODEL'] = self.openai_vision_model
        sm = data.get('openai_search_model')
        if isinstance(sm, str) and sm.strip():
            self.openai_search_model = sm.strip()
        qs = data.get('quick_search_shortcut')
        if isinstance(qs, str) and qs.strip():
            self.quick_search_shortcut = qs.strip().lower()
        self.quick_search_autopaste = bool(data.get('quick_search_autopaste', self.quick_search_autopaste))
        self.quick_search_auto_confirm = bool(data.get('quick_search_auto_confirm', self.quick_search_auto_confirm))
        qsg = data.get('quick_search_geometry')
        if isinstance(qsg, dict):
            self.quick_search_geometry = {k: int(v) for k, v in qsg.items() if k in {'x','y','w','h'} and isinstance(v, (int, float, str))}
        # Theme
        theme = data.get('theme')
        if theme in ('dark', 'light'):
            self.theme = theme
        # Auto-index downloads
        self.auto_index_downloads = bool(data.get('auto_index_downloads', False))

    def _save_config(self) -> None:
        cfg = {
            'use_openai_fallback': self.use_openai_fallback,
            'openai_api_key': self.openai_api_key or '',
            'openai_vision_model': self.openai_vision_model,
            'use_openai_search_rerank': self.use_openai_search_rerank,
            'openai_search_model': self.openai_search_model,
            'use_quick_search': self.use_quick_search,
            'quick_search_shortcut': self.quick_search_shortcut,
            'quick_search_autopaste': self.quick_search_autopaste,
            'quick_search_auto_confirm': self.quick_search_auto_confirm,
            'quick_search_geometry': self.quick_search_geometry,
            'theme': self.theme,
            'auto_index_downloads': self.auto_index_downloads,
        }
        try:
            with open(self._config_file(), 'w', encoding='utf-8') as f:
                json.dump(cfg, f)
        except Exception:
            pass

    # Search rerank toggle
    def set_use_openai_search_rerank(self, use: bool) -> None:
        self.use_openai_search_rerank = bool(use)
        self._save_config()

    # Quick search setters
    def set_quick_search_shortcut(self, shortcut: str) -> None:
        sc = (shortcut or '').strip().lower() or 'ctrl+x'
        self.quick_search_shortcut = sc
        self._save_config()

    def set_quick_search_autopaste(self, use: bool) -> None:
        self.quick_search_autopaste = bool(use)
        self._save_config()

    def set_quick_search_auto_confirm(self, use: bool) -> None:
        self.quick_search_auto_confirm = bool(use)
        self._save_config()

    def set_theme(self, theme: str) -> None:
        """Set the application theme ('dark' or 'light')."""
        if theme in ('dark', 'light'):
            self.theme = theme
            self._save_config()

    def set_auto_index_downloads(self, enabled: bool) -> None:
        """Enable or disable auto-indexing of Downloads folder."""
        self.auto_index_downloads = bool(enabled)
        self._save_config()


# Global settings instance
settings = Settings()


