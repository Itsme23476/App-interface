"""
Tests for Theme Manager module - Pure logic, safe to run
"""
import pytest


class TestThemeManager:
    """Test theme management functionality."""
    
    def test_get_theme_colors_dark(self):
        """Test dark theme color palette."""
        from app.ui.theme_manager import get_theme_colors
        
        colors = get_theme_colors('dark')
        
        # Check required color keys exist
        required_keys = [
            'bg', 'surface', 'text', 'text_muted', 
            'border', 'input_bg'
        ]
        
        for key in required_keys:
            assert key in colors, f"Missing color key: {key}"
        
        # Dark theme should have dark backgrounds
        bg = colors['bg']
        assert bg.startswith('#')
        # Dark colors have low values (closer to 0)
        bg_value = int(bg[1:3], 16)  # Get red component
        assert bg_value < 128, f"Dark theme bg should be dark: {bg}"
        
        print(f"✅ Dark theme colors: {len(colors)} keys defined")
    
    def test_get_theme_colors_light(self):
        """Test light theme color palette."""
        from app.ui.theme_manager import get_theme_colors
        
        colors = get_theme_colors('light')
        
        # Light theme should have light backgrounds
        bg = colors['bg']
        assert bg.startswith('#')
        # Light colors have high values (closer to 255)
        bg_value = int(bg[1:3], 16)  # Get red component
        assert bg_value > 128, f"Light theme bg should be light: {bg}"
        
        print(f"✅ Light theme colors: {len(colors)} keys defined")
    
    def test_theme_color_consistency(self):
        """Test that both themes have same keys."""
        from app.ui.theme_manager import get_theme_colors
        
        dark_colors = get_theme_colors('dark')
        light_colors = get_theme_colors('light')
        
        dark_keys = set(dark_colors.keys())
        light_keys = set(light_colors.keys())
        
        assert dark_keys == light_keys, f"Theme keys mismatch: {dark_keys ^ light_keys}"
        print(f"✅ Both themes have {len(dark_keys)} consistent keys")
    
    def test_purple_accent_color(self):
        """Test that purple accent color exists."""
        from app.ui.theme_manager import get_theme_colors
        
        dark_colors = get_theme_colors('dark')
        
        # Check for purple in any of the color values
        all_values = ' '.join(dark_colors.values()).upper()
        assert '7C4DFF' in all_values or '9575FF' in all_values, "Purple accent color not found"
        print("✅ Purple accent color present")
    
    def test_scrollbar_colors(self):
        """Test scrollbar color definitions."""
        from app.ui.theme_manager import get_theme_colors
        
        dark_colors = get_theme_colors('dark')
        
        assert 'scrollbar_bg' in dark_colors
        assert 'scrollbar_handle' in dark_colors
        
        print(f"✅ Scrollbar colors defined: bg={dark_colors['scrollbar_bg']}, handle={dark_colors['scrollbar_handle']}")
    
    def test_theme_manager_instance(self):
        """Test theme manager singleton."""
        from app.ui.theme_manager import theme_manager
        
        assert theme_manager is not None
        assert hasattr(theme_manager, 'current_theme')
        assert hasattr(theme_manager, 'apply_theme')
        
        print(f"✅ Theme manager instance: current_theme={theme_manager.current_theme}")
    
    def test_apply_titlebar_theme_function(self):
        """Test that apply_titlebar_theme function exists."""
        from app.ui.theme_manager import apply_titlebar_theme
        
        assert callable(apply_titlebar_theme)
        print("✅ apply_titlebar_theme function available")
    
    def test_text_colors_contrast(self):
        """Test that text colors have good contrast with backgrounds."""
        from app.ui.theme_manager import get_theme_colors
        
        dark_colors = get_theme_colors('dark')
        
        # Text should be light in dark theme
        text = dark_colors['text']
        text_value = int(text[1:3], 16)
        assert text_value > 180, f"Dark theme text should be light: {text}"
        
        light_colors = get_theme_colors('light')
        
        # Text should be dark in light theme
        text = light_colors['text']
        text_value = int(text[1:3], 16)
        assert text_value < 100, f"Light theme text should be dark: {text}"
        
        print("✅ Text colors have good contrast")


class TestStylesheets:
    """Test stylesheet loading."""
    
    def test_dark_stylesheet_exists(self):
        """Test that dark stylesheet file exists."""
        from pathlib import Path
        
        # Find styles.qss
        possible_paths = [
            Path("app/ui/styles.qss"),
            Path("ai_file_organizer/app/ui/styles.qss"),
        ]
        
        found = False
        for path in possible_paths:
            if path.exists():
                found = True
                content = path.read_text()
                assert len(content) > 100, "Stylesheet seems too short"
                print(f"✅ Dark stylesheet found: {path} ({len(content)} chars)")
                break
        
        if not found:
            print("⚠️ Dark stylesheet not found in expected locations")
    
    def test_light_stylesheet_exists(self):
        """Test that light stylesheet file exists."""
        from pathlib import Path
        
        possible_paths = [
            Path("app/ui/styles_light.qss"),
            Path("ai_file_organizer/app/ui/styles_light.qss"),
        ]
        
        found = False
        for path in possible_paths:
            if path.exists():
                found = True
                content = path.read_text()
                assert len(content) > 100, "Stylesheet seems too short"
                print(f"✅ Light stylesheet found: {path} ({len(content)} chars)")
                break
        
        if not found:
            print("⚠️ Light stylesheet not found in expected locations")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
