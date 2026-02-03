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
        # AI Provider: 'openai' (default, recommended), 'local' (Ollama), or 'none'
        self.ai_provider: str = 'openai'  # OpenAI is now the default - no local setup needed!
        self.openai_api_key: str | None = os.environ.get('OPENAI_API_KEY')
        self.openai_vision_model: str = os.environ.get('OPENAI_VISION_MODEL', 'gpt-4o-mini')  # Cost-effective default
        # Search rerank option (ChatGPT)
        self.use_openai_search_rerank: bool = False
        self.openai_search_model: str = 'gpt-4o-mini'
        # Local AI model settings (Ollama)
        # Qwen 2.5-VL handles BOTH text AND vision in one model
        self.local_model: str = 'qwen2.5vl:3b'
        # Quick search overlay
        self.use_quick_search: bool = True
        self.quick_search_shortcut: str = 'ctrl+alt+h'
        self.quick_search_autopaste: bool = True
        self.quick_search_auto_confirm: bool = True
        self.quick_search_geometry: Dict[str, int] = {}
        # Theme: 'dark' or 'light'
        self.theme: str = 'dark'
        # Auto-index downloads folder (legacy - kept for compatibility)
        self.auto_index_downloads: bool = False
        # Watch for new downloads - common folders (Downloads, Desktop, Documents, etc.)
        self.watch_common_folders: bool = False
        # Watch for new downloads - custom folders list
        self.watch_custom_folders: List[str] = []
        # OCR during indexing (slow - disable for faster indexing)
        self.enable_ocr_indexing: bool = False
        # Search enhancements
        # Single toggle: when enabled, we apply BOTH fuzzy keyword matching + spell correction
        self.enable_spell_check: bool = False
        # Auth tokens (stored securely)
        self.auth_access_token: str = ''
        self.auth_refresh_token: str = ''
        self.auth_user_email: str = ''
        # Auto-organize watcher settings
        # List of {path: str, instruction: str} for each watched folder
        self.auto_organize_folders: List[Dict[str, str]] = []
        # Auto-start watcher when app opens (DEFAULT: True)
        self.auto_organize_auto_start: bool = True
        # Last time watcher was active (ISO timestamp) - for catch-up feature
        self.auto_organize_last_active: str = ''
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

    def set_ai_provider(self, provider: str) -> None:
        """Set the AI provider: 'openai' (default), 'local' (Ollama), or 'none'."""
        if provider in ('openai', 'local', 'none'):
            self.ai_provider = provider
        else:
            self.ai_provider = 'openai'  # Default to OpenAI
        self._save_config()
    
    # Legacy compatibility - maps to ai_provider
    @property
    def use_openai_fallback(self) -> bool:
        """Legacy property - returns True if AI provider is OpenAI."""
        return self.ai_provider == 'openai'
    
    def set_use_openai_fallback(self, use: bool) -> None:
        self.ai_provider = 'openai' if use else 'local'
        self._save_config()

    def set_openai_vision_model(self, model: str) -> None:
        model = (model or '').strip() or 'gpt-4o-mini'
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

    # Local AI model setter
    def set_local_model(self, model: str) -> None:
        """Set the local model for Ollama (Qwen 2.5-VL handles both text and vision)."""
        self.local_model = (model or '').strip() or 'qwen2.5vl:3b'
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
        # AI Provider (migrate from old use_openai_fallback)
        ai_prov = data.get('ai_provider')
        if ai_prov in ('openai', 'local', 'none'):
            self.ai_provider = ai_prov
        elif data.get('use_openai_fallback'):
            # Migrate old setting: if they had OpenAI fallback enabled, keep using OpenAI
            self.ai_provider = 'openai'
        # else keep default 'openai'
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
        # Local AI model (single model for both text and vision)
        lm = data.get('local_model')
        if isinstance(lm, str) and lm.strip():
            # Migrate from 7b to 3b (7b requires too much RAM for most systems)
            loaded_model = lm.strip()
            if loaded_model == 'qwen2.5vl:7b':
                loaded_model = 'qwen2.5vl:3b'  # Auto-migrate to lighter version
            self.local_model = loaded_model
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
        # Auto-index downloads (legacy)
        self.auto_index_downloads = bool(data.get('auto_index_downloads', False))
        # Watch for new downloads - common folders
        self.watch_common_folders = bool(data.get('watch_common_folders', False))
        # Watch for new downloads - custom folders
        self.watch_custom_folders = list(data.get('watch_custom_folders', []))
        # OCR during indexing (disabled by default for speed)
        self.enable_ocr_indexing = bool(data.get('enable_ocr_indexing', False))
        # Search enhancements
        # Migration: previously we had separate enable_fuzzy_search and enable_spell_check.
        # Now a single toggle controls both; treat either legacy flag as enabling spell_check.
        legacy_fuzzy = data.get('enable_fuzzy_search')
        legacy_spell = data.get('enable_spell_check')
        if legacy_spell is not None:
            self.enable_spell_check = bool(legacy_spell)
        elif legacy_fuzzy is not None:
            self.enable_spell_check = bool(legacy_fuzzy)
        else:
            self.enable_spell_check = bool(data.get('enable_spell_check', self.enable_spell_check))
        # Auth tokens
        self.auth_access_token = data.get('auth_access_token', '')
        self.auth_refresh_token = data.get('auth_refresh_token', '')
        self.auth_user_email = data.get('auth_user_email', '')
        # Auto-organize watcher
        self.auto_organize_folders = list(data.get('auto_organize_folders', []))
        self.auto_organize_auto_start = bool(data.get('auto_organize_auto_start', True))
        self.auto_organize_last_active = str(data.get('auto_organize_last_active', ''))

    def _save_config(self) -> None:
        cfg = {
            'ai_provider': self.ai_provider,
            'openai_api_key': self.openai_api_key or '',
            'openai_vision_model': self.openai_vision_model,
            'use_openai_search_rerank': self.use_openai_search_rerank,
            'openai_search_model': self.openai_search_model,
            'local_model': self.local_model,
            'use_quick_search': self.use_quick_search,
            'quick_search_shortcut': self.quick_search_shortcut,
            'quick_search_autopaste': self.quick_search_autopaste,
            'quick_search_auto_confirm': self.quick_search_auto_confirm,
            'quick_search_geometry': self.quick_search_geometry,
            'theme': self.theme,
            'auto_index_downloads': self.auto_index_downloads,
            'watch_common_folders': self.watch_common_folders,
            'watch_custom_folders': self.watch_custom_folders,
            'enable_ocr_indexing': self.enable_ocr_indexing,
            'enable_spell_check': self.enable_spell_check,
            'auth_access_token': self.auth_access_token,
            'auth_refresh_token': self.auth_refresh_token,
            'auth_user_email': self.auth_user_email,
            'auto_organize_folders': self.auto_organize_folders,
            'auto_organize_auto_start': self.auto_organize_auto_start,
            'auto_organize_last_active': self.auto_organize_last_active,
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
    
    def set_watch_common_folders(self, enabled: bool) -> None:
        """Enable or disable watching common folders for new downloads."""
        self.watch_common_folders = bool(enabled)
        self._save_config()
    
    def add_watch_custom_folder(self, folder_path: str) -> None:
        """Add a custom folder to watch for new downloads."""
        if folder_path and folder_path not in self.watch_custom_folders:
            self.watch_custom_folders.append(folder_path)
            self._save_config()
    
    def remove_watch_custom_folder(self, folder_path: str) -> None:
        """Remove a custom folder from the watch list."""
        if folder_path in self.watch_custom_folders:
            self.watch_custom_folders.remove(folder_path)
            self._save_config()

    def set_auth_tokens(self, access_token: str, refresh_token: str, email: str = '') -> None:
        """Store authentication tokens securely."""
        self.auth_access_token = access_token or ''
        self.auth_refresh_token = refresh_token or ''
        self.auth_user_email = email or ''
        self._save_config()

    def clear_auth_tokens(self) -> None:
        """Clear stored authentication tokens (logout)."""
        self.auth_access_token = ''
        self.auth_refresh_token = ''
        self.auth_user_email = ''
        self._save_config()

    def has_stored_session(self) -> bool:
        """Check if we have stored auth tokens."""
        return bool(self.auth_access_token and self.auth_refresh_token)

    def set_enable_spell_check(self, enabled: bool) -> None:
        """Enable or disable typo correction in search (fuzzy + spell check)."""
        self.enable_spell_check = bool(enabled)
        self._save_config()

    # Auto-organize watcher methods
    def add_auto_organize_folder(self, folder_path: str, instruction: str = '') -> None:
        """Add a folder to auto-organize with its instruction."""
        # Check if folder already exists
        for folder in self.auto_organize_folders:
            if folder.get('path') == folder_path:
                folder['instruction'] = instruction
                self._save_config()
                return
        # Add new folder
        self.auto_organize_folders.append({
            'path': folder_path,
            'instruction': instruction
        })
        self._save_config()
    
    def remove_auto_organize_folder(self, folder_path: str) -> None:
        """Remove a folder from auto-organize."""
        self.auto_organize_folders = [
            f for f in self.auto_organize_folders 
            if f.get('path') != folder_path
        ]
        self._save_config()
    
    def update_auto_organize_instruction(self, folder_path: str, instruction: str) -> None:
        """Update the instruction for a specific folder."""
        for folder in self.auto_organize_folders:
            if folder.get('path') == folder_path:
                folder['instruction'] = instruction
                self._save_config()
                return
    
    def set_auto_organize_auto_start(self, enabled: bool) -> None:
        """Enable or disable auto-start of watcher on app open."""
        self.auto_organize_auto_start = bool(enabled)
        self._save_config()
    
    def update_auto_organize_last_active(self) -> None:
        """Update the last active timestamp to now."""
        from datetime import datetime
        self.auto_organize_last_active = datetime.now().isoformat()
        self._save_config()
    
    def get_auto_organize_last_active_time(self):
        """Get the last active time as a datetime object, or None if not set."""
        from datetime import datetime
        if not self.auto_organize_last_active:
            return None
        try:
            return datetime.fromisoformat(self.auto_organize_last_active)
        except Exception:
            return None


# Global settings instance
settings = Settings()


