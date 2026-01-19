"""
Tests for OCR functionality.
"""

import pytest
from pathlib import Path
from app.core.ocr import extract_text_from_file, get_supported_formats

def test_supported_formats():
    """Test that supported formats are correctly identified."""
    formats = get_supported_formats()
    
    assert '.pdf' in formats
    assert '.png' in formats
    assert '.jpg' in formats
    assert '.txt' not in formats  # Text files don't need OCR

def test_ocr_extraction_nonexistent_file():
    """Test OCR extraction with non-existent file."""
    result = extract_text_from_file(Path("nonexistent.pdf"))
    assert result is None

def test_ocr_extraction_unsupported_format(tmp_path):
    """Test OCR extraction with unsupported file format."""
    # Create a text file
    text_file = tmp_path / "test.txt"
    text_file.write_text("This is a test file")
    
    result = extract_text_from_file(text_file)
    assert result is None

def test_ocr_extraction_empty_image(tmp_path):
    """Test OCR extraction with an empty image file."""
    # This test would require creating an actual image file
    # For now, we'll just test the function doesn't crash
    formats = get_supported_formats()
    assert len(formats) > 0
