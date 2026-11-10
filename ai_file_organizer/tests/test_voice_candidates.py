"""Resume-by-name — Windows port of the Mac tests/test_voice_candidates.py. _build_voice_candidates must (a) filter the
router's own junk sessions, and (b) surface an OLD named conversation (Hyper3D) that's far outside the newest ones —
the exact miss seen on the Mac. Cost-free: a fake list_sessions (Codex's own index faked too), never your real chats.
Windows: the candidates come from the chat lists the page loads off the UI thread (_recent / _all_sessions — the
Mac's build read list_sessions per request, which froze its window), so the fake list goes through the real loader.
(The Mac file's check ran as main(), which pytest never collected; here it's a test.)
Run: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_voice_candidates.py -q"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.core import agent_runner as ar  # noqa: E402
from app.core import agent_sessions as asess  # noqa: E402
from app.core import codex_runner as crun  # noqa: E402
from app.core import supabase_client as sc  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402

_PAGES = []


class _NeverRunner:
    def __init__(self, *a, **k):
        pass

    async def run(self, *a, **k):
        raise RuntimeError("no real agents in tests")


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch, tmp_path):
    """Never the user's settings / chats / app-data files; no sound, no analytics, no real agents."""
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    for k, v in (("assistant_bypass", False), ("assistant_model", ""), ("assistant_effort", ""),
                 ("assistant_agent", "claude"), ("assistant_auto_open", True), ("live_speak", False)):
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(sc, "track", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_track", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_play_chime", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap.AssistantPage, "_show_idle_overlays", lambda self: None)
    monkeypatch.setattr(ap.AssistantPage, "_load_recent", lambda self: None)
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", str(tmp_path / "Filect Assistant"))
    monkeypatch.setattr(asess, "_touched_path", lambda: tmp_path / "assistant_sessions.json")
    monkeypatch.setattr(asess, "_projects_root", lambda: tmp_path / "claude-projects")
    monkeypatch.setattr(ar, "_models_cache_path", lambda: tmp_path / "claude_models.json")
    monkeypatch.setattr(ar, "AgentRunner", _NeverRunner)
    monkeypatch.setattr(crun, "CodexRunner", _NeverRunner)
    monkeypatch.setattr(crun, "codex_threads", lambda *a, **k: [])          # (Codex's own index: never read)
    orig = ap.AssistantPage.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        _PAGES.append(self)
    monkeypatch.setattr(ap.AssistantPage, "__init__", track)
    yield
    while _PAGES:
        _PAGES.pop().shutdown()


def _pump(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


def mk(i, title, preview="", mtime=0.0):
    return asess.SessionInfo(id=i, cwd=r"C:\x", title=title, preview=preview, mtime=mtime, project="x")


class _MW:
    pass


def _fake_sessions():
    fake = [mk("j1", "Current conversation: (none) Other conversations: 0. Filect", "Spoken command: 'x'", 300),
            mk("j2", "Current conversation: Build One Page Website Other conv", "Spoken command: 'y'", 299),
            mk("stub", "This session is being continued from a previous conversation", "", 298)]
    fake += [mk(f"r{n}", f"Recent Task {n}", f"do recent thing {n}", 200 - n) for n in range(15)]
    fake.append(mk("hd", "Set up Hyper3D MCP in this client (Codex or Claude Code)",
                   "set up hyper3d mcp server", 50))        # OLD — far outside the 12 newest the router gets
    fake.sort(key=lambda s: s.mtime, reverse=True)
    return fake


def _check(cands):
    titles = [c["title"] for c in cands]
    assert not any(t.lower().startswith("current conversation:") for t in titles), f"router junk leaked: {titles}"
    assert not any("continued from a previous" in t.lower() for t in titles), "stub leaked"
    assert any("Hyper3D" in t for t in titles), f"Hyper3D not reachable by name! got: {titles}"
    assert len(titles) == len(set(titles)), "duplicate candidates"
    hd = next(c for c in cands if "Hyper3D" in c["title"])
    assert hd["id"] == "hd" and hd["kind"] == "saved" and hd["cwd"] == r"C:\x", hd


def test_voice_candidates(monkeypatch):
    fake = _fake_sessions()
    monkeypatch.setattr(asess, "list_sessions", lambda limit=500, cwd=None: fake[:limit])
    page = ap.AssistantPage(_MW())
    page._sessions = {}
    page._load_all_sessions()                                 # the real loader (off the UI thread), fake list
    assert _pump(lambda: page._all_sessions, 5), "the chat list loaded"
    assert {s.id for s in page._all_sessions} == {s.id for s in fake} - {"j1", "j2", "stub"}, "junk filtered on load"
    _check(page._build_voice_candidates("use my hyper3d mcp conversation and spin it up"))
    # junk that reaches the lists another way (Filect's own chats) is filtered when the candidates are built, too
    page._recent, page._all_sessions = list(fake), []
    cands = page._build_voice_candidates("use my hyper3d mcp conversation and spin it up")
    _check(cands)
    assert len([c for c in cands if c["title"].startswith("Recent Task")]) == 12, "the 12 newest + what you named"
    # what you didn't name: just the newest ones (Hyper3D stays out — it's old)
    assert not any("Hyper3D" in c["title"] for c in page._build_voice_candidates("make me a landing page"))
