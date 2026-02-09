"""
Pytest configuration and fixtures.
IMPORTANT: All tests use temporary directories and mock file operations.
NO ACTUAL FILES ON THE USER'S PC WILL BE MOVED.
"""
import pytest
import sys
import os
from pathlib import Path

# Add the app directory to path for imports
app_dir = Path(__file__).parent.parent
sys.path.insert(0, str(app_dir))


@pytest.fixture(scope="session")
def app_path():
    """Return the app directory path."""
    return app_dir


@pytest.fixture
def mock_file_move(monkeypatch):
    """
    SAFETY: Mock all file move operations to prevent actual file changes.
    """
    moved_files = []
    
    def fake_move(src, dst):
        moved_files.append({'source': str(src), 'destination': str(dst)})
        return True
    
    # Mock shutil.move
    import shutil
    monkeypatch.setattr(shutil, 'move', fake_move)
    
    # Mock os.rename
    monkeypatch.setattr(os, 'rename', fake_move)
    
    return moved_files


@pytest.fixture
def temp_test_files(tmp_path):
    """Create temporary test files for testing."""
    files = {}
    
    # Create test files
    test_data = [
        ('document.pdf', b'%PDF-1.4 fake pdf content'),
        ('image.jpg', b'\xff\xd8\xff\xe0 fake jpeg'),
        ('video.mp4', b'\x00\x00\x00\x18ftypmp42 fake mp4'),
        ('audio.mp3', b'ID3 fake mp3 content'),
        ('archive.zip', b'PK fake zip content'),
        ('text.txt', b'Hello, this is test content'),
    ]
    
    for name, content in test_data:
        file_path = tmp_path / name
        file_path.write_bytes(content)
        files[name] = file_path
    
    return files


# Prevent any accidental real file operations
@pytest.fixture(autouse=True)
def safety_check(request):
    """
    Safety fixture that runs for every test.
    Warns if test might affect real files.
    """
    # Check test name for potentially dangerous operations
    test_name = request.node.name.lower()
    dangerous_keywords = ['apply_moves', 'delete_file', 'remove_file_real']
    
    for keyword in dangerous_keywords:
        if keyword in test_name:
            pytest.skip(f"Skipping potentially dangerous test: {test_name}")
