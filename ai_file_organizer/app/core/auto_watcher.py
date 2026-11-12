"""
Auto-organize watcher module.

Watches folders for new files and automatically organizes them using AI.
Supports per-folder instructions and catch-up mode for files added while app was closed.
"""

import json
import os
import shutil
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from collections import defaultdict

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, QTimer, QThread

from .apply import is_busy, move_file

logger = logging.getLogger(__name__)


class _Cancelled(Exception):
    """A background step whose run was stopped (stop(), or a newer start()) gives up at its next check."""


class _OffUIThread(QThread):
    """One heavy step — walking or hashing a big folder — off the UI thread. Measured on a 20,000-file folder
    (2026-11-09): turning on 'only new files' froze the window ~4.5 min, the other modes ~20 s, and the
    every-3-seconds check ~0.3-0.5 s each time, because all of it ran on the UI thread. The result comes back
    on the UI thread (done -> AutoOrganizeWatcher._on_bg_done), where the watcher's state is changed."""
    done = Signal(int, object, object)          # token, result, error

    def __init__(self, token: int, fn):
        super().__init__()
        self._token, self._fn = token, fn

    def run(self):
        try:
            result = self._fn()
        except Exception as e:                  # (_Cancelled too: the caller drops it)
            self.done.emit(self._token, None, e)
            return
        self.done.emit(self._token, result, None)


def _path_key(path: str) -> str:
    """How two paths are compared (Windows paths are case-insensitive)."""
    return os.path.normcase(os.path.normpath(path))


def _is_inside(path: str, folder: str) -> bool:
    """`path` is somewhere under `folder` (not the folder itself). Case-insensitive, like Windows."""
    if not path or not folder:
        return False
    p, f = _path_key(path), _path_key(folder)
    return p != f and p.startswith(f.rstrip("\\/") + os.sep)


def _created_at(path: str) -> Optional[float]:
    """When the file was created on this disk, or None where the system can't say.
    st_birthtime (macOS; Windows from Python 3.12), else st_ctime on Windows, which IS the creation time there.
    (The Mac code catches AttributeError and treats the file as already there: on a Python without
    st_birthtime that would make the watching_since fix silently do nothing.)"""
    st = os.stat(path)
    born = getattr(st, 'st_birthtime', None)
    if born:
        return float(born)
    if os.name == 'nt':
        return float(st.st_ctime)
    return None


def _arrived_since(path: str, since: float) -> bool:
    """True if `path` was created after `since` (when its folder was turned on)."""
    if not since:
        return False
    try:
        born = _created_at(path)
    except OSError:
        return False
    return born is not None and born > since


# ---- Organize New Only: the snapshot of what was already there, kept on disk ---------------------------------
# Taken when the folder is turned on and kept up to date while it's watched; at the next start the SAME snapshot is
# used (as long as the folder wasn't stopped in between: its watching_since is unchanged), so anything that isn't in
# it is new - however it got there. Before (2026-11-12) the snapshot was taken again at every start and a file MOVED
# in while Filect was closed (cut/paste keeps its old creation date) counted as already there and was never sorted.
# A file counts as already there if its CONTENT or its NAME (path inside the folder) is in the snapshot: a renamed
# old file stays (same content), an old file edited while Filect was closed stays (same name).
_BASELINE_FILE = "auto_organize_baseline.json"


def _saving_baselines() -> bool:
    """The test suite (offscreen Qt or pytest) never writes the real app data."""
    import sys
    return os.environ.get("QT_QPA_PLATFORM") != "offscreen" and "pytest" not in sys.modules


def _baselines_path() -> Path:
    from app.core.settings import settings
    return Path(settings.get_app_data_dir()) / _BASELINE_FILE


def _read_baselines() -> dict:
    try:
        d = json.loads(_baselines_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_baselines(d: dict) -> bool:
    """Temp file + fsync + replace, so a crash or a full disk keeps the old file. Windows: antivirus / OneDrive can
    hold the file for a moment, so the replace is retried a few times."""
    if not _saving_baselines():
        return False
    path = _baselines_path()
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
            fh.flush()
            os.fsync(fh.fileno())
        for i in range(5):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(0.1 * (i + 1))
        logger.warning(f"[OrganizeNewOnly] Couldn't save the snapshot file (held by another program): {path}")
    except Exception as e:
        logger.warning(f"[OrganizeNewOnly] Couldn't save the snapshot file: {e}")
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def _rel_name(path: str, folder: str) -> str:
    """A file's name inside its watched folder, as Windows compares it (case-insensitive)."""
    try:
        return os.path.normcase(os.path.relpath(os.path.normpath(path), os.path.normpath(folder)))
    except ValueError:                                   # (another drive)
        return os.path.normcase(os.path.normpath(path))


class _Base(set):
    """An Organize New Only snapshot: the content hashes of what was already there, plus `names`. `loaded`: it came
    from the saved file (else it was just taken and still has to be saved)."""
    def __init__(self, hashes=(), names=(), loaded=False):
        super().__init__(hashes)
        self.names = set(names)
        self.loaded = loaded


def _record_auto_moves(folder: str, moves) -> None:
    """Automatic moves go in History (apply.record_auto_moves: one entry per folder per day).
    History must never break a move: any failure is logged and the moves stand."""
    if not moves:
        return
    try:
        from app.core.apply import record_auto_moves
        record_auto_moves(folder, list(moves))
    except Exception as e:
        logger.warning(f"[AutoOrganize] Couldn't add {len(moves)} automatic move(s) to History: {e}")


def existing_folders_instruction(instruction: str, existing_folders: List[str]) -> str:
    """The planner's rules for "only use the folders already there" (Organize As-Is). Also used by the voice
    set-up of auto-organize. Folder names may be nested ('Photos/Vacation')."""
    folders_list = ', '.join(f"'{f}'" for f in existing_folders)
    return (
        f"[AUTO-ORGANIZE - EXISTING FOLDERS ONLY]\n"
        f"User's instructions: {instruction if instruction else 'Organize files into appropriate folders'}\n\n"
        f"EXISTING FOLDERS YOU CAN USE: {folders_list}\n\n"
        "GROUND RULES:\n"
        "• You can ONLY use the folders listed above — never create new folders.\n"
        "• Folder names use forward slashes for nested paths ('Photos/Vacation' = Vacation inside Photos).\n"
        "• When both a parent and a nested folder apply, PREFER the most-specific nested one (a vacation photo "
        "goes to 'Photos/Vacation', not just 'Photos').\n"
        "• Every file_id MUST appear exactly once in your plan. Omitting a file is FORBIDDEN.\n\n"
        "DECISION TREE — for EACH file, follow these steps IN ORDER:\n\n"
        "  Step 1: Compare the file's TAGS to the LEAF NAME of each existing folder. The LEAF NAME is the "
        "LAST segment after any slashes — for 'everything-else/pigs' the leaf is 'pigs' (not 'everything-else'). "
        "Does any tag (or its singular/plural form) match a leaf name? Tag 'pig' matches leaf 'pigs'; tag 'lion' "
        "matches leaf 'lions'; tag 'vacation' matches leaf 'Vacation'.\n"
        "    → YES: place the file in that folder. If MULTIPLE folders share a matching leaf (e.g. both 'pigs' "
        "and 'everything-else/pigs' would match a pig file), pick the most-specific (nested) one.\n"
        "    → NO: go to Step 2.\n\n"
        "  Step 2: Is there a CATCH-ALL folder among the existing folders? (A catch-all is one whose LEAF NAME "
        "contains 'other', 'everything', 'misc', 'general', 'rest', 'else', 'various', or 'unsorted'.)\n"
        "    → YES: place the file in that catch-all folder. STOP HERE. Do NOT put it in a subject folder it "
        "doesn't actually belong to just because that folder vaguely exists.\n"
        "    → NO: go to Step 3.\n\n"
        "  Step 3: No catch-all available — pick the existing folder whose name is most similar to the file's "
        "content type, using your best judgment.\n\n"
        "WORKED EXAMPLES:\n\n"
        "Folders = ['lions', 'everything-else', 'everything-else/pigs']  (NOTE: 'pigs' is NESTED inside "
        "everything-else):\n"
        "  • Lion photo  (tag 'lion') → 'lions'                  [Step 1: tag matches leaf 'lions']\n"
        "  • Pig photo   (tag 'pig')  → 'everything-else/pigs'   [Step 1: tag 'pig' matches LEAF 'pigs' of the "
        "nested folder — use the nested folder, NOT the parent 'everything-else']\n"
        "  • Bear photo  (tag 'bear') → 'everything-else'        [Step 2: no bear folder anywhere; catch-all]\n"
        "  • Sunset photo (no clear subject) → 'everything-else' [Step 2: catch-all]\n\n"
        "Folders = ['Photos', 'Photos/Vacation', 'Docs']:\n"
        "  • Vacation photo (tags 'photo','vacation') → 'Photos/Vacation'  [Step 1: 'vacation' matches the "
        "leaf of the nested folder; prefer nested over the parent 'Photos']\n"
        "  • Regular photo (tag 'photo' only) → 'Photos'  [Step 1: 'photo' matches leaf 'Photos'; nested "
        "'Photos/Vacation' does not apply because the file has no vacation tag]\n"
        "  • PDF document → 'Docs'  [Step 1: 'document' matches leaf 'Docs']\n\n"
        "FORBIDDEN:\n"
        "  • Putting a PIG photo in 'everything-else' when 'everything-else/pigs' exists. The nested 'pigs' "
        "folder is the correct destination for pig files — don't dump them in the parent.\n"
        "  • Putting a BEAR photo in 'lions' or 'pigs' just because they're animal-themed — they are not bear "
        "folders. Bears with no bear-named folder go to the catch-all.\n"
        "  • Omitting any file from your response (every file_id must appear exactly once).\n"
        "  • Creating a folder not in the listed existing folders."
    )


class AutoWatcherWorker(QThread):
    """
    Background worker thread for auto-watcher file processing.
    Runs AI indexing and organization off the main thread to prevent UI lag.
    """
    
    # Signals to communicate back to main thread
    file_indexed = Signal(str)  # file_path that was indexed
    file_organized = Signal(str, str, str)  # source_path, dest_path, category
    status_changed = Signal(str)  # status message
    error_occurred = Signal(str, str)  # file_path, error_message
    finished_processing = Signal(list)  # Emitted when done, with list of all processed file paths
    limit_reached = Signal(dict)  # emitted once when the monthly index limit is hit
    # Files this batch could NOT deal with — never marked done, so they're tried again later (the watcher spaces the
    # tries out). Before 2026-11-11 every failure path marked the whole batch done: one offline moment and those
    # files were never sorted that session.
    not_done = Signal(list, str)        # file paths, why ("no plan" / "plan failed" / "move failed")
    busy = Signal(list)                 # file paths open in another program: left untouched, tried once they're free
    
    def __init__(self, file_paths: List[str], folder: str, instruction: str, 
                 folder_instructions: Dict[str, str] = None,
                 existing_folders: List[str] = None):
        super().__init__()
        self.file_paths = file_paths
        self.folder = folder
        self.instruction = instruction
        self.folder_instructions = folder_instructions or {}
        self.existing_folders = existing_folders  # If set, ONLY use these folders (Organize As-Is mode)
        self._should_stop = False
    
    def stop(self):
        """Request the worker to stop."""
        self._should_stop = True
    
    def _fuzzy_match_folder(self, ai_folder: str) -> Optional[str]:
        """
        Fuzzy match an AI-suggested folder name to an existing folder.
        
        Returns the best matching existing folder name, or None if no good match.
        """
        if not self.existing_folders:
            return ai_folder  # No restriction, use as-is
        
        ai_lower = ai_folder.lower().strip()
        
        # First, try exact match (case insensitive)
        for existing in self.existing_folders:
            if existing.lower() == ai_lower:
                return existing
        
        # Try matching with common variations removed
        def normalize(s):
            return s.lower().replace(' ', '').replace('-', '').replace('_', '')
        
        ai_norm = normalize(ai_folder)
        for existing in self.existing_folders:
            if normalize(existing) == ai_norm:
                return existing
        
        # Try substring match (e.g., "documents 1" contains "document")
        for existing in self.existing_folders:
            if ai_lower in existing.lower() or existing.lower() in ai_lower:
                return existing
        
        # No good match found
        return None

    def _best_existing_folder_for_name(self, ai_folder: str) -> str:
        """Pick the best existing folder to absorb files the AI assigned to a
        non-existing folder. Prefers a catch-all folder, otherwise the existing
        folder whose name is most similar to the AI's suggested name.

        Note: in As-Is mode, the worker NO LONGER falls back to this method —
        if the AI hallucinates a folder we'd rather leave the files in place
        than force them into a wrong bucket (see the bears+lions bug at
        2026-05-29 01:29 where every file got dumped into 'animals/Bears'
        because difflib happened to score that highest against 'everything
        else'). This method is retained for non-As-Is callers and tests.
        """
        import difflib
        catchall_keywords = ['everything', 'other', 'misc', 'general', 'rest', 'else']
        for f in self.existing_folders or []:
            if any(k in f.lower() for k in catchall_keywords):
                return f
        ai_lower = ai_folder.lower().strip()
        if not self.existing_folders:
            return ai_folder
        best_folder = self.existing_folders[0]
        best_score = 0.0
        for f in self.existing_folders:
            score = difflib.SequenceMatcher(None, ai_lower, f.lower()).ratio()
            if score > best_score:
                best_score = score
                best_folder = f
        return best_folder

    def run(self):
        """Process files in background thread."""
        # Track all files we process (for marking as organized)
        all_processed_files = []
        history_moves = []     # (from, to) of every file this batch moved: goes in History at the end
        busy_files = []        # open in another program: untouched, tried again once free
        failed_moves = []      # a move that failed for another reason: tried again later
        crashed = False

        if not self.file_paths:
            self.finished_processing.emit(all_processed_files)
            return
        
        from app.core.database import file_index
        from app.core.ai_organizer import (
            request_organization_plan,
            validate_plan,
            deduplicate_plan,
            ensure_all_files_included,
            _apply_tag_safety_net,
        )
        from app.core.search import SearchService
        
        # First pass: identify files that need indexing
        # Simple logic: if a file (by filename) already has tags, don't re-index
        # Get all filenames that have tags upfront for fast lookup
        filenames_with_tags = file_index.get_filenames_with_tags()
        logger.info(f"[Worker] Found {len(filenames_with_tags)} files with tags in database")
        
        files_to_index = []
        files_skipped = 0
        
        for file_path in self.file_paths:
            if self._should_stop:
                self.finished_processing.emit(all_processed_files)
                return
            
            file_name = os.path.basename(file_path)
            
            # Simple check: does this filename have tags?
            if file_name in filenames_with_tags:
                # File already has tags - skip indexing
                files_skipped += 1
                logger.debug(f"[Worker] Skipping {file_name} - already has tags")
            else:
                # File doesn't have tags - needs indexing
                files_to_index.append(file_path)
        
        if files_skipped > 0:
            logger.info(f"[Worker] Skipped {files_skipped} file(s) that already have tags")
        
        # Index unindexed files first
        if files_to_index:
            logger.info(f"[Worker] Auto-indexing {len(files_to_index)} unindexed file(s)...")
            self.status_changed.emit(f"Indexing {len(files_to_index)} new file(s)...")
            
            search_service = SearchService()
            indexed_count = 0
            
            # Index files in PARALLEL (mirrors the manual-indexing path), so
            # when several new files appear at once the vision API calls run
            # concurrently instead of one-by-one. RAM-capped (see search.py).
            from app.core.search import recommended_index_workers
            workers = recommended_index_workers(len(files_to_index))
            total = len(files_to_index)
            limit_reached = False
            limit_signal_emitted = False

            def _index_one(fp: str):
                try:
                    return fp, search_service.index_single_file(Path(fp), force_ai=False)
                except Exception as exc:
                    return fp, {'error': str(exc)}

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_index_one, fp): fp for fp in files_to_index}
                for fut in as_completed(futures):
                    if self._should_stop:
                        for f in futures:
                            f.cancel()
                        self.finished_processing.emit(all_processed_files)
                        return
                    try:
                        file_path, result = fut.result()
                    except Exception as e:
                        logger.error(f"[Worker] Indexing future failed: {e}")
                        continue

                    if result.get('success'):
                        indexed_count += 1
                        logger.info(f"[Worker] Auto-indexed: {os.path.basename(file_path)}")
                        self.file_indexed.emit(file_path)
                        self.status_changed.emit(f"Indexed {indexed_count}/{total} files...")
                    elif result.get('limit_reached'):
                        # Index limit hit — stop submitting more work, but let
                        # in-flight tasks finish naturally; emit the upgrade
                        # signal once per run.
                        limit_reached = True
                        logger.warning(f"[Worker] Index limit reached: {result.get('error')}")
                        self.status_changed.emit("Index limit reached - upgrade for more")
                        self.error_occurred.emit(file_path, result.get('error', 'Index limit reached'))
                        if not limit_signal_emitted:
                            limit_signal_emitted = True
                            self.limit_reached.emit({})
                        for f in futures:
                            f.cancel()
                        break
                    elif result.get('error'):
                        logger.warning(f"[Worker] Failed to index {file_path}: {result.get('error')}")
            
            if indexed_count > 0:
                self.status_changed.emit(f"Indexed {indexed_count} file(s), now organizing...")
                logger.info(f"[Worker] Auto-indexed {indexed_count} files successfully")
        
        if self._should_stop:
            self.finished_processing.emit(all_processed_files)
            return
        
        # Build file info for AI using SIMPLE SEQUENTIAL IDs
        files_info = []
        files_by_id = {}
        
        for idx, file_path in enumerate(self.file_paths, start=1):
            if self._should_stop:
                self.finished_processing.emit(all_processed_files)
                return
                
            try:
                file_name = os.path.basename(file_path)
                file_size = os.path.getsize(file_path)
                file_id = idx
                
                # First try the exact path. If the file was just moved (e.g. by
                # flatten()) the DB might still hold its OLD path — fall back to
                # filename lookup so we still recover its tags. Without this
                # fallback, the AI-plan files_info ends up with empty tags after
                # a flatten, and downstream consumers (the tag-based safety net
                # in particular) silently see "no tags to match."
                indexed_info = file_index.get_file_by_path(file_path)
                if not indexed_info:
                    indexed_info = file_index.get_file_by_name(file_name)
                    if indexed_info:
                        # Reconcile the DB path so we don't keep hitting this
                        # fallback every time.
                        try:
                            file_index.update_file_path(indexed_info['id'], file_path)
                            indexed_info['file_path'] = file_path
                        except Exception:
                            pass

                files_info.append({
                    'id': file_id,
                    'file_path': file_path,
                    'file_name': file_name,
                    'file_size': file_size,
                    'label': indexed_info.get('label') if indexed_info else None,
                    'caption': indexed_info.get('caption') if indexed_info else None,
                    'tags': indexed_info.get('tags', []) if indexed_info else [],
                    'category': indexed_info.get('category') if indexed_info else None,
                })
                files_by_id[file_id] = file_path
                
            except Exception as e:
                logger.warning(f"[Worker] Error getting file info for {file_path}: {e}")
        
        if not files_info:
            # All files were either processed or skipped
            all_processed_files = list(self.file_paths)
            self.finished_processing.emit(all_processed_files)
            return
        
        # Request organization plan from AI
        logger.info(f"[Worker] Requesting AI plan for {len(files_info)} files with instruction: {self.instruction[:50]}...")
        self.status_changed.emit(f"AI analyzing {len(files_info)} files...")
        
        try:
            plan = request_organization_plan(self.instruction, files_info)

            if not plan:
                logger.warning("[Worker] No plan returned from AI: the files wait and are tried again later")
                self.not_done.emit(list(self.file_paths), "no plan")
                self.finished_processing.emit([])
                return

            # Belt-and-suspenders: run the deterministic tag-based safety net
            # HERE in the worker as well. request_organization_plan already
            # calls it, but the worker is the only auto-organize caller path
            # so calling it explicitly here makes it impossible to bypass
            # via any future change to that function. files_info is the exact
            # dict list we built above (with tags) — no risk of shape mismatch.
            try:
                plan = _apply_tag_safety_net(
                    plan,
                    files_info,
                    self.instruction,
                    existing_folders_only=bool(self.existing_folders),
                )
            except Exception as e:
                logger.warning(f"[Worker] Safety net skipped due to error: {e}")

            plan = deduplicate_plan(plan)

            # Fill in any files the AI omitted. In As-Is mode this is a
            # no-op — see ensure_all_files_included docstring; omitted files
            # are intentionally left in place rather than force-placed into
            # a best-guess existing folder.
            plan = ensure_all_files_included(
                plan,
                {f['id'] for f in files_info},
                files_info,
                existing_folders_only=bool(self.existing_folders),
            )
            
            is_valid, errors = validate_plan(plan, {f['id'] for f in files_info})
            if not is_valid:
                logger.warning(f"[Worker] Plan validation failed: {errors} — the files are tried again later")
                self.not_done.emit(list(self.file_paths), "no plan")
                self.finished_processing.emit([])
                return
            
            # Filter plan to only include files we're processing.
            # Also apply fuzzy matching for folder names in "Organize As-Is" mode.
            valid_file_ids = set(files_by_id.keys())
            filtered_folders = {}
            left_in_place_ids: List[int] = []

            for folder_name, file_ids in plan.get('folders', {}).items():
                valid_ids = [fid for fid in file_ids if fid in valid_file_ids]
                if not valid_ids:
                    continue

                if self.existing_folders:
                    # As-Is mode. We pre-create folders the user explicitly
                    # named in their instruction (see organize_single_folder
                    # / organize_folders_with_per_folder_options), so the
                    # allowed list already includes the user's intended
                    # destinations. The AI is told (Mode A prompt) to use
                    # ONLY those folders. If it still hallucinates a folder,
                    # we DO NOT force-route into a wrong bucket — we leave
                    # those files where they currently are on disk and tell
                    # the user how many we skipped. The user can update
                    # their instruction or pre-create the missing folder
                    # and re-run.
                    matched_folder = self._fuzzy_match_folder(folder_name)
                    if matched_folder:
                        if matched_folder != folder_name:
                            logger.info(f"[Worker] Mapped AI folder '{folder_name}' -> existing '{matched_folder}'")
                        if matched_folder in filtered_folders:
                            filtered_folders[matched_folder].extend(valid_ids)
                        else:
                            filtered_folders[matched_folder] = list(valid_ids)
                    else:
                        # No fuzzy match against the user-permitted folder set.
                        # Leave these files alone — do not invent a destination.
                        logger.info(
                            f"[Worker] As-Is: AI returned folder '{folder_name}' "
                            f"which isn't user-permitted; leaving "
                            f"{len(valid_ids)} file(s) in place"
                        )
                        left_in_place_ids.extend(valid_ids)
                else:
                    # Reorganize-All / normal mode - use folder as-is
                    filtered_folders[folder_name] = valid_ids

            placed_count = sum(len(ids) for ids in filtered_folders.values())
            logger.info(
                f"[Worker] Filtered plan has {placed_count} files in "
                f"{len(filtered_folders)} folders"
                + (f"; {len(left_in_place_ids)} file(s) left in place"
                   if left_in_place_ids else "")
            )
            for _fid in left_in_place_ids:       # (decided: they stay. Not done, they were planned again every few s)
                if files_by_id.get(_fid):
                    all_processed_files.append(files_by_id[_fid])
            if left_in_place_ids:
                self.status_changed.emit(
                    f"As-Is: left {len(left_in_place_ids)} file(s) in place "
                    f"(no matching folder)"
                )
            
            # Execute moves
            moved_count = 0
            skipped_count = 0
            
            for folder_name, file_ids in filtered_folders.items():
                for file_id in file_ids:
                    if self._should_stop:
                        _record_auto_moves(self.folder, history_moves)   # (what already moved stays revertable)
                        self.finished_processing.emit(all_processed_files)
                        return
                        
                    file_path = files_by_id.get(file_id)
                    if not file_path or not os.path.exists(file_path):
                        continue
                    
                    dest_folder = os.path.join(self.folder, folder_name)
                    dest_path = os.path.join(dest_folder, os.path.basename(file_path))
                    
                    # CRITICAL: Check if file is ALREADY in the correct location
                    # This prevents the _1 suffix bug and unnecessary moves
                    source_normalized = os.path.normpath(file_path)
                    dest_normalized = os.path.normpath(dest_path)
                    
                    if source_normalized == dest_normalized:
                        # File is already exactly where it should be - skip move but track as processed
                        skipped_count += 1
                        all_processed_files.append(file_path)
                        logger.debug(f"[Worker] File already in place, skipping: {os.path.basename(file_path)}")
                        continue
                    
                    # Check if file is already in the destination folder (even if paths differ slightly)
                    source_folder = os.path.normpath(os.path.dirname(file_path))
                    dest_folder_normalized = os.path.normpath(dest_folder)
                    
                    if source_folder == dest_folder_normalized:
                        # File is already in the correct folder - skip move but track as processed
                        skipped_count += 1
                        all_processed_files.append(file_path)
                        logger.debug(f"[Worker] File already in correct folder, skipping: {os.path.basename(file_path)}")
                        continue
                    
                    # Create destination folder only if we're actually going to move something
                    os.makedirs(dest_folder, exist_ok=True)
                    
                    # Handle duplicates - only if dest_path is a DIFFERENT file
                    if os.path.exists(dest_path):
                        base, ext = os.path.splitext(os.path.basename(file_path))
                        counter = 1
                        while os.path.exists(dest_path):
                            dest_path = os.path.join(dest_folder, f"{base}_{counter}{ext}")
                            counter += 1
                    
                    try:
                        move_file(file_path, dest_path)     # (never a half copy of a file open elsewhere)
                        moved_count += 1
                        history_moves.append((file_path, dest_path))
                        all_processed_files.append(dest_path)  # Track new location
                        logger.info(f"[Worker] Organized: {file_path} -> {dest_path}")
                        self.file_organized.emit(file_path, dest_path, folder_name)
                        
                        # Update database path
                        try:
                            # Find the file in database by old path or filename
                            old_record = file_index.get_file_by_path(file_path)
                            if not old_record:
                                # Try by filename
                                old_record = file_index.get_file_by_name(os.path.basename(file_path))
                            
                            if old_record:
                                file_index.update_file_path(old_record['id'], dest_path)
                                logger.info(f"[Worker] Updated DB path for file {old_record['id']}")
                            else:
                                logger.warning(f"[Worker] Could not find file in DB to update path: {file_path}")
                        except Exception as e:
                            logger.warning(f"[Worker] Could not update index for moved file: {e}")
                            
                    except Exception as e:
                        if is_busy(e):
                            busy_files.append(file_path)
                            logger.info(f"[Worker] {os.path.basename(file_path)} is open in another program: left "
                                        f"where it is, tried again once it's free")
                        else:
                            failed_moves.append(file_path)
                            logger.error(f"[Worker] Error moving {file_path}: {e}")
                            self.error_occurred.emit(file_path, str(e))
            
            if skipped_count > 0:
                logger.info(f"[Worker] Skipped {skipped_count} file(s) already in correct location")
            
            if moved_count > 0:
                self.status_changed.emit(f"Organized {moved_count} file(s)")
            else:
                self.status_changed.emit(f"All files already organized")
            
        except Exception as e:
            logger.error(f"[Worker] Error in organization: {e}")
            self.error_occurred.emit("", str(e))
            crashed = True

        _record_auto_moves(self.folder, history_moves)   # (one write per batch, on this background thread)

        # Only what this batch really dealt with counts as done (moved, already in place, or deliberately left).
        # (It marked EVERY file done when nothing was — a batch whose files were all open elsewhere, or whose plan
        # failed, was never tried again.)
        handled = {os.path.normcase(os.path.normpath(p)) for p in all_processed_files}
        handled |= {os.path.normcase(os.path.normpath(src)) for src, _ in history_moves}
        if busy_files:
            self.busy.emit(list(busy_files))
        waiting = {os.path.normcase(os.path.normpath(p)) for p in busy_files}
        rest = [p for p in self.file_paths if os.path.normcase(os.path.normpath(p)) not in handled | waiting
                and os.path.exists(p)]
        if crashed:
            if rest:
                self.not_done.emit(rest, "plan failed")
        else:
            failed = {os.path.normcase(os.path.normpath(p)) for p in failed_moves}
            retry = [p for p in rest if os.path.normcase(os.path.normpath(p)) in failed]
            if retry:
                self.not_done.emit(retry, "move failed")
            # (the plan said nothing about the others and the fill-in placed them nowhere: they stay, like As-Is)
            all_processed_files.extend(p for p in rest if os.path.normcase(os.path.normpath(p)) not in failed)
        self.finished_processing.emit(all_processed_files)


STILL_WRITING_SECS = 5    # a file changed (or created) this recently, or still growing, waits: it's being written
RETRY_AFTER = (60, 300, 1800)    # a file whose plan / move failed is tried again after 1, 5 and 30 min, then left
                                 # until the next start (the Mac's ladder, 5946868)


def _can_move(path: str) -> bool:
    """Could Filect move this file right now? False only when another program has it open in a way that blocks
    moving (Word, a recorder, a download still being written): Windows then refuses to rename it. A cheap check
    (opening the file for DELETE access, sharing everything else), so a file open elsewhere waits with NO AI call
    instead of being planned every few seconds and failing to move each time."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        DELETE, SHARE_ALL, OPEN_EXISTING, BACKUP_SEMANTICS = 0x00010000, 0x7, 3, 0x02000000
        h = k32.CreateFileW(str(path), DELETE, SHARE_ALL, None, OPEN_EXISTING, BACKUP_SEMANTICS, None)
        if h is None or h == wintypes.HANDLE(-1).value:
            return ctypes.get_last_error() not in (32, 33)          # sharing / lock violation: it's open elsewhere
        k32.CloseHandle(h)
        return True
    except Exception:
        return True


class AutoOrganizeWatcher(QObject):
    """
    Watches folders for new files and auto-organizes them using AI.
    
    Features:
    - Per-folder instructions
    - Catch-up mode (organize files added while app was closed)
    - Flatten and re-organize existing folders
    - Background file system monitoring
    """
    
    # Signals
    file_organized = Signal(str, str, str)  # source_path, dest_path, category
    file_indexed = Signal(str)  # file_path that was auto-indexed
    error_occurred = Signal(str, str)  # file_path, error_message
    status_changed = Signal(str)  # status message
    limit_reached = Signal(dict)  # monthly index limit hit during auto-organize

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Watched folders list
        self.watched_folders: List[str] = []
        
        # Per-folder instructions: {folder_path: instruction_text}
        self.folder_instructions: Dict[str, str] = {}
        
        # For catch-up mode: only organize files modified after this time
        self.catch_up_since: Optional[datetime] = None
        
        # Internal state
        self._is_running = False
        self._pending_files: Dict[str, float] = {}  # path -> first_seen_time
        self._processed_files: Set[str] = set()
        self._file_check_timer: Optional[QTimer] = None
        self._debounce_seconds = 2.0  # Wait for file to stabilize
        self._protected_cache: Dict[str, bool] = {}  # (folder -> is it inside an app / code project)
        self._protected_at = 0.0
        # Paths this watcher indexed itself this session (normcase). In a `skip_known` folder a file the index
        # already knows stays where it is, except one of these (just indexed here, still waiting to be sorted).
        self._indexed_here: Set[str] = set()

        # Background steps (walking / hashing big folders happens off the UI thread)
        self._run_gen = 0                      # bumped by start() / stop(): a step from an older run is dropped
        self._preparing: Set[str] = set()      # (path keys) folders still being looked through: not checked yet
        self._bg: Dict[int, tuple] = {}        # token -> (thread, then, run generation)
        self._bg_token = 0
        self._bg_retired: List[QThread] = []   # (threads stop() couldn't wait out: kept until they end)
        self._tick_busy = False                # a periodic check is running in the background

        # Counter for periodic cleanup (every N checks)
        self._check_count = 0
        self._cleanup_interval = 10  # Run cleanup every 10 checks (~30 seconds)
        
        # Files to ignore (system files, temp files, etc.)
        self._ignore_patterns = {
            '.DS_Store', 'Thumbs.db', 'desktop.ini', '.git', '.gitignore',
            '__pycache__', '.pyc', '.pyo', '.tmp', '.temp', '.swp', '.bak',
            '~$'  # Office temp files
        }
        self._ignore_extensions = {
            '.tmp', '.temp', '.crdownload', '.part', '.partial'
        }
        
        # Background worker for file processing (prevents UI lag)
        self._current_worker: Optional[AutoWatcherWorker] = None
        # Queue stores (folder, instruction, existing_folders) - we re-scan folder when processing
        self._worker_queue: List[tuple] = []
        self._current_batch: List[str] = []     # the files the running batch was given
        # (path key) -> (failed tries, when the next try may start): files whose plan or move failed
        self._retries: Dict[str, tuple] = {}
        self._waiting_busy: Set[str] = set()    # (path keys) open in another program: noted once, waiting
        
        # Track files that have been successfully organized
        # This prevents re-processing the same files and unnecessary AI calls
        self._organized_files: Set[str] = set()  # normalized paths of organized files

        # --- Organize New Only (action = 3) state ---
        # Per-folder set of SHA-256 hashes captured at start() time. Files
        # whose hash is in this set were present when watching began and must
        # be left untouched for the rest of the session — even if the user
        # later moves or renames them.
        self._baseline_hashes: Dict[str, Set[str]] = {}
        # Normalized lowercased paths the watcher has explicitly decided to
        # leave in place for this session. Checked at the top of the periodic
        # scan loop so we never re-hash or re-evaluate the same pre-existing
        # files again.
        self._left_in_place_paths: Set[str] = set()
        self._baseline_names: Dict[str, Set[str]] = {}   # per folder: names already there (see _Base)
        self._baseline_dirty: Set[str] = set()           # folders whose snapshot changed since it was last saved
        self._flush_pending = False
        _qapp = QCoreApplication.instance()
        if _qapp is not None:
            _qapp.aboutToQuit.connect(self._flush_baselines)   # (quitting: what was just sorted stays known)
        self._sizes: Dict[str, tuple] = {}       # path key -> (size, mtime) last seen: a write in progress shows
        self._old_workers: List[Any] = []        # finished workers whose threads are still ending (never collected)

    @property
    def is_running(self) -> bool:
        return self._is_running

    # ------------------------------------------------------------------ background steps
    def _off_ui(self, fn, then) -> None:
        """Run fn() off the UI thread; then(result, error) runs on the UI thread when it's done — unless the
        watcher was stopped or restarted meanwhile (that run's results no longer apply)."""
        self._bg_token += 1
        token = self._bg_token
        thread = _OffUIThread(token, fn)
        self._bg[token] = (thread, then, self._run_gen)
        thread.done.connect(self._on_bg_done, Qt.QueuedConnection)
        thread.start()

    def _on_bg_done(self, token: int, result, error) -> None:
        entry = self._bg.pop(token, None)
        if entry is None:
            return
        thread, then, gen = entry
        thread.wait(2000)                      # (it emitted on its way out: it's ending, never drop a running one)
        self._bg_retired = [t for t in self._bg_retired if not t.isFinished()]
        if gen != self._run_gen:
            return
        if error is not None and not isinstance(error, _Cancelled):
            logger.error(f"[Watcher] a background step failed: {error}")
        try:
            then(result, error)
        except Exception as e:
            logger.error(f"[Watcher] after a background step: {e}")

    def _check_gen(self, gen: Optional[int]) -> None:
        if gen is not None and gen != self._run_gen:
            raise _Cancelled()

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """Pump events until no folder is being looked through and no background step is pending (tests, and
        anything that needs the watcher settled). True if it settled in time."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            QCoreApplication.processEvents()
            if not self._bg and not self._preparing:
                return True
            time.sleep(0.005)
        QCoreApplication.processEvents()
        return not self._bg and not self._preparing

    def add_folder(self, folder_path: str) -> bool:
        """Add a folder to watch. Returns True if successful.

        A folder whose entry is `paused` is skipped: voice turned on only another folder on the list, and the
        others stay off (through restarts) until Start is pressed in the Organize tab, which clears `paused`.
        """
        # Normalize path for consistent lookups
        folder_path = os.path.normpath(folder_path)

        if not os.path.isdir(folder_path):
            logger.warning(f"Not a valid directory: {folder_path}")
            return False

        if self._is_paused(folder_path):
            logger.info(f"Skipping paused watch folder: {folder_path} (left off until Start is pressed)")
            return False

        self._watch(folder_path)
        return True

    def _watch(self, folder_path: str) -> str:
        """Put a folder on the watch list once (in any letter case). Returns the spelling on the list."""
        for f in self.watched_folders:
            if _path_key(f) == _path_key(folder_path):
                return f
        self.watched_folders.append(folder_path)
        logger.info(f"Added watch folder: {folder_path}")
        return folder_path

    @staticmethod
    def _is_paused(folder_path: str) -> bool:
        """The folder's own `paused` flag (not the whole watcher's Stop, settings.auto_organize_paused)."""
        try:
            from app.core.settings import settings
            entry = settings._auto_organize_entry(folder_path)
        except Exception:
            return False
        return bool(entry and entry.get('paused'))

    def remove_folder(self, folder_path: str) -> None:
        """Remove a folder from watch list (in any letter case). Whatever is still queued for it is dropped and
        a batch running for it is asked to stop, so a folder that was turned off stops moving files."""
        key = _path_key(folder_path)
        kept = [f for f in self.watched_folders if _path_key(f) != key]
        if len(kept) != len(self.watched_folders):
            self.watched_folders[:] = kept
            logger.info(f"Removed watch folder: {os.path.normpath(folder_path)}")
        self._worker_queue = [q for q in self._worker_queue if _path_key(q[0]) != key]
        worker = self._current_worker
        if worker is not None and worker.isRunning() and _path_key(worker.folder) == key:
            worker.stop()

    def add_folder_live(self, folder_path: str, instruction: str = '') -> bool:
        """Turned on by voice while the watcher runs: watch this one folder from now on without restarting
        the others.

        Does for this folder what start() does for each folder: its Organize New Only baseline, and every file
        already there counted as known (whatever was there was dealt with before this), so only files that
        arrive from now on are sorted. The folder's action, instruction and watching_since must already be
        saved in settings. It is the folder the person asked for, so its `paused` flag doesn't stop it here.
        Not running: the folder is only put on the list (start() does the rest). False if it isn't a folder.
        """
        folder_path = os.path.normpath(folder_path)
        if not os.path.isdir(folder_path):
            logger.warning(f"Not a valid directory: {folder_path}")
            return False
        folder_path = self._watch(folder_path)
        self.folder_instructions[folder_path] = instruction or ''
        if not self._is_running:
            return True
        # Looked through in the background (a big folder took minutes on the UI thread); until then the
        # periodic check leaves this folder alone, so nothing already there is mistaken for a new file.
        key = _path_key(folder_path)
        self._preparing.add(key)
        gen = self._run_gen

        def look():
            return self._compute_baseline(folder_path, gen), self._compute_snapshot(folder_path, gen)

        def then(result, error):
            if result is None:                 # (it failed: do it here rather than sort what was there)
                self._capture_baseline(folder_path)
                self._snapshot_existing(folder_path)
            else:
                self._merge_look(folder_path, *result)
            self._preparing.discard(key)
            logger.info(f"Watching {folder_path} live (now {len(self.watched_folders)} folder(s))")
            self.status_changed.emit(f"Watching {len(self.watched_folders)} folder(s) for new files...")

        self._off_ui(look, then)
        return True
    
    def clear_folders(self) -> None:
        """Remove all watched folders."""
        self.watched_folders.clear()
        self.folder_instructions.clear()
        logger.info("Cleared all watch folders")
    
    def set_instruction(self, folder_path: str, instruction: str) -> None:
        """Set the organization instruction for a specific folder."""
        folder_path = os.path.normpath(folder_path)
        self.folder_instructions[folder_path] = instruction
        logger.info(f"Set instruction for {folder_path}: {instruction[:50]}...")
    
    def start(self, organize_existing: bool = True, flatten_first: bool = False) -> None:
        """
        Start watching folders.
        
        Args:
            organize_existing: If True, organize files already in the folders
            flatten_first: If True, flatten folder structure before organizing
        """
        if self._is_running:
            logger.warning("Watcher already running")
            return
        
        if not self.watched_folders:
            logger.warning("No folders to watch")
            self.status_changed.emit("No folders configured to watch")
            return
        
        self._is_running = True
        self._run_gen += 1
        gen = self._run_gen
        self._tick_busy = False
        self._processed_files.clear()
        self._pending_files.clear()
        self._retries.clear()
        self._waiting_busy.clear()
        # Reset Organize-New-Only state every start() so the baseline reflects
        # the folder contents at THIS moment.
        self._flush_baselines()                  # (what the last run learned is kept)
        self._baseline_hashes.clear()
        self._baseline_names.clear()
        self._left_in_place_paths.clear()
        self._indexed_here.clear()

        # Everything below walks (and for Organize New Only, hashes) every file: done in the background, then
        # the periodic check starts. Same order as always: the Organize-New-Only baseline FIRST — before any
        # flatten or organize-existing pass — so files the app itself moves aren't mistaken for "new" arrivals;
        # then every file already there counts as known (except what arrived while Filect was closed, see
        # _compute_snapshot); then flatten / organize what's there, if asked.
        folders = [os.path.normpath(f) for f in self.watched_folders]
        self._preparing = {_path_key(f) for f in folders}
        folder_count = len(folders)
        self.status_changed.emit(f"Starting watch on {folder_count} folder(s)...")
        logger.info(f"Starting watcher for {folder_count} folders (looking through them in the background)")

        def look():
            looked = [(f, self._compute_baseline(f, gen), self._compute_snapshot(f, gen)) for f in folders]
            flattened = 0
            if flatten_first:
                for f in folders:
                    self._check_gen(gen)
                    flattened += self.flatten_folder(f, instruction=self.folder_instructions.get(f, ''))
            existing = self._collect_existing(folders, gen) if organize_existing else {}
            return looked, flattened, existing

        def then(result, error):
            if result is None:                 # (it failed: do it here rather than sort what was there)
                for f in folders:
                    self._capture_baseline(f)
                    self._snapshot_existing(f)
                if organize_existing:
                    self._organize_existing_files()
            else:
                looked, flattened, existing = result
                for f, baseline, snapshot in looked:
                    self._merge_look(f, baseline, snapshot)
                logger.info(f"Watcher looked through {folder_count} folder(s): "
                            f"{len(self._processed_files)} pre-existing files")
                if flattened:
                    self.status_changed.emit(f"Flattened {flattened} files from subfolders")
                self._organize_collected(existing)
            for f in folders:
                self._preparing.discard(_path_key(f))
            if self._file_check_timer is None:
                self._file_check_timer = QTimer(self)
                self._file_check_timer.timeout.connect(self._on_check_timer)
                self._file_check_timer.start(3000)  # Check every 3 seconds
            self.status_changed.emit(f"Watching {len(self.watched_folders)} folder(s) for new files...")

        self._off_ui(look, then)

    def stop(self) -> None:
        """Stop watching folders."""
        self._flush_baselines()                       # (Organize New Only: keep what this run learned)
        if not self._is_running:
            return

        self._is_running = False
        self._run_gen += 1                     # (background steps of this run stop at their next check)
        self._preparing.clear()
        self._tick_busy = False
        for thread, _then, _gen in list(self._bg.values()):
            if not thread.wait(1500):
                self._bg_retired.append(thread)
        self._bg.clear()

        if self._file_check_timer:
            self._file_check_timer.stop()
            self._file_check_timer = None
        
        # Stop any running worker
        if self._current_worker is not None and self._current_worker.isRunning():
            logger.info("Stopping background worker...")
            self._current_worker.stop()
            self._current_worker.wait(3000)  # Wait up to 3 seconds
        
        # Clear queues
        self._pending_files.clear()
        self._retries.clear()
        self._waiting_busy.clear()
        self._worker_queue.clear()
        
        self.status_changed.emit("Watcher stopped")
        logger.info("Watcher stopped")
    
    def flatten_folder(self, folder_path: str, instruction: str = None) -> int:
        """
        Flatten folder by moving all files from subfolders to root.

        This is used for the "Re-organize All" feature to reset folder structure
        before applying new organization instructions.

        Args:
            folder_path: Path to the folder to flatten
            instruction: Optional user instruction. When provided, folders the
                user explicitly named ("called X" / "named X") are auto-created
                up front and protected from the empty-folder cleanup that runs
                at the end of flattening.

        Returns:
            Number of files moved
        """
        folder_path = os.path.normpath(folder_path)
        if not os.path.isdir(folder_path):
            logger.warning(f"Cannot flatten - not a directory: {folder_path}")
            return 0

        # Auto-create user-named folders up front so they exist as destinations
        # for the AI step and survive the empty-folder cleanup below.
        protected: set = set()
        if instruction:
            named = self._extract_named_folders(instruction)
            if named:
                self._ensure_named_folders_exist(folder_path, instruction)
                protected = {n.lower() for n in named}

        moved_count = 0
        files_to_move = []
        
        # Collect files from subfolders (not the root level)
        for root, dirs, files in os.walk(folder_path):
            if root == folder_path:
                continue  # Skip root level files
            
            for file_name in files:
                file_path = os.path.join(root, file_name)
                # Full path: pinned files, and anything inside an app / code project, stay put
                if self._should_ignore(file_path, folder_path):
                    continue
                files_to_move.append(file_path)

        if not files_to_move:
            logger.info(f"No files to flatten in {folder_path}")
            return 0

        self.status_changed.emit(f"Flattening {len(files_to_move)} files...")

        # Move files to root, handling name conflicts
        flattened = []     # (from, to): goes in History
        for file_path in files_to_move:
            try:
                file_name = os.path.basename(file_path)
                dest_path = os.path.join(folder_path, file_name)
                
                # Handle name conflicts by adding (1), (2), etc.
                if os.path.exists(dest_path):
                    base, ext = os.path.splitext(file_name)
                    counter = 1
                    while os.path.exists(dest_path):
                        dest_path = os.path.join(folder_path, f"{base} ({counter}){ext}")
                        counter += 1
                
                move_file(file_path, dest_path)          # (never a half copy of a file open elsewhere)
                moved_count += 1
                flattened.append((file_path, dest_path))
                logger.info(f"Flattened: {file_path} -> {dest_path}")

                # Keep the DB in sync with disk. Without this the AI-plan
                # build later in this same run can't find the file's tags
                # via get_file_by_path (the path it knows is now stale),
                # and downstream consumers — notably the tag-based safety
                # net — silently see empty tags for every file. Lookup is
                # by old path first, then by filename as a fallback.
                try:
                    from app.core.database import file_index
                    rec = file_index.get_file_by_path(file_path)
                    if not rec:
                        rec = file_index.get_file_by_name(os.path.basename(file_path))
                    if rec:
                        file_index.update_file_path(rec['id'], dest_path)
                except Exception as e:
                    logger.debug(f"Flatten: could not update DB path for {file_path}: {e}")

            except Exception as e:
                logger.error(f"Error flattening {file_path}: {e}")
                self.error_occurred.emit(file_path, str(e))

        _record_auto_moves(folder_path, flattened)

        # Clean up empty subdirectories, preserving any folders the user
        # explicitly named in their instruction.
        self._cleanup_empty_folders(folder_path, protected_names=protected)

        logger.info(f"Flattened {moved_count} files in {folder_path}")
        return moved_count
    
    def _extract_named_folders(self, instruction: str) -> List[str]:
        """Extract folder names the user explicitly named in their instruction.

        Relies on the reliable "called X" / "named X" phrasing. The capture
        stops at conjunctions/prepositions (and, except, where, to, ...) so
        names like "everything else" are kept whole without bleeding into the
        rest of the sentence.
        """
        if not instruction:
            return []
        import re
        names = []
        seen = set()
        stopwords = {'a', 'an', 'the', 'it', 'them', 'this', 'that'}

        pattern = (
            r'(?:called|named)\s+["\']?([A-Za-z0-9][\w \-]*?)["\']?'
            r'(?=\s+(?:and|except|where|to|in|on|into|onto|or|but|then)\b|["\',.\n]|$)'
        )
        for m in re.finditer(pattern, instruction, flags=re.IGNORECASE):
            name = m.group(1).strip().strip('"\'').strip()
            key = name.lower()
            if not name or key in seen or key in stopwords or len(name) > 60:
                continue
            if 'folder' in key or ' move ' in key:
                continue
            seen.add(key)
            names.append(name)
        return names

    def _ensure_named_folders_exist(self, folder_path: str, instruction: str) -> None:
        """Create folders the user named in their instruction if they don't exist.

        We collect the LEAF name of every existing subfolder (top-level AND
        nested) — case-insensitive — so a user-named ``bears`` doesn't get
        created at the root when ``animals/Bears`` already exists. In that
        scenario the existing nested folder already fulfils the user's
        intent; creating a parallel ``bears/`` at the root would split the
        user's collection between two locations, which is the bug seen on
        2026-05-29 02:05.
        """
        named = self._extract_named_folders(instruction)
        if not named:
            return

        # Walk the tree (top-level + nested) and collect LEAF names so a
        # user-named 'bears' matches an existing 'animals/Bears'.
        existing_leaf_lower: set = set()
        try:
            for root, dirs, _ in os.walk(folder_path):
                # Don't descend into hidden / build folders
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith('.') and d not in self._SKIP_DIR_NAMES
                ]
                for d in dirs:
                    existing_leaf_lower.add(d.lower())
        except OSError:
            pass

        for name in named:
            if name.lower() in existing_leaf_lower:
                logger.info(
                    f"User-specified folder '{name}' already exists as an "
                    f"existing leaf — skipping creation"
                )
                continue
            try:
                os.makedirs(os.path.join(folder_path, name), exist_ok=True)
                logger.info(f"Created user-specified folder: {name}")
                existing_leaf_lower.add(name.lower())
            except OSError as e:
                logger.warning(f"Could not create folder '{name}': {e}")

    def _cleanup_empty_folders(self, root_folder: str, protected_names: set = None) -> int:
        """Remove empty subdirectories. Returns count of removed folders.

        Folders whose name is in ``protected_names`` (case-insensitive) are
        never deleted, even when empty — typically the folders the user
        explicitly named in their instruction.
        """
        removed_count = 0
        root_folder = os.path.normpath(root_folder)
        protected = {n.lower() for n in (protected_names or set())}

        # Walk bottom-up to remove nested empty folders
        for dirpath, dirnames, filenames in os.walk(root_folder, topdown=False):
            # Skip the root folder itself (normalize for comparison)
            if os.path.normpath(dirpath) == root_folder:
                continue

            # Skip hidden folders
            if os.path.basename(dirpath).startswith('.'):
                continue

            # Skip folders the user explicitly named in their instruction
            if os.path.basename(dirpath).lower() in protected:
                continue

            # Never inside an app, a library or a code project (an empty .git\refs\tags is part of a repository)
            if self._inside_protected(os.path.join(dirpath, '_'), root_folder):
                continue

            try:
                # Check if folder is empty (no files or subdirs)
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
                    removed_count += 1
                    logger.info(f"Removed empty folder: {dirpath}")
            except OSError as e:
                logger.debug(f"Could not remove folder {dirpath}: {e}")

        return removed_count
    
    def _should_ignore(self, file_path: str, root: Optional[str] = None) -> bool:
        """Check if a file should be ignored.

        Args:
            file_path: Full path or just filename to check. Only a full path can be
                checked for pins and for being inside an app / a code project.
            root: The folder being scanned, if the caller knows it (else the
                watched folder holding the file is used).
        """
        from app.core.settings import settings
        
        file_name = os.path.basename(file_path)
        
        # Check exact names
        if file_name in self._ignore_patterns:
            return True
        
        # Check patterns
        for pattern in self._ignore_patterns:
            if file_name.startswith(pattern):
                return True
        
        # Check extensions
        _, ext = os.path.splitext(file_name.lower())
        if ext in self._ignore_extensions:
            return True
        
        # Ignore hidden files (starting with .)
        if file_name.startswith('.'):
            return True
        
        # Check user-defined exclusions from settings
        if settings.should_exclude(file_path):
            logger.debug(f"Excluding {file_path} (matches user exclusion pattern)")
            return True

        # Never reach inside an app, a library or a code project in a watched folder
        return self._inside_protected(file_path, root)

    def _inside_protected(self, file_path: str, root: Optional[str] = None) -> bool:
        """organize_reader.protected (the same rule as organize by voice) for a file under `root`, or under
        the watched folder holding it."""
        if not os.path.dirname(file_path):          # (a bare name: the caller has no location yet)
            return False
        path = os.path.normpath(file_path)
        key = _path_key(path)
        for folder in ([root] if root else list(self.watched_folders)):
            top = os.path.normpath(folder)
            if not key.startswith(_path_key(top).rstrip(os.sep) + os.sep):
                continue
            try:
                from app.core.organize_reader import protected
                return bool(protected(path, top, self._fresh_protected_cache()))
            except Exception as e:
                logger.warning(f"Couldn't check whether {path} is inside a project or an app ({e}): leaving it alone")
                return True
        return False

    def _fresh_protected_cache(self) -> Dict[str, bool]:
        """{folder: is it off-limits}, shared by every check; forgotten each minute (a project can appear later)."""
        now = time.monotonic()
        if now - self._protected_at > 60:
            self._protected_cache, self._protected_at = {}, now
        return self._protected_cache

    def _prune_walk(self, root: str, dirs: List[str]) -> None:
        """For os.walk, in place: don't descend into hidden folders, nor apps / libraries / code projects
        (nothing inside them may move, so there's nothing there to look at)."""
        try:
            from app.core.organize_reader import _dir_is_off_limits
        except Exception:
            _dir_is_off_limits = None
        cache = self._fresh_protected_cache()
        kept = []
        for d in dirs:
            if d.startswith('.'):
                continue
            p = os.path.join(root, d)
            if _dir_is_off_limits is not None:
                if p not in cache:
                    try:
                        cache[p] = bool(_dir_is_off_limits(p))
                    except Exception:
                        cache[p] = True
                if cache[p]:
                    continue
            kept.append(d)
        dirs[:] = kept

    def _get_instruction_for_folder(self, folder_path: str) -> str:
        """Get the CURRENT instruction for a specific folder.

        Reads live from settings (the source of truth, updated immediately
        when the user edits the instruction in the UI) so the watcher and the
        re-organize flow always honor the latest edits — without needing the
        watcher to be restarted or having to push updates through other paths.
        Falls back to the in-memory cache for legacy/test callers that pre-set
        instructions there.
        """
        folder_path = os.path.normpath(folder_path)
        try:
            from app.core.settings import settings
            for f in settings.auto_organize_folders:
                if os.path.normpath(f.get('path', '')) == folder_path:
                    instruction = f.get('instruction', '') or ''
                    logger.debug(f"Instruction (live) for {folder_path}: {instruction[:50] if instruction else '(none)'}")
                    return instruction
        except Exception as e:
            logger.warning(f"Could not read live instruction from settings: {e}")
        # Fallback to the cached dict
        instruction = self.folder_instructions.get(folder_path, '')
        logger.debug(f"Instruction (cache) for {folder_path}: {instruction[:50] if instruction else '(none)'}")
        return instruction

    # Folder names that look like build/system scaffolding rather than user
    # destinations; filtered out of the existing-folders list for As-Is mode.
    _SKIP_DIR_NAMES = {
        '__pycache__', 'node_modules', 'venv', '.venv',
        'dist', 'build', '.cache', '.next', '.nuxt',
    }

    def _collect_existing_folders(self, folder_path: str,
                                  max_depth: int = 3,
                                  max_total: int = 200) -> List[str]:
        """Existing subfolders for As-Is mode (kept for older callers): see _usable_subfolders."""
        return self._usable_subfolders(folder_path, max_depth=max_depth, max_total=max_total)

    @staticmethod
    def _usable_subfolders(folder_path: str, max_depth: int = 3, max_total: int = 200) -> List[str]:
        """The subfolders files may be sorted into (Organize As-Is): never a code project, an app or a
        library (organize_reader._dir_is_off_limits), and nothing inside one.

        Walks up to ``max_depth`` levels deep so nested folders like
        'Photos/Vacation' are also valid destinations — not just the immediate
        children. Hidden folders ('.git', '.idea', …) and obvious build
        artifacts are skipped. Returns paths RELATIVE to ``folder_path``,
        using forward slashes for stable consumption by the AI prompt.

        If the total exceeds ``max_total``, falls back to top-level only —
        deep listings produce noisy AI plans and the user is likely using
        nested folders as scaffolding, not destinations.
        """
        from app.core.organize_reader import _dir_is_off_limits
        skip = AutoOrganizeWatcher._SKIP_DIR_NAMES

        def usable(parent: str, name: str) -> bool:
            return (not name.startswith('.') and name not in skip
                    and not _dir_is_off_limits(os.path.join(parent, name)))

        folder_path = os.path.normpath(folder_path)
        if not os.path.isdir(folder_path):
            return []
        out: List[str] = []
        try:
            for root, dirs, files in os.walk(folder_path):
                rel = os.path.relpath(root, folder_path)
                depth = 0 if rel == '.' else rel.count(os.sep) + 1
                # Children of this root are at depth+1. Stop descending past the cap.
                if depth + 1 > max_depth:
                    dirs[:] = []
                    continue
                # Prune hidden, scaffolding and off-limits folders in-place so we
                # neither include them nor descend into them.
                dirs[:] = [d for d in dirs if usable(root, d)]
                for d in dirs:
                    sub_rel = os.path.relpath(os.path.join(root, d), folder_path)
                    out.append(sub_rel.replace(os.sep, '/'))
        except OSError as e:
            logger.warning(f"Could not walk {folder_path} for existing folders: {e}")
            return []

        if len(out) > max_total:
            logger.info(
                f"Existing-folders walk found {len(out)} folders (cap {max_total}); "
                "falling back to top-level only."
            )
            out = []
            try:
                for item in os.listdir(folder_path):
                    if os.path.isdir(os.path.join(folder_path, item)) and usable(folder_path, item):
                        out.append(item)
            except OSError:
                pass
        return out

    # ─────────────────────────────────────────────────────────────────────
    # Organize New Only (action = 3): hash-based pre-existing-file detection.
    # The watcher snapshots every file's SHA-256 at start() and uses that
    # baseline to recognize pre-existing files even if they're later moved,
    # renamed, or duplicated. Genuinely new files (no hash in the baseline,
    # not known in the DB) are the only ones that get organized.
    # ─────────────────────────────────────────────────────────────────────

    def _compute_file_hash(self, path: str, gen: Optional[int] = None) -> Optional[str]:
        """SHA-256 of the file's bytes (1 MiB chunks). MUST match the hash
        scheme the indexer uses (search.py uses hashlib.sha256 + 1 MiB) so a
        baseline hash equals the DB-stored content_hash for that file. Returns
        None on read error. `gen` (a background step): gives up between chunks once the watcher is stopped.
        """
        import hashlib
        try:
            h = hashlib.sha256()
            with open(path, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                    self._check_gen(gen)
                    h.update(chunk)
            return h.hexdigest()
        except _Cancelled:
            raise
        except Exception as e:
            logger.warning(f"_compute_file_hash failed for {path}: {e}")
            return None

    def _capture_baseline(self, folder: str) -> None:
        """Snapshot the SHA-256 of every file currently in `folder` (recursively)
        — but ONLY when the folder's action is Organize New Only (3). Reuses
        the indexer's stored content_hash whenever possible so large media
        folders don't get re-hashed from disk. Stores the resulting set on
        ``self._baseline_hashes[normpath(folder)]``. (start() / add_folder_live do this in the background:
        _compute_baseline; this is the same, here and now.)
        """
        baseline = self._compute_baseline(folder)
        if baseline is not None:
            self._baseline_hashes[os.path.normpath(folder)] = baseline
            self._baseline_names[os.path.normpath(folder)] = set(getattr(baseline, "names", ()))
            if not getattr(baseline, "loaded", True):
                self._baseline_dirty.add(os.path.normpath(folder))
                self._schedule_flush()

    def _merge_look(self, folder: str, baseline: Optional[Set[str]], snapshot) -> None:
        """On the UI thread: what a background look through `folder` found becomes the watcher's state."""
        folder = os.path.normpath(folder)
        if baseline is not None:
            self._baseline_hashes[folder] = baseline
            self._baseline_names[folder] = set(getattr(baseline, "names", ()))
            if not getattr(baseline, "loaded", True):
                self._baseline_dirty.add(folder)                 # (just taken: save it)
                self._schedule_flush()
        paths, arrived = snapshot
        self._processed_files.update(p for p in paths if p not in self._pending_files)
        if arrived:
            logger.info(f"{arrived} file(s) in {folder} arrived while Filect was closed: they count as new")

    def _compute_baseline(self, folder: str, gen: Optional[int] = None) -> Optional[Set[str]]:
        """The Organize New Only baseline for `folder` (see _capture_baseline), or None if the folder isn't
        Organize New Only (or can't be walked). Safe off the UI thread: changes nothing on the watcher."""
        from app.core.settings import settings
        if not self._only_new_files(folder):
            return None

        try:
            from app.core.database import file_index
        except Exception:
            file_index = None

        folder = os.path.normpath(folder)
        since = self._watching_since(folder)
        saved = self._saved_baseline(folder, since)
        if saved is not None:
            b = _Base(saved.get("hashes") or (), saved.get("names") or (), loaded=True)
            logger.info(f"[OrganizeNewOnly] Using the saved snapshot for {folder} ({len(b.names)} file(s) there "
                        "since it was turned on): anything not in it is new")
            return b
        baseline: Set[str] = set()
        names: Set[str] = set()
        hashed_from_disk = 0
        total = 0
        arrived = 0
        known = self._indexed_hashes_under(folder)     # (one query, not one per file: 20,000 took ~20 s)

        try:
            for root, dirs, files in os.walk(folder):
                self._check_gen(gen)
                # Skip hidden subdirs entirely
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fn in files:
                    path = os.path.join(root, fn)
                    if fn.startswith('.') or self._should_ignore(path, folder):
                        continue
                    if _arrived_since(path, since):
                        arrived += 1
                        continue    # arrived after it was turned on (Filect was closed): that's a new file
                    total += 1
                    # Prefer the indexer's stored hash to avoid re-reading
                    # large media files from disk.
                    h: Optional[str] = None
                    if known is not None:
                        h = known.get(_path_key(path))
                    elif file_index is not None:
                        try:
                            row = file_index.get_file_by_path(path)
                            if row:
                                h = row.get('content_hash')
                        except Exception:
                            h = None
                    if not h:
                        h = self._compute_file_hash(path, gen)
                        if h:
                            hashed_from_disk += 1
                    if h:
                        baseline.add(h)
                    names.add(_rel_name(path, folder))
        except OSError as e:
            logger.warning(f"[OrganizeNewOnly] Walk failed for baseline of {folder}: {e}")
            return None

        logger.info(
            f"[OrganizeNewOnly] Captured baseline of {total} existing file(s) "
            f"for {folder} ({hashed_from_disk} hashed from disk, "
            f"{total - hashed_from_disk} reused from DB"
            + (f"; {arrived} created after it was turned on count as new" if arrived else "") + ")"
        )
        return _Base(baseline, names)

    @staticmethod
    def _saved_baseline(folder: str, since: float) -> Optional[dict]:
        """The saved snapshot of `folder`, if it belongs to this watching session (same watching_since)."""
        if not since:
            return None
        e = _read_baselines().get(_path_key(folder))
        if isinstance(e, dict) and e.get("since") == since and isinstance(e.get("names"), list) \
                and isinstance(e.get("hashes"), list):
            return e
        return None

    def _remember(self, folder: str, path: str, h: Optional[str] = None) -> None:
        """A file now belongs to `folder`'s snapshot (sorted by Filect, or recognised as already there)."""
        folder = os.path.normpath(folder)
        if folder not in self._baseline_hashes:
            return                                   # (not an Organize New Only folder)
        self._baseline_names.setdefault(folder, set()).add(_rel_name(path, folder))
        if h:
            self._baseline_hashes[folder].add(h)
        self._baseline_dirty.add(folder)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_pending:
            return
        self._flush_pending = True
        QTimer.singleShot(1500, self._flush_baselines)

    def _flush_baselines(self) -> None:
        """Save the changed snapshots (one small file in the app data folder)."""
        self._flush_pending = False
        if not self._baseline_dirty:
            return
        dirty, self._baseline_dirty = set(self._baseline_dirty), set()
        try:
            from app.core.settings import settings
            listed = {_path_key(f.get("path", "")) for f in (settings.auto_organize_folders or [])}
        except Exception:
            listed = None
        d = _read_baselines()
        if listed is not None:
            d = {k: v for k, v in d.items() if k in listed}      # (folders taken off the list are forgotten)
        for folder in dirty:
            since = self._watching_since(folder)
            if not since or folder not in self._baseline_hashes:
                continue
            d[_path_key(folder)] = {"since": since, "hashes": sorted(self._baseline_hashes[folder]),
                                    "names": sorted(self._baseline_names.get(folder, ()))}
        _write_baselines(d)

    def _watched_root(self, path: str) -> Optional[str]:
        """The watched folder `path` is in (the deepest one), or None."""
        best = None
        for f in self.watched_folders:
            if _is_inside(path, f) and (best is None or len(f) > len(best)):
                best = os.path.normpath(f)
        return best

    @staticmethod
    def _watching_since(folder: str) -> float:
        """When this folder was turned on (settings.auto_organize_watching_since), 0 if unknown."""
        try:
            from app.core.settings import settings
            return float(settings.auto_organize_watching_since(folder) or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _skip_known(folder: str) -> bool:
        """settings.auto_organize_skip_known: an auto-organize day was reverted here."""
        try:
            from app.core.settings import settings
            return bool(settings.auto_organize_skip_known(folder))
        except Exception:
            return False

    @staticmethod
    def _indexed_paths_under(folder: str) -> Optional[Set[str]]:
        """Every path the index holds under `folder` (normcase), in one query. None if it can't be read."""
        import sqlite3
        try:
            from app.core.database import file_index
            prefix = folder.rstrip('\\/') + os.sep
            like = prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            conn = sqlite3.connect(file_index.db_path)
            try:
                rows = conn.execute("SELECT file_path FROM files WHERE file_path LIKE ? ESCAPE '\\'", (like,))
                return {_path_key(r[0]) for r in rows if r[0]}
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Couldn't read the index for {folder}: {e}")
            return None

    @staticmethod
    def _indexed_hashes_under(folder: str) -> Optional[Dict[str, str]]:
        """{path (normcase): content_hash} for every file the index holds under `folder`, in one query.
        None if it can't be read (the caller then asks per file)."""
        import sqlite3
        try:
            from app.core.database import file_index
            prefix = folder.rstrip('\\/') + os.sep
            like = prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            conn = sqlite3.connect(file_index.db_path)
            try:
                rows = conn.execute("SELECT file_path, content_hash FROM files WHERE file_path LIKE ? ESCAPE '\\'",
                                    (like,))
                return {_path_key(p): h for p, h in rows if p and h}
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Couldn't read the index's hashes for {folder}: {e}")
            return None

    def _snapshot_existing(self, folder: str) -> None:
        """Count every file already in `folder` (root AND subfolders) as known, so the periodic
        checker only fires on files added later. Pre-existing files are either
        already organized (sitting in their subfolders) or were left in place
        intentionally — either way the watcher shouldn't keep re-processing them
        every cycle.

        Except a file created after the folder was turned on (watching_since) that
        the index doesn't hold at that path: it arrived while Filect was closed, so
        it's new and gets sorted (without this, Organize New Only never sorted
        anything that came in while the app was closed). Files the watcher already
        sorted are indexed at their new path, so they stay known. A file being
        debounced right now (add_folder_live) is left to the periodic check.
        (start() / add_folder_live do this in the background: _compute_snapshot; this is the same, here and now.)
        """
        self._merge_look(folder, None, self._compute_snapshot(folder))

    def _compute_snapshot(self, folder: str, gen: Optional[int] = None):
        """(paths already in `folder` that count as known, how many arrived while Filect was closed) — see
        _snapshot_existing. Safe off the UI thread: changes nothing on the watcher."""
        folder = os.path.normpath(folder)
        paths: Set[str] = set()
        if not os.path.isdir(folder):
            return paths, 0
        since = self._watching_since(folder)
        saved = self._saved_baseline(folder, since) if self._only_new_files(folder) else None
        saved_names = set(saved.get("names") or ()) if saved is not None else None
        indexed: Optional[Set[str]] = None      # (read once, only if some file is newer than watching_since)
        looked = False
        arrived = 0
        for root, dirs, files in os.walk(folder):
            self._check_gen(gen)
            # Skip hidden subdirs (and apps / code projects) in-place
            self._prune_walk(root, dirs)
            for fn in files:
                path = os.path.normpath(os.path.join(root, fn))
                if self._should_ignore(path, folder):
                    continue
                if saved_names is not None:
                    # The saved snapshot decides: a name in it is already there; anything else arrived while
                    # Filect was closed (or was renamed - the periodic check then knows it by its content).
                    if _rel_name(path, folder) in saved_names:
                        paths.add(path)
                    else:
                        arrived += 1
                    continue
                if _arrived_since(path, since):
                    if not looked:
                        indexed, looked = self._indexed_paths_under(folder), True
                    if indexed is not None and _path_key(path) not in indexed:
                        arrived += 1
                        continue
                paths.add(path)
        return paths, arrived

    def _filter_genuinely_new_files(self, folder: str, candidate_paths: List[str],
                                    hashes: Optional[Dict[str, str]] = None,
                                    known: Optional[Dict[str, Optional[dict]]] = None) -> List[str]:
        """For an Organize New Only folder: return only the candidate paths
        that are genuinely new (their hash is not in the baseline AND the
        file_index doesn't already know their content). Files that are
        recognized as pre-existing are added to ``_left_in_place_paths`` (and
        ``_processed_files`` / ``_organized_files``) so the watcher never
        re-evaluates them this session; if a pre-existing file's hash is
        already known to the DB at a different path, its DB record's path
        is also updated so the index stays consistent after a move/rename.

        Also applies to a folder with ``skip_known`` (an auto-organize day was
        reverted there): a file the index already knows stays where it is, so
        what was put back isn't sorted again. New files still are.
        For other folders, returns ``candidate_paths`` unchanged.
        """
        from app.core.settings import settings
        folder_n = os.path.normpath(folder)
        skip_known = self._skip_known(folder_n)
        if not self._only_new_files(folder_n) and not skip_known:
            return list(candidate_paths)

        baseline = self._baseline_hashes.get(folder_n, set())
        try:
            from app.core.database import file_index
        except Exception:
            file_index = None

        looked_up = known or {}
        kept: List[str] = []
        for path in candidate_paths:
            try:
                # (`hashes`: worked out in the background by the periodic check — a big new download isn't
                # read again here, on the UI thread)
                h = hashes.get(path) if hashes and hashes.get(path) else self._compute_file_hash(path)
            except Exception:
                kept.append(path)  # be lenient — if we can't hash, treat as new
                continue
            # Already there = its content OR its name is in the snapshot (a renamed old file / an old file edited
            # while Filect was closed both stay where they are).
            is_baseline = (bool(h) and h in baseline) or \
                (_rel_name(path, folder_n) in self._baseline_names.get(folder_n, ()))
            known = None
            if not is_baseline and h and path in looked_up:
                known = looked_up[path]          # (looked up in the background, with the hash)
            elif not is_baseline and h and file_index is not None:
                try:
                    known = file_index.get_file_by_hash(h)
                except Exception:
                    known = None

            np = os.path.normpath(path)
            # Baseline match → pre-existing file. Leave in place.
            if is_baseline:
                logger.info(
                    f"[OrganizeNewOnly] '{os.path.basename(path)}' pre-existing (baseline) - leaving in place"
                )
                self._remember(folder_n, np, h)          # (a renamed one: its new name is known from now on)
                self._left_in_place_paths.add(np.lower())
                self._processed_files.add(np)
                self._organized_files.add(np)
                continue

            # DB match. Only treat as pre-existing if the stored path is
            # DIFFERENT from the candidate path — that signals the user
            # moved/renamed a previously-indexed file into the watched
            # folder, which is the case we want to honor (rename detection).
            #
            # If the stored path EQUALS the candidate path, that's almost
            # always a race condition: the watcher just auto-indexed this
            # very file moments ago and the periodic scan came back around
            # to check it. Suppressing it here would lock a genuinely-new
            # file in place forever (seen in the 22:52:02 log for
            # animal-hero-tiger_0.jpg). Treat as new in that case so the
            # AI plan can still route it.
            # Known from somewhere ELSE means you just brought it in (dragged, copied or downloaded again), so it's
            # new to THIS folder. Only a file that was already inside this folder (one you moved around yourself)
            # stays where you put it. (Mac bug 37: a dragged-in video was silently left alone.)
            if known is not None and not _is_inside(known.get('file_path') or '', folder_n):
                logger.info(f"[OrganizeNewOnly] '{os.path.basename(path)}' is known from another folder - "
                            "new to this one")
                known = None
            if known is not None:
                stored = known.get('file_path') or ''
                moved_here = bool(stored) and _path_key(stored) != _path_key(np)
                # skip_known: known at this very path is known too (a file put back by a
                # revert), unless this watcher indexed it itself (the race described above).
                known_here = skip_known and not moved_here and _path_key(np) not in self._indexed_here
                if moved_here or known_here:
                    if moved_here:
                        try:
                            file_index.update_file_path(known['id'], np)
                        except Exception as e:
                            logger.warning(f"[OrganizeNewOnly] Could not update DB path: {e}")
                    logger.info(
                        f"[OrganizeNewOnly] '{os.path.basename(path)}' already known "
                        f"({'moved' if moved_here else 'skip_known'}) - leaving in place"
                    )
                    self._left_in_place_paths.add(np.lower())
                    self._processed_files.add(np)
                    self._organized_files.add(np)
                    continue
                # stored == np → same path → just-indexed by this watcher.
                # Fall through to keep this file as a new candidate.

            kept.append(path)
        return kept

    def _get_existing_folders_if_as_is(self, folder_path: str) -> list:
        """Return existing subfolders (nested too) if folder is in ORGANIZE_AS_IS mode, else None."""
        from app.core.settings import settings
        ORGANIZE_AS_IS = 2
        if settings.get_auto_organize_action(folder_path) == ORGANIZE_AS_IS:
            return self._usable_subfolders(folder_path)
        return None
    
    def _organize_existing_files(self) -> None:
        """Organize files already in the watched folders (including subfolders).

        Folders configured for Organize New Only (action=3) are deliberately
        skipped here — by contract those folders' pre-existing files must
        stay exactly where they are, and only files added AFTER the watcher
        starts get organized (the periodic scan in ``_check_for_new_files``
        handles those via the baseline filter). (start() does this in the background: _collect_existing, then
        _organize_collected; this is the same, here and now.)
        """
        self._organize_collected(self._collect_existing([os.path.normpath(f) for f in self.watched_folders]))

    def _collect_existing(self, folders: List[str], gen: Optional[int] = None) -> Dict[str, tuple]:
        """{folder: (its files to organize, its existing folders if As-Is)} for an organize-existing pass —
        Organize New Only folders left out. Safe off the UI thread: changes nothing on the watcher."""
        from app.core.settings import settings
        ORGANIZE_NEW_ONLY = 3
        out: Dict[str, tuple] = {}
        catch_up = self.catch_up_since
        for folder in folders:
            folder = os.path.normpath(folder)
            if not os.path.isdir(folder):
                continue
            if self._only_new_files(folder):
                logger.info(
                    f"[OrganizeNewOnly] Skipping existing-files pass for {folder} "
                    "(only new files: pre-existing files stay in place)"
                )
                continue

            files_here: List[str] = []
            # Get ALL files in this folder AND subfolders
            for root, dirs, files in os.walk(folder):
                self._check_gen(gen)
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for item in files:
                    item_path = os.path.join(root, item)
                    if self._should_ignore(item_path, folder):
                        continue

                    # Check catch-up filter
                    if catch_up:
                        try:
                            mtime = datetime.fromtimestamp(os.path.getmtime(item_path))
                            if mtime < catch_up:
                                continue  # Skip files older than catch-up time
                        except Exception:
                            pass

                    files_here.append(item_path)
            if files_here:
                out[folder] = (files_here, self._get_existing_folders_if_as_is(folder))
        return out

    def _organize_collected(self, collected: Dict[str, tuple]) -> None:
        """On the UI thread: hand what _collect_existing found to the planner, folder by folder."""
        total = sum(len(files) for files, _ in collected.values())
        if not total:
            self.status_changed.emit("No existing files to organize")
            return

        self.status_changed.emit(f"Organizing {total} existing files...")
        logger.info(f"Found {total} existing files to organize")

        # Process each folder with its instruction
        for folder, (files, existing_folders) in collected.items():
            instruction = self._get_instruction_for_folder(folder)
            self._process_files_with_ai(files, folder, instruction, existing_folders)

    def _organize_existing_files_with_options(self, flatten_first: bool = False) -> None:
        """
        Organize existing files with options.
        Called when instructions are changed while watching. Flattening and looking through the folders
        happen in the background (a big folder froze the window).

        Args:
            flatten_first: If True, flatten folder structure before organizing
        """
        # Clear the catch-up filter so ALL files are processed, not just recent ones
        self.catch_up_since = None
        folders = [os.path.normpath(f) for f in self.watched_folders]
        gen = self._run_gen

        def work():
            flattened = 0
            if flatten_first:
                for folder in folders:
                    self._check_gen(gen)
                    flattened += self.flatten_folder(folder, instruction=self.folder_instructions.get(folder, ''))
            return flattened, self._collect_existing(folders, gen)

        def then(result, error):
            if result is None:
                return
            flattened, collected = result
            if flattened > 0:
                self.status_changed.emit(f"Flattened {flattened} files, now organizing...")
            self._organize_collected(collected)

        self._off_ui(work, then)
    
    def organize_folders_with_per_folder_options(self, folder_choices: Dict[str, int]) -> None:
        """
        Organize folders with per-folder options.
        
        Args:
            folder_choices: Dict mapping folder_path -> choice where choice is:
                1 = REORGANIZE_ALL (flatten + organize)
                2 = ORGANIZE_AS_IS (organize without flatten)
                3 = CONTINUE_WATCHING (skip, just watch for new files)
        """
        # Clear the catch-up filter so ALL files are processed
        self.catch_up_since = None
        
        REORGANIZE_ALL = 1
        ORGANIZE_AS_IS = 2
        CONTINUE_WATCHING = 3
        
        # Separate folders by their chosen action
        folders_to_reorganize = []  # flatten + organize
        folders_to_organize = []     # organize as-is
        
        for folder_path, choice in folder_choices.items():
            folder_path = os.path.normpath(folder_path)
            if not os.path.isdir(folder_path):
                continue
                
            if choice == REORGANIZE_ALL:
                folders_to_reorganize.append(folder_path)
            elif choice == ORGANIZE_AS_IS:
                folders_to_organize.append(folder_path)
            # CONTINUE_WATCHING = do nothing for this folder
        
        # Steps 1-2 walk (and, for Re-organize All, move) every file: in the background (a big folder froze the
        # window), then the planner gets each folder's files.
        all_folders_to_organize = folders_to_reorganize + folders_to_organize
        if not all_folders_to_organize:
            self.status_changed.emit("No folders selected for organizing")
            return
        gen = self._run_gen

        def work():
            # Step 1: Flatten folders that need it
            total_flattened = 0
            for folder in folders_to_reorganize:
                self._check_gen(gen)
                total_flattened += self.flatten_folder(folder, instruction=self.folder_instructions.get(folder, ''))

            # Step 2: Collect files from selected folders and track existing subfolders for "Organize As-Is"
            existing_folders_by_parent: Dict[str, List[str]] = {}  # folder -> list of existing subfolders
            for folder in folders_to_organize:  # Only for "Organize As-Is" folders
                folder = os.path.normpath(folder)
                # Pre-create folders the user explicitly named in their instruction
                # (e.g. "put everything else in a folder called everything else").
                # Without this, As-Is mode tells the AI "ONLY use existing folders"
                # but those user-named folders DON'T exist yet — the AI then
                # proposes them anyway and the fuzzy-match fallback can dump
                # files into the wrong place. Pre-creating closes that gap.
                try:
                    instr = self._get_instruction_for_folder(folder) or ''
                    if instr:
                        self._ensure_named_folders_exist(folder, instr)
                except Exception as e:
                    logger.debug(f"As-Is: could not pre-create user-named folders for {folder}: {e}")
                existing_subfolders = self._usable_subfolders(folder)
                existing_folders_by_parent[folder] = existing_subfolders
                logger.info(f"Organize As-Is for {folder}: existing folders = {existing_subfolders}")

            files_by_folder: Dict[str, List[str]] = defaultdict(list)
            for folder in all_folders_to_organize:
                folder = os.path.normpath(folder)
                if not os.path.isdir(folder):
                    continue
                # Get ALL files in this folder AND subfolders
                for root, dirs, files in os.walk(folder):
                    self._check_gen(gen)
                    # Skip hidden directories
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for item in files:
                        item_path = os.path.join(root, item)
                        if self._should_ignore(item_path, folder):
                            continue
                        files_by_folder[folder].append(item_path)
            return total_flattened, existing_folders_by_parent, dict(files_by_folder)

        def then(result, error):
            if result is None:
                return
            total_flattened, existing_folders_by_parent, files_by_folder = result
            if total_flattened > 0:
                self.status_changed.emit(
                    f"Flattened {total_flattened} files from {len(folders_to_reorganize)} folder(s)...")
            total = sum(len(f) for f in files_by_folder.values())
            if not total:
                self.status_changed.emit("No files to organize in selected folders")
                return
            self.status_changed.emit(f"Organizing {total} files from {len(all_folders_to_organize)} folder(s)...")
            logger.info(f"Per-folder organize: {total} files from {len(all_folders_to_organize)} folders")
            # Process each folder with its instruction
            for folder, files in files_by_folder.items():
                instruction = self._get_instruction_for_folder(folder)
                # Pass existing folders for "Organize As-Is" folders
                existing_folders = existing_folders_by_parent.get(folder)
                self._process_files_with_ai(files, folder, instruction, existing_folders)

        self._off_ui(work, then)

    def organize_single_folder(self, folder_path: str, flatten_first: bool = False) -> None:
        """
        Organize a single folder with the given option. Flattening and looking through the folder happen in the
        background (a big folder froze the window); then the planner gets its files.

        Args:
            folder_path: Path to the folder to organize
            flatten_first: If True, flatten folder structure before organizing.
                          If False (Organize As-Is), only use existing folders.
        """
        folder_path = os.path.normpath(folder_path)

        if not os.path.isdir(folder_path):
            logger.warning(f"Cannot organize - not a directory: {folder_path}")
            return

        # Clear the catch-up filter so ALL files are processed
        self.catch_up_since = None

        logger.info(f"Organizing single folder: {folder_path}, flatten={flatten_first}")
        gen = self._run_gen
        instr_now = self.folder_instructions.get(folder_path, '') or ''

        def work():
            # Step 1: Flatten if requested
            count = 0
            if flatten_first:
                count = self.flatten_folder(folder_path, instruction=instr_now)

            # Step 2: Get existing folder structure (for "Organize As-Is" mode).
            # Includes nested subfolders (up to a sensible depth) so the AI can route
            # files directly into Photos/Vacation/ instead of just Photos/.
            existing_folders = []
            if not flatten_first:
                # Pre-create folders the user explicitly named in their instruction
                # so the AI sees them in the existing-folders list. Without this,
                # As-Is tells the AI "ONLY use existing folders" but the user-named
                # destinations (e.g. "everything else") don't exist yet — the AI
                # then invents them anyway and the fuzzy-match fallback can dump
                # files into the wrong place (the bug seen on 2026-05-29 01:29).
                try:
                    if instr_now:
                        self._ensure_named_folders_exist(folder_path, instr_now)
                except Exception as e:
                    logger.debug(f"As-Is: could not pre-create user-named folders for {folder_path}: {e}")
                existing_folders = self._usable_subfolders(folder_path)
                logger.info(f"Organize As-Is mode: existing folders = {existing_folders}")

            # Step 3: Collect files from this folder
            all_files = []
            for root, dirs, files in os.walk(folder_path):
                self._check_gen(gen)
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for item in files:
                    item_path = os.path.join(root, item)
                    if self._should_ignore(item_path, folder_path):
                        continue
                    all_files.append(item_path)
            return count, existing_folders, all_files

        def then(result, error):
            if result is None:
                return
            count, existing_folders, all_files = result
            if count > 0:
                self.status_changed.emit(f"Flattened {count} files, now organizing...")
            if not all_files:
                self.status_changed.emit(f"No files to organize in {os.path.basename(folder_path)}")
                return
            self.status_changed.emit(f"Organizing {len(all_files)} files in {os.path.basename(folder_path)}...")
            logger.info(f"Single folder organize: {len(all_files)} files in {folder_path}")
            # Process files with AI
            instruction = self._get_instruction_for_folder(folder_path)
            use_existing_only = not flatten_first  # Organize As-Is = only use existing folders
            self._process_files_with_ai(all_files, folder_path, instruction,
                                        existing_folders if use_existing_only else None)

        self._off_ui(work, then)

    def _check_for_new_files(self) -> None:
        """Periodic check for new files in watched folders — here and now (tests; the timer runs the same check
        with its walk in the background: _on_check_timer)."""
        if not self._is_running:
            return
        plan = self._check_plan()
        self._after_check(self._walk_for_new(plan, self._run_gen))

    def _on_check_timer(self) -> None:
        """Every 3 seconds: walk the watched folders in the background (a 20,000-file folder took 0.3-0.5 s on
        the UI thread, every time), then decide what's new here, on the UI thread."""
        if not self._is_running or self._tick_busy:
            return
        plan = self._check_plan()
        self._tick_busy = True
        gen = self._run_gen

        def then(result, error):
            self._tick_busy = False
            if result is not None:
                self._after_check(result)

        self._off_ui(lambda: self._walk_for_new(plan, gen), then)

    def _check_plan(self) -> dict:
        """On the UI thread: what this check looks at (a copy, so the walk can run elsewhere)."""
        # Periodic cleanup of empty folders. Each watched folder's user
        # instruction may explicitly name destinations (e.g. "in a folder
        # called bears"). Those folders are PROTECTED from deletion even
        # when empty — the user just told us they want them. Without this,
        # an As-Is run can pre-create bears/tigers/everything-else, the AI
        # might place 0 files in one of them (if no files of that subject
        # exist this run), and the next cleanup tick would silently delete
        # the folder the user asked for. Saw this at 2026-05-29 02:00:25.
        cleanup = None
        self._check_count += 1
        if self._check_count >= self._cleanup_interval:
            self._check_count = 0
            cleanup = {}
            for folder in self.watched_folders:
                folder = os.path.normpath(folder)
                protected = set()
                try:
                    instr = self._get_instruction_for_folder(folder) or ''
                    if instr:
                        for n in self._extract_named_folders(instr):
                            protected.add(n.lower())
                except Exception as e:
                    logger.debug(f"Periodic cleanup: could not derive protected names for {folder}: {e}")
                cleanup[folder] = protected
        # (a folder still being looked through isn't checked yet: everything in it would look new)
        folders = [os.path.normpath(f) for f in self.watched_folders if _path_key(f) not in self._preparing]
        hash_for = {f: self._needs_hash(f) for f in folders}
        return {"folders": folders, "cleanup": cleanup, "pending": dict(self._pending_files), "now": time.time(),
                "hash_for": hash_for, "busy": self._busy_keys()}

    def _busy_keys(self) -> set:
        """Files a batch is sorting right now, or that wait for one in the queue. Without this, a big batch's files
        were found "new" again at every check while it ran — re-hashed, looked up and re-queued every 3 s."""
        keys = set()
        if self._current_worker is not None and self._current_worker.isRunning():
            keys.update(_path_key(p) for p in self._current_batch)
        for item in self._worker_queue:
            if len(item) >= 4 and item[3]:
                keys.update(_path_key(p) for p in item[3])
        return keys

    @staticmethod
    def _only_new_files(folder: str) -> bool:
        """The files already in `folder` stay where they are: "only new files" (action 3), or "only the folders
        already there" (2) set up by voice (its `only_new` flag). Never Re-organize All (1). (Mac fix 6.)"""
        from app.core.settings import settings
        try:
            entry = settings._auto_organize_entry(folder)          # (compared the way Windows does: no case)
            action = entry.get('action', 3) if entry else settings.get_auto_organize_action(folder)
            return action == 3 or (action == 2 and bool(entry and entry.get('only_new')))
        except Exception:
            return False

    def _ready(self, path: str) -> bool:
        """Not being written any more (Mac fix 2, Windows way): changed or created in the last STILL_WRITING_SECS,
        or a different size / time than the last check, = still being written: it waits for a later check. (On
        Windows st_ctime is the CREATION time, so a write is seen by st_mtime + the size between checks; a program
        that sets the date back is still caught by the size growing, and a fresh file by its creation time.)"""
        try:
            st = os.stat(path)
        except OSError:
            return False
        now = time.time()
        key = _path_key(path)
        seen, self._sizes[key] = self._sizes.get(key), (st.st_size, st.st_mtime)
        if now - st.st_mtime < STILL_WRITING_SECS or now - st.st_ctime < STILL_WRITING_SECS:
            return False
        return seen is not None and seen == (st.st_size, st.st_mtime)

    def _needs_hash(self, folder: str) -> bool:
        """New files here are told apart from known ones by their content (Organize New Only, skip_known)."""
        from app.core.settings import settings
        try:
            return self._only_new_files(folder) or self._skip_known(folder)
        except Exception:
            return False

    def _walk_for_new(self, plan: dict, gen: Optional[int] = None) -> dict:
        """The heavy part of a check, safe off the UI thread (reads the watcher's sets, changes nothing on it):
        the empty-folder cleanup when it's due, then every file not known yet, per folder; and, for a file that
        has waited out its debounce in a folder that needs it, its content hash (a big new download is read
        here, not on the UI thread)."""
        if plan["cleanup"]:
            for folder, protected in plan["cleanup"].items():
                self._check_gen(gen)
                if not os.path.isdir(folder):
                    continue
                deleted = self._cleanup_empty_folders(folder, protected_names=protected)
                if deleted > 0:
                    logger.info(
                        f"Periodic cleanup: removed {deleted} empty folder(s) "
                        f"(protected: {sorted(protected) if protected else 'none'})"
                    )
        found: Dict[str, List[str]] = {}
        hashes: Dict[str, str] = {}
        known: Dict[str, Optional[dict]] = {}    # (path -> what the index knows about that content: looked up HERE,
        try:                                     #  not per file on the UI thread — 200 lookups at once stalled it)
            from app.core.database import file_index
        except Exception:
            file_index = None
        busy = plan.get("busy") or set()
        pending, now = plan["pending"], plan["now"]
        for folder in plan["folders"]:
            if not os.path.isdir(folder):
                continue
            paths: List[str] = []
            try:
                # Walk the WHOLE tree (root + subfolders) so a file dropped
                # into a subfolder is also detected. Files that existed when
                # the watcher started were added to _processed_files in
                # start(); files we've organized are tracked in
                # _organized_files. Anything outside both sets is genuinely
                # new and should be routed by the current instruction.
                for root, dirs, files in os.walk(folder):
                    self._check_gen(gen)
                    # Hidden folders, apps and code projects: never walked (nothing in them moves)
                    self._prune_walk(root, dirs)
                    for item in files:
                        item_path = os.path.normpath(os.path.join(root, item))

                        # Organize-New-Only loop suppression: once a file has
                        # been declared "leave in place" this session, skip it
                        # immediately — no hashing, no DB lookup, no nothing.
                        # (Cheap set lookups first: this runs on every file,
                        # every 3 seconds.)
                        if item_path.lower() in self._left_in_place_paths:
                            continue
                        if item_path in self._processed_files:
                            continue
                        if item_path in self._organized_files:
                            continue
                        # Being sorted right now, or waiting in the queue: not new again at every check
                        if _path_key(item_path) in busy:
                            continue
                        # Full path: pinned files, and anything inside an app / code project
                        if self._should_ignore(item_path, folder):
                            continue
                        paths.append(item_path)
                        first_seen = pending.get(item_path)
                        if plan["hash_for"].get(folder) and first_seen is not None and \
                                now - first_seen >= self._debounce_seconds:
                            h = self._compute_file_hash(item_path, gen)
                            if h:
                                hashes[item_path] = h
                                if file_index is not None:
                                    try:
                                        known[item_path] = file_index.get_file_by_hash(h)
                                    except Exception:
                                        pass
            except _Cancelled:
                raise
            except Exception as e:
                logger.error(f"Error checking folder {folder}: {e}")
            found[folder] = paths
        return {"found": found, "hashes": hashes, "known": known, "now": now}

    def _after_check(self, result: dict) -> None:
        """On the UI thread: what the walk found → debounce, the Organize New Only filter, the planner."""
        if not self._is_running:
            return
        current_time = result["now"]
        watched = {_path_key(f) for f in self.watched_folders}
        busy = self._busy_keys()
        for folder, paths in result["found"].items():
            if _path_key(folder) not in watched:      # (turned off while the walk ran)
                continue
            try:
                ready = []
                for item_path in paths:
                    # (dealt with while the walk ran; or a batch took it on meanwhile)
                    if item_path.lower() in self._left_in_place_paths or item_path in self._processed_files \
                            or item_path in self._organized_files or _path_key(item_path) in busy:
                        continue
                    retry = self._retries.get(_path_key(item_path))
                    if retry and current_time < retry[1]:
                        continue                         # (its plan / move failed: waits for its next try)
                    # Track pending files for debounce
                    if item_path not in self._pending_files:
                        self._pending_files[item_path] = current_time
                        self._ready(item_path)               # (its size now: the next check compares)
                        logger.debug(f"New file detected: {item_path}")
                    elif current_time - self._pending_files[item_path] >= self._debounce_seconds:
                        if self._ready(item_path):
                            ready.append(item_path)      # (stable long enough, and not being written)
                    else:
                        self._ready(item_path)           # (remember its size: the next check compares)
                # Open in another program (Word, a recorder, a download still running): it stays pending -- checked
                # again every few seconds for free -- and is planned only once it can actually be moved.
                still_open = [x for x in ready if not _can_move(x)]
                if still_open:
                    for x in still_open:
                        if _path_key(x) not in self._waiting_busy:
                            self._waiting_busy.add(_path_key(x))
                            logger.info(f"[AutoOrganize] {os.path.basename(x)} is open in another program: "
                                        f"waiting until it's closed")
                    ready = [x for x in ready if x not in still_open]
                for x in ready:
                    self._waiting_busy.discard(_path_key(x))
                if not ready:
                    continue
                for item_path in ready:
                    self._pending_files.pop(item_path, None)
                # Organize-New-Only filter (no-op for other modes): drop pre-existing files (baseline hash match OR
                # known by content_hash in DB) and leave them in place forever.
                to_process = self._filter_genuinely_new_files(folder, ready, hashes=result["hashes"],
                                                              known=result.get("known"))
                if to_process:
                    # ONE batch for everything that's ready in this folder. (It went one file per batch: 200
                    # downloads landing at once took ~5 s each, one after another — measured 2026-11-10, 134 of
                    # 200 still waiting after 7 minutes. A batch reads its files several at a time.)
                    instruction = self._get_instruction_for_folder(folder)
                    existing_folders = self._get_existing_folders_if_as_is(folder)
                    self._process_files_with_ai(to_process, folder, instruction, existing_folders)
            except Exception as e:
                logger.error(f"Error checking folder {folder}: {e}")

    def _process_files_with_ai(self, file_paths: List[str], folder: str, instruction: str, 
                                existing_folders: List[str] = None) -> None:
        """
        Process files using AI to determine organization.
        
        Uses a background worker thread to prevent UI lag.
        
        Args:
            file_paths: List of file paths to organize
            folder: The destination folder (same as source folder for watch mode)
            instruction: User's organization instruction
            existing_folders: If provided, AI must ONLY use these folders (Organize As-Is mode)
        """
        if not file_paths:
            return
        
        # Build full instruction for AI
        parent_folder_name = os.path.basename(folder).lower()
        
        if existing_folders is not None and len(existing_folders) > 0:
            # ORGANIZE AS-IS MODE: Only use existing folders
            full_instruction = existing_folders_instruction(instruction, existing_folders)
            logger.info(f"[Worker] Organize As-Is mode: restricting to folders: {existing_folders}")
        elif instruction:
            full_instruction = (
                f"[AUTO-ORGANIZE] User's specific instructions: {instruction}\n\n"
                f"PARENT FOLDER NAME: '{parent_folder_name}' - DO NOT create a folder with this name!\n\n"
                "RULES FOR AUTO-ORGANIZE MODE:\n"
                "1. CRITICAL — TAGS ARE AUTHORITATIVE: classify each file by its tags FIRST, the filename is only a fallback. "
                "If a file's tags contain a topic that matches a user-named folder (e.g. tags include 'lion' and the user "
                "named a 'lions' folder), the file MUST go to that folder, even if the filename does not obviously suggest it. "
                "A photo whose filename is 'hassan-pond.jpg' but whose tags include 'lion' is a LION photo and belongs in the "
                "lions folder.\n"
                "2. STRICT: Files of any TYPE/SUBJECT the user explicitly mentioned MUST go to the folder the user named for "
                "that subject. No exceptions, no alternative folders. If the user said 'videos to Clips', EVERY video file "
                "goes to 'Clips' — do NOT route any video to a different folder (no 'media', no 'videos', no 'Clips_2').\n"
                "3. ONLY for files whose tags/subject the user did NOT mention may you create a new, meaningful folder by "
                "content type (e.g. 'documents' for text/PDFs, 'audio' for music, 'code' for source files). This permission "
                "applies STRICTLY to subjects the user did not name in their instruction — it is NOT a license to invent extra "
                "folders for files the user already routed.\n"
                "4. FORBIDDEN folder names (catch-all junk drawers): 'misc', 'other', 'unsorted', 'miscellaneous', 'random', "
                "'stuff', 'various', 'etc'. Never use these names. If a file truly fits nowhere meaningful, use a plain "
                "folder for its content type ('documents', 'photos', 'videos').\n"
                "4b. A file the user's instruction does NOT cover never goes in a folder the user named for something "
                "else: a video, a recipe or a photo is not a bill, so it never goes in the folder named for bills.\n"
                f"5. IMPORTANT: Do NOT create a folder named '{parent_folder_name}' - use a different name.\n"
                "6. Every file MUST be placed in exactly one folder."
            )
        else:
            full_instruction = (
                f"[AUTO-ORGANIZE] Organize ALL files into logical folders based on file type and content.\n"
                f"PARENT FOLDER NAME: '{parent_folder_name}' - DO NOT create a folder with this name!\n\n"
                "Use clear folder names like 'photos', 'docs', 'videos', 'audio', 'misc'.\n"
                f"IMPORTANT: Do NOT create a folder named '{parent_folder_name}' since that's the parent folder.\n"
                "EVERY file MUST be placed in a folder - NO files left out."
            )
        
        # If a worker is already running, queue this request.
        # Store BOTH the folder context AND the explicit file_paths the caller
        # collected. On dequeue, if the queued file_paths are valid we use
        # them directly — otherwise we fall back to re-scanning. Without the
        # explicit paths, a user-initiated Reorganize-All / As-Is request
        # that gets queued (because another folder's worker is running)
        # would re-scan via _scan_folder_for_files, which filters against the
        # start-time snapshot — and so finds "0 new files" and silently does
        # nothing. That's the bug we hit at 2026-05-29 02:00 where the As-Is
        # request queued behind the auto-organizing run and never executed.
        if self._current_worker is not None and self._current_worker.isRunning():
            folder_key = _path_key(folder)
            for i, item in enumerate(self._worker_queue):
                if _path_key(item[0]) != folder_key:
                    continue
                queued_paths = list(item[3]) if len(item) >= 4 and item[3] else []
                if queued_paths:
                    # Already waiting with its own files: these join them. (They were dropped as a "duplicate",
                    # and each was found again — and sorted — on its own, later.)
                    seen = {_path_key(p) for p in queued_paths}
                    more = [p for p in file_paths if _path_key(p) not in seen]
                    self._worker_queue[i] = (item[0], full_instruction, existing_folders, queued_paths + more)
                    logger.info(f"Folder {os.path.basename(folder)} already queued: {len(more)} more file(s) join it "
                                f"({len(queued_paths) + len(more)} waiting)")
                else:                            # (a re-scan is queued: it finds these too)
                    logger.info(f"Folder {os.path.basename(folder)} already queued (re-scan), skipping duplicate")
                return
            logger.info(f"Worker busy, queuing folder {os.path.basename(folder)} for later processing")
            self._worker_queue.append((folder, full_instruction, existing_folders, list(file_paths)))
            return
        
        # Create and start worker thread
        self._start_worker(file_paths, folder, full_instruction, existing_folders)
    
    def _start_worker(self, file_paths: List[str], folder: str, instruction: str, 
                       existing_folders: List[str] = None) -> None:
        """Start a background worker to process files."""
        logger.info(f"Starting background worker for {len(file_paths)} files")
        self._current_batch = list(file_paths)
        old = self._current_worker
        if old is not None and old.isRunning():
            # Its result arrived a moment before its thread ended. Replacing the last reference to a running QThread
            # crashes ("QThread: Destroyed while thread is still running"): keep it until it has ended (Mac fix 8).
            old.setParent(self)
            self._old_workers.append(old)
        self._old_workers = [w for w in self._old_workers if w.isRunning()]

        self._current_worker = AutoWatcherWorker(
            file_paths, folder, instruction, self.folder_instructions, existing_folders
        )
        
        # Connect worker signals to our signals
        self._current_worker.file_indexed.connect(self._on_worker_file_indexed)
        self._current_worker.file_organized.connect(self._on_worker_file_organized)
        self._current_worker.status_changed.connect(self._on_worker_status)
        self._current_worker.error_occurred.connect(self._on_worker_error)
        self._current_worker.limit_reached.connect(self._on_worker_limit_reached)
        self._current_worker.not_done.connect(self._on_worker_not_done)
        self._current_worker.busy.connect(self._on_worker_busy)
        self._current_worker.finished_processing.connect(self._on_worker_finished_with_files)
        
        self._current_worker.start()
    
    def _on_worker_file_indexed(self, file_path: str):
        """Handle file indexed from worker."""
        self._indexed_here.add(_path_key(file_path))
        self.file_indexed.emit(file_path)
    
    def _on_worker_file_organized(self, source: str, dest: str, category: str):
        """Handle file organized from worker."""
        self._indexed_here.discard(_path_key(source))   # (put back there by a revert, it's a known file)
        self._retries.pop(_path_key(source), None)
        self._waiting_busy.discard(_path_key(source))
        self._processed_files.add(source)
        self._processed_files.add(dest)
        # Track the destination as an organized file (prevents re-processing)
        self._organized_files.add(os.path.normpath(dest))
        root = self._watched_root(dest)
        if root:
            self._remember(root, dest)           # (Organize New Only: sorted = already there from now on)
        self.file_organized.emit(source, dest, category)
    
    def _on_worker_status(self, status: str):
        """Handle status update from worker."""
        self.status_changed.emit(status)
    
    def _on_worker_error(self, file_path: str, error: str):
        """Handle error from worker."""
        self.error_occurred.emit(file_path, error)

    def _on_worker_not_done(self, paths: list, why: str):
        """Files the batch couldn't plan or move: tried again after 1, 5 and 30 min, then left until the next start."""
        now = time.time()
        later, given_up = [], []
        for p in paths:
            k = _path_key(p)
            tries = self._retries.get(k, (0, 0.0))[0] + 1
            if tries <= len(RETRY_AFTER):
                self._retries[k] = (tries, now + RETRY_AFTER[tries - 1])
                later.append(p)
            else:
                self._retries.pop(k, None)
                self._organized_files.add(os.path.normpath(p))        # (left where it is until the next start)
                given_up.append(p)
        if later:
            wait = RETRY_AFTER[self._retries[_path_key(later[0])][0] - 1]
            when = f"{wait // 60} min" if wait >= 60 else f"{wait} s"
            logger.warning(f"[AutoOrganize] {len(later)} file(s) not sorted ({why}): trying again in {when}: "
                           f"{[os.path.basename(p) for p in later[:10]]}")
            self.status_changed.emit(f"Couldn't sort {len(later)} file(s) just now ({why}). Trying again in {when}.")
        if given_up:
            logger.warning(f"[AutoOrganize] {len(given_up)} file(s) still not sorted after {len(RETRY_AFTER)} tries: "
                           f"left where they are until Filect starts again: "
                           f"{[os.path.basename(p) for p in given_up[:10]]}")
            self.status_changed.emit(f"{len(given_up)} file(s) couldn't be sorted. Left where they are.")

    def _on_worker_busy(self, paths: list):
        """Open in another program: left untouched; sorted once the program lets go (no AI call meanwhile)."""
        for p in paths:
            self._waiting_busy.add(_path_key(p))
        names = ", ".join(os.path.basename(p) for p in paths[:3]) + ("..." if len(paths) > 3 else "")
        self.status_changed.emit(f"{names} {'is' if len(paths) == 1 else 'are'} open in another program. "
                                 f"Sorted once closed.")

    def _on_worker_limit_reached(self, info: dict):
        """Forward the index-limit signal so the UI can show the upgrade popup."""
        self.limit_reached.emit(info or {})

    def _on_worker_finished_with_files(self, processed_files: list):
        """Handle worker finished with list of processed files."""
        w = self.sender()
        if w is not None:
            w._reported_done = True               # (its thread may still be ending: the next job may start now)
        # Add all processed files to _organized_files to prevent re-processing
        for file_path in processed_files:
            self._organized_files.add(os.path.normpath(file_path))
        
        logger.info(f"Worker finished, marked {len(processed_files)} files as organized")
        
        # Continue with regular finish handling
        self._on_worker_finished()
    
    def _on_worker_finished(self):
        """Handle worker finished - process queue if any."""
        logger.info("Worker finished processing")
        
        # Process next item in queue if any
        if self._worker_queue:
            queued = self._worker_queue.pop(0)
            # Backwards-compatible unpack — old queue entries were 3-tuples,
            # new ones are 4-tuples with explicit file_paths.
            if len(queued) >= 4:
                folder, instruction, existing_folders, queued_paths = queued[:4]
            else:
                folder, instruction, existing_folders = queued
                queued_paths = None

            if queued_paths:
                # User-initiated request (Reorganize-All / As-Is). Use the
                # explicit paths the caller collected. Filter to ones still on
                # disk in case another worker moved them.
                #
                # Deliberately do NOT filter by self._organized_files here:
                # the user just clicked Save and wants EVERY file in this
                # folder reprocessed against the latest instruction. Files
                # processed in an earlier Save session are still in
                # _organized_files (it's session-wide, not per-run), and
                # dropping them now would silently skip them — exactly the
                # bug seen on 2026-05-29 02:05 where the shutterstock bear
                # file stayed put in animals/Bears because a previous Save
                # had moved it once. The periodic-scan dequeue branch below
                # still filters by _organized_files because that path is
                # "detect changes since last tick" rather than user intent.
                file_paths = [p for p in queued_paths if os.path.isfile(p)]
                if file_paths:
                    logger.info(
                        f"Processing queued request: {len(file_paths)} "
                        f"explicit file(s) in {os.path.basename(folder)}"
                    )
            else:
                # Periodic / detection-driven request. Re-scan so we pick up
                # files added since the queue entry was created — walked (and hashed, where it's needed) in
                # the background: on a big folder this froze the window each time a batch finished.
                gen = self._run_gen
                needs_hash = self._needs_hash(folder)

                def rescan():
                    found = self._scan_folder_for_files(folder, exclude_organized=True)
                    hashes = {}
                    if needs_hash:
                        for p in found:
                            h = self._compute_file_hash(p, gen)
                            if h:
                                hashes[p] = h
                    return found, hashes

                def then(result, error):
                    found, hashes = result if result is not None else ([], {})
                    # Organize-New-Only baseline + DB filter — same path the
                    # periodic scan uses. No-op for non-action-3 folders.
                    paths = self._filter_genuinely_new_files(folder, found, hashes=hashes)
                    if paths:
                        logger.info(f"Processing queued request: {len(paths)} NEW files in {os.path.basename(folder)}")
                    self._continue_queue(folder, instruction, existing_folders, paths)

                self._off_ui(rescan, then)
                return

            self._continue_queue(folder, instruction, existing_folders, file_paths)
        else:
            # All done - update status
            self.status_changed.emit(f"Watching {len(self.watched_folders)} folder(s) for new files...")

    def _continue_queue(self, folder, instruction, existing_folders, file_paths) -> None:
        """A dequeued request's files are ready: start its batch, or move on to the next one."""
        if (file_paths and self._current_worker is not None and self._current_worker.isRunning()
                and not getattr(self._current_worker, "_reported_done", False)):
            # (another batch started while this one was being looked through: it waits its turn. One that already
            # reported its result is only ending: waiting for it here stalled the queue for good — Mac fix 8)
            self._worker_queue.insert(0, (folder, instruction, existing_folders, list(file_paths)))
            return

        if file_paths:
            self._start_worker(file_paths, folder, instruction, existing_folders)
        else:
            logger.info(f"Queued folder {folder} has no NEW files to process (all already organized)")
            # Clear remaining queue for same folder to prevent loops
            self._worker_queue = [
                item for item in self._worker_queue
                if os.path.normpath(item[0]) != os.path.normpath(folder)
            ]
            # Process next in queue if any remain
            if self._worker_queue:
                self._on_worker_finished()
            else:
                self.status_changed.emit(f"Watching {len(self.watched_folders)} folder(s) for new files...")
    
    def _scan_folder_for_files(self, folder: str, exclude_organized: bool = False) -> List[str]:
        """
        Scan a folder and return list of file paths.
        Used when processing queued requests to get fresh file paths.

        Args:
            folder: The folder to scan
            exclude_organized: If True, exclude files already in _organized_files

        IMPORTANT: This is the queue-dequeue path. It MUST apply the same
        "not new" filters as the periodic scan in ``_check_for_new_files``,
        otherwise pre-existing files in subfolders (captured by the
        start()-time ``_processed_files`` snapshot, or marked
        leave-in-place by Organize New Only) get re-walked here and end up
        in the next worker batch. That manifests as "the watcher organized
        files that were already sitting in subfolders before I clicked
        Start" — exactly the v12.2.2 regression seen in the log at
        22:40:54 where everything-else/* and tiggers/regal-bengal-tiger
        were reorganized despite being in the baseline.
        """
        folder = os.path.normpath(folder)
        file_paths = []

        if not os.path.isdir(folder):
            return file_paths

        for root, dirs, files in os.walk(folder):
            # Skip hidden directories (and apps / code projects: nothing in them moves)
            self._prune_walk(root, dirs)

            for item in files:
                item_path = os.path.join(root, item)
                if self._should_ignore(item_path, folder):
                    continue
                normalized_path = os.path.normpath(item_path)

                # Files captured by the start()-time snapshot were there
                # before watching began; never let them through the queue
                # path either.
                if normalized_path in self._processed_files:
                    continue
                # Organize-New-Only loop suppression: once a file has been
                # declared "leave in place" this session, skip it here too.
                if normalized_path.lower() in self._left_in_place_paths:
                    continue
                # Skip files that have already been organized (prevents loops).
                if exclude_organized and normalized_path in self._organized_files:
                    continue

                file_paths.append(item_path)

        return file_paths
    
    def _execute_plan(self, plan: Dict, files_by_id: Dict, dest_folder: str) -> None:
        """Execute the organization plan by moving files."""
        folders = plan.get('folders', {})
        
        if not folders:
            logger.info("Plan has no folders")
            return
        
        moved_count = 0
        error_count = 0
        dest_folder = os.path.normpath(dest_folder)
        
        for folder_name, file_ids in folders.items():
            # Create destination subfolder
            target_folder = os.path.join(dest_folder, folder_name)
            
            for file_id in file_ids:
                try:
                    # Handle string IDs from AI
                    file_id_int = int(file_id)
                    file_info = files_by_id.get(file_id_int)
                    
                    if not file_info:
                        logger.warning(f"File ID not found: {file_id}")
                        continue
                    
                    source_path = file_info['file_path']
                    file_name = file_info['file_name']
                    
                    if not os.path.exists(source_path):
                        logger.warning(f"Source file no longer exists: {source_path}")
                        continue
                    
                    # Create target folder if needed
                    os.makedirs(target_folder, exist_ok=True)
                    
                    dest_path = os.path.join(target_folder, file_name)
                    
                    # Handle name conflicts
                    if os.path.exists(dest_path) and os.path.normpath(source_path) != os.path.normpath(dest_path):
                        base, ext = os.path.splitext(file_name)
                        counter = 1
                        while os.path.exists(dest_path):
                            dest_path = os.path.join(target_folder, f"{base} ({counter}){ext}")
                            counter += 1
                    
                    # Skip if source and dest are the same
                    if os.path.normpath(source_path) == os.path.normpath(dest_path):
                        logger.debug(f"File already in place: {source_path}")
                        self._processed_files.add(source_path)
                        continue
                    
                    # Move the file (never a half copy of a file open elsewhere)
                    move_file(source_path, dest_path)
                    moved_count += 1
                    
                    # Track as processed
                    self._processed_files.add(source_path)
                    self._processed_files.add(dest_path)
                    
                    logger.info(f"Organized: {source_path} -> {dest_path}")
                    _root = self._watched_root(dest_path)
                    if _root:
                        self._remember(_root, dest_path)
                    self.file_organized.emit(source_path, dest_path, folder_name)
                    
                    # Update database path using actual DB ID (not sequential ID)
                    from app.core.database import file_index
                    db_id = file_info.get('db_id')
                    if db_id:
                        file_index.update_file_path(db_id, dest_path)
                        logger.debug(f"Updated DB path for file {db_id}: {dest_path}")
                    else:
                        # File wasn't in DB yet - try to find by old path and update
                        old_record = file_index.get_file_by_path(source_path)
                        if old_record:
                            file_index.update_file_path(old_record['id'], dest_path)
                            logger.debug(f"Updated DB path for file {old_record['id']}: {dest_path}")
                    
                except Exception as e:
                    error_count += 1
                    logger.error(f"Error moving file {file_id}: {e}")
                    self.error_occurred.emit(str(file_id), str(e))
        
        # Clean up empty folders
        if moved_count > 0:
            deleted_folders = self._cleanup_empty_folders(dest_folder)
            if deleted_folders > 0:
                logger.info(f"Deleted {deleted_folders} empty folder(s)")
            
            self.status_changed.emit(f"Organized {moved_count} file(s)" + 
                                     (f" ({error_count} errors)" if error_count else ""))
        elif error_count > 0:
            self.status_changed.emit(f"Organization failed: {error_count} error(s)")
    