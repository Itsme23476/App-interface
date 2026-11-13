"""A tag the person asks for in the Analyze instructions is never cut. Live probe 2026-11-12 ("Add the tag
'lantern-test' to every file"): the AI wrote the tag in 6 of 6 answers, but as tag 31 of 31 (photo) and 28 of 28
(video); Filect keeps 25, so it was cut 2 times out of 6. Now the tags the instructions name go first, before the cap.
And a file the AI can't read still gets a tag asked for on EVERY file (live 2026-11-12: doorbell_chime.m4a, no words
in it, was the only one of 33 without the tag): the instructions are read once per run for such tags, and Filect adds
them to every file itself.
Real parsing code for photos, documents, video frames and transcripts; the AI's answer is the recorded one.
Windows port of the Mac's test (de286ca), 2026-11-13: the same cap and the same missing tag were on Windows."""
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image  # noqa: E402
from app.core.settings import settings  # noqa: E402
settings._save_config = lambda *a, **k: None              # (and conftest guards it too)
from app.core import vision as V, video_analysis as VA  # noqa: E402

TAG, INSTR = "lantern-test", "Add the tag 'lantern-test' to every file."
THIRTY = [f"tag{i}" for i in range(1, 31)]                       # 30 ordinary tags, then the requested one (= run 1)


def answer(tags):
    body = json.dumps({"type": "photo", "category": "photo", "caption": "a zebra", "description": "a zebra", "tags": tags})
    return {"choices": [{"message": {"content": body}, "finish_reason": "stop"}]}


def png():
    b = io.BytesIO()
    Image.new("RGB", (8, 8)).save(b, "PNG")
    return b.getvalue()


def every_reader(instr):
    """→ {reader: tags Filect keeps} for the AI answer with the requested tag written last."""
    with mock.patch.object(V, "_call_openai_proxy", return_value=answer(THIRTY + [TAG])):
        return {"photo": (V.gpt_vision_fallback("aGk=", filename="zebra.jpg", user_instructions=instr) or {}).get("tags"),
                "document": (V._gpt_text_analysis("Q3 budget notes", filename="notes.md", user_instructions=instr) or {}).get("tags"),
                "video": VA._analyze_frames([png()], "clip.mp4", user_instructions=instr).get("tags"),
                "voice memo": VA._tag_transcript("Call the flour supplier on Monday.", user_instructions=instr).get("tags")}


def test_the_requested_tag_survives_the_cap():
    for reader, tags in every_reader(INSTR).items():
        assert tags and tags[0] == TAG, f"{reader}: {tags}"
        assert len(tags) <= 25, f"{reader}: still capped ({len(tags)})"
    print("photo, document, video, voice memo: the requested tag is kept (first), the cap still holds  OK")


def test_without_instructions_nothing_changes():
    for reader, tags in every_reader(None).items():
        assert tags and tags[0] == "tag1" and TAG not in tags, f"{reader}: {tags}"
    print("no instructions: the AI's own order and the cap, as before  OK")


def test_the_prompt_asks_for_them_first():
    p = V.build_analysis_prompt("BASE", INSTR)
    assert "FIRST" in p and "25-45" not in p, p[-600:]
    print("the prompt asks for the requested tags first (and no longer for 25-45 tags)  OK")


def every_file(instr, ai_says, audio=None):
    """Analyze a no-speech audio file with these instructions; the instructions-reading call answers ai_says."""
    import tempfile
    from app.core import search as S
    V._EVERY_FILE_TAGS.clear()
    f = Path(tempfile.mkdtemp()) / "doorbell_chime.m4a"
    f.write_bytes(b"\0" * 64)
    reply = {"choices": [{"message": {"content": json.dumps({"tags": ai_says})}}]}
    with mock.patch.object(V, "_call_openai_proxy", return_value=reply) as call, \
            mock.patch("app.core.video_analysis.analyze_audio", return_value=audio), \
            mock.patch.object(S.settings, "ai_provider", "openai"):
        out = S.search_service._process_single_file({"source_path": str(f), "name": f.name}, f.parent,
                                                    force_ai=True, user_instructions=instr)
    return out.get("tags"), call.call_count


def test_a_file_the_ai_cant_read_still_gets_the_tag():
    tags, _ = every_file(INSTR, [TAG])
    assert tags == [TAG], tags
    tags, _ = every_file(INSTR, [TAG], audio={"label": "audio", "tags": ["doorbell", TAG, "chime"]})
    assert tags == [TAG, "doorbell", "chime"], tags
    print("a doorbell chime (no words, no AI read) still gets the requested tag; no duplicates  OK")


def test_a_conditional_instruction_adds_nothing_to_every_file():
    tags, _ = every_file("Tag photos of dogs with 'dog'.", [])
    assert not tags, tags
    print("'tag photos of dogs with dog': nothing is added to every file (left to the per-file read)  OK")


def test_the_instructions_are_read_once_per_run():
    import threading
    V._EVERY_FILE_TAGS.clear()
    reply = {"choices": [{"message": {"content": json.dumps({"tags": [TAG]})}}]}
    with mock.patch.object(V, "_call_openai_proxy", return_value=reply) as call:
        ts = [threading.Thread(target=V.tags_for_every_file, args=(INSTR,)) for _ in range(32)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert call.call_count == 1, call.call_count
    print("32 files analyzed at once: the instructions are read by the AI once  OK")


def test_an_unchanged_file_gets_the_tag_without_an_ai_read():
    import hashlib, tempfile
    from app.core import search as S
    V._EVERY_FILE_TAGS.clear()
    f = Path(tempfile.mkdtemp()) / "zebra.jpg"
    f.write_bytes(b"zebra")
    old = {"id": 7, "content_hash": hashlib.sha256(b"zebra").hexdigest(), "tags": '["zebra", "stripes"]'}
    reply = {"choices": [{"message": {"content": json.dumps({"tags": [TAG]})}}]}
    with mock.patch.object(V, "_call_openai_proxy", return_value=reply) as call, \
            mock.patch.object(S.search_service.index, "get_file_by_path", return_value=dict(old)), \
            mock.patch.object(S.search_service.index, "update_file_field", return_value=True) as upd:
        out = S.search_service._process_single_file({"source_path": str(f), "name": f.name}, f.parent,
                                                    user_instructions=INSTR)
        again = S.search_service._process_single_file({"source_path": str(f), "name": f.name}, f.parent)
    assert out.get("_skipped") and again.get("_skipped"), "still skipped: no new AI read, no usage counted"
    upd.assert_called_once_with(7, "tags", [TAG, "zebra", "stripes"])
    assert call.call_count == 1, "only the one read of the instructions"
    print("an already-analyzed, unchanged file gets the requested tag saved, with no new AI read  OK")


if __name__ == "__main__":
    import warnings
    warnings.simplefilter("ignore")
    tests = [test_the_requested_tag_survives_the_cap, test_without_instructions_nothing_changes, test_the_prompt_asks_for_them_first,
             test_a_file_the_ai_cant_read_still_gets_the_tag, test_a_conditional_instruction_adds_nothing_to_every_file,
             test_the_instructions_are_read_once_per_run, test_an_unchanged_file_gets_the_tag_without_an_ai_read]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:
            failed += 1
            print("FAIL", fn.__name__, repr(e)[:300])
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)
