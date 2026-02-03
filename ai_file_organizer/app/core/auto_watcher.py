"""
Auto-organize watcher module.

Watches folders for new files and automatically organizes them using AI.
Supports per-folder instructions and catch-up mode for files added while app was closed.
"""

import os
import shutil
import logging
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Set
from collections import defaultdict

from PySide6.QtCore import QObject, Signal, QTimer

logger = logging.getLogger(__name__)


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
    
    @property
    def is_running(self) -> bool:
        return self._is_running
    
    def add_folder(self, folder_path: str) -> bool:
        """Add a folder to watch. Returns True if successful."""
        # Normalize path for consistent lookups
        folder_path = os.path.normpath(folder_path)
        
        if not os.path.isdir(folder_path):
            logger.warning(f"Not a valid directory: {folder_path}")
            return False
        
        if folder_path not in self.watched_folders:
            self.watched_folders.append(folder_path)
            logger.info(f"Added watch folder: {folder_path}")
        return True
    
    def remove_folder(self, folder_path: str) -> None:
        """Remove a folder from watch list."""
        folder_path = os.path.normpath(folder_path)
        if folder_path in self.watched_folders:
            self.watched_folders.remove(folder_path)
            logger.info(f"Removed watch folder: {folder_path}")
    
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
        self._processed_files.clear()
        self._pending_files.clear()
        
        folder_count = len(self.watched_folders)
        self.status_changed.emit(f"Starting watch on {folder_count} folder(s)...")
        logger.info(f"Starting watcher for {folder_count} folders")
        
        # Flatten folders first if requested (for re-organize)
        if flatten_first:
            total_flattened = 0
            for folder in self.watched_folders:
                count = self.flatten_folder(folder)
                total_flattened += count
            if total_flattened > 0:
                self.status_changed.emit(f"Flattened {total_flattened} files from subfolders")
        
        # Organize existing files if requested
        if organize_existing:
            self._organize_existing_files()
        
        # Start periodic file check
        self._file_check_timer = QTimer(self)
        self._file_check_timer.timeout.connect(self._check_for_new_files)
        self._file_check_timer.start(3000)  # Check every 3 seconds
        
        self.status_changed.emit(f"Watching {folder_count} folder(s) for new files...")
    
    def stop(self) -> None:
        """Stop watching folders."""
        if not self._is_running:
            return
        
        self._is_running = False
        
        if self._file_check_timer:
            self._file_check_timer.stop()
            self._file_check_timer = None
        
        self._pending_files.clear()
        
        self.status_changed.emit("Watcher stopped")
        logger.info("Watcher stopped")
    
    def flatten_folder(self, folder_path: str) -> int:
        """
        Flatten folder by moving all files from subfolders to root.
        
        This is used for the "Re-organize All" feature to reset folder structure
        before applying new organization instructions.
        
        Args:
            folder_path: Path to the folder to flatten
            
        Returns:
            Number of files moved
        """
        folder_path = os.path.normpath(folder_path)
        if not os.path.isdir(folder_path):
            logger.warning(f"Cannot flatten - not a directory: {folder_path}")
            return 0
        
        moved_count = 0
        files_to_move = []
        
        # Collect files from subfolders (not the root level)
        for root, dirs, files in os.walk(folder_path):
            if root == folder_path:
                continue  # Skip root level files
            
            for file_name in files:
                if self._should_ignore(file_name):
                    continue
                files_to_move.append(os.path.join(root, file_name))
        
        if not files_to_move:
            logger.info(f"No files to flatten in {folder_path}")
            return 0
        
        self.status_changed.emit(f"Flattening {len(files_to_move)} files...")
        
        # Move files to root, handling name conflicts
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
                
                shutil.move(file_path, dest_path)
                moved_count += 1
                logger.info(f"Flattened: {file_path} -> {dest_path}")
                
            except Exception as e:
                logger.error(f"Error flattening {file_path}: {e}")
                self.error_occurred.emit(file_path, str(e))
        
        # Clean up empty subdirectories
        self._cleanup_empty_folders(folder_path)
        
        logger.info(f"Flattened {moved_count} files in {folder_path}")
        return moved_count
    
    def _cleanup_empty_folders(self, root_folder: str) -> int:
        """Remove empty subdirectories. Returns count of removed folders."""
        removed_count = 0
        root_folder = os.path.normpath(root_folder)
        
        # Walk bottom-up to remove nested empty folders
        for dirpath, dirnames, filenames in os.walk(root_folder, topdown=False):
            # Skip the root folder itself (normalize for comparison)
            if os.path.normpath(dirpath) == root_folder:
                continue
            
            # Skip hidden folders
            if os.path.basename(dirpath).startswith('.'):
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
    
    def _should_ignore(self, file_name: str) -> bool:
        """Check if a file should be ignored."""
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
        
        return False
    
    def _get_instruction_for_folder(self, folder_path: str) -> str:
        """Get the instruction for a specific folder."""
        folder_path = os.path.normpath(folder_path)
        instruction = self.folder_instructions.get(folder_path, '')
        logger.debug(f"Instruction for {folder_path}: {instruction[:50] if instruction else '(none)'}")
        return instruction
    
    def _organize_existing_files(self) -> None:
        """Organize files already in the watched folders (including subfolders)."""
        all_files = []
        
        for folder in self.watched_folders:
            folder = os.path.normpath(folder)
            if not os.path.isdir(folder):
                continue
            
            # Get ALL files in this folder AND subfolders
            for root, dirs, files in os.walk(folder):
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                
                for item in files:
                    if self._should_ignore(item):
                        continue
                    
                    item_path = os.path.join(root, item)
                    
                    # Check catch-up filter
                    if self.catch_up_since:
                        try:
                            mtime = datetime.fromtimestamp(os.path.getmtime(item_path))
                            if mtime < self.catch_up_since:
                                continue  # Skip files older than catch-up time
                        except Exception:
                            pass
                    
                    all_files.append((item_path, folder))
        
        if not all_files:
            self.status_changed.emit("No existing files to organize")
            return
        
        self.status_changed.emit(f"Organizing {len(all_files)} existing files...")
        logger.info(f"Found {len(all_files)} existing files to organize")
        
        # Group files by their source folder
        files_by_folder: Dict[str, List[str]] = defaultdict(list)
        for file_path, folder in all_files:
            files_by_folder[folder].append(file_path)
        
        # Process each folder with its instruction
        for folder, files in files_by_folder.items():
            instruction = self._get_instruction_for_folder(folder)
            self._process_files_with_ai(files, folder, instruction)
    
    def _organize_existing_files_with_options(self, flatten_first: bool = False) -> None:
        """
        Organize existing files with options.
        Called when instructions are changed while watching.
        
        Args:
            flatten_first: If True, flatten folder structure before organizing
        """
        if flatten_first:
            total_flattened = 0
            for folder in self.watched_folders:
                count = self.flatten_folder(folder)
                total_flattened += count
            if total_flattened > 0:
                self.status_changed.emit(f"Flattened {total_flattened} files, now organizing...")
        
        # Now organize
        self._organize_existing_files()
    
    def _check_for_new_files(self) -> None:
        """Periodic check for new files in watched folders."""
        if not self._is_running:
            return
        
        current_time = time.time()
        
        # Periodic cleanup of empty folders
        self._check_count += 1
        if self._check_count >= self._cleanup_interval:
            self._check_count = 0
            for folder in self.watched_folders:
                folder = os.path.normpath(folder)
                if os.path.isdir(folder):
                    deleted = self._cleanup_empty_folders(folder)
                    if deleted > 0:
                        logger.info(f"Periodic cleanup: removed {deleted} empty folder(s)")
        
        for folder in self.watched_folders:
            folder = os.path.normpath(folder)
            if not os.path.isdir(folder):
                continue
            
            try:
                for item in os.listdir(folder):
                    item_path = os.path.join(folder, item)
                    
                    if not os.path.isfile(item_path):
                        continue
                    
                    if self._should_ignore(item):
                        continue
                    
                    # Skip already processed files
                    if item_path in self._processed_files:
                        continue
                    
                    # Track pending files for debounce
                    if item_path not in self._pending_files:
                        self._pending_files[item_path] = current_time
                        logger.debug(f"New file detected: {item_path}")
                    else:
                        # Check if file has been stable long enough
                        first_seen = self._pending_files[item_path]
                        if current_time - first_seen >= self._debounce_seconds:
                            # File is stable, process it
                            instruction = self._get_instruction_for_folder(folder)
                            self._process_files_with_ai([item_path], folder, instruction)
                            self._pending_files.pop(item_path, None)
                            
            except Exception as e:
                logger.error(f"Error checking folder {folder}: {e}")
    
    def _process_files_with_ai(self, file_paths: List[str], folder: str, instruction: str) -> None:
        """
        Process files using AI to determine organization.
        
        First indexes any unindexed files, then organizes them.
        
        Args:
            file_paths: List of file paths to organize
            folder: The destination folder (same as source folder for watch mode)
            instruction: User's organization instruction
        """
        if not file_paths:
            return
        
        from app.core.database import file_index
        from app.core.ai_organizer import request_organization_plan, validate_plan, deduplicate_plan, ensure_all_files_included
        from app.core.search import SearchService
        
        # First pass: identify files that need indexing
        files_to_index = []
        for file_path in file_paths:
            try:
                indexed_info = file_index.get_file_by_path(file_path)
                if not indexed_info:
                    files_to_index.append(file_path)
            except Exception as e:
                logger.warning(f"Error checking index status for {file_path}: {e}")
                files_to_index.append(file_path)
        
        # Index unindexed files first
        if files_to_index:
            logger.info(f"Auto-indexing {len(files_to_index)} unindexed file(s) before organizing...")
            self.status_changed.emit(f"Indexing {len(files_to_index)} new file(s)...")
            
            search_service = SearchService()
            indexed_count = 0
            
            for file_path in files_to_index:
                try:
                    from pathlib import Path
                    result = search_service.index_single_file(Path(file_path), force_ai=True)
                    
                    if result.get('success'):
                        indexed_count += 1
                        logger.info(f"Auto-indexed: {os.path.basename(file_path)}")
                        self.file_indexed.emit(file_path)
                    elif result.get('error'):
                        logger.warning(f"Failed to index {file_path}: {result.get('error')}")
                    elif result.get('skipped'):
                        logger.debug(f"Skipped indexing: {file_path}")
                        
                except Exception as e:
                    logger.error(f"Error auto-indexing {file_path}: {e}")
            
            if indexed_count > 0:
                self.status_changed.emit(f"Indexed {indexed_count} file(s), now organizing...")
                logger.info(f"Auto-indexed {indexed_count} files successfully")
        
        # Build file info for AI using SIMPLE SEQUENTIAL IDs
        # This ensures consistent IDs regardless of database state
        files_info = []
        files_by_id = {}
        
        for idx, file_path in enumerate(file_paths, start=1):
            try:
                file_name = os.path.basename(file_path)
                file_size = os.path.getsize(file_path)
                
                # Use simple sequential ID for reliability
                file_id = idx
                
                # Try to get extra metadata from index if available
                indexed_info = file_index.get_file_by_path(file_path)
                
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
                files_by_id[file_id] = {
                    'file_path': file_path,
                    'file_name': file_name,
                    'file_size': file_size,
                    'db_id': indexed_info.get('id') if indexed_info else None,
                }
                    
            except Exception as e:
                logger.error(f"Error getting file info for {file_path}: {e}")
                continue
        
        if not files_info:
            return
        
        # Build instruction for AI
        if instruction:
            full_instruction = (
                f"[AUTO-ORGANIZE] User's specific instructions: {instruction}\n\n"
                "RULES FOR AUTO-ORGANIZE MODE:\n"
                "1. FOLLOW the user's specific instructions EXACTLY for any files they mentioned\n"
                "2. For ALL REMAINING files not covered by user's instructions, organize them logically by file type\n"
                "3. EVERY file MUST be placed in a folder - NO files left out\n"
                "4. Use simple, clear folder names (e.g., 'images', 'documents', 'videos', 'audio')\n"
                "5. If user says 'screenshots to X' - put screenshots in X, organize everything else by type"
            )
        else:
            full_instruction = (
                "[AUTO-ORGANIZE] Organize ALL files into logical folders based on file type and content.\n"
                "Use clear folder names: 'images', 'documents', 'videos', 'audio', etc.\n"
                "EVERY file MUST be placed in a folder - NO files left out."
            )
        
        logger.info(f"Requesting AI plan for {len(files_info)} files with instruction: {instruction[:50]}...")
        self.status_changed.emit(f"AI analyzing {len(files_info)} files...")
        
        try:
            plan = request_organization_plan(full_instruction, files_info)
            
            if not plan:
                logger.warning("AI returned no plan")
                self.status_changed.emit("AI could not generate organization plan")
                return
            
            # Deduplicate the plan
            plan = deduplicate_plan(plan)
            valid_ids = set(files_by_id.keys())
            
            # Filter plan to only include valid file IDs (ones we actually have)
            filtered_plan = {"folders": {}}
            for folder_name, file_ids in plan.get("folders", {}).items():
                valid_file_ids = []
                for fid in file_ids:
                    try:
                        fid_int = int(fid)
                        if fid_int in valid_ids:
                            valid_file_ids.append(fid_int)
                    except (TypeError, ValueError):
                        pass
                if valid_file_ids:
                    filtered_plan["folders"][folder_name] = valid_file_ids
            
            # Ensure all files are included (add missing to 'misc')
            filtered_plan = ensure_all_files_included(filtered_plan, valid_ids, files_info)
            
            if not filtered_plan.get("folders"):
                logger.warning("No valid files in plan after filtering")
                self.status_changed.emit("No files to organize")
                return
            
            logger.info(f"Filtered plan has {sum(len(v) for v in filtered_plan['folders'].values())} files in {len(filtered_plan['folders'])} folders")
            
            # Execute the filtered plan
            self._execute_plan(filtered_plan, files_by_id, folder)
            
        except Exception as e:
            logger.error(f"AI processing error: {e}")
            self.error_occurred.emit("", f"AI error: {e}")
            self.status_changed.emit(f"AI error: {str(e)[:50]}")
    
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
                    
                    # Move the file
                    shutil.move(source_path, dest_path)
                    moved_count += 1
                    
                    # Track as processed
                    self._processed_files.add(source_path)
                    self._processed_files.add(dest_path)
                    
                    logger.info(f"Organized: {source_path} -> {dest_path}")
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
    