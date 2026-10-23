"""Video + audio content analysis for Filect (Windows port).

A video has two signals Filect can already understand in isolation — pictures and speech —
so this module does NOT invent an analyzer. It extracts a few keyframes + the audio track,
then hands them to the EXISTING vision and transcription paths and fills the SAME metadata
fields an image/doc produces, so search and organize treat a video like any other file.

Windows: uses ffmpeg (via imageio-ffmpeg, which bundles the binary and survives PyInstaller)
for keyframe + audio extraction. Only extract_keyframes / extract_audio / video_duration_seconds
are platform-specific; analyze_video / _analyze_frames / analyze_audio / the AI calls are shared
with the Mac implementation.
"""
import logging
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm', '.wmv', '.flv', '.mpeg', '.mpg'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.oga', '.opus',
                    '.aiff', '.aif', '.wma', '.caf'}


# ---- ffmpeg plumbing (Windows replacement for AVFoundation) ----

def _ffmpeg_exe():
    """Path to the ffmpeg binary bundled by imageio-ffmpeg."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run_ffmpeg(args, timeout=180):
    """Run ffmpeg with no console window (Windows) and captured output."""
    exe = _ffmpeg_exe()
    kwargs = dict(capture_output=True, timeout=timeout)
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW — no flashing console
    return subprocess.run([exe, "-hide_banner", "-nostdin"] + list(args), **kwargs)


def video_duration_seconds(path) -> float:
    """Duration in seconds, parsed from ffmpeg's probe output. 0.0 if unknown."""
    try:
        r = _run_ffmpeg(["-i", str(path)], timeout=30)
        err = (r.stderr or b"").decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
        if m:
            return float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3))
    except Exception as e:
        logger.debug(f"duration probe failed for {path}: {e}")
    return 0.0


def extract_keyframes(path, n: int = 4) -> list:
    """Up to `n` PNG keyframes (bytes), evenly spaced across the video. [] if unreadable.

    ffmpeg: for each of n segments, fast-seek to its center and grab one frame
    (12.5 / 37.5 / 62.5 / 87.5 % of duration for n=4)."""
    import os
    import tempfile
    seconds = video_duration_seconds(path)
    if not seconds or seconds <= 0:
        return []
    frames = []
    for i in range(max(1, n)):
        t = seconds * ((i + 0.5) / n)   # center of each of n equal segments
        fd, out = tempfile.mkstemp(suffix=".png"); os.close(fd)
        try:
            # -ss before -i = fast input seek; one frame to PNG.
            _run_ffmpeg(["-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-y", out], timeout=60)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                with open(out, "rb") as fh:
                    frames.append(fh.read())
        except Exception as e:
            logger.warning(f"keyframe at {t:.1f}s failed for {path}: {e}")
        finally:
            try:
                os.unlink(out)
            except Exception:
                pass
    return frames


def extract_audio(path, max_seconds: int = 600):
    """Export the audio track to a temp .m4a (trimmed to max_seconds) for the existing
    transcription path. Returns the temp path, or None if there's no audio track / export
    fails. Caller deletes the temp file."""
    import os
    import tempfile
    fd, out = tempfile.mkstemp(suffix=".m4a"); os.close(fd)
    try:
        # -vn drops video; -acodec aac re-encodes the audio track to m4a. If the input has
        # no audio stream, ffmpeg writes no usable output and we return None.
        _run_ffmpeg(["-i", str(path), "-t", str(max_seconds), "-vn", "-acodec", "aac", "-y", out],
                    timeout=180)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception as e:
        logger.warning(f"audio extraction failed for {path}: {e}")
    try:
        os.unlink(out)
    except Exception:
        pass
    return None


# ---- shared with the Mac implementation (platform-independent) ----

def _analyze_frames(frames: list, filename: str, transcript: str = "") -> dict:
    """Send the keyframes to the EXISTING vision proxy in ONE call (downscaled, same 1024px
    cap images use) and parse the same JSON shape gpt_vision_fallback returns."""
    import io
    from PIL import Image
    from . import vision as V
    b64s = []
    for png in frames:
        try:
            img = Image.open(io.BytesIO(png)).convert("RGB")
            img.thumbnail((1024, 1024))
            b64s.append(V._pil_image_to_b64(img, "PNG"))
        except Exception:
            continue
    if not b64s:
        return {}
    system = V.build_analysis_prompt(V.DETAILED_SYSTEM_PROMPT, None)
    content = [
        {"type": "text", "text": f"filename: {filename}"},
        {"type": "text", "text": f"These are {len(b64s)} frames sampled in order across a "
                                 "VIDEO. Analyze the video as a whole. Set \"type\" to a short "
                                 "content category for the video (e.g. tutorial, demo, presentation, "
                                 "gameplay, vlog, ad, screen-recording, music video, interview) — "
                                 "NOT an image type like photo or meme."},
    ]
    if transcript:
        content.append({"type": "text", "text": "Transcript of the video's audio (context):\n"
                                                 + transcript[:2000]})
    content.append({"type": "text", "text": "Return STRICT JSON only using the schema."})
    for b in b64s:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b}", "detail": "auto"}})
    resp = V._call_openai_proxy("vision", [{"role": "system", "content": system},
                                           {"role": "user", "content": content}],
                                max_tokens=600, temperature=0.2)
    content_str = ""
    if resp:
        content_str = (resp.get("choices", [{}])[0].get("message", {}).get("content") or "")
    data = V._parse_json_relaxed(content_str) if content_str else None
    if not isinstance(data, dict):
        return {}
    tags = data.get("tags") or []
    tags = [str(t).lower()[:64] for t in tags][:25] if isinstance(tags, list) else []
    desc = str(data.get("description", "")).strip()
    caption = (desc or str(data.get("caption", "")).strip())[:1200]
    return {
        "label": str(data.get("type", "video")).strip().lower() or "video",
        "tags": tags,
        "caption": caption,
        "description": desc,
        "type": data.get("type"),
        "detected_text": data.get("detected_text"),
        "ai_source": "video:frames",
    }


def analyze_video(path, n_frames: int = 4, max_audio_seconds: int = 600,
                  transcribe: bool = True) -> "dict | None":
    """Analyze a video by its keyframes (vision) + audio (transcription), returning the SAME
    metadata shape an image/doc produces so the indexer/search/organize need no changes:
    label, tags, caption, type, ocr_text (= the transcript), has_ocr, duration, ai_source.
    None only if nothing at all could be extracted."""
    import os
    frames = extract_keyframes(path, n=n_frames)
    transcript = ""
    if transcribe:
        audio = extract_audio(path, max_seconds=max_audio_seconds)
        if audio:
            try:
                from .transcription import transcribe_audio
                r = transcribe_audio(audio)
                if r.get("ok"):
                    transcript = (r.get("text") or "").strip()
            finally:
                try:
                    os.unlink(audio)
                except Exception:
                    pass
    meta = _analyze_frames(frames, Path(path).name, transcript) if frames else {}
    if not meta and not transcript:
        return None
    if transcript:
        meta["ocr_text"] = transcript[:5000]
        meta["has_ocr"] = True
        if not meta.get("caption"):
            meta["caption"] = transcript[:300]
    meta.setdefault("label", "video")
    meta["duration"] = round(video_duration_seconds(path), 1)
    meta["ai_source"] = "video:frames+transcript" if transcript else meta.get("ai_source", "video:frames")
    return meta


def analyze_audio(path, max_seconds: int = 600) -> "dict | None":
    """Analyze a standalone AUDIO file: transcribe it, then tag it from what's said. Returns the
    same metadata shape as other files (ocr_text = transcript, label/tags from the transcript
    tagger, duration, ai_source). Long files are trimmed to `max_seconds`. None if nothing usable."""
    import os
    from .transcription import transcribe_audio
    src, tmp = str(path), None
    try:
        if video_duration_seconds(path) > max_seconds:
            tmp = extract_audio(path, max_seconds=max_seconds)  # trim long audio (podcasts etc.)
            if tmp:
                src = tmp
        r = transcribe_audio(src)
    except Exception as e:
        logger.warning(f"audio transcription failed for {path}: {e}")
        r = {}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    transcript = (r.get("text") or "").strip() if isinstance(r, dict) else ""
    if not transcript:
        return None
    meta = {"label": "audio", "ocr_text": transcript[:5000], "has_ocr": True,
            "duration": round(video_duration_seconds(path), 1)}
    tinfo = _tag_transcript(transcript)
    if tinfo:
        meta.update({k: v for k, v in tinfo.items() if v})
    if not meta.get("caption"):
        meta["caption"] = transcript[:300]
    meta["ai_source"] = "audio:transcript"
    return meta


def _tag_transcript(transcript: str) -> dict:
    """Tag an audio clip from its transcript with an AUDIO-appropriate prompt (not the
    document prompt analyze_text uses, which leaks tags like 'transcript'/'caption'/the
    filename). Returns {caption, label(=category), tags} or {}."""
    try:
        from . import vision as V
        system = (
            "You label an AUDIO clip for a file-search index, given a transcript of what is "
            "said. Identify the real TOPICS, people, products, tools and themes discussed. "
            "Return a one-sentence caption summarizing the clip, a short category (e.g. "
            "'podcast', 'meeting', 'voice memo', 'lecture', 'interview', 'music'), and up to 20 "
            "topic tags — only as many as the clip GENUINELY covers; do NOT pad a short clip to "
            "reach a number. Tag only actual content — NEVER the file format, the words "
            "'audio'/'transcript'/'caption', or any filename. Return JSON only: "
            "{\"caption\": \"...\", \"category\": \"...\", \"tags\": [\"t1\", \"t2\"]}."
        )
        resp = V._call_openai_proxy(
            "chat",
            [{"role": "system", "content": system},
             {"role": "user", "content": transcript[:6000]}],
            max_tokens=400, model="gpt-5-mini",
        )
        content = (resp or {}).get("choices", [{}])[0].get("message", {}).get("content", "") if resp else ""
        data = V._parse_json_relaxed(content) if content else None
        if not isinstance(data, dict):
            return {}
        tags = data.get("tags") or []
        tags = [str(t).lower()[:64] for t in tags][:20] if isinstance(tags, list) else []
        return {
            "caption": str(data.get("caption", "")).strip()[:1200],
            "label": (str(data.get("category", "audio")).strip().lower() or "audio"),
            "tags": tags,
        }
    except Exception as e:
        logger.debug(f"transcript tagging failed: {e}")
        return {}
