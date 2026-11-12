"""The top card + left rail match the Mac guide (Desktop/notch-rail-guide), headless:
  * hover bar = the Mac's 399×88 with the orb IN the band and the input BELOW it (they overlapped with no notch)
  * the card shrinks back when a panel closes (Qt kept the old minimum → rows stretched apart, e.g. Stopping)
  * spec heights: working 277 · needs you 371 · new chat 195
  * ⚠ / ◼ are drawn as text glyphs (Segoe UI Symbol), Stop shows a red ■ — not Windows' emoji / grey box
  * the Claude logo is the real spark (transparent 128×128), not a round badge
  * the card's model list keeps your pick even when it's an older version
  * the rail is clipped to its edge tab while tucked (never shows on a monitor to its left)"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.ui import claude_overlay as co      # noqa: E402
from app.ui import conversation_rail as cr   # noqa: E402

IS_WIN = sys.platform.startswith("win")
MODELS = [("opus", "Opus 5.5"), ("fable", "Fable 5.1"), ("sonnet", "Sonnet 5.5"), ("haiku", "Haiku 5.5")]


def _pump(s=0.05):
    end = time.time() + s
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def _card():
    ov = co.ClaudeOverlay()
    ov.set_agent("claude", MODELS, "")
    ov.set_model_effort("opus", "high")
    return ov


def _working(ov):
    ov.present(True)
    ov.restore_panel()
    ov.set_task("Make a landing page for my coffee shop Bean & Leaf", "Bean & Leaf Landing Page")
    ov.show_working("Writing landing.html", "")
    _pump()


@pytest.mark.skipif(not IS_WIN, reason="Windows stand-in for the notch")
def test_hover_bar_is_the_mac_size_and_orb_clears_the_input():
    ov = _card()
    ov.rest_idle()
    ov.show()
    _pump()
    ov._hover_in = True
    ov._show_hover_bar()
    _pump()
    assert (ov.width(), ov.height()) == (399, 88), (ov.width(), ov.height())
    orb, field = ov._hover_bar._orb.geometry(), ov._hover_bar.field.geometry()
    assert orb.bottom() < field.top(), f"the orb {orb} overlaps the input {field}"
    assert orb.top() >= 4 and orb.bottom() <= co.WIN_HOVER_BAND, "the orb must sit in the top band"
    ov.close()


def test_card_shrinks_back_after_a_panel_closes():
    ov = _card()
    ov.show()
    _working(ov)
    base = ov.height()
    assert base == 277, base                                  # SPEC §1.9: working, bubble + title
    ov._toggle_model_panel()
    ov._toggle_model_list()
    _pump()
    assert ov.height() > 500
    ov._toggle_model_list()
    ov._toggle_model_panel()
    _pump()
    assert ov.height() == base, f"stayed {ov.height()} tall (rows stretched apart) instead of {base}"
    ov._toggle_details()
    _pump()
    ov._toggle_details()
    _pump()
    assert ov.height() == base
    ov.close()


def test_spec_heights():
    ov = _card()
    ov.show()
    _working(ov)
    ov.show_needs_you("Bash", {"command": "npm run build"})
    _pump()
    assert ov.height() == 371, ov.height()
    ov.close()
    ov = _card()
    ov.show()
    ov.show_new_chat()
    _pump()
    assert ov.height() == 195, ov.height()                    # (the Mac guide's 14-new-chat shows it stretched)
    ov.close()


@pytest.mark.skipif(not IS_WIN, reason="Windows fonts")
def test_glyphs_are_text_not_emoji():
    ov = _card()
    ov.show()
    _working(ov)
    assert ov._primary_btn.text().startswith("■"), repr(ov._primary_btn.text())   # red ■, not a grey ◼ box
    ov.show_error("The command failed: npm: command not found")
    _pump()
    from PySide6.QtWidgets import QLabel
    warn = [l for l in ov.findChildren(QLabel) if l.text() == "⚠"]
    assert warn and "Segoe UI Symbol" in warn[0].styleSheet()
    ov.show_done("", 0.0, "", "Stopped by you.", True)
    _pump()
    assert "Segoe UI Symbol" in ov._done_glyph.styleSheet()
    ov.close()


def test_claude_logo_is_the_transparent_spark():
    img = QImage(os.path.join(os.path.dirname(co.__file__), "assets", "claude-logo.png"))
    assert (img.width(), img.height()) == (128, 128)
    assert img.pixelColor(2, 2).alpha() == 0 and img.pixelColor(125, 125).alpha() == 0, "not transparent (a badge)"
    c = img.pixelColor(64, 64)
    assert (c.red(), c.green(), c.blue()) == (0xD9, 0x77, 0x57), c.name()


def test_card_models_keep_an_older_pick(monkeypatch):
    from app.core.settings import settings
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    from app.ui import assistant_page as ap

    class _MW:
        pass
    page = ap.AssistantPage(_MW())
    try:
        page._claude_models = [{"value": "opus", "name": "Opus 5.5", "efforts": []},
                               {"value": "haiku", "name": "Haiku 5.5", "efforts": []},
                               {"value": "claude-opus-4-8", "name": "Opus 4.8", "efforts": []}]
        page._model = "claude-opus-4-8"
        ov = page._ensure_notch()
        page._sync_card_models()
        assert "claude-opus-4-8" in ov._model_names and ov._model == "claude-opus-4-8", ov._model_names
        page._model = "opus"
        page._sync_card_models()
        assert "claude-opus-4-8" not in ov._model_names, "older versions are only on the Agents page"
    finally:
        page.shutdown()


@pytest.mark.skipif(not IS_WIN, reason="Windows multi-monitor clip")
def test_rail_is_clipped_to_its_tab_while_tucked():
    class S:
        def __init__(self, key, title, status):
            self.key, self.title, self.status, self.started_at, self.agent, self.prompt = key, title, status, time.time(), "claude", ""
    rail = cr.ConversationRail()
    rail.update_sessions([S("s1", "Bean & Leaf", "done")], None, [])
    rail._hover_timer.stop()
    _pump(0.4)
    rail.collapse()
    _pump(0.4)
    m = rail.mask().boundingRect()
    assert m == QRect(rail.width() - cr.TAB_W, 0, cr.TAB_W, rail.height()), m
    rail.open_full()
    _pump(0.05)
    assert rail.mask().isEmpty(), "the open drawer must not be clipped"
    rail.collapse()
    _pump(0.05)
    assert rail.mask().isEmpty(), "clipped before the slide-out finished (it must still animate)"
    _pump(0.4)
    assert not rail.mask().isEmpty()
    rail.close()


@pytest.mark.skipif(not IS_WIN, reason="Windows has no notch: the resting pill stands in for it")
def test_resting_pill_shows_and_hides_over_fullscreen():
    """At rest (no task) a small black pill sits at the top centre — something to find and hover (2026-11-08:
    "I'm not seeing anything that I can open"). Over a fullscreen app it's gone."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QRegion
    from PySide6.QtWidgets import QWidget
    h = co._Handle()
    h.resize(co.STRIP_W, co.PEEK_H)

    def drawn():
        img = QImage(co.STRIP_W, co.PEEK_H, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        h.render(img, QPoint(0, 0), QRegion(), QWidget.RenderFlag.DrawChildren)
        return [(x, y) for x in range(img.width()) for y in range(img.height()) if img.pixelColor(x, y).alpha() > 200]
    px = drawn()
    assert px, "nothing at rest"
    xs, ys = [x for x, _ in px], [y for _, y in px]
    assert min(ys) == 0 and max(ys) <= co.WIN_PILL_H, "the pill hangs flush from the top edge, small"
    assert abs((min(xs) + max(xs)) / 2 - co.STRIP_W / 2) <= 2, "centred"
    h.set_pill_hidden(True)
    assert drawn() == [], "drawn over a fullscreen app"


@pytest.mark.skipif(not IS_WIN, reason="Windows hover zone")
def test_hovering_the_pill_opens_the_bar(monkeypatch):
    """The pill is the hover target: the cursor on it wakes the strip; just below it (over a browser's tabs) it
    doesn't; over a fullscreen app it doesn't."""
    ov = _card()
    ov.rest_idle(True)
    ov.show()
    _pump(0.3)
    g = ov.frameGeometry()
    cx = g.center().x()
    where = {"pos": None}
    monkeypatch.setattr(co.QCursor, "pos", staticmethod(lambda: where["pos"]))
    monkeypatch.setattr(co.notch_panel, "fullscreen_app_in_front", lambda *a: False)
    where["pos"] = QPoint(cx, g.top() + co.WIN_PILL_H + 8)          # just under the pill: not it
    ov._fs_at = 0
    ov._poll_hover()
    assert not ov._hover_in
    where["pos"] = QPoint(cx, g.top() + 3)                          # on the pill
    ov._poll_hover()
    assert ov._hover_in
    where["pos"] = QPoint(cx, g.bottom() + 300)
    ov._poll_hover()
    monkeypatch.setattr(co.notch_panel, "fullscreen_app_in_front", lambda *a: True)
    ov._fs_at = 0
    where["pos"] = QPoint(cx, g.top() + 3)
    ov._poll_hover()
    assert ov._handle._pill_hidden and not ov._hover_in, "a fullscreen app: no pill, nothing to wake"
    ov.hide()



@pytest.mark.skipif(not IS_WIN, reason="Windows stand-in for the notch")
def test_new_chat_after_a_closed_chat_shows_no_old_question_or_timer():
    """A6 (2026-11-11, Mac 331b229 — the same on Windows): a chat finishes, ✕ (or it tucks itself), then the drawer's
    New chat. The blank card showed the OLD question and its timer: resting only HID the bubble, and opening the card
    shows the bubble whenever it still has text. Checked on what's shown, not just the card's state."""
    ov = _card()
    _working(ov)
    _pump(1.2)                                       # (the timer runs)
    ov.show_done("sid-1", 0.0, "Make a landing page", "Done: landing.html")
    _pump()
    assert ov._transcript.text(), "(the finished chat's question is on the card)"
    ov.rest_idle(force=True)                         # ✕ / Esc
    _pump()
    ov.show_new_chat()                               # the drawer's New chat
    _pump(0.3)
    assert ov._transcript.text() == "" and not ov._transcript.isVisible(), "no old question on the blank card"
    assert ov._timer_label.text() == "", "no old timer"
    ov.restore_panel()                               # (opening it again must not bring it back either)
    _pump()
    assert not ov._transcript.isVisible() and ov._timer_label.text() == ""
    ov.close()


@pytest.mark.skipif(not IS_WIN, reason="Windows stand-in for the notch")
def test_new_chat_while_a_chat_is_still_working_starts_blank():
    ov = _card()
    _working(ov)
    _pump(1.2)
    assert ov._timer_label.text(), "(the working chat's timer shows)"
    ov.show_new_chat()                               # (the working chat keeps running in the drawer)
    _pump(1.2)
    assert ov._transcript.text() == "" and ov._timer_label.text() == "", "blank: no question, no ticking timer"
    ov.close()
