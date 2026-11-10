"""
Filect Voice — client-side dictation service (Windows).

Ported from the macOS implementation (Filect-Windows-Port pack), with three
Windows-specific adaptations proven out by the C1 de-risk probe:

  1. Mic capture uses **QtMultimedia QAudioSource**, NOT sounddevice/PortAudio.
     The dev machine is Windows ARM64 where sounddevice has no working
     PortAudio binary; QAudioSource is native on ARM64 + x64 and needs no
     bundled DLL. (See memory: project-voice-arm64-qtmultimedia.)
  2. Audio is **normalized (peak-boosted) before streaming**. Windows mics
     here capture very quietly (~2-4% of full scale) — too quiet for xAI's
     voice-activity detection. The Mac streaming path sends raw PCM; Windows
     must boost first. We use an adaptive running-peak gain so loud mics
     aren't clipped and quiet mics are lifted into range.
  3. System-output mute while recording uses **pycaw**, not macOS osascript.

Everything else (the xAI WS protocol, speech_final assembly, batch fallback,
10-min cap, the batch/distill/clean edge-function helpers) is the Mac logic.

Public surface (drop-in for the UI layer):
  - StreamingTranscriber(language=None, device=None, terms=None): QObject with
    signals finished(str) / error(str) / recording_stopped() / level(float) /
    capped(); methods start() / stop_recording().
  - transcribe_audio(path, language, terms): batch transcription (fallback).
  - distill_search_query(text): spoken sentence -> tight search keywords.
  - clean_transcript(text, level, terms): optional transcript polishing.
"""
import os
import logging
import tempfile
import threading
from typing import Optional, Dict, Any, List

import requests
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# Dev diagnostics: when FILECT_DEV is set, the streaming path logs the full
# partial-by-partial progression + final text. Off in release so transcripts
# aren't logged.
_DEV = bool(os.environ.get("FILECT_DEV"))

SUPABASE_URL = "https://gsvccxhdgcshiwgjvgfi.supabase.co"
TRANSCRIBE_URL = f"{SUPABASE_URL}/functions/v1/transcribe"
STREAM_URL = (f"{SUPABASE_URL}/functions/v1/transcribe-stream"
              .replace("https://", "wss://"))  # WebSocket streaming proxy
DISTILL_URL = f"{SUPABASE_URL}/functions/v1/distill-query"
CLEAN_URL = f"{SUPABASE_URL}/functions/v1/clean-transcript"

SAMPLE_RATE = 16000          # 16 kHz mono — plenty for speech, small payloads
_FRAME_BYTES = 3200          # 1600 samples * 2 bytes = 100 ms per streamed frame

# Adaptive normalization for the (quiet) Windows mic. Gain = target/running_peak,
# clamped to [1.0, _MAX_STREAM_GAIN]. _PEAK_FLOOR stops pure silence/room-noise
# from being blown up to full scale.
_NORM_TARGET = 0.95
_MAX_STREAM_GAIN = 25.0
_PEAK_FLOOR = 350            # int16 peak below this is treated as silence (no boost)


def _get_auth_token() -> Optional[str]:
    """Current user's Supabase access token — the LIVE, auto-refreshed one."""
    try:
        from .supabase_client import supabase_auth
        if not supabase_auth.is_authenticated:
            return None
        return supabase_auth.get_access_token()
    except Exception as e:
        logger.error(f"Failed to get auth token: {e}")
        return None


# ---------------------------------------------------------------------------
# Batch transcription + LLM helpers (cross-platform — ported verbatim)
# ---------------------------------------------------------------------------

def transcribe_audio(audio_path: str, language: Optional[str] = None,
                     terms: Optional[list] = None) -> Dict[str, Any]:
    """Send an audio file to the transcribe proxy. Returns:
        {ok: True,  text: str, duration: float}
        {ok: False, error: <code>, message: <human message>}
    error codes: not_authenticated | no_subscription | rate_limited | provider | network
    `terms` = Custom Words (key-term biasing) — names/jargon to spell right.
    """
    token = _get_auth_token()
    if not token:
        return {"ok": False, "error": "not_authenticated",
                "message": "Please sign in to use voice dictation."}
    # The speech provider decodes a fixed set of containers; AUDIO_EXTENSIONS is broader (it also
    # lists .aiff/.aif/.caf/.wma). For an unsupported container, transcode to WAV first (via
    # soundfile) so the provider can decode it — otherwise it rejects the raw bytes (format="").
    _PROVIDER_AUDIO = {".mp3", ".wav", ".m4a", ".mp4", ".aac", ".flac",
                       ".ogg", ".oga", ".opus", ".webm", ".mpga", ".mpeg"}
    _send_name = os.path.basename(audio_path)
    _tmp = None
    if os.path.splitext(audio_path)[1].lower() not in _PROVIDER_AUDIO:
        import tempfile
        _fd, _wav = tempfile.mkstemp(suffix=".wav")
        os.close(_fd)
        try:
            import soundfile as sf
            _data, _sr = sf.read(audio_path)
            sf.write(_wav, _data, _sr, subtype="PCM_16")
            audio_path, _tmp = _wav, _wav
            _send_name = os.path.splitext(_send_name)[0] + ".wav"
        except Exception as e:
            # soundfile (libsndfile) can't read some containers — WMA above all. The ffmpeg that
            # ships for video analysis reads them: 16 kHz mono WAV is all transcription needs.
            try:
                from .video_analysis import _run_ffmpeg
                _run_ffmpeg(["-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000",
                             "-c:a", "pcm_s16le", "-y", _wav], timeout=180)
                if os.path.getsize(_wav) > 44:
                    audio_path, _tmp = _wav, _wav
                    _send_name = os.path.splitext(_send_name)[0] + ".wav"
                else:
                    raise RuntimeError("ffmpeg produced no audio")
            except Exception as e2:
                logger.warning(f"Audio transcode failed ({e}; ffmpeg: {e2}); sending the original bytes.")
        if _tmp is None:
            try:
                os.unlink(_wav)
            except Exception:
                pass

    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
    except Exception as e:
        if _tmp:
            try: os.unlink(_tmp)
            except Exception: pass
        return {"ok": False, "error": "network", "message": f"Could not read audio: {e}"}
    if _tmp:
        try: os.unlink(_tmp)
        except Exception: pass

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "X-Audio-Filename": _send_name,
    }
    if language:
        headers["X-Audio-Language"] = language
    if terms:
        joined = ",".join(str(t).strip() for t in terms if str(t).strip())
        if joined:
            headers["X-Audio-Terms"] = joined[:4000]

    try:
        r = requests.post(TRANSCRIBE_URL, data=audio_bytes, headers=headers, timeout=45)
    except Exception as e:
        logger.error(f"Transcription request failed: {e}")
        return {"ok": False, "error": "network", "message": "Network error — check your connection."}

    if r.status_code == 200:
        data = r.json()
        text = (data.get("text") or "").strip()
        logger.info(f"Transcription succeeded: {len(text)} chars")
        return {"ok": True, "text": text, "duration": float(data.get("duration") or 0)}
    if r.status_code == 401:
        return {"ok": False, "error": "not_authenticated",
                "message": "Your session expired — please sign in again."}
    if r.status_code == 403:
        return {"ok": False, "error": "no_subscription",
                "message": "An active subscription is required for voice dictation."}
    if r.status_code == 429:
        return {"ok": False, "error": "rate_limited",
                "message": "Daily voice limit reached. Try again tomorrow."}
    logger.error(f"Transcribe proxy error {r.status_code}: {r.text[:300]}")
    return {"ok": False, "error": "provider", "message": "Transcription failed — please try again."}


def distill_search_query(text: str) -> Optional[str]:
    """Turn a spoken search sentence into tight keywords via the distill-query
    edge function. Returns None on ANY failure so the caller falls back to the
    raw transcript."""
    text = (text or "").strip()
    if not text:
        return None
    token = _get_auth_token()
    if not token:
        return None
    try:
        r = requests.post(DISTILL_URL, json={"text": text},
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/json"}, timeout=10)
        if r.status_code == 200:
            q = (r.json().get("query") or "").strip()
            return q or None
        logger.warning(f"distill-query returned {r.status_code}")
    except Exception as e:
        logger.warning(f"distill_search_query failed: {e}")
    return None


def clean_transcript(text: str, level: str = "light", terms: Optional[list] = None) -> str:
    """Optional transcript polishing via the clean-transcript edge function.
    `level`: 'light' = strip filler + fix punctuation; 'polished' = + smooth
    phrasing. Returns the ORIGINAL on any failure (polishing must never break
    dictation)."""
    text = (text or "").strip()
    if not text or level not in ("light", "polished"):
        return text
    token = _get_auth_token()
    if not token:
        return text
    payload = {"text": text, "level": level}
    if terms:
        payload["terms"] = [str(t).strip() for t in terms if str(t).strip()][:200]
    try:
        r = requests.post(CLEAN_URL, json=payload,
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/json"}, timeout=12)
        if r.status_code == 200:
            cleaned = (r.json().get("text") or "").strip()
            return cleaned or text
        logger.warning(f"clean-transcript returned {r.status_code}")
    except Exception as e:
        logger.warning(f"clean_transcript failed: {e}")
    return text


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _normalize_peak(audio, target: float = 0.95, max_gain: float = 8.0):
    """Linear peak gain: raise a too-quiet recording so soft speech reaches the
    model at a strong, even level. Preserves the waveform (never changes WHAT was
    said), only ever raises the level, caps the gain so near-silence isn't blown
    up. Used by the BATCH fallback. Returns int16."""
    import numpy as np
    if audio is None or len(audio) == 0:
        return audio
    peak = int(np.max(np.abs(audio.astype(np.int32))))
    if peak <= 0:
        return audio
    raw_gain = (target * 32767.0) / peak
    if raw_gain <= 1.01:
        return audio
    gain = min(raw_gain, max_gain)
    return np.clip(audio.astype(np.float32) * gain, -32768.0, 32767.0).astype(np.int16)


def _rms_level(chunk_int16) -> float:
    """Normalized 0..1 loudness of an int16 ndarray chunk (drives the animation)."""
    import numpy as np
    if chunk_int16 is None or len(chunk_int16) == 0:
        return 0.0
    x = chunk_int16.astype("float32") / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    return min(1.0, rms * 4.0)


def _pick_text(ev: dict) -> str:
    """Pull the transcript string out of an xAI STT event regardless of field name."""
    for k in ("text", "transcript", "content"):
        v = ev.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _assemble(utterances, cur) -> str:
    """The transcript so far: finalized utterances + the trailing in-progress one (if the last
    utterance didn't get speech_final before we stopped waiting). Guards against a cumulative
    trailing interim that restates everything already committed, which would otherwise duplicate
    earlier utterances. (Mac, verbatim.)"""
    parts = list(utterances)
    tail = cur["t"].strip()
    if tail:
        joined = " ".join(parts).strip()
        if joined and tail.startswith(joined):
            parts = [tail]                       # cumulative → it already contains all of it
        elif not parts or tail != parts[-1]:     # skip an exact-duplicate restatement
            parts.append(tail)
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# System-output mute while recording (Windows: pycaw)
# ---------------------------------------------------------------------------

def _output_muted() -> Optional[bool]:
    """Current default-render-endpoint muted state (True/False), or None."""
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        dev = AudioUtilities.GetSpeakers()
        iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(iface, POINTER(IAudioEndpointVolume))
        return bool(vol.GetMute())
    except Exception as e:
        logger.debug(f"_output_muted failed: {e}")
        return None


def _set_output_muted(muted: bool):
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        dev = AudioUtilities.GetSpeakers()
        iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(iface, POINTER(IAudioEndpointVolume))
        vol.SetMute(1 if muted else 0, None)
    except Exception as e:
        logger.debug(f"_set_output_muted failed: {e}")


_LIVE_STREAMS = []   # StreamingTranscribers kept alive until their worker thread has ended (see _keep_stream)


def _keep_stream(t):
    """Hold a StreamingTranscriber until its worker thread has ENDED, and let go of finished ones here — on
    the GUI thread. Otherwise a caller that drops its reference first (Esc / cancel while it's still writing
    down your words) leaves the worker thread holding the LAST one: when that thread ends it frees the
    QObject (and its QAudioSource) on the wrong thread → access violation, the whole app gone without a
    trace. (2026-11-08: Esc right after letting go of Shift+Z closed Filect ~2 s later, as the text arrived.)"""
    _LIVE_STREAMS[:] = [s for s in _LIVE_STREAMS if s._ws_thread is not None and s._ws_thread.is_alive()]
    _LIVE_STREAMS.append(t)


_RUNNING = []   # capture QThreads kept alive until their thread has fully finished


def keep_until_finished(thread):
    """Hold a reference to a capture QThread until it has FULLY finished. A caller dropping its
    reference (a re-click mid-transcription, a closed dialog) otherwise leaves run()'s own frame as
    the last one, and when run() returns PySide deletes the QThread from inside itself →
    "QThread: Destroyed while thread is still running" → abort. Call from the UI thread before
    start(); finished ones are pruned here, so a QThread is never deleted while it runs."""
    _RUNNING[:] = [t for t in _RUNNING if not t.isFinished()]
    _RUNNING.append(thread)


# ---------------------------------------------------------------------------
# StreamingTranscriber — QtMultimedia capture + xAI WS streaming
# ---------------------------------------------------------------------------

class StreamingTranscriber(QObject):
    """Streams mic audio to the `transcribe-stream` edge function while you speak,
    so the final transcript is ready the instant you release the key.

    Windows architecture (differs from Mac's QThread+sounddevice):
      - Capture runs via QAudioSource on the thread that calls start() (the GUI
        thread, which has the Qt event loop QtMultimedia needs). readyRead
        appends boosted PCM to shared buffers and emits `level`.
      - The WS session runs on a worker thread (asyncio) reading those buffers.
      - Stopping the mic is marshaled back to the capture thread via a signal.

    Non-breaking: the mic is always buffered locally; on ANY streaming problem
    (ws lib missing, connect fails, mid-stream drop, no events, auth) it falls
    back to the batch path (`transcribe_audio`).
    """
    finished = Signal(str)
    error = Signal(str)
    recording_stopped = Signal()
    level = Signal(float)
    capped = Signal()
    _request_stop_capture = Signal()   # internal: worker -> capture thread

    _DONE_WAIT = 3.0
    _MAX_RECORD_SEC = 600              # 10-min runaway / stuck-hotkey cap

    def __init__(self, language: Optional[str] = None, device=None, terms=None, external: bool = False):
        super().__init__()
        self.language = language
        self.terms = terms
        self.device = device           # QAudioDevice or None (default input)
        self._external = external      # True: no mic of its own — Filect Assistant's listener feed()s the audio
        self._cancelled = False        # cancel(): nobody wants the text — never fall back to a batch upload
        self._heard_nothing = False    # streaming finished properly and heard NO words (not a failure)
        self._release_fg = None        # mic_helper.foreground_begin()'s release — hands-free waits meanwhile
        self.is_recording = False
        self._outbox: List[bytes] = [] # boosted int16 PCM frames for streaming
        self._chunks: List[bytes] = [] # raw int16 PCM for the batch fallback
        self._audio_source = None
        self._io = None
        self._mic_stopped = False
        self._stopped_emitted = False
        self._prior_muted = None
        self._run_peak = _PEAK_FLOOR   # adaptive-gain running peak
        self._ws_thread = None
        self._request_stop_capture.connect(self._stop_capture)

    # ----- public API (drop-in) -----
    def start(self):
        self.is_recording = True
        self._mic_stopped = False
        self._stopped_emitted = False
        self._outbox = []
        self._chunks = []
        self._run_peak = _PEAK_FLOOR
        if self._external:
            self._mic_stopped = True            # (no mic: the listener feeds the audio)
        else:
            self._start_capture()
        self._ws_thread = threading.Thread(target=self._run_stream, daemon=True)
        _keep_stream(self)              # (on the GUI thread) — never freed by its own worker thread
        self._ws_thread.start()

    def stop_recording(self):
        self.is_recording = False

    def cancel(self) -> None:
        """Hands-free dropped this one (Esc, a noise): stop, and if streaming came back empty DON'T upload the clip
        again for a batch transcription — that costs voice time for text nobody uses. (Dictation never calls it.)"""
        self._cancelled = True
        self.is_recording = False

    def feed(self, data: bytes) -> None:
        """External audio (external=True — Filect Assistant's listener owns the mic): 16 kHz int16 mono, any block
        size. Streamed up exactly like mic blocks (no gain: the listener's mic level is what it is), and kept for
        the batch fallback."""
        if not data:
            return
        data = data[: len(data) - len(data) % 2]
        self._chunks.append(data)
        self._outbox.append(data)

    def text_so_far(self) -> str:
        """Everything transcribed so far, the part still in progress included — read from the UI thread by the
        hands-free listener (Mac: the whole punctuated sentence is here ~0.5 s after you stop)."""
        live = getattr(self, "_live", None)
        return _assemble(*live).strip() if live else ""

    def words_so_far(self) -> int:
        """Words heard so far (finished sentences + the one in progress) — a screenshot taken
        mid-message records this so it can be placed at that point in the final transcript. (Mac.)"""
        live = getattr(self, "_live", None)
        return len(_assemble(*live).split()) if live else 0

    # ----- capture (runs on the GUI thread) -----
    def _start_capture(self):
        try:
            from app.core import mic_helper           # Filect Assistant (hands-free) lets go of the mic first
            self._release_fg = mic_helper.foreground_begin()
        except Exception:
            self._release_fg = None
        try:
            from PySide6.QtMultimedia import QMediaDevices, QAudioSource, QAudioFormat
            fmt = QAudioFormat()
            fmt.setSampleRate(SAMPLE_RATE)
            fmt.setChannelCount(1)
            fmt.setSampleFormat(QAudioFormat.Int16)
            dev = self.device if self.device is not None else QMediaDevices.defaultAudioInput()
            if dev is None:
                self.error.emit("No microphone found.")
                self._emit_stopped_once()
                return
            self._audio_source = QAudioSource(dev, fmt)
            self._io = self._audio_source.start()
            if self._io is None:
                self.error.emit("Could not access the microphone.")
                self._emit_stopped_once()
                return
            self._io.readyRead.connect(self._on_audio_ready)
            logger.info(f"[STREAM] mic capture started (device={dev.description()})")
        except Exception as e:
            logger.error(f"[STREAM] mic start failed: {e}")
            self.error.emit("Could not access the microphone.")
            self._emit_stopped_once()

    def _on_audio_ready(self):
        if self._mic_stopped or self._io is None:
            return
        try:
            data = bytes(self._io.readAll())
        except Exception:
            return
        if not data:
            return
        import numpy as np
        arr = np.frombuffer(data[: len(data) - (len(data) % 2)], dtype=np.int16)
        if len(arr) == 0:
            return
        # Raw audio for the batch fallback (its own _normalize_peak boosts it).
        self._chunks.append(arr.tobytes())
        # Adaptive peak-normalize for streaming (the quiet-Windows-mic fix).
        peak = int(np.max(np.abs(arr.astype(np.int32))))
        if peak > self._run_peak:
            self._run_peak = peak
        gain = min(_MAX_STREAM_GAIN, (_NORM_TARGET * 32767.0) / max(self._run_peak, _PEAK_FLOOR))
        if gain > 1.01:
            boosted = np.clip(arr.astype(np.float32) * gain, -32768.0, 32767.0).astype(np.int16)
        else:
            boosted = arr
        self._outbox.append(boosted.tobytes())
        try:
            self.level.emit(_rms_level(boosted))
        except Exception:
            pass

    def _stop_capture(self):
        rel, self._release_fg = self._release_fg, None
        if rel is not None:
            rel()                                      # (also when the mic never opened)
        if self._mic_stopped:
            return
        self._mic_stopped = True
        try:
            if self._audio_source is not None:
                self._audio_source.stop()
        except Exception:
            pass

    # ----- mute helpers -----
    def _mute_output_if_enabled(self):
        self._prior_muted = None
        try:
            from app.core.settings import settings as _s
            if not getattr(_s, "dictation_mute_while_recording", True):
                logger.info("[MUTE] disabled by setting — leaving audio as-is")
                return
            self._prior_muted = _output_muted()
            if self._prior_muted is not None:
                _set_output_muted(True)
                logger.info(f"[MUTE] system output muted while recording (was muted={self._prior_muted})")
            else:
                logger.warning("[MUTE] couldn't read output state (pycaw) — not muting")
        except Exception as e:
            logger.warning(f"[MUTE] mute failed: {e}")
            self._prior_muted = None

    def _restore_output(self):
        try:
            if getattr(self, "_prior_muted", None) is not None:
                _set_output_muted(self._prior_muted)
                logger.info(f"[MUTE] system output restored (muted={self._prior_muted})")
        except Exception as e:
            logger.warning(f"[MUTE] restore failed: {e}")
        self._prior_muted = None

    def _emit_stopped_once(self):
        if not self._stopped_emitted:
            self._stopped_emitted = True
            self.recording_stopped.emit()

    def _build_url(self) -> str:
        from urllib.parse import quote
        params = []
        if self.language:
            params.append(f"language={quote(str(self.language))}")
        if self.terms:
            joined = ",".join(str(t).strip() for t in self.terms if str(t).strip())
            if joined:
                params.append(f"terms={quote(joined[:4000])}")
        q = ("?" + "&".join(params)) if params else ""
        return f"{STREAM_URL}{q}"

    # ----- streaming (runs on the worker thread) -----
    def _run_stream(self):
        # Mute AFTER the mic is already capturing (mic started in start()), so
        # the opening words aren't lost to the mute call. External audio (hands-free): never mute — it runs for
        # hours while you listen to things.
        if not self._external:
            self._mute_output_if_enabled()
        stream_text, stream_ok = None, False
        try:
            import asyncio
            stream_text, stream_ok = asyncio.run(self._session())
        except Exception as e:
            logger.warning(f"[STREAM] session crashed ({e}); batch fallback")
        finally:
            self._request_stop_capture.emit()   # stop mic on the GUI thread
            self._restore_output()
            self._emit_stopped_once()

        if stream_ok and stream_text is not None:
            logger.info(f"[STREAM] final via streaming: {len(stream_text)} chars")
            self.finished.emit(stream_text)
            return
        if self._cancelled:                       # (hands-free only — dictation never cancels)
            logger.info("[STREAM] cancelled — no batch fallback")
            self.finished.emit("")
            return
        if self._external and self._heard_nothing:
            # Hands-free: the stream finished properly and heard no words (a cough, a long "hmm") — trust it;
            # re-uploading the clip for a batch pass only costs voice time. Dictation keeps its batch fallback.
            logger.info("[STREAM] no words heard (hands-free) — no batch re-upload")
            self.finished.emit("")
            return
        self._finish_via_batch()

    async def _session(self):
        """Returns (final_text, ok). ok=False → use the batch fallback."""
        import asyncio

        # Resolve streaming prerequisites; if missing, leave ws=None and let the
        # mic keep buffering for the batch fallback (never truncate mid-sentence).
        ws = None
        token = _get_auth_token()
        try:
            import websockets
        except ImportError as e:
            logger.warning(f"[STREAM] websockets missing ({e}); batch fallback")
            websockets = None
        # wss:// uses stdlib ssl, which in a frozen bundle has NO default CA bundle — without
        # an explicit one the TLS handshake fails (CERTIFICATE_VERIFY_FAILED) and streaming
        # silently falls back to the slow batch path. requests/httpx are unaffected (they carry
        # their own certifi), which is why batch works but streaming didn't — only when frozen.
        ssl_ctx = None
        try:
            import ssl as _ssl, certifi
            ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
        except Exception as e:
            logger.warning(f"[STREAM] certifi ssl context unavailable ({e}); using default")

        if token and websockets is not None:
            try:
                t0 = asyncio.get_event_loop().time()
                connect_kwargs = {
                    "additional_headers": {"Authorization": f"Bearer {token}"},
                    "max_size": None,
                }
                if ssl_ctx is not None:
                    connect_kwargs["ssl"] = ssl_ctx
                ws = await asyncio.wait_for(
                    websockets.connect(self._build_url(), **connect_kwargs),
                    timeout=8)
                logger.info(f"[STREAM] connected in {asyncio.get_event_loop().time()-t0:.2f}s "
                            f"({len(self._outbox)} frames buffered while connecting)")
            except Exception as e:
                logger.warning(f"[STREAM] connect failed ({e}); batch fallback")
                ws = None
        else:
            logger.info(f"[STREAM] prerequisites missing (token={bool(token)}, "
                        f"ws_lib={websockets is not None}); batch fallback")

        utterances: List[str] = []
        cur = {"t": ""}
        done_flag = {"v": False}
        n_part = {"v": 0}
        self._live = (utterances, cur)  # read by words_so_far() from the UI thread

        async def receiver():
            try:
                async for msg in ws:
                    try:
                        ev = __import__("json").loads(msg)
                    except Exception:
                        continue
                    typ = ev.get("type")
                    if typ == "transcript.partial":
                        txt = _pick_text(ev)
                        n_part["v"] += 1
                        if ev.get("speech_final"):
                            if txt:
                                utterances.append(txt)
                            cur["t"] = ""
                        else:
                            cur["t"] = txt
                        if _DEV:
                            logger.info(f"[STREAM] partial #{n_part['v']} "
                                        f"speech_final={ev.get('speech_final')}: {txt!r}")
                    elif typ == "transcript.done":
                        done_flag["v"] = True
                    elif typ == "error":
                        logger.warning(f"[STREAM] upstream error event: {ev}")
            except Exception as e:
                logger.info(f"[STREAM] receiver ended: {e}")

        streaming = ws is not None
        rx = asyncio.create_task(receiver()) if streaming else None
        cursor = 0

        # Re-chunk the capture buffer into ~100ms frames for pacing. _outbox holds
        # QAudioSource blocks (variable size); we concatenate + slice so each WS
        # send is a consistent 100ms PCM frame.
        def _pending_pcm():
            return b"".join(self._outbox)

        t_rec0 = asyncio.get_event_loop().time()
        sent_bytes = 0
        while self.is_recording:
            if asyncio.get_event_loop().time() - t_rec0 > self._MAX_RECORD_SEC:
                logger.warning(f"[STREAM] hit {self._MAX_RECORD_SEC}s cap; finalizing")
                try:
                    self.capped.emit()
                except Exception:
                    pass
                break
            if streaming:
                pcm = _pending_pcm()
                while cursor + _FRAME_BYTES <= len(pcm):
                    try:
                        await ws.send(pcm[cursor:cursor + _FRAME_BYTES])
                        cursor += _FRAME_BYTES
                        sent_bytes = cursor
                    except Exception as e:
                        logger.warning(f"[STREAM] send failed ({e}); batch fallback")
                        streaming = False
                        if rx:
                            rx.cancel()
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        break
            await asyncio.sleep(0.02)

        # Released: let the in-flight tail arrive, then stop the mic.
        await asyncio.sleep(0.12)
        self._request_stop_capture.emit()
        # Give the stop + final readyRead a moment to flush on the GUI thread.
        await asyncio.sleep(0.05)
        self._restore_output()
        self._emit_stopped_once()

        if not streaming:
            logger.info(f"[STREAM] dropped mid-record → batch fallback")
            return None, False

        # Flush remaining audio, then tell xAI we're done.
        pcm = _pending_pcm()
        while cursor < len(pcm):
            end = min(cursor + _FRAME_BYTES, len(pcm))
            try:
                await ws.send(pcm[cursor:end])
                cursor = end
            except Exception:
                break
        try:
            await ws.send(__import__("json").dumps({"type": "audio.done"}))
        except Exception as e:
            logger.warning(f"[STREAM] audio.done send failed ({e})")

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._DONE_WAIT
        while loop.time() < deadline and not done_flag["v"]:
            await asyncio.sleep(0.02)
        if rx:
            rx.cancel()
        try:
            await ws.close()
        except Exception:
            pass

        # Assemble: finalized utterances + any trailing in-progress one. Guard a
        # cumulative trailing interim that restates everything already committed.
        final = _assemble(utterances, cur)
        logger.info(f"[STREAM] FINAL: utterances={len(utterances)}, partials={n_part['v']}, "
                    f"done={done_flag['v']}, chars={len(final)}")
        if _DEV:
            logger.info(f"[STREAM] FINAL text: {final!r}")
        if final:
            return final, True
        if done_flag["v"]:
            self._heard_nothing = True                  # (a proper end with no words — see _run_stream)
        logger.info(f"[STREAM] empty streaming result → batch fallback")
        return None, False

    def _finish_via_batch(self):
        """Encode the buffered raw audio and transcribe in one request."""
        import numpy as np
        if not self._chunks:
            self.error.emit("No audio recorded.")
            return
        pcm = b"".join(self._chunks)
        audio = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16)
        audio = _normalize_peak(audio)
        logger.info(f"[STREAM] batch fallback on {len(audio)/SAMPLE_RATE:.1f}s of audio")
        tmp = None
        try:
            try:
                import soundfile as sf
                with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
                    tmp = f.name
                sf.write(tmp, audio, SAMPLE_RATE, format="FLAC", subtype="PCM_16")
            except Exception as e:
                logger.warning(f"FLAC encode unavailable ({e}); WAV instead")
                from scipy.io import wavfile
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp = f.name
                    wavfile.write(tmp, SAMPLE_RATE, audio)
            result = transcribe_audio(tmp, language=self.language, terms=self.terms)
            if result.get("ok"):
                self.finished.emit(result.get("text", ""))
            else:
                self.error.emit(result.get("message", "Transcription failed."))
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
