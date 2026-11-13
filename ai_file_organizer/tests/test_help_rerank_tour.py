"""Mac 14.17 items 4-7 on Windows. The real main window, off-screen, in a temp app-data folder (tests/real_window.py):
your settings.json and index are never touched; nothing is sent.
  · Help & Feedback at the bottom of the sidebar (Mac 0494a96): an action, not a page; it opens the Need help? form
  · Smart Rerank removed (Mac 97677a2): it only ran with a local OpenAI key, which customers never have
  · the Assistant tour's "Or just say “look at my screen”" line - and saying it really does attach the screen
  · the Need help? form says what to do when sending fails, not the raw error ("verification_failed", "Network
    error: [Errno 11001] getaddrinfo failed"), with the email selectable (Windows' own; found 2026-11-13)"""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from real_window import RealWindow  # noqa: E402


@pytest.fixture(scope="module")
def win():
    rw = RealWindow(agent_was_ready=True)
    yield rw.W
    rw.close()


def _y(w, x):
    return x.mapTo(w, x.rect().topLeft()).y()


# ---- Help & Feedback in the sidebar -------------------------------------------------------------------------------
def test_help_and_feedback_sits_under_settings(win):
    w = win
    b = w.sidebar_help_button
    assert b.text() == "  Help && Feedback"                 # (&& shows one "&"; a single & would vanish)
    assert not b.isCheckable() and b not in w.nav_button_group.buttons() and b not in w.nav_buttons
    assert len(w.nav_buttons) == 7
    settings_btn = w.nav_button_group.button(4)
    assert _y(w, b) >= _y(w, settings_btn) + settings_btn.height() - 1, "under Settings"
    assert _y(w, b) + b.height() <= _y(w, w.avatar_label), "above your account"
    box = b.parentWidget()
    assert box.height() - b.geometry().bottom() <= 24, "at the bottom of the list (after the stretch), as on the Mac"
    assert b.geometry().top() - (settings_btn.geometry().bottom()) > 40, "with the free space above it, not under Settings"


def test_help_and_feedback_opens_the_form_and_stays_on_the_page(win):
    w = win
    page = w.page_stack.currentIndex()
    import app.ui.main_window as mwm
    with mock.patch.object(mwm.HelpDialog, "exec", lambda self: 0) as _:
        opened = []
        real_init = mwm.HelpDialog.__init__

        def rec(self, *a, **k):
            real_init(self, *a, **k)
            opened.append(self)
        with mock.patch.object(mwm.HelpDialog, "__init__", rec):
            w.sidebar_help_button.click()
    assert len(opened) == 1 and w.page_stack.currentIndex() == page
    opened[0].deleteLater()


# ---- Smart Rerank removed ------------------------------------------------------------------------------------------
def test_no_smart_rerank_in_settings(win):
    from PySide6.QtWidgets import QLabel, QPushButton
    w = win
    assert not any("Smart Rerank" in b.text() for b in w.findChildren(QPushButton))
    assert not hasattr(w, "gpt_rerank_button") and not hasattr(w, "on_toggle_gpt_rerank")
    hints = [x.text() for x in w.findChildren(QLabel) if "Spell Check fixes typos" in x.text()]
    assert hints == ["💡 Spell Check fixes typos in your search queries."], hints
    assert any(b.text().startswith("Spell Check") for b in w.findChildren(QPushButton))


def test_the_rerank_settings_and_code_are_gone(tmp_path):
    import json
    from app.core import settings as S
    from app.core.search import SearchService
    assert not hasattr(SearchService, "_gpt_rerank_results")
    assert not hasattr(S.settings, "use_openai_search_rerank") and not hasattr(S.settings, "openai_search_model")
    assert not hasattr(S.Settings, "set_use_openai_search_rerank")
    d = S.settings._cfg_dict()
    assert "use_openai_search_rerank" not in d and "openai_search_model" not in d
    # an older settings.json that still has them loads as before (they're simply ignored)
    old = {"use_openai_search_rerank": True, "openai_search_model": "gpt-4o-mini", "enable_spell_check": True}
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(old), encoding="utf-8")
    with mock.patch.object(S.Settings, "_config_file", lambda self: f), \
            mock.patch.object(S.Settings, "_save_config", lambda self, *a, **k: None):
        s2 = S.Settings()
    assert not hasattr(s2, "use_openai_search_rerank") and s2.enable_spell_check is True


# ---- the Assistant tour's line --------------------------------------------------------------------------------------
def test_the_tour_says_you_can_just_say_it():
    from app.ui import page_tours
    from app.ui.assistant_page import _mentions_screen
    step = next(s for s in page_tours.steps("assistant") if s["title"] == "Point at your screen")
    assert step["description"].endswith("\n• Or just say “look at my screen”"), step["description"]
    assert _mentions_screen("look at my screen"), "saying it must really attach the screen"


# ---- the help form when a message isn't sent -----------------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [
    ("Network error: [Errno 11001] getaddrinfo failed",
     "Couldn't reach the server. Check your internet connection, or email softwaregentofficial@gmail.com directly."),
    ("verification_failed", "Couldn't send your message. Please email softwaregentofficial@gmail.com directly."),
    ("Server error (HTTP 500)", "Couldn't send your message. Please email softwaregentofficial@gmail.com directly."),
    ("rate_limited",
     "Too many messages in a minute. Wait a moment and try again, or email softwaregentofficial@gmail.com."),
])
def test_the_help_form_says_what_to_do(raw, want):
    import app.ui.main_window as mwm
    from PySide6.QtCore import Qt
    d = mwm.HelpDialog(None, default_email="a@b.co")
    try:
        d._on_send_failed(raw)
        assert d.error_label.text() == want, d.error_label.text()
        assert not d.error_label.isHidden()
        assert d.error_label.textInteractionFlags() & Qt.TextSelectableByMouse, "the address can be copied"
        assert d.send_button.isEnabled()
    finally:
        d.deleteLater()
