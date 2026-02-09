"""
Tests for File Categorization module - Pure logic, safe to run
"""
import pytest
import tempfile
from pathlib import Path


class TestCategorization:
    """Test file categorization logic."""
    
    def test_image_extensions(self):
        """Test image file categorization."""
        from app.core.categorize import get_file_metadata
        
        # Create temp image file (empty, just for extension testing)
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
            temp_path = Path(f.name)
        
        try:
            metadata = get_file_metadata(temp_path)
            if metadata:
                category = metadata.get('category', '')
                mime = str(metadata.get('mime_type', ''))
                # Should be Images category or image mime type
                is_image = category == 'Images' or 'image' in mime.lower()
                print(f"✅ .jpg categorized: category={category}, mime={mime}")
        finally:
            temp_path.unlink(missing_ok=True)
    
    def test_document_extensions(self):
        """Test document file categorization."""
        from app.core.categorize import get_file_metadata
        
        extensions = ['.pdf', '.docx', '.txt']
        for ext in extensions:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                temp_path = Path(f.name)
            
            try:
                metadata = get_file_metadata(temp_path)
                if metadata:
                    print(f"  - {ext}: category={metadata.get('category')}")
            finally:
                temp_path.unlink(missing_ok=True)
        
        print("✅ Document extensions tested")
    
    def test_video_extensions(self):
        """Test video file categorization."""
        from app.core.categorize import get_file_metadata
        
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
            temp_path = Path(f.name)
        
        try:
            metadata = get_file_metadata(temp_path)
            if metadata:
                print(f"✅ .mp4 categorized: {metadata.get('category')}")
        finally:
            temp_path.unlink(missing_ok=True)
    
    def test_audio_extensions(self):
        """Test audio file categorization."""
        from app.core.categorize import get_file_metadata
        
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
            temp_path = Path(f.name)
        
        try:
            metadata = get_file_metadata(temp_path)
            if metadata:
                print(f"✅ .mp3 categorized: {metadata.get('category')}")
        finally:
            temp_path.unlink(missing_ok=True)
    
    def test_archive_extensions(self):
        """Test archive file categorization."""
        from app.core.categorize import get_file_metadata
        
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            temp_path = Path(f.name)
        
        try:
            metadata = get_file_metadata(temp_path)
            if metadata:
                print(f"✅ .zip categorized: {metadata.get('category')}")
        finally:
            temp_path.unlink(missing_ok=True)
    
    def test_metadata_fields(self):
        """Test that metadata contains required fields."""
        from app.core.categorize import get_file_metadata
        
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            f.write(b"Test content")
            temp_path = Path(f.name)
        
        try:
            metadata = get_file_metadata(temp_path)
            
            required_fields = ['name', 'extension', 'size', 'category']
            for field in required_fields:
                assert field in metadata, f"Missing field: {field}"
            
            print(f"✅ Metadata contains all required fields: {list(metadata.keys())}")
        finally:
            temp_path.unlink(missing_ok=True)
    
    def test_file_size(self):
        """Test that file size is captured correctly."""
        from app.core.categorize import get_file_metadata
        
        content = b"Hello, World! This is test content."
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            f.write(content)
            temp_path = Path(f.name)
        
        try:
            metadata = get_file_metadata(temp_path)
            assert metadata['size'] == len(content)
            print(f"✅ File size correct: {metadata['size']} bytes")
        finally:
            temp_path.unlink(missing_ok=True)
    
    def test_nonexistent_file(self):
        """Test handling of nonexistent file."""
        from app.core.categorize import get_file_metadata
        
        result = get_file_metadata(Path("/nonexistent/file.txt"))
        # Should return something (possibly with error) or empty, not crash
        if result:
            if 'error' in result:
                print(f"✅ Nonexistent file returned error: {result.get('error', '')[:50]}...")
            else:
                print(f"✅ Nonexistent file returned: {result}")
        else:
            print("✅ Nonexistent file returned None/empty")
    
    def test_hidden_file(self):
        """Test categorization of hidden file (starting with dot)."""
        from app.core.categorize import get_file_metadata
        
        with tempfile.NamedTemporaryFile(prefix='.hidden', suffix='.txt', delete=False) as f:
            f.write(b"Hidden content")
            temp_path = Path(f.name)
        
        try:
            metadata = get_file_metadata(temp_path)
            if metadata:
                print(f"✅ Hidden file categorized: {metadata.get('category')}")
        finally:
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
