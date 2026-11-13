"""Filect Assistant (hands-free) — the listener (Windows port of the Mac test). Real speech made with Windows' own
voice (SAPI → a 16 kHz recording; the Mac used `say`), a fake mic
and a fake transcriber, so every rule is exercised exactly:
  · one sentence → one request; a short pause (< "send after") stays ONE request; a long one → two
  · coughs / one-word noises are dropped; a quiet far-away voice doesn't start anything (once it knows you)
  · only your speech is sent (never the silence before/after) — that's what counts toward the limit
  · it lets go of the mic for a Filect hotkey (instantly), another app using the mic, a locked screen,
    and comes back after; no voice processing → plain mic; a mic that keeps dying → stops with an error
  · Esc cancels what you're saying; Stop drops what's still being transcribed; a one-word result and the
    daily-limit error are handled
Your settings.json is never written; the usage file goes to a temp dir.
Run: .venv/Scripts/python -m pytest tests/test_live_listener.py"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pytest  # noqa: E402
from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
settings._save_config = lambda: None            # NEVER write the user's settings.json
from app.core import live_listener as ll  # noqa: E402
from app.core import mic_helper  # noqa: E402

_TMP = Path(tempfile.mkdtemp())
ll._usage_path = lambda: _TMP / "assistant_usage.json"
settings.live_send_after_sec, settings.live_sensitivity, settings.live_noise_filter = 2.0, "medium", True
RATE = 16000
_CACHE = {}


def say_wav(text: str, path: str) -> None:
    """Windows' own voice (SAPI) straight into a 16 kHz 16-bit mono WAV — nothing is played."""
    import comtypes.client
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
    fmt.Type = 18                                   # SAFT16kHz16BitMono
    stream.Format = fmt
    stream.Open(path, 3)                            # SSFMCreateForWrite
    voice.AudioOutputStream = stream
    voice.Speak(text)
    stream.Close()


def speech(text: str, gain: float = 0.35) -> np.ndarray:
    """`text` spoken by Windows, 16 kHz int16, scaled to the Mac test's level: peak-normalised, then × gain
    (0.35 ≈ a person close to the mic, RMS ~0.04)."""
    if text not in _CACHE:
        d = tempfile.mkdtemp()
        say_wav(text, f"{d}/s.wav")
        import wave
        w = wave.open(f"{d}/s.wav")
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        a = a / max(1.0, float(np.max(np.abs(a)))) * 32767.0 * 0.6               # (`say` peaks ~-4 dBFS)
        loud = np.nonzero(np.abs(a) > 0.01 * 32767)[0]                             # SAPI pads ~0.3 s of silence
        a = a[max(0, loud[0] - 160): loud[-1] + 160] if len(loud) else a           #   at both ends; `say` doesn't
        _CACHE[text] = a
    return np.clip(_CACHE[text] * gain, -32768, 32767).astype(np.int16)


def silence(secs: float, noise: float = 0.0004) -> np.ndarray:
    """Room silence after Apple's noise filter (measured ~0.0002–0.0005 RMS)."""
    rng = np.random.default_rng(1)
    return (rng.standard_normal(int(secs * RATE)) * noise * 32768).astype(np.int16)


def run_detector(audio: np.ndarray, send_after: float = 2.0, det=None, block: int = 1600):
    det = det or ll.UtteranceDetector(send_after, "medium")
    events, sent = [], 0
    b = audio.tobytes()
    for i in range(0, len(b), block * 2):
        for ev in det.feed(b[i:i + block * 2]):
            events.append(ev)
            if ev[0] in ("start", "audio"):
                sent += len(ev[1])
    return events, sent / (RATE * 2), det


def kinds(events):
    return [e[0] for e in events if e[0] != "audio"]


def _pump(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


# ---- the detector ---------------------------------------------------------------------------------
def test_one_sentence_is_one_request_and_only_speech_is_sent():
    s = speech("Create a new folder on my desktop called invoices.")
    audio = np.concatenate([silence(1.5), s, silence(4.0)])
    ev, sent, _ = run_detector(audio)
    assert kinds(ev) == ["start", "pause", "end"], kinds(ev)
    st = ev[-1][1]
    assert st["speech_sec"] > 1.5 and st["level"] > 0.01, st
    spoken = len(s) / RATE
    assert spoken - 0.3 < sent < spoken + 1.0, (sent, spoken)       # speech + 0.4 s lead-in + the 0.5 s gap
    assert sent < len(audio) / RATE - 3.5, "the silence before/after was never sent"
    print(f"one sentence → one request; sent {sent:.2f}s of {len(audio)/RATE:.1f}s (speech {spoken:.2f}s)  OK")


def test_send_after_timing():
    s = speech("Summarize my last three emails please.")
    for send_after in (1.0, 2.0, 4.0):
        audio = np.concatenate([silence(1.0), s, silence(send_after + 2.0)])
        det = ll.UtteranceDetector(send_after, "medium")
        b, t_end, t = audio.tobytes(), None, 0
        for i in range(0, len(b), 3200):
            evs = det.feed(b[i:i + 3200])
            t += 0.1
            if any(e[0] == "end" for e in evs) and t_end is None:
                t_end = t
        speech_end = 1.0 + len(s) / RATE
        assert t_end is not None and send_after - 0.6 < t_end - speech_end < send_after + 0.5, (send_after, t_end, speech_end)
    print("ends 'send after' seconds after you stop talking (1 s / 2 s / 4 s)  OK")


def test_short_pause_stays_one_request_long_pause_makes_two():
    a, b = speech("Open my project folder."), speech("And then rename the latest file to final.")
    one = np.concatenate([silence(1.0), a, silence(1.0), b, silence(3.5)])
    ev, _, _ = run_detector(one, send_after=2.0)
    assert kinds(ev) == ["start", "pause", "resume", "pause", "end"], kinds(ev)
    two = np.concatenate([silence(1.0), a, silence(3.5), b, silence(3.5)])
    ev, _, _ = run_detector(two, send_after=2.0)
    assert kinds(ev) == ["start", "pause", "end", "start", "pause", "end"], kinds(ev)
    print("a 1 s pause stays one request; a 3.5 s pause makes two  OK")


def test_noise_and_coughs_dont_count():
    ev, sent, _ = run_detector(silence(6.0))
    assert ev == [] and sent == 0, ev
    rng = np.random.default_rng(3)
    clicks = silence(4.0)
    for at in (0.5, 1.6, 2.9):                                    # door knocks / keyboard thumps
        i = int(at * RATE)
        clicks[i:i + 800] = (rng.standard_normal(800) * 0.3 * 32768).clip(-32768, 32767).astype(np.int16)
    ev, _, _ = run_detector(clicks)
    assert "end" not in kinds(ev), kinds(ev)
    cough = np.concatenate([silence(1.0), speech("Hm.", 0.5), silence(3.0)])
    ev, _, _ = run_detector(cough)
    assert "end" not in kinds(ev), kinds(ev)                      # never started, or dropped as too short
    print("room noise, knocks and a lone 'hm' never become a request  OK")


def babble(secs: float, level: float = 0.012) -> np.ndarray:
    """Speech-LIKE room noise (a TV / people talking far away + hiss) — what fooled the detector live
    with the filter off: webrtcvad called 80% of the real room "speech"."""
    parts = [speech(t, 1.0).astype(np.float32) for t in (
        "The weather tomorrow will be mostly sunny with a few clouds in the afternoon.",
        "And in other news the market closed slightly higher today after a quiet session.",
        "Welcome back to the show, tonight we have a very special guest joining us.")]
    n = int(secs * RATE)
    mix = np.zeros(n, dtype=np.float32)
    for k, p in enumerate(parts):
        rep = np.tile(p, int(np.ceil(n / len(p))) + 1)
        off = int(k * 0.37 * RATE)
        mix += rep[off:off + n]
    mix += np.random.default_rng(5).standard_normal(n).astype(np.float32) * 2000
    mix *= level * 32768 / float(np.sqrt(np.mean(mix ** 2)))
    return mix.clip(-32768, 32767).astype(np.int16)


def test_noisy_room_without_the_filter():
    """REGRESSION (live, filter off): the request never ended in a noisy room. Now the room's own level
    is learned and speech must stand clearly above it."""
    ev, sent, _ = run_detector(babble(12.0))
    assert "end" not in kinds(ev), ("background chatter alone became a request", kinds(ev))
    s = speech("What is seven times eight? Just answer with the number.", 0.6)   # you, close to the mic
    bg = babble(1.5 + len(s) / RATE + 5.0)
    fg = np.concatenate([np.zeros(int(1.5 * RATE), np.int16), s, np.zeros(int(5.0 * RATE), np.int16)])
    mixed = (bg.astype(np.int32) + fg.astype(np.int32)).clip(-32768, 32767).astype(np.int16)
    det = ll.UtteranceDetector(2.0, "medium")
    b, t_end, t = mixed.tobytes(), None, 0
    evs = []
    for i in range(0, len(b), 3200):
        e = det.feed(b[i:i + 3200])
        evs += e
        t += 0.1
        if any(x[0] == "end" for x in e) and t_end is None:
            t_end = t
    speech_end = 1.5 + len(s) / RATE
    assert t_end is not None, ("never ended", kinds(evs))
    assert t_end - speech_end < 2.0 + 1.0, (t_end, speech_end)
    st = [x[1] for x in evs if x[0] == "end"][0]
    print(f"noisy room (chatter at 0.012, floor {det.noise_floor():.4f}): chatter alone → nothing; your request "
          f"→ ended {t_end - speech_end:.1f}s after you stopped (send-after 2 s), speech {st['speech_sec']}s  OK")


def test_far_voice_ignored_once_it_knows_you():
    near = speech("Find the contract I signed last week.", 0.35)
    far = speech("Did you watch the game last night?", 0.035)    # 20 dB quieter: across the room
    det = ll.UtteranceDetector(2.0, "medium")
    ev, _, det = run_detector(np.concatenate([silence(1.0), near, silence(3.0)]), det=det)
    det.learn(ev[-1][1]["level"])                                 # that one was really you
    ev, sent, _ = run_detector(np.concatenate([silence(1.0), far, silence(3.0)]), det=det)
    assert kinds(ev) == [] and sent == 0, kinds(ev)
    ev, _, _ = run_detector(np.concatenate([silence(1.0), near, silence(3.0)]), det=det)
    assert kinds(ev)[-1] == "end"                                 # you still are heard
    print(f"after learning your voice (level {det.ref:.3f}), a voice 20 dB quieter is ignored  OK")


def test_sensitivity_changes_the_floor():
    soft = speech("Please open the downloads folder.", 0.04)
    audio = np.concatenate([silence(1.0), soft, silence(3.0)])
    got = {s: kinds(run_detector(audio, det=ll.UtteranceDetector(2.0, s))[0]) for s in ("low", "medium", "high")}
    assert "end" in got["high"] and "end" not in got["low"], got
    print(f"a soft voice: high sensitivity hears it, low ignores it ({got})  OK")


# ---- the listener (fake mic + fake transcriber) ---------------------------------------------------
class FakeProc:
    def __init__(self, pid):
        self.pid, self.killed = pid, False

    def kill(self):
        self.killed = True


class FakeMic:
    """Records each open_mic call; push(audio) delivers blocks like the reader thread does."""

    def __init__(self):
        self.calls = []

    def __call__(self, on_chunk, voice_processing=False, background=False, on_end=None):
        stop_calls = []

        def stop():
            stop_calls.append(1)
        stop.proc = FakeProc(1000 + len(self.calls))
        stop.calls = stop_calls
        self.calls.append({"vp": voice_processing, "bg": background, "on_chunk": on_chunk, "on_end": on_end,
                           "stop": stop})
        return stop

    def push(self, audio, k=-1):
        cb = self.calls[k]["on_chunk"]
        b = audio.tobytes()
        for i in range(0, len(b), 3200):
            cb(b[i:i + 3200])
        app.processEvents()


class FakeTx(QObject):
    finished = Signal(str)
    error = Signal(str)
    reply = "Create a new folder called invoices"
    delay = 10                       # ms until the "transcript" arrives
    made = []

    def __init__(self):
        super().__init__()
        self.fed, self.stopped, self.cancelled = 0, False, False
        FakeTx.made.append(self)

    def cancel(self):                    # like the real one: stops; a streamed text still arrives
        self.cancelled = True
        self.stop_recording()

    def start(self):
        pass

    def feed(self, data):
        self.fed += len(data)

    def stop_recording(self):
        self.stopped = True
        r = FakeTx.reply
        if isinstance(r, Exception):
            QTimer.singleShot(FakeTx.delay, lambda: self.error.emit(str(r)))
        else:
            QTimer.singleShot(FakeTx.delay, lambda: self.finished.emit(r))


class FakeMonitor:
    others, locked, exclude, started = [], False, set(), 0

    def __init__(self):
        FakeMonitor.started += 1

    def start(self):
        pass

    def stop(self):
        pass


def _listener():
    FakeTx.made.clear()
    FakeTx.reply, FakeTx.delay = "Create a new folder called invoices", 10
    FakeMonitor.others, FakeMonitor.locked = [], False
    mic = FakeMic()
    L = ll.LiveListener(open_mic=mic, make_transcriber=FakeTx, monitor_factory=lambda: FakeMonitor())
    rec = {"states": [], "heard": [], "dropped": [], "ended": []}
    L.state_changed.connect(lambda s, d: rec["states"].append((s, d)))
    L.heard.connect(lambda u, t: rec["heard"].append((u, t)))
    L.dropped.connect(lambda u, t, w: rec["dropped"].append((u, t, w)))
    L.utterance_ended.connect(lambda u: rec["ended"].append(u))
    return L, mic, rec


def test_listener_hears_a_request():
    L, mic, rec = _listener()
    L.start()
    try:
        assert mic.calls[0]["vp"] is False and mic.calls[0]["bg"], \
            "Windows: no voice processing (Qt has none) + background (doesn't mark the mic busy)"
        assert not L.echo_cancelled, "Windows never claims echo cancellation (the page mutes while Filect speaks)"
        assert L._monitor is not None and FakeMonitor.exclude == {1000} or L._monitor.exclude == {1000}
        s = speech("Create a new folder on my desktop called invoices.")
        mic.push(np.concatenate([silence(1.0), s, silence(3.0)]))
        assert _pump(lambda: rec["heard"], 2), rec
        assert rec["heard"] == [(1, "Create a new folder called invoices")]
        assert rec["ended"] == [1] and FakeTx.made[0].stopped
        fed = FakeTx.made[0].fed / 32000
        assert fed < len(s) / RATE + 1.0 and abs(L.used_today() - fed) < 0.01, (fed, L.used_today())
        assert ll.load_usage_today() == round(L.used_today(), 1)
        states = [s for s, _ in rec["states"]]
        assert states[:2] == ["starting", "listening"] and "hearing" in states and "countdown" in states
        L.learn(1)
        assert L._det.ref and L._det.ref > 0.01
    finally:
        L.stop()
    assert rec["states"][-1][0] == "off" and mic.calls[0]["stop"].calls
    print("listener: speech → 'heard' with the text; only speech fed + counted; usage saved; learns your level  OK")


def test_one_word_and_errors():
    L, mic, rec = _listener()
    L.start()
    try:
        FakeTx.reply = "Okay."
        mic.push(np.concatenate([silence(0.5), speech("Okay, sounds good to me then."), silence(3.0)]))
        assert _pump(lambda: rec["dropped"], 2) and not rec["heard"]
        assert rec["dropped"][0][2] == "too short to be a request", rec["dropped"]
        FakeTx.reply = RuntimeError("Daily voice limit reached. Try again tomorrow.")
        mic.push(np.concatenate([silence(0.5), speech("Open my calendar for tomorrow please."), silence(3.0)]))
        assert _pump(lambda: rec["states"][-1][0] == "error", 2), rec["states"][-3:]
        assert not L.on and "limit" in rec["states"][-1][1]
    finally:
        L.stop()
    print("a one-word result is dropped; the daily-limit error stops listening with a message  OK")


def test_esc_and_stop_drop_what_isnt_finished():
    L, mic, rec = _listener()
    L.start()
    try:
        s = speech("Delete all the files in my downloads folder.")
        mic.push(np.concatenate([silence(0.5), s[: len(s) // 2]]))       # mid-sentence…
        assert L._cur is not None
        L.cancel_current()                                                # …Esc
        assert rec["dropped"][-1][2] == "cancelled" and L._cur is None
        mic.push(np.concatenate([s[len(s) // 2:], silence(3.0)]))       # you keep talking: the rest of that
        _pump(lambda: False, 0.3)                                        # sentence is NOT a new request
        _pump(lambda: False, 0.3)
        assert not rec["heard"], rec["heard"]
        FakeTx.reply = "Rename the folder to archive"
        # (its text comes 0.6 s after the sentence ends: with 10 ms it often came back before Stop was pressed - a
        #  race in this test, not the app - so "still transcribing" wasn't what was being tested; 2026-11-13)
        FakeTx.delay = 600
        mic.push(np.concatenate([silence(1.0), speech("Rename the folder to archive."), silence(2.1)]))
        n = len(rec["dropped"])
        assert not rec["heard"] and L._pending, "the sentence should have ended and still be being transcribed"
        L.stop()                                                          # ended, still transcribing → Stop
        _pump(lambda: False, 1.0)                                         # (its late text arrives - and is ignored)
        assert not rec["heard"], "Stop: nothing runs after you stopped"
        assert any(d[2] == "stopped" for d in rec["dropped"][n:]) or not L._pending
    finally:
        L.stop()
    print("Esc mid-sentence drops it; Stop drops what was still being transcribed  OK")


def test_esc_at_every_stage():
    """REGRESSION (his live test): Esc must cancel the request at ANY stage before it runs — while you
    talk, during the pause, while it's being transcribed — and show WHAT was cancelled. With nothing in
    flight it hands over to the page (esc_late: the router may still be checking one)."""
    L, mic, rec = _listener()
    late = []
    L.esc_late.connect(lambda: late.append(1))
    L.start()
    try:
        s = speech("Move all my screenshots into a folder called old stuff.")
        # 1) during the pause (you stopped talking, the 2 s countdown is running)
        mic.push(np.concatenate([silence(0.5), s, silence(1.0)]))
        assert L.state == "countdown" and L._cur is not None, L.state
        L._on_esc()
        assert rec["dropped"][-1][2] == "cancelled" and L._cur is None
        _pump(lambda: False, 0.2)
        mic.push(silence(3.0))
        assert not rec["heard"], "cancelled in the pause → never heard/run"
        # 2) while it's being transcribed (after the countdown ended)
        FakeTx.reply, FakeTx.delay = "Move all my screenshots into a folder called old stuff", 400
        mic.push(np.concatenate([silence(0.5), s, silence(2.4)]))
        assert L._pending and L._cur is None, (L._pending, L._cur)
        uid = max(L._pending)
        L._on_esc()
        assert rec["dropped"][-1] == (uid, "", "cancelled") and not L._pending
        assert _pump(lambda: rec["dropped"][-1][1], 2), "the text still arrives — to show what was cancelled"
        assert rec["dropped"][-1] == (uid, "Move all my screenshots into a folder called old stuff", "cancelled")
        assert not rec["heard"], "cancelled while transcribing → never run"
        # 3) nothing in flight → esc_late (the page cancels one the router is still checking)
        L._on_esc()
        assert late == [1]
    finally:
        L.stop()
    print("Esc cancels during the pause and while transcribing (and shows what was said); otherwise → "
          "esc_late for the router stage  OK")


def test_short_yes_only_when_an_answer_is_expected():
    """A short "yes" is noise — unless the agent just asked you something. Noise is never even SENT
    (it's held on the Mac until it's clearly speech), so it costs nothing from the daily limit."""
    yes = speech("Yes.")
    audio = np.concatenate([silence(1.0), yes, silence(3.0)])
    det = ll.UtteranceDetector(2.0, "medium")
    assert kinds(run_detector(audio, det=det)[0])[-1] == "drop", "a lone 'yes' is too short normally"
    det = ll.UtteranceDetector(2.0, "medium")
    det.min_speech = ll.MIN_ANSWER_SEC
    assert kinds(run_detector(audio, det=det)[0])[-1] == "end", "…but counts while an answer is expected"

    L, mic, rec = _listener()
    expected = {"v": False}
    L.set_answer_probe(lambda: expected["v"])
    L.start()
    try:
        used0 = L.used_today()
        mic.push(audio)                                                 # nobody asked anything
        _pump(lambda: False, 0.3)
        assert FakeTx.made == [] and L.used_today() == used0, "noise is never sent / never counted"
        assert not rec["heard"]
        FakeTx.reply = "Yes."
        expected["v"] = True                                            # the agent asked a question
        mic.push(audio)
        assert _pump(lambda: rec["heard"], 2), rec
        assert rec["heard"][-1][1] == "Yes." and len(FakeTx.made) == 1 and L.used_today() > used0
        # Esc on something being SENT: cancelled → no batch upload after it
        FakeTx.reply = "Delete the old folder"
        s = speech("Delete the old folder on my desktop please.")
        mic.push(np.concatenate([silence(0.5), s[: len(s) * 2 // 3]]))
        assert L._cur is not None and L._cur[1] is not None
        L._on_esc()
        assert FakeTx.made[-1].cancelled, "cancel() → the transcriber skips its batch fallback"
        # Esc before it's clearly speech: nothing was ever sent
        n = len(FakeTx.made)
        mic.push(silence(1.0))
        mic.push(np.concatenate([silence(0.5), s[:2400]]))             # 0.15 s in
        if L._cur is not None:
            L._on_esc()
        assert len(FakeTx.made) == n, "nothing sent before it was clearly speech"
    finally:
        L.stop()
    print("a lone 'yes': never sent normally (0 s used), counts when the agent asked; Esc → no extra upload  OK")


def test_cancel_skips_the_batch_upload():
    """StreamingTranscriber.cancel(): if streaming came back empty, it must NOT upload the clip again."""
    from app.core.transcription import StreamingTranscriber
    tr = StreamingTranscriber(external=True)
    batches, done = [], []

    async def empty_session():
        return None, False
    tr._session = empty_session
    tr._finish_via_batch = lambda: batches.append(1)
    tr.finished.connect(lambda t: done.append(t))
    tr.cancel()
    tr._run_stream()                                   # (synchronously: the worker thread body)
    assert batches == [] and done == [""], (batches, done)
    tr2 = StreamingTranscriber(external=True)
    tr2._session = empty_session
    tr2._finish_via_batch = lambda: batches.append(2)
    tr2._run_stream()
    assert batches == [2], "a FAILED stream (no transcript.done) still falls back to batch"

    # REGRESSION (his test): a 1.7 s cough → the stream finished properly with no words → no re-upload
    async def heard_nothing_session(t):
        t._heard_nothing = True
        return None, False
    tr3, done3 = StreamingTranscriber(external=True), []
    tr3._session = lambda: heard_nothing_session(tr3)
    tr3._finish_via_batch = lambda: batches.append(3)
    tr3.finished.connect(lambda t: done3.append(t))
    tr3._run_stream()
    assert batches == [2] and done3 == [""], (batches, done3)
    tr4 = StreamingTranscriber()                                   # dictation: unchanged
    tr4._session = lambda: heard_nothing_session(tr4)
    tr4._finish_via_batch = lambda: batches.append(4)
    tr4._run_stream()
    assert batches == [2, 4], "dictation keeps its batch fallback exactly as before"
    print("cancelled / heard-nothing hands-free transcription skips the batch re-upload; failures and "
          "dictation still fall back  OK")


def test_lets_go_of_the_mic_and_comes_back():
    L, mic, rec = _listener()
    real_busy = mic_helper.busy
    busy = {"v": False}
    mic_helper.busy = lambda: busy["v"]
    L.start()
    try:
        assert _pump(lambda: True, 0.05)
        # 1) a Filect hotkey: the foreground hook kills the voice-processing child BEFORE it records
        first = mic.calls[-1]
        for fn in list(mic_helper._fg_hooks):
            fn()
        assert _pump(lambda: L.state == "paused", 1), L.state
        assert first["stop"].calls, "the mic is closed before the hotkey's capture starts"
        busy["v"] = True
        assert "Filect shortcut" in rec["states"][-1][1]
        _pump(lambda: False, 1.6)
        assert len(mic.calls) == 1, "stays paused while the hotkey records"
        # REGRESSION (his test, 01:10:25): the 1st dictation ends, and a 2nd shortcut starts BEFORE it
        # has come back — it used to stay "Paused while you use a Filect shortcut" forever.
        busy["v"] = False
        _pump(lambda: False, 0.5)                                       # (not back yet: needs ~1 s clear)
        assert len(mic.calls) == 1
        for fn in list(mic_helper._fg_hooks):                          # the 2nd shortcut
            fn()
        busy["v"] = True
        _pump(lambda: False, 0.8)
        busy["v"] = False                                               # …and it ends
        assert _pump(lambda: len(mic.calls) == 2, 3.5), "must come back after back-to-back shortcuts"
        mic.calls[-1]["on_chunk"](silence(0.1).tobytes())
        assert _pump(lambda: L.state == "listening", 1)
        # 2) another app records (Zoom)
        FakeMonitor.others = ["Zoom"]
        assert _pump(lambda: L.state == "paused", 1.5)
        assert rec["states"][-1][1] == "Paused while Zoom uses the mic" and mic.calls[-1]["stop"].calls
        FakeMonitor.others = []
        assert _pump(lambda: len(mic.calls) == 3, 3)
        mic.calls[-1]["on_chunk"](silence(0.1).tobytes())
        FakeMonitor.others = [ll.own_label()]                          # Filect's own capture winding down
        assert _pump(lambda: L.state == "paused", 1.5)
        assert rec["states"][-1][1] == "Paused while you use a Filect shortcut", rec["states"][-1]
        FakeMonitor.others = []
        assert _pump(lambda: len(mic.calls) == 4, 3)
        # 3) the screen locks
        FakeMonitor.locked = True
        assert _pump(lambda: L.state == "paused", 1.5) and "locked" in rec["states"][-1][1]
        FakeMonitor.locked = False
        assert _pump(lambda: len(mic.calls) == 5, 3)
        # 4) mid-sentence when a hotkey fires → that sentence is dropped, not half-sent
        mic.calls[-1]["on_chunk"](silence(0.1).tobytes())
        s = speech("Move these into the photos folder.")
        mic.push(np.concatenate([silence(0.5), s[: len(s) // 2]]))
        assert L._cur is not None
        busy["v"] = True
        assert _pump(lambda: L.state == "paused", 1.5)
        assert rec["dropped"][-1][2] == "paused" and not rec["heard"]
        busy["v"] = False
    finally:
        mic_helper.busy = real_busy
        L.stop()
    assert L._fg_hook not in mic_helper._fg_hooks, "the hook is removed on Stop"
    print("lets go of the mic for a hotkey (killed first), Zoom, a locked screen — and comes back after  OK")


def test_no_voice_processing_and_a_dying_mic():
    L, mic, rec = _listener()
    L.start()
    try:
        mic.calls[-1]["on_end"](1)                                         # the device stopped by itself
        assert _pump(lambda: len(mic.calls) == 2, 1.5) and mic.calls[-1]["vp"] is False, "reopened, plain mic"
        for i in range(5):                                               # keeps dying
            mic.calls[-1]["on_end"](-9)
            _pump(lambda n=len(mic.calls): len(mic.calls) > n or L.state == "error", 1.5)
        assert L.state == "error" and not L.on, (L.state, len(mic.calls))
        assert "keeps stopping" in rec["states"][-1][1]
    finally:
        L.stop()
    print("no voice processing → plain mic; a mic that keeps dying → stops with an error  OK")


def test_real_mic():
    """The REAL mic (QAudioSource) for 2 s: 16 kHz audio arrives in 100 ms blocks; it doesn't mark the mic busy;
    Stop ends it. (Records the room — nothing is sent anywhere.)"""
    got = []
    stop = mic_helper.open_mic(got.append, voice_processing=True, background=True)
    try:
        assert not mic_helper.busy()
        _pump(lambda: len(b"".join(got)) > 32000, 6)
    finally:
        stop()
    n = len(b"".join(got)) // 2
    assert n > 16000, n
    print(f"real mic: {n/RATE:.1f}s of 16 kHz audio, mic not marked busy  OK")


def test_mic_users_runs_fast():
    ll.mic_users()                                   # (the first call loads Windows' audio service once)
    t0 = time.process_time()
    for _ in range(5):
        users = ll.mic_users()
    cpu = (time.process_time() - t0) / 5 * 1000
    assert isinstance(users, list) and "Filect" not in users, users
    assert cpu < 20, cpu
    print(f"other-app mic check: {users} ({cpu:.1f} ms CPU)  OK")


_HOLDER = """
import sys
from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
app = QCoreApplication([])
fmt = QAudioFormat(); fmt.setSampleRate(16000); fmt.setChannelCount(1); fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
src = QAudioSource(QMediaDevices.defaultAudioInput(), fmt)
io = src.start()
t = QTimer(); t.timeout.connect(lambda: io.readAll()); t.start(100)
print("holding", flush=True)
QTimer.singleShot(int(float(sys.argv[1]) * 1000), app.quit)
app.exec()
src.stop()
"""


def test_mic_users_sees_another_app_but_not_filect():
    """REAL (Windows 11 24H2+): another program holding the microphone is seen — so hands-free pauses for Zoom, Teams,
    VS Code voice… (the old registry check saw nothing there) — and Filect's own mic never counts. Nothing recorded."""
    import subprocess
    from PySide6.QtMultimedia import QMediaDevices
    if QMediaDevices.defaultAudioInput().isNull():
        pytest.skip("no microphone")
    before = set(ll.mic_users())
    got = []
    stop = mic_helper.open_mic(got.append, background=True)        # Filect's own mic (this process)
    try:
        assert _pump(lambda: got, 5), "our mic delivers"
        assert set(ll.mic_users()) <= before, "Filect's own mic is never 'another app'"
    finally:
        stop()
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, "6"], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "holding"
        assert _pump(lambda: len(ll.mic_users()) > len(before), 4), "another program on the mic → reported"
        seen = ll.mic_users()
        assert "Python" in seen or any("process" in n for n in seen), seen
    finally:
        p.wait(15)
    assert _pump(lambda: set(ll.mic_users()) <= before, 4), "…and gone once it lets go"


def test_unfinished_wait_rule():
    u = ll.unfinished_wait
    assert u("Rename the folder on my desktop called", 2.0) == 6.0, "hangs on 'called' → ×3"
    assert u("Open the folder and", 2.0) == 6.0 and u("Move these files to the folder,", 2.0) == 6.0
    assert u("Make me a list of my files", 2.0) == 4.0, "not closed by the speech service → ×2"
    for done in ("What is the capital of Australia?", "Capital of Australia.", "Stop!", "yes", "", "Rename it."):
        assert u(done, 2.0) is None, done
    assert u("Rename the folder called", 5.0) == 10.0, "never over 10 s"
    assert u("Rename the folder called", 10.0) is None, "already the longest wait"


class _TextTx(FakeTx):
    """A transcriber whose words so far are known (as the streaming one's are) — the listener reads them."""
    text = ""

    def text_so_far(self):
        return _TextTx.text


def _ended_after(text, reply, pause=8.0):
    """Say something, then `pause` s of quiet: when (s of quiet) did the listener end it? None = it didn't."""
    FakeTx.made.clear()
    FakeMonitor.others, FakeMonitor.locked = [], False
    _TextTx.text, FakeTx.reply, FakeTx.delay = text, reply, 10
    mic = FakeMic()
    L = ll.LiveListener(open_mic=mic, make_transcriber=_TextTx, monitor_factory=lambda: FakeMonitor())
    ended, counts = [], []
    L.utterance_ended.connect(lambda u: ended.append(u))
    L.countdown.connect(lambda left: counts.append((round(left, 1), L.send_after_now())))
    L.start()
    try:
        mic.push(np.concatenate([silence(1.0), speech("Rename the folder on my desktop called")]))
        q = 0.0
        while q < pause and not ended:
            mic.push(silence(0.1))
            q += 0.1
        return (round(q, 1) if ended else None), counts, L
    finally:
        L.stop()


def test_unfinished_sentence_waits_longer():
    """"Rename the folder on my desktop called…" [you think] — the speech service didn't close it, it hangs on
    "called": it waits 6 s instead of 2 s, and the page's countdown is told the longer wait. A finished sentence
    still goes after ~0.8 s; an answer the agent is waiting for isn't held."""
    q, counts, L = _ended_after("Rename the folder on my desktop called", "Rename the folder on my desktop called")
    assert q is not None and 5.5 <= q <= 6.6, q
    assert any(total == 6.0 for _, total in counts), counts
    assert L.send_after_now() == 2.0, "the next request starts from the normal wait again"
    q, counts, _ = _ended_after("Rename the folder on my desktop called test stuff.", "Rename it.")
    assert q is not None and q <= 1.5, f"a finished sentence still goes quickly ({q} s)"
    q, _, _ = _ended_after("Make me a list of my files", "Make me a list of my files")
    assert q is not None and 3.5 <= q <= 4.6, q


if __name__ == "__main__":
    test_one_sentence_is_one_request_and_only_speech_is_sent()
    test_send_after_timing()
    test_short_pause_stays_one_request_long_pause_makes_two()
    test_noise_and_coughs_dont_count()
    test_noisy_room_without_the_filter()
    test_far_voice_ignored_once_it_knows_you()
    test_sensitivity_changes_the_floor()
    test_listener_hears_a_request()
    test_one_word_and_errors()
    test_esc_and_stop_drop_what_isnt_finished()
    test_esc_at_every_stage()
    test_short_yes_only_when_an_answer_is_expected()
    test_cancel_skips_the_batch_upload()
    test_lets_go_of_the_mic_and_comes_back()
    test_no_voice_processing_and_a_dying_mic()
    test_real_mic()
    test_mic_users_runs_fast()
    test_unfinished_wait_rule()
    test_unfinished_sentence_waits_longer()
    print("test_live_listener OK")


def test_changing_a_setting_never_restarts_the_mic(monkeypatch):
    """(Windows) Every switch in the Assistant tab calls apply_settings. The mic never has voice processing here, so
    a setting must not reopen it: the reopen dropped what you were saying and left it deaf for the 1 s it takes to
    learn the room again (found in the live organize test — the sentence right after the Preview switch was lost)."""
    from app.core.settings import settings
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: None)
    L, mic, rec = _listener()
    L.start()
    try:
        for on in (True, False, True):
            monkeypatch.setattr(settings, "live_noise_filter", on)
            L.apply_settings()
        monkeypatch.setattr(settings, "live_send_after_sec", 3.0)
        L.apply_settings()
        assert len(mic.calls) == 1 and not mic.calls[0]["stop"].calls, "the mic stayed open"
        assert L._det.send_after == 3.0, "the new send-after still applies"
        s = speech("Create a new folder on my desktop called invoices.")
        mic.push(np.concatenate([silence(1.0), s, silence(3.0)]))
        assert _pump(lambda: rec["heard"], 2), rec
    finally:
        L.stop()
