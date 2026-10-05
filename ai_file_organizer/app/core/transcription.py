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
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
    except Exception as e:
        return {"ok": False, "error": "network", "message": f"Could not read audio: {e}"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "X-Audio-Filename": os.path.basename(audio_path),
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

    def __init__(self, language: Optional[str] = None, device=None, terms=None):
        super().__init__()
        self.language = language
        self.terms = terms
        self.device = device           # QAudioDevice or None (default input)
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
        self._start_capture()
        self._ws_thread = threading.Thread(target=self._run_stream, daemon=True)
        self._ws_thread.start()

    def stop_recording(self):
        self.is_recording = False

    # ----- capture (runs on the GUI thread) -----
    def _start_capture(self):
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
        # the opening words aren't lost to the mute call.
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
        if token and websockets is not None:
            try:
                t0 = asyncio.get_event_loop().time()
                ws = await asyncio.wait_for(
                    websockets.connect(self._build_url(),
                                       additional_headers={"Authorization": f"Bearer {token}"},
                                       max_size=None),
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
        parts = list(utterances)
        tail = cur["t"].strip()
        if tail:
            joined = " ".join(parts).strip()
            if joined and tail.startswith(joined):
                parts = [tail]
            elif not parts or tail != parts[-1]:
                parts.append(tail)
        final = " ".join(parts).strip()
        logger.info(f"[STREAM] FINAL: utterances={len(utterances)}, partials={n_part['v']}, "
                    f"done={done_flag['v']}, chars={len(final)}")
        if _DEV:
            logger.info(f"[STREAM] FINAL text: {final!r}")
        if final:
            return final, True
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
