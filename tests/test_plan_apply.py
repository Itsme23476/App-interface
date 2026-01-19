"""
Tests for move planning and application logic.
"""

import pytest
import shutil
from pathlib import Path
from app.core.plan import create_move_plan, validate_move_plan, get_plan_summary
from app.core.apply import apply_moves


@pytest.fixture
def test_files(tmp_path):
    """Create test files for testing."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    
    # Create test files
    files = []
    test_data = [
        ("doc1.pdf", "Documents/PDFs"),
        ("image1.jpg", "Images/Photos"),
        ("script.py", "Code"),
        ("unknown.xyz", "Misc")
    ]
    
    for filename, expected_category in test_data:
        file_path = source_dir / filename
        file_path.write_text(f"test content for {filename}")
        files.append({
            "name": filename,
            "source_path": str(file_path),
            "category": expected_category,
            "size": file_path.stat().st_size
        })
    
    return files, source_dir


def test_create_move_plan(test_files):
    """Test move plan creation."""
    files, source_dir = test_files
    dest_dir = source_dir.parent / "destination"
    
    move_plan = create_move_plan(files, source_dir, dest_dir)
    
    assert len(move_plan) == len(files)
    
    for move in move_plan:
        assert "source_path" in move
        assert "destination_path" in move
        assert "category" in move
        assert Path(move["source_path"]).exists()


def test_validate_move_plan(test_files):
    """Test move plan validation."""
    files, source_dir = test_files
    dest_dir = source_dir.parent / "destination"
    
    move_plan = create_move_plan(files, source_dir, dest_dir)
    
    is_valid, errors = validate_move_plan(move_plan, source_dir, dest_dir)
    
    assert is_valid
    assert len(errors) == 0


def test_validate_move_plan_same_directories(test_files):
    """Test validation when source and destination are the same."""
    files, source_dir = test_files
    
    move_plan = create_move_plan(files, source_dir, source_dir)
    
    is_valid, errors = validate_move_plan(move_plan, source_dir, source_dir)
    
    assert not is_valid
    assert any("same" in error.lower() for error in errors)


def test_get_plan_summary(test_files):
    """Test plan summary generation."""
    files, source_dir = test_files
    dest_dir = source_dir.parent / "destination"
    
    move_plan = create_move_plan(files, source_dir, dest_dir)
    summary = get_plan_summary(move_plan)
    
    assert summary["total_files"] == len(files)
    assert summary["total_size"] > 0
    assert "categories" in summary


def test_apply_moves(test_files):
    """Test move application."""
    files, source_dir = test_files
    dest_dir = source_dir.parent / "destination"
    
    move_plan = create_move_plan(files, source_dir, dest_dir)
    
    success, errors, log_file = apply_moves(move_plan)
    
    assert success
    assert len(errors) == 0
    assert log_file
    
    # Verify files were moved
    for move in move_plan:
        dest_path = Path(move["destination_path"])
        assert dest_path.exists()
        assert not Path(move["source_path"]).exists()


def test_apply_moves_with_collision(test_files):
    """Test move application with filename collisions."""
    files, source_dir = test_files
    dest_dir = source_dir.parent / "destination"

    # Create the destination directory structure
    dest_dir.mkdir()

    # Create a collision file in the correct category directory
    pdf_category_dir = dest_dir / "Documents/PDFs"
    pdf_category_dir.mkdir(parents=True, exist_ok=True)
    collision_file = pdf_category_dir / "doc1.pdf"
    collision_file.write_text("existing file")

    move_plan = create_move_plan(files, source_dir, dest_dir)

    success, errors, log_file = apply_moves(move_plan)

    assert success
    assert len(errors) == 0

    # Verify collision was resolved - the new file should be named "doc1 (1).pdf"
    resolved_file = pdf_category_dir / "doc1 (1).pdf"
    assert resolved_file.exists(), f"Expected collision resolution file not found: {resolved_file}"

    # Verify the original collision file still exists
    assert collision_file.exists(), f"Original collision file was overwritten: {collision_file}"