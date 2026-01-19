"""
Tests for file categorization logic.
"""

import pytest
from pathlib import Path
from app.core.categorize import categorize_file, get_file_metadata


def test_categorize_by_extension():
    """Test categorization by file extension."""
    # Test known extensions
    assert categorize_file(Path("document.pdf")) == "Documents/PDFs"
    assert categorize_file(Path("image.jpg")) == "Images/Photos"
    assert categorize_file(Path("video.mp4")) == "Videos"
    assert categorize_file(Path("script.py")) == "Code"
    
    # Test unknown extension
    assert categorize_file(Path("unknown.xyz")) == "Misc"


def test_categorize_by_mime(tmp_path):
    """Test categorization by MIME type."""
    # Create a test file (this is a simplified test)
    test_file = tmp_path / "test.txt"
    test_file.write_text("test content")
    
    # Test that text files are categorized correctly
    category = categorize_file(test_file)
    assert category == "Documents/Text"


def test_get_file_metadata(tmp_path):
    """Test file metadata extraction."""
    # Create a test file
    test_file = tmp_path / "test.txt"
    test_file.write_text("test content")
    
    metadata = get_file_metadata(test_file)
    
    assert metadata["name"] == "test.txt"
    assert metadata["extension"] == ".txt"
    assert metadata["category"] == "Documents/Text"
    assert metadata["is_file"] is True
    assert metadata["is_dir"] is False
    assert metadata["size"] > 0


def test_get_file_metadata_nonexistent():
    """Test metadata extraction for non-existent file."""
    metadata = get_file_metadata(Path("nonexistent.txt"))
    
    assert metadata["name"] == "nonexistent.txt"
    assert metadata["extension"] == ".txt"
    assert metadata["category"] == "Misc"
    assert metadata["is_file"] is False
    assert metadata["is_dir"] is False
    assert metadata["size"] == 0


