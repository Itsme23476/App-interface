"""Mute while recording actually mutes. Live (stage 45, full feature test 2026-11-13): with it On, the PC's sound
stayed on - the log said "[MUTE] couldn't read output state (pycaw) — not muting", as it had since 2026-10.
pycaw's GetSpeakers() now returns an AudioDevice wrapper (no .Activate; .EndpointVolume is the interface) and the
recorder's worker thread had never initialized COM. Stand-in speakers - your sound is never touched here."""
import os
import sys
import threading
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import comtypes  # noqa: E402
import pycaw.pycaw as pc  # noqa: E402

import app.core.transcription as tr  # noqa: E402


class Volume:
    def __init__(self):
        self.muted = 0

    def GetMute(self):
        return self.muted

    def SetMute(self, m, _ctx):
        self.muted = m


class NewDevice:                      # pycaw 2025+: AudioDevice, .EndpointVolume, no .Activate
    def __init__(self, vol):
        self.EndpointVolume = vol


def _on_a_worker(fn):
    out = {}
    t = threading.Thread(target=lambda: out.update(r=fn()))
    t.start()
    t.join(10)
    return out.get("r")


def test_it_reads_and_sets_the_mute_through_the_new_pycaw():
    vol = Volume()
    with mock.patch.object(pc.AudioUtilities, "GetSpeakers", return_value=NewDevice(vol)):
        assert tr._output_muted() is False
        tr._set_output_muted(True)
        assert vol.muted == 1 and tr._output_muted() is True
        tr._set_output_muted(False)
        assert vol.muted == 0


def test_a_worker_thread_initializes_com_first():
    """(The recorder mutes from its worker thread: without CoInitialize there, pycaw fails.)"""
    calls = []
    real = comtypes.CoInitialize
    vol = Volume()
    with mock.patch.object(pc.AudioUtilities, "GetSpeakers", return_value=NewDevice(vol)), \
            mock.patch.object(comtypes, "CoInitialize", side_effect=lambda: calls.append(threading.get_ident()) or real()):
        assert _on_a_worker(tr._output_muted) is False
    assert calls and calls[-1] != threading.get_ident(), "COM wasn't initialized on the worker thread"


def test_the_real_speakers_can_be_read_from_a_worker_thread():
    """Read-only, on this PC's real speakers (the call that failed live): a True/False, not None."""
    got = _on_a_worker(tr._output_muted)
    if got is None and not _on_a_worker(lambda: bool(pc.AudioUtilities.GetAllDevices())):
        import pytest
        pytest.skip("no audio devices on this machine")
    assert got in (True, False), got
