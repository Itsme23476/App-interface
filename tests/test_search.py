"""
Tests for search functionality.
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from app.core.database import FileIndex
from app.core.search import SearchService

@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_index.db"
    
    # Create test database
    file_index = FileIndex(db_path)
    
    yield file_index
    
    # Cleanup
    shutil.rmtree(temp_dir)

@pytest.fixture
def temp_search_service(temp_db):
    """Create a search service with temporary database."""
    return SearchService()

@pytest.fixture
def sample_files(tmp_path):
    """Create sample files for testing."""
    # Create test files
    files = []
    
    # Text file
    text_file = tmp_path / "document.txt"
    text_file.write_text("This is a test document with important information.")
    files.append({
        'source_path': str(text_file),
        'name': 'document.txt',
        'extension': '.txt',
        'size': text_file.stat().st_size,
        'mime_type': 'text/plain',
        'category': 'Documents/Text',
        'has_ocr': False,
        'ocr_text': '',
        'is_file': True,
        'is_dir': False
    })
    
    # PDF file (simulated)
    pdf_file = tmp_path / "report.pdf"
    pdf_file.write_text("PDF content placeholder")
    files.append({
        'source_path': str(pdf_file),
        'name': 'report.pdf',
        'extension': '.pdf',
        'size': pdf_file.stat().st_size,
        'mime_type': 'application/pdf',
        'category': 'Documents/PDFs',
        'has_ocr': True,
        'ocr_text': 'This is a PDF report about quarterly results and financial data.',
        'is_file': True,
        'is_dir': False
    })
    
    # Image file (simulated)
    image_file = tmp_path / "screenshot.png"
    image_file.write_text("Image content placeholder")
    files.append({
        'source_path': str(image_file),
        'name': 'screenshot.png',
        'extension': '.png',
        'size': image_file.stat().st_size,
        'mime_type': 'image/png',
        'category': 'Images/Screenshots',
        'has_ocr': True,
        'ocr_text': 'Screenshot showing login page with username and password fields.',
        'is_file': True,
        'is_dir': False
    })
    
    return files

def test_database_initialization(temp_db):
    """Test database initialization."""
    assert temp_db.db_path.exists()
    
    # Check if tables were created
    import sqlite3
    with sqlite3.connect(temp_db.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        assert 'files' in tables
        assert 'files_fts' in tables
        assert 'search_history' in tables

def test_add_file(temp_db, sample_files):
    """Test adding files to the index."""
    # Add files to index
    for file_data in sample_files:
        success = temp_db.add_file(file_data)
        assert success
    
    # Check statistics
    stats = temp_db.get_statistics()
    assert stats['total_files'] == 3
    assert stats['files_with_ocr'] == 2

def test_search_files(temp_db, sample_files):
    """Test searching for files."""
    # Add files to index
    for file_data in sample_files:
        temp_db.add_file(file_data)
    
    # Search for PDF files
    results = temp_db.search_files("PDF")
    assert len(results) > 0
    
    # Check that PDF file is in results
    pdf_results = [r for r in results if r['file_extension'] == '.pdf']
    assert len(pdf_results) > 0

def test_search_by_ocr_content(temp_db, sample_files):
    """Test searching by OCR content."""
    # Add files to index
    for file_data in sample_files:
        temp_db.add_file(file_data)
    
    # Search for content in OCR text
    results = temp_db.search_files("login")
    assert len(results) > 0
    
    # Check that image with login content is found
    login_results = [r for r in results if 'login' in r.get('ocr_text', '').lower()]
    assert len(login_results) > 0

def test_search_service_index_directory(temp_search_service, tmp_path):
    """Test search service directory indexing."""
    # Create test directory with files
    test_dir = tmp_path / "test_files"
    test_dir.mkdir()
    
    # Create some test files
    (test_dir / "test1.txt").write_text("Test file 1")
    (test_dir / "test2.txt").write_text("Test file 2")
    
    # Index the directory
    result = temp_search_service.index_directory(test_dir)
    
    assert 'total_files' in result
    assert 'indexed_files' in result
    assert result['indexed_files'] >= 2

def test_search_service_search(temp_search_service, sample_files):
    """Test search service search functionality."""
    # Add files to index
    for file_data in sample_files:
        temp_search_service.index.add_file(file_data)
    
    # Search for files
    results = temp_search_service.search_files("document")
    assert len(results) > 0
    
    # Check enhanced results
    for result in results:
        assert 'file_path_obj' in result
        assert 'exists' in result
        assert 'size_formatted' in result
        assert 'relevance_score' in result

def test_search_by_category(temp_search_service, sample_files):
    """Test searching by category."""
    # Add files to index
    for file_data in sample_files:
        temp_search_service.index.add_file(file_data)
    
    # Search by category
    results = temp_search_service.search_by_category("Documents")
    assert len(results) > 0
    
    # All results should be documents
    for result in results:
        assert 'Documents' in result['category']

def test_search_suggestions(temp_search_service):
    """Test search suggestions."""
    # Add some search history
    temp_search_service.index._log_search("test query", 5)
    temp_search_service.index._log_search("another test", 3)
    
    # Get suggestions
    suggestions = temp_search_service.get_search_suggestions("test")
    assert len(suggestions) > 0
    assert any("test" in s.lower() for s in suggestions)

def test_file_details(temp_search_service, sample_files):
    """Test getting file details."""
    # Add files to index
    for file_data in sample_files:
        temp_search_service.index.add_file(file_data)
    
    # Get details for a specific file
    file_path = sample_files[0]['source_path']
    details = temp_search_service.get_file_details(file_path)
    
    assert details is not None
    assert details['file_path'] == file_path
    assert 'file_path_obj' in details
    assert 'exists' in details

def test_index_statistics(temp_search_service, sample_files):
    """Test getting index statistics."""
    # Add files to index
    for file_data in sample_files:
        temp_search_service.index.add_file(file_data)
    
    # Get statistics
    stats = temp_search_service.get_index_statistics()
    
    assert 'total_files' in stats
    assert 'files_with_ocr' in stats
    assert 'total_size' in stats
    assert 'categories' in stats
    assert stats['total_files'] == 3
    assert stats['files_with_ocr'] == 2
