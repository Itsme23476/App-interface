"""Filect Assistant — "Speak replies" (OFF by default) — Windows port of the Mac tests/test_live_speak.py.
No real agents, no network, no sound out loud (the real-player checks play at volume 0); your settings.json is never
written.
  · spoken_line: with the note sent, the agent's FIRST PARAGRAPH (it decides: short, or long when you asked for more;
    capped ~2.5 min) — else the first sentence (+ its question); "needs your OK to …", "That didn't work: …"; never
    code / links / headings / lists; a bare "4" is said as "4"
  · a long reply is said in pieces, the next fetched while one plays; only the agent's FINAL message (not its "I'll
    create the file…" before a tool) is spoken
  · louder(): quiet audio turned up to peak -1 dBFS, never clipped (Windows: MP3 → WAV with Filect's ffmpeg)
  · Speaker: one line at a time, in order; waits while held; Esc/stop silences it even mid-download; no audio (signed
    out / offline / limit) or a broken player → Windows' own voice (SAPI)
  · the Agents page: only HANDS-FREE turns are spoken (Shift+Z / typed ones never); the agent is asked for a one-line
    summary only when Speak replies is on; a follow-up queued by voice keeps that; approval / done / final error are
    reported
  · the tab: the toggle is OFF by default and saved; it speaks only when on AND listening; waits while you talk / use a
    shortcut; stops on Esc, a Filect shortcut, Stop. Windows has no echo cancellation → the mic is ignored while
    Filect speaks (the Mac's "noise filter off" path), so talking over it can't stop it there
Run: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_live_speak.py -q"""
import io
import json
import os
import sys
import time
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
from app.core import agent_runner as ar  # noqa: E402
from app.core import agent_sessions as asess  # noqa: E402
from app.core import codex_runner as crun  # noqa: E402
from app.core import intent_router as ir  # noqa: E402
from app.core import live_listener as ll  # noqa: E402
from app.core import speech_out as so  # noqa: E402
from app.core import supabase_client as sc  # noqa: E402
from app.core import transcription as tx  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402
from app.ui import live_page as lp  # noqa: E402

saves, tracked = [], []
_PAGES = []
_SETTINGS = (("live_send_after_sec", 2.0), ("live_sensitivity", "medium"), ("live_chime", True),
             ("live_noise_filter", True), ("live_speak", False), ("live_voice", "sal"), ("live_tour_seen", False),
             ("live_settings_collapsed", False), ("assistant_auto_open", True), ("assistant_bypass", False),
             ("assistant_model", ""), ("assistant_effort", ""), ("assistant_agent", "claude"),
             ("agents_list_collapsed", True), ("agents_tour_seen", False))


class _NoNet:
    """The voice service's HTTP session in tests: nothing ever leaves the PC."""
    def _no(self, *a, **k):
        raise RuntimeError("no network in tests")
    post = get = options = _no


class _NoRouter:
    def classify(self, *a, **k):
        raise RuntimeError("no real router in tests")


class _NeverRunner:
    """Never the real Claude Code / Codex: a test that forgets to fake one fails instead of starting an agent."""
    def __init__(self, *a, **k):
        pass

    async def run(self, *a, **k):
        raise RuntimeError("no real agents in tests")

    async def interrupt(self):
        pass


class _NoHook:
    """The listener's low-level key hook in tests: never installed (keys are fed through _key_event)."""
    def __init__(self, on_event):
        self.installed = False

    def start(self):
        pass

    def stop(self):
        pass


def _no_codex(*a, **k):
    raise RuntimeError("no real Codex in tests")


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch, tmp_path):
    """Never the user's settings / app-data files, no sound, no analytics, no network, no real agents or router; every
    live_* / assistant_* setting starts known and is restored after."""
    monkeypatch.setattr(settings, "_save_config", lambda *a, **k: saves.append(1))
    for k, v in _SETTINGS:
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(sc, "track", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_track", lambda ev, **k: tracked.append((ev, k)))
    monkeypatch.setattr(lp, "_chime", lambda name="Tink": None)
    monkeypatch.setattr(ap, "_track", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_play_chime", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(ap.AssistantPage, "_show_idle_overlays", lambda self: None)
    monkeypatch.setattr(ap.AssistantPage, "_load_recent", lambda self: None)
    monkeypatch.setattr(ap, "DEFAULT_WORKSPACE", str(tmp_path / "Filect Assistant"))
    monkeypatch.setattr(asess, "_touched_path", lambda: tmp_path / "assistant_sessions.json")
    monkeypatch.setattr(asess, "_projects_root", lambda: tmp_path / "claude-projects")
    monkeypatch.setattr(ar, "_models_cache_path", lambda: tmp_path / "claude_models.json")
    monkeypatch.setattr(ar, "AgentRunner", _NeverRunner)
    monkeypatch.setattr(crun, "CodexRunner", _NeverRunner)
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: _NoRouter()))
    monkeypatch.setattr(ir, "classify_with_codex", _no_codex)
    monkeypatch.setattr(so, "_HTTP", _NoNet())
    monkeypatch.setattr(tx, "_get_auth_token", lambda: None)
    monkeypatch.setattr(ll, "_KeyHook", _NoHook)
    monkeypatch.setattr(ll, "_usage_path", lambda: tmp_path / "assistant_usage.json")
    orig = ap.AssistantPage.__init__

    def track(self, *a, **k):
        orig(self, *a, **k)
        _PAGES.append(self)
    monkeypatch.setattr(ap.AssistantPage, "__init__", track)
    saves.clear()
    tracked.clear()
    yield
    while _PAGES:
        _PAGES.pop().shutdown()


def _pump(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


def _speakers():
    """A real audio output device (the real-player checks play at volume 0 on it)."""
    try:
        from PySide6.QtMultimedia import QMediaDevices
        return not QMediaDevices.defaultAudioOutput().isNull()
    except Exception:
        return False


def _sapi_wav(text: str, path: str, fmt_type: int = 26) -> None:
    """Windows' own voice (SAPI) straight into a WAV file — 26 = 24 kHz 16-bit mono. Nothing is played. (Retried:
    SAPI under x64 emulation now and then fails a Speak() with E_OUTOFMEMORY.)"""
    import comtypes.client
    err = None
    for _ in range(5):
        try:
            voice = comtypes.client.CreateObject("SAPI.SpVoice")
            stream = comtypes.client.CreateObject("SAPI.SpFileStream")
            fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
            fmt.Type = fmt_type
            stream.Format = fmt
            stream.Open(path, 3)                            # SSFMCreateForWrite
            voice.AudioOutputStream = stream
            try:
                voice.Speak(text)
            finally:
                stream.Close()
            return
        except Exception as e:
            err = e
            time.sleep(0.2)
    raise err


# ---- what gets said -----------------------------------------------------------------------------------
def test_spoken_line():
    L = so.spoken_line
    assert L("done", {"text": "4"}) == "4", "a bare answer is said as it is"
    assert L("done", {"text": "## Summary\nI created `index.html` with a hero and a menu. See https://a.b/c\n"
                              "```html\n<div>\n```"}) == "I created index.html with a hero and a menu."
    assert L("done", {"text": "**Results:**\n- Moved 12 files into Kitchen.\n- Skipped 2."}) == "Moved 12 files into Kitchen."
    assert L("done", {"text": "I found two Kitchen folders. Should I merge them? I'll wait.",
                      "question": "Should I merge them?"}) == "I found two Kitchen folders. Should I merge them?"
    assert L("done", {"text": "Made it blue.\n\nAnything else?", "question": "Anything else?"}) == "Made it blue. Anything else?"
    assert L("done", {"text": ""}) == "Done." and L("done", {"text": "```\nonly code\n```"}) == "Done."
    long = L("done", {"text": "word " * 200 + "end."})
    assert len(long) <= 221 and long.endswith("…"), "one long sentence is cut on a word"
    assert L("done", {"text": "Έτοιμο. Έφτιαξα τη σελίδα."}) == "Έτοιμο. Έφτιαξα τη σελίδα.", "Greek as-is"
    assert L("approval", {"tool": "Bash", "input": {"command": "rm -rf x"}}) == "It needs your OK to run a command. Yes or no?"
    assert L("approval", {"tool": "Write", "input": {"file_path": "/a/b/index.html"}}) == \
        "It needs your OK to create index.html. Yes or no?"
    assert L("approval", {"tool": "Edit", "input": {}}) == "It needs your OK to edit a file. Yes or no?"
    assert L("error", {"text": "Claude Code isn't signed in. Open Terminal to sign in. More text."}) == \
        "That didn't work: Claude Code isn't signed in. Open Terminal to sign in."
    assert L("error", {"text": ""}) == "That didn't work." and L("weird", {"text": "x"}) == ""
    assert all(len(L("done", {"text": "x" * 5000})) <= so.MAX_LINE for _ in [0])
    # the read-aloud note is never shown as your words
    note = so.SPEAK_NOTE
    assert so.strip_speak_note(f"make a page\n\n{note}") == "make a page"
    assert so.strip_speak_note(f"make a page\n\n{note}\n\n(Screenshot of my screen attached — x.)") == \
        "make a page\n\n(Screenshot of my screen attached — x.)"
    assert so.strip_speak_note("no note (here)") == "no note (here)"
    st = so.is_stop_phrase
    assert st("Stop.") and st("Hey, stop talking, please. Thank you.") and st("Be quiet") and st("Shut up Jarvis")
    assert not st("Yes.") and not st("No.") and not st("okay") and not st("Thanks"), "answers are never 'stop'"
    assert not st("Stop the server on port 3000") and not st("stop making it blue, make it red"), "real requests"
    from app.core.codex_runner import _strip_recap
    assert _strip_recap(f"[You're picking up a conversation …]\n\nmake a page\n\n{note}") == "make a page"


# ---- the speaker ----------------------------------------------------------------------------------------
class FakePlayer:
    made = []

    def __init__(self, data):
        if data == b"BROKEN":
            raise RuntimeError("bad audio")
        self.data, self.on, self.stopped = data, False, False
        FakePlayer.made.append(self)

    def play(self):
        self.on = True

    def playing(self):
        return self.on

    def stop(self):
        self.on, self.stopped = False, True


class FakeVoice(FakePlayer):
    def __init__(self, line):
        self.line, self.on, self.stopped = line, False, False
        FakePlayer.made.append(self)


def _speaker(fetch_map=None, delay=0.0):
    FakePlayer.made = []
    fetched = []

    def fetch(line):
        fetched.append(line)
        time.sleep(delay)                     # (the fetch thread: a slow download)
        return (fetch_map or {}).get(line, b"MP3:" + line.encode())
    sp = so.Speaker(fetch=fetch, make_player=FakePlayer, make_fallback=FakeVoice)
    sp.GRACE_SEC = 0.0
    ev = []
    sp.started.connect(lambda: ev.append("started"))
    sp.finished.connect(lambda: ev.append("finished"))
    return sp, fetched, ev


def _finish_current():
    for p in FakePlayer.made:
        p.on = False


def test_speaker_queue_hold_stop_fallback():
    sp, fetched, ev = _speaker()
    sp.say("one")
    sp.say("two")
    assert _pump(lambda: ev == ["started"], 2) and FakePlayer.made[0].data == b"MP3:one" and sp.speaking
    _finish_current()
    assert _pump(lambda: len(FakePlayer.made) == 2 and FakePlayer.made[1].on, 2), "then the next, in order"
    assert FakePlayer.made[1].data == b"MP3:two"
    _finish_current()
    assert _pump(lambda: ev[-1] == "finished" and not sp.speaking, 2)
    # stop while it plays: silent at once, queue dropped
    sp.say("three")
    sp.say("four")
    assert _pump(lambda: any(p.on for p in FakePlayer.made), 2)
    sp.stop()
    assert all(not p.on for p in FakePlayer.made) and FakePlayer.made[-1].stopped and not sp.speaking
    assert ev[-1] == "finished"
    _pump(lambda: False, 0.2)
    assert "four" not in fetched, "Esc dropped what was waiting"
    # stop while it's still being fetched: it never plays
    sp2, fetched2, ev2 = _speaker(delay=0.3)
    sp2.say("slow")
    _pump(lambda: fetched2, 1)
    sp2.stop()
    _pump(lambda: False, 0.6)
    assert not FakePlayer.made and "started" not in ev2
    # held (you're talking): nothing starts; let go → it plays
    sp3, fetched3, ev3 = _speaker()
    sp3.hold(True)
    sp3.say("after you")
    _pump(lambda: False, 0.2)
    assert fetched3 == [] and not FakePlayer.made
    sp3.hold(False)
    assert _pump(lambda: FakePlayer.made and FakePlayer.made[0].on, 2)
    sp3.stop()
    # you start talking WHILE it's being fetched: it waits for you, and isn't fetched twice
    sp4, fetched4, ev4 = _speaker(delay=0.3)
    sp4.say("wait for me")
    _pump(lambda: fetched4, 1)
    sp4.hold(True)
    _pump(lambda: False, 0.6)
    assert not FakePlayer.made and not sp4.speaking
    sp4.hold(False)
    assert _pump(lambda: FakePlayer.made and FakePlayer.made[0].on, 2) and fetched4 == ["wait for me"]
    sp4.stop()
    # no audio (signed out / offline / over the limit) → Windows' own voice; a broken player → too
    sp5, _, ev5 = _speaker(fetch_map={"offline": None, "broken": b"BROKEN"})
    sp5.say("offline")
    assert _pump(lambda: FakePlayer.made and isinstance(FakePlayer.made[0], FakeVoice), 2)
    assert FakePlayer.made[0].line == "offline"
    _finish_current()
    sp5.say("broken")
    assert _pump(lambda: len(FakePlayer.made) == 2 and isinstance(FakePlayer.made[1], FakeVoice), 2)
    sp5.stop()
    # too many waiting: the oldest go (stale news)
    sp6, fetched6, _ = _speaker()
    sp6.hold(True)
    for i in range(7):
        sp6.say(f"line {i}")
    assert [q[0] for q in sp6._queue] == ["line 3", "line 4", "line 5", "line 6"]
    sp6.stop()
    # the real Speaker's defaults: streamed audio, Windows' players, Windows' own voice as the fallback
    sp7 = so.Speaker()
    assert sp7._fetch is so.fetch_live and sp7._make_player is so._player_for and sp7._make_fallback is so._SystemVoice


def test_agent_chooses_what_is_said():
    """With the note sent, the agent's FIRST PARAGRAPH is what's said — short or long, its choice — never the lists /
    code / links after it; capped at ~2.5 min."""
    L = lambda text, **k: so.spoken_line("done", {"text": text, "noted": True, **k})  # noqa: E731
    assert L("4") == "4"
    expand = ("Lift comes from the wing's shape and its angle. Air over the top moves faster, so the "
              "pressure there drops, and the wing is pushed up. Thrust from the engines keeps air moving "
              "over the wings, and the tail keeps the plane steady.")
    assert L(f"{expand}\n\nMore:\n- Bernoulli\n- Newton's third law\n\n```py\nlift()\n```") == expand, \
        "the whole first paragraph — what you asked to hear — and nothing after it"
    assert L("Done — I made the landing page.\n\n## Changes\n- `index.html`: hero\n- see https://x.y") == \
        "Done — I made the landing page.", "a finished task: its one sentence"
    assert L("## Answer\n**Short:** it's *4*.\n\nDetails…") == "Short: it's 4.", "headings / emphasis gone"
    assert L("**Results:**\n\nMoved 12 files into Kitchen.\n\n- a\n- b") == "Moved 12 files into Kitchen.", \
        "a label isn't the answer"
    assert L("I made two versions.\n\nWhich one do you want?", question="Which one do you want?") == \
        "I made two versions. Which one do you want?", "its question is still asked"
    assert L("- Moved 12 files.\n- Skipped 2.") == "Moved 12 files.", "opened with a list: the first item"
    assert L("See [the docs](https://a.b) and run `npm test`.") == "See the docs and run npm test."
    assert "code" not in L("Here it is:\n```\ncode\n```\nThat's it."), "code is never read out"
    poem = ("Sea Song\n\nThe tide unfolds upon the sand,  \nThen slips beyond the reach of land.  \n"
            "The moon lays silver on the foam,  \nAnd every wave comes singing home.")
    assert L(poem) == ("Sea Song. The tide unfolds upon the sand, Then slips beyond the reach of land. "
                       "The moon lays silver on the foam, And every wave comes singing home."), \
        "his test: a title over a poem → the title AND the poem (it said just 'Sea Song')"
    assert L("**The Old Lighthouse**\n\nIt stood on the cliff for a hundred years.") == \
        "The Old Lighthouse. It stood on the cliff for a hundred years."
    assert L("Two plus two is four\n\nDetails: 2 + 2.") == "Two plus two is four", "an answer isn't a title"
    assert L("Done\n\nI made the page.") == "Done" and L("Sea Song") == "Sea Song", "one word / nothing after"
    assert L("Sea Song\n\n- line one\n- line two") == "Sea Song", "a title over a list: just the title"
    assert L("Sea Song\nThe waves unfold beneath the moon,\nAnd hum the shore a silver tune.") == \
        "Sea Song. The waves unfold beneath the moon, And hum the shore a silver tune.", "title on the 1st line"
    assert L("Lift comes from the wings\nand the engines push it forward.") == \
        "Lift comes from the wings and the engines push it forward.", "a wrapped sentence isn't a title"
    huge = " ".join(f"Sentence number {i} says a little more." for i in range(150))
    out = L(huge)
    assert out.endswith("The rest is on screen.") and len(out) <= so.SPOKEN_MAX + 30, len(out)
    assert out[:-len(" The rest is on screen.")].endswith("."), "cut after a whole sentence"
    assert len(so.spoken_line("done", {"text": huge})) <= so.MAX_LINE, "no note (turned on mid-task): short"
    assert so.strip_speak_note(f"tell me more\n\n{so.SPEAK_NOTE}") == "tell me more", "the new note is hidden"
    old = ("(This reply will be read aloud — start it with one short, plain sentence that sums up the "
           "result.)")
    assert so.strip_speak_note(f"make a page\n\n{old}") == "make a page", "older chats' notes too"
    assert ")" not in so.SPEAK_NOTE[:-1] and "\n" not in so.SPEAK_NOTE
    # pieces for the voice
    C = so._chunks
    assert C("Two plus two is four.") == ["Two plus two is four."], "one sentence: one piece"
    assert C("Done. The page is ready.") == ["Done.", "The page is ready."], "short too: the first one alone"
    assert C("One. Two. Three.") == ["One.", "Two. Three."], "…then the rest packed"
    assert C("") == [""] and C("no stop at the end") == ["no stop at the end"]
    parts = C(huge)
    assert parts[0] == "Sentence number 0 says a little more.", "the first sentence alone: it starts at once"
    assert all(len(p) <= so.MAX_LINE for p in parts) and " ".join(parts) == huge, "nothing lost, none too big"
    run_on = "word, " * 120 + "end"
    assert all(len(p) <= so.MAX_LINE for p in C(run_on)) and " ".join(C(run_on)).split() == run_on.split()


def test_long_reply_in_pieces():
    """A long reply: piece after piece with the next one fetched WHILE one plays (no gaps, no 'finished' in between);
    Esc stops the rest; no audio for the first piece → Windows' voice for the whole line; a long reply counts as ONE
    line in the queue."""
    long = " ".join(f"This is sentence {i} of a long reply that goes on." for i in range(20))
    parts = so._chunks(long)
    assert len(parts) >= 3
    sp, fetched, ev = _speaker(delay=0.05)
    sp.say(long)
    assert _pump(lambda: FakePlayer.made and FakePlayer.made[0].on, 2)
    assert _pump(lambda: len(fetched) == 2, 2) and fetched == parts[:2], "the next piece comes while this plays"
    _pump(lambda: False, 0.3)
    assert len(fetched) == 2, "…only ONE ahead (Esc after the first sentence wastes nothing more)"
    for n in range(1, len(parts)):
        _finish_current()
        assert _pump(lambda: len(FakePlayer.made) == n + 1 and FakePlayer.made[n].on, 2), n
        assert sp.speaking and "finished" not in ev, "still one reply: no 'finished' between pieces"
    assert [p.data for p in FakePlayer.made] == [b"MP3:" + p.encode() for p in parts], "in order, all of it"
    _finish_current()
    assert _pump(lambda: ev[-1] == "finished" and not sp.speaking, 2) and ev.count("finished") == 1
    # Esc in the middle: quiet at once, the rest never fetched
    sp2, fetched2, ev2 = _speaker()
    sp2.say(long)
    assert _pump(lambda: len(fetched2) == 2, 2)
    sp2.stop()
    _pump(lambda: False, 0.3)
    assert len(fetched2) == 2 and not sp2.speaking and ev2[-1] == "finished"
    # no audio (offline / over the daily limit): Windows' voice says the WHOLE line, fetched once
    sp3, fetched3, _ = _speaker(fetch_map={parts[0]: None})
    sp3.say(long)
    for n in range(len(parts)):
        assert _pump(lambda: len(FakePlayer.made) == n + 1 and isinstance(FakePlayer.made[n], FakeVoice), 2), n
        _finish_current()
    assert fetched3 == [parts[0]] and [v.line for v in FakePlayer.made] == parts
    sp3.stop()
    # one long reply = one line in the queue; the next line isn't fetched until its turn
    sp4, fetched4, _ = _speaker()
    sp4.hold(True)
    sp4.say(long)
    for i in range(4):
        sp4.say(f"line {i}")
    assert [q[0] for q in sp4._queue] == ["line 0", "line 1", "line 2", "line 3"], "the oldest LINE went whole"
    sp4.stop()
    sp5, fetched5, _ = _speaker()
    sp5.say("short one")
    sp5.say(long)
    assert _pump(lambda: FakePlayer.made and FakePlayer.made[0].on, 2)
    _pump(lambda: False, 0.2)
    assert fetched5 == ["short one"], "the next line waits for its turn (Esc may drop it)"
    sp5.stop()


# ---- Windows' real players (replaces the Mac's AVAudioPlayer + NSSpeechSynthesizer check) ------------------------
def test_real_windows_clip_player(tmp_path):
    """A whole clip (WAV, as louder() hands it back) → the real Qt sink plays it (volume 0 — silent) and Stop ends it;
    bytes that aren't WAV go to Qt's media player instead (a temp file, removed on stop)."""
    if not _speakers():
        pytest.skip("no audio output device")
    _sapi_wav("Done, the landing page is ready.", str(tmp_path / "a.wav"))
    data = (tmp_path / "a.wav").read_bytes()
    p = so._player_for(data)
    assert isinstance(p, so._PCMPlayer) and isinstance(p._s, so._ClipStream), type(p)
    secs = len(p._s._buf) / 2 / p._s.rate
    assert secs > 0.5, secs
    p._sink.setVolume(0.0)
    p.play()
    assert p.playing()
    _pump(lambda: False, 0.2)
    p.stop()
    assert not p.playing(), "Stop: quiet at once"
    with pytest.raises(Exception):
        so._ClipStream(b"RIFF not really a wav file")                 # (the Speaker then uses Windows' voice)
    q = so._player_for(b"ID3 an mp3 louder() couldn't decode")
    assert isinstance(q, so._QtClipPlayer) and os.path.exists(q._path)
    q.stop()                                                           # (never played)
    assert not os.path.exists(q._path), "its temp file goes on stop"


def test_real_windows_voice():
    """Windows' own voice (SAPI — the fallback; the Mac's NSSpeechSynthesizer) speaks async and stops at once. Volume 0
    (silent). A Greek line picks a Greek voice when one is installed."""
    try:
        v = so._SystemVoice("This is a longer sentence, so the voice is still talking while the test checks on it.")
    except Exception as e:
        pytest.skip(f"SAPI unavailable: {e}")
    v._v.Volume = 0
    if int(v._v.Volume) != 0:
        pytest.skip("couldn't mute the system voice — not playing it out loud")
    t0 = time.monotonic()
    v.play()
    assert time.monotonic() - t0 < 0.5, "async: play() doesn't wait for it to finish"
    assert _pump(v.playing, 3), "it's talking (silently)"
    v.stop()
    assert _pump(lambda: not v.playing(), 2), "stop: quiet at once"
    g = so._SystemVoice("Έτοιμο.")
    greek = g._v.GetVoices("Language=408")
    if greek.Count:
        assert g._v.Voice.Id == greek.Item(0).Id, "a Greek voice for Greek text"
    else:
        assert g._v.Voice is not None, "no Greek voice installed: the default voice says it"


def test_undecodable_audio_falls_back_to_windows_voice():
    """The Mac's 'garbage must not play' (→ the system voice says it), end to end through the Speaker with Windows'
    real player choice (_player_for — muted anyway; the bytes can't be decoded; the fallback is a fake)."""
    FakePlayer.made = []
    players = []

    def muted(data):
        p = so._player_for(data)                              # the real choice — it raises, or it doesn't
        out = getattr(p, "_out", None) or getattr(p, "_sink", None)
        if out is not None:
            out.setVolume(0.0)
        players.append(p)
        return p
    sp = so.Speaker(fetch=lambda line: b"not audio", make_player=muted, make_fallback=FakeVoice)
    try:
        sp.say("Hello there.")
        _pump(lambda: FakePlayer.made, 3)
    finally:
        sp.stop()
        for p in players:
            p.stop()                                          # (its temp file goes)
    assert FakePlayer.made and isinstance(FakePlayer.made[0], FakeVoice), "the line is said by Windows' voice"


# ---- the streamed voice --------------------------------------------------------------------------------------
class _Resp:
    closed = False

    def close(self):
        _Resp.closed = True


def _pcm(peak_db, secs, rate=24000):
    t = np.arange(int(rate * secs)) / rate
    return (np.sin(2 * np.pi * 220 * t) * 32767 * 10 ** (peak_db / 20)).astype("<i2").tobytes()


def test_streamed_voice():
    """The live assistant's voice, streamed: turned up as it arrives but never clipped; half samples carried over;
    unused streams closed on stop; no stream → the old whole clip; the live Speaker uses all this by default."""
    s = so._PCMStream(_Resp(), 24000)
    s.add(_pcm(-12, 0.1))                                # quiet start → turned up like louder() would
    first = np.frombuffer(s.take(10 ** 9), dtype="<i2")
    assert abs(20 * np.log10(np.abs(first).max() / 32767) - (-12 + 20 * np.log10(so.PEAK * 32767 / so.STREAM_PEAK_FLOOR))) < 0.2
    s.add(_pcm(-2, 0.1))                                 # a loud moment → less gain, never clipped
    loud = np.frombuffer(s.take(10 ** 9), dtype="<i2").astype(np.int32)
    assert np.abs(loud).max() <= so.PEAK * 32767 + 1, "never clips"
    s2 = so._PCMStream(_Resp(), 24000)
    s2.add(b"\x01")
    s2.add(b"\x00\x02\x00")                            # a sample split across two chunks
    assert len(s2.take(100)) == 4 and not s2.pending()
    s3 = so._PCMStream(_Resp(), 24000)
    s3.add(b"\x00" * 10)
    s3.add(b"\x00" * 10)
    assert len(s3.take(6)) == 6 and len(s3.take(100)) == 14, "take() splits pieces right"

    def broken():
        yield b"\x00\x00" * 50
        raise OSError("connection reset")
    _Resp.closed = False
    s4 = so._PCMStream(_Resp(), 24000)
    s4.read_rest(broken())
    assert s4.done and s4.pending() and _Resp.closed, "broke off → says what came, done, closed"
    _Resp.closed = False
    s4.cancel()
    assert s4.cancelled and _Resp.closed

    # the Speaker closes streams it won't play
    made = []

    def fetch(line):
        st = so._PCMStream(_Resp(), 24000)
        st.add(b"\x00\x00")
        made.append(st)
        return st
    sp = so.Speaker(fetch=fetch, make_player=FakePlayer, make_fallback=FakeVoice)
    FakePlayer.made = []
    sp.say("One. Two.")
    assert _pump(lambda: FakePlayer.made and len(made) == 2, 2), "playing piece 1, piece 2 fetched"
    sp.stop()
    assert _pump(lambda: made[1].cancelled, 2), "the next piece's stream is closed when you talk over it"
    _pump(lambda: False, 0.2)
    assert len(FakePlayer.made) == 1, "…and never played"

    # no stream → the whole clip from `speak`, as before; a stream → used
    real_stream, real_fetch = so.stream_speech, so.fetch_speech
    try:
        so.stream_speech = lambda line, voice=None: None
        so.fetch_speech = lambda line, voice=None: b"MP3"
        assert so.fetch_live("Hi.") == b"MP3"
        st = so._PCMStream(_Resp(), 24000)
        so.stream_speech = lambda line, voice=None: st
        assert so.fetch_live("Hi.") is st
    finally:
        so.stream_speech, so.fetch_speech = real_stream, real_fetch
    sp = so.Speaker()
    assert sp._fetch is so.fetch_live and sp._make_player is so._player_for
    # signed out → no stream, no clip: Windows' voice (nothing was sent anywhere)
    assert so.stream_speech("Hi.") is None and so.fetch_speech("Hi.") is None and so.fetch_live("Hi.") is None


def test_streamed_voice_real_player():
    """The real Qt player (volume 0 — silent) plays ALL of a stream before saying it's done (regression: it stopped
    0.25 s early — QtAudio vs QAudio enum); Stop → silent, the download closed."""
    if not _speakers():
        pytest.skip("no audio output device")
    s5 = so._PCMStream(_Resp(), 24000)
    s5.add(_pcm(-6, 0.6))
    s5.done = True
    p = so._PCMPlayer(s5)
    p._sink.setVolume(0.0)
    t0 = time.monotonic()
    p.play()
    assert _pump(lambda: not p.playing(), 5)
    took = time.monotonic() - t0
    assert took > 0.5 and abs(p._sink.processedUSecs() / 1e6 - 0.6) < 0.05, (took, p._sink.processedUSecs())
    s6 = so._PCMStream(_Resp(), 24000)
    s6.add(_pcm(-6, 1.0))
    p = so._PCMPlayer(s6)
    p._sink.setVolume(0.0)
    p.play()
    _pump(lambda: False, 0.2)
    p.stop()
    assert not p.playing() and s6.cancelled, "stop → silent, download closed"
    assert isinstance(so._player_for(s5), so._PCMPlayer)


# ---- the Agents page ------------------------------------------------------------------------------------
class FakeWorker(QObject):
    """Stands in for AgentWorker: records what the agent would be sent; never runs anything."""
    step = Signal(str, object)
    approvalRequested = Signal(str, object)
    result = Signal(str, str, float)
    error = Signal(str, str)
    done = Signal(str)
    session_known = Signal(str, str)
    sent = []

    def __init__(self, key, cwd, prompt, **kw):
        super().__init__()
        self.key = key
        FakeWorker.sent.append(prompt)

    def run(self):
        pass

    def resolve(self, ok):
        pass

    def stop(self):
        pass

    def cancel(self):
        pass

    def kill_process(self):
        pass

    def set_allow_all(self, on):
        pass


class _MW:
    def _on_nav_clicked(self, i):
        pass


class _Router:
    def __init__(self, intent="NEW"):
        self.intent = intent

    def classify(self, text, focused, cands, live=False, asked=None):
        return {"intent": self.intent, "index": None, "task": text}


def _agents():
    page = ap.AssistantPage(_MW())
    page._primary = "claude"
    page._notch = lambda *a: None
    page._agent_ok = lambda agent: True
    events = []
    page.live_event.connect(lambda k, kind, p: events.append((k, kind, p)))
    return page, events


def _wait_sent(n):
    return _pump(lambda: len(FakeWorker.sent) >= n, 5)


def test_agents_page_reports_only_hands_free_turns(monkeypatch):
    monkeypatch.setattr(ap, "AgentWorker", FakeWorker)
    FakeWorker.sent = []
    router = _Router("NEW")
    monkeypatch.setattr(ir.IntentRouter, "get", classmethod(lambda cls: router))
    page, events = _agents()
    # 1) hands-free, Speak replies ON → the agent is asked for a one-line summary; done is reported
    settings.live_speak = True
    page.run_voice_task("make a landing page for my coffee shop", source="live", tag=1)
    assert _wait_sent(1)
    assert FakeWorker.sent[-1] == f"make a landing page for my coffee shop\n\n{so.SPEAK_NOTE}"
    s = page._focused
    assert s.live_turn and s.prompt == "make a landing page for my coffee shop", "the card shows your words"
    s.result_text = "I made the page. Want it in blue?"
    page._on_result(s.key, "sid-1", 0.0)
    assert events[-1][:2] == (s.key, "done") and events[-1][2] == {"text": "I made the page. Want it in blue?",
                                                                   "noted": True, "question": "Want it in blue?"}
    page._on_done(s.key)
    # 1b) what's spoken is the agent's FINAL message — not its "I'll create it…" before a tool call
    from app.core.agent_runner import Step
    router.intent = "FOLLOW_UP"
    page.run_voice_task("put a haiku in haiku.txt", source="live", tag=11)
    assert _wait_sent(2)
    s = page._focused
    page._on_step(s.key, Step("text", text="I'll create the file with a short haiku."))
    page._on_step(s.key, Step("tool", tool="Write", detail="haiku.txt", input={"file_path": "C:/w/haiku.txt"}))
    page._on_step(s.key, Step("text", text="Created haiku.txt with a coffee haiku.\n\n- haiku.txt"))
    page._on_result(s.key, "sid-1", 0.0)
    assert events[-1][2]["text"] == "Created haiku.txt with a coffee haiku.\n\n- haiku.txt", events[-1][2]
    assert "I'll create" in s.result_text, "the card still has everything"
    assert so.spoken_line("done", events[-1][2]) == "Created haiku.txt with a coffee haiku."
    page._on_done(s.key)
    FakeWorker.sent.pop()
    # 2) hands-free, Speak replies OFF → no note sent (the tab decides not to speak); the turn says so
    settings.live_speak = False
    router.intent = "FOLLOW_UP"
    page.run_voice_task("make the header darker", source="live", tag=2)
    assert _wait_sent(2) and FakeWorker.sent[-1] == "make the header darker"
    assert page._focused.live_turn
    page._focused.result_text = "Darker now."
    page._on_result(page._focused.key, "sid-1", 0.0)
    assert events[-1][2]["noted"] is False, "no note went out → the speaker won't read its first paragraph"
    page._on_done(page._focused.key)
    # 3) Shift+Z (the voice chord — not hands-free) → not a live turn: never reported, never a note
    settings.live_speak = True
    router.intent = "FOLLOW_UP"
    page.run_voice_task("now add a footer", source="voice")
    assert _wait_sent(3) and FakeWorker.sent[-1] == "now add a footer"
    s = page._focused
    assert not s.live_turn
    n = len(events)
    s.result_text = "Added a footer."
    page._on_result(s.key, "sid-1", 0.0)
    assert len(events) == n, "Shift+Z / typed turns are never spoken"
    page._on_done(s.key)
    # 4) a hands-free follow-up QUEUED behind a running (typed) turn keeps being hands-free
    page.run_task("typed task: rename the files")             # typed: running, not live
    assert _wait_sent(4)
    s = page._focused
    assert not s.live_turn and s.running
    router.intent = "FOLLOW_UP"
    page.run_voice_task("and zip them", source="live", tag=3)
    assert _pump(lambda: s.pending_followup == "and zip them", 3) and s.pending_live
    s.session_id = "sid-typed"                               # (a real chat has its id by then)
    page._on_done(s.key)                                      # the typed turn ends → the follow-up runs
    assert _wait_sent(5) and FakeWorker.sent[-1] == f"and zip them\n\n{so.SPEAK_NOTE}"
    assert page._focused is s and s.live_turn and not s.pending_live
    # 5) approval + a FINAL error are reported for a hands-free turn
    req = type("Req", (), {"tool": "Bash", "tool_input": {"command": "zip -r a.zip ."}})()
    page._on_approval(s.key, req)
    assert events[-1][1] == "approval" and events[-1][2] == {"tool": "Bash", "input": {"command": "zip -r a.zip ."}}
    page._on_error(s.key, "Something broke. Details here.")
    assert events[-1][1] == "error" and events[-1][2] == {"text": "Something broke. Details here."}
    page._on_done(s.key)
    # 6) a typed turn on that same (once hands-free) chat is not spoken
    page.run_task("typed again", resume="sid-1")
    assert _wait_sent(6) and not page._focused.live_turn
    page._on_done(page._focused.key)


# ---- the tab ----------------------------------------------------------------------------------------------
class FakeListener(QObject):
    state_changed = Signal(str, str)
    level = Signal(float)
    countdown = Signal(float)
    utterance_started = Signal(int)
    utterance_ended = Signal(int)
    speech_confirmed = Signal(int)
    heard = Signal(int, str)
    dropped = Signal(int, str, str)
    usage = Signal(float)
    esc_late = Signal()
    draw_tap = Signal()
    draw_spoiled = Signal()

    def __init__(self):
        super().__init__()
        # Windows: like the real LiveListener, no echo cancellation (Qt has no voice processing)
        self.on, self.esc_filter, self.echo_cancelled, self.muted = False, None, False, []

    def start(self):
        self.on = True
        self.state_changed.emit("listening", "")

    def stop(self):
        self.on = False
        self.state_changed.emit("off", "")

    def set_muted(self, on):
        self.muted.append(on)

    def learn(self, uid):
        pass

    def apply_settings(self):
        pass

    def used_today(self):
        return 0.0

    def set_answer_probe(self, fn):
        pass

    def set_esc_filter(self, fn):
        self.esc_filter = fn


class FakeSpeaker(QObject):
    started = Signal()
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.said, self.holds, self.stops, self.speaking, self.data = [], [], 0, False, None

    def say(self, line, data=None):
        self.said.append(line)
        self.data = data
        self.speaking = True

    def hold(self, on):
        self.holds.append(on)

    def stop(self):
        self.stops += 1
        if self.speaking:
            self.speaking = False
            self.finished.emit()


class FakeAP(QObject):
    live_routed = Signal(object, str, str, str)
    live_screen_used = Signal(object)
    live_event = Signal(object, str, object)

    def __init__(self):
        super().__init__()
        self.ov = type("Ov", (), {"set_listening": lambda s, m: None, "flash_note": lambda s, *a, **k: None})()
        self._overlay = None
        self.sent = []

    def _ensure_notch(self):
        return self.ov

    def live_answer_context(self):
        return None

    def run_voice_task(self, text, **k):
        self.sent.append(text)


def _tab():
    mw = type("MW", (), {})()
    mw.assistant_page = FakeAP()
    L, sp = FakeListener(), FakeSpeaker()
    page = lp.LivePage(mw, listener=L, speaker=sp, capture=lambda rect: None)
    return page, L, sp, mw.assistant_page


def test_tab_speak_replies():
    page, L, sp, AP = _tab()
    assert not page.speak_toggle.isChecked() and page.speak_toggle.isEnabled(), "OFF by default, usable"
    page.start()
    AP.live_event.emit("k1", "done", {"text": "I made the page."})
    assert sp.said == [], "OFF: never says anything"
    n = len(saves)
    page.speak_toggle.click()
    assert settings.live_speak is True and len(saves) == n + 1 and page.speak_toggle.isChecked()
    AP.live_event.emit("k1", "done", {"text": "I made the page. ```x```"})
    assert sp.said == ["I made the page."] and sp.holds[-1] is False
    assert ("live_spoke", {"kind": "done"}) in tracked, "telemetry: which moment only, never the words"
    AP.live_event.emit("k1", "approval", {"tool": "Bash", "input": {}})
    assert sp.said[-1] == "It needs your OK to run a command. Yes or no?"
    # you're talking / pausing / using a Filect shortcut: it waits (held); a shortcut also silences it at once
    L.state_changed.emit("hearing", "")
    assert sp.holds[-1] is True
    L.state_changed.emit("countdown", "")
    assert sp.holds[-1] is True
    L.state_changed.emit("listening", "")
    assert sp.holds[-1] is False
    stops = sp.stops
    L.state_changed.emit("paused", "Paused while you use a Filect shortcut")
    assert sp.stops == stops + 1 and sp.holds[-1] is True
    L.state_changed.emit("listening", "")
    # Windows has no echo cancellation (the listener never claims it): while Filect speaks the mic is IGNORED
    # (muted) — the Mac's own "noise filter off" path — so talking over it can't stop it here.
    sp.speaking = True
    sp.started.emit()
    assert L.muted[-1] is True, "no echo cancellation: the mic is ignored while it speaks"
    L.utterance_started.emit(5)
    assert sp.speaking, "(muted: nothing to barge in with)"
    # if something still gets through while it talks: a plain "stop" isn't a task; a real request still is
    L.heard.emit(5, "Hey, stop talking, please. Thank you.")
    assert AP.sent == [] and page._rows[5].outcome.text() == "Stopped talking — not sent"
    L.utterance_started.emit(6)
    L.heard.emit(6, "Actually make the header red")
    assert AP.sent == ["Actually make the header red"], "a real request said over it goes through"
    sp.speaking = False
    sp.finished.emit()                                  # it just finished: the mic is back right after
    assert _pump(lambda: L.muted[-1] is False, 1), "…and back right after"
    L.utterance_started.emit(7)                        # "yes" right after it spoke is an answer
    L.heard.emit(7, "Yes.")
    assert AP.sent[-1] == "Yes.", "yes / no are answers, never dropped"
    L.utterance_started.emit(8)
    L.heard.emit(8, "Stop.")
    assert AP.sent[-1] == "Yes." and page._rows[8].outcome.text() == "Stopped talking — not sent", "just after it: too"
    page._spoke_until = 0.0                             # long after it spoke: a plain "stop" goes to the router
    L.utterance_started.emit(9)
    L.heard.emit(9, "Stop.")
    assert AP.sent[-1] == "Stop."
    # (the page's echo-cancelled branch — the Mac's default; the Windows listener never takes it)
    L.echo_cancelled = True
    n_mute = len(L.muted)
    sp.speaking = True
    sp.started.emit()
    assert len(L.muted) == n_mute, "with echo cancellation the mic stays on"
    L.utterance_started.emit(10)
    assert not sp.speaking, "…and talking over it stops it the moment you start"
    L.echo_cancelled = False
    # Esc: drawing first; then just stop talking; then drop waiting drawings
    sp.speaking = True
    assert L.esc_filter() is True and not sp.speaking
    page._drawings = [r"C:\tmp\x.png"]
    sp.speaking = True
    assert L.esc_filter() is True and page._drawings == [r"C:\tmp\x.png"], "1st Esc: quiet, drawings kept"
    assert L.esc_filter() is False and page._drawings == [], "2nd Esc: drawings dropped"
    # Stop: quiet; nothing said while off
    sp.speaking = True
    page.stop()
    assert not sp.speaking
    n = len(sp.said)
    AP.live_event.emit("k1", "done", {"text": "late"})
    assert len(sp.said) == n, "off: nothing"
    # the toggle off silences it too
    page.start()
    sp.speaking = True
    page.speak_toggle.click()
    assert settings.live_speak is False and not sp.speaking
    page.stop()


class _FakeMic:
    def __init__(self):
        self.calls = []

    def __call__(self, on_chunk, voice_processing=False, background=False, on_end=None):
        def stop():
            pass
        stop.proc = None
        self.calls.append({"vp": voice_processing})
        return stop


class _FakeMonitor:
    others, locked, exclude = [], False, set()

    def start(self):
        pass

    def stop(self):
        pass


def test_windows_listener_never_claims_echo_cancellation():
    """Why the page mutes: the real Windows listener never reports echo cancellation — even with the noise-filter
    setting on — and a muted listener drops what it was hearing."""
    settings.live_noise_filter = True
    mic = _FakeMic()
    L = ll.LiveListener(open_mic=mic, make_transcriber=lambda: None, monitor_factory=_FakeMonitor)
    L.start()
    try:
        assert L.on and mic.calls[0]["vp"] is False and L.echo_cancelled is False
        L._cur = [1, None]                                  # (something being heard right now)
        dropped = []
        L.dropped.connect(lambda u, t, w: dropped.append((u, w)))
        L.set_muted(True)
        assert L._muted and L._cur is None and dropped == [(1, "muted")]
        L.set_muted(False)
        assert not L._muted
    finally:
        L.stop()


# ---- louder() (Windows: Filect's ffmpeg; the Mac's afconvert) --------------------------------------------------
def test_louder(tmp_path):
    """Quiet audio is turned up to peak -1 dBFS (never clipped) and still plays; already-loud audio comes back as it
    was and anything that isn't audio comes back untouched. Windows: the server's MP3 comes back as WAV (the clip
    player's format)."""
    from app.core import video_analysis as va
    try:
        exe = va._ffmpeg_exe()
    except Exception:
        exe = None
    if not exe or not os.path.exists(str(exe)):
        pytest.skip("ffmpeg isn't available")
    _sapi_wav("Done, the landing page is ready.", str(tmp_path / "a.wav"))
    with wave.open(str(tmp_path / "a.wav")) as w:
        params, x = w.getparams(), np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")

    def wav(gain):
        b = io.BytesIO()
        with wave.open(b, "wb") as w:
            w.setparams(params)
            w.writeframes((x.astype(float) * gain / np.abs(x).max() * 32767).astype("<i2").tobytes())
        return b.getvalue()

    def frames(data):
        with wave.open(io.BytesIO(data)) as w:
            return w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(int)

    def peak(data):
        return np.abs(frames(data)[1]).max() / 32767

    quiet = wav(0.5)                                      # peaks at -6 dBFS, like Sal (-5.4)
    out = so.louder(quiet)
    assert out[:4] == b"RIFF" and abs(peak(out) - so.PEAK) < 0.01, peak(out)
    clip = so._ClipStream(out)                            # what the Windows clip player plays
    assert len(clip._buf) / 2 / clip.rate > 1.0
    assert so.louder(wav(0.95)) == wav(0.95), "already loud: untouched"
    assert peak(so.louder(wav(0.01))) <= 0.01 * so.MAX_GAIN + 0.001, "a near-silent clip stays quiet"
    assert so.louder(b"not audio") == b"not audio"
    # the real server sends MP3: decoded + turned up into a WAV the clip player takes
    (tmp_path / "q.wav").write_bytes(quiet)
    r = va._run_ffmpeg(["-i", str(tmp_path / "q.wav"), "-y", str(tmp_path / "q.mp3")], timeout=30)
    assert r.returncode == 0 and (tmp_path / "q.mp3").exists()
    out = so.louder((tmp_path / "q.mp3").read_bytes())
    assert out[:4] == b"RIFF" and abs(peak(out) - so.PEAK) < 0.02, peak(out)
    assert isinstance(so._ClipStream(out), so._ClipStream)


# ---- the voice picker -------------------------------------------------------------------------------------
def test_voice_picker(monkeypatch, tmp_path):
    """The voice picker: Sal by default; the picked one is used for every line; ▶ plays a sample that's fetched once
    and cached (instant + free after that); no sample (offline) → nothing plays, a note."""
    assert [v[0] for v in so.VOICES] == ["sal", "ara", "eve", "leo", "rex"]
    settings.live_voice = "sal"
    assert so.current_voice() == "sal"
    settings.live_voice = "eve"
    assert so.current_voice() == "eve"
    settings.live_voice = "nobody"
    assert so.current_voice() == "sal", "not one of ours → Sal"
    # every line goes out in the picked voice
    sent = []

    class _R:
        status_code, content, headers, text = 200, b"ID3audio", {"content-type": "audio/mpeg"}, ""

    class _S:                                    # (fetch_speech reuses one keep-alive session)
        def post(self, url, **k):
            sent.append(k["json"])
            return _R()
    with monkeypatch.context() as m:
        m.setattr(so, "_HTTP", _S())
        m.setattr(tx, "_get_auth_token", lambda: "tok")
        m.setattr(so, "louder", lambda a: a)
        settings.live_voice = "leo"
        assert so.fetch_speech("hello") == b"ID3audio" and sent[-1]["voice"] == "leo"
        assert so.fetch_speech("hi", "ara") and sent[-1]["voice"] == "ara", "a preview names its own voice"
    # previews: fetched once, then from the cache
    d = tmp_path / "appdata"
    d.mkdir()
    fetched = []
    with monkeypatch.context() as m:
        m.setattr(type(settings), "get_app_data_dir", lambda self: d)
        m.setattr(so, "fetch_speech", lambda line, voice=None: (fetched.append((line, voice)), b"W" * 5000)[1])
        assert so.preview_audio("eve") == b"W" * 5000 and fetched[-1] == (so.PREVIEW_LINE.format(name="Eve"), "eve")
        assert so.preview_audio("eve") == b"W" * 5000 and len(fetched) == 1, "the 2nd time: from the cache"
        assert (d / "voice_previews" / f"eve-{so.PREVIEW_VERSION}.audio").exists()
        assert so.preview_audio("nobody") is None and len(fetched) == 1
        m.setattr(so, "fetch_speech", lambda line, voice=None: None)
        assert so.preview_audio("rex") is None and not (d / "voice_previews" / "rex-1.audio").exists(), \
            "offline: nothing (and nothing cached)"
    # Speaker plays ready audio without fetching
    sp, fetched2, ev = _speaker()
    sp.say("Hi, I'm Eve.", data=b"PREVIEW")
    assert _pump(lambda: FakePlayer.made and FakePlayer.made[0].on, 2) and FakePlayer.made[0].data == b"PREVIEW"
    assert fetched2 == []
    sp.stop()
    # the tab: five rows, the current one checked; picking saves; ▶ plays the sample through the speaker
    settings.live_voice = "sal"
    page, L, fsp, AP = _tab()
    assert list(page.voice_btns) == ["sal", "ara", "eve", "leo", "rex"] and page.voice_btns["sal"].isChecked()
    assert all(page.voice_play[v].text() == "▶" for v in page.voice_play)
    n = len(saves)
    page.voice_btns["ara"].click()
    assert settings.live_voice == "ara" and len(saves) == n + 1 and page.voice_btns["ara"].isChecked()
    assert not page.voice_btns["sal"].isChecked()
    with monkeypatch.context() as m:
        m.setattr(so, "preview_audio", lambda v: b"SAMPLE-" + v.encode())
        page.voice_play["leo"].click()
        assert page.voice_play["leo"].text() == "…" and not page.voice_play["leo"].isEnabled()
        assert _pump(lambda: fsp.said, 2) and fsp.said[-1] == so.PREVIEW_LINE.format(name="Leo")
        assert fsp.data == b"SAMPLE-leo" and page.voice_play["leo"].text() == "▶" and fsp.holds[-1] is False
        m.setattr(so, "preview_audio", lambda v: None)
        n_said = len(fsp.said)
        page.voice_play["rex"].click()
        assert _pump(lambda: "couldn't" in page._voice_lbl.text(), 2) and len(fsp.said) == n_said
        assert page.voice_play["rex"].isEnabled()
    assert settings.live_voice == "ara", "previewing doesn't change your pick"
    # saved / loaded
    d2 = tmp_path / "appdata2"
    d2.mkdir()
    with monkeypatch.context() as m:
        m.setattr(type(settings), "get_app_data_dir", lambda self: d2)
        (d2 / "settings.json").write_text(json.dumps({"live_chime": True}))
        assert type(settings)().live_voice == "sal", "default: Sal"
        (d2 / "settings.json").write_text(json.dumps({"live_voice": "eve"}))
        assert type(settings)().live_voice == "eve" and type(settings)()._cfg_dict()["live_voice"] == "eve"
        (d2 / "settings.json").write_text(json.dumps({"live_voice": "Robert'); DROP"}))
        assert type(settings)().live_voice == "sal", "junk ignored"


def test_settings_default_off(monkeypatch, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"live_chime": True}))
    monkeypatch.setattr(type(settings), "get_app_data_dir", lambda self: tmp_path)
    s2 = type(settings)()
    assert s2.live_speak is False, "a fresh / older settings file: OFF"
    (tmp_path / "settings.json").write_text(json.dumps({"live_speak": True}))
    assert type(settings)().live_speak is True and s2._cfg_dict()["live_speak"] is False
