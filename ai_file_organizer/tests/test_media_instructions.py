"""Analyze Files: the person's custom instructions reach videos and audio files too. Photos, documents and scanned
PDFs got them, but analyze_video / analyze_audio were called without them (the video prompt was built with None), so
"tag every file with X" skipped every video and recording. No network: the AI call, frames and transcription are
stubbed and the prompt that would be sent is checked. Ported from the Mac (pack §6.2, item 2).
"""
import io
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import video_analysis as va  # noqa: E402
from app.core import vision as V  # noqa: E402

ASK = "Add the tag 'lantern-test' to every file"
SENT = []


def fake_proxy(endpoint, messages, *a, **k):
    SENT.append(" ".join(m["content"] if isinstance(m["content"], str) else "" for m in messages))
    return {"choices": [{"message": {"content": '{"label": "x", "tags": ["a"], "caption": "c", "category": "memo"}'}}]}


def test_video_frames_prompt_has_the_instructions():
    SENT.clear()
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(buf, "PNG")
    with mock.patch.object(va, "extract_keyframes", lambda p, n=4: [buf.getvalue()]), \
            mock.patch.object(va, "extract_audio", lambda *a, **k: None), \
            mock.patch.object(V, "_call_openai_proxy", fake_proxy):
        va.analyze_video(r"C:\tmp\clip.mp4", user_instructions=ASK)
    assert SENT and "lantern-test" in SENT[-1], SENT


def test_audio_tagging_prompt_has_the_instructions():
    SENT.clear()
    import app.core.transcription as tr
    with mock.patch.object(va, "video_duration_seconds", lambda p: 5.0), \
            mock.patch.object(tr, "transcribe_audio", lambda p, *a, **k: {"ok": True,
                                                                         "text": "Remind me to call the supplier."}), \
            mock.patch.object(V, "_call_openai_proxy", fake_proxy):
        va.analyze_audio(r"C:\tmp\memo.m4a", user_instructions=ASK)
    assert SENT and "lantern-test" in SENT[-1], SENT


def test_the_indexer_passes_them_on():
    src = (Path(__file__).resolve().parents[1] / "app" / "core" / "search.py").read_text(encoding="utf-8")
    assert "analyze_video(file_path, user_instructions=user_instructions)" in src
    assert "analyze_audio(file_path, user_instructions=user_instructions)" in src
