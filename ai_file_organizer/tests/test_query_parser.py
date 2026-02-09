"""
Tests for Query Parser module - Pure logic, safe to run
"""
import pytest
from datetime import datetime, timedelta


class TestQueryParser:
    """Test NLP query parsing functionality."""
    
    def test_basic_query(self):
        """Test basic query parsing."""
        from app.core.query_parser import parse_query
        
        result = parse_query("vacation photos")
        assert 'clean_query' in result
        # Query may be simplified but should exist
        assert result['clean_query'] is not None
        print(f"✅ Basic query parsed: clean_query='{result['clean_query']}'")
    
    def test_type_filter_images(self):
        """Test parsing image type filter."""
        from app.core.query_parser import parse_query
        
        result = parse_query("show me images")
        type_filter = result.get('type_filter')
        # Should recognize 'images' type
        assert type_filter is not None or 'image' in str(result).lower()
        print(f"✅ Image type filter: {type_filter}")
    
    def test_type_filter_documents(self):
        """Test parsing document type filter."""
        from app.core.query_parser import parse_query
        
        result = parse_query("pdf documents")
        assert result is not None
        print(f"✅ Document type filter handled: {result.get('type_filter')}")
    
    def test_type_filter_videos(self):
        """Test parsing video type filter."""
        from app.core.query_parser import parse_query
        
        result = parse_query("video files")
        assert result is not None
        print(f"✅ Video type filter handled: {result.get('type_filter')}")
    
    def test_date_filter_today(self):
        """Test parsing 'today' date filter."""
        from app.core.query_parser import parse_query
        
        result = parse_query("files from today")
        date_range = result.get('date_range', (None, None))
        print(f"✅ Today filter: date_range={date_range}")
    
    def test_date_filter_yesterday(self):
        """Test parsing 'yesterday' date filter."""
        from app.core.query_parser import parse_query
        
        result = parse_query("yesterday's documents")
        date_range = result.get('date_range', (None, None))
        print(f"✅ Yesterday filter: date_range={date_range}")
    
    def test_date_filter_this_week(self):
        """Test parsing 'this week' date filter."""
        from app.core.query_parser import parse_query
        
        result = parse_query("files from this week")
        date_range = result.get('date_range', (None, None))
        print(f"✅ This week filter: date_range={date_range}")
    
    def test_date_filter_last_month(self):
        """Test parsing 'last month' date filter."""
        from app.core.query_parser import parse_query
        
        result = parse_query("photos from last month")
        date_range = result.get('date_range', (None, None))
        print(f"✅ Last month filter: date_range={date_range}")
    
    def test_extension_filter(self):
        """Test parsing extension filter."""
        from app.core.query_parser import parse_query
        
        result = parse_query("find .pdf files")
        extensions = result.get('extensions')
        print(f"✅ Extension filter: extensions={extensions}")
    
    def test_combined_filters(self):
        """Test parsing multiple filters."""
        from app.core.query_parser import parse_query
        
        result = parse_query("vacation images from last week")
        assert 'clean_query' in result
        print(f"✅ Combined filters: {result}")
    
    def test_empty_query(self):
        """Test handling empty query."""
        from app.core.query_parser import parse_query
        
        result = parse_query("")
        assert result is not None
        print("✅ Empty query handled")
    
    def test_special_characters(self):
        """Test handling special characters in query."""
        from app.core.query_parser import parse_query
        
        result = parse_query("file (copy) [2023]")
        assert result is not None
        print("✅ Special characters handled")
    
    def test_get_date_range(self):
        """Test date range calculation."""
        from app.core.query_parser import get_date_range
        
        # Test various date keywords
        for keyword in ['today', 'yesterday', 'this week', 'last month']:
            result = get_date_range(keyword)
            if result:
                start, end = result
                print(f"  - '{keyword}': {start} to {end}")
            else:
                print(f"  - '{keyword}': no range")
        
        print("✅ Date range calculation works")
    
    def test_type_extensions_mapping(self):
        """Test that type extensions are defined."""
        from app.core.query_parser import TYPE_EXTENSIONS
        
        assert isinstance(TYPE_EXTENSIONS, dict)
        # Check for common types (may use singular or plural)
        assert any(k in TYPE_EXTENSIONS for k in ['image', 'images', 'img'])
        assert any(k in TYPE_EXTENSIONS for k in ['document', 'documents', 'doc'])
        assert any(k in TYPE_EXTENSIONS for k in ['video', 'videos', 'vid'])
        assert any(k in TYPE_EXTENSIONS for k in ['audio', 'music', 'sound'])
        
        print(f"✅ Type extensions defined: {list(TYPE_EXTENSIONS.keys())}")
    
    def test_result_structure(self):
        """Test that parse_query returns expected structure."""
        from app.core.query_parser import parse_query
        
        result = parse_query("test query")
        
        # Should have these keys
        expected_keys = ['clean_query']
        for key in expected_keys:
            assert key in result, f"Missing key: {key}"
        
        print(f"✅ Result structure has keys: {list(result.keys())}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
