"""
Application settings and configuration management.
"""

import copy
import json
import re
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# Test-only keys used on the Parallels dev box (Shift+letter: they'd type a capital letter for everyone else).
# A saved one is put back to the real shortcut when settings load.
_TEST_KEYS = {'shift+a', 'shift+z', 'shift+m'}
DEFAULT_QUICK_SEARCH_SHORTCUT = 'ctrl+alt+h'      # (a new install, the fallback, a dev-box test key: one default)


class Settings:
    """Application settings manager."""
    
    def __init__(self):
        self.app_name = "ai-file-organizer"
        self.category_map = self._load_default_categories()
        self.mime_fallbacks = self._get_mime_fallbacks()
        # AI Provider: 'openai' (default, recommended), 'local' (Ollama), or 'none'
        self.ai_provider: str = 'openai'  # OpenAI is now the default - no local setup needed!
        self.openai_api_key: str | None = os.environ.get('OPENAI_API_KEY')
        self.openai_vision_model: str = 'gpt-6-luna'   # (a label only: the proxy picks the model — vision.FILECT_MODEL)
        # Local AI model settings (Ollama)
        # Qwen 2.5-VL handles BOTH text AND vision in one model
        self.local_model: str = 'qwen2.5vl:3b'
        # Quick search overlay
        self.use_quick_search: bool = True
        self.quick_search_shortcut: str = DEFAULT_QUICK_SEARCH_SHORTCUT
        self.quick_search_autopaste: bool = True
        self.quick_search_auto_confirm: bool = True
        self.quick_search_geometry: Dict[str, int] = {}
        # Theme: 'dark' or 'light'
        self.theme: str = 'light'
        self._theme_explicitly_set: bool = False
        # Auto-index downloads folder (legacy - kept for compatibility)
        self.auto_index_downloads: bool = False
        # Watch for new downloads - common folders (Downloads, Desktop, Documents, etc.)
        self.watch_common_folders: bool = False
        # Watch for new downloads - custom folders list (paths normalised via str(Path()))
        self.watch_custom_folders: List[str] = []
        # Per-folder custom AI instructions for watched custom folders.
        # Keyed by the SAME normalised path string used in watch_custom_folders.
        self.watch_folder_instructions: Dict[str, str] = {}
        # OCR during indexing (slow - disable for faster indexing)
        self.enable_ocr_indexing: bool = False
        # Search enhancements
        # Single toggle: when enabled, we apply BOTH fuzzy keyword matching + spell correction
        self.enable_spell_check: bool = False
        # Auth tokens (stored securely)
        self.auth_access_token: str = ''
        self.auth_refresh_token: str = ''
        self.auth_user_email: str = ''

        # ======= VOICE / DICTATION SETTINGS =======
        # Language for transcription ('' = auto-detect; else an ISO code like 'en').
        self.dictation_language: str = ''
        # Custom Words — names/jargon biased to spell correctly during transcription.
        self.dictation_custom_terms: List[str] = []
        # Mute other audio output while actively recording the mic.
        self.dictation_mute_while_recording: bool = False
        # AI Cleanup level applied to a finished transcript: 'none' | 'light' | 'polished'.
        self.dictation_polish_level: str = 'none'
        # Voice > Custom Words: the last field the user asked the suggester about (prefilled).
        self.dictation_last_niche: str = ''

        # ======= AGENTS (local Claude Code / Codex) — keys copied verbatim from the Mac app =======
        self.assistant_bypass: bool = False         # "Full access": run without Approve/Deny ("Ask first" = False)
        self.assistant_model: str = ''              # '' = the UI default (opus), else a value from Claude's list
        self.assistant_effort: str = ''             # '' = the UI default (high), else low/medium/high/xhigh/max
        self.assistant_agent: str = 'claude'        # primary agent: 'claude' or 'codex'
        self.assistant_codex_model: str = ''        # '' = Codex's top model
        self.assistant_codex_effort: str = 'high'
        self.assistant_codex_close_app: bool = False
        self.assistant_claude_open_in: str = 'app'  # "Open in …": 'app' (desktop app) or 'terminal'
        self.assistant_codex_open_in: str = 'app'
        self.assistant_auto_open: bool = True       # the top card opens by itself for each task + answer
        self.assistant_router_model: str = 'opus'   # model for the voice intent router
        self.agents_list_collapsed: bool = True     # Agents page: the chat list starts hidden for a new user
        self.agents_tour_seen: bool = False         # Agents page: "See what it can do" stops glowing once opened
        self.agent_was_ready: bool = False          # Claude Code or Codex was set up at the last check (the first-run
                                                    # guide's Assistant screen uses it before the next check is back)
        self.page_tours_seen: list = []             # the other pages' "See what it can do" tours opened once
        # ======= Filect Assistant (hands-free voice) — keys + defaults copied verbatim from the Mac app =======
        self.live_send_after_sec: float = 2.0       # send after you've stopped talking this long (1–10 s)
        self.live_sensitivity: str = 'medium'       # 'low' (ignores more) | 'medium' | 'high' (picks up softer)
        self.live_chime: bool = True                # a short sound when something you said becomes a task
        self.live_noise_filter: bool = True         # (Mac: voice processing. Windows has none in Qt — kept for the file)
        self.live_speak: bool = False               # Speak replies — OFF by default
        self.live_voice: str = 'sal'                # Speak replies' voice (speech_out.VOICES; xAI voice id)
        self.live_organize_preview: bool = True     # "organize my Downloads": show the plan first (Off: moves at once)
        self.live_tour_seen: bool = False          # Assistant page: its tour button stands out until opened once
        self.live_settings_collapsed: bool = False  # Assistant page: the Settings panel (right) hidden
        # Hold to talk to the agent from anywhere: hold Ctrl + Alt (the owner's pick, 2026-11-12 — the Mac's
        # fn + ⌃ has no Windows twin). agent_hotkey watches it (left Alt only, alone, after 0.3 s).
        self.assistant_voice_shortcut: str = 'ctrl+alt'

        # ======= AUTO-ORGANIZE WATCHER SETTINGS =======
        # List of folders with per-folder instructions: [{path: str, instruction: str}, ...]
        self.auto_organize_folders: List[Dict[str, str]] = []
        # Auto-start watcher when app opens (default True)
        self.auto_organize_auto_start: bool = True
        # User explicitly stopped the watcher and we should NOT auto-start it
        # back up — across tab switches, dialog Saves, or app restarts.
        # Cleared again when the user clicks Start.
        self.auto_organize_paused: bool = False
        # Last active timestamp (ISO format) for catch-up feature
        self.auto_organize_last_active: str = ''
        
        # ======= EXCLUSIONS SETTINGS =======
        # Patterns to exclude from organization (folders, files, wildcards)
        self.exclusion_patterns: List[str] = self._get_default_exclusions()
        
        # ======= PINNED FILES SETTINGS =======
        # Specific file/folder paths that are "pinned" and should never be organized
        self.pinned_paths: List[str] = []
        
        # ======= ONBOARDING SETTINGS =======
        # Whether the user has completed the onboarding tour
        self.has_completed_onboarding: bool = False
        # How many times user clicked "Remind Me Later" (stop after 3)
        self.onboarding_remind_count: int = 0
        # List of contextual tip IDs that have been seen/dismissed
        self.seen_tips: List[str] = []
        
        # Load persisted config if available
        self._loaded_cfg: Dict[str, Any] = {}
        try:
            self._load_config()
        except Exception:
            pass
        # Snapshot (deep copy) of what we synced from disk. _save_config writes only the keys THIS
        # process actually changed and merges the rest from disk, so two processes sharing
        # settings.json (e.g. the app + a background helper) no longer clobber each other's writes.
        self._loaded_cfg = copy.deepcopy(self._cfg_dict())
    
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
    
    def _get_default_exclusions(self) -> List[str]:
        """Get default exclusion patterns for files/folders that should not be organized."""
        return [
            # Version control
            ".git",
            ".svn",
            ".hg",
            # Dependencies & packages
            "node_modules",
            "vendor",
            "packages",
            # Python
            "__pycache__",
            "venv",
            ".venv",
            "*.pyc",
            ".eggs",
            "*.egg-info",
            # Build & cache
            "build",
            "dist",
            ".cache",
            ".tox",
            # Config files
            ".env",
            ".env.*",
            # IDE & editors
            ".vscode",
            ".idea",
            "*.sublime-*",
            # System files
            "Thumbs.db",
            ".DS_Store",
            "desktop.ini",
            # Temp files
            "*.tmp",
            "*.temp",
            "~$*",
        ]
    
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
        model = (model or '').strip() or 'gpt-6-luna'
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
        self.use_quick_search = bool(data.get('use_quick_search', self.use_quick_search))
        k = data.get('openai_api_key')
        if isinstance(k, str) and k.strip():
            self.openai_api_key = k.strip()
            os.environ['OPENAI_API_KEY'] = self.openai_api_key
        m = data.get('openai_vision_model')
        if isinstance(m, str) and m.strip():
            self.openai_vision_model = m.strip()
            os.environ['OPENAI_VISION_MODEL'] = self.openai_vision_model
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
        if self.quick_search_shortcut in _TEST_KEYS:          # (a dev-box test key, never a real one)
            self.quick_search_shortcut = DEFAULT_QUICK_SEARCH_SHORTCUT
        self.quick_search_autopaste = bool(data.get('quick_search_autopaste', self.quick_search_autopaste))
        self.quick_search_auto_confirm = bool(data.get('quick_search_auto_confirm', self.quick_search_auto_confirm))
        qsg = data.get('quick_search_geometry')
        if isinstance(qsg, dict):
            self.quick_search_geometry = {k: int(v) for k, v in qsg.items() if k in {'x','y','w','h'} and isinstance(v, (int, float, str))}
        # Theme — migrate existing users to light if they never explicitly chose dark
        theme = data.get('theme')
        if theme in ('dark', 'light'):
            if theme == 'dark' and not data.get('theme_explicitly_set', False):
                self.theme = 'light'  # one-time migration: default changed to light
            else:
                self.theme = theme
        # Auto-index downloads (legacy)
        self.auto_index_downloads = bool(data.get('auto_index_downloads', False))
        # Watch for new downloads - common folders
        self.watch_common_folders = bool(data.get('watch_common_folders', False))
        # Watch for new downloads - custom folders. Normalise every stored
        # path through str(Path()) so old forward-slash entries (from
        # QFileDialog) migrate to the OS-native form the watcher uses,
        # keeping settings / watcher / instructions all keyed identically.
        self.watch_custom_folders = [str(Path(p)) for p in data.get('watch_custom_folders', [])]
        # Per-folder instructions, re-keyed to normalised paths on load.
        _raw_instr = dict(data.get('watch_folder_instructions', {}))
        self.watch_folder_instructions = {str(Path(k)): v for k, v in _raw_instr.items()}
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

        # Voice / dictation settings
        self.dictation_language = data.get('dictation_language', self.dictation_language)
        _terms = data.get('dictation_custom_terms', self.dictation_custom_terms)
        self.dictation_custom_terms = [str(t) for t in _terms] if isinstance(_terms, list) else []
        self.dictation_mute_while_recording = bool(data.get('dictation_mute_while_recording', self.dictation_mute_while_recording))
        _lvl = data.get('dictation_polish_level', self.dictation_polish_level)
        self.dictation_polish_level = _lvl if _lvl in ('none', 'light', 'polished') else 'none'
        ln = data.get('dictation_last_niche')
        if isinstance(ln, str):
            self.dictation_last_niche = ln.strip()

        # Auto-organize watcher settings
        auto_folders = data.get('auto_organize_folders', [])
        if isinstance(auto_folders, list):
            self.auto_organize_folders = auto_folders
        self.auto_organize_auto_start = bool(data.get('auto_organize_auto_start', True))
        self.auto_organize_paused = bool(data.get('auto_organize_paused', False))
        self.auto_organize_last_active = data.get('auto_organize_last_active', '')
        
        # Exclusion patterns
        exclusions = data.get('exclusion_patterns')
        if isinstance(exclusions, list):
            self.exclusion_patterns = exclusions
        # If not in config, keep defaults (already set in __init__)
        
        # Pinned paths
        pinned = data.get('pinned_paths')
        if isinstance(pinned, list):
            self.pinned_paths = pinned
        
        # Onboarding
        # Agents (same rules as the Mac loader)
        self.assistant_bypass = bool(data.get('assistant_bypass', self.assistant_bypass))
        for _k in ('assistant_model', 'assistant_effort', 'assistant_codex_model', 'assistant_codex_effort'):
            if isinstance(data.get(_k), str):
                setattr(self, _k, data[_k].strip())
        vs = data.get('assistant_voice_shortcut')
        if isinstance(vs, str) and vs.strip():
            self.assistant_voice_shortcut = vs.strip().lower()
        if self.assistant_voice_shortcut in _TEST_KEYS:       # (Shift+Z was the dev box's test key)
            self.assistant_voice_shortcut = 'ctrl+alt'
        rm = data.get('assistant_router_model')
        if isinstance(rm, str) and rm.strip():
            self.assistant_router_model = rm.strip()
        if data.get('assistant_agent') in ('claude', 'codex'):
            self.assistant_agent = data['assistant_agent']
        for _k in ('assistant_claude_open_in', 'assistant_codex_open_in'):
            if data.get(_k) in ('app', 'terminal'):
                setattr(self, _k, data[_k])
        for _k in ('assistant_codex_close_app', 'assistant_auto_open', 'agents_list_collapsed', 'agents_tour_seen',
                   'agent_was_ready'):
            if isinstance(data.get(_k), bool):
                setattr(self, _k, data[_k])
        if isinstance(data.get('page_tours_seen'), list):
            self.page_tours_seen = [x for x in data['page_tours_seen'] if isinstance(x, str)]
        try:                                         # Filect Assistant (hands-free)
            if data.get('live_send_after_sec') is not None:
                self.live_send_after_sec = min(10.0, max(1.0, float(data['live_send_after_sec'])))
        except (TypeError, ValueError):
            pass
        if data.get('live_sensitivity') in ('low', 'medium', 'high'):
            self.live_sensitivity = data['live_sensitivity']
        if isinstance(data.get('live_voice'), str) and re.fullmatch(r'[a-z]{2,16}', data['live_voice']):
            self.live_voice = data['live_voice']
        for _k in ('live_chime', 'live_noise_filter', 'live_speak', 'live_organize_preview', 'live_tour_seen',
                   'live_settings_collapsed'):
            if isinstance(data.get(_k), bool):
                setattr(self, _k, data[_k])
        self.has_completed_onboarding = bool(data.get('has_completed_onboarding', False))
        self.onboarding_remind_count = int(data.get('onboarding_remind_count', 0))
        self.seen_tips = list(data.get('seen_tips', []))

    def _cfg_dict(self) -> Dict[str, Any]:
        """The full settings as they currently stand in memory (one dict, ready to persist)."""
        return {
            'ai_provider': self.ai_provider,
            'openai_api_key': self.openai_api_key or '',
            'openai_vision_model': self.openai_vision_model,
            'local_model': self.local_model,
            'use_quick_search': self.use_quick_search,
            'quick_search_shortcut': self.quick_search_shortcut,
            'quick_search_autopaste': self.quick_search_autopaste,
            'quick_search_auto_confirm': self.quick_search_auto_confirm,
            'quick_search_geometry': self.quick_search_geometry,
            'theme': self.theme,
            'theme_explicitly_set': getattr(self, '_theme_explicitly_set', False),
            'auto_index_downloads': self.auto_index_downloads,
            'watch_common_folders': self.watch_common_folders,
            'watch_custom_folders': self.watch_custom_folders,
            'watch_folder_instructions': self.watch_folder_instructions,
            'enable_ocr_indexing': self.enable_ocr_indexing,
            'enable_spell_check': self.enable_spell_check,
            'auth_access_token': self.auth_access_token,
            'auth_refresh_token': self.auth_refresh_token,
            'auth_user_email': self.auth_user_email,
            # Voice / dictation settings
            'dictation_language': self.dictation_language,
            'dictation_custom_terms': self.dictation_custom_terms,
            'dictation_mute_while_recording': self.dictation_mute_while_recording,
            'dictation_polish_level': self.dictation_polish_level,
            'dictation_last_niche': self.dictation_last_niche,
            # Agents
            'assistant_bypass': self.assistant_bypass,
            'assistant_model': self.assistant_model,
            'assistant_effort': self.assistant_effort,
            'assistant_agent': self.assistant_agent,
            'assistant_codex_model': self.assistant_codex_model,
            'assistant_codex_effort': self.assistant_codex_effort,
            'assistant_codex_close_app': self.assistant_codex_close_app,
            'assistant_claude_open_in': self.assistant_claude_open_in,
            'assistant_codex_open_in': self.assistant_codex_open_in,
            'assistant_auto_open': self.assistant_auto_open,
            'assistant_router_model': self.assistant_router_model,
            'agents_list_collapsed': self.agents_list_collapsed,
            'agents_tour_seen': self.agents_tour_seen,
            'agent_was_ready': self.agent_was_ready,
            'page_tours_seen': list(self.page_tours_seen),
            'assistant_voice_shortcut': self.assistant_voice_shortcut,
            # Filect Assistant (hands-free)
            'live_send_after_sec': self.live_send_after_sec,
            'live_sensitivity': self.live_sensitivity,
            'live_chime': self.live_chime,
            'live_noise_filter': self.live_noise_filter,
            'live_speak': self.live_speak,
            'live_organize_preview': self.live_organize_preview,
            'live_voice': self.live_voice,
            'live_tour_seen': self.live_tour_seen,
            'live_settings_collapsed': self.live_settings_collapsed,
            # Auto-organize watcher settings
            'auto_organize_folders': self.auto_organize_folders,
            'auto_organize_auto_start': self.auto_organize_auto_start,
            'auto_organize_paused': self.auto_organize_paused,
            'auto_organize_last_active': self.auto_organize_last_active,
            # Exclusion patterns
            'exclusion_patterns': self.exclusion_patterns,
            # Pinned paths
            'pinned_paths': self.pinned_paths,
            # Onboarding
            'has_completed_onboarding': self.has_completed_onboarding,
            'onboarding_remind_count': self.onboarding_remind_count,
            'seen_tips': self.seen_tips,
        }

    def _config_lock(self):
        """Cross-process exclusive lock around the read-merge-write. On Windows it uses msvcrt
        byte-range locking; on POSIX it uses fcntl.flock. Best-effort — degrades to a no-op if
        neither is available, so a lock problem never blocks persisting."""
        import contextlib

        @contextlib.contextmanager
        def _lock():
            f = None
            locked = False
            lockpath = str(self._config_file()) + '.lock'
            try:
                f = open(lockpath, 'a+')
                try:
                    f.seek(0, os.SEEK_END)
                    if f.tell() == 0:
                        f.write(' ')
                        f.flush()
                except Exception:
                    pass
                f.seek(0)
                try:
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    locked = True
                except ImportError:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX)
                    locked = True
            except Exception:
                locked = False
            try:
                yield
            finally:
                if f is not None:
                    if locked:
                        try:
                            import msvcrt
                            f.seek(0)
                            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                        except ImportError:
                            try:
                                import fcntl
                                fcntl.flock(f, fcntl.LOCK_UN)
                            except Exception:
                                pass
                        except Exception:
                            pass
                    try:
                        f.close()
                    except Exception:
                        pass
        return _lock()

    def _read_disk_cfg(self) -> Dict[str, Any]:
        try:
            with open(self._config_file(), 'r', encoding='utf-8') as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        import tempfile
        path = self._config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.settings-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise

    def _save_config(self) -> None:
        # Write only the keys THIS process actually changed since its last sync; merge everything
        # else from the current on-disk file. Prevents two processes sharing settings.json from
        # clobbering each other's writes. Locked + atomic so it's race-safe.
        mem = self._cfg_dict()
        base = getattr(self, '_loaded_cfg', None) or {}
        try:
            with self._config_lock():
                merged = dict(self._read_disk_cfg())   # start from the latest on disk
                for k, v in mem.items():
                    if v != base.get(k):               # changed here → our value wins
                        merged[k] = v
                    elif k not in merged:              # not persisted yet → seed it
                        merged[k] = v
                    # else: keep disk's value (may be another process's change)
                self._atomic_write(merged)
                self._loaded_cfg = copy.deepcopy(merged)
        except Exception:
            # Best-effort fallback so we never lose the ability to persist at all.
            try:
                self._atomic_write(mem)
                self._loaded_cfg = copy.deepcopy(mem)
            except Exception:
                pass

    # Quick search setters
    def set_quick_search_shortcut(self, shortcut: str) -> None:
        sc = (shortcut or '').strip().lower() or DEFAULT_QUICK_SEARCH_SHORTCUT   # (was 'ctrl+x': Cut, in every app)
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
            self._theme_explicitly_set = True
            self._save_config()

    def set_auto_index_downloads(self, enabled: bool) -> None:
        """Enable or disable auto-indexing of Downloads folder."""
        self.auto_index_downloads = bool(enabled)
        self._save_config()
    
    def set_watch_common_folders(self, enabled: bool) -> None:
        """Enable or disable watching common folders for new downloads."""
        self.watch_common_folders = bool(enabled)
        self._save_config()
    
    def add_watch_custom_folder(self, folder_path: str, instruction: str = None) -> None:
        """Add a custom folder to watch, with optional per-folder AI
        instruction. Paths are normalised so settings / watcher /
        instructions stay keyed identically (see watch_custom_folders)."""
        if not folder_path:
            return
        norm = str(Path(folder_path))
        if norm not in self.watch_custom_folders:
            self.watch_custom_folders.append(norm)
        if instruction:
            self.watch_folder_instructions[norm] = instruction
        self._save_config()

    def remove_watch_custom_folder(self, folder_path: str) -> None:
        """Remove a custom folder (and its instruction) from the watch list."""
        norm = str(Path(folder_path))
        removed = False
        if norm in self.watch_custom_folders:
            self.watch_custom_folders.remove(norm)
            removed = True
        if norm in self.watch_folder_instructions:
            del self.watch_folder_instructions[norm]
            removed = True
        # Also try the raw (un-normalised) form in case an old forward-slash
        # entry slipped through before migration.
        if folder_path in self.watch_custom_folders:
            self.watch_custom_folders.remove(folder_path)
            removed = True
        if removed:
            self._save_config()

    def set_watch_folder_instruction(self, folder_path: str, instruction: str) -> None:
        """Set (or clear, if empty) the AI instruction for a watched folder."""
        norm = str(Path(folder_path))
        if instruction:
            self.watch_folder_instructions[norm] = instruction
        else:
            self.watch_folder_instructions.pop(norm, None)
        self._save_config()

    def get_watch_folder_instruction(self, folder_path: str) -> str:
        """Return the AI instruction for a watched folder, or '' if none."""
        return self.watch_folder_instructions.get(str(Path(folder_path)), '')

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

    def live_dictation_custom_terms(self) -> List[str]:
        """Dictation custom terms, read FRESH from disk so words added by another process (the MCP
        server) — or in a different app run — take effect WITHOUT restarting the app. Keeps the
        in-memory list in sync; falls back to it on any error."""
        try:
            disk = self._read_disk_cfg()
            terms = disk.get('dictation_custom_terms')
            if isinstance(terms, list):
                self.dictation_custom_terms = [str(t) for t in terms]
        except Exception:
            pass
        return self.dictation_custom_terms

    def add_custom_terms(self, terms) -> int:
        """Merge new Custom Words into dictation_custom_terms (case-insensitive dedup) and
        persist. Capped at 100 — Grok's key-term biasing limit per request. Returns the
        number actually added."""
        self.live_dictation_custom_terms()    # start from what's on disk (the MCP may have added some)
        existing = {t.lower() for t in self.dictation_custom_terms}
        added = 0
        for t in (terms or []):
            t = str(t).strip()
            if not t or len(t) > 50 or t.lower() in existing:
                continue
            if len(self.dictation_custom_terms) >= 100:
                break
            self.dictation_custom_terms.append(t)
            existing.add(t.lower())
            added += 1
        if added:
            self._save_config()
        return added

    def has_stored_session(self) -> bool:
        """Check if we have stored auth tokens."""
        return bool(self.auth_access_token and self.auth_refresh_token)

    def set_enable_spell_check(self, enabled: bool) -> None:
        """Enable or disable typo correction in search (fuzzy + spell check)."""
        self.enable_spell_check = bool(enabled)
        self._save_config()

    # ======= AUTO-ORGANIZE WATCHER METHODS =======
    
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
        """Update instruction for a folder."""
        for folder in self.auto_organize_folders:
            if folder.get('path') == folder_path:
                folder['instruction'] = instruction
                self._save_config()
                return

    def update_auto_organize_action(self, folder_path: str, action: int) -> None:
        """Update (or insert) the organization action for a folder.

        If the folder isn't registered yet in auto_organize_folders, it is
        created on the spot. This matters when the user picks a per-folder
        action via the Options button *before* clicking the main Save — the
        choice would otherwise be silently dropped because the folder didn't
        yet exist in settings.

        Args:
            folder_path: Path to the folder
            action: 1=Re-organize All, 2=Organize As-Is, 3=Watch Only
        """
        for folder in self.auto_organize_folders:
            if folder.get('path') == folder_path:
                folder['action'] = action
                self._save_config()
                return
        # Folder not registered yet — create it with the chosen action so the
        # user's pick is persisted immediately. Instruction stays empty until
        # the main Save flow writes it.
        self.auto_organize_folders.append({
            'path': folder_path,
            'instruction': '',
            'action': action,
        })
        self._save_config()
    
    def get_auto_organize_action(self, folder_path: str) -> int:
        """Get the organization action for a folder.
        
        Returns:
            action: 1=Re-organize All, 2=Organize As-Is, 3=Watch Only (default)
        """
        for folder in self.auto_organize_folders:
            if folder.get('path') == folder_path:
                return folder.get('action', 3)  # Default to Watch Only
        return 3

    # Per-folder flags (set up by voice). Not the global `auto_organize_paused` above (the user's Stop):
    # `paused` = this one folder was left off when only another folder was turned on by voice.
    @staticmethod
    def _norm_folder(path: str) -> str:
        """A folder as Windows compares it: no case, one kind of slash, no trailing slash."""
        return os.path.normcase(os.path.normpath(str(path or ''))) if path else ''

    def _auto_organize_entry(self, folder_path: str) -> Optional[Dict[str, Any]]:
        wanted = self._norm_folder(folder_path)
        if not wanted:
            return None
        for folder in self.auto_organize_folders:
            if isinstance(folder, dict) and self._norm_folder(folder.get('path', '')) == wanted:
                return folder
        return None

    def set_auto_organize_flags(self, folder_path: str, **flags) -> None:
        """Set `paused` (left off when only another folder was turned on) / `skip_known` on one folder."""
        entry = self._auto_organize_entry(folder_path)
        if entry is not None:
            entry.update(flags)
            self._save_config()

    def auto_organize_only_new(self, folder_path: str) -> bool:
        """Set up by voice as "only the folders already there": the files already in it stay where they are, the
        same as "only new files" (Mac fix 6). The Organize tab's As-Is still sorts what's there."""
        entry = self._auto_organize_entry(folder_path)
        return bool(entry and entry.get('only_new'))

    def auto_organize_skip_known(self, folder_path: str) -> bool:
        """True once a day's auto-organize was reverted here: files Filect already knows stay where they are."""
        entry = self._auto_organize_entry(folder_path)
        return bool(entry and entry.get('skip_known'))

    def auto_organize_watching_since(self, folder_path: str) -> float:
        """When this folder was turned on (0 if unknown). Anything that arrives after that is new, even while Filect is closed."""
        entry = self._auto_organize_entry(folder_path)
        try:
            return float((entry or {}).get('watching_since') or 0)
        except (TypeError, ValueError):
            return 0.0

    def clear_auto_organize_paused(self) -> None:
        """Pressing Start means "run my whole list"."""
        changed = False
        for folder in self.auto_organize_folders:
            if isinstance(folder, dict) and folder.pop('paused', None) is not None:
                changed = True
        if changed:
            self._save_config()

    def mark_auto_organize_skip_known(self, paths) -> None:
        """Files were just put back: any auto-organized folder holding them must not sort them again."""
        wanted = [self._norm_folder(p) for p in paths if p]
        changed = False
        for folder in self.auto_organize_folders:
            if not isinstance(folder, dict):
                continue
            base = self._norm_folder(folder.get('path', ''))
            if not base:
                continue
            root = base.rstrip('\\/') + os.sep
            if not folder.get('skip_known') and any(p.startswith(root) for p in wanted):
                folder['skip_known'] = True
                changed = True
        if changed:
            self._save_config()

    def set_auto_organize_paused(self, paused: bool) -> None:
        """Mark the watcher as user-paused (or un-pause it). Persisted across
        tab switches, dialog saves, and app restarts.
        """
        self.auto_organize_paused = bool(paused)
        self._save_config()

    def set_auto_organize_auto_start(self, enabled: bool) -> None:
        """Enable or disable auto-start of watcher on app open."""
        self.auto_organize_auto_start = bool(enabled)
        self._save_config()

    def update_auto_organize_last_active(self) -> None:
        """Update the last active timestamp to now."""
        self.auto_organize_last_active = datetime.now().isoformat()
        self._save_config()

    def get_auto_organize_last_active_time(self) -> Optional[datetime]:
        """Get the last active time as a datetime object, or None if not set."""
        if not self.auto_organize_last_active:
            return None
        try:
            return datetime.fromisoformat(self.auto_organize_last_active)
        except Exception:
            return None

    def clear_auto_organize_last_active(self) -> None:
        """Clear the last active timestamp."""
        self.auto_organize_last_active = ''
        self._save_config()

    # ======= EXCLUSION METHODS =======
    
    def add_exclusion_pattern(self, pattern: str) -> None:
        """Add an exclusion pattern."""
        pattern = pattern.strip()
        if pattern and pattern not in self.exclusion_patterns:
            self.exclusion_patterns.append(pattern)
            self._save_config()
    
    def remove_exclusion_pattern(self, pattern: str) -> None:
        """Remove an exclusion pattern."""
        if pattern in self.exclusion_patterns:
            self.exclusion_patterns.remove(pattern)
            self._save_config()
    
    def reset_exclusions_to_defaults(self) -> None:
        """Reset exclusion patterns to defaults."""
        self.exclusion_patterns = self._get_default_exclusions()
        self._save_config()
    
    def should_exclude(self, file_path: str) -> bool:
        """Check if a file/folder should be excluded based on patterns or if pinned.
        
        Supports:
        - Exact folder/file names: "node_modules", ".git"
        - Wildcards: "*.pyc", "*.tmp", "~$*"
        - Prefix wildcards: ".env.*"
        - File extensions: ".json" is automatically treated as "*.json"
        - Pinned paths: specific files/folders that are protected
        """
        import logging
        logger = logging.getLogger(__name__)

        # First check if the path is pinned (protected)
        if self.is_pinned(file_path):
            logger.debug(f"Excluding pinned path: {file_path}")
            return True

        # The same three checks as _should_exclude_by_patterns below, with every pattern compiled into one regex
        # per check (cached until the list changes). Measured 2026-11-09: ~250 patterns one by one took ~2.5 ms
        # a file — 20,000 files ≈ 50 s, on the UI thread when auto-organize started.
        name_re, path_re, part_re = self._exclusion_matchers()
        nc = os.path.normcase
        name = os.path.basename(file_path)
        if name_re is not None and name_re.match(nc(name.lower())):
            logger.debug(f"Excluding {name} - matched an exclusion pattern")
            return True
        if path_re is not None:
            if path_re.match(nc(file_path.lower())):
                logger.debug(f"Excluding {name} - path matched an exclusion pattern")
                return True
            for part in file_path.replace('\\', '/').split('/'):
                if part_re.match(nc(part.lower())):
                    logger.debug(f"Excluding {name} - path component '{part}' matched an exclusion pattern")
                    return True
        return False

    def _exclusion_matchers(self):
        """(name, full path, path component) regexes for exclusion_patterns — exactly what fnmatch.fnmatch does
        for each pattern (normcase both sides, then translate), joined with |. Rebuilt when the list changes."""
        import fnmatch
        import re
        key = tuple(self.exclusion_patterns or ())
        cached = getattr(self, "_exclusion_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        nc = os.path.normcase
        names, paths = [], []
        for pattern in key:
            p = pattern.lower()
            names.append(fnmatch.translate(nc(p)))
            if p.startswith('.') and not p.startswith('.*') and '*' not in p:
                names.append(fnmatch.translate(nc('*' + p)))          # ".json" means "*.json"
            # (No skip for short dot-patterns any more: ".git" / ".svn" / ".hg" / ".venv" were treated as extensions
            # and so never matched the FOLDER - files inside .git were organized and indexed. Mac 2026-11-12. An
            # extension pattern like ".json" can never equal a whole folder name, so it still acts as an extension.)
            paths.append(p)
        join = lambda parts: re.compile("|".join(f"(?:{x})" for x in parts)) if parts else None
        name_re = join(names)
        # full path: "*/<p>" or "*\<p>" (the same once normcased on Windows); a component: "<p>" alone
        path_re = join([fnmatch.translate(nc(f"*{sep}{p}")) for p in paths for sep in ("/", "\\")])
        part_re = join([fnmatch.translate(nc(p)) for p in paths])
        self._exclusion_cache = (key, (name_re, path_re, part_re))
        return name_re, path_re, part_re

    def _should_exclude_by_patterns(self, file_path: str) -> bool:
        """The original pattern-by-pattern check (kept as the reference the compiled one is tested against)."""
        import fnmatch
        import logging
        logger = logging.getLogger(__name__)
        name = os.path.basename(file_path)
        name_lower = name.lower()
        file_path_lower = file_path.lower()

        for pattern in self.exclusion_patterns:
            pattern_lower = pattern.lower()
            
            # Check if pattern matches the name directly
            if fnmatch.fnmatch(name_lower, pattern_lower):
                logger.debug(f"Excluding {name} - matched pattern '{pattern}' directly")
                return True
            
            # Handle patterns like ".json" - treat as "*.json" for file extensions
            # This makes it user-friendly (no need to type the asterisk)
            if pattern_lower.startswith('.') and not pattern_lower.startswith('.*') and '*' not in pattern_lower:
                ext_pattern = '*' + pattern_lower
                if fnmatch.fnmatch(name_lower, ext_pattern):
                    logger.debug(f"Excluding {name} - matched extension pattern '{ext_pattern}'")
                    return True
        
        # Also check full path and path components for folder-based exclusions
        for pattern in self.exclusion_patterns:
            pattern_lower = pattern.lower()
            
            # (".git" & co. must match the FOLDER too - see _exclusion_matchers.)

            # Check if pattern matches the full path (for folder paths)
            if fnmatch.fnmatch(file_path_lower, f"*/{pattern_lower}") or fnmatch.fnmatch(file_path_lower, f"*\\{pattern_lower}"):
                logger.debug(f"Excluding {name} - path matched pattern '{pattern}'")
                return True
            
            # Check if any path component matches (for nested exclusions)
            path_parts = file_path.replace('\\', '/').split('/')
            for part in path_parts:
                if fnmatch.fnmatch(part.lower(), pattern_lower):
                    logger.debug(f"Excluding {name} - path component '{part}' matched pattern '{pattern}'")
                    return True
        
        return False
    
    # ======= PINNED PATHS METHODS =======
    
    def add_pinned_path(self, file_path: str) -> bool:
        """Pin a specific file or folder path so it's never organized.
        Returns True if successfully added, False if already pinned.
        """
        # Normalize the path for consistent comparison
        normalized = os.path.normpath(file_path)
        if normalized and normalized not in self.pinned_paths:
            self.pinned_paths.append(normalized)
            self._save_config()
            return True
        return False
    
    def remove_pinned_path(self, file_path: str) -> bool:
        """Unpin a file or folder path.
        Returns True if successfully removed, False if wasn't pinned.
        """
        normalized = os.path.normpath(file_path)
        if normalized in self.pinned_paths:
            self.pinned_paths.remove(normalized)
            self._save_config()
            return True
        return False
    
    def is_pinned(self, file_path: str) -> bool:
        """Check if a specific file or folder path is pinned."""
        normalized = os.path.normpath(file_path).lower()
        for pinned in self.pinned_paths:
            pinned_normalized = os.path.normpath(pinned).lower()
            # Check exact match
            if normalized == pinned_normalized:
                return True
            # Check if file is inside a pinned folder
            if normalized.startswith(pinned_normalized + os.sep):
                return True
        return False
    
    def get_pinned_paths(self) -> List[str]:
        """Get all pinned file/folder paths."""
        return self.pinned_paths.copy()
    
    def clear_all_pinned(self) -> None:
        """Remove all pinned paths."""
        self.pinned_paths = []
        self._save_config()
    
    # ======= ONBOARDING METHODS =======
    def complete_onboarding(self) -> None:
        """Mark the onboarding as completed."""
        self.has_completed_onboarding = True
        self._save_config()
    
    def reset_onboarding(self) -> None:
        """Reset onboarding so it shows again next time."""
        self.has_completed_onboarding = False
        self.onboarding_remind_count = 0
        self._save_config()
    
    def mark_tip_seen(self, tip_id: str) -> None:
        """Mark a contextual tip as seen"""
        if tip_id not in self.seen_tips:
            self.seen_tips.append(tip_id)
            self._save_config()
    
    def reset_tips(self) -> None:
        """Reset all contextual tips to show them again"""
        self.seen_tips = []
        self._save_config()


# Global settings instance
settings = Settings()


