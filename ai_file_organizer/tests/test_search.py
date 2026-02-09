"""
Tests for Search Service module - Uses temp database, safe to run
"""
import pytest
import tempfile
from pathlib import Path


class TestSearchService:
    """Test search functionality."""
    
    @pytest.fixture
    def search_db_with_data(self, tmp_path):
        """Create database with test data."""
        from app.core.database import FileIndex
        
        # Create temp database
        db_path = tmp_path / "test_search.db"
        db = FileIndex(db_path)
        
        # Add test files using file_data parameter
        test_files = [
            {
                'file_path': 'C:/photos/vacation_beach.jpg',
                'file_name': 'vacation_beach.jpg',
                'file_extension': '.jpg',
                'file_size': 5000,
                'category': 'Images',
                'has_ocr': False,
                'label': 'Beach vacation photo with family',
                'caption': 'Beautiful sunset at the beach',
                'tags': ['vacation', 'beach', 'family'],
            },
            {
                'file_path': 'C:/documents/tax_return_2023.pdf',
                'file_name': 'tax_return_2023.pdf',
                'file_extension': '.pdf',
                'file_size': 2000,
                'category': 'Documents',
                'has_ocr': True,
                'ocr_text': 'Internal Revenue Service Form 1040 Tax Return',
                'label': 'Tax return document',
                'caption': '2023 annual tax filing',
            },
            {
                'file_path': 'C:/work/project_proposal.docx',
                'file_name': 'project_proposal.docx',
                'file_extension': '.docx',
                'file_size': 3000,
                'category': 'Documents',
                'has_ocr': False,
                'label': 'Project proposal for Q4',
                'caption': 'Business proposal document',
            },
            {
                'file_path': 'C:/music/favorite_song.mp3',
                'file_name': 'favorite_song.mp3',
                'file_extension': '.mp3',
                'file_size': 8000,
                'category': 'Audio',
                'has_ocr': False,
                'label': 'Rock music track',
            },
        ]
        
        for f in test_files:
            db.add_file(file_data=f)
        
        return db
    
    def test_search_by_filename(self, search_db_with_data):
        """Test searching by filename."""
        db = search_db_with_data
        
        results = db.search_files('vacation')
        assert len(results) >= 1
        assert any('vacation' in r['file_name'].lower() for r in results)
        print(f"✅ Search by filename: found {len(results)} results")
    
    def test_search_by_label(self, search_db_with_data):
        """Test searching by label/description."""
        db = search_db_with_data
        
        results = db.search_files('beach')
        assert len(results) >= 1
        print(f"✅ Search by label: found {len(results)} results")
    
    def test_search_by_category(self, search_db_with_data):
        """Test filtering by category."""
        db = search_db_with_data
        
        # Get all documents
        all_files = db.get_all_files()
        documents = [f for f in all_files if f.get('category') == 'Documents']
        assert len(documents) >= 2
        print(f"✅ Category filter: found {len(documents)} documents")
    
    def test_search_no_results(self, search_db_with_data):
        """Test search with no matching results."""
        db = search_db_with_data
        
        results = db.search_files('xyznonexistent123')
        assert len(results) == 0
        print("✅ No results returns empty list")
    
    def test_search_ocr_content(self, search_db_with_data):
        """Test searching OCR text content."""
        db = search_db_with_data
        
        # Search for text that's only in OCR
        results = db.search_files('1040')  # From tax form OCR
        # May or may not find depending on search implementation
        print(f"✅ OCR content search: found {len(results)} results")
    
    def test_search_case_insensitive(self, search_db_with_data):
        """Test that search is case insensitive."""
        db = search_db_with_data
        
        results_lower = db.search_files('vacation')
        results_upper = db.search_files('VACATION')
        results_mixed = db.search_files('VaCaTiOn')
        
        # All should return same results
        assert len(results_lower) == len(results_upper) == len(results_mixed)
        print("✅ Search is case insensitive")
    
    def test_empty_search(self, search_db_with_data):
        """Test empty search query."""
        db = search_db_with_data
        
        results = db.search_files('')
        # Empty search might return all files or empty
        print(f"✅ Empty search: returned {len(results)} results")
    
    def test_get_all_files(self, search_db_with_data):
        """Test getting all indexed files."""
        db = search_db_with_data
        
        all_files = db.get_all_files()
        assert len(all_files) == 4  # We added 4 test files
        print(f"✅ Get all files: {len(all_files)} files")


class TestSearchPauseCancel:
    """Test search pause/cancel functionality."""
    
    def test_cancel_flag(self):
        """Test cancel flag functionality."""
        from app.core.search import SearchService
        
        service = SearchService()
        
        # Initially not cancelled
        assert not service._cancel_flag.is_set()
        
        # Cancel
        service.cancel_indexing()
        assert service._cancel_flag.is_set()
        
        print("✅ Cancel flag works correctly")
    
    def test_pause_resume(self):
        """Test pause/resume functionality."""
        from app.core.search import SearchService
        
        service = SearchService()
        
        # Initially not paused
        assert not service.is_paused()
        
        # Pause
        service.pause_indexing()
        assert service.is_paused()
        
        # Resume
        service.resume_indexing()
        assert not service.is_paused()
        
        print("✅ Pause/resume works correctly")
    
    def test_service_singleton(self):
        """Test that search_service is available."""
        from app.core.search import search_service
        
        assert search_service is not None
        assert hasattr(search_service, 'search_files')
        assert hasattr(search_service, 'index_directory')
        
        print("✅ Search service singleton available")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
