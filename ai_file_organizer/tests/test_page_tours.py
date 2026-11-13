"""Each page's "See what it can do" tour, in the real main window, off-screen (Windows' own fonts), temp app data.
TOUR_SHOTS=<folder> saves every step as a picture (the window with its dim + the panel where it sits).

Ported from the Mac (tests/test_page_tours.py, 8 tests) with Windows' pages, keys and words, plus: no Mac words in any
tour, the keys come from what you've set (Quick Search, the agent shortcut), and the real settings.json is untouched.
"""
import hashlib
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))          # (real_window.py, next to this file)

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.ui import page_tours  # noqa: E402
from app.ui.theme_manager import theme_manager  # noqa: E402
from real_window import RealWindow, pump as _pump  # noqa: E402

REAL_CFG = Path(os.environ.get("APPDATA", "")) / "ai-file-organizer" / "settings.json"
MAC_WORDS = re.compile(r"\b(Fn|fn|Option|notch|Mac|Cmd|Command|Control)\b|[⌃⇧⌥⌘]")
SHOTS = os.environ.get("TOUR_SHOTS")
NAMES = list(page_tours.TOURS)
W = None
TRACKED = []


def _hash():
    return hashlib.sha256(REAL_CFG.read_bytes()).hexdigest() if REAL_CFG.exists() else None


@pytest.fixture(scope="module", autouse=True)
def _window():
    global W, TRACKED
    before = _hash()
    rw = RealWindow((1300, 860))
    W, TRACKED = rw.W, rw.tracked
    yield
    rw.close()
    assert _hash() == before, "the real settings.json changed"


def _button(name):
    """The page's own "See what it can do"."""
    if name == "assistant":
        return W.live_page.tour_btn
    if name == "agents":
        return W.assistant_page.view.composer.tour_btn
    return W.findChild(QPushButton, f"{name}TourButton")


def _open(name):
    page = page_tours.TOURS[name]()[0]
    W.nav_button_group.button(page).click()
    _pump(0.05)
    b = _button(name)
    assert b is not None and b.isVisible(), f"{name}: no visible tour button on its page"
    b.click()
    _pump(0.1)
    t = W._page_tour
    assert t is not None and t.isVisible() and t._tour == name, name
    return t, page


def _paths(h):
    return list(h) if isinstance(h, (list, tuple)) else [h] if h else []


def _shot(t, name, i):
    img = W.grab()
    p = QPainter(img)
    p.drawPixmap(t.frameGeometry().topLeft() - W.geometry().topLeft(), t.grab())
    p.end()
    img.save(str(Path(SHOTS) / f"{name}_{i + 1}_{theme_manager.current_theme}.png"))


def test_every_page_has_a_tour_and_every_path_is_real():
    assert sorted(NAMES) == ["agents", "assistant", "index", "organize", "search", "settings", "voice"]
    for name in NAMES:
        t, _page = _open(name)
        for i, s in enumerate(t.steps):
            for path in _paths(s["highlight"]):
                assert isinstance(t._resolve(path), QWidget), f"{name} step {i + 1}: {path} isn't a widget (typo?)"
        t._remind_later()
        _pump(0.05)


def test_windows_pages_keys_and_words():
    assert {n: page_tours.TOURS[n]()[0] for n in NAMES} == {"search": 0, "organize": 1, "index": 2, "voice": 3,
                                                           "settings": 4, "agents": 5, "assistant": 6}
    for name in NAMES:
        for s in page_tours.steps(name):
            text = s["title"] + " " + s["description"]
            assert not MAC_WORDS.search(text), (name, text)
            art = s["art"]
            if isinstance(art, tuple) and art[0] == "keys":
                assert not any(MAC_WORDS.search(k) for k in art[1]), (name, art)
    titles = {n: [s["title"] for s in page_tours.steps(n)] for n in NAMES}
    mute = [s for s in page_tours.steps("assistant") if s["title"] == "Mute it"]
    assert titles["assistant"][1] == "Mute it" and mute[0]["art"] == ("keys", ["Shift", "→", "Ctrl", "+", "Win"])
    assert "Shift, then Ctrl + Win" in mute[0]["description"], mute[0]["description"]
    assert len(titles["agents"]) == 6, "Windows' agent shortcut has no collecting"
    assert page_tours._keys("ctrl+alt+h") == ["Ctrl", "+", "Alt", "+", "H"]
    assert page_tours._keys("") == ["Ctrl", "+", "Alt", "+", "H"]


def test_the_keys_come_from_your_shortcuts(monkeypatch):
    monkeypatch.setattr(settings, "quick_search_shortcut", "ctrl+shift+space")
    monkeypatch.setattr(settings, "assistant_voice_shortcut", "ctrl+win+alt")
    search = page_tours.steps("search")[2]
    assert search["art"] == ("keys", ["Ctrl", "+", "Shift", "+", "Space"]), search["art"]
    agents = page_tours.steps("agents")
    assert agents[1]["art"] == ("keys", ["Ctrl", "+", "Win", "+", "Alt"]) and "Hold Ctrl + Win + Alt" in agents[1]["description"]
    assert "hold Ctrl + Win + Alt" in agents[2]["description"]


def test_every_step_shows_its_page_and_dims_the_app():
    for name in NAMES:
        t, page = _open(name)
        assert all(not c.isVisible() for c in t._chips.values()), "no sections on a page tour"
        assert t.remind_btn.text() == "✕ Close" and "Esc to close" in t.keyboard_hint.text()
        for i in range(len(t.steps)):
            t.current_step = i
            t._update_step()
            _pump(0.03)
            s = t.steps[i]
            assert W.page_stack.currentIndex() == page and W.nav_button_group.button(page).isChecked(), (name, i)
            assert not t.skip_btn.isVisible() and not t.try_btn.isVisible(), (name, i)
            assert t.spotlight.isVisible() and t.spotlight.geometry() == W.rect(), (name, i)
            showing = [w for w in map(t._resolve, _paths(s["highlight"])) if w.isVisible()]
            if showing:
                r = t.spotlight.spotlight_rect
                for w in showing:
                    wr = w.rect().translated(w.mapTo(W, QPoint(0, 0)))
                    assert r is not None and r.contains(wr), (name, i, s["highlight"], r, wr)
            else:
                assert t.spotlight.spotlight_rect is None, (name, i)
            if s.get("sub_tab") is not None:
                assert W.organize_page.content_stack.currentIndex() == s["sub_tab"], (name, i)
            assert t.next_btn.text() == ("Done" if i == len(t.steps) - 1 else "Next")
        t._remind_later()
        _pump(0.05)


def test_everything_fits_in_both_themes():
    from PySide6.QtGui import QFontDatabase
    assert "Segoe UI" in QFontDatabase.families(), "the fit check needs Windows' real fonts (conftest QT_QPA_FONTDIR)"
    start = theme_manager.current_theme
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        for name in NAMES:
            t, _page = _open(name)
            for i in range(len(t.steps)):
                t.current_step = i
                t._update_step()
                _pump(0.15 if SHOTS else 0.02)
                lay = t.container.layout()
                assert lay.minimumSize().height() <= t.container.height(), (name, i, lay.minimumSize().height())
                for w in t.container.findChildren(QLabel):
                    if w.isVisible() and w.wordWrap() and w.text():
                        assert w.heightForWidth(w.width()) <= w.height() + 1, (name, i, w.text()[:40], w.height())
                t.grab()                                  # (every drawing has to paint)
                if SHOTS:
                    _shot(t, name, i)
            t._remind_later()
            _pump(0.05)
        W._on_theme_toggle()
        _pump(0.1)
    assert theme_manager.current_theme == start


def test_close_esc_and_done_leave_the_first_run_guide_alone():
    W.organize_page._switch_tab(0)
    for how in ("close", "esc", "done"):
        TRACKED.clear()
        t, page = _open("organize")
        if how == "close":
            t.remind_btn.click()
        elif how == "esc":
            t.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        else:
            for _ in range(len(t.steps)):
                t.next_btn.click()
        _pump(0.1)
        assert not t.isVisible() and not t.spotlight.isVisible() and W._page_tour is None, how
        assert settings.has_completed_onboarding and W.page_stack.currentIndex() == page, how
        assert getattr(t, "confetti", None) is None, "no confetti for a page tour"
        events = [e for e, _ in TRACKED if e.startswith(("page_tour", "onboarding"))]
        assert events[0] == "page_tour_opened" and "onboarding_step_viewed" not in events, events
        assert events[-1] == ("page_tour_completed" if how == "done" else "page_tour_closed"), events


def test_opened_once_the_button_goes_quiet_and_its_remembered():
    settings.page_tours_seen = []
    settings.live_tour_seen = settings.agents_tour_seen = False
    for _ in range(2):                                    # (a theme change restyles every button from the settings)
        W._on_theme_toggle()
        _pump(0.05)
    W.assistant_page.view.set_tour_seen(False)            # (the Agents page keeps its own glow, set at start)
    from app.ui.live_page import style_tour_button
    style_tour_button(W.live_page.tour_btn, False, W.live_page._c)
    for name in NAMES:
        W.nav_button_group.button(page_tours.TOURS[name]()[0]).click()
        _pump(0.03)
        b = _button(name)
        if name == "agents":                              # (Windows' Agents page: its own "New" badge + glow)
            assert W.assistant_page.view.composer.new_badge.isVisible() and b.graphicsEffect() is not None
        else:
            assert "New here?" in b.text() and b.graphicsEffect() is not None, (name, b.text())
        t, _page = _open(name)
        assert page_tours.is_seen(name) and b.graphicsEffect() is None, name
        if name == "agents":
            assert not W.assistant_page.view.composer.new_badge.isVisible()
        else:
            assert b.text().strip() == "See what it can do", (name, b.text())
        t._remind_later()
        _pump(0.03)
    assert sorted(settings.page_tours_seen) == ["index", "organize", "search", "settings", "voice"]
    assert settings.live_tour_seen and settings.agents_tour_seen


def test_one_tour_at_a_time():
    a, _ = _open("voice")
    b, _ = _open("settings")
    _pump(0.05)
    assert not a.isVisible() and b.isVisible() and W._page_tour is b
    dims = [w for w in W.findChildren(QWidget) if type(w).__name__ == "SpotlightOverlay" and w.isVisible()]
    assert len(dims) == 1, f"one dim, not {len(dims)}"
    b._remind_later()


def test_a_tour_never_turns_off_the_assistant_you_started():
    calls = []
    lp = W.live_page
    real_stop, lp.stop = lp.stop, lambda: calls.append(1)
    try:
        t, _page = _open("assistant")
        lp.listener.on = True                             # (you clicked Start listening during the tour)
        t._go_next()
        t._go_next()
        assert calls == [], calls
        t._remind_later()
    finally:
        lp.stop = real_stop
        lp.listener.on = False


def test_the_search_button_hides_with_the_hints_after_a_search():
    W.nav_button_group.button(0).click()
    _pump(0.05)
    assert W.search_tour_wrap.isVisible()
    W._switch_to_results_view()
    _pump(0.6)
    assert not W.search_tour_wrap.isVisible() and not W.voice_hint_row.isVisible()
    W._switch_to_landing_view()
    _pump(0.6)
    assert W.search_tour_wrap.isVisible() and W.voice_hint_row.isVisible()


def test_the_first_step_is_placed_for_its_target_too():
    """Showing the panel mustn't undo where the first step put it: every tour's step 1 sat in the bottom-right corner
    whatever it pointed at (live 2026-11-12, at Windows' default 1200 x 800: 54% of the search box hidden)."""
    W.resize(1200, 800)
    _pump(0.1)
    try:
        for name in NAMES:
            t, _page = _open(name)
            where = t.pos()
            t._update_spotlight(t._highlight)                  # (where step 1 belongs, worked out again)
            assert t.pos() == where, (name, "step 1 was moved when the panel showed", where, t.pos())
            t._remind_later()
            _pump(0.05)
    finally:
        W.resize(1300, 860)
        _pump(0.1)


def test_a_tour_has_the_keyboard_as_soon_as_it_opens():
    """Opened from its button (a click in the main window), the tour must get the keyboard: it says "Esc to close"
    and "← →". Seen live 2026-11-12: Esc right after opening went to the main window, the tour stayed open - real
    Windows shows a Tool window without activating it. (Off screen, Qt activates every window it shows, so only the
    live run catches that part; this checks the keys once it has the keyboard.)"""
    from PySide6.QtTest import QTest
    W.activateWindow()
    _pump(0.05)
    t, _page = _open("index")
    _pump(0.1)
    assert QApplication.activeWindow() is t, ("the tour isn't the active window", QApplication.activeWindow())
    QTest.keyClick(QApplication.focusWidget() or QApplication.activeWindow(), Qt.Key_Right)
    _pump(0.02)
    assert t.current_step == 1
    QTest.keyClick(QApplication.focusWidget() or QApplication.activeWindow(), Qt.Key_Escape)
    _pump(0.05)
    assert W._page_tour is None and not t.isVisible()
