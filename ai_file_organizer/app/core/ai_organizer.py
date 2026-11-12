"""
AI-powered file organization planner.
LLM proposes → App validates → User approves → App executes

Core Principle: The LLM must never directly modify files.
- LLM plans
- App validates
- User approves
- App executes
"""

import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────────────────────

ORGANIZATION_SCHEMA = """{
  "folders": {
    "<folder-name>": [<file_id>, <file_id>, ...],
    ...
  }
}"""

SYSTEM_PROMPT = f"""You are a file organization assistant. Given a user's instruction and files with metadata, propose how to organize them into folders.

CRITICAL: FOLLOW USER INSTRUCTIONS LITERALLY
- The user's instruction is the PRIMARY directive - follow it EXACTLY
- If user says "move all files to X" or "put all files in X", put ALL files in folder "X" - no exceptions
- Do NOT organize by file type unless the user asks for it (or gives no instruction and the files are a random mix)
- When the user NAMES the folders to use, do NOT create additional folders beyond those
- When the user names NO folders ("sort these by what's in them", "organize this folder"), design the folders
  yourself from what the files are about (their label / tags / caption): one folder per topic, project, person or
  kind — never by file name or number, and no catch-all 'misc' / 'other' for files that have a clear topic

FRESH START ON EVERY REQUEST:
- Each instruction is a NEW organization request
- Files may currently be in subfolders - their current folder is shown in the "folder" field
- Use the current folder information ONLY when the user asks to preserve specific folders
- Otherwise, RECLASSIFY every file into the folders the user asked for — do NOT keep a file in its current subfolder just because that's where it is now
- When the user names the folders, create ONLY those. Do not add extra folders (e.g. if the user asks for "animals" and "paintings", return EXACTLY those two — never a third)

FILE INFORMATION PROVIDED:
- id: unique file identifier (use this in your response)
- name: the filename
- folder: the current subfolder the file lives in (or "." if at the root level)
- ext: the FILE EXTENSION (e.g., .mp4, .json, .png, .pdf)
- label/tags/caption: AI-generated descriptions

STRICT RULES:
1. Return ONLY valid JSON matching this schema:
{ORGANIZATION_SCHEMA}

2. folder-name: use EXACTLY what the user specifies, or lowercase kebab-case if organizing by type.
   The folder-name must be a SIMPLE name only (e.g. "Animals") — NEVER a path, NEVER contain "/" or "\\",
   and NEVER prefix it with the name of the folder being organized (the target folder is already the parent).
3. Use ONLY file_ids from the provided list - NEVER invent IDs
4. By default, EVERY file_id must appear in exactly ONE folder — UNLESS the user asks to preserve specific folders (see below)
5. Maximum 2 folder levels
6. Do NOT rename files - only organize into folders
7. NEVER return empty folders - every folder must have at least one file

PRESERVE INSTRUCTIONS (e.g., "keep X as is", "preserve folder X", "don't touch X", "leave X alone"):
- Files inside the named folder must be OMITTED from the plan entirely — do NOT include their file_ids
- Omitted files are left exactly where they are on disk — this is how preservation works
- You can identify which files belong to a folder using the "folder" field
- Example: "organize everything but preserve Work Projects" → include all files EXCEPT those with folder:"Work Projects"
- Only omit files for folders the user explicitly names — organize everything else normally

INSTRUCTION INTERPRETATION:

SIMPLE MOVE INSTRUCTIONS (e.g., "move all files to X", "put everything in X", "move files to folder called X"):
- Put ALL files in the single folder the user specified
- Do NOT create any other folders
- Do NOT organize by type - just move everything to that one folder
- Example: "move all files to hello" → {{"folders": {{"hello": [1, 2, 3, 4, 5, ...]}}}}

TYPE-BASED INSTRUCTIONS (e.g., "organize by type", "sort files by extension", "put videos in videos folder"):
- Only then organize files by their type/extension
- Use file extensions to determine type

MIXED INSTRUCTIONS (e.g., "put screenshots in screenshots, organize rest by type"):
- Follow the specific instruction for mentioned types
- Organize remaining files by type

JSON only. No markdown. No explanation. No prose."""


# ─────────────────────────────────────────────────────────────
# FILE SUMMARY FOR LLM
# ─────────────────────────────────────────────────────────────

def _infer_file_type_hints(file_name: str) -> List[str]:
    """
    Infer file type hints from filename patterns.
    This helps the AI identify files even without proper tags.
    """
    hints = []
    name_lower = file_name.lower()
    
    # Screenshot patterns
    if any(p in name_lower for p in ['screenshot', 'screen shot', 'screen_shot', 'snip', 'capture']):
        hints.append('screenshot')
    
    # Invoice/Receipt patterns
    if any(p in name_lower for p in ['invoice', 'receipt', 'bill', 'payment']):
        hints.append('invoice/receipt')
    
    # Document patterns
    if any(p in name_lower for p in ['document', 'doc', 'report', 'letter', 'contract']):
        hints.append('document')
    
    # Photo patterns
    if any(p in name_lower for p in ['img_', 'dsc_', 'photo', 'pic_', 'image']):
        hints.append('photo')
    
    # Video patterns  
    if any(p in name_lower for p in ['vid_', 'video', 'mov_', 'clip']):
        hints.append('video')
    
    # Download patterns
    if any(p in name_lower for p in ['download', 'downloaded']):
        hints.append('download')
    
    return hints


def build_file_summary(files: List[Dict[str, Any]], max_files: int = 300) -> str:
    """
    Create a compact summary of files for the LLM context.
    Limits tokens while preserving key metadata.
    Also includes file extension for accurate type matching.
    """
    lines = []
    for f in files[:max_files]:
        fid = f.get('id')
        full_name = f.get('file_name', 'unknown')
        # Extract the ext from the FULL filename BEFORE truncating for display.
        # Otherwise long names like "1000_F_…_n7pMsr8Jt3UlNfXbr7Z5WbgIQTxkzvAc.jpg"
        # get sliced at 50 chars (which can land mid-extension or strip it
        # entirely), leaving the AI with a junk `ext:.` or empty `ext:` field
        # and no signal that the file is actually a .jpg/.webp image.
        ext = ''
        if '.' in full_name:
            ext = full_name[full_name.rfind('.'):].lower()
        name = full_name[:50]
        label = f.get('label', '') or ''
        caption = (f.get('caption', '') or '')[:80]
        tags = f.get('tags', []) or []
        subfolder = f.get('subfolder', '.') or '.'

        # Add inferred hints from filename patterns. Use the FULL name (not
        # the 50-char display copy) so patterns living past position 50 —
        # e.g. "screen_shot" inside a very long path component — still
        # contribute a hint. Mirrors the ext fix above; same root cause.
        hints = _infer_file_type_hints(full_name)
        all_tags = list(tags[:8]) + hints
        tags_str = ', '.join(all_tags) if all_tags else ''

        line = f"id:{fid} | {name} | folder:{subfolder} | ext:{ext} | label:{label} | tags:[{tags_str}]"
        if caption:
            line += f" | caption:{caption}"
        lines.append(line)
    
    summary = "\n".join(lines)
    
    if len(files) > max_files:
        summary += f"\n... and {len(files) - max_files} more files"
    
    return summary


# ─────────────────────────────────────────────────────────────
# DETERMINISTIC TAG-BASED SAFETY NET
# ─────────────────────────────────────────────────────────────
# Even with the cleanest possible prompt, the LLM sometimes refuses to use
# a file's `tag:tiger` as the deciding signal when the FILENAME looks like
# something else (e.g. Adobe Stock URLs, base64-encoded image URLs). It
# invents alternate buckets — "documents", "images" — that the user never
# asked for.
#
# The safety net runs AFTER the LLM returns its plan and AUTHORITATIVELY
# rewrites any placement that violates a tag→user-named-folder rule the
# user explicitly stated in their instruction. The user is the source of
# truth; the LLM is a hint generator.
# ─────────────────────────────────────────────────────────────


def _stem_category(word: str) -> str:
    """Light stemmer for category words.

    Drops trailing run of ``s``/``z`` so plurals and a common class of
    misspellings collapse to the same root: ``tigers``/``tigerss``/``tigerz``
    all become ``tiger``; ``lions``/``lionsss`` become ``lion``. Words too
    short to safely strip (e.g. ``as``, ``is``) are returned untouched.
    """
    import re
    w = (word or '').lower().strip()
    if len(w) <= 3:
        return w
    stripped = re.sub(r'[sz]+$', '', w)
    return stripped if len(stripped) >= 3 else w


def _extract_category_to_folder_map(user_instruction: str) -> Dict[str, str]:
    """Best-effort: parse the user's instruction for ``category → folder`` pairs.

    Supported phrasings (case-insensitive), with optional ``all the`` /
    ``move`` / quotes around the folder name:

    * ``move all the tigers in a folder called hello``  →  ``{tiger: hello}``
    * ``put X in folder named Y``                       →  ``{X: Y}``
    * ``X(s) in the Y folder``                          →  ``{X: Y}``

    Returns an empty dict if the instruction has no extractable mapping —
    in which case the safety net is a no-op.
    """
    if not user_instruction:
        return {}
    import re

    text = user_instruction
    # Strip the boilerplate prefix the watcher adds so the regex anchors
    # against actual user words.
    for marker in ("User's specific instructions:", "User's instructions:"):
        if marker in text:
            text = text.split(marker, 1)[1]
            break
    # Only the user's own words: the watcher's rules follow them after a blank line, and their examples ("do NOT
    # route any video to a different folder", "belongs in the lions folder") must never become rules.
    text = text.strip().split("\n\n", 1)[0].strip()

    mapping: Dict[str, str] = {}
    stopwords = {
        'a', 'an', 'the', 'all', 'every', 'any', 'and', 'or', 'but',
        'move', 'put', 'send', 'place', 'organize', 'sort', 'files',
        'file', 'them', 'it', 'this', 'that', 'these', 'those',
    }

    # Pattern A: "<category>(s|z) in/to [a|the] folder [called|named] <folder>"
    # The folder name is captured until the next sentence boundary or
    # conjunction so multi-word names like 'everything else' stay whole.
    pattern_a = re.compile(
        r'(?P<cat>\b[A-Za-z][A-Za-z\-]{1,29})\b'
        r'\s+(?:in|to|into|go(?:es)?\s+to)\s+'
        r'(?:a\s+|the\s+)?folder\s+'
        r'(?:called|named)\s+'
        r'[\'"]?(?P<folder>[A-Za-z0-9][\w \-]{0,40}?)[\'"]?'
        r'(?=\s+(?:and|or|but|except|where|to|in|on|into|onto)\b|[\'",.;!?\n]|$)',
        flags=re.IGNORECASE,
    )

    # Pattern B: "<category>(s|z) in/to [a|the] <folder> folder"
    # ('lions in lions folder, pigs in pigs folder')
    pattern_b = re.compile(
        r'(?P<cat>\b[A-Za-z][A-Za-z\-]{1,29})\b'
        r'\s+(?:in|to|into)\s+'
        r'(?:a\s+|the\s+)?'
        r'(?P<folder>[A-Za-z0-9][\w\-]{0,40})\s+folder\b',
        flags=re.IGNORECASE,
    )

    for pat in (pattern_a, pattern_b):
        for m in pat.finditer(text):
            cat = m.group('cat').strip().lower()
            folder = m.group('folder').strip().strip("'\"").strip()
            if not cat or not folder:
                continue
            if cat in stopwords or folder.lower() in stopwords:
                continue
            root = _stem_category(cat)
            if not root or len(root) < 2:
                continue
            # First win — don't overwrite an existing mapping. The user's
            # instruction is read left-to-right; the earliest mention is
            # the most explicit assignment.
            mapping.setdefault(root, folder)
    return mapping


def _apply_tag_safety_net(
    plan: Dict[str, Any],
    files: List[Dict[str, Any]],
    user_instruction: str,
    existing_folders_only: bool = False,
) -> Dict[str, Any]:
    logger.info(
        f"[SafetyNet] Entry: {len(files)} files, plan folders={list((plan or {}).get('folders', {}).keys())}, "
        f"existing_folders_only={existing_folders_only}, instruction[:60]={user_instruction[:60]!r}"
    )
    """Rewrite any AI placement that violates a tag→user-named-folder mapping.

    For each file in the plan, if any tag matches a category root the user
    named, the file is moved to the user's named folder — overriding the
    LLM's choice. Files with no tag match are left exactly where the LLM
    placed them.

    In ``existing_folders_only`` (Organize As-Is) mode, the rewrite is
    skipped if the target folder isn't in the existing-folders list parsed
    from the instruction — so the safety net never invents new folders that
    the As-Is contract forbids.
    """
    if not isinstance(plan, dict):
        return plan

    cat_map = _extract_category_to_folder_map(user_instruction)
    logger.info(f"[SafetyNet] Extracted category map: {cat_map}")
    if not cat_map:
        logger.info("[SafetyNet] Empty category map -> no-op return")
        return plan

    folders = plan.get('folders')
    if not isinstance(folders, dict) or not folders:
        return plan

    # Build id → tags lookup
    file_tags: Dict[int, List[str]] = {}
    for f in files:
        try:
            fid = int(f.get('id'))
        except (TypeError, ValueError):
            continue
        raw = f.get('tags') or []
        file_tags[fid] = [str(t).lower().strip() for t in raw if t]
    # Diagnostic — surface every (id, tags) pair so we can see what shape the
    # caller actually passed in. This is what determines whether a tag rule
    # can match.
    logger.info(f"[SafetyNet] file_tags built: {file_tags}")

    # If As-Is mode, parse the existing folders list out of the instruction
    existing_lower: Optional[set] = None
    if existing_folders_only:
        import re
        m = re.search(r'EXISTING FOLDERS YOU CAN USE:\s*(.+)', user_instruction or '')
        if m:
            raw = m.group(1).split('\n', 1)[0]
            existing_lower = {
                p.strip().strip("'\"").lower()
                for p in raw.split(',')
                if p.strip()
            }
        else:
            existing_lower = set()

    # Walk the plan and rebuild it with rewrites applied
    rewritten: Dict[str, List[int]] = {}
    rewrite_count = 0
    for ai_folder, file_ids in folders.items():
        if not isinstance(file_ids, (list, tuple)):
            continue
        for fid in file_ids:
            try:
                fid_int = int(fid)
            except (TypeError, ValueError):
                continue

            chosen = ai_folder
            tags = file_tags.get(fid_int, [])
            for root, dest in cat_map.items():
                # Check every tag (and its stem) for a match against this root
                if any(t == root or _stem_category(t) == root for t in tags):
                    if existing_lower is not None and dest.lower() not in existing_lower:
                        # In As-Is mode, never invent a folder the AI/user
                        # rule names but that doesn't exist on disk.
                        continue
                    if chosen != dest:
                        rewrite_count += 1
                        logger.info(
                            f"[SafetyNet] Rerouting file_id={fid_int} from "
                            f"'{chosen}' to '{dest}' (tag '{root}' matches user rule)"
                        )
                    chosen = dest
                    break

            rewritten.setdefault(chosen, []).append(fid_int)

    if rewrite_count:
        logger.info(
            f"[SafetyNet] Applied {rewrite_count} tag-based rerouting(s); "
            f"category map: {cat_map}"
        )

    return {'folders': rewritten}


# ─────────────────────────────────────────────────────────────
# LLM REQUEST
# ─────────────────────────────────────────────────────────────

PLAN_CHUNK_THRESHOLD = 250  # above this, split the request into chunks

# The organize PLAN names the model it runs on: gpt-6-luna, like every Filect AI call since 2026-11-11 (gpt-5-mini,
# which it used before, is shut off Dec 11, 2026; the proxy runs every call on gpt-6-luna whatever is asked).
from .vision import FILECT_MODEL as PLAN_MODEL  # noqa: E402
_PLAN_CHUNK_RETRIES = 3  # a single gpt-5 call occasionally returns a bad/empty plan


def _normalize_plan_shape(plan: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Coerce an LLM reply into the canonical ``{"folders": {name: [ids]}}`` shape.

    - A correct plan (non-empty ``folders`` dict) is returned UNCHANGED.
    - A wrapper-less reply (``{name: [ids], ...}``) is wrapped as ``{"folders": …}``.
    - Empty / garbled replies return ``None`` so the retry loop kicks in.

    Idempotent: normalizing an already-correct plan is a no-op.
    """
    if not isinstance(plan, dict):
        return None
    if "folders" in plan:
        folders = plan.get("folders")
        if isinstance(folders, dict) and folders:
            return plan
        return None  # empty folders -> treat as failure so we retry
    # Wrapper-less: keep only keys whose value is a list of ids.
    folders = {k: v for k, v in plan.items() if isinstance(v, (list, tuple))}
    if not folders:
        return None
    return {"folders": folders}


def _plan_with_retries(
    user_instruction: str,
    files: List[Dict[str, Any]],
    idx: int = 0,
    n_chunks: int = 1,
) -> Optional[Dict[str, Any]]:
    """Call ``_request_plan_single`` up to ``_PLAN_CHUNK_RETRIES`` times, returning
    the first result that actually carries a non-empty ``folders`` dict. Returns
    ``None`` only after every attempt failed. Used for BOTH the single-folder path
    and each chunk of a large folder."""
    for attempt in range(1, _PLAN_CHUNK_RETRIES + 1):
        part = _request_plan_single(user_instruction, files)
        if part and part.get("folders"):
            if attempt > 1:
                logger.info(f"Plan chunk {idx + 1}/{n_chunks} succeeded on attempt {attempt}")
            return part
        logger.warning(f"Plan chunk {idx + 1}/{n_chunks} attempt {attempt}/{_PLAN_CHUNK_RETRIES} produced no usable plan")
    return None


def _fallback_plan_by_category(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Deterministic, network-free grouping used as a last resort by the UI when
    the AI plan fails for an ENTITLED user. Groups each file by its ``category``
    (or file extension if no category). Returns ``{"folders": {…}}`` or ``None``
    when there are no files. NEVER called by the background auto-watcher (it moves
    files without a human preview, so on failure it skips + retries next scan)."""
    if not files:
        return None
    folders: Dict[str, list] = {}
    for f in files:
        fid = f.get("id")
        if fid is None:
            continue
        cat = (f.get("category") or "").strip()
        if not cat:
            name = f.get("file_name") or f.get("name") or ""
            ext = os.path.splitext(name)[1].lstrip(".").upper()
            cat = f"{ext} Files" if ext else "Other"
        folders.setdefault(cat, []).append(fid)
    return {"folders": folders} if folders else None


# ─────────────────────────────────────────────────────────────
# BIG FOLDERS: design the folders first, then sort the files into them
# ─────────────────────────────────────────────────────────────
# Measured (2026-11-10, 200 and 120 files, 3 runs each): asked for the whole plan in ONE answer, with no thinking
# (the proxy's 'minimal'), the planner took shortcuts on a big folder — one 150-file "Notes" folder, a 102-file
# "misc", folders by file NUMBER ("Numbered 000-099 (batch)"), or 69 files simply left out: 21-65% of files well
# filed. Designing the folders first (it sees every file, no ids to track), then sorting the files into THOSE
# folders in small groups in parallel (a simple choice per file) filed 79-100%. Small folders keep the one call.
BIG_PLAN_FILES = 50          # above this many files, plan in two steps
_ASSIGN_CHUNK = 40           # files per sorting request (sent in parallel)
_DESIGN_LIST_MAX = 600       # the design step sees at most this many files (a spread-out sample beyond)
DESIGN_EFFORT = "low"        # thinking for the design step (the proxy allow-lists none / low / medium)
ASSIGN_EFFORT = "low"        # thinking for each sorting request

_DESIGN_SYSTEM = """You design the folder structure for a file organizer. You see a folder's files (name, current
subfolder, kind, what's in each) and the user's request. You do NOT list files: you only name the folders and say
what goes in each. Reply with JSON only, exactly:
{"folders": [{"name": "<folder name>", "holds": "<what goes in it, one short line>"}]}"""

_ASSIGN_SYSTEM = """You sort files into a FIXED list of folders for a file organizer. Reply with JSON only, exactly:
{"assign": {"<file id>": "<folder name, copied exactly from the list>"}, "leave": [<file ids to leave where they are>]}
Every file id you are given appears exactly once: in "assign" or (only when the rules allow it) in "leave"."""


def _plan_mode(user_instruction: str) -> str:
    """Which of the planner's modes an instruction is. (auto_watcher's "only the folders already there" instruction
    starts "[AUTO-ORGANIZE - EXISTING FOLDERS ONLY]" and carries its own rules: only those folders, a catch-all or the
    closest one for a file that fits none, never a file left out.)"""
    ui = user_instruction or ""
    if ui.startswith("[AUTO-ORGANIZE"):
        if "EXISTING FOLDERS ONLY" in ui:
            return "auto_existing"
        return "auto_instruction" if "User's specific instructions:" in ui else "auto"
    return "manual" if ui.strip() else "manual_none"


def _file_line(f: Dict[str, Any], with_id: bool) -> str:
    name = (f.get("file_name") or "unknown")
    ext = name[name.rfind("."):].lower() if "." in name else ""
    tags = [t for t in (f.get("tags") or [])[:6] if t] + _infer_file_type_hints(name)
    out = (f"{'id:' + str(f.get('id')) + ' | ' if with_id else ''}{name[:60]} | folder:{f.get('subfolder') or '.'} | "
           f"ext:{ext} | kind:{(f.get('label') or '')[:30]} | tags:[{', '.join(tags)}]")
    cap = (f.get("caption") or "").strip()
    return out + (f" | about:{cap[:110]}" if cap else "")


def _design_rules(mode: str) -> str:
    common = """- Group files that are about the same thing: by topic, project, person, client, event or kind — whatever fits
  THESE files best. Read each file's kind / tags / about, not just its name.
- NEVER group by file name, number or position ("batch 1", "notes 000-099", "late", "remaining").
- No catch-all folder ("misc", "other", "general", "unsorted") unless the user asked for one, or a few files truly
  fit nowhere else. A big group of similar files with different topics gets one folder per topic, but only for topics with
  several files: never a folder for a single file when a broader folder fits it (12 class assignments in 12
  subjects: one "School" folder, not 12 — measured 2026-11-11 on gpt-6-luna, 139 files x3: 18-44 folders with
  3-27 one-file folders before this line, 16-21 with 2-7 after).
- Specific, short, plain folder names. One level only: never put "/" or "\\" in a name.
- Never a folder named like the folder being organized, and never an empty one."""
    if mode == "auto_existing":
        return ""     # (not used: the folders are the existing ones)
    if mode == "auto_instruction":
        return f"""- FOLLOW the user's specific instructions EXACTLY: every file they are about goes in the folder they name,
  using the EXACT folder names the user typed. Only those files go there ("put the bills in Money": only bills).
- A file the instruction doesn't cover gets the closest folder the user named only if that folder clearly fits it;
  otherwise a new folder with a short clear name for its type or topic — never 'misc', 'other' or 'unsorted'.
{common}"""
    if mode == "auto":
        return f"""- Choose the BEST organization for these files: by project, client/person, topic, event, or — for a random
  mix of types — by file type. Never everything in one folder.
{common}"""
    if mode == "manual":
        return f"""- FOLLOW the user's instruction. Use the EXACT folder names the user typed (same spelling and capitals).
- If the user says to move everything to one folder, that one folder is the whole plan.
- If the user names the folders to use, create those; only the files the instruction is about go in a folder it
  names ("put the bills in Money": only bills, never a recipe or a photo).
- Files the instruction doesn't cover go in OTHER folders by type or topic, with real names — not 'misc' or
  'other' unless the user asked for one.
- If the user says to keep / preserve / not touch a folder, do NOT design a folder for its files (they stay put).
- If the user wants everything else in a named folder ("the rest in Stuff"), include that folder.
{common}"""
    return f"""- Choose the BEST organization for these files: by project, client/person, topic, event, or — for a random mix of
  types — by file type. Never everything in one folder.
{common}"""


def _assign_rules(mode: str) -> str:
    if mode == "auto_existing":
        return """- These are the folder's EXISTING folders: use only them, choosing by the instruction's own rules above (match
  the file's tags / kind / about to a folder's last name part; prefer the most specific nested folder; a file that
  matches none goes to a catch-all folder if there is one, else the most similar folder).
- Do not use "leave": every file goes in one of the folders. Copy the folder name exactly as listed."""
    leave = ("- \"leave\" is ONLY for files the user's instruction says to keep where they are (keep / preserve / don't "
             "touch that folder). Every other file goes in a folder.") if mode == "manual" else \
            "- Do not use \"leave\": every file goes in one of the folders."
    return f"""- Put each file in the ONE folder whose description fits what the file is about (its kind / tags / about first,
  then its name). Copy the folder name exactly as listed.
- Follow the user's instruction: a folder the user named for something gets only that thing ("Money" for bills
  gets only bills).
{leave}"""


def _ask_planner(system: str, user: str, max_tokens: int, effort: Optional[str]) -> Optional[Dict[str, Any]]:
    from .vision import _call_openai_proxy
    resp = _call_openai_proxy("chat", [{"role": "system", "content": system}, {"role": "user", "content": user}],
                              max_tokens=max_tokens, temperature=0.1, model=PLAN_MODEL, reasoning_effort=effort)
    try:
        content = (resp or {}).get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except Exception:
        content = ""
    return _parse_json(content) if content else None


def _design_folders(user_instruction: str, files: List[Dict[str, Any]], mode: str) -> List[Dict[str, str]]:
    shown = files
    if len(files) > _DESIGN_LIST_MAX:      # a spread-out sample (sorted by kind so every kind is in it)
        ordered = sorted(files, key=lambda f: ((f.get("label") or ""), f.get("file_name") or ""))
        step = len(ordered) / _DESIGN_LIST_MAX
        shown = [ordered[int(i * step)] for i in range(_DESIGN_LIST_MAX)]
    listing = "\n".join("- " + _file_line(f, with_id=False) for f in shown)
    note = "" if shown is files else f" (a spread-out sample of {len(shown)} of them)"
    user = (f'User instruction: "{user_instruction or "(none: organize this folder)"}"\n\n'
            f"The folder holds {len(files)} files{note}:\n{listing}\n\n"
            f"RULES:\n{_design_rules(mode)}\n\nDesign the folders. Return JSON only.")
    for attempt in range(2):
        out = _ask_planner(_DESIGN_SYSTEM, user, 3000, DESIGN_EFFORT)
        seen, folders = set(), []
        for d in (out or {}).get("folders") or []:
            if isinstance(d, str):
                d = {"name": d}
            if not isinstance(d, dict):
                continue
            name = re.sub(r"[/\\]+", " - ", str(d.get("name") or "")).strip().strip(".")
            if name and name.lower() not in seen:
                seen.add(name.lower())
                folders.append({"name": name, "holds": str(d.get("holds") or "")[:200]})
        if folders:
            return folders
        logger.warning(f"[plan] folder design attempt {attempt + 1} gave no folders")
    return []


def _assign_chunk(user_instruction: str, menu: str, names: Dict[str, str], chunk: List[Dict[str, Any]],
                  mode: str) -> Tuple[Dict[int, str], set]:
    """One sorting request: {file id: folder name} and the ids to leave. Ids not answered are simply missing."""
    rows = "\n".join(_file_line(f, with_id=True) for f in chunk)
    user = (f'User instruction: "{user_instruction or "(none: organize this folder)"}"\n\nFOLDERS:\n{menu}\n\n'
            f"FILES ({len(chunk)}):\n{rows}\n\nRULES:\n{_assign_rules(mode)}\n\nSort every file. Return JSON only.")
    out = _ask_planner(_ASSIGN_SYSTEM, user, 2500, ASSIGN_EFFORT) or {}
    ids = {int(f["id"]) for f in chunk}
    got: Dict[int, str] = {}
    raw = out.get("assign") if isinstance(out.get("assign"), dict) else {}
    for k, v in raw.items():
        try:
            fid = int(str(k).replace("id:", "").strip())
        except (TypeError, ValueError):
            continue
        name = names.get(str(v or "").strip().lower())
        if fid in ids and name and fid not in got:
            got[fid] = name
    leave = set()
    if mode == "manual":                 # (only a person's instruction can ask to keep a folder as it is)
        for v in out.get("leave") or []:
            try:
                fid = int(v)
            except (TypeError, ValueError):
                continue
            if fid in ids and fid not in got:
                leave.add(fid)
    return got, leave


def _plan_two_step(user_instruction: str, files: List[Dict[str, Any]],
                   folders: Optional[List[Dict[str, str]]] = None,
                   info: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Design the folders (or use the ones given), then sort the files into them in parallel groups; files a group
    didn't answer are asked about once more. Returns {"folders": {name: [ids]}} (files left out stay put; the
    callers' safety nets — reconcile_plan / ensure_all_files_included — finish the job) or None."""
    from concurrent.futures import ThreadPoolExecutor
    mode = _plan_mode(user_instruction)
    files = [f for f in files if f.get("id") is not None]
    if not files:
        return None
    if folders is None:
        folders = _design_folders(user_instruction, files, mode)
    if not folders:
        return None
    names = {d["name"].strip().lower(): d["name"] for d in folders}
    menu = "\n".join(f"- {d['name']}" + (f": {d['holds']}" if d.get("holds") else "") for d in folders)
    logger.info(f"[plan] {len(files)} files: designed {len(folders)} folders: {[d['name'] for d in folders][:40]}")
    placed: Dict[int, str] = {}
    leave: set = set()
    todo = list(files)
    for round_no in range(2):                     # the first pass, then once more for anything not answered
        chunks = [todo[i:i + _ASSIGN_CHUNK] for i in range(0, len(todo), _ASSIGN_CHUNK)]
        with ThreadPoolExecutor(max_workers=min(6, len(chunks))) as ex:
            for got, keep in ex.map(lambda c: _assign_chunk(user_instruction, menu, names, c, mode), chunks):
                placed.update(got)
                leave |= keep
        todo = [f for f in files if int(f["id"]) not in placed and int(f["id"]) not in leave]
        if not todo:
            break
        logger.warning(f"[plan] sorting round {round_no + 1} left {len(todo)} file(s) unanswered"
                       + ("; asking again" if round_no == 0 else ""))
    plan: Dict[str, List[int]] = {}
    for d in folders:
        ids = [int(f["id"]) for f in files if placed.get(int(f["id"])) == d["name"]]
        if ids:
            plan[d["name"]] = ids
    if not plan:
        return None
    if leave:
        logger.info(f"[plan] {len(leave)} file(s) left where they are, as asked")
        if info is not None:
            info["leave"] = set(leave)                # (plan_and_fill never asks about these again)
    plan = {"folders": plan}
    try:
        plan = _apply_tag_safety_net(plan, files, user_instruction,
                                     existing_folders_only=(mode == "auto_existing"))
    except Exception as e:
        logger.warning(f"[SafetyNet] Skipped due to error: {e}")
    return plan


def _existing_folders_in(user_instruction: str) -> List[str]:
    """The folder names an [AUTO-ORGANIZE - EXISTING FOLDERS ONLY] instruction lists — auto_watcher writes them as
    "EXISTING FOLDERS YOU CAN USE: 'A', 'B/C'". [] when there's no such line (the caller then plans the old way)."""
    m = re.search(r"EXISTING FOLDERS YOU CAN USE:\s*(.+)", user_instruction or "")
    if not m:
        return []
    out = []
    for name in re.findall(r"'([^'\n]+)'", m.group(1)):
        name = name.strip()
        if name and name not in out and len(name) <= 120:
            out.append(name)
    return out


def request_organization_plan(
    user_instruction: str,
    files: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Plan an organization for `files`: {"folders": {name: [file_id, ...]}} or None on failure. Every path plans
    through here (the Organize page and voice via PlanWorker, Auto-Organize, the MCP tools): plan_and_fill."""
    return plan_and_fill(user_instruction, files)[0]


def plan_and_fill(user_instruction: str, files: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """The plan, plus what happened to files the planner left out (Mac plan_and_fill, 2026-11-10/11). The planner
    forgets some files in its answer on big folders; those are asked about once more, alone, with the folders the
    plan already has, so they're placed by what they are like the rest. Anything still missing gets the closest
    folder by its tags and name. Files a two-step plan was TOLD to leave where they are (your instruction kept them)
    are never asked about again. A plan made in independent batches gets one tidy pass (_tidy_batches). Every plan
    ends with each file in one folder only (deduplicate_plan).
    Report: {"left_out", "placed_on_second_ask", "guessed": [names], "tidy"?}."""
    report: Dict[str, Any] = {"left_out": 0, "placed_on_second_ask": 0, "guessed": []}
    info: Dict[str, Any] = {"batched": False, "leave": set()}
    plan = _plan_all(user_instruction, files, info)
    if not plan or not plan.get("folders"):
        return plan, report
    with_id = [f for f in files if f.get("id") is not None]
    leave = info["leave"]
    missing = [f for f in _missing_files(plan, with_id) if int(f["id"]) not in leave]
    report["left_out"] = len(missing)
    if missing:
        second = _ask_about_missing(user_instruction, missing, list(plan["folders"]))
        report["placed_on_second_ask"] = _merge_into(plan, second, missing)
        still = [f for f in _missing_files(plan, with_id) if int(f["id"]) not in leave]
        if still:
            report["guessed"] = [f.get("file_name", "?") for f in still]
            placed = _ids_in(plan)
            plan = ensure_all_files_included(plan, placed | {int(f["id"]) for f in still}, with_id,
                                             existing_folders_only="EXISTING FOLDERS ONLY" in (user_instruction or ""))
        logger.info(f"Plan left out {len(missing)} of {len(with_id)} files: asked again, "
                    f"{report['placed_on_second_ask']} placed by the planner, {len(still)} by tags and name "
                    f"{report['guessed'][:10]}")
    if info["batched"]:                                            # (planned in batches: tidy the folder names)
        report["tidy"] = _tidy_batches(user_instruction, plan, with_id)
    plan = deduplicate_plan(plan)                                  # (every caller: no file in two folders)
    return plan, report


def _ids_in(plan: Optional[Dict[str, Any]]) -> set:
    out = set()
    for ids in ((plan or {}).get("folders") or {}).values():
        for fid in ids if isinstance(ids, (list, tuple)) else []:
            try:
                out.add(int(fid))
            except (TypeError, ValueError):
                pass
    return out


def _missing_files(plan: Dict[str, Any], files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    placed = _ids_in(plan)
    return [f for f in files if int(f["id"]) not in placed]


def _name_set(name: str) -> frozenset:
    """A folder name's words, however it's spelled ("Pet Photos" = "pet-photos" = "photos_pet")."""
    return frozenset(_name_words(name))


def _ask_about_missing(user_instruction: str, missing: List[Dict[str, Any]],
                       folders: List[str], only_existing: Optional[bool] = None) -> Dict[str, List[Any]]:
    """One more request, about only the files the plan left out (or a catch-all's files, for the tidy). Merged
    answers of every chunk; {} if it failed. only_existing: no new folder may be made (default: when the instruction
    is an "EXISTING FOLDERS ONLY" one)."""
    from .settings import settings
    if only_existing is None:
        only_existing = "EXISTING FOLDERS ONLY" in (user_instruction or "")
    out: Dict[str, List[Any]] = {}
    for ci in range(0, len(missing), PLAN_CHUNK_THRESHOLD):
        chunk = missing[ci:ci + PLAN_CHUNK_THRESHOLD]
        message = f"""These files were left out of an organization plan. Put each one in a folder.

The plan's instruction: "{user_instruction}"
Folders the plan already has: {json.dumps(folders[:200], ensure_ascii=False)}

Files ({len(chunk)} total):
{build_file_summary(chunk)}

RULES:
- Put each file in the folder from the list that it belongs in, judged by what the file is (its name, type, label, tags and caption), following the instruction
- Use the folder's exact name from the list
- {"Use ONLY folders from the list" if only_existing else "Only if no folder in the list fits it, make a new folder for it"}
- You MUST include EVERY file_id ({len(chunk)} total), each exactly once

Return JSON only: {{"folders": {{"folder name": [file_id, ...]}}}}"""
        for _attempt in range(_PLAN_CHUNK_RETRIES):
            reply = _request_ollama(message) if settings.ai_provider == "local" else _request_openai(message)
            reply = _normalize_plan_shape(reply)
            if reply and reply.get("folders"):
                for name, ids in reply["folders"].items():
                    if isinstance(ids, (list, tuple)):
                        out.setdefault(name, []).extend(ids)
                break
    return out


def _merge_into(plan: Dict[str, Any], second: Dict[str, List[Any]], missing: List[Dict[str, Any]]) -> int:
    """Add the second answer's files to the plan: only files that were missing, each once, under the plan's own
    folder name when the answer spelled it a little differently. Returns how many it placed."""
    wanted = {int(f["id"]) for f in missing}
    by_words = {_name_set(n): n for n in plan["folders"]}
    placed = 0
    for name, ids in second.items():
        home = name if name in plan["folders"] else by_words.get(_name_set(name), name)
        for fid in ids:
            try:
                fid = int(fid)
            except (TypeError, ValueError):
                continue
            if fid in wanted:
                plan["folders"].setdefault(home, []).append(fid)
                wanted.discard(fid)
                placed += 1
    return placed


_TIDY_SYSTEM = "You tidy file organization plans. Reply with JSON only."


def _tidy_batches(user_instruction: str, plan: Dict[str, Any], files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A big folder planned in batches that never see each other's folder names (the chunk path) gets folders that
    mean the same thing ("pet-photos", "photos-pets") and catch-alls ("financial-documents" next to "invoices", "tax"
    and "receipts"). One look at just the folder names (sizes, 4 example files each) says which to fold together and
    which are catch-alls; the catch-alls' files then go into the specific folders through the same "place these
    files" ask as left-out files, existing folders only (Mac _tidy_batches, 2026-11-11: 1,200 files, purity 0.81 ->
    0.96-1.00, files in two folders 16 -> 0). Names in your instruction are never folded or emptied, a file it can't
    place stays where it was, and NOTHING is folded into or out of a catch-all (the Mac's trap: "invoices", "tax" and
    "receipts" folded INTO "financial-documents", which was then spread — those files ended up in "contracts").
    Changes `plan` in place; any failure, or a change in which files the plan holds, leaves it as it was."""
    from .settings import settings
    from .vision import _call_openai_proxy
    done: Dict[str, Any] = {"folded": {}, "catch_alls": [], "moved": 0}
    folders = plan.get("folders") or {}
    if len(folders) < 3 or "EXISTING FOLDERS ONLY" in (user_instruction or "") or settings.ai_provider == "local":
        return done
    by_id = {}
    for f in files:
        try:
            by_id[int(f["id"])] = f
        except (TypeError, ValueError, KeyError):
            pass
    lines = []
    for name, ids in folders.items():
        examples = [by_id[i].get("file_name", "?") for i in sorted(_ids_in({"folders": {name: ids}}))[:4] if i in by_id]
        lines.append(f"- {json.dumps(name, ensure_ascii=False)}: {len(ids)} files, e.g. {', '.join(examples)}")
    message = f"""An organization plan was made in batches that didn't see each other's folder names. So some folders mean the same thing, and some are catch-alls whose files belong in more specific folders that also exist.

The plan's instruction: "{user_instruction}"

Folders (name: number of files, example files):
{chr(10).join(lines)}

Return JSON only: {{"same": {{"folder to fold away": "folder to keep"}}, "catch_all": ["folder", ...]}}
- "same": folders that mean the same thing (e.g. "pet-photos" and "photos-pets"). Keep the clearer name. Use exact names from the list, never a new name.
- "catch_all": a broad folder whose files belong in two or more of the more specific folders in the list (e.g. "documents" when "invoices" and "contracts" exist). Never list a folder whose files have nowhere more specific to go.
- Leave every other folder out. If nothing needs changing return {{"same": {{}}, "catch_all": []}}."""
    try:
        resp = _call_openai_proxy("chat", [{"role": "system", "content": _TIDY_SYSTEM},
                                           {"role": "user", "content": message}], max_tokens=1500, temperature=0,
                                  model=PLAN_MODEL)
        reply = _parse_json(resp["choices"][0]["message"]["content"]) if resp else None
    except Exception as e:
        logger.warning(f"Plan tidy: no answer ({e}); plan kept as it was")
        return done
    if not isinstance(reply, dict):
        return done
    said = (user_instruction or "").lower()
    protected = {n for n in folders if n.lower() in said}          # (a name you gave is never folded away)
    named_catch = {c for c in (reply.get("catch_all") or []) if isinstance(c, str)}
    same = reply.get("same") if isinstance(reply.get("same"), dict) else {}
    fold = {a: b for a, b in same.items() if isinstance(a, str) and isinstance(b, str) and a in folders
            and b in folders and a != b and a not in protected and a not in named_catch and b not in named_catch}

    def home(name: str) -> str:                                    # (follows a -> b -> c, never loops)
        seen = set()
        while name in fold and name not in seen:
            seen.add(name)
            name = fold[name]
        return name
    before = _ids_in(plan)
    out: Dict[str, List[Any]] = {}
    for name, ids in folders.items():
        out.setdefault(home(name), []).extend(ids)
    done["folded"] = {a: home(a) for a in fold if home(a) != a}
    catch = [c for c in named_catch if c in out and c not in protected]
    specific = [n for n in out if n not in catch]
    if catch and len(specific) >= 2:
        loose = [by_id[i] for c in catch for i in sorted(_ids_in({"folders": {c: out[c]}})) if i in by_id]
        # (the catch-alls are offered too: a file with no better home stays in its own, never forced elsewhere)
        answer = _ask_about_missing(user_instruction, loose, specific + catch, only_existing=True)
        loose_ids = {int(f["id"]) for f in loose}
        by_words = {_name_set(n): n for n in specific}
        moved = set()
        for name, ids in answer.items():
            target = name if name in specific else by_words.get(_name_set(name))
            if target is None:
                continue                                           # (a catch-all or a new name: those stay put)
            for fid in (_ids_in({"folders": {name: ids}}) & loose_ids) - moved:
                out[target].append(fid)
                moved.add(fid)
        for c in catch:
            out[c] = [x for x in out[c] if not (str(x).lstrip("-").isdigit() and int(x) in moved)]
        done["catch_alls"], done["moved"] = catch, len(moved)
    out = {n: ids for n, ids in out.items() if ids}
    if _ids_in({"folders": out}) != before:                        # (never lose or invent a file)
        logger.error("Plan tidy changed which files are in the plan; kept the plan as it was")
        return {"folded": {}, "catch_alls": [], "moved": 0}
    plan["folders"] = out
    logger.info(f"Plan tidy: folded {done['folded']}, catch-alls {catch} -> {done['moved']} files into specific folders")
    return done


def _plan_all(
    user_instruction: str,
    files: List[Dict[str, Any]],
    info: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Dispatcher: one AI request for a normal folder, chunked requests for a big
    one. (`info`, when given, is told how: info["batched"] = planned in independent chunks; info["leave"] = ids a
    two-step plan was told to leave where they are.)

    For <= PLAN_CHUNK_THRESHOLD files this calls ``_request_plan_single`` and
    returns its result UNCHANGED — i.e. behaviour for normal folders is byte-for-
    byte what it was before. Only large folders change: previously
    ``build_file_summary`` capped the prompt at 300 files and silently truncated
    the rest, so files past 300 were never planned. Now we split into
    PLAN_CHUNK_THRESHOLD-sized chunks and plan them **concurrently** (up to 6 in
    flight), then MERGE by folder name. Chunks are disjoint sets of file_ids, so
    the union can never place a file twice — conservation is preserved — and the
    callers' deduplicate_plan / ensure_all_files_included finish the job. Running
    the chunks in parallel (instead of one after another) is what keeps a big
    folder's planning from taking N sequential AI calls' worth of wall-clock.
    The merged plan has the exact same shape (``{"folders": {name: [file_id,…]}}``)
    the single path returns, so everything downstream is unchanged.

    More than BIG_PLAN_FILES files: planned in two steps instead (_plan_two_step — design the folders, then sort
    the files into them in parallel groups, with more thinking). An auto-organize into EXISTING folders skips the
    design (its folders are given). If the two-step plan fails, the one-call planner below is the fallback.
    """
    if not files:
        logger.warning("No files provided for organization")
        return None

    if len(files) > BIG_PLAN_FILES:
        mode = _plan_mode(user_instruction)
        given = None
        if mode == "auto_existing":
            names = _existing_folders_in(user_instruction)
            given = [{"name": n, "holds": ""} for n in names] if names else None
        if mode != "auto_existing" or given:
            try:
                plan = _plan_two_step(user_instruction, files, given, info)
                if plan and plan.get("folders"):
                    return plan
            except Exception as e:
                logger.warning(f"[plan] two-step planning failed: {e}")
            logger.warning(f"[plan] two-step planning gave no plan for {len(files)} files; planning in one call")

    if len(files) <= PLAN_CHUNK_THRESHOLD:
        return _plan_with_retries(user_instruction, files, 0, 1)

    chunks = [files[i:i + PLAN_CHUNK_THRESHOLD]
              for i in range(0, len(files), PLAN_CHUNK_THRESHOLD)]
    logger.info(f"Large organize ({len(files)} files): planning {len(chunks)} chunks in parallel")
    if info is not None:
        info["batched"] = True

    # Text-only calls on background threads — no RAM spike, only a handful at
    # once (capped at 6). Each chunk retries via _plan_with_retries. We drop
    # the old cross-chunk folder-name hint (it forced sequential ordering); the
    # merge-by-name below + the callers' deduplicate_plan/ensure_all_files_included
    # preserve conservation. Worst case is a tiny folder-naming inconsistency,
    # never a correctness one.
    from concurrent.futures import ThreadPoolExecutor
    results: List[Optional[Dict[str, Any]]] = [None] * len(chunks)

    def _plan_chunk(item):
        idx, chunk = item
        try:
            return idx, _plan_with_retries(user_instruction, chunk, idx, len(chunks))
        except Exception as e:
            logger.warning(f"Chunk {idx + 1}/{len(chunks)} failed: {e}")
            return idx, None

    with ThreadPoolExecutor(max_workers=min(6, len(chunks))) as ex:
        for idx, plan in ex.map(_plan_chunk, list(enumerate(chunks))):
            results[idx] = plan

    merged_folders: Dict[str, list] = {}
    any_ok = False
    for plan in results:
        if not plan or not isinstance(plan.get("folders"), dict):
            continue
        any_ok = True
        for name, ids in plan["folders"].items():
            if not isinstance(ids, (list, tuple)):
                continue
            merged_folders.setdefault(name, []).extend(ids)

    if not any_ok or not merged_folders:
        logger.error("Chunked organize produced no plan")
        return None
    if (user_instruction.startswith("[AUTO-ORGANIZE]") and "User's specific instructions:" not in user_instruction
            and "EXISTING FOLDERS ONLY" not in user_instruction):      # (names you gave are never merged)
        merged_folders = _merge_similar_folders(merged_folders)
    return {"folders": merged_folders}


def _merge_similar_folders(folders: Dict[str, List[Any]]) -> Dict[str, List[Any]]:
    """Chunks of a big folder are planned without seeing each other's names, which gave "invoices" next to
    "invoices-northwind", and "recipes-tomato-soup" next to "tomato-soup-recipes". Fold a name into a bigger
    folder when one's words are all in the other. (A nested "a/b" name is never merged; Windows: "a\\b" too.)"""
    def words(name: str) -> frozenset:
        return frozenset(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", name.lower()))

    def nested(name: str) -> bool:
        return "/" in name or "\\" in name

    kept: Dict[str, List[Any]] = {}
    for name in sorted(folders, key=lambda n: (-len(folders[n]), n)):
        w = words(name)
        home = None if nested(name) or not w else next(
            (k for k in kept if not nested(k) and (w <= words(k) or words(k) <= w)), None)
        if home is None:
            kept[name] = list(folders[name])
        else:
            kept[home].extend(folders[name])
    return kept


def _request_plan_single(
    user_instruction: str,
    files: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Send user instruction + file metadata to LLM (single request).
    Returns the proposed plan as a dict, or None on failure.

    The LLM acts only as a planner - it never executes anything.
    """
    from .settings import settings

    if not files:
        logger.warning("No files provided for organization")
        return None

    file_summary = build_file_summary(files)
    
    # Detect auto-organize mode vs specific instruction mode
    # "[AUTO-ORGANIZE" without the closing bracket: the existing-folders instruction starts
    # "[AUTO-ORGANIZE - EXISTING FOLDERS ONLY]", so with the bracket its rules never reached the planner (Mac fix 9).
    is_auto_organize = user_instruction.startswith("[AUTO-ORGANIZE")

    # Check for different auto-organize modes
    is_existing_folders_only = "EXISTING FOLDERS ONLY" in user_instruction
    has_user_instruction = "User's specific instructions:" in user_instruction

    if is_auto_organize and is_existing_folders_only:
        # ORGANIZE AS-IS MODE: Must use only existing folders (passed in instruction)
        user_message = f"""User instruction: "{user_instruction}"

Files to organize ({len(files)} total):
{file_summary}

CRITICAL - EXISTING FOLDERS ONLY:
- You can ONLY use the folders listed in the instruction - DO NOT create new folders.
- For each file, look at its tags / label / caption first. If the file clearly belongs in one of the existing folders (e.g. a file tagged 'bear' when there's a 'Bears' or 'animals/Bears' folder), place it there. Filename is a secondary signal.
- Each file_id may appear in AT MOST one folder. If a file genuinely fits no existing folder, OMIT IT from your response — do NOT invent a new folder for it, and do NOT force it into the closest-sounding existing folder. The app will leave omitted files in place on disk.
- It is fine for your response to cover only a subset of file_ids.
- Each file_id must appear in exactly ONE folder OR be omitted entirely; never both.

Propose an organization plan. Return JSON only."""
    elif is_auto_organize and has_user_instruction:
        # Auto-organize WITH user instruction: follow user instruction, route any
        # un-covered file to the CLOSEST user-specified folder (do NOT invent
        # additional folders like "documents", "misc", or "other"). This is the
        # Mac prompt's key insight: rather than letting the AI guess a catch-all
        # name, force it to reuse one of the user's own named folders.
        user_message = f"""User instruction: "{user_instruction}"

Files to organize ({len(files)} total):
{file_summary}

CRITICAL RULES:
- FOLLOW the user's specific instructions EXACTLY: every file the instruction is about goes in the folder it names for it
- Only the files the instruction is about go in a folder it names: "put the bills in Money" puts ONLY bills in Money;
  a recipe, a photo or a ticket is not a bill and never goes there
- If the user says to put everything (all files) in one folder, every file goes there
- A file the instruction does NOT cover: put it in another folder the user named only if that folder clearly fits it
  (a PDF report when they named a 'Docs' folder for documents); otherwise put it in ONE new folder with a short, clear
  name for its type or topic ('recipes', 'photos', 'tickets') — never in a folder the user named for something else,
  and never 'misc', 'other' or 'unsorted'. (This overrides "do not create folders the user didn't name": new files
  keep arriving here, and one the instruction isn't about must not be filed as something it isn't.) Unsure whether
  a file IS one of the things the instruction names (a lease is not an invoice, a schedule is not a bill)? It isn't.
- Examples, for the instruction "put the bills in a folder called Money":
  files 1 electricity bill, 2 pasta recipe, 3 beach photo -> {{"folders": {{"Money": [1], "recipes": [2], "photos": [3]}}}}
  one file, 1 pasta recipe -> {{"folders": {{"recipes": [1]}}}}
- You MUST include EVERY file_id in your response
- Each file_id must appear in exactly ONE folder
- Total files in your response must equal {len(files)}

Propose an organization plan. Return JSON only."""
    elif is_auto_organize:
        # Auto-organize WITHOUT specific instruction: let AI decide best organization
        user_message = f"""Analyze these files and organize them in the SMARTEST way possible.

Files to organize ({len(files)} total):
{file_summary}

ANALYZE THE FILES and choose the BEST organization approach:
- If files seem related to different PROJECTS → organize by project name
- If files seem related to different CLIENTS/PEOPLE → organize by client/person
- If files seem related to different TOPICS/SUBJECTS → organize by topic
- If files seem related to different EVENTS/DATES → organize by event
- If files are just random mixed types → organize by file type (photos, videos, docs, etc.)

CRITICAL RULES:
- LOOK at the file names and tags to understand what they are about
- Choose folder names that MAKE SENSE for these specific files
- Use CLEAR, DESCRIPTIVE folder names (not generic like "folder1")
- DO NOT put all files in a single folder - create a logical structure
- You MUST include EVERY file_id in your response ({len(files)} total)
- Each file_id must appear in exactly ONE folder
- Create as many folders as the files naturally need (a handful for a small mixed folder; one per topic when the files have clear topics) — never one folder per file

Examples:
- Project files: {{"folders": {{"website-redesign": [1,2,3], "mobile-app": [4,5], "marketing": [6,7,8]}}}}
- Mixed types: {{"folders": {{"photos": [1,2,3], "videos": [4,5], "documents": [6,7,8]}}}}

Propose an organization plan. Return JSON only."""
    else:
        # Manual organization (non-auto) with user instruction (or empty instruction).
        #
        # Note: the NESTED-FOLDER SYNTAX rule below is added to the user
        # message for manual modes only (not the shared SYSTEM_PROMPT).
        # Without it, when the user asks for a 2-level structure (e.g.
        # "create everything else and inside that a folder called cartoons")
        # the AI sometimes emits a dict inside a folder's file_id list
        # (e.g. {"everything else": [{"cartoons":[83]}, 67, 68, ...]}).
        # That's not valid per our schema, and the malformed entry causes
        # the affected file to be silently dropped by plan_to_moves.
        # Spelling out the syntax with an example fixes it.
        if user_instruction and user_instruction.strip():
            user_message = f"""User instruction: "{user_instruction}"

Files to organize ({len(files)} total):
{file_summary}

CRITICAL RULES:
- FOLLOW the user's instruction for organizing these files
- Only the files the instruction is about go in the folder(s) it names: "put the bills in Money" puts ONLY the
  bills in Money; a party plan, a recipe or a photo is not a bill and never goes there
- If user says "move all files to X" (or "everything in X"), put ALL files in folder "X"
- If user specifies a structure, follow it exactly
- For any files NOT covered by the instruction, organize them logically by type or topic, in OTHER folders —
  never in a folder the instruction named for something else, and with a real name ('events', 'documents'), not
  'Miscellaneous', 'Misc' or 'Other' unless the user asked for one
- You MUST include EVERY file_id in your response ({len(files)} total)
- Every file_id must appear exactly once in your response
- NEVER return empty folders

NESTED-FOLDER SYNTAX (max 2 levels):
- To create a nested folder, put a forward slash IN THE FOLDER NAME.
- USE THE EXACT FOLDER NAMES THE USER TYPED — do not change capitalisation, pluralisation, or spelling. If the user wrote "Cartoon", use "Cartoon", not "cartoons" or "Cartoons".
- Every folder's value MUST be a flat list of integer file_ids — never a dict, never a nested object.
- Generic correct shape (use the user's actual names, not these placeholders):
{{
  "folders": {{
    "<parent>": [<id>, <id>, ...],
    "<parent>/<child>": [<id>]
  }}
}}
- WRONG (do NOT do this — putting a dict inside the file_id list silently drops the affected files):
{{
  "folders": {{
    "<parent>": [{{"<child>": [<id>]}}, <id>, <id>, ...]
  }}
}}

Propose an organization plan. Return JSON only."""
        else:
            # No instruction provided - let AI decide the best organization
            user_message = f"""Analyze these files and organize them in the SMARTEST way possible.

Files to organize ({len(files)} total):
{file_summary}

ANALYZE THE FILES and choose the BEST organization approach:
- If files seem related to different PROJECTS → organize by project name
- If files seem related to different CLIENTS/PEOPLE → organize by client/person
- If files seem related to different TOPICS/SUBJECTS → organize by topic
- If files seem related to different EVENTS/DATES → organize by event
- If files are just random mixed types → organize by file type (photos, videos, docs, etc.)

CRITICAL RULES:
- LOOK at the file names and tags to understand what they are about
- Choose folder names that MAKE SENSE for these specific files
- Use CLEAR, DESCRIPTIVE folder names (not generic like "folder1")
- DO NOT put all files in a single folder - create a logical structure
- You MUST include EVERY file_id in your response ({len(files)} total)
- Each file_id must appear in exactly ONE folder
- Create as many folders as the files naturally need (a handful for a small mixed folder; one per topic when the files have clear topics) — never one folder per file

NESTED-FOLDER SYNTAX (max 2 levels):
- To create a nested folder, put a forward slash IN THE FOLDER NAME (e.g. "Photos/Vacation").
- Every folder's value MUST be a flat list of integer file_ids — never a dict, never a nested object.
- Correct shape: {{"folders": {{"<parent>": [<id>,<id>], "<parent>/<child>": [<id>]}}}}

Propose an organization plan. Return JSON only."""

    provider = settings.ai_provider

    if provider == 'openai':
        plan = _request_openai(user_message)
    elif provider == 'local':
        plan = _request_ollama(user_message)
    else:
        logger.warning("No AI provider configured")
        return None

    # Coerce to the canonical {"folders": {...}} shape: wraps a wrapper-less
    # reply, and nulls an empty/garbled one so _plan_with_retries can retry.
    plan = _normalize_plan_shape(plan)

    # Deterministic tag-based safety net: rewrite any placement that violates
    # an explicit user category→folder rule. No-op when the instruction has
    # no extractable mapping, or when no file's tags match a user rule.
    if plan:
        try:
            plan = _apply_tag_safety_net(
                plan, files, user_instruction,
                existing_folders_only=is_existing_folders_only,
            )
        except Exception as e:
            logger.warning(f"[SafetyNet] Skipped due to error: {e}")

    return plan


def request_plan_refinement(
    original_instruction: str,
    current_plan: Dict[str, Any],
    feedback: str,
    files: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Refine an existing plan based on user feedback.
    Returns the updated plan as a dict, or None on failure.
    """
    from .settings import settings
    
    if not current_plan:
        logger.warning("No plan to refine")
        return None
    
    file_summary = build_file_summary(files)
    
    # Format current plan for context
    current_plan_json = json.dumps(current_plan, indent=2)
    
    user_message = f"""Original instruction: "{original_instruction}"

Current plan:
{current_plan_json}

User feedback: "{feedback}"

Files available ({len(files)} total):
{file_summary}

Based on the user feedback, provide an UPDATED organization plan.
Apply the user's requested changes to the current plan.
First work out every file the feedback refers to: check each file's name, label and tags. "The invoice and the receipt"
means every file matching either word. Return JSON with "files_mentioned": [every such file_id] FIRST, then "folders".
Each file in files_mentioned must end up where the feedback says, under the folder name it gives.
If the feedback renames or merges whole folders, also return "folders_renamed": {{"old folder name": "new folder name"}}.
Return the complete updated plan as JSON only."""

    provider = settings.ai_provider

    if provider == 'openai':
        plan = _request_openai(user_message)
        if _same_folders(plan, current_plan):      # (measured: about 1 in 4 comes back unchanged)
            logger.info("Refined plan came back unchanged — asking once more")
            plan = _request_openai(user_message + "\n\nYour last answer did not change the plan. "
                                                  "The user asked for a change: make it.") or plan
        plan = _gather_mentioned(plan, current_plan, feedback)
        return _only_mentioned_move(current_plan, plan, feedback)
    elif provider == 'local':
        plan = _gather_mentioned(_request_ollama(user_message), current_plan, feedback)
        return _only_mentioned_move(current_plan, plan, feedback)
    else:
        logger.warning("No AI provider configured")
        return None


# A change about the WHOLE plan (not a few files): the new plan is taken as it is.
_WHOLE_PLAN_WORDS = re.compile(
    r"\b(?:start over|from scratch|re-?do|redo it|reorgani[sz]e|all (?:the |my )?files|every(?:thing| file)|"
    r"fewer folders|more folders|less folders|different(?:ly)?|by (?:type|date|topic|year|month|kind)|"
    r"split|separate them)\b", re.I)


def _ids_of(folders) -> Dict[str, set]:
    out = {}
    for name, ids in (folders or {}).items():
        s = set()
        for fid in ids if isinstance(ids, (list, tuple)) else []:
            try:
                s.add(int(fid))
            except (TypeError, ValueError):
                pass
        out[name] = s
    return out


def _only_mentioned_move(current_plan: Optional[Dict[str, Any]], new_plan: Optional[Dict[str, Any]],
                         feedback: str = "") -> Optional[Dict[str, Any]]:
    """A change moves only the files it's about ("files_mentioned"); every other file keeps the folder it had in the
    plan the person saw. (Mac 26e1a3b; the same bug was live on Windows: "put the recipes in a folder called Food"
    came back with nearly every file in Food, and "go ahead" moved them all.)
    A rename can't be told from "move just these" by the plan's shape (a plan that dumped every file into the named
    folder looks like a merge — the Food bug), so the answer SAYS it: "folders_renamed" {"old": "new"} (Mac
    09e83da) — every file of a renamed folder goes with it, named or not. The whole new plan is taken when the AI
    listed no files, or the words are about the whole plan ("start over", "by type", "fewer folders"). Either way,
    a file the new plan forgot keeps its folder (a change never silently drops files out of the plan)."""
    if not isinstance(new_plan, dict):
        return new_plan
    fr = new_plan.pop("folders_renamed", None)
    renamed = ({str(k): v.strip() for k, v in fr.items() if isinstance(v, str) and v.strip()}
               if isinstance(fr, dict) else {})
    mentioned = set()
    for fid in new_plan.pop("files_mentioned", None) or []:
        try:
            mentioned.add(int(fid))
        except (TypeError, ValueError):
            pass
    if not isinstance(new_plan.get("folders"), dict) or not isinstance(current_plan, dict):
        return new_plan
    old = _ids_of(current_plan.get("folders"))
    new = _ids_of(new_plan.get("folders"))
    whole = not mentioned or bool(_WHOLE_PLAN_WORDS.search(feedback or ""))
    if whole:
        placed = set().union(*new.values()) if new else set()
        forgot = [(name, i) for name, ids in old.items() for i in sorted(ids) if i not in placed]
        if forgot:
            folders = {k: list(v) for k, v in new_plan["folders"].items()}
            for name, i in forgot:
                folders.setdefault(name, []).append(i)
            logger.info(f"Plan change: kept {len(forgot)} file(s) the new plan forgot in their folders")
            return {**new_plan, "folders": folders}
        return new_plan
    new_home: Dict[int, str] = {}
    for name, ids in new.items():
        for i in ids:
            new_home.setdefault(i, name)
    out: Dict[str, List[int]] = {}
    seen = set()
    for name, ids in (current_plan.get("folders") or {}).items():
        for fid in ids:
            try:
                i = int(fid)
            except (TypeError, ValueError):
                continue
            if i in seen:
                continue
            seen.add(i)
            out.setdefault(new_home.get(i, name) if i in mentioned else renamed.get(name, name), []).append(i)
    for i in sorted(mentioned - seen):                    # (named, but not in the plan before)
        if i in new_home:
            out.setdefault(new_home[i], []).append(i)
    moved = sum(1 for ids in out.values() for i in ids if i in mentioned)
    logger.info(f"Plan change: {len(mentioned)} file(s) named by the change, {moved} placed by it; "
                f"the other {len(seen) - len(mentioned & seen)} kept their folders")
    return {**{k: v for k, v in new_plan.items() if k != "folders"}, "folders": {k: v for k, v in out.items() if v}}


_KEEP_WORDS = re.compile(r"\b(?:leave|keep|stay|stays|except|but not|not the|apart from|other than)\b", re.I)


def _gather_mentioned(plan: Optional[Dict[str, Any]], current_plan: Optional[Dict[str, Any]],
                      feedback: str) -> Optional[Dict[str, Any]]:
    """The model lists the files the feedback means ("files_mentioned"), then sometimes leaves one of them where it
    was. Seen live: "Put the invoice and the receipt in a folder called Billing" -> files_mentioned [invoice, receipt],
    but only the receipt in "billing". When the feedback sends them to ONE folder (the only folder in the plan it
    names, else the only new one holding any of them), every file it listed goes there. Not when the feedback also
    keeps something where it is ("leave the receipt", "except…"), or names more than one destination."""
    try:
        ids = plan.get("files_mentioned") if isinstance(plan, dict) else None
        folders = plan.get("folders") if isinstance(plan, dict) else None
        if not isinstance(ids, list) or len(ids) < 2 or not isinstance(folders, dict) or _KEEP_WORDS.search(feedback or ""):
            return plan
        want = {str(i) for i in ids}
        holding = [n for n, f in folders.items() if isinstance(f, list) and any(str(x) in want for x in f)]
        if len(holding) < 2:
            return plan
        said = " " + " ".join(_name_words(feedback or "")) + " "

        def named(folder: str) -> bool:
            key = _name_key(re.split(r"[\\/]", str(folder))[-1])
            return bool(key) and (f" {key} " in said or f" {key}s " in said)
        everywhere = [n for n in folders if named(n)]
        if len(everywhere) > 1:
            return plan
        target = [n for n in holding if named(n)]
        if not target:
            old = set(((current_plan or {}).get("folders") or {}).keys())
            target = [n for n in holding if n not in old]
        if len(target) != 1:
            return plan
        dest = target[0]
        moved = []
        for n in holding:
            if n == dest:
                continue
            keep = [x for x in folders[n] if str(x) not in want]
            moved += [x for x in folders[n] if str(x) in want]
            folders[n] = keep
        folders[dest] = list(folders[dest]) + moved
        for n in [n for n, f in folders.items() if isinstance(f, list) and not f]:
            del folders[n]
        if moved:
            logger.info(f"[refine] the feedback sends its files to {dest!r}: moved {moved} there too")
    except Exception as e:
        logger.warning(f"[refine] couldn't check files_mentioned: {e}")
    return plan


def _same_folders(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    def shape(p):
        folders = (p or {}).get("folders") if isinstance(p, dict) else None
        return {k: sorted(map(str, v if isinstance(v, (list, tuple)) else [v]))
                for k, v in (folders if isinstance(folders, dict) else {}).items()}
    return bool(a) and shape(a) == shape(b)


_HOW_FILLER = {"by", "the", "a", "an", "in", "into", "to", "of", "and", "all", "put", "move", "sort", "organize",
               "folder", "folders", "called", "named", "file", "files"}


def _how_from_reply(raw: str) -> Optional[str]:
    """The answer inside organize_how's reply. Windows: the shared proxy forces a JSON object on every chat call,
    so the model answers {"how": "..."}; a bare text answer is taken as it is. None = no answer at all."""
    raw = (raw or "").strip()
    if not raw:
        return None
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e > s:
        try:
            obj = json.loads(raw[s:e + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            val = obj.get("how", next((v for v in obj.values() if isinstance(v, str)), ""))
            return str(val or "NONE")             # (an explicit empty answer: it doesn't say how)
    return raw


def organize_how(sentence: str, folder: str) -> str:
    """What a spoken organize request says about HOW to sort, without which folder; "" if it doesn't say.
    The planner must not get the whole sentence: "organize the Reports folder" reads to it as "put everything in
    a folder called Reports", the files are already there, and nothing moves. On any failure: the sentence."""
    try:
        from .vision import _call_openai_proxy
        # (Windows: the shared openai-proxy forces response_format=json_object on ALL chat calls, so the request
        # must ask for JSON (and say "json") or OpenAI answers 400 and this would quietly give back the sentence.)
        system = (
            "You read a spoken request to organize the files in a folder. Which folder has already been worked out. "
            "Find ONLY the part that says HOW to sort the files, e.g. 'by file type', 'by year', 'put the invoices "
            "in a folder called Money'. Leave out which folder is being organized and words like 'organize', 'sort' "
            "or 'clean up' on their own. If it doesn't say how, the answer is exactly: NONE. "
            'Respond with a JSON object of the form {"how": "<that part, or NONE>"}. JSON only, nothing else.'
        )
        user = f"Request:\n{sentence}\n\nFolder being organized: {Path(folder).name}\n\nReturn JSON only."
        resp = _call_openai_proxy("chat", [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                  max_tokens=400, temperature=0, model=PLAN_MODEL)   # (nano missed real instructions)
        if not resp:
            return sentence
        out = _how_from_reply(resp.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
        out = (out or "").strip().strip("\"'`").strip()
        if not out:
            return sentence
        if out.upper().rstrip(".") == "NONE":
            return ""
        # (Measured: it sometimes makes one up for a plain "organize the Reports folder". An answer that shares
        # no real word with what you said, other than the folder's own name, is one you didn't ask for.)
        said = set(re.findall(r"[a-z0-9]+", sentence.lower()))
        name = set(re.findall(r"[a-z0-9]+", Path(folder).name.lower()))
        words = set(re.findall(r"[a-z0-9]+", out.lower())) - _HOW_FILLER - name
        return out if words & said else ""
    except Exception as e:
        logger.warning(f"organize_how failed: {e}")
        return sentence


_NOT_A_FOLDER_NAME = {"a", "an", "the", "my", "our", "your", "this", "that", "these", "those", "in", "on", "at", "from",
                      "of", "to", "into", "organize", "sort", "clean", "tidy", "up", "out", "please", "and",
                      # (not a name either: "put the screenshots in their own folder", "a separate folder")
                      "their", "its", "his", "her", "own", "separate", "same", "different", "another"}


def _fold(text: str) -> str:
    """Lower case, accents off ("Café" is said and typed "cafe"), curly quotes straight, "&" said as "and"."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t.lower().replace("’", "'").replace("‘", "'").replace("&", " and ")


_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                 "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}


def _name_words(text: str) -> list:
    """The words of a name, the same however it's said or typed: "Mom's Recipes" = "moms recipes", "Bills &
    Receipts" = "bills and receipts", "New folder (2)" = "new folder two". (Found by the 2026-11-10 hunt: these
    never counted as named, and "organize new folder 2" matched Windows' "New folder" instead.)"""
    words = [w.replace("'", "") for w in re.findall(r"[a-z0-9']+", _fold(text))]
    return [_NUMBER_WORDS.get(w, w) for w in words if w and w != "and"]


def _name_key(text: str) -> str:
    """A folder name as it would be spoken: no case, accents, apostrophes, separators, plural, or trailing "folder"."""
    key = " ".join(_name_words(text))
    key = re.sub(r"\s*\bfolders?$", "", key)
    return key[:-1] if key.endswith("s") else key


def _folder_phrases(sentence: str) -> set:
    """What the sentence calls a folder: "the Filect Test Small folder" -> "filect test small"; "new folder 2" ->
    "new folder 2" (Windows names copies "New folder (2)")."""
    words = _name_words(sentence)
    phrases = set()
    for i, w in enumerate(words):
        if w in ("folder", "folders"):
            j = i
            while j > 0 and words[j - 1] not in _NOT_A_FOLDER_NAME:
                j -= 1
            if j < i:
                numbered = i + 1 < len(words) and words[i + 1].isdigit()
                phrases.add(_name_key(" ".join(words[j:i + 2] if numbered else words[j:i])))
    return phrases


def sentence_names_a_folder(sentence: str) -> bool:
    """Does it say which folder at all? ("put these in a folder called Money" doesn't.)"""
    return bool(_folder_phrases(sentence)) or bool(re.search(r"\b(?:desktop|downloads?|documents?)\b", sentence, re.I))


def folder_is_named(name: str, sentence: str) -> bool:
    """Does the sentence name this folder outright? "the Blue Elephant Project folder" names a folder called exactly
    that, not one called "Project"; and the plain word "folder" never names a folder called "Folder"."""
    key = _name_key(name)
    if not key:
        return False
    phrases = _folder_phrases(sentence)
    if phrases:
        return key in phrases
    # (No "... folder" in the sentence: the name has to be where a folder's name goes, so that a folder that happens
    # to be called "put" isn't named by "Put this in...".)
    said = " ".join(_name_words(sentence))
    return bool(re.search(r"\b(?:organi[sz]e|sort|clean(?: up)?|tidy(?: up)?|the|my|our|this|in|called|named) "
                          + " ".join(map(re.escape, key.split())) + r"s?(?!\w)", said))


def _folder_named_in(sentence: str, candidates: list) -> Optional[str]:
    """The one subfolder a sentence names outright, or None. The AI guesses by meaning and, measured, sometimes
    picked a similar name ("Filect Organize Test" for "Filect Test Small") or none at all. Never Desktop / Downloads
    / Documents themselves: "on my desktop" doesn't mean the whole Desktop, it says where to look.
    Windows: paths end in "\\" or "/", and compare without case (C:\\Users\\x\\desktop is the Desktop)."""
    def name(c):
        return os.path.basename(c.rstrip("/\\"))

    def key(c):
        c = c.rstrip("/\\") or c
        return os.path.normcase(os.path.normpath(c))

    def parent(c):
        d = os.path.dirname(c.rstrip("/\\"))
        return key(d) if d and key(d) != key(c) else ""        # (a drive root has no parent)

    sentence = sentence or ""
    known = {key(c) for c in candidates}
    tops = {key(c) for c in candidates if parent(c) not in known  # (the place you said: "...in my Documents")
            and re.search(rf"(?<!\w){re.escape(name(c))}(?!\w)", sentence, re.I)}
    subs = [c for c in candidates if parent(c) in known and len(name(c)) >= 3
            and folder_is_named(name(c), sentence) and (not tops or parent(c) in tops)]
    # "put these in a folder called Money" says where things go, not which folder
    plain = [c for c in subs if not re.search(rf"\b(?:called|named)\s+{re.escape(name(c))}(?!\w)", sentence, re.I)]
    subs = plain if plain or re.search(r"\b(?:put|move|into)\b", sentence, re.I) else subs
    if not subs:
        return None
    longest = max(len(name(c)) for c in subs)
    best = [c for c in subs if len(name(c)) == longest]
    return best[0] if len(best) == 1 else None


# Words that never say WHICH folder: what to do ("organize", "keep … tidy"), how ("only new files"), and filler.
# Measured live: the AI picked "organize-and-auto-organize-for-windows" for "organize my messy stuff" and "auto organizing"
# for "keep my inbox organized automatically" — the action words matched those folders' names.
_GUESS_SKIP = frozenset("""
a an the my our your this that these those it its me i we you us please can could would will should just
now then too also again all any some of for in on at to into onto from with by and or so up out over
folder folders file files stuff things thing
organize organise organized organised organizing organising organizer auto automatic automatically sort sorted
sorting tidy tidied tidying clean cleaned cleaning arrange arranged arranging keep keeping kept neat set setup start
make get have want like help do turn
new only existing already there using use way how everything
""".split())
_GUESS_PLACES = {"desktop": "desktop", "download": "downloads", "document": "documents"}


def _guess_words(t):
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", _fold(t))}


def _only_action_words_match(sentence: str, path: str) -> bool:
    """The AI's pick shares nothing with the sentence but an action word ("Organize my photos" -> the
    "organize-and-auto-organize-for-windows" folder, measured): not a match. (A pick by meaning — "my photos" ->
    "images" — shares no word at all, and stands.)"""
    def root(w):                     # ("organized automatically" and "auto organizing" share the same two roots)
        for r in ("organi", "auto", "sort", "tid", "clean", "arrang", "keep", "kept"):
            if w.startswith(r):
                return "action:" + r
        return w

    said = {root(w) for w in _guess_words(sentence)}
    named = {root(w) for w in _guess_words(os.path.basename(path.rstrip("/\\")))}
    common = said & named
    return bool(common) and all(w.startswith("action:") for w in common)


def _word_guess_folder(sentence: str, candidates: list) -> Optional[str]:
    """The one subfolder whose name shares the words that describe it ("my messy stuff" -> Demo Messy Desk, "the
    pile in my downloads" -> Downloads\\Demo Downloads Pile), or None (no word in common, or a tie: the AI decides).
    Only the part before an instruction counts ("…, only new files", "… and put the bills in Money"). A guess: the
    caller asks before anything is read or moved. Never Desktop / Downloads / Documents themselves."""
    words = _guess_words

    def name(c):
        return os.path.basename(c.rstrip("/\\"))

    def key(c):
        return os.path.normcase(os.path.normpath(c.rstrip("/\\") or c))

    head = re.split(r",|;|\b(?:put|move|send|by|called|named|except|but|so that|and then)\b", sentence or "",
                    maxsplit=1, flags=re.I)[0]
    said = words(head) - _GUESS_SKIP
    places = {_GUESS_PLACES[w] for w in said if w in _GUESS_PLACES}
    said -= set(_GUESS_PLACES)
    if not said:
        return None
    known = {key(c) for c in candidates}
    scored = []
    for c in candidates:
        par = os.path.dirname(c.rstrip("/\\"))
        if not par or key(par) not in known:                 # (a place itself, not a folder in it)
            continue
        hits = len(said & words(name(c)))
        if hits:
            scored.append((hits, not places or name(par).lower() in places, c))
    if not scored:
        return None
    best = max((h, p) for h, p, _ in scored)
    top = [c for h, p, c in scored if (h, p) == best]
    return top[0] if len(top) == 1 else None


def _request_openai(user_message: str) -> Optional[Dict[str, Any]]:
    """Request plan via OpenAI through the Supabase Edge Function proxy."""
    try:
        from .vision import _call_openai_proxy

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        resp_data = _call_openai_proxy("chat", messages, max_tokens=4000, temperature=0.1, model=PLAN_MODEL)
        if not resp_data:
            logger.error("OpenAI proxy returned no data for organization plan")
            return None

        choices = resp_data.get("choices", [])
        if not choices:
            logger.warning("OpenAI proxy response had no choices")
            return None
        content = choices[0].get("message", {}).get("content", "") or ""
        if not content:
            logger.warning("OpenAI proxy response had empty content")
            return None

        logger.info(f"OpenAI organization response (truncated): {content[:300]}")
        return _parse_json(content)
    except Exception as e:
        logger.error(f"OpenAI organization request failed: {e}")
        return None


def resolve_folder_with_ai(instruction: str, candidate_paths: List[str]) -> Optional[str]:
    """Map a spoken organize request to exactly ONE folder from a bounded
    candidate list (real paths). Returns the exact path verbatim if the LLM
    picks one that's in the list, else None.

    This is how voice organize decides WHICH folder to organize — it does NOT
    read Explorer's current directory. The caller passes the known folders
    (Desktop / Downloads / Documents + immediate subdirs); the model matches
    the spoken request against them.
    """
    instruction = (instruction or "").strip()
    if not instruction or not candidate_paths:
        return None
    named = _folder_named_in(instruction, candidate_paths)   # (said outright: no AI, so never a similar name)
    if named:
        logger.info(f"[VOICE ORGANIZE] folder resolve: {instruction!r} -> {named!r} (named outright)")
        return named
    by_words = _word_guess_folder(instruction, candidate_paths)   # (a guess: the caller always asks "did you mean")
    if by_words:
        logger.info(f"[VOICE ORGANIZE] folder resolve: {instruction!r} -> {by_words!r} (by its words)")
        return by_words
    try:
        from .vision import _call_openai_proxy
        listing = "\n".join(candidate_paths)
        # NOTE: the shared openai-proxy forces response_format=json_object on ALL
        # chat calls, so the request MUST ask for JSON (and contain the word
        # "json") or OpenAI 400s. Return the choice as a JSON object and parse it.
        system = (
            "You map a spoken request to the ONE folder the user wants to "
            "organize. Choose STRICTLY from the AVAILABLE FOLDERS list below "
            "(they are real paths).\n"
            "How to match:\n"
            "- The request's action words (organize, organized, organizing, auto organize, sort, tidy, clean up, "
            "arrange, keep ... organized / tidy, automatically) say what to DO. They are NOT the folder's name: never "
            "pick a folder because its name contains one of them. Only when the user plainly calls a folder by such a "
            "name ('the auto organizing folder', 'my Organize Test folder') is that the folder.\n"
            "- Match on the OTHER words: the folder's name or what it holds ('my messy stuff' -> a folder named like "
            "'Messy Desk'; 'my camera roll' -> 'Camera Roll'; 'my inbox' -> a folder whose name has 'Inbox'). Names can "
            "be loose: singular / plural, a word left out, a prefix the user doesn't say ('Demo ...', 'Filect ...').\n"
            "- A place they say ('in my downloads', 'on the desktop') is WHERE the folder is: pick a folder inside that "
            "place whose name matches the other words. Pick the place itself (e.g. Downloads) only when they mean the "
            "whole place ('organize my downloads').\n"
            "Respond with a JSON object of the form "
            '{"folder": "<exact folder path, verbatim>"} — or {"folder": "NONE"} '
            "if none clearly matches. JSON only, no prose."
        )
        user = f"REQUEST: {instruction}\n\nAVAILABLE FOLDERS:\n{listing}\n\nReturn JSON only."
        resp = _call_openai_proxy(
            "chat",
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            max_tokens=120, temperature=0,
        )
        if not resp:
            return None
        choices = resp.get("choices", [])
        if not choices:
            return None
        raw = (choices[0].get("message", {}).get("content", "") or "").strip()
        # Extract the folder value from the JSON object (fall back to raw text if
        # the model somehow returned a bare string).
        answer = ""
        try:
            s = raw.find("{"); e = raw.rfind("}")
            obj = json.loads(raw[s:e + 1] if (s != -1 and e != -1) else raw)
            answer = str(obj.get("folder", "") or "").strip()
        except Exception:
            answer = raw
        answer = answer.strip().strip('"\'`').strip()
        if not answer or answer.upper() == "NONE":
            logger.info(f"[VOICE ORGANIZE] folder resolve: NONE for {instruction!r}")
            return None
        # Accept only if the answer actually corresponds to a candidate.
        cand = {p.rstrip("/\\"): p for p in candidate_paths}
        resolved = cand.get(answer.rstrip("/\\"))
        if resolved is None:
            # Looser normalized match (slash/case differences on Windows).
            norm = {os.path.normpath(p).lower(): p for p in candidate_paths}
            resolved = norm.get(os.path.normpath(answer).lower())
        if resolved is None:
            # The model sometimes returns just the folder NAME (e.g. "images")
            # instead of the full path — match on basename. Unique match wins;
            # if several share the name, prefer a Desktop one, else give up.
            ans_base = os.path.basename(answer.rstrip("/\\")).lower()
            if ans_base:
                matches = [p for p in candidate_paths
                           if os.path.basename(p.rstrip("/\\")).lower() == ans_base]
                if len(matches) == 1:
                    resolved = matches[0]
                elif len(matches) > 1:
                    desk = [p for p in matches if "desktop" in p.lower()]
                    resolved = desk[0] if len(desk) == 1 else None
        if resolved and _only_action_words_match(instruction, resolved):
            logger.info(f"[VOICE ORGANIZE] folder resolve: {instruction!r} -> {resolved!r} matched only on an "
                        "action word: not a match")
            return None
        logger.info(f"[VOICE ORGANIZE] folder resolve: {instruction!r} -> {resolved!r} (raw={answer[:80]!r})")
        return resolved
    except Exception as e:
        logger.error(f"[VOICE ORGANIZE] resolve_folder_with_ai failed: {e}")
        return None


def _request_ollama(user_message: str) -> Optional[Dict[str, Any]]:
    """Request plan via local Ollama."""
    import requests
    from .vision import OLLAMA_URL, get_local_model, _ollama_is_alive
    
    if not _ollama_is_alive():
        logger.warning("Ollama not running")
        return None
    
    payload = {
        "model": get_local_model(),
        "prompt": SYSTEM_PROMPT + "\n\n" + user_message,
        "stream": False,
        "temperature": 0.1,
    }
    
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=180)
        if r.ok:
            content = r.json().get("response", "")
            logger.info(f"Ollama organization response (truncated): {content[:300]}")
            return _parse_json(content)
    except Exception as e:
        logger.error(f"Ollama organization request failed: {e}")
    return None


def _parse_json(content: str) -> Optional[Dict[str, Any]]:
    """Parse JSON from LLM response, handling markdown wrapping."""
    # Try direct parse first
    try:
        return json.loads(content)
    except:
        pass
    
    # Try extracting JSON from markdown code block
    if "```" in content:
        import re
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except:
                pass
    
    # Try finding JSON object in the content
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(content[start:end+1])
        except:
            pass
    
    logger.error("Failed to parse JSON from LLM response")
    return None


def deduplicate_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove duplicate file_ids from the plan.
    If a file appears in multiple folders, keep only the first occurrence.
    This handles cases where the AI mistakenly puts the same file in multiple folders.
    """
    if not plan or "folders" not in plan:
        return plan
    
    seen_ids = set()
    duplicates_removed = 0
    cleaned_folders = {}
    
    for folder_name, file_ids in plan.get("folders", {}).items():
        if not isinstance(file_ids, list):
            continue
        
        cleaned_ids = []
        for fid in file_ids:
            try:
                fid_int = int(fid)
                if fid_int not in seen_ids:
                    seen_ids.add(fid_int)
                    cleaned_ids.append(fid_int)
                else:
                    duplicates_removed += 1
                    logger.debug(f"Removed duplicate file_id {fid_int} from folder '{folder_name}'")
            except (TypeError, ValueError):
                # Keep invalid IDs for validation to catch
                cleaned_ids.append(fid)
        
        if cleaned_ids:
            cleaned_folders[folder_name] = cleaned_ids
    
    if duplicates_removed > 0:
        logger.warning(f"Removed {duplicates_removed} duplicate file_id(s) from AI plan")
    
    return {"folders": cleaned_folders}


# Category words that mean "everything else" — used to find the catch-all
# folder from the user's instruction (by the CATEGORY word, so the folder can
# be named anything: "elves", "Bob", "random junk", …).
_CATCHALL_CATS = {
    "else", "everything", "other", "rest", "remaining", "remainder",
    "misc", "general", "leftover", "anything", "whatever", "unsorted",
}


def catchall_folder_from_instruction(instruction: str) -> Optional[str]:
    """Return the folder the user designated for 'everything else', parsed from
    their instruction (e.g. '…and everything else in a folder called elves' ->
    'elves'). Matched on the CATEGORY word, so the folder name is unrestricted.
    Returns None if the instruction names no catch-all."""
    try:
        cat_map = _extract_category_to_folder_map(instruction or "")
    except Exception:
        return None
    for cat, folder in cat_map.items():
        if _stem_category(str(cat)) in _CATCHALL_CATS or str(cat).lower() in _CATCHALL_CATS:
            return folder
    return None


def reconcile_plan(
    plan: Dict[str, Any],
    files_by_id: Dict[int, Dict[str, Any]],
    previous_plan: Optional[Dict[str, Any]] = None,
    catchall_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Enforce the plan invariants in CODE (never trust the AI to get them
    right). This is the deterministic reconciliation shared by generate + refine:

      #1 Conservation — every known file ends up in EXACTLY ONE folder, or is
         deliberately left in place. Nothing dropped, nothing duplicated.
      #2 New-wins   — where this plan and the previous one disagree, THIS plan
         wins. The previous plan is used ONLY to fill in files this plan forgot
         (so a sloppy/partial AI refine never loses a file).
      #3 Catch-all  — files still unplaced go to the user's 'everything else'
         folder (passed by name) when one was given.

    Returns a cleaned plan {"folders": {name: [int ids]}} with empty folders
    removed and folder order preserved.
    """
    if not isinstance(plan, dict):
        plan = {}
    folders_in = plan.get("folders") or {}

    valid_ids = set()
    for k in (files_by_id or {}).keys():
        try:
            valid_ids.add(int(k))
        except (TypeError, ValueError):
            pass

    # Previous assignment: file_id -> folder (for new-wins tie-break + drop recovery)
    prev_assign: Dict[int, str] = {}
    if isinstance(previous_plan, dict):
        for fname, ids in (previous_plan.get("folders") or {}).items():
            for fid in (ids or []):
                try:
                    prev_assign.setdefault(int(fid), fname)
                except (TypeError, ValueError):
                    pass

    # Collect, in plan order, every (valid) folder this file was placed in.
    ordered_folders: List[str] = []
    placed: Dict[int, List[str]] = {}
    for fname, ids in folders_in.items():
        if fname not in ordered_folders:
            ordered_folders.append(fname)
        for fid in (ids or []):
            try:
                fid = int(fid)
            except (TypeError, ValueError):
                continue
            if fid not in valid_ids:
                continue  # drop hallucinated id
            lst = placed.setdefault(fid, [])
            if fname not in lst:
                lst.append(fname)

    # #1/#2: resolve duplicates — the NEW placement wins over the file's PREVIOUS folder.
    final: Dict[int, str] = {}
    for fid, flist in placed.items():
        if len(flist) == 1:
            final[fid] = flist[0]
        else:
            prev = prev_assign.get(fid)
            news = [f for f in flist if f != prev]   # folders that aren't the stale one
            final[fid] = news[-1] if news else flist[-1]

    # #1/#3: recover files THIS plan forgot (keep previous folder, else catch-all).
    recovered = 0
    for fid in valid_ids:
        if fid in final:
            continue
        if fid in prev_assign:
            final[fid] = prev_assign[fid]
            recovered += 1
        elif catchall_folder:
            final[fid] = catchall_folder
            recovered += 1
        # else: no prior home and no catch-all -> leave in place (no move)

    # Rebuild, preserving folder order and appending any carried-forward folders.
    out: Dict[str, List[int]] = {}
    for fname in ordered_folders:
        out.setdefault(fname, [])
    if catchall_folder:
        out.setdefault(catchall_folder, [])
    for fid, fname in final.items():
        out.setdefault(fname, [])
    for fid in sorted(final.keys()):
        out[final[fid]].append(fid)
    out = {k: v for k, v in out.items() if v}   # drop empties

    if recovered:
        logger.info(f"[reconcile] recovered {recovered} file(s) the AI left unplaced")
    return {"folders": out}


def ensure_all_files_included(
    plan: Dict[str, Any],
    all_file_ids: set,
    files_info: List[Dict[str, Any]] = None,
    existing_folders_only: bool = False,
) -> Dict[str, Any]:
    """
    Ensure all provided file IDs are included in the plan.

    If the AI missed any files, place them in the most relevant existing folder
    based on each file's metadata. This prevents files from being left
    unorganized and avoids inventing a generic 'misc' folder when real folders
    already exist in the plan.

    Args:
        plan: The organization plan from AI
        all_file_ids: Set of all file IDs that should be in the plan
        files_info: Optional list of file info dicts for better folder selection
        existing_folders_only: When True (As-Is mode), do NOT fill omitted files
            into best-guess existing folders. As-Is honours the AI's omission
            as "leave the file in place." The worker reports the count to the
            user. Without this, the per-file scoring inside this function
            silently dumps every orphan into the highest-scoring existing
            folder — exactly the failure mode the bears+lions bug surfaced.

    Returns:
        Updated plan. In As-Is mode, omitted files stay omitted and the worker
        leaves them in place on disk.
    """
    if not plan or "folders" not in plan:
        plan = {"folders": {}}

    # Collect all file IDs currently in the plan
    included_ids = set()
    for folder_name, file_ids in plan.get("folders", {}).items():
        for fid in file_ids:
            try:
                included_ids.add(int(fid))
            except (TypeError, ValueError):
                pass

    # Find missing file IDs
    missing_ids = set(all_file_ids) - included_ids     # (a list works too — it used to crash: "list - set")

    if not missing_ids:
        return plan  # All files are included

    if existing_folders_only:
        # As-Is contract: never invent a destination. If the AI omitted these
        # files we honour that — the worker leaves them on disk where they
        # currently sit. Skip the force-placement logic below entirely.
        logger.info(
            f"[As-Is] AI plan omitted {len(missing_ids)} file(s); "
            f"leaving them in place on disk"
        )
        return plan

    logger.warning(f"AI plan missing {len(missing_ids)} file(s). Adding them to existing folders.")

    # Log which files are missing for debugging
    if files_info:
        missing_names = []
        for f in files_info:
            try:
                if int(f.get('id', 0)) in missing_ids:
                    missing_names.append(f.get('file_name', 'unknown'))
            except (TypeError, ValueError):
                pass
        if missing_names:
            logger.info(f"Missing files: {', '.join(missing_names[:10])}")

    folders = plan.get("folders", {})

    # If real folders already exist in the plan, route each missing file to the
    # MOST RELEVANT one based on its own metadata (name/ext/label/category/tags)
    # rather than inventing a generic 'misc' folder or dumping into the first
    # folder. A 'misc' folder is created only as a last resort when the plan
    # has no folders at all.
    if folders:
        info_by_id = {}
        if files_info:
            for f in files_info:
                try:
                    info_by_id[int(f.get('id', 0))] = f
                except (TypeError, ValueError):
                    pass
        folder_names = list(folders.keys())
        for missing_id in missing_ids:
            target = _best_folder_for_file(info_by_id.get(missing_id), folder_names)
            folders[target].append(missing_id)
            logger.info(f"Added missing file id {missing_id} to most relevant folder '{target}'")
    else:
        # No folders at all — create a misc folder as the last resort.
        folders['misc'] = list(missing_ids)
        logger.info(f"Added {len(missing_ids)} missing file(s) to 'misc' folder (no existing folders)")

    return {"folders": folders}


def _best_folder_for_file(file_info: Optional[Dict[str, Any]], folder_names: List[str]) -> str:
    """Pick the existing folder whose name best matches a file's metadata.

    Scoring strategy:
    1. Tags / filename / label tokens that overlap a folder name (substring
       either way) score 1.0 — the strongest signal.
    2. Otherwise use difflib similarity.
    3. If the best non-catch-all match is weak (no strong substring hit), and
       a catch-all-style folder exists (name contains 'everything', 'other',
       'misc', 'general', 'rest', 'else'), prefer the catch-all. This
       prevents a bear photo from being shoved into 'lions' just because of
       random character overlap when an 'everything-else' folder is sitting
       right there for exactly this case.
    4. Falls back to the first folder when no signal is available, and to
       'misc' when there are no folders at all.
    """
    if not folder_names:
        return 'misc'
    if not file_info:
        return folder_names[0]

    import difflib

    catchall_keywords = {'everything', 'other', 'misc', 'general', 'rest', 'else', 'miscellaneous', 'various', 'unsorted'}

    def _is_catchall(folder: str) -> bool:
        # Only the leaf folder name counts — 'everything-else/pigs' is NOT a
        # catch-all (the LEAF is 'pigs'), even though its parent name contains
        # 'else'. We use forward slash because the helper that builds nested
        # folder lists also uses forward slashes.
        leaf = folder.rsplit('/', 1)[-1].lower()
        return any(k in leaf for k in catchall_keywords)

    # Build descriptive terms for the file from its metadata
    terms = []
    name = file_info.get('file_name', '') or ''
    if name:
        stem = name.rsplit('.', 1)[0]
        ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
        terms.extend(part for part in stem.replace('_', ' ').replace('-', ' ').split() if part)
        if ext:
            terms.append(ext)
    if file_info.get('label'):
        terms.append(str(file_info['label']))
    if file_info.get('category'):
        terms.append(str(file_info['category']))
    for tag in (file_info.get('tags') or []):
        terms.append(str(tag))
    terms = [t.lower() for t in terms if t]

    best_folder = folder_names[0]
    best_score = 0.0
    had_strong_hit = False
    for folder in folder_names:
        folder_lc = folder.lower()
        # Don't let a catch-all folder win on fuzzy similarity alone — only via
        # an actual substring hit. Otherwise short generic names ("Other")
        # would be picked accidentally.
        if _is_catchall(folder):
            continue
        score = 0.0
        for term in terms:
            if term and (term in folder_lc or folder_lc in term):
                score = max(score, 1.0)
            else:
                score = max(score, difflib.SequenceMatcher(None, term, folder_lc).ratio())
        if score >= 0.85:
            had_strong_hit = True
        if score > best_score:
            best_score = score
            best_folder = folder

    # If no real signal pointed at a specific folder, prefer a catch-all if one
    # exists — that's the whole point of the catch-all folder.
    if not had_strong_hit:
        for folder in folder_names:
            if _is_catchall(folder):
                return folder

    return best_folder


# ─────────────────────────────────────────────────────────────
# VALIDATION (MANDATORY - App is the final authority)
# ─────────────────────────────────────────────────────────────

def validate_plan(
    plan: Dict[str, Any],
    valid_file_ids: set,
    max_depth: int = 2
) -> Tuple[bool, List[str]]:
    """
    Validate the organization plan for safety.
    
    This is the critical safety gate - the app validates everything
    before any file operation occurs.
    
    Checks:
    - All file_ids exist in our database
    - No duplicates across folders
    - Folder depth is limited
    - No system/root folders touched
    - No path traversal attacks
    
    Returns: (is_valid, list_of_errors)
    """
    errors = []
    
    if not plan:
        errors.append("Plan is empty")
        return False, errors
    
    folders = plan.get("folders")
    if not folders or not isinstance(folders, dict):
        errors.append("Plan must contain 'folders' dict")
        return False, errors
    
    seen_ids = set()
    
    for folder_name, file_ids in folders.items():
        # Safety checks on folder name
        if not folder_name or not isinstance(folder_name, str):
            errors.append(f"Invalid folder name: {folder_name}")
            continue
        
        # Prevent path traversal
        if ".." in folder_name:
            errors.append(f"Path traversal not allowed: {folder_name}")
            continue
        
        # Prevent absolute paths
        if folder_name.startswith("/") or folder_name.startswith("\\"):
            errors.append(f"Absolute paths not allowed: {folder_name}")
            continue
        
        # Windows drive letters
        if ":" in folder_name:
            errors.append(f"Drive letters not allowed: {folder_name}")
            continue
        
        # Check for system folder names
        dangerous_names = {'system32', 'windows', 'program files', 'programdata', '$recycle.bin'}
        if folder_name.lower() in dangerous_names:
            errors.append(f"System folder name not allowed: {folder_name}")
            continue
        
        # Check depth
        depth = folder_name.replace("\\", "/").count("/") + 1
        if depth > max_depth:
            errors.append(f"Folder too deep ({depth} > {max_depth}): {folder_name}")
        
        # Validate file IDs
        if not isinstance(file_ids, list):
            errors.append(f"Folder '{folder_name}' must have list of file IDs")
            continue
        
        for fid in file_ids:
            # Ensure fid is an integer
            try:
                fid_int = int(fid)
            except (TypeError, ValueError):
                errors.append(f"Invalid file_id type: {fid}")
                continue
            
            if fid_int not in valid_file_ids:
                errors.append(f"Unknown file_id: {fid_int}")
            elif fid_int in seen_ids:
                errors.append(f"Duplicate file_id: {fid_int} (appears in multiple folders)")
            seen_ids.add(fid_int)
    
    return len(errors) == 0, errors


# ─────────────────────────────────────────────────────────────
# CONVERT PLAN TO MOVE OPERATIONS
# ─────────────────────────────────────────────────────────────

def _sanitize_folder_name(folder_name: str, destination_root: Path) -> str:
    """Flatten an AI-proposed folder name to a SINGLE level — the last path
    COMPONENT, split on '/' and '\\' (not the last word). Voice-organize folders
    are always flat, so any stray prefix the AI (or a garbled transcript) adds is
    dropped, no matter what it is:

        'images/animals'            -> 'animals'
        'Filect/everything else'    -> 'everything else'   (whole component, not 'else')
        'images/images/animals'     -> 'animals'
        'a/b/c/d'                   -> 'd'
        'animals'                   -> 'animals'

    A component equal to the target folder's own name is dropped (a category
    named the same as the folder being organized would just nest it).
    Returns '' if nothing valid remains (caller skips it)."""
    import re
    parts = [p.strip() for p in re.split(r'[/\\]+', str(folder_name or '').strip())]
    parts = [p for p in parts if p and p not in ('.', '..')]
    if not parts:
        return ''
    leaf = parts[-1]
    if leaf.lower() == destination_root.name.lower():
        return ''
    return leaf


def plan_to_moves(
    plan: Dict[str, Any],
    files_by_id: Dict[int, Dict[str, Any]],
    destination_root: Path
) -> List[Dict[str, Any]]:
    """
    Convert validated plan to concrete move operations.

    This is deterministic - no AI involved here.
    The app fully controls what actually happens.
    """
    moves = []
    skipped_not_found = 0
    skipped_no_info = 0
    skipped_already_in_dest = 0
    skipped_bad_folder = 0

    for raw_folder_name, file_ids in plan.get("folders", {}).items():
        folder_name = _sanitize_folder_name(raw_folder_name, destination_root)
        if not folder_name:
            skipped_bad_folder += 1
            logger.warning(f"plan_to_moves: dropping invalid folder name {raw_folder_name!r}")
            continue
        if folder_name != str(raw_folder_name):
            logger.info(f"plan_to_moves: sanitized folder {raw_folder_name!r} -> {folder_name!r}")
        dest_folder = destination_root / folder_name

        for fid in file_ids:
            # Normalize fid to int
            try:
                fid_int = int(fid)
            except (TypeError, ValueError):
                logger.warning(f"Invalid file ID type: {fid}")
                continue
            
            file_info = files_by_id.get(fid_int)
            if not file_info:
                skipped_no_info += 1
                logger.debug(f"No file info for ID {fid_int}")
                continue
            
            source_path = Path(file_info['file_path'])
            if not source_path.exists():
                skipped_not_found += 1
                logger.debug(f"Source file doesn't exist: {source_path}")
                continue
            
            dest_path = dest_folder / source_path.name
            
            # Skip files that are already in the destination folder
            # This prevents "moving" files to where they already are
            if source_path.parent.resolve() == dest_folder.resolve():
                skipped_already_in_dest += 1
                logger.debug(f"Skipping {source_path.name} - already in destination folder {dest_folder}")
                continue
            
            # Also skip if the exact destination file already exists and is the same file
            if dest_path.exists() and source_path.resolve() == dest_path.resolve():
                skipped_already_in_dest += 1
                logger.debug(f"Skipping {source_path.name} - source and destination are the same file")
                continue
            
            # Handle collisions by adding numeric suffix (only for different files)
            counter = 1
            original_stem = source_path.stem
            original_suffix = source_path.suffix
            while dest_path.exists():
                dest_path = dest_folder / f"{original_stem} ({counter}){original_suffix}"
                counter += 1
            
            moves.append({
                "file_id": fid_int,
                "file_name": source_path.name,
                "source_path": str(source_path),
                "destination_path": str(dest_path),
                "destination_folder": folder_name,
                "size": file_info.get('file_size', 0),
            })
    
    # Log summary
    total_in_plan = sum(len(fids) for fids in plan.get("folders", {}).values())
    logger.info(f"plan_to_moves: {len(moves)} valid moves from {total_in_plan} files in plan. "
                f"Skipped: {skipped_not_found} not found, {skipped_no_info} no info, "
                f"{skipped_already_in_dest} already in destination")
    
    return moves


# ─────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────

def get_plan_summary(plan: Dict[str, Any], files_by_id: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Get a human-readable summary of the organization plan.
    """
    folders = plan.get("folders", {})
    
    total_files = sum(len(fids) for fids in folders.values())
    total_size = 0
    
    folder_summaries = []
    for folder_name, file_ids in folders.items():
        folder_size = 0
        for fid in file_ids:
            try:
                fid_int = int(fid)
                file_info = files_by_id.get(fid_int, {})
                folder_size += file_info.get('file_size', 0)
            except:
                pass
        total_size += folder_size
        
        folder_summaries.append({
            "name": folder_name,
            "file_count": len(file_ids),
            "size_bytes": folder_size,
            "size_mb": round(folder_size / (1024 * 1024), 2),
        })
    
    return {
        "total_folders": len(folders),
        "total_files": total_files,
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "folders": folder_summaries,
    }
