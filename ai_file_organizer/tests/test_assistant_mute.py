"""Muting the hands-free Assistant (Windows port of the Mac tests/test_assistant_mute.py, pack §5.1).
The Mac's "Shift, then Fn" is Windows' Shift, then Ctrl + Win (the voice shortcut):
  · the keys: Shift held at least 0.1 s BEFORE Ctrl + Win = mute / unmute, decided the moment they go down; held,
    it toggles once and records nothing; pressed together, or Ctrl + Win first, it's still a search; Alt too = not
    mute; Win is masked so Start doesn't open
  · the controller: it goes to the Assistant page, and does nothing in the middle of a capture
  · the listener: muting really closes the mic, nothing else reopens it (another app, the lock screen, Filect done
    talking), unmuting comes straight back, Stop/Start leaves it unmuted
  · the page: the heading, the sub-line with the Windows keys, the chip, the notes, the Mute / Unmute button
  · the Esc note while collecting and Filect is talking
Fake keys, a fake clock, a fake mic. Your settings.json is never written."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import test_live_listener as tl  # noqa: E402  (its fake mic / monitor; settings stubbed, usage in temp)
from tests import test_live_page as tp  # noqa: E402
from tests.test_live_page import _no_side_effects  # noqa: E402,F401  (autouse: no settings / sounds / analytics)
from app.ui import dictation_hotkey as dh  # noqa: E402
from app.ui import live_page as lp  # noqa: E402
from app.ui.dictation import DictationController  # noqa: E402

app = tl.app
CTRL, LWIN, ALT, SHIFT, LSHIFT, RSHIFT = 0xA2, 0x5B, 0x12, 0x10, 0xA0, 0xA1
FRAME = 0.025


# ---- the keys --------------------------------------------------------------------------------------
@pytest.fixture
def keys(monkeypatch):
    held, masks, events, now = set(), [], [], [0.0]

    def down(vk):
        aliases = {0x11: {0xA2, 0xA3, 0x11}, 0x12: {0x12, 0xA4, 0xA5}, 0x10: {0x10, 0xA0, 0xA1}}
        return bool(held & aliases.get(vk, {vk}))
    monkeypatch.setattr(dh, "_key_down", down)
    monkeypatch.setattr(dh, "_mask_menu_keys", lambda: masks.append(frozenset(held)))
    hk = dh.DictationHotkey(trigger="ctrl+win", hold_ms=180, poll_ms=25)
    hk._clock = lambda: now[0]
    hk.dictate_start.connect(lambda m: events.append(("start", m)))
    hk.dictate_stop.connect(lambda: events.append(("stop",)))
    hk.mute_toggle.connect(lambda: events.append(("mute",)))

    def frames(n):
        for _ in range(n):
            hk._poll()
            now[0] += FRAME
    return SimpleNamespace(hk=hk, held=held, masks=masks, events=events, frames=frames)


def _shift_then_chord(k, wait_frames=5, hold_frames=12, shift=LSHIFT):
    k.held.add(shift)
    k.frames(wait_frames)                            # (5 polls = 0.125 s with Shift alone)
    k.held |= {CTRL, LWIN}
    k.frames(hold_frames)
    k.held -= {CTRL, LWIN}
    k.frames(2)


def test_shift_first_then_ctrl_win_mutes_and_records_nothing(keys):
    _shift_then_chord(keys)
    assert keys.events == [("mute",)], keys.events        # no start, no stop: nothing recorded
    assert len(keys.masks) == 1 and LWIN in keys.masks[0], "Win is masked: letting go mustn't open Start"


def test_holding_them_toggles_once_and_again_unmutes(keys):
    _shift_then_chord(keys, hold_frames=40)              # held a full second
    assert keys.events == [("mute",)], keys.events
    keys.frames(2)                                       # (Shift still down) the same keys again
    keys.held |= {CTRL, LWIN}
    keys.frames(5)
    keys.held -= {CTRL, LWIN}
    keys.frames(2)
    assert keys.events == [("mute",), ("mute",)], keys.events


def test_pressed_almost_together_stays_a_search(keys):
    keys.held.add(LSHIFT)
    keys.frames(1)                                       # 25 ms: not "first"
    keys.held |= {CTRL, LWIN}
    keys.frames(12)
    assert keys.events == [("start", "search")], keys.events


def test_ctrl_win_first_then_shift_stays_a_search(keys):
    keys.held |= {CTRL, LWIN}
    keys.frames(3)
    keys.held.add(LSHIFT)
    keys.frames(12)
    keys.held.clear()
    keys.frames(2)
    assert keys.events == [("start", "search"), ("stop",)], keys.events


def test_a_shift_already_let_go_doesnt_count(keys):
    keys.held.add(LSHIFT)
    keys.frames(6)
    keys.held.discard(LSHIFT)
    keys.frames(1)
    keys.held |= {CTRL, LWIN}
    keys.frames(12)
    assert keys.events == [("start", "dictate")], keys.events


def test_the_right_shift_works_and_two_shifts_keep_the_first_time(keys):
    keys.held.add(RSHIFT)
    keys.frames(3)
    keys.held.add(LSHIFT)                                # (a second Shift doesn't restart the clock)
    keys.frames(2)
    keys.held |= {CTRL, LWIN}
    keys.frames(10)
    assert keys.events == [("mute",)], keys.events


def test_with_alt_too_it_isnt_mute(keys):
    keys.held |= {LSHIFT, ALT}
    keys.frames(6)
    keys.held |= {CTRL, LWIN}
    keys.frames(12)
    assert ("mute",) not in keys.events and keys.events[0][0] == "start", keys.events


def test_after_muting_plain_ctrl_win_still_dictates(keys):
    _shift_then_chord(keys)
    keys.held.clear()
    keys.frames(2)
    keys.held |= {CTRL, LWIN}
    keys.frames(12)
    keys.held.clear()
    keys.frames(2)
    assert keys.events == [("mute",), ("start", "dictate"), ("stop",)], keys.events


# ---- the controller ---------------------------------------------------------------------------------
def _controller(recorder=None):
    toggled = []
    c = SimpleNamespace(_recorder=recorder,
                        main_window=SimpleNamespace(live_page=SimpleNamespace(toggle_mute=lambda: toggled.append(1))))
    return c, toggled


def test_the_keys_go_to_the_assistant_page():
    c, toggled = _controller()
    DictationController._on_mute_keys(c)
    assert toggled == [1]


def test_it_does_nothing_in_the_middle_of_a_capture(monkeypatch):
    c, toggled = _controller(recorder=object())          # dictation still recording / transcribing
    DictationController._on_mute_keys(c)
    from app.core import mic_helper
    monkeypatch.setattr(mic_helper, "busy", lambda: True)  # another Filect shortcut is recording
    c2, toggled2 = _controller()
    DictationController._on_mute_keys(c2)
    assert toggled == [] and toggled2 == []


def test_no_assistant_page_is_fine():
    c = SimpleNamespace(_recorder=None, main_window=SimpleNamespace(live_page=None))
    DictationController._on_mute_keys(c)                 # (the page failed to load: nothing happens, no error)


def test_the_real_signal_is_wired():
    src = Path(dh.__file__).with_name("dictation.py").read_text(encoding="utf-8")
    assert "self.hotkey.mute_toggle.connect(self._on_mute_keys)" in src


# ---- the listener -----------------------------------------------------------------------------------
def _listening():
    L, mic, rec = tl._listener()
    L.start()
    mic.calls[0]["on_chunk"](tl.silence(0.1).tobytes())
    assert tl._pump(lambda: L.state == "listening", 1), L.state
    return L, mic, rec


def test_muting_really_closes_the_mic():
    L, mic, rec = _listening()
    try:
        stop = mic.calls[0]["stop"]
        L.set_user_muted(True)
        assert L.state == "paused" and rec["states"][-1] == ("paused", "Muted"), rec["states"][-1]
        assert stop.calls and L._stop_fn is None, "the microphone must really close"
        tl._pump(lambda: False, 1.6)
        assert L.state == "paused" and len(mic.calls) == 1, "it must not reopen by itself"
    finally:
        L.stop()


def test_nothing_else_can_unmute_it():
    L, mic, rec = _listening()
    try:
        L.set_user_muted(True)
        tl.FakeMonitor.others = ["Zoom"]
        tl._pump(lambda: False, 0.9)
        assert rec["states"][-1][1] == "Muted", rec["states"][-1]
        tl.FakeMonitor.others = []
        tl.FakeMonitor.locked = True
        tl._pump(lambda: False, 0.9)
        tl.FakeMonitor.locked = False
        tl._pump(lambda: False, 1.8)
        L.set_muted(True)                                # (Filect speaking, then done)
        L.set_muted(False)
        tl._pump(lambda: False, 0.5)
        assert L.user_muted and len(mic.calls) == 1 and rec["states"][-1][1] == "Muted", (len(mic.calls),
                                                                                           rec["states"][-1])
    finally:
        L.stop()


def test_unmuting_comes_straight_back():
    L, mic, rec = _listening()
    try:
        L.set_user_muted(True)
        L.set_user_muted(False)
        assert len(mic.calls) == 2 and L.state == "starting", (len(mic.calls), L.state)
        mic.calls[-1]["on_chunk"](tl.silence(0.1).tobytes())
        assert tl._pump(lambda: L.state == "listening", 1), L.state
    finally:
        L.stop()


def test_unmuting_while_another_app_has_the_mic_waits_for_it():
    L, mic, rec = _listening()
    try:
        L.set_user_muted(True)
        tl.FakeMonitor.others = ["Zoom"]
        L.set_user_muted(False)
        assert L.state == "paused" and "Zoom" in rec["states"][-1][1] and len(mic.calls) == 1, rec["states"][-1]
        tl.FakeMonitor.others = []
        assert tl._pump(lambda: len(mic.calls) == 2, 3), "must come back when the other app is done"
    finally:
        tl.FakeMonitor.others = []
        L.stop()


def test_stop_and_start_leaves_it_unmuted():
    L, mic, rec = _listening()
    L.set_user_muted(True)
    L.stop()
    assert not L.user_muted
    L.start()
    try:
        assert len(mic.calls) == 2 and not L.user_muted
    finally:
        L.stop()


# ---- the page ---------------------------------------------------------------------------------------
def _tab():
    mw = tp._MW()
    mw.assistant_page, mw.nav_button_group = tp.FakeAP(), tp.FakeGroup()
    L, mic, rec = tl._listener()
    page = lp.LivePage(mw, listener=L)
    return page, L, mic


def test_the_page_says_muted_and_how_to_undo_it_with_the_windows_keys():
    page, L, mic = _tab()
    notes = []
    page._note = lambda t, *a, **k: notes.append(t)
    page.toggle_mute()
    assert notes == ["The Assistant isn't on"] and not L.user_muted, notes
    L.start()
    try:
        mic.calls[0]["on_chunk"](tl.silence(0.1).tobytes())
        assert tl._pump(lambda: L.state == "listening", 1)
        page.toggle_mute()
        assert tl._pump(lambda: page.status.text() == "Muted", 1), page.status.text()
        sub = page._sub.text()
        assert "Muted" in page._chip.text() and "to unmute" in sub and notes[-1] == "Muted", (page._chip.text(), notes)
        assert all(k in sub for k in ("Shift", "Ctrl", "Win")) and not any(m in sub for m in ("⇧", "fn", "⌃")), sub
        page.toggle_mute()
        mic.calls[-1]["on_chunk"](tl.silence(0.1).tobytes())
        assert tl._pump(lambda: L.state == "listening" and "Listening" in page.status.text(), 1), page.status.text()
        assert "Muted" not in page._chip.text() and notes[-1] == "Listening again"
    finally:
        L.stop()


def test_the_mute_button():
    page, L, mic = _tab()
    page._note = lambda *a, **k: None
    b = page.mute_btn
    assert b.isHidden(), "nothing to mute while it's off"
    assert "Shift, then Ctrl + Win" in b.toolTip(), b.toolTip()
    L.start()
    try:
        mic.calls[0]["on_chunk"](tl.silence(0.1).tobytes())
        assert tl._pump(lambda: L.state == "listening" and not b.isHidden(), 1), (L.state, b.isHidden())
        assert "Mute" in b.text() and "Unmute" not in b.text(), b.text()
        b.click()
        assert tl._pump(lambda: page.status.text() == "Muted", 1) and L.user_muted and "Unmute" in b.text(), b.text()
        assert L._stop_fn is None, "the microphone must really close"
        b.click()
        mic.calls[-1]["on_chunk"](tl.silence(0.1).tobytes())
        assert tl._pump(lambda: L.state == "listening", 1) and not L.user_muted and "Unmute" not in b.text(), b.text()
        page.toggle_mute()                               # (the keys: the button has to follow them)
        assert tl._pump(lambda: "Unmute" in b.text(), 1), b.text()
        page.toggle_mute()
        assert tl._pump(lambda: "Unmute" not in b.text(), 1), b.text()
        page.toggle_mute()
        assert tl._pump(lambda: "Unmute" in b.text(), 1)
    finally:
        L.stop()
    assert tl._pump(lambda: b.isHidden(), 1), "gone once it's off"
    L.start()
    try:
        assert tl._pump(lambda: not b.isHidden() and "Unmute" not in b.text(), 1), b.text()
    finally:
        L.stop()


def test_esc_while_collecting_and_talking_says_press_esc_again():
    page, L, mic = _tab()
    notes, stopped = [], []
    page._note = lambda t, *a, **k: notes.append(t)
    page._speaker_obj = SimpleNamespace(speaking=True, stop=lambda: stopped.append(1))
    page._collect = {"uids": [], "text": "", "sending": False, "bits": [("text", "the budget")]}
    assert page._esc_filter() is True
    assert stopped == [1] and page._collect is not None, "the first Esc only stops the talking"
    assert notes == ["Stopped talking. Press Esc again to throw away what you collected."], notes
    page._speaker_obj = SimpleNamespace(speaking=True, stop=lambda: stopped.append(1))
    page._collect = None
    notes.clear()
    assert page._esc_filter() is True and notes == [], "talking without collecting: no note"
