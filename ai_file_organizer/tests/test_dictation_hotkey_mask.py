"""The voice shortcut (Ctrl+Win, + Shift = search, + Alt = organize) sends ONE unassigned 'mask' key while it's held.
Without it, letting go of Alt makes the app in front open its menu, which swallows the next keys typed there (live test
2026-11-12: ' eggs, milk' arrived as 'ggs, milk'; the same with Filect closed — it's what the chord does on Windows)."""
import ctypes

import pytest
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # (headless, like every other test)
from PySide6.QtWidgets import QApplication

from app.ui import dictation_hotkey as dh

_app = QApplication.instance() or QApplication([])

CTRL, LWIN, ALT, SHIFT = 0xA2, 0x5B, 0x12, 0x10


@pytest.fixture
def rig(monkeypatch):
    held, masks, events = set(), [], []

    def down(vk):
        aliases = {0x11: {0xA2, 0xA3, 0x11}, 0x12: {0x12, 0xA4, 0xA5}, 0x10: {0x10, 0xA0, 0xA1}}
        return bool(held & aliases.get(vk, {vk}))
    monkeypatch.setattr(dh, "_key_down", down)
    monkeypatch.setattr(dh, "_mask_menu_keys", lambda: masks.append(frozenset(held)))
    hk = dh.DictationHotkey(trigger="ctrl+win", hold_ms=180, poll_ms=25)
    hk.dictate_start.connect(lambda m: events.append(("start", m)))
    hk.dictate_stop.connect(lambda: events.append(("stop",)))

    def frames(n):
        for _ in range(n):
            hk._poll()
    return hk, held, masks, events, frames


def test_organize_chord_sends_the_mask_key_once_while_held(rig):
    hk, held, masks, events, frames = rig
    held |= {CTRL, LWIN, ALT}
    frames(30)
    assert events == [("start", "organize")]
    assert len(masks) == 1 and {LWIN, ALT} <= masks[0]        # sent while Win and Alt were down
    held.clear()
    frames(2)
    assert events[-1] == ("stop",) and len(masks) == 1


def test_a_quick_tap_sends_nothing(rig):
    hk, held, masks, events, frames = rig
    held |= {CTRL, LWIN, ALT}
    frames(3)                                                  # shorter than the 180 ms hold
    held.clear()
    frames(3)
    assert events == [] and masks == []


def test_alt_pressed_after_the_start_is_masked_too(rig):
    hk, held, masks, events, frames = rig
    held |= {CTRL, LWIN}
    frames(10)
    assert events == [("start", "dictate")] and len(masks) == 1
    held.add(ALT)                                              # a moment later
    frames(10)
    assert len(masks) == 2
    frames(10)
    assert len(masks) == 2                                     # once per key per press, not every poll


def test_every_new_press_is_masked_again(rig):
    hk, held, masks, events, frames = rig
    for _ in range(3):
        held |= {CTRL, LWIN, SHIFT}
        frames(10)
        held.clear()
        frames(2)
    assert [e[1] for e in events if e[0] == "start"] == ["search"] * 3
    assert len(masks) == 3


def test_a_single_letter_test_trigger_never_sends_it(monkeypatch, rig):
    hk, held, masks, events, frames = rig
    hk.set_trigger("q")
    held.add(ord("Q"))
    frames(10)
    assert events == [("start", "dictate")] and masks == []


def test_the_mask_key_is_an_unassigned_key_pressed_and_released(monkeypatch):
    sent = []
    monkeypatch.setattr(ctypes.windll.user32, "keybd_event", lambda vk, scan, flags, extra: sent.append((vk, flags)))
    dh._mask_menu_keys()
    assert sent == [(0xE8, 0), (0xE8, 0x2)]
