"""The first-run guide, in the real main window, off-screen (Windows' own fonts), with a temp app-data folder.

Ported from the Mac (tests/test_onboarding.py, 19 tests) with the Windows keys, words and page numbers, plus:
no Mac words on any screen, the panel isn't "always on top" of other apps, replaying it from Settings never opens a
second guide, and the real settings.json is untouched at the end.
"""
import hashlib
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))          # (real_window.py, next to this file)

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.ui.onboarding import OnboardingOverlay  # noqa: E402
from app.ui.theme_manager import theme_manager  # noqa: E402
from real_window import RealWindow, pump as _pump  # noqa: E402

READY, NOT_READY = {"claude": "ready", "codex": "install"}, {"claude": "install", "codex": "signin"}
MAC_WORDS = re.compile(r"\b(Fn|fn|Option|notch|Mac|Cmd|Command)\b|[⌃⇧⌥⌘]|\bControl\b(?! your entire)")
REAL_CFG = Path(os.environ.get("APPDATA", "")) / "ai-file-organizer" / "settings.json"
W = None
TRACKED = []


def _hash():
    return hashlib.sha256(REAL_CFG.read_bytes()).hexdigest() if REAL_CFG.exists() else None


@pytest.fixture(scope="module", autouse=True)
def _window():
    global W, TRACKED
    before = _hash()
    rw = RealWindow()
    W, TRACKED = rw.W, rw.tracked
    yield
    rw.close()
    assert _hash() == before, "the real settings.json changed"


def _guide(state=READY):
    W.assistant_page._agents_state = state
    g = OnboardingOverlay(W)
    g.show()
    _pump()
    return g


def _titles(g):
    out = []
    for i in range(len(g.steps)):
        g.current_step = i
        g._update_step()
        out.append(g.title_label.text())
    return out


def _done(g):
    g.continue_btn.hide()
    if g.spotlight:
        g.spotlight.hide()
    g.anim_widget.stop()
    g.hide()


def _chip(g):
    return [k for k, b in g._chips.items() if b.isChecked()]


def _resolve(path):
    obj = W
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def test_the_screens_in_order():
    g = _guide()
    assert len(g.steps) == 9 == len(g._STEP_SLUGS), len(g.steps)
    assert [s.get("section") for s in g.steps] == [None, "agents", "agents", "voice", "voice", "files", "files", "files", None]
    t = _titles(g)
    assert t[0] == "Control your entire computer by voice" and t[-1] == "You're ready", t
    assert t[3] == "Hold Ctrl + Win and talk", t
    _done(g)


def test_windows_words_and_keys_on_every_screen():
    for state in (READY, NOT_READY):
        g = _guide(state)
        for i in range(len(g.steps)):
            g.current_step = i
            g._update_step()
            shown = [w.text() for w in g.container.findChildren(QLabel) + g.container.findChildren(QPushButton)
                     if w.isVisible() and w.text()]
            shown.append(g.try_box.placeholderText())
            bad = [t for t in shown if MAC_WORDS.search(t)]
            assert not bad, (i, bad)
        _done(g)
    g = _guide()
    keys = {g._STEP_SLUGS[i]: g.steps[i]["description"] for i in range(len(g.steps))}
    assert "Ctrl + Win + Shift" in keys["search"] and "Ctrl + Win + Alt" in keys["organize"]
    assert "Ctrl + Win" in keys["done"] and "card at the top of your screen" in keys["assistant"]
    assert "Ctrl + Win" in g.try_box.placeholderText()
    _done(g)


def test_every_screen_shows_its_page_tab_and_spotlight():
    g = _guide()
    for i, s in enumerate(g.steps):
        g.current_step = i
        g._update_step()
        _pump(0.03)
        if s["nav_index"] is not None:
            assert W.page_stack.currentIndex() == s["nav_index"], (i, W.page_stack.currentIndex())
            assert W.nav_button_group.button(s["nav_index"]).isChecked(), i
        if s["highlight"]:
            assert _resolve(s["highlight"]).isVisible(), (i, s["highlight"])
            assert g.spotlight.isVisible() and g.spotlight.spotlight_rect is not None, i
        else:
            assert g.spotlight.isVisible() and g.spotlight.spotlight_rect is None, i
        assert g.spotlight.geometry() == W.rect(), i      # (the whole app is dimmed on every screen)
    assert g.steps[3]["nav_index"] == 3 == W.page_stack.indexOf(W.page_stack.widget(3)), "Voice is page 3 on Windows"
    _done(g)


def test_the_organize_screen_opens_organize_now():
    g = _guide()
    W.organize_page._switch_tab(1)                        # (you were on Auto-Organize)
    g.current_step = g._STEP_SLUGS.index("organize")
    g._update_step()
    _pump(0.05)
    assert W.page_stack.currentIndex() == 1 and W.organize_page.content_stack.currentIndex() == 0
    assert W.organize_page.dest_card.isVisible() and g.spotlight.spotlight_rect is not None
    assert "Ctrl + Win + Alt" in g.desc_label.text()
    _done(g)


def test_try_it_and_back_dims_again():
    g = _guide()
    g._chips["files"].click()
    g._try_it()
    assert not g.spotlight.isVisible() and g.continue_btn.isVisible()
    g._restore_from_try()
    _pump(0.02)
    assert g.spotlight.isVisible() and g.spotlight.spotlight_rect is not None and not g.continue_btn.isVisible()
    _done(g)


def test_everything_fits_on_every_screen_in_both_themes():
    from PySide6.QtGui import QFontDatabase
    assert "Segoe UI" in QFontDatabase.families(), "the fit check needs Windows' real fonts (conftest QT_QPA_FONTDIR)"
    start = theme_manager.get_colors()["surface"]
    for _ in range(2):
        for state in (READY, NOT_READY):
            g = _guide(state)
            g.try_box.setPlainText("words")
            for i in range(len(g.steps)):
                g.current_step = i
                g._update_step()
                _pump(0.02)
                lay = g.container.layout()
                assert lay.minimumSize().height() <= g.container.height(), (i, lay.minimumSize().height())
                for w in g.container.findChildren(QLabel):
                    if w.isVisible() and w.wordWrap() and w.text():
                        assert w.heightForWidth(w.width()) <= w.height() + 1, (i, w.text()[:40], w.height())
                for b in g.container.findChildren(QPushButton):
                    if b.isVisible() and b.text():
                        assert b.sizeHint().width() <= b.width() + 1, (i, b.text(), b.sizeHint(), b.size())
                g.grab()                                  # (every drawing has to paint)
            _done(g)
        W._on_theme_toggle()
        _pump(0.05)
    assert theme_manager.get_colors()["surface"] == start


def test_back_next_and_the_arrow_keys():
    g = _guide()
    g._go_next()
    assert g.current_step == 1 and _chip(g) == ["agents"]
    g.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
    assert g.current_step == 2
    g.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Left, Qt.NoModifier))
    assert g.current_step == 1
    g._go_back()
    g._go_back()
    assert g.current_step == 0 and _chip(g) == [] and not g.back_btn.isVisible()
    _done(g)


def test_the_section_chips_jump():
    g = _guide()
    for key, first in (("files", 5), ("agents", 1), ("voice", 3)):
        g._chips[key].click()
        assert g.current_step == first and _chip(g) == [key], (key, g.current_step, _chip(g))
    _done(g)


def test_skip_this_section():
    g = _guide()
    assert not g.skip_btn.isVisible()
    g._chips["agents"].click()
    g.skip_btn.click()
    assert g.current_step == 3 and _chip(g) == ["voice"], g.current_step
    g.skip_btn.click()
    assert g.current_step == 5 and _chip(g) == ["files"]
    g.skip_btn.click()
    assert g.current_step == len(g.steps) - 1 and not g.skip_btn.isVisible()
    _done(g)


def test_no_agent_set_up_gets_setup_and_never_blocks():
    g = _guide(NOT_READY)
    calls = []
    real, W.assistant_page.setup_agent = W.assistant_page.setup_agent, calls.append
    try:
        g._chips["agents"].click()
        assert g.title_label.text() == "Set up your agent" and g.setup_row.isVisible() and not g.try_btn.isVisible()
        assert g.next_btn.text() == "Later" and g.next_btn.isEnabled()
        for b in g.setup_row.findChildren(QPushButton):
            b.click()
        assert calls == ["claude", "codex"], calls
        g._go_next()
        assert g.current_step == 2 and not g.setup_row.isVisible(), "Later has to move on"
    finally:
        W.assistant_page.setup_agent = real
        _done(g)


def test_it_notices_when_setup_finishes():
    g = _guide(NOT_READY)
    g._chips["agents"].click()
    assert g.setup_row.isVisible()
    W.assistant_page._agents_state = READY
    W.assistant_page.agents_setup_changed.emit(READY)
    _pump(0.05)
    assert not g.setup_row.isVisible() and g.title_label.text() == "Talk, and it gets done" and g.try_btn.isVisible()
    _done(g)


def test_before_the_first_check_it_uses_what_was_saved():
    settings.agent_was_ready = True
    g = _guide(None)
    g._chips["agents"].click()
    assert not g.setup_row.isVisible()
    _done(g)
    settings.agent_was_ready = False
    g = _guide(None)
    g._chips["agents"].click()
    assert g.setup_row.isVisible()
    _done(g)


def test_the_check_saves_whether_an_agent_was_ready():
    ap = W.assistant_page
    settings.agent_was_ready = False
    ap._publish_agents(dict(READY))
    assert settings.agent_was_ready is True
    ap._publish_agents(dict(NOT_READY))
    assert settings.agent_was_ready is False


def test_an_old_guide_doesnt_react_after_its_closed():
    g = _guide(NOT_READY)
    g._chips["agents"].click()
    _done(g)
    n = len(TRACKED)
    W.assistant_page.agents_setup_changed.emit(READY)
    _pump(0.05)
    assert len(TRACKED) == n, "a hidden guide must not redraw"


def test_the_dictation_box():
    g = _guide()
    g._chips["voice"].click()
    assert g.try_box.isVisible() and not g.anim_widget.isVisible() and not g.try_ok.isVisible()
    g.try_box.setPlainText("hello there")
    _pump(0.02)
    assert g.try_ok.isVisible()
    g._go_next()
    assert not g.try_box.isVisible() and g.vocab_panel.isVisible()
    g._go_back()
    assert g.try_box.toPlainText() == "hello there" and g.try_ok.isVisible()
    g.try_box.clear()
    _pump(0.02)
    assert not g.try_ok.isVisible()
    _done(g)


def _fake_listener(on):
    calls = []
    lp, W.live_page = W.live_page, SimpleNamespace(listener=SimpleNamespace(on=on), stop=lambda: calls.append(1))
    return lp, calls


def test_moving_on_turns_off_what_the_tour_started():
    g = _guide()
    real, calls = _fake_listener(False)
    try:
        g._was_listening = False
        W.live_page.listener.on = True                    # (you tried it on the Assistant screen...)
        g.current_step = 2
        g._update_step()                                  # (...and moved on)
        assert calls == [1], calls
    finally:
        W.live_page = real
        _done(g)


def test_it_leaves_a_session_you_already_had_alone():
    real, calls = _fake_listener(True)
    try:
        g = OnboardingOverlay.__new__(OnboardingOverlay)
        g.main_window, g._was_listening = W, True
        OnboardingOverlay._stop_listening_if_tour_started_it(g)
        assert calls == [], calls
    finally:
        W.live_page = real


def test_finishing_lands_on_the_right_tab():
    for state, page, tab in ((READY, 6, "Assistant"), (NOT_READY, 0, "Search")):
        settings.has_completed_onboarding = False
        g = _guide(state)
        g.finished_onboarding.connect(W._on_onboarding_finished)
        g.current_step = len(g.steps) - 1
        g._update_step()
        g._go_next()
        _pump(0.05)
        assert W.page_stack.currentIndex() == page, (page, W.page_stack.currentIndex())
        assert [b.text().strip() for b in W.nav_buttons if b.isChecked()] == [tab]
        assert settings.has_completed_onboarding
        if getattr(g, "confetti", None) is not None:
            g.confetti.hide()


def test_the_panel_gets_out_of_the_way():
    W.resize(1200, 800)                                   # (Windows' smallest window)
    _pump(0.05)
    g = _guide()

    def covered(attr):
        o = _resolve(attr)
        t = QRect(o.mapToGlobal(QPoint(0, 0)), o.size())
        i = g.frameGeometry().intersected(t)
        return i.width() * i.height() / (t.width() * t.height())

    p0 = g.pos()
    for step, attr in ((5, "drop_zone"), (6, "search_input"), (7, "organize_page.dest_card")):
        g.current_step = step
        g._update_step()
        _pump(0.05)
        assert g.pos() != p0 and covered(attr) < 0.35, (attr, g.pos(), covered(attr))
    g.current_step = 8
    g._update_step()
    assert g.pos() == p0, "back to its usual corner when it isn't pointing at anything"
    g._dragged = True
    g.move(100, 100)
    g.current_step = 6
    g._update_step()
    assert g.pos() == QPoint(100, 100), "dragged: it stays where you put it"
    _done(g)
    W.resize(1250, 860)
    _pump(0.05)


def test_continue_tour_fits_and_follows_the_window():
    W.resize(1200, 800)
    _pump(0.05)
    g = _guide()
    g._chips["files"].click()
    g._try_it()
    b = g.continue_btn
    assert b.isVisible() and b.sizeHint().width() <= b.width(), ("text cut off", b.sizeHint(), b.size())
    img = b.grab().toImage()
    px = img.pixelColor(12 * img.width() // b.width(), img.height() // 2)
    assert px.red() < 160 and px.blue() > 200, ("not the guide's purple", px.name())
    W.resize(1500, 950)                                   # (maximized, or the window made bigger)
    _pump(0.3)
    assert b.geometry().bottomRight() == QPoint(W.width() - 21, W.height() - 21), (b.geometry(), W.size())
    W.resize(1200, 800)
    _pump(0.3)
    assert W.rect().contains(b.geometry()), ("smaller window: the button went out of it", b.geometry())
    g._restore_from_try()
    W.resize(1250, 860)
    _pump(0.3)
    t, p = W.drop_zone, W.drop_zone.mapTo(W, QPoint(0, 0))
    assert g.spotlight.geometry() == W.rect(), g.spotlight.geometry()
    assert g.spotlight.spotlight_rect == QRect(p.x() - 10, p.y() - 10, t.width() + 20, t.height() + 20), "cutout left behind"
    _done(g)


def test_each_screen_reports_its_own_name():
    g = _guide()
    TRACKED.clear()
    for i in range(len(g.steps)):
        g.current_step = i
        g._update_step()
    assert [k["step"] for e, k in TRACKED if e == "onboarding_step_viewed"] == g._STEP_SLUGS
    _done(g)


def test_the_panel_belongs_to_filect_not_on_top_of_every_app():
    g = _guide()
    assert g.parent() is W and not (g.windowFlags() & Qt.WindowStaysOnTopHint), "Windows: owned, not always on top"
    _done(g)


def test_settings_replays_it_but_never_twice():
    W._show_onboarding()
    _pump(0.05)
    first = W.onboarding
    assert first.isVisible()
    W._show_onboarding()                                  # (Show Welcome Guide again while it's open)
    _pump(0.05)
    assert W.onboarding is first and first.isVisible()
    first._try_it()
    W._show_onboarding()                                  # (...or while it waits behind Continue Tour)
    _pump(0.05)
    assert W.onboarding is first and first.isVisible() and not first.continue_btn.isVisible()
    first._remind_later()
    _pump(0.05)
    assert not first.isVisible() and not first.spotlight.isVisible()


def test_the_arrow_keys_still_work_after_clicking_a_button():
    """Windows: a clicked button takes the keyboard focus, and a focused button spends ← → on moving the focus to
    the next button - so after clicking Next (or Continue Tour) the arrows did nothing. Seen live 2026-11-12."""
    from PySide6.QtTest import QTest
    g = _guide()
    g.activateWindow()
    _pump(0.05)
    QTest.mouseClick(g.next_btn, Qt.LeftButton)               # (a real click)
    _pump(0.02)
    assert g.current_step == 1
    QTest.keyClick(QApplication.focusWidget() or g, Qt.Key_Right)   # (the key goes where the focus is)
    _pump(0.02)
    assert g.current_step == 2, ("→ after a click", type(QApplication.focusWidget()).__name__)
    QTest.mouseClick(g.back_btn, Qt.LeftButton)
    QTest.keyClick(QApplication.focusWidget() or g, Qt.Key_Right)
    _pump(0.02)
    assert g.current_step == 2, ("→ after clicking Back", type(QApplication.focusWidget()).__name__)
    _done(g)


def test_the_keys_work_again_after_the_typing_screens():
    """The dictation box and the custom-words field keep the keyboard on their screens (the arrows move the text
    cursor there). Leaving them, the keys must reach the panel again. Seen live 2026-11-12: after the custom-words
    screen, Return and → did nothing on "Add a folder"."""
    from PySide6.QtTest import QTest
    g = _guide()
    g.activateWindow()
    g._chips["voice"].click()                                 # the dictation box takes the keyboard
    _pump(0.05)
    QTest.mouseClick(g.next_btn, Qt.LeftButton)               # → the custom-words screen
    _pump(0.05)
    g._chips["files"].click()                                 # → "Add a folder"
    _pump(0.05)
    QTest.keyClick(QApplication.focusWidget() or g, Qt.Key_Return)
    _pump(0.02)
    assert g.current_step == 6, ("Return on Add a folder", type(QApplication.focusWidget()).__name__)
    QTest.keyClick(QApplication.focusWidget() or g, Qt.Key_Right)
    _pump(0.02)
    assert g.current_step == 7, ("→ on Find any file", type(QApplication.focusWidget()).__name__)
    _done(g)
