"""
Tests for Settings module - NO file moves, safe to run
"""
import pytest
import tempfile
import json
import os
from pathlib import Path


class TestSettings:
    """Test settings persistence and functionality."""
    
    @pytest.fixture
    def temp_config_dir(self, tmp_path):
        """Create a temporary config directory."""
        return tmp_path / "config"
    
    def test_default_values(self):
        """Test that default settings are initialized correctly."""
        from app.core.settings import Settings
        
        # Create fresh settings instance (won't load from disk in test)
        s = Settings()
        
        # Check defaults
        assert s.theme in ['dark', 'light']
        assert s.quick_search_shortcut == 'ctrl+alt+h'
        assert s.use_quick_search == True
        assert isinstance(s.exclusion_patterns, list)
        assert isinstance(s.pinned_paths, list)
        assert isinstance(s.auto_organize_folders, list)
        print("✅ Default values initialized correctly")
    
    def test_exclusion_patterns_default(self):
        """Test default exclusion patterns exist."""
        from app.core.settings import Settings
        s = Settings()
        
        defaults = s._get_default_exclusions()
        assert len(defaults) > 0
        # Check for common exclusion patterns
        all_patterns = ' '.join(defaults)
        assert '.git' in all_patterns or 'node_modules' in all_patterns or '__pycache__' in all_patterns
        print(f"✅ Default exclusions: {len(defaults)} patterns")
    
    def test_should_exclude(self):
        """Test exclusion pattern matching."""
        from app.core.settings import Settings
        s = Settings()
        
        # Add test patterns
        s.exclusion_patterns = ['*.tmp', '*.log', 'node_modules', '.git']
        
        # Test matching
        assert s.should_exclude('test.tmp') == True
        assert s.should_exclude('debug.log') == True
        assert s.should_exclude('C:/project/node_modules/package.json') == True
        assert s.should_exclude('important.pdf') == False
        assert s.should_exclude('document.docx') == False
        print("✅ Exclusion pattern matching works correctly")
    
    def test_category_map_loaded(self):
        """Test that category mappings are loaded."""
        from app.core.settings import Settings
        s = Settings()
        
        categories = s._load_default_categories()
        assert isinstance(categories, dict)
        assert len(categories) > 0
        print(f"✅ Category map loaded: {len(categories)} categories")
        print(f"   Categories: {list(categories.keys())}")
    
    def test_pinned_paths(self):
        """Test pinned paths functionality."""
        from app.core.settings import Settings
        s = Settings()
        
        # Test adding pinned path
        test_path = "C:/test/important_file.pdf"
        s.pinned_paths = []
        s.pinned_paths.append(test_path)
        
        assert test_path in s.pinned_paths
        assert len(s.pinned_paths) == 1
        print("✅ Pinned paths functionality works")
    
    def test_onboarding_settings(self):
        """Test onboarding-related settings."""
        from app.core.settings import Settings
        s = Settings()
        
        # Check onboarding settings exist
        assert hasattr(s, 'has_completed_onboarding')
        assert hasattr(s, 'onboarding_remind_count')
        assert hasattr(s, 'seen_tips')
        assert isinstance(s.seen_tips, list)
        print("✅ Onboarding settings initialized")
    
    def test_theme_values(self):
        """Test theme setting accepts valid values."""
        from app.core.settings import Settings
        s = Settings()
        
        # Should accept dark/light
        s.theme = 'dark'
        assert s.theme == 'dark'
        
        s.theme = 'light'
        assert s.theme == 'light'
        print("✅ Theme values work correctly")
    
    def test_quick_search_settings(self):
        """Test quick search related settings."""
        from app.core.settings import Settings
        s = Settings()
        
        assert hasattr(s, 'use_quick_search')
        assert hasattr(s, 'quick_search_shortcut')
        assert hasattr(s, 'quick_search_autopaste')
        assert hasattr(s, 'quick_search_auto_confirm')
        
        assert s.quick_search_shortcut == 'ctrl+alt+h'
        print("✅ Quick search settings initialized")
    
    def test_auto_organize_settings(self):
        """Test auto-organize related settings."""
        from app.core.settings import Settings
        s = Settings()
        
        assert hasattr(s, 'auto_organize_folders')
        assert hasattr(s, 'auto_organize_auto_start')
        assert isinstance(s.auto_organize_folders, list)
        print("✅ Auto-organize settings initialized")
    
    def test_ai_provider_settings(self):
        """Test AI provider settings."""
        from app.core.settings import Settings
        s = Settings()
        
        assert hasattr(s, 'ai_provider')
        assert s.ai_provider in ['openai', 'local', 'none']
        print(f"✅ AI provider: {s.ai_provider}")
    
    def test_app_data_dir(self):
        """Test app data directory getter."""
        from app.core.settings import Settings
        s = Settings()
        
        app_dir = s.get_app_data_dir()
        assert isinstance(app_dir, Path)
        print(f"✅ App data dir: {app_dir}")
    
    def test_settings_singleton(self):
        """Test that settings is a singleton instance."""
        from app.core.settings import settings
        
        assert settings is not None
        assert hasattr(settings, 'theme')
        assert hasattr(settings, 'exclusion_patterns')
        print("✅ Settings singleton available")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
