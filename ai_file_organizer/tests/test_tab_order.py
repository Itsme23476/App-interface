"""The sidebar's order, and which tab Filect opens on (Windows port of the Mac tests/test_tab_order.py, pack §5.2).
The real main window, off-screen, in a temp app-data folder (tests/real_window.py): your settings.json and index are
never touched.
  · Assistant, Agents, Voice, Organize, Search, Analyze Files, Settings — and every button still opens ITS page (the
    page numbers never change: Windows' Voice is 3 and Settings 4)
  · it opens on the Assistant once Claude Code or Codex was set up at the last check, on Search before that, and on
    Search if the Assistant page couldn't load
  · the "was an agent set up" flag is written only when it changes, and a failed check keeps what we knew"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from real_window import RealWindow, pump  # noqa: E402

ORDER = ["Assistant", "Agents", "Voice", "Organize", "Search", "Analyze Files", "Settings"]
PAGE = {"Assistant": 6, "Agents": 5, "Voice": 3, "Organize": 1, "Search": 0, "Analyze Files": 2, "Settings": 4}


def _name(b):
    return b.text().strip()


def _on_screen(w):
    """The sidebar's buttons top to bottom, as you see them."""
    return [_name(b) for b in sorted(w.nav_buttons, key=lambda b: b.mapTo(w, b.rect().topLeft()).y())]


def _checked(w):
    return [_name(b) for b in w.nav_buttons if b.isChecked()]


@pytest.fixture
def ready():
    rw = RealWindow(agent_was_ready=True)
    yield rw.W
    rw.close()


def test_the_order_on_screen(ready):
    assert _on_screen(ready) == ORDER, _on_screen(ready)


def test_opens_on_the_assistant_once_an_agent_is_set_up(ready):
    w = ready
    assert w.page_stack.currentWidget() is w.live_page and w.page_stack.currentIndex() == 6
    assert _checked(w) == ["Assistant"], _checked(w)


def test_opens_on_search_before_that():
    rw = RealWindow(agent_was_ready=False)
    try:
        w = rw.W
        assert w.page_stack.currentIndex() == 0 and _checked(w) == ["Search"], (w.page_stack.currentIndex(), _checked(w))
    finally:
        rw.close()


def test_opens_on_search_if_the_assistant_page_failed_to_load(monkeypatch):
    import app.ui.main_window as mwm
    from PySide6.QtWidgets import QLabel

    def broken(win):                                       # (what setup_live_page does when LivePage can't be made)
        win.live_page = QLabel("Filect Assistant couldn't load. Please restart Filect.")
        win.page_stack.addWidget(win.live_page)
    monkeypatch.setattr(mwm.MainWindow, "setup_live_page", broken)
    rw = RealWindow(agent_was_ready=True)
    try:
        w = rw.W
        assert w.page_stack.currentIndex() == 0 and _checked(w) == ["Search"], (w.page_stack.currentIndex(), _checked(w))
    finally:
        rw.close()


def test_every_tab_opens_its_own_page_and_lights_up_alone(ready):
    w = ready
    for b in sorted(w.nav_buttons, key=lambda b: b.mapTo(w, b.rect().topLeft()).y()):
        b.click()
        pump(0.03)
        assert w.page_stack.currentIndex() == PAGE[_name(b)] and _checked(w) == [_name(b)], \
            (_name(b), w.page_stack.currentIndex(), _checked(w))
    assert w.page_stack.widget(6) is w.live_page and w.page_stack.widget(5) is w.assistant_page
    assert w.page_stack.widget(1) is w.organize_page


def test_going_to_a_page_from_elsewhere_finds_the_right_button(ready):
    w = ready
    for name, i in PAGE.items():          # (the guide, the tours, dictation, the account card: by page number)
        assert _name(w.nav_button_group.button(i)) == name
        assert w.nav_buttons[i] is w.nav_button_group.button(i), (name, i)


def test_it_remembers_whether_an_agent_is_set_up(ready, monkeypatch):
    from app.core.settings import settings
    p = ready.assistant_page
    saves = []
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: saves.append(1))   # (still never your file)
    monkeypatch.setattr(settings, "agent_was_ready", False)
    p._publish_agents({"claude": "install", "codex": "signin"})
    assert not settings.agent_was_ready and not saves, "nothing changed, nothing to save"
    p._publish_agents({"claude": "signin", "codex": "ready"})
    assert settings.agent_was_ready and len(saves) == 1
    p._publish_agents({"claude": "ready", "codex": "ready"})
    assert len(saves) == 1, "already true: no write on every check"
    p._publish_agents({"claude": "signin", "codex": "signin"})
    assert not settings.agent_was_ready and len(saves) == 2
    p._publish_agents(None)                                # (the check itself failed: keep what we knew)
    assert not settings.agent_was_ready and len(saves) == 2
