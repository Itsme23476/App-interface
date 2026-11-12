"""Hold Ctrl + Alt = talk to the agent (2026-11-12, the owner's pick; replaces the test key Shift+Z).

Windows can't register a modifiers-only hotkey, so the chord is watched. It must never get in the way of typing or
of other shortcuts: AltGr (Ctrl + right Alt) never counts, Ctrl+Alt+<key> shortcuts never count (before or after
the hold time), dictation's Ctrl+Win(+Alt) never counts, and letting go of Alt must not open the front app's menu."""
import pytest
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # (headless, like every other test)
from PySide6.QtWidgets import QApplication

from app.ui import agent_hotkey as ah

_app = QApplication.instance() or QApplication([])

LCTRL, RCTRL, LALT, RALT, LWIN, LSHIFT = 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0xA0
ALIASES = {0x11: {0x11, 0xA2, 0xA3}, 0x12: {0x12, 0xA4, 0xA5}, 0x10: {0x10, 0xA0, 0xA1}}


@pytest.fixture
def rig(monkeypatch):
    held, clock, masks, events, claims = set(), [0.0], [], [], []
    monkeypatch.setattr(ah, "_down", lambda vk: bool(held & ALIASES.get(vk, {vk})))
    monkeypatch.setattr(ah, "_now", lambda: clock[0])
    monkeypatch.setattr(ah, "_mask", lambda: masks.append(frozenset(held)))
    hk = ah.AgentHotkey(0, "ctrl+alt", poll_ms=10)
    monkeypatch.setattr(hk, "_claim", lambda: claims.append(1))
    monkeypatch.setattr(hk._mouse, "start", lambda: None)
    monkeypatch.setattr(hk._mouse, "stop", lambda: None)
    for name in ("pressed", "released", "escape"):
        getattr(hk, name).connect(lambda *a, n=name: events.append(n))
    assert hk.start() and hk._modchord

    def tick(secs=0.0, n=1):
        for _ in range(n):
            clock[0] += secs
            hk._poll()
    yield hk, held, tick, masks, events
    hk.stop()


def test_default_is_ctrl_alt_and_labels():
    assert ah.DEFAULT_SHORTCUT == "ctrl+alt"
    assert ah.parse("ctrl+alt") == (ah.MOD_CONTROL | ah.MOD_ALT, 0)
    assert ah.label("ctrl+alt") == "Ctrl+Alt"


def test_holding_ctrl_alt_starts_then_letting_go_sends(rig):
    hk, held, tick, masks, events = rig
    held |= {LCTRL, LALT}
    tick(0.1, 2)                                         # (first seen at the first tick: 0.1 s held so far)
    assert events == []                                  # (not yet: a shortcut is faster than this)
    tick(0.25, 1)
    assert events == ["pressed"] and hk.active
    assert len(masks) == 1                               # Alt's menu masked the moment it counted
    tick(0.5, 5)
    assert events == ["pressed"]
    held.clear()
    tick(0.02)
    assert events == ["pressed", "released"] and not hk.active


def test_a_quick_tap_does_nothing(rig):
    hk, held, tick, masks, events = rig
    held |= {LCTRL, LALT}
    tick(0.1, 2)
    held.clear()
    tick(0.1, 5)
    assert events == [] and masks == []


def test_altgr_never_counts(rig):
    hk, held, tick, masks, events = rig
    held |= {LCTRL, RALT}                                # AltGr = Ctrl + RIGHT Alt (€, @, ż…)
    tick(0.2, 10)
    assert events == []


def test_a_ctrl_alt_shortcut_before_the_hold_is_never_ours(rig):
    hk, held, tick, masks, events = rig
    held |= {LCTRL, LALT}
    tick(0.1)
    held.add(ord("H"))                                   # Ctrl+Alt+H (Quick Search)
    tick(0.1)
    held.discard(ord("H"))
    tick(0.5, 4)                                          # still holding Ctrl+Alt after the shortcut
    assert events == []
    held.clear()
    tick(0.05)
    held |= {LCTRL, LALT}                                 # a fresh press afterwards works again
    tick(0.35, 2)
    assert events == ["pressed"]


def test_a_shortcut_after_the_hold_cancels_quietly(rig):
    hk, held, tick, masks, events = rig
    held |= {LCTRL, LALT}
    tick(0.35, 2)
    assert events == ["pressed"]
    held.add(ord("H"))                                    # held a bit long, then the shortcut's key
    tick(0.02)
    assert events == ["pressed", "escape"] and not hk.active      # cancelled — never "released" (sent)
    held.discard(ord("H"))
    tick(0.4, 3)
    assert events == ["pressed", "escape"]                         # not re-armed while Ctrl+Alt stay down


def test_dictation_chords_never_count(rig):
    hk, held, tick, masks, events = rig
    held |= {LCTRL, LWIN, LALT}                           # Ctrl+Win+Alt = voice organize
    tick(0.2, 10)
    held.clear()
    tick(0.02)
    held |= {LCTRL, LALT}
    tick(0.1)
    held.add(LWIN)                                        # Win joins a moment later
    tick(0.2, 5)
    assert events == []


def test_space_and_shift_while_held_keep_it_going(rig):
    hk, held, tick, masks, events = rig
    held |= {LCTRL, LALT}
    tick(0.35, 2)
    held |= {LSHIFT, 0x20}                                # Shift + Space while holding (drawing)
    tick(0.1, 5)
    assert hk.active and events == ["pressed"]


def test_the_test_keys_become_the_real_ones_when_settings_load(tmp_path, monkeypatch):
    import json
    from app.core import settings as sm
    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps({"quick_search_shortcut": "shift+a", "assistant_voice_shortcut": "shift+z"}),
                   encoding="utf-8")
    monkeypatch.setattr(sm.Settings, "_config_file", lambda self: cfg)
    s2 = sm.Settings()
    assert s2.quick_search_shortcut == "ctrl+alt+h"
    assert s2.assistant_voice_shortcut == "ctrl+alt"
    cfg.write_text(json.dumps({"quick_search_shortcut": "ctrl+shift+f", "assistant_voice_shortcut": "ctrl+alt+z"}),
                   encoding="utf-8")
    s3 = sm.Settings()
    assert s3.quick_search_shortcut == "ctrl+shift+f"         # a real choice is kept
    assert s3.assistant_voice_shortcut == "ctrl+alt+z"
