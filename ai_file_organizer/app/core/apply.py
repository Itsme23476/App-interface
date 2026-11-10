"""
Move application and execution logic.
"""

import hashlib
import json
import logging
import os
import shutil
import stat
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional, Callable
from .settings import settings


logger = logging.getLogger(__name__)


def _nc(p) -> str:
    """A path as Windows compares it: no case, one kind of slash."""
    return os.path.normcase(os.path.normpath(str(p)))


def _get_unique_path(dest_path: Path) -> Path:
    """
    Generate a unique file path by adding (1), (2), etc. if file exists.
    
    Example: document.pdf → document (1).pdf → document (2).pdf
    
    Args:
        dest_path: The desired destination path
        
    Returns:
        A unique path that doesn't exist yet
    """
    if not dest_path.exists():
        return dest_path
    
    stem = dest_path.stem  # filename without extension
    suffix = dest_path.suffix  # .pdf, .jpg, etc.
    parent = dest_path.parent
    
    counter = 1
    while True:
        new_name = f"{stem} ({counter}){suffix}"
        new_path = parent / new_name
        if not new_path.exists():
            logger.info(f"Duplicate detected: {dest_path.name} → {new_name}")
            return new_path
        counter += 1
        if counter > 1000:  # Safety limit
            raise ValueError(f"Could not find unique name for {dest_path.name} after 1000 attempts")


def _clear_file_blocking_dir(dir_path: Path, move_log: Dict[str, Any],
                             move_plan: List[Dict[str, Any]] = ()) -> int:
    """
    mkdir(parents=True) fails if any path that must become a directory already
    exists as a FILE (e.g. a file literally named "images" blocks the "images/"
    destination folder). Walk dir_path's ancestor chain and rename any such file
    out of the way to a safe unique name ("images" -> "images (file)", then
    "images (file) (1)", ...), so the directory can be created without data loss.
    If that file was due to move later in the same plan, the plan follows it to
    its new name (it used to move the freshly made folder instead, with its contents).

    Windows: names are case-insensitive, so a file "money" blocks a "Money" folder
    too; it keeps its own spelling ("money (file)").

    Returns the number of files renamed.
    """
    renamed = 0
    # Shallowest ancestor first: once a blocker is renamed, deeper paths that
    # were nested under it no longer exist as files.
    for ancestor in reversed([dir_path, *dir_path.parents]):
        if ancestor.is_file():
            real = ancestor.name
            try:                                # (its real spelling on disk)
                real = next((n for n in os.listdir(ancestor.parent) if os.path.normcase(n) == os.path.normcase(real)),
                            real)
            except OSError:
                pass
            blocker = ancestor.parent / real
            safe = _get_unique_path(ancestor.parent / f"{real} (file)")
            blocker.rename(safe)
            renamed += 1
            for later in move_plan:             # that file may be due to move too: it's under its new name now
                if later.get("source_path") and _nc(later["source_path"]) == _nc(blocker):
                    later["source_path"] = str(safe)
            move_log["renamed_files"].append({
                "original_name": real,
                "new_name": safe.name,
                "folder": str(ancestor.parent),
                "reason": "file name collided with a destination folder"
            })
            logger.info(f"Renamed file blocking destination folder: {blocker} → {safe.name}")
    return renamed


def apply_moves(move_plan: List[Dict[str, Any]],
                progress_cb: Optional[Callable[[int, int], None]] = None) -> Tuple[bool, List[str], str, int]:
    """
    Apply the move plan to actually move files.

    Handles duplicate files by auto-renaming (e.g., file.pdf → file (1).pdf).

    Args:
        move_plan: List of move plan dictionaries
        progress_cb: Optional callback ``progress_cb(done, total)`` fired after
            each file (in a ``finally`` so one bad move never stalls progress).
            When ``None`` (the default) behaviour is identical to before.

    Returns:
        Tuple of (success, list_of_errors, log_file_path, renamed_count)
    """
    errors = []
    successful_moves = []
    _total = len(move_plan)
    
    # Create move log entry
    move_log = {
        "timestamp": datetime.now().isoformat(),
        "total_files": len(move_plan),
        "moves": [],
        "renamed_files": []  # Track files that were auto-renamed
    }
    renamed_count = 0
    
    try:
        for i, move in enumerate(move_plan):
            try:
                source_path = Path(move['source_path'])
                dest_path = Path(move['destination_path'])
                
                # Ensure source still exists
                if not source_path.exists():
                    if dest_path.exists():
                        # File already reached its destination — treat as success
                        successful_moves.append(move)
                        logger.debug(f"Already at destination, counting as success: {source_path.name}")
                    else:
                        error_msg = f"Source file no longer exists: {source_path}"
                        errors.append(error_msg)
                        logger.error(error_msg)
                    continue
                
                # Create destination directory if needed. A destination FOLDER
                # can collide with an existing FILE of the same name; rename the
                # blocking file out of the way first so mkdir can't fail.
                renamed_count += _clear_file_blocking_dir(dest_path.parent, move_log, move_plan[i + 1:])
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Handle duplicate files - auto-rename if destination already exists
                original_dest = dest_path
                if dest_path.exists():
                    dest_path = _get_unique_path(dest_path)
                    # Update the move entry with new destination path
                    move['destination_path'] = str(dest_path)
                    renamed_count += 1
                    move_log["renamed_files"].append({
                        "original_name": original_dest.name,
                        "new_name": dest_path.name,
                        "folder": str(dest_path.parent)
                    })
                
                # Move the file
                shutil.move(str(source_path), str(dest_path))
                
                # Log successful move
                move_entry = {
                    "from": str(source_path.absolute()),
                    "to": str(dest_path.absolute()),
                    "timestamp": datetime.now().isoformat()
                }
                move_log["moves"].append(move_entry)
                successful_moves.append(move)
                
                logger.debug(f"Moved {source_path.name} to {dest_path}")

            except Exception as e:
                error_msg = f"Error moving {move.get('file_name', 'unknown')}: {e}"
                errors.append(error_msg)
                logger.error(error_msg)
                continue
            finally:
                # Fire progress even when the iteration ends via continue/raise,
                # so a single bad move never stalls the progress bar.
                if progress_cb is not None:
                    try:
                        progress_cb(i + 1, _total)
                    except Exception:
                        pass
        
        # Save move log
        log_file_path = _save_move_log(move_log)
        
        success = len(errors) == 0
        renamed_msg = f", {renamed_count} renamed to avoid duplicates" if renamed_count > 0 else ""
        logger.info(f"Move operation completed. {len(successful_moves)} successful{renamed_msg}, {len(errors)} errors")
        
        return success, errors, log_file_path, renamed_count
        
    except Exception as e:
        error_msg = f"Critical error during move operation: {e}"
        errors.append(error_msg)
        logger.error(error_msg)
        return False, errors, "", 0


def _save_move_log(move_log: Dict[str, Any]) -> str:
    """
    Save move log to JSON file.
    
    Args:
        move_log: Move log dictionary
        
    Returns:
        Path to saved log file
    """
    try:
        moves_dir = settings.get_moves_dir()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_filename = f"moves-{timestamp}.json"
        log_file_path = moves_dir / log_filename
        
        with open(log_file_path, 'w', encoding='utf-8') as f:
            json.dump(move_log, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Move log saved to: {log_file_path}")
        return str(log_file_path)
        
    except Exception as e:
        logger.error(f"Error saving move log: {e}")
        return ""


_auto_log_lock = threading.Lock()


def _auto_log_path(folder: str, now: datetime) -> Path:
    """Today's open History entry for this auto-organized folder. Named to sort before that day's manual
    entries. Once it has been reverted, the next automatic move starts a new one. (Windows: the folder is hashed
    without case, so C:\\Users\\x\\Downloads and c:\\users\\x\\downloads share one entry.)"""
    digest = hashlib.sha1(_nc(folder).encode("utf-8")).hexdigest()[:8]
    moves_dir = settings.get_moves_dir()
    day = now.strftime("%Y%m%d")
    path = moves_dir / f"moves-{day}-000000-auto-{digest}.json"
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                if json.load(f).get("reverted"):
                    return moves_dir / f"moves-{day}-{now.strftime('%H%M%S')}-auto-{digest}.json"
        except (OSError, ValueError):
            pass
    return path


def record_auto_moves(folder: str, moves: List[Tuple[str, str]]) -> None:
    """Add what auto-organize just moved to History: one entry per folder per day, so a file-at-a-time
    feature doesn't push everything else off the list. A file that moves again the same day keeps its first
    origin, so reverting takes it straight home. Runs on the thread that moved the files; 0.2 ms at 20 moves
    a day, 17 ms at 5,000. Never raises: History must not get in the way of the move."""
    if not moves:
        return
    try:
        with _auto_log_lock:
            now = datetime.now()
            path = _auto_log_path(folder, now)
            log = {"source": "auto", "folder": os.path.normpath(folder), "moves": [], "renamed_files": []}
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    log = json.load(f)
                log.setdefault("moves", [])
            by_dest = {_nc(m["to"]): m for m in log["moves"]}
            for src, dst in moves:
                earlier = by_dest.pop(_nc(src), None)
                if earlier is not None:
                    earlier["to"] = str(dst)
                    earlier["timestamp"] = now.isoformat()
                    by_dest[_nc(dst)] = earlier
                else:
                    entry = {"from": str(src), "to": str(dst), "timestamp": now.isoformat()}
                    log["moves"].append(entry)
                    by_dest[_nc(dst)] = entry
            log["timestamp"] = now.isoformat()
            log["total_files"] = len(log["moves"])
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Couldn't save auto-organize History: {e}")


def get_move_history() -> List[Dict[str, Any]]:
    """
    Get history of move operations.
    
    Returns:
        List of move log summaries
    """
    history = []
    
    try:
        moves_dir = settings.get_moves_dir()
        
        for log_file in moves_dir.glob("moves-*.json"):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    log_data = json.load(f)
                
                history.append({
                    "log_file": str(log_file),
                    "timestamp": log_data.get("timestamp", ""),
                    "total_files": log_data.get("total_files", 0),
                    "successful_moves": len(log_data.get("moves", [])),
                    "reverted": bool(log_data.get("reverted", False)),
                    "source": log_data.get("source", ""),
                    "folder": log_data.get("folder", "")
                })
                
            except Exception as e:
                logger.error(f"Error reading log file {log_file}: {e}")
                continue
        
        # Sort by timestamp (newest first)
        history.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        return history
        
    except Exception as e:
        logger.error(f"Error getting move history: {e}")
        return []


def split_revertable(moves: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Which logged moves can still go back: (those moves, why the others can't)."""
    can, cannot = [], []
    for move in moves:
        dest_path = Path(move.get("to", ""))
        source_path = Path(move.get("from", ""))
        if not dest_path.exists():
            cannot.append(f"File not found: {dest_path.name}")
        elif source_path.exists():
            cannot.append(f"Original location occupied: {source_path.name}")
        else:
            can.append(move)
    return can, cannot


# What a folder may still hold and count as empty: the Mac's Finder settings file, and Windows Explorer's
# own files (its folder settings and its picture cache). Compared without case.
_IGNORABLE = {".ds_store", "desktop.ini", "thumbs.db"}


def _prune_empty(starts, keep) -> None:
    """Remove these folders, and any parent that becomes empty, if nothing but .DS_Store / desktop.ini /
    Thumbs.db is in them. Never a folder in `keep` (compared as Windows does: no case)."""
    keep = {_nc(k) for k in keep}
    for start in sorted(starts, key=lambda d: len(Path(d).parts), reverse=True):
        d = Path(start)
        while _nc(d) not in keep:
            try:
                names = os.listdir(d)
                if any(n.lower() not in _IGNORABLE for n in names):
                    break
                for n in names:
                    junk = d / n
                    try:
                        os.chmod(junk, stat.S_IWRITE)      # (desktop.ini is often read-only / system)
                    except OSError:
                        pass
                    junk.unlink()
                try:
                    d.rmdir()
                except PermissionError:
                    os.chmod(d, stat.S_IWRITE)             # (a customized folder can be marked read-only)
                    d.rmdir()
            except OSError:
                break
            if d.parent == d:
                break
            d = d.parent


def _remove_emptied_folders(moves: List[Dict[str, Any]]) -> None:
    """After a revert: drop the folders the organize made, if nothing else is in them. Never a folder a file
    just went back to, or an ancestor of one."""
    homes = {Path(m["from"]).parent for m in moves}
    _prune_empty({Path(m["to"]).parent for m in moves}, homes | {a for h in homes for a in h.parents})


def tidy_emptied_sources(log_file: str, root: str) -> None:
    """Once Undo is no longer offered: remove the folders that organize emptied (never `root` or above).
    They're kept until then because on an iCloud Desktop, deleting a folder and re-creating it seconds later makes
    iCloud rename it "name 2". (Windows: kept the same way; whether OneDrive does this too isn't verified.)"""
    with open(log_file, 'r', encoding='utf-8') as f:
        log = json.load(f)
    if log.get("reverted"):
        return
    top = Path(root)
    _prune_empty({Path(m["from"]).parent for m in log.get("moves", [])}, {top, *top.parents})


def revert_moves(log_file: str, moves: List[Dict[str, Any]]) -> Tuple[int, List[str]]:
    """Move these logged files back where they were, keep the index right, and mark the log as reverted so
    History shows it. `moves` should come from split_revertable. Returns (files moved back, errors)."""
    reverted = 0
    errors = []
    done = []
    for move in moves:
        dest_path = Path(move.get("to", ""))
        source_path = Path(move.get("from", ""))
        try:
            source_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest_path), str(source_path))
            reverted += 1
            done.append(move)
            logger.info(f"[REVERT] Moved back: {dest_path.name} -> {source_path.parent.name}/")
            try:
                from .database import file_index
                if file_index.update_file_path_by_old_path(str(dest_path), str(source_path)):
                    logger.info(f"[REVERT] DB path updated: {dest_path.name}")
                else:
                    logger.debug("[REVERT] DB path update skipped (file not in DB)")
            except Exception as db_err:
                logger.warning(f"[REVERT] DB path update failed: {db_err}")
        except Exception as e:
            errors.append(f"{dest_path.name}: {str(e)}")
    try:
        _remove_emptied_folders(done)
    except Exception as e:
        logger.warning(f"[REVERT] Couldn't tidy emptied folders: {e}")
    if done:
        # Put back into a folder auto-organize watches, they'd only be sorted again.
        try:
            settings.mark_auto_organize_skip_known([m["from"] for m in done])
        except Exception as e:
            logger.warning(f"[REVERT] Couldn't mark auto-organize folders: {e}")
    if reverted > 0 and log_file:
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                log_data = json.load(f)
            log_data['reverted'] = True
            log_data['reverted_count'] = reverted
            log_data['reverted_at'] = datetime.now().isoformat()
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
            logger.info(f"[REVERT] Marked log as reverted: {log_file}")
        except Exception as e:
            logger.warning(f"[REVERT] Could not mark log as reverted: {e}")
    return reverted, errors


def undo_log(log_file: str) -> Dict[str, Any]:
    """Put back everything one organize moved. {"reverted", "skipped", "errors"}."""
    with open(log_file, 'r', encoding='utf-8') as f:
        moves = json.load(f).get("moves", [])
    can, cannot = split_revertable(moves)
    reverted, errors = revert_moves(log_file, can)
    return {"reverted": reverted, "skipped": len(cannot), "errors": errors}


def validate_destination_space(move_plan: List[Dict[str, Any]], 
                             destination_root: Path) -> Tuple[bool, str]:
    """
    Validate that there's enough space in destination.
    
    Args:
        move_plan: List of move plan dictionaries
        destination_root: Destination directory root
        
    Returns:
        Tuple of (has_enough_space, error_message)
    """
    try:
        # Calculate required space
        required_space = sum(move.get('size', 0) for move in move_plan)
        
        # Get available space on destination drive
        total, used, free = shutil.disk_usage(destination_root)
        
        if required_space > free:
            required_mb = round(required_space / (1024 * 1024), 2)
            free_mb = round(free / (1024 * 1024), 2)
            return False, f"Insufficient space. Required: {required_mb}MB, Available: {free_mb}MB"
        
        return True, ""
        
    except Exception as e:
        return False, f"Error checking disk space: {e}"


