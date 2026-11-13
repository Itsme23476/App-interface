"""The daily voice limit is said right after you press the shortcut, not after you let go (Windows port of the Mac's
tests/test_voice_limit_early.py, 1df3842). Over the limit the live stream is refused as a 502 (the gateway hides the
429), so the app recorded the whole clip, uploaded it, and only THEN heard the limit - and on Windows the pill just
vanished, so nobody was told at all (live test 2026-11-13: 8 s held, nothing said). Now a refused stream asks
`transcribe` with an EMPTY upload (nothing transcribed or counted): 429 = stop at once, say it, no upload; anything
else = exactly as before. The real StreamingTranscriber with fed-in audio; no mic, no network."""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
import websockets  # noqa: E402
from app.core import transcription as T  # noqa: E402

LIMIT = "Daily voice limit reached. Try again tomorrow."
AUDIO = b"\x10\x00" * 1600                        # 100 ms of 16 kHz int16


def run(status, release_after=None, wait=3.0):
    """Start, feed audio; let go after `release_after` s (None = keep talking). Returns what happened."""
    probes, uploads, out = [], [], {}
    refused = mock.MagicMock(side_effect=OSError("server rejected WebSocket connection: HTTP 502"))
    with mock.patch.object(T, "_get_auth_token", return_value="tok"), \
            mock.patch.object(websockets, "connect", refused), \
            mock.patch.object(T.requests, "post",
                              lambda url, **k: probes.append(k.get("data")) or SimpleNamespace(status_code=status)), \
            mock.patch.object(T, "transcribe_audio", lambda *a, **k: uploads.append(a) or {"ok": True, "text": "hello"}):
        st = T.StreamingTranscriber(language="en", external=True)
        # (direct: the handlers run on the worker thread - no Qt event loop pumped in this test)
        st.finished.connect(lambda t: out.setdefault("finished", t), Qt.DirectConnection)
        st.error.connect(lambda m: out.setdefault("error", m), Qt.DirectConnection)
        st.start()
        t0 = time.time()
        while time.time() - t0 < wait and "finished" not in out and "error" not in out:
            st.feed(AUDIO)
            if release_after is not None and time.time() - t0 > release_after:
                st.stop_recording()
            time.sleep(0.05)
        out["after"] = round(time.time() - t0, 2)
        st.stop_recording()
        st._ws_thread.join(10)
    return out, probes, uploads


def test_over_the_limit_it_is_said_while_you_still_talk():
    out, probes, uploads = run(429)                 # never let go
    assert out.get("error") == LIMIT, out
    assert out["after"] < 2.0, f"said only after {out['after']} s"
    assert probes == [b""], f"the check is one EMPTY upload: {probes}"
    assert uploads == [], "the recording isn't uploaded (it would only be refused)"


def test_under_the_limit_nothing_changes():
    out, probes, uploads = run(400, release_after=1.0, wait=6.0)
    assert out.get("finished") == "hello" and "error" not in out, out
    assert len(uploads) == 1, "a refused stream still falls back to the batch upload, as before"


# ---- Windows: the message is SHOWN (the pill used to vanish), and letting go doesn't say "Transcribing…" ---------
def test_dictation_shows_the_limit_in_the_pill():
    from app.ui.dictation import DictationController
    c = SimpleNamespace(_overlay=mock.MagicMock(), state_changed=mock.MagicMock(), _recorder=object())
    DictationController._on_error(c, LIMIT)
    c._overlay.show_message.assert_called_once_with(LIMIT)
    c._overlay.hide_pill.assert_not_called()
    assert c._recorder is None
    DictationController._on_dictate_stop(c)          # you let go of Ctrl + Win afterwards
    c._overlay.show_transcribing.assert_not_called()


def test_the_agent_shortcut_shows_it_too():
    from app.ui.agent_voice import AgentVoiceController
    pill = mock.MagicMock()
    c = SimpleNamespace(_cancel=mock.MagicMock(), _get_pill=lambda: pill)
    AgentVoiceController._on_error(c, LIMIT)
    c._cancel.assert_called_once()
    pill.show_message.assert_called_once_with(LIMIT)


def test_the_pill_says_it_then_goes_and_listening_looks_normal_again():
    from app.ui.dictation_overlay import DictationOverlay, _PILL_W
    ov = DictationOverlay()
    ov.show_message(LIMIT, ms=4500)
    assert ov.isVisible() and ov.showing_message() and ov._msg.text() == LIMIT
    assert not ov._bars.isVisible() and not ov._dots.isVisible()
    assert ov._card.width() > _PILL_W, "the message has room"
    assert ov._msg_timer.isActive() and ov._msg_timer.interval() == 4500, "it goes by itself"
    ov.show_listening("dictate")                      # the next press: the normal pill
    assert not ov.showing_message() and ov._card.width() == _PILL_W and ov._bars.isVisible()
    assert not ov._msg_timer.isActive()
    ov.hide_pill()
    ov.deleteLater()


def test_hands_free_doesnt_say_you_clicked_stop(monkeypatch, tmp_path):
    from tests import test_live_listener as tl
    monkeypatch.setattr(tl.ll, "_usage_path", lambda: tmp_path / "usage.json")   # (its own: never other tests')
    L, mic, rec = tl._listener()
    L.start()
    try:
        s = tl.speech("Please make a new folder called limit test.")
        mic.push(tl.np.concatenate([tl.silence(0.5), s[: len(s) // 2]]))          # mid-sentence…
        assert L._cur is not None
        uid, tr = L._cur
        tr.error.emit(LIMIT)                                                        # …the limit
        tl._pump(lambda: False, 0.1)
        why = [d[2] for d in rec["dropped"] if d[0] == uid]
        assert why and why[0].startswith("Couldn't transcribe: Daily voice limit"), why
        assert "stopped" not in why, f"marked 'you clicked Stop': {why}"
        assert not L.on and L.state == "error", (L.on, L.state)
    finally:
        L.stop()
