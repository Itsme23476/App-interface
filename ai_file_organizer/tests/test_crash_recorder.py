"""A native crash leaves the exact place in logs/crash.log (a user's crash report had nothing we could read).
A child process sets up logging in a temp folder (never your real one), then crashes on purpose on an indexing
thread. Ported from the Mac (pack §6.1, item 2); on Windows the crash is an access violation.
"""
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent("""
    import sys, threading
    from pathlib import Path
    sys.path.insert(0, {root!r})
    from app.core import settings as S
    S.Settings.get_app_data_dir = lambda self: Path({tmp!r})   # (never your folder)
    from app.core.logging_config import setup_logging
    setup_logging()
    def analyze_one_file():
        import faulthandler
        faulthandler._read_null()          # (a real access violation in native code, on an indexing thread)
    t = threading.Thread(target=analyze_one_file, name="index-worker"); t.start(); t.join()
""")


def test_a_native_crash_is_written_to_crash_log():
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([sys.executable, "-c", CHILD.format(root=str(ROOT), tmp=tmp)], capture_output=True,
                           text=True, timeout=60)
        log = Path(tmp) / "logs" / "crash.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        assert r.returncode != 0, "it should have crashed"
        assert "--- Filect " in text and "started" in text, text[:300]
        assert ("access violation" in text or "Segmentation fault" in text) and "analyze_one_file" in text, text[-800:]
