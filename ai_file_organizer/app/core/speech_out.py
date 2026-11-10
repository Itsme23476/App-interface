"""Windows port of the Mac app's speech_out.py (Speak replies). Everything that decides WHAT is said
and WHEN is the Mac's, unchanged. Windows pieces: whole clips are decoded + turned up with the ffmpeg Filect
already ships (Mac: afconvert) and played through a Qt audio sink (Mac: AVAudioPlayer); the fallback voice is
Windows' own (SAPI; Mac: NSSpeechSynthesizer). The streamed voice (`speak-stream` → _PCMPlayer) is plain Qt on
both.

Filect Assistant — "Speak replies" (a setting, OFF by default).

When it's on, a hands-free turn's moments that matter are spoken. The AGENT decides what's said (no
other AI involved): SPEAK_NOTE asks it to put what should be spoken in its first paragraph — as short as
it can (one sentence for a finished task, just the answer for a question), longer only when you asked
for more ("expand", "read it to me"). The card has the whole answer.
  · done      → that first paragraph, as plain words (at most SPOKEN_MAX, ~2.5 min, then "The rest is on
                screen.") + the question the answer ends with, if it isn't in it. A turn that went out
                without the note (Speak replies turned on mid-task) → just its first sentence or two.
  · approval  → "It needs your OK to run a command. Yes or no?" (a spoken yes / no answers it)
  · error     → "That didn't work: …"
Voice: xAI Grok TTS, voice "sal" (his pick), STREAMED through the `speak-stream` edge function (starts
playing on the first audio; else the whole clip from `speak` as before; both: auth + entitlement + a
daily character ceiling server-side; the key never leaves the server). No network / signed out /
over the limit → the Mac's own voice says it instead. `Speaker` plays one line at a time (queue),
can be held while you're talking, and stops at once (Esc, you start talking, Stop)."""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger("ai_file_organizer")

VOICE = "sal"                    # xAI voice id — "smooth and balanced" (his choice, 2026-11-06): the default
# The picker (Assistant tab): xAI's five main voices, with how xAI describes them (2026-11-07). Measured
# pitch earlier: Ara + Eve female; Leo, Rex, Sal male. Any other xAI voice id also works server-side.
VOICES = [("sal", "Sal", "Smooth and balanced", "male"),
          ("ara", "Ara", "Warm and conversational", "female"),
          ("eve", "Eve", "Energetic and upbeat", "female"),
          ("leo", "Leo", "Authoritative and strong", "male"),
          ("rex", "Rex", "Clear and professional", "male")]
PREVIEW_LINE = ("Hi, I'm {name}. When you ask me something, I'll tell you the answer out loud — short, "
                "unless you want to hear more.")
PREVIEW_VERSION = 1              # bump when PREVIEW_LINE changes (cached previews are re-fetched)
MAX_LINE = 300                   # characters per piece sent to the voice (the server allows 1000); a
                                 # longer line goes in pieces, the next fetched while one plays
SPOKEN_MAX = 2500                # a reply read out in full stops here (~2.5 min) — "The rest is on screen."
HARD_MAX = SPOKEN_MAX + 300      # the longest thing ever said (a full reply + its question)
MAX_QUEUE = 4                    # lines waiting; older ones beyond this are dropped (stale news)
# (no ")" or newline inside: _SPEAK_NOTE_RE finds it by its opening words — older chats' notes too)
SPEAK_NOTE = ("(This reply will be read aloud: put what should be spoken in your first paragraph, in plain "
              "spoken words with no lists, code or links. Keep it as short as you can, one or two "
              "sentences: for a finished task, what you did; for a question, just the answer; plus any "
              "question you need me to answer. Go longer only when I ask for more detail or to hear "
              "something in full, and then only as much as I asked for. Everything else goes after a "
              "blank line.)")
_SPEAK_NOTE_RE = re.compile(r"\s*\(This reply will be read aloud[^)\n]*\)")


def strip_speak_note(text: str) -> str:
    """Your words without the read-aloud note the app adds for the agent (shown nowhere)."""
    return _SPEAK_NOTE_RE.sub("", text or "")


# ---- the line to say ---------------------------------------------------------------------------------
def _plain(text: str) -> str:
    """Markdown → plain speakable text: no code blocks, links/URLs, headings, bullets or emphasis."""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)                # code blocks: never read out
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)                    # [text](url) → text
    t = re.sub(r"https?://\S+", "", t)                                 # addresses aren't read out
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)                         # headings
    t = re.sub(r"(?m)^\s*(?:[-*•+]|\d+[.)])\s+", "", t)                 # bullets / numbered lists
    t = re.sub(r"(?m)^\s*\|.*\|\s*$", " ", t)                           # tables
    t = re.sub(r"(\*\*|__|\*|_|~~)(\S(?:.*?\S)?)\1", r"\2", t)          # emphasis
    return t


def _first_sentences(text: str, limit: int = 220) -> str:
    """The first sentence or two (at most `limit` characters) of the first line that says something —
    skipping headings and short labels ("## Summary", "**Results:**"); a bare answer ("4") counts."""
    raw = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    lines = []
    for ln in raw.splitlines():
        if re.match(r"\s{0,3}#{1,6}\s", ln):                          # a heading: not the answer
            continue
        ln = " ".join(_plain(ln).split())
        if re.search(r"[^\W_]", ln):
            lines.append(ln)
    if not lines:
        return ""
    # a real sentence (≥ 5 words, or it ends like one), else whatever there is ("4", "Yes")
    para = next((ln for ln in lines if len(ln.split()) >= 5 or re.search(r"[.!?…]$", ln)), lines[0])
    out = ""
    for n, s in enumerate(re.split(r"(?<=[.!?…])\s+", para)):
        if out and (n >= 2 or len(out) + 1 + len(s) > limit or len(out) >= 60 or len(s.split()) < 3):
            break                                                        # (a 2nd one only if it says something)
        out = f"{out} {s}".strip()
    if len(out) > limit:                                                 # one long sentence: cut on a word
        out = out[:limit].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    return out


def _spoken_part(text: str) -> str:
    """What the agent wrote to be said (SPEAK_NOTE): its first paragraph — headings and short labels
    ("**Results:**") skipped — as plain words. "" when it opens with a list / table instead (it didn't
    follow the note: the caller falls back to the first sentence)."""
    t = re.sub(r"```.*?```", "\n\n", text or "", flags=re.S)           # code ends the paragraph
    title = ""
    for para in re.split(r"\n\s*\n", t):
        lines = [ln for ln in para.strip().splitlines() if not re.match(r"\s{0,3}#{1,6}\s", ln)]
        if not lines:
            continue
        if re.match(r"\s*(?:\||[-*•+]\s|\d+[.)]\s)", lines[0]):
            return title
        plain = " ".join(_plain("\n".join(lines)).split())
        if not re.search(r"[^\W_]", plain) or (plain.endswith(":") and len(plain.split()) <= 4):
            continue
        head = " ".join(_plain(lines[0]).split())
        if not title and len(lines) > 1 and _is_title(head):          # "Sea Song" on the poem's first line
            plain = f"{head}. {' '.join(_plain(chr(10).join(lines[1:])).split())}"
        elif not title and _is_title(plain):                           # "Sea Song" over a poem: say both
            title = plain
            continue
        return f"{title}. {plain}" if title else plain
    return title


_SMALL = {"a", "an", "the", "of", "and", "in", "on", "to", "for", "at", "by", "with", "or", "from"}


def _is_title(plain: str) -> bool:
    """A title on its own line ("Sea Song", "The Old Lighthouse") — not an answer ("Two plus two is
    four", "Done"): 2–6 words, Title Case, no end punctuation. (His test: Sal said just "Sea Song".)"""
    words = plain.split()
    if not 2 <= len(words) <= 6 or re.search(r"[.!?…:,;]$", plain):
        return False
    return all(w[0].isupper() or w.lower() in _SMALL for w in words if re.search(r"[^\W\d_]", w))


def _cap(text: str, limit: int = SPOKEN_MAX) -> str:
    """At most `limit` characters, cut after a sentence (else a word) + "The rest is on screen."."""
    if len(text) <= limit:
        return text
    cut = max(text.rfind(p, 0, limit) for p in (". ", "! ", "? ", "… "))
    head = text[:cut + 1] if cut > limit // 2 else text[:limit].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    return f"{head} The rest is on screen."


def _chunks(text: str, size: int = MAX_LINE) -> list:
    """A line → pieces for the voice: the first sentence alone (so it starts sooner — measured 0.85 s to
    make one sentence vs 1.19 s for two; the next piece is made while this one plays), then sentences
    packed up to `size` characters. One sentence = one piece."""
    out = []
    for s in re.split(r"(?<=[.!?…])\s+", text):
        while len(s) > size:                                           # one very long sentence
            cut = s.rfind(", ", 0, size)
            cut = cut + 1 if cut > size // 2 else (s.rfind(" ", 0, size) if " " in s[:size] else size)
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if not s:
            continue
        if len(out) >= 2 and len(out[-1]) + 1 + len(s) <= size:
            out[-1] = f"{out[-1]} {s}"
        else:
            out.append(s)
    return out or [text]


_ACTIONS = {"Bash": "run a command", "WebFetch": "open a web page", "WebSearch": "search the web",
            "Task": "start a helper agent"}


def _approval_what(tool: str, tool_input) -> str:
    inp = tool_input if isinstance(tool_input, dict) else {}
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    name = str(path).rstrip("/").rsplit("/", 1)[-1] if path else ""
    if tool in _ACTIONS:
        return _ACTIONS[tool]
    if tool == "Write":
        return f"create {name}" if name else "create a file"
    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"edit {name}" if name else "edit a file"
    if tool == "Read":
        return f"read {name}" if name else "read a file"
    return f"use {tool}" if tool else "go ahead"


def spoken_line(kind: str, payload) -> str:
    """What Filect says for one moment ("done" / "approval" / "error"), or "" (nothing worth saying)."""
    p = payload if isinstance(payload, dict) else {"text": payload}
    if kind == "done":
        text = str(p.get("text") or "")
        said = _spoken_part(text) if p.get("noted") else ""          # the agent chose what to say
        summary = _cap(said) if said else _first_sentences(text)
        limit = SPOKEN_MAX if said else MAX_LINE
        q = " ".join(_plain(str(p.get("question") or "")).split())
        if q and q.lower() not in summary.lower():
            summary = f"{summary} {q}".strip() if len(summary) + len(q) < limit else (summary if said else q)
        line = summary or "Done."
    elif kind == "approval":
        line = f"It needs your OK to {_approval_what(str(p.get('tool') or ''), p.get('input'))}. Yes or no?"
    elif kind == "error":
        why = _first_sentences(str(p.get("text") or ""), limit=120)
        line = f"That didn't work: {why}" if why else "That didn't work."
    else:
        return ""
    return line[:HARD_MAX]


_STOP = {"stop", "quiet", "shush", "hush", "enough", "silence", "shut"}
_FILLER = {"hey", "ok", "okay", "alright", "all", "right", "please", "thanks", "thank", "you", "talking",
           "speaking", "be", "up", "it", "that's", "thats", "that", "is", "now", "filect", "jarvis", "sal",
           "sorry", "just", "can", "could", "would", "the", "voice", "reading"}


def is_stop_phrase(text: str) -> bool:
    """"Stop", "be quiet", "hey, stop talking please, thank you" — said to make Filect stop talking, not a
    task for the agent. Needs a real stop word; "yes" / "no" / "okay" alone are answers, never this."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    return 0 < len(words) <= 8 and any(w in _STOP for w in words) and all(w in _STOP or w in _FILLER for w in words)


# ---- getting + playing the audio --------------------------------------------------------------------
def current_voice() -> str:
    """The voice picked in the Assistant tab (settings.live_voice), else Sal."""
    try:
        from app.core.settings import settings
        v = str(getattr(settings, "live_voice", "") or "")
    except Exception:
        v = ""
    return v if v in {x[0] for x in VOICES} else VOICE


def preview_audio(voice: str):
    """The preview line in `voice`, ready to play (turned up) — from the cache after the first time, so
    previews are instant and cost nothing again. None if it can't be fetched (offline / signed out)."""
    name = next((x[1] for x in VOICES if x[0] == voice), None)
    if name is None:
        return None
    try:
        from app.core.settings import settings
        path = settings.get_app_data_dir() / "voice_previews" / f"{voice}-{PREVIEW_VERSION}.audio"
        if path.exists() and path.stat().st_size > 1000:
            return path.read_bytes()
    except Exception:
        path = None
    data = fetch_speech(PREVIEW_LINE.format(name=name), voice)
    if data and path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception as e:
            logger.info("[speak] couldn't cache the %s preview: %s", voice, e)
    return data


_HTTP = None                     # one keep-alive connection to the voice service (no new TLS handshake per line)


def _http():
    global _HTTP
    if _HTTP is None:
        import requests
        _HTTP = requests.Session()
    return _HTTP


_woke = 0.0
WAKE_EVERY_SEC = 20              # wake(): at most this often


def warm_up() -> None:
    """Open the connection to the voice service ahead of time and wake it (off the UI thread): an empty
    OPTIONS call to `speak-stream` — no audio, no usage. The first spoken answer then skips the TLS
    handshake and the service's wake-up (measured after a quiet minute: voice starts 0.54 s vs 0.69 s).
    Harmless if it fails."""
    global _woke
    _woke = time.monotonic()

    def go():
        try:
            from app.core.supabase_client import SUPABASE_URL
            _http().options(f"{SUPABASE_URL}/functions/v1/speak-stream", timeout=5)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True, name="filect-speak-warm").start()


def wake() -> None:
    """You started talking (hands-free, Speak replies on): wake the voice service while you talk, so it's
    ready when the answer is. At most every WAKE_EVERY_SEC."""
    if time.monotonic() - _woke >= WAKE_EVERY_SEC:
        warm_up()


def fetch_speech(line: str, voice: str = None):
    """MP3 bytes for `line` from the `speak` edge function, or None (signed out / offline / over the
    daily limit / any error — the caller falls back to Windows' own voice)."""
    try:
        from app.core.transcription import _get_auth_token
        from app.core.supabase_client import SUPABASE_URL
        token = _get_auth_token()
        if not token:
            logger.info("[speak] not signed in — using Windows' voice")
            return None
        t0 = time.monotonic()
        r = _http().post(f"{SUPABASE_URL}/functions/v1/speak", timeout=15,
                          headers={"Authorization": f"Bearer {token}"},
                          json={"text": line, "voice": voice or current_voice(), "language": "auto"})
        if r.status_code == 200 and r.content and "audio" in (r.headers.get("content-type") or ""):
            logger.info("[speak] %d bytes in %.2fs", len(r.content), time.monotonic() - t0)
            return louder(r.content)
        logger.info("[speak] server said %s: %s — using Windows' voice", r.status_code, r.text[:120])
    except Exception as e:
        logger.info("[speak] request failed (%s) — using Windows' voice", e)
    return None


class _PCMStream:
    """A spoken line arriving from `speak-stream` as raw 16-bit mono PCM: a reader thread adds to it, the
    player takes from it. Turned up as it arrives (as louder() does for a whole clip — see STREAM_GAIN)."""
    def __init__(self, resp, rate: int):
        self.rate = rate
        self._resp = resp
        self._parts: deque = deque()
        self._carry = b""                # half a sample left from the last chunk
        self._peak = STREAM_PEAK_FLOOR
        self.done = False                # all of it is here (or it was cancelled / broke off)
        self.cancelled = False

    def add(self, data: bytes) -> None:
        import numpy as np
        data = self._carry + data
        cut = len(data) - len(data) % 2
        self._carry, data = data[cut:], data[:cut]
        if not data:
            return
        x = np.frombuffer(data, dtype="<i2").astype(np.int32)
        self._peak = max(self._peak, int(np.abs(x).max()))
        gain = PEAK * 32767 / self._peak                 # only ever goes down within a line → never clips
        if gain > 1.0:
            data = np.clip(np.round(x * gain), -32768, 32767).astype("<i2").tobytes()
        self._parts.append(data)

    def read_rest(self, it) -> None:
        try:
            for c in it:
                if self.cancelled:
                    break
                if c:
                    self.add(c)
        except Exception as e:
            if not self.cancelled:
                logger.info("[speak] the voice broke off (%s) — saying what came", e)
        finally:
            self.done = True
            try:
                self._resp.close()
            except Exception:
                pass

    def take(self, n: int) -> bytes:
        out = bytearray()
        while self._parts and len(out) < n:
            p = self._parts.popleft()
            room = n - len(out)
            if len(p) > room:
                self._parts.appendleft(p[room:])
                p = p[:room]
            out += p
        return bytes(out)

    def pending(self) -> bool:
        return bool(self._parts)

    def cancel(self) -> None:
        self.cancelled, self.done = True, True
        self._parts.clear()
        try:
            self._resp.close()
        except Exception:
            pass


def stream_speech(line: str, voice: str = None):
    """The live assistant's voice, STREAMED from `speak-stream`: returns a _PCMStream as soon as the first
    audio is here (the rest keeps coming in the background), or None (signed out / offline / refused /
    not deployed — the caller falls back). Measured through the real backend: playing can start 0.45 s
    after asking (vs 0.63 s for a whole short sentence via `speak`, 0.50 vs 1.23 s for a long one)."""
    try:
        from app.core.transcription import _get_auth_token
        from app.core.supabase_client import SUPABASE_URL
        token = _get_auth_token()
        if not token:
            return None
        t0 = time.monotonic()
        r = _http().post(f"{SUPABASE_URL}/functions/v1/speak-stream", timeout=(5, 15), stream=True,
                          headers={"Authorization": f"Bearer {token}"},
                          json={"text": line, "voice": voice or current_voice(), "language": "auto"})
        if r.status_code != 200 or "audio/pcm" not in (r.headers.get("content-type") or ""):
            logger.info("[speak] stream: server said %s: %s", r.status_code, r.text[:120])
            r.close()
            return None
        it = r.iter_content(None)
        s = _PCMStream(r, int(r.headers.get("x-sample-rate") or 24000))
        for c in it:                                     # (wait for the first audio)
            if c:
                s.add(c)
                break
        if not s.pending():
            r.close()
            return None
        logger.info("[speak] streaming: first audio in %.2fs", time.monotonic() - t0)
        threading.Thread(target=s.read_rest, args=(it,), daemon=True, name="filect-speak-stream").start()
        return s
    except Exception as e:
        logger.info("[speak] stream failed (%s)", e)
    return None


def fetch_live(line: str, voice: str = None):
    """What the live assistant's Speaker fetches: streamed audio, else the whole clip from `speak` (as
    before), else None (→ Windows' own voice)."""
    return stream_speech(line, voice) or fetch_speech(line, voice)


PEAK = 0.89                      # louder(): the loudest moment lands at -1 dBFS (no clipping)
MAX_GAIN = 4.0                   # …but never more than +12 dB (a near-silent clip stays quiet)
# Streamed audio can't be measured whole first, so it's turned up by at most PEAK / Sal's usual peak
# (-5.4 dBFS → ×1.66, what louder() gives a typical line), less as soon as a louder moment arrives.
# ponytail: running peak, not lookahead — a line whose loudest moment comes late starts a bit louder.
STREAM_PEAK_FLOOR = int(32767 * 10 ** (-5.4 / 20))


def louder(audio: bytes) -> bytes:
    """Sal comes ~4.5 dB quieter than the system's own voice (peaks -5.4 dBFS) and a Qt sink can't go past volume
    1.0 → turn the audio itself up: plain gain to just under full scale, so it's as loud as other apps and never
    distorts. Windows: MP3 → 16-bit WAV with the ffmpeg Filect already ships (Mac: afconvert). Anything goes wrong
    → the original audio, at the old volume."""
    try:
        import io
        import os
        import tempfile
        import wave
        import numpy as np
        from app.core.video_analysis import _run_ffmpeg
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "in"), os.path.join(d, "out.wav")
            with open(src, "wb") as f:
                f.write(audio)
            r = _run_ffmpeg(["-i", src, "-ac", "1", "-c:a", "pcm_s16le", "-y", dst], timeout=15)
            if r.returncode != 0 or not os.path.exists(dst):
                raise RuntimeError((r.stderr or b"").decode("utf-8", "ignore")[-200:])
            with wave.open(dst) as w:
                params, x = w.getparams(), np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        peak = int(np.abs(x.astype(np.int32)).max()) if x.size else 0
        gain = min(MAX_GAIN, PEAK * 32767 / peak) if peak else 1.0
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setparams(params)
            y = x if gain <= 1.05 else np.clip(np.round(x * gain), -32768, 32767).astype("<i2")
            w.writeframes(y.tobytes())
        return buf.getvalue()                             # (always WAV on Windows: the clip player plays WAV)
    except Exception as e:
        logger.info("[speak] couldn't decode / turn it up (%s) — normal volume", e)
        return audio


class _ClipStream:
    """A whole decoded clip (WAV bytes from louder()) shaped like a finished _PCMStream, so _PCMPlayer plays it."""
    def __init__(self, data: bytes):
        import io
        import wave
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2:
                raise RuntimeError("not 16-bit audio")
            self.rate, ch = w.getframerate(), w.getnchannels()
            frames = w.readframes(w.getnframes())
        if ch != 1:
            import numpy as np
            x = np.frombuffer(frames, dtype="<i2").reshape(-1, ch).mean(axis=1)
            frames = x.astype("<i2").tobytes()
        self._buf = bytearray(frames)
        self.done = True

    def take(self, n: int) -> bytes:
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def pending(self) -> bool:
        return bool(self._buf)

    def cancel(self) -> None:
        self._buf.clear()


class _QtClipPlayer:
    """Bytes that aren't WAV (louder() couldn't decode them) → Qt's media player from a temp file."""
    def __init__(self, data: bytes):
        import tempfile
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        f.write(data)
        f.close()
        self._path = f.name
        self._out = QAudioOutput()
        self._out.setVolume(1.0)
        self._p = QMediaPlayer()
        self._p.setAudioOutput(self._out)
        self._p.setSource(QUrl.fromLocalFile(self._path))
        self._started = False

    def play(self):
        self._p.play()
        self._started = True

    def playing(self) -> bool:
        st = self._p.playbackState()
        ms = self._p.mediaStatus()
        loading = ms in (type(ms).LoadingMedia, type(ms).LoadedMedia, type(ms).BufferingMedia,
                         type(ms).BufferedMedia) and self._started and st != type(st).StoppedState
        return st == type(st).PlayingState or (loading and ms != type(ms).EndOfMedia)

    def stop(self):
        try:
            self._p.stop()
        finally:
            import os
            try:
                os.unlink(self._path)
            except OSError:
                pass


class _PCMPlayer:
    """Plays a _PCMStream as it arrives (Qt's audio output, push mode): starts on the first audio,
    keeps writing as more comes, done when all of it has been heard."""
    def __init__(self, stream: _PCMStream):
        from PySide6.QtCore import QTimer as _T
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
        fmt = QAudioFormat()
        fmt.setSampleRate(stream.rate)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        dev = QMediaDevices.defaultAudioOutput()
        if dev.isNull() or not dev.isFormatSupported(fmt):
            raise RuntimeError("the speakers don't take this audio")
        self._s = stream
        self._sink = QAudioSink(dev, fmt)
        # (the sink's own enum: PySide 6.11 returns QtAudio.State, which never equals QAudio.State — the
        #  last ~0.25 s in the sink's buffer was cut off when this compared against QAudio)
        self._active = type(self._sink.state()).ActiveState
        self._io = None
        self._timer = _T()
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._feed)

    def play(self):
        self._io = self._sink.start()
        if self._io is None:
            raise RuntimeError("the speakers wouldn't start")
        self._feed()
        self._timer.start()

    def _feed(self):
        free = self._sink.bytesFree()
        if free > 0 and self._s.pending():
            self._io.write(self._s.take(free - free % 2))

    def playing(self) -> bool:
        return not self._s.done or self._s.pending() or self._sink.state() == self._active

    def stop(self):
        self._timer.stop()
        self._s.cancel()
        self._sink.stop()


def _looks_like_audio(b: bytes) -> bool:
    """MP3 (an ID3 tag or an MPEG frame header — what the voice service sends), Ogg, FLAC or MP4/M4A."""
    if len(b) < 12:
        return False
    if b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0):
        return True
    return b[:4] in (b"OggS", b"fLaC") or b[4:8] == b"ftyp"


def _player_for(data):
    if isinstance(data, _PCMStream):
        return _PCMPlayer(data)
    if isinstance(data, (bytes, bytearray)) and bytes(data[:4]) == b"RIFF":
        return _PCMPlayer(_ClipStream(bytes(data)))       # a decoded, turned-up clip (louder())
    data = bytes(data or b"")
    if not _looks_like_audio(data):
        # (Qt's media player never refuses — it would just stay silent and the line would be lost. Refusing here
        #  sends the Speaker to Windows' own voice instead, as the Mac's player does.)
        raise ValueError("not audio Windows can play")
    return _QtClipPlayer(data)


class _SystemVoice:
    """Windows' own voice (the fallback, SAPI) — async, stops at once; a Greek voice for Greek text when one is
    installed (Mac: NSSpeechSynthesizer)."""
    def __init__(self, line: str):
        import comtypes.client
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        self._v = comtypes.client.CreateObject("SAPI.SpVoice")
        if re.search(r"[Ͱ-Ͽ]", line):
            try:
                toks = self._v.GetVoices("Language=408")          # Greek (LCID 0x408)
                if toks.Count:
                    self._v.Voice = toks.Item(0)
            except Exception:
                pass
        self._line = line

    def play(self):
        self._v.Speak(self._line, 1)                      # SVSFlagsAsync

    def playing(self) -> bool:
        try:
            return int(self._v.Status.RunningState) == 2  # SRSEIsSpeaking
        except Exception:
            return False

    def stop(self):
        try:
            self._v.Speak("", 3)                          # SVSFlagsAsync | SVSFPurgeBeforeSpeak
        except Exception:
            pass


class Speaker(QObject):
    """Says lines one at a time. A long line is said in pieces (`_chunks`): the next piece of the SAME
    line is fetched while one plays, so there are no gaps; the next LINE is only fetched when its turn
    comes (Esc may drop it). `started` when a piece starts playing, `finished` when it's quiet again.
    `hold(True)` keeps the next piece waiting and fetches nothing new (you're talking); `stop()` silences
    it and drops the queue. fetch / player / fallback are injectable for tests."""
    started = Signal()
    finished = Signal()
    _ready = Signal(int, object, object)     # (generation, queue item, audio bytes | _PCMStream | None) — fetch thread

    GRACE_SEC = 0.3                          # a just-started player may not report "playing" yet

    def __init__(self, parent=None, fetch=None, make_player=None, make_fallback=None):
        super().__init__(parent)
        self._fetch = fetch or fetch_live
        self._make_player = make_player or _player_for
        self._make_fallback = make_fallback or _SystemVoice
        self._queue: deque = deque()         # [piece, audio bytes | b"" (no audio: Windows' own voice) | None, line no.]
        self._gen = 0                        # bumped by stop(): a fetch from before it is ignored
        self._line = 0                       # numbers the lines given to say()
        self._fetching = None                # the queue item being fetched (one at a time)
        self._cur = None                     # the player that's playing
        self._cur_line = 0
        self._t_play = 0.0
        self._held = False
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._check)
        self._ready.connect(self._on_ready)

    @property
    def speaking(self) -> bool:
        """Playing, or about to (fetching / between two pieces). Not while held."""
        return self._cur is not None or (bool(self._queue) and not self._held)

    def say(self, line: str, data: bytes = None) -> None:
        """Say `line` (fetched in pieces), or play `data` — audio that's already here (a voice preview)."""
        line = " ".join((line or "").split())[:HARD_MAX]
        if not line:
            return
        self._line += 1
        if data:
            self._queue.append([line, data, self._line])
        else:
            self._queue.extend([piece, None, self._line] for piece in _chunks(line))
        while len({q[2] for q in self._queue}) > MAX_QUEUE:          # too many waiting: the oldest line goes
            old = self._queue[0][2]
            while self._queue and self._queue[0][2] == old:
                self._queue.popleft()
        self._pump()

    def hold(self, on: bool) -> None:
        self._held = bool(on)
        if not on:
            self._pump()
            self._fetch_next()

    def stop(self) -> None:
        was = self.speaking
        self._gen += 1
        for q in self._queue:                             # (streams that came but won't be played: close them)
            if isinstance(q[1], _PCMStream):
                q[1].cancel()
        self._queue.clear()
        self._fetching = None
        cur, self._cur = self._cur, None
        if cur is not None:
            try:
                cur.stop()
            except Exception:
                pass
        self._poll.stop()
        if was:
            self.finished.emit()

    def _pump(self) -> None:
        """Play the next piece if nothing is playing and it's here; else make sure it's coming."""
        if self._cur is not None or self._held or not self._queue:
            return
        if self._queue[0][1] is None:
            self._fetch_next()
            return
        self._play(self._queue.popleft())

    def _fetch_next(self) -> None:
        if self._fetching is not None or self._held:
            return
        item = next((q for q in self._queue if q[1] is None), None)
        if item is None:
            return
        if self._cur is not None and (item[2] != self._cur_line or any(q[1] is not None for q in self._queue)):
            return                                        # the next line waits for its turn; one piece ahead is enough
        self._fetching, gen = item, self._gen

        def work():
            data = None
            try:
                data = self._fetch(item[0])
            except Exception as e:
                logger.info("[speak] fetch failed: %s", e)
            self._ready.emit(gen, item, data)
        threading.Thread(target=work, daemon=True, name="filect-speak-fetch").start()

    def _on_ready(self, gen: int, item, data) -> None:
        if gen != self._gen:                              # stopped while it was being fetched
            if isinstance(data, _PCMStream):
                data.cancel()
            return
        self._fetching = None
        if data:
            item[1] = data
        else:                                             # no audio: Windows' own voice — for the rest of this line too
            for q in (item, *self._queue):
                if q[2] == item[2] and q[1] is None:
                    q[1] = b""
        self._pump()
        self._fetch_next()

    def _play(self, item) -> None:
        piece, data = item[0], item[1]
        player = None
        if data:
            try:
                player = self._make_player(data)
                player.play()
            except Exception as e:
                logger.info("[speak] playback failed (%s) — using Windows' voice", e)
                player = None
        if player is None:
            try:
                player = self._make_fallback(piece)
                player.play()
            except Exception as e:
                logger.warning("[speak] couldn't say it: %s", e)
                if self._queue:
                    self._pump()
                else:
                    self.finished.emit()
                return
        self._cur, self._cur_line, self._t_play = player, item[2], time.monotonic()
        logger.info("[speak] saying: %r", piece)
        self.started.emit()
        self._poll.start()
        self._fetch_next()                                # the next piece of this line, while this plays

    def _check(self) -> None:
        cur = self._cur
        if cur is None:
            self._poll.stop()
            return
        try:
            if cur.playing() or time.monotonic() - self._t_play < self.GRACE_SEC:
                return
        except Exception:
            pass
        self._poll.stop()
        self._cur = None
        if self._queue and not self._held:
            self._pump()                                  # (the next piece, or wait for it — still speaking)
        else:
            self.finished.emit()
