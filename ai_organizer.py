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

SYSTEM_PROMPT = f"""You are a file organization assistant. Propose how to organize files into folders based on user instructions.

OUTPUT FORMAT - Return ONLY this JSON structure:
{ORGANIZATION_SCHEMA}

CRITICAL ID RULE:
- Each file I give you has a numeric ID shown as "id:NUMBER"
- You MUST use the EXACT same number in your response
- Example: If I show "id:38 | interface.jpg", you return {{"folders": {{"interfaces": [38]}}}}
- NEVER invent IDs - only use the numbers I provide

FOLDER NAMING:
- Use lowercase, kebab-case (e.g., "interfaces", "client-invoices", "2024-receipts")
- Create the folder name the user asks for, or a descriptive name based on content
- Maximum 2 levels deep (e.g., "clients/acme" ok, "a/b/c" not ok)

FILE SELECTION:
- ONLY include files that match the user's instruction
- Leave non-matching files OUT of the response entirely
- It's OK to return empty {{"folders": {{}}}} if nothing matches
- Do NOT create "unsorted" or "other" folders unless asked

EXAMPLE:
User: "Put interface files in an interfaces folder"
Files: id:38 | interface.jpg | tags:[ui, mockup]
       id:39 | receipt.pdf | tags:[finance]
Response: {{"folders": {{"interfaces": [38]}}}}
(Note: receipt.pdf is NOT included because it doesn't match)

JSON only. No markdown. No explanation."""


# ─────────────────────────────────────────────────────────────
# FILE SUMMARY FOR LLM
# ─────────────────────────────────────────────────────────────

def build_file_summary(files: List[Dict[str, Any]], max_files: int = 300) -> str:
    """
    Create a compact summary of files for the LLM context.
    Limits tokens while preserving key metadata.
    """
    lines = []
    for f in files[:max_files]:
        fid = f.get('id')
        name = f.get('file_name', 'unknown')[:50]
        label = f.get('label', '') or ''
        caption = (f.get('caption', '') or '')[:80]
        tags = f.get('tags', []) or []
        tags_str = ', '.join(tags[:8]) if tags else ''
        
        line = f"id:{fid} | {name} | label:{label} | tags:[{tags_str}]"
        if caption:
            line += f" | caption:{caption}"
        lines.append(line)
    
    summary = "\n".join(lines)
    
    if len(files) > max_files:
        summary += f"\n... and {len(files) - max_files} more files"
    
    return summary


# ─────────────────────────────────────────────────────────────
# LLM REQUEST
# ─────────────────────────────────────────────────────────────

def request_organization_plan(
    user_instruction: str,
    files: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Send user instruction + file metadata to LLM.
    Returns the proposed plan as a dict, or None on failure.
    
    The LLM acts only as a planner - it never executes anything.
    """
    from .settings import settings
    
    if not files:
        logger.warning("No files provided for organization")
        return None
    
    file_summary = build_file_summary(files)
    
    # Detect auto-organize mode vs specific instruction mode
    is_auto_organize = user_instruction.startswith("[AUTO-ORGANIZE]")
    
    if is_auto_organize:
        # Auto-organize: MUST include ALL files
        user_message = f"""User instruction: "{user_instruction}"

Files to organize ({len(files)} total):
{file_summary}

CRITICAL OVERRIDE FOR AUTO-ORGANIZE:
- You MUST include EVERY file_id in your response
- Each file_id must appear in exactly ONE folder
- Do NOT skip any files
- If a file doesn't fit a category, put it in 'misc' or 'other'
- Total files in your response must equal {len(files)}

Propose an organization plan. Return JSON only."""
    else:
        # Specific instruction: only organize matching files
        user_message = f"""User instruction: "{user_instruction}"

Files to organize ({len(files)} total):
{file_summary}

REMEMBER: Use ONLY the exact numeric IDs shown above (the number after "id:"). Do NOT invent IDs!

Propose an organization plan. Return JSON only."""

    # Log what we're sending to AI
    logger.info(f"Sending to AI - instruction: '{user_instruction[:100]}...'")
    logger.info(f"File summary being sent:\n{file_summary[:500]}")
    
    provider = settings.ai_provider
    
    if provider == 'openai':
        return _request_openai(user_message)
    elif provider == 'local':
        return _request_ollama(user_message)
    else:
        logger.warning("No AI provider configured")
        return None


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
Return the complete updated plan as JSON only."""

    provider = settings.ai_provider
    
    if provider == 'openai':
        return _request_openai(user_message)
    elif provider == 'local':
        return _request_ollama(user_message)
    else:
        logger.warning("No AI provider configured")
        return None


def _request_openai(user_message: str) -> Optional[Dict[str, Any]]:
    """Request plan via OpenAI API."""
    try:
        from openai import OpenAI
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.error("OPENAI_API_KEY not set")
            return None
        
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
            max_tokens=4000,
        )
        content = resp.choices[0].message.content or ""
        logger.info(f"OpenAI organization response (truncated): {content[:300]}")
        return _parse_json(content)
    except Exception as e:
        logger.error(f"OpenAI organization request failed: {e}")
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
    
    for folder_name, file_ids in plan.get("folders", {}).items():
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
