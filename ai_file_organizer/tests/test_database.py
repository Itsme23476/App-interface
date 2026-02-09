"""
Tests for Database module - Uses temp database, safe to run
"""
import pytest
import tempfile
import os
from pathlib import Path
from datetime import datetime


class TestFileIndex:
    """Test database operations with temporary database."""
    
    @pytest.fixture
    def temp_db(self, tmp_path):
        """Create a temporary database for testing."""
        from app.core.database import FileIndex
        db_path = tmp_path / "test_index.db"
        return FileIndex(db_path)
    
    def test_database_creation(self, temp_db):
        """Test that database is created correctly."""
        assert temp_db.db_path.exists()
        print(f"✅ Database created at: {temp_db.db_path}")
    
    def test_add_file(self, temp_db):
        """Test adding a file to the index."""
        test_file = {
            'file_path': 'C:/test/document.pdf',
            'file_name': 'document.pdf',
            'file_extension': '.pdf',
            'file_size': 1024,
            'category': 'Documents',
            'has_ocr': False,
            'label': 'Test Document',
            'tags': ['test', 'document'],
            'caption': 'A test document',
        }
        
        # Use file_data parameter
        result = temp_db.add_file(file_data=test_file)
        assert result is not None
        print("✅ File added to database successfully")
    
    def test_get_file_by_path(self, temp_db):
        """Test retrieving a file by path."""
        # Add a file first
        test_path = 'C:/test/image.jpg'
        temp_db.add_file(file_data={
            'file_path': test_path,
            'file_name': 'image.jpg',
            'file_extension': '.jpg',
            'file_size': 2048,
            'category': 'Images',
            'has_ocr': False,
            'label': 'Test Image',
        })
        
        # Retrieve it
        result = temp_db.get_file_by_path(test_path)
        assert result is not None
        assert result['file_name'] == 'image.jpg'
        assert result['category'] == 'Images'
        print("✅ File retrieved by path correctly")
    
    def test_get_nonexistent_file(self, temp_db):
        """Test retrieving a file that doesn't exist."""
        result = temp_db.get_file_by_path('C:/nonexistent/file.txt')
        assert result is None
        print("✅ Nonexistent file returns None")
    
    def test_remove_file(self, temp_db):
        """Test removing a file from the index."""
        test_path = 'C:/test/to_remove.txt'
        temp_db.add_file(file_data={
            'file_path': test_path,
            'file_name': 'to_remove.txt',
            'file_extension': '.txt',
            'file_size': 100,
            'category': 'Documents',
            'has_ocr': False,
        })
        
        # Verify it exists
        assert temp_db.get_file_by_path(test_path) is not None
        
        # Remove it
        temp_db.remove_file(test_path)
        
        # Verify it's gone
        assert temp_db.get_file_by_path(test_path) is None
        print("✅ File removed successfully")
    
    def test_get_all_files(self, temp_db):
        """Test getting all files from index."""
        # Add multiple files
        for i in range(5):
            temp_db.add_file(file_data={
                'file_path': f'C:/test/file_{i}.txt',
                'file_name': f'file_{i}.txt',
                'file_extension': '.txt',
                'file_size': 100 * i,
                'category': 'Documents',
                'has_ocr': False,
            })
        
        all_files = temp_db.get_all_files()
        assert len(all_files) == 5
        print(f"✅ Retrieved all {len(all_files)} files")
    
    def test_search_files(self, temp_db):
        """Test basic search functionality."""
        # Add files with different labels
        temp_db.add_file(file_data={
            'file_path': 'C:/test/vacation_photo.jpg',
            'file_name': 'vacation_photo.jpg',
            'file_extension': '.jpg',
            'file_size': 5000,
            'category': 'Images',
            'has_ocr': False,
            'label': 'Beach vacation photo',
            'caption': 'Family at the beach',
        })
        
        temp_db.add_file(file_data={
            'file_path': 'C:/test/tax_document.pdf',
            'file_name': 'tax_document.pdf',
            'file_extension': '.pdf',
            'file_size': 2000,
            'category': 'Documents',
            'has_ocr': False,
            'label': 'Tax return 2023',
            'caption': 'Annual tax filing',
        })
        
        # Search for vacation
        results = temp_db.search_files('vacation')
        assert len(results) >= 1
        assert any('vacation' in r.get('file_name', '').lower() or 
                   'vacation' in str(r.get('label', '')).lower() 
                   for r in results)
        print("✅ Search finds matching files")
    
    def test_clear_all(self, temp_db):
        """Test clearing all files from database."""
        # Add some files
        for i in range(3):
            temp_db.add_file(file_data={
                'file_path': f'C:/test/clear_{i}.txt',
                'file_name': f'clear_{i}.txt',
                'file_extension': '.txt',
                'file_size': 100,
                'category': 'Documents',
                'has_ocr': False,
            })
        
        # Verify files exist
        assert len(temp_db.get_all_files()) == 3
        
        # Clear all
        temp_db.clear_all()
        
        # Verify empty
        assert len(temp_db.get_all_files()) == 0
        print("✅ Database cleared successfully")
    
    def test_update_file(self, temp_db):
        """Test updating an existing file."""
        test_path = 'C:/test/updateme.pdf'
        temp_db.add_file(file_data={
            'file_path': test_path,
            'file_name': 'updateme.pdf',
            'file_extension': '.pdf',
            'file_size': 1000,
            'category': 'Documents',
            'has_ocr': False,
            'label': 'Original label',
        })
        
        # Update the file
        temp_db.add_file(file_data={
            'file_path': test_path,
            'file_name': 'updateme.pdf',
            'file_extension': '.pdf',
            'file_size': 2000,  # Changed size
            'category': 'Documents',
            'has_ocr': True,  # Now has OCR
            'label': 'Updated label',  # Changed label
        })
        
        # Retrieve and verify
        result = temp_db.get_file_by_path(test_path)
        assert result['label'] == 'Updated label'
        assert result['file_size'] == 2000
        print("✅ File updated successfully")
    
    def test_file_count(self, temp_db):
        """Test counting files in database."""
        # Add files
        for i in range(7):
            temp_db.add_file(file_data={
                'file_path': f'C:/test/count_{i}.txt',
                'file_name': f'count_{i}.txt',
                'file_extension': '.txt',
                'file_size': 100,
                'category': 'Documents',
                'has_ocr': False,
            })
        
        count = temp_db.get_file_count()
        assert count == 7
        print(f"✅ File count correct: {count}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
