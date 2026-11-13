"""Dictation ~1 s faster. Live (Windows stage 32, 2026-11-13): letting go → text took 1.55 / 1.42 / 1.39 s, because
the finished text was only handed over after closing the connection to the speech service, and that close waits
about 1 s for the server's side. Now the text is handed over first, then the connection closes (Mac 08f6208). Also
the hands-free Assistant (same code). A stand-in server whose close takes 1 s; the real StreamingTranscriber; no mic,
nothing sent anywhere."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402

app = QCoreApplication.instance() or QCoreApplication([])
import websockets  # noqa: E402
from app.core import transcription as T  # noqa: E402


class SlowCloseServer:
    """Answers audio.done with the final sentence + transcript.done; its close takes 1 s (like the real one)."""
    def __init__(self):
        self.q, self.closed_at = asyncio.Queue(), None

    async def send(self, data):
        if isinstance(data, str) and json.loads(data).get("type") == "audio.done":
            await self.q.put(json.dumps({"type": "transcript.partial", "text": "Hello there.", "speech_final": True}))
            await self.q.put(json.dumps({"type": "transcript.done", "text": ""}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.q.get()

    async def close(self):
        await asyncio.sleep(1.0)
        self.closed_at = time.monotonic()


def _run(server, batch=None):
    out = {"texts": []}

    async def connect(*a, **k):
        return server
    with mock.patch.object(T, "_get_auth_token", return_value="tok"), \
            mock.patch.object(websockets, "connect", connect), \
            mock.patch.object(T.StreamingTranscriber, "_finish_via_batch", batch or (lambda self: out.setdefault("batch", 1))):
        st = T.StreamingTranscriber(language="en", external=True)       # (hands-free's way: audio fed, no mic)
        st.finished.connect(lambda t: out["texts"].append((t, time.monotonic())), Qt.DirectConnection)
        st.start()
        for _ in range(10):
            st.feed(b"\x10\x00" * 1600)
            time.sleep(0.05)
        t0 = time.monotonic()
        st.stop_recording()
        st._ws_thread.join(8)
    return out, t0


def test_the_text_comes_before_the_slow_close():
    server = SlowCloseServer()
    out, t0 = _run(server)
    assert len(out["texts"]) == 1, out                       # (once - not again after the close)
    text, at = out["texts"][0]
    assert text == "Hello there.", text
    assert server.closed_at, "the connection still closes"
    assert server.closed_at - at >= 0.8, \
        f"the text came {server.closed_at - at:.2f}s before the 1 s close ended (it should come before the close)"
    # (no fixed "within N s of letting go" here: on a busy PC that alone varies; the live test times the real thing)
    assert "batch" not in out


def test_a_failure_after_the_text_doesnt_send_it_twice():
    """If anything goes wrong after the text was handed over, nothing more happens: no batch upload of the same audio,
    no second copy of the text."""
    st = T.StreamingTranscriber(language="en", external=True)
    texts, batch = [], []

    async def session(self):
        self._delivered = True
        self.finished.emit("Hello there.")
        raise RuntimeError("the close went wrong")
    st.finished.connect(lambda t: texts.append(t), Qt.DirectConnection)
    with mock.patch.object(T.StreamingTranscriber, "_session", session), \
            mock.patch.object(T.StreamingTranscriber, "_finish_via_batch", lambda self: batch.append(1)):
        st._run_stream()
    assert texts == ["Hello there."] and batch == [], (texts, batch)


def test_no_words_still_falls_back_as_before():
    """Nothing heard: nothing is handed over early; the old path still decides (hands-free: one empty text, no
    re-upload of the clip)."""
    class Silent(SlowCloseServer):
        async def send(self, data):
            if isinstance(data, str) and json.loads(data).get("type") == "audio.done":
                await self.q.put(json.dumps({"type": "transcript.done", "text": ""}))
    batch = []
    out, _ = _run(Silent(), batch=lambda self: batch.append(1))
    assert [t for t, _ in out["texts"]] == [""], out
    assert batch == [], "hands-free trusts 'no words' (no re-upload), as before"
