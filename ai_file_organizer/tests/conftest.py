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


_KEEP_PAGES = []   # every Agents test page, kept until the session ends (see below)


@pytest.fixture(autouse=True)
def _no_real_codex(monkeypatch, request):
    """Codex is installed on the dev PC (inside the Codex app), so a test where Claude can't run would hand its
    task to the REAL Codex — a real chat on your plan. Every test sees Codex as not installed unless it's marked
    @pytest.mark.real_codex (the few that mean to run it)."""
    if request.node.get_closest_marker("real_codex") is None:
        monkeypatch.setenv("FILECT_CODEX_CLI", r"C:\no-codex-in-tests\codex.exe")
    # The settings object holds YOUR saved settings: tests start from the defaults for the agent picks, whatever
    # you've chosen in the app (you picking Codex for new chats broke three tests that assume Claude Code).
    mod = sys.modules.get("app.core.settings")
    if mod is not None:
        for k, v in (("assistant_agent", "claude"), ("assistant_codex_model", ""), ("assistant_codex_effort", "high"),
                     ("assistant_codex_close_app", False)):
            monkeypatch.setattr(mod.settings, k, v, raising=False)
    yield


@pytest.fixture(autouse=True)
def _agents_pages_outlive_their_threads(monkeypatch):
    """An Agents page starts background loaders (past chats, every chat, models) that hold a reference to it.
    A PARENTLESS test page whose last reference is dropped by such a thread is destroyed ON that thread → a
    Qt access violation that kills the whole run. In the app the main window owns the page, so it can't
    happen there; in tests, keep every page alive until the end of the session."""
    mod = sys.modules.get("app.ui.assistant_page")
    if mod is None:
        yield
        return
    orig = mod.AssistantPage.__init__

    def keep(self, *a, **k):
        orig(self, *a, **k)
        _KEEP_PAGES.append(self)
    monkeypatch.setattr(mod.AssistantPage, "__init__", keep)
    yield


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


def pytest_configure(config):
    config.addinivalue_line("markers", "real_codex: runs the real Codex CLI (a real chat on your ChatGPT plan)")
