"""
Auto-organize file watcher service.

Monitors folders for new files and automatically organizes them.
Auto-indexes files with AI analysis before organizing for smart categorization.
"""

import os
import time
import shutil
import logging
import threading
from pathlib import Path
from typing import List, Dict, Callable, Optional
from datetime import datetime

from PySide6.QtCore import QObject, Signal, QThread

logger = logging.getLogger(__name__)


def _index_file_with_ai(file_path: str) -> Optional[Dict]:
    """
    Index a single file with AI analysis.
    
    Returns the indexed file record from database, or None on failure.
    """
    from app.core.search import search_service
    from app.core.database import file_index
    
    try:
        logger.info(f"Auto-indexing file with AI: {file_path}")
        
        # Clear any stuck pause/cancel flags from previous sessions
        # (These are for the main UI indexing, not auto-watcher)
        search_service._pause_flag.clear()
        search_service._cancel_flag.clear()
        
        # Index the file (this will do AI analysis)
        result = search_service.index_single_file(Path(file_path), force_ai=False)
        
        if result.get('error'):
            logger.warning(f"Failed to index {file_path}: {result['error']}")
            return None
        
        if result.get('skipped'):
            logger.debug(f"File already indexed: {file_path}")
        else:
            logger.info(f"Successfully indexed with AI: {file_path}")
        
        # Retrieve the full record from database
        record = file_index.get_file_by_path(file_path)
        if record:
            logger.info(f"Retrieved record from DB: id={record.get('id')}, file_name={record.get('file_name')}")
        return record
        
    except Exception as e:
        logger.error(f"Error indexing file {file_path}: {e}")
        return None


class FileEvent:
    """Represents a file system event."""
    def __init__(self, path: str, event_type: str):
        self.path = path
        self.event_type = event_type
        self.timestamp = datetime.now()
    
    def __repr__(self):
        return f"FileEvent({self.event_type}: {self.path})"


class AutoOrganizeWatcher(QObject):
    """
    Watches folders for new files and auto-organizes them.
    
    Uses polling instead of OS-level watching for simplicity and cross-platform support.
    """
    
    # Signals
    file_organized = Signal(str, str, str)  # source, destination, category
    error_occurred = Signal(str, str)  # path, error message
    status_changed = Signal(str)  # status message
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.watched_folders: List[str] = []
        self.is_running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._known_files: Dict[str, set] = {}  # folder -> set of known file paths
        self._pending_files: Dict[str, float] = {}  # path -> first seen timestamp
        
        # Configuration
        self.poll_interval = 2.0  # seconds between checks
        self.debounce_time = 3.0  # seconds to wait before processing new file
        self.use_ai = True  # always use AI for organization
        self.instruction = ""  # user-provided organization instruction (legacy/single)
        self.folder_instructions = {}  # {folder_path: instruction} for per-folder instructions
        self.catch_up_since = None  # datetime for catch-up mode (organize files newer than this)
        self._organize_existing_on_start = False  # organize existing files when starting
        
        # Callbacks
        self.on_organize: Optional[Callable] = None
    
    def add_folder(self, folder_path: str) -> bool:
        """Add a folder to watch."""
        folder_path = os.path.normpath(folder_path)
        
        if not os.path.isdir(folder_path):
            logger.warning(f"Cannot watch non-existent folder: {folder_path}")
            return False
        
        if folder_path not in self.watched_folders:
            self.watched_folders.append(folder_path)
            self._known_files[folder_path] = self._scan_folder(folder_path)
            logger.info(f"Added watch folder: {folder_path} ({len(self._known_files[folder_path])} existing files)")
            return True
        
        return False
    
    def remove_folder(self, folder_path: str) -> bool:
        """Remove a folder from watch list."""
        folder_path = os.path.normpath(folder_path)
        
        if folder_path in self.watched_folders:
            self.watched_folders.remove(folder_path)
            self._known_files.pop(folder_path, None)
            logger.info(f"Removed watch folder: {folder_path}")
            return True
        
        return False
    
    def _scan_folder(self, folder_path: str) -> set:
        """Scan folder (top-level only) and return set of file paths. Used for watching new files."""
        files = set()
        try:
            for entry in os.scandir(folder_path):
                if entry.is_file():
                    files.add(entry.path)
        except OSError as e:
            logger.warning(f"Error scanning folder {folder_path}: {e}")
        return files
    
    def _scan_folder_recursive(self, folder_path: str) -> list:
        """Scan folder recursively and return list of ALL file paths including subfolders."""
        files = []
        try:
            for root, dirs, filenames in os.walk(folder_path):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    files.append(file_path)
        except OSError as e:
            logger.warning(f"Error scanning folder recursively {folder_path}: {e}")
        return files
    
    def start(self, organize_existing: bool = True):
        """
        Start watching folders.
        
        Args:
            organize_existing: If True, organize existing files first before watching for new ones
        """
        if self.is_running:
            return
        
        self.is_running = True
        self._stop_event.clear()
        # Check if we have any instructions (single or per-folder)
        has_instructions = bool(self.instruction) or any(self.folder_instructions.values())
        self._organize_existing_on_start = organize_existing and has_instructions
        
        # Start the background thread immediately (UI stays responsive)
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        
        if self._organize_existing_on_start:
            self.status_changed.emit(f"Organizing existing files in background...")
        else:
            self.status_changed.emit(f"Watching {len(self.watched_folders)} folder(s)...")
        
        logger.info(f"Started auto-organize watcher for {len(self.watched_folders)} folders")
    
    def _organize_existing_files(self):
        """Organize all existing files in watched folders using AI (including subfolders)."""
        from app.core.smart_categorizer import smart_categorizer
        from datetime import datetime
        
        # Process each folder separately (for per-folder instructions)
        for folder in self.watched_folders:
            folder_instruction = self.folder_instructions.get(folder, '') or self.instruction
            
            if not folder_instruction:
                logger.info(f"Skipping {folder} - no instruction provided")
                continue
            
            # Scan files in this folder
            folder_files = self._scan_folder_recursive(folder)
            logger.info(f"Scanned {len(folder_files)} files in {folder}")
            
            # Filter files
            files_to_process = []
            for file_path in folder_files:
                if not os.path.exists(file_path):
                    continue
                if smart_categorizer.should_ignore(file_path):
                    logger.debug(f"Skipped (ignored): {file_path}")
                    continue
                
                # Catch-up mode: only process files newer than catch_up_since
                if self.catch_up_since:
                    try:
                        mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                        if mtime <= self.catch_up_since:
                            continue  # File was already present before last session
                    except Exception:
                        pass
                
                files_to_process.append(file_path)
            
            if not files_to_process:
                logger.info(f"No files to organize in {folder}")
                continue
            
            logger.info(f"Organizing {len(files_to_process)} files in {folder}")
            self.status_changed.emit(f"Organizing {len(files_to_process)} files in {os.path.basename(folder)}...")
            
            # Index files
            indexed_files = []
            for i, file_path in enumerate(files_to_process):
                if i % 5 == 0 or i == len(files_to_process) - 1:
                    self.status_changed.emit(f"Processing {i+1}/{len(files_to_process)} files...")
                
                record = _index_file_with_ai(file_path)
                if record:
                    indexed_files.append(record)
                else:
                    indexed_files.append({
                        'id': hash(file_path),
                        'file_path': file_path,
                        'file_name': os.path.basename(file_path),
                        'file_size': os.path.getsize(file_path) if os.path.exists(file_path) else 0,
                        'tags': [],
                        'caption': '',
                        'label': '',
                    })
            
            # Organize with this folder's instruction
            if indexed_files:
                self._process_with_ai_indexed_for_folder(indexed_files, folder, folder_instruction)
        
        # Reset catch-up mode
        self.catch_up_since = None
        
        # Clean up empty folders after organization
        self._cleanup_empty_folders()
        
        logger.info(f"Finished organizing existing files")
    
    def stop(self):
        """Stop watching folders."""
        if not self.is_running:
            return
        
        self.is_running = False
        self._stop_event.set()
        
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        
        self.status_changed.emit("Watcher stopped")
        logger.info("Stopped auto-organize watcher")
    
    def _watch_loop(self):
        """Main watch loop - runs in background thread."""
        # Organize existing files first if requested (runs in background)
        if getattr(self, '_organize_existing_on_start', False):
            try:
                self._organize_existing_files()
                self.status_changed.emit(f"Watching {len(self.watched_folders)} folder(s)...")
            except Exception as e:
                logger.error(f"Error organizing existing files: {e}")
            self._organize_existing_on_start = False
        
        while not self._stop_event.is_set():
            try:
                self._check_for_new_files()
                self._process_pending_files()
            except Exception as e:
                logger.error(f"Error in watch loop: {e}")
            
            # Wait for next poll interval
            self._stop_event.wait(self.poll_interval)
    
    def _check_for_new_files(self):
        """Check watched folders for new files."""
        for folder in self.watched_folders:
            if not os.path.isdir(folder):
                continue
            
            current_files = self._scan_folder(folder)
            known_files = self._known_files.get(folder, set())
            
            # Find new files
            new_files = current_files - known_files
            
            for file_path in new_files:
                if file_path not in self._pending_files:
                    self._pending_files[file_path] = time.time()
                    logger.debug(f"New file detected: {file_path}")
            
            # Update known files
            self._known_files[folder] = current_files
    
    def _process_pending_files(self):
        """Process files that have been pending long enough (debounce)."""
        from app.core.smart_categorizer import smart_categorizer
        
        current_time = time.time()
        files_to_remove = []
        files_to_process = []
        
        for file_path, first_seen in list(self._pending_files.items()):
            # Check if file has been stable long enough
            if current_time - first_seen < self.debounce_time:
                continue
            
            # Check if file still exists and is accessible
            if not os.path.exists(file_path):
                files_to_remove.append(file_path)
                continue
            
            # Check if file is still being written (size changed)
            try:
                size1 = os.path.getsize(file_path)
                time.sleep(0.5)
                size2 = os.path.getsize(file_path)
                
                if size1 != size2:
                    # File still being written, wait more
                    continue
            except OSError:
                files_to_remove.append(file_path)
                continue
            
            # File is ready to process
            files_to_remove.append(file_path)
            
            if smart_categorizer.should_ignore(file_path):
                logger.debug(f"Ignoring file: {file_path}")
                continue
            
            files_to_process.append(file_path)
        
        # STEP 1: Auto-index all files with AI FIRST
        # This gives us rich metadata (tags, captions, labels) for smart organization
        indexed_files = []
        for file_path in files_to_process:
            record = _index_file_with_ai(file_path)
            if record:
                indexed_files.append(record)
            else:
                # Fallback: create basic metadata if indexing fails
                indexed_files.append({
                    'id': hash(file_path),
                    'file_path': file_path,
                    'file_name': os.path.basename(file_path),
                    'file_size': os.path.getsize(file_path) if os.path.exists(file_path) else 0,
                    'tags': [],
                    'caption': '',
                    'label': '',
                })
        
        # STEP 2: Organize using AI with rich indexed metadata
        # Group files by their parent watched folder
        files_by_folder = {}
        for record in indexed_files:
            file_path = record.get('file_path', '')
            if not file_path:
                continue
            parent_folder = self._find_parent_watched_folder(file_path)
            if parent_folder not in files_by_folder:
                files_by_folder[parent_folder] = []
            files_by_folder[parent_folder].append(record)
        
        # Process each folder's files with its instruction
        has_any_instruction = bool(self.instruction) or any(self.folder_instructions.values())
        if indexed_files and has_any_instruction:
            for folder, folder_files in files_by_folder.items():
                instruction = self.folder_instructions.get(folder, '') or self.instruction
                if instruction:
                    self._process_with_ai_indexed_for_folder(folder_files, folder, instruction)
                else:
                    # Fallback for folders without instruction
                    from app.core.smart_categorizer import smart_categorizer
                    for record in folder_files:
                        file_path = record.get('file_path', '')
                        if file_path and os.path.exists(file_path):
                            dest_folder = os.path.dirname(file_path)
                            dest_path = smart_categorizer.get_destination_path(file_path, dest_folder)
                            category, _ = smart_categorizer.categorize_file(file_path)
                            if os.path.dirname(file_path) != os.path.dirname(dest_path):
                                self._move_file(file_path, dest_path, category)
        elif indexed_files:
            # Fallback to simple categorization if no instruction
            for record in indexed_files:
                file_path = record.get('file_path', '')
                if not file_path or not os.path.exists(file_path):
                    continue
                    
                folder = os.path.dirname(file_path)
                dest_path = smart_categorizer.get_destination_path(file_path, folder)
                category, _ = smart_categorizer.categorize_file(file_path)
                
                # Skip if already in correct location
                if os.path.dirname(file_path) == os.path.dirname(dest_path):
                    continue
                
                self._move_file(file_path, dest_path, category)
        
        # Remove processed files from pending
        for file_path in files_to_remove:
            self._pending_files.pop(file_path, None)
    
    def _move_file(self, source: str, dest: str, category: str) -> bool:
        """Move a file and emit signals. Returns True on success."""
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(source, dest)
            
            logger.info(f"Auto-organized: {os.path.basename(source)} → {category}/")
            self.file_organized.emit(source, dest, category)
            
            # Update known files
            dest_folder = os.path.dirname(dest)
            if dest_folder in self._known_files:
                self._known_files[dest_folder].add(dest)
            
            return True
        except Exception as e:
            logger.error(f"Failed to move {source}: {e}")
            self.error_occurred.emit(source, str(e))
            return False
    
    def _process_with_ai_indexed(self, indexed_files: List[Dict]):
        """
        Process files using AI-based organization with INDEXED metadata.
        Uses the legacy single instruction for backward compatibility.
        """
        # Determine which folder these files belong to
        if indexed_files:
            sample_path = indexed_files[0].get('file_path', '')
            folder = self._find_parent_watched_folder(sample_path)
            instruction = self.folder_instructions.get(folder, '') or self.instruction
        else:
            folder = self.watched_folders[0] if self.watched_folders else ''
            instruction = self.instruction
        
        self._process_with_ai_indexed_for_folder(indexed_files, folder, instruction)
    
    def _find_parent_watched_folder(self, file_path: str) -> str:
        """Find which watched folder contains this file."""
        file_path_norm = os.path.normpath(file_path).lower()
        for folder in self.watched_folders:
            folder_norm = os.path.normpath(folder).lower()
            if file_path_norm.startswith(folder_norm):
                return folder
        return self.watched_folders[0] if self.watched_folders else ''
    
    def _process_with_ai_indexed_for_folder(self, indexed_files: List[Dict], folder: str, instruction: str):
        """
        Process files using AI-based organization with INDEXED metadata.
        
        This is the smart path - files have already been indexed with AI,
        so we have rich tags, captions, and labels to help the AI organize.
        """
        try:
            from app.core.ai_organizer import request_organization_plan, plan_to_moves, validate_plan, deduplicate_plan
            from app.core.database import file_index
            
            # Build file metadata list with ALL the rich AI data
            files = []
            for record in indexed_files:
                file_path = record.get('file_path', '')
                if not file_path or not os.path.exists(file_path):
                    continue
                    
                files.append({
                    "id": record.get('id', hash(file_path)),
                    "file_path": file_path,
                    "file_name": record.get('file_name', os.path.basename(file_path)),
                    "file_size": record.get('file_size', 0),
                    "extension": record.get('file_extension', Path(file_path).suffix),
                    # Rich AI-generated metadata!
                    "tags": record.get('tags', []),
                    "caption": record.get('caption', ''),
                    "label": record.get('label', ''),
                    "category": record.get('category', ''),
                    "ocr_text": (record.get('ocr_text', '') or '')[:200],  # Truncate for API
                })
            
            if not files:
                logger.warning("No valid files to organize")
                return
            
            # Log the file IDs we're sending to AI
            file_ids_sent = [f["id"] for f in files]
            logger.info(f"Sending {len(files)} files to AI with IDs: {file_ids_sent}")
            logger.info(f"Using instruction: {instruction[:50]}..." if len(instruction) > 50 else f"Using instruction: {instruction}")
            for f in files:
                logger.debug(f"  File: id={f['id']}, name={f['file_name']}, tags={f.get('tags', [])}")
            
            self.status_changed.emit(f"Asking AI to organize {len(files)} file(s)...")
            
            # Get AI plan - now with rich metadata!
            plan = request_organization_plan(instruction, files)
            
            if not plan:
                logger.warning("AI returned no plan, using fallback categorization")
                self._fallback_organize(indexed_files)
                return
            
            # Build files_by_id FIRST (needed for validation)
            files_by_id = {f["id"]: f for f in files}
            
            # Validate and deduplicate
            plan = deduplicate_plan(plan)
            is_valid, error = validate_plan(plan, set(files_by_id.keys()))
            
            if not is_valid:
                logger.warning(f"Invalid AI plan: {error}. Using fallback.")
                self._fallback_organize(indexed_files)
                return
            
            # Use the folder we're processing as destination (not always the first watched folder!)
            # This ensures files stay within their own watched folder
            dest_folder = Path(folder)
            
            # Convert to moves
            moves = plan_to_moves(plan, files_by_id, dest_folder)
            
            # Execute moves and update database paths
            for move in moves:
                source = move["source_path"]
                dest = move["destination_path"]
                category = move["destination_folder"]
                
                if self._move_file(source, dest, category):
                    # Update database with new path
                    try:
                        # Find the file record
                        for f in files:
                            if f["file_path"] == source:
                                file_index.update_file_path(f["id"], dest)
                                break
                    except Exception as e:
                        logger.warning(f"Failed to update DB path for {source}: {e}")
            
            self.status_changed.emit(f"Organized {len(moves)} file(s)")
                
        except Exception as e:
            logger.error(f"AI organization failed: {e}")
            self._fallback_organize(indexed_files)
    
    def _fallback_organize(self, indexed_files: List[Dict]):
        """Fallback to simple rule-based categorization."""
        from app.core.smart_categorizer import smart_categorizer
        
        for record in indexed_files:
            file_path = record.get('file_path', '')
            if not file_path or not os.path.exists(file_path):
                continue
                
            folder = os.path.dirname(file_path)
            dest_path = smart_categorizer.get_destination_path(file_path, folder)
            category, _ = smart_categorizer.categorize_file(file_path)
            
            if os.path.dirname(file_path) != os.path.dirname(dest_path):
                self._move_file(file_path, dest_path, category)
    
    def _cleanup_empty_folders(self):
        """Remove empty folders in watched directories after organization."""
        for folder in self.watched_folders:
            if not os.path.isdir(folder):
                continue
            
            # Walk bottom-up to delete empty folders
            deleted_count = 0
            for dirpath, dirnames, filenames in os.walk(folder, topdown=False):
                # Skip the root watched folder itself
                if os.path.normpath(dirpath) == os.path.normpath(folder):
                    continue
                
                try:
                    # Check if folder is empty
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                        deleted_count += 1
                        logger.info(f"Deleted empty folder: {dirpath}")
                except OSError as e:
                    logger.debug(f"Could not delete folder {dirpath}: {e}")
            
            if deleted_count > 0:
                self.status_changed.emit(f"Cleaned up {deleted_count} empty folder(s)")
    
    def flatten_folder(self, folder_path: str) -> int:
        """
        Flatten a folder by moving all files from subfolders to the root.
        Returns the number of files moved.
        
        This is used for "Re-organize" feature to start fresh with new instructions.
        """
        import shutil
        
        folder_path = os.path.normpath(folder_path)
        if not os.path.isdir(folder_path):
            return 0
        
        moved_count = 0
        files_to_move = []
        
        # Collect all files from subfolders (not root level)
        for root, dirs, files in os.walk(folder_path):
            # Skip the root folder itself - we only want files in subfolders
            if root == folder_path:
                continue
            
            for file_name in files:
                file_path = os.path.join(root, file_name)
                files_to_move.append(file_path)
        
        # Move files to root
        for file_path in files_to_move:
            file_name = os.path.basename(file_path)
            dest_path = os.path.join(folder_path, file_name)
            
            # Handle name conflicts
            if os.path.exists(dest_path):
                base, ext = os.path.splitext(file_name)
                counter = 1
                while os.path.exists(dest_path):
                    dest_path = os.path.join(folder_path, f"{base} ({counter}){ext}")
                    counter += 1
            
            try:
                shutil.move(file_path, dest_path)
                moved_count += 1
                logger.info(f"Flattened: {os.path.basename(file_path)} → root")
            except Exception as e:
                logger.warning(f"Failed to flatten {file_path}: {e}")
        
        # Clean up empty folders after flattening
        if moved_count > 0:
            self._cleanup_empty_folders()
            logger.info(f"Flattened {moved_count} files in {folder_path}")
        
        return moved_count
    
    def _process_with_ai(self, file_paths: List[str]):
        """
        Process files using AI-based organization (legacy - without pre-indexing).
        Prefer _process_with_ai_indexed for better results.
        """
        try:
            from app.core.ai_organizer import request_organization_plan, plan_to_moves
            
            # Build file metadata for AI
            files = []
            for fp in file_paths:
                files.append({
                    "id": hash(fp),
                    "file_path": fp,
                    "file_name": os.path.basename(fp),
                    "file_size": os.path.getsize(fp) if os.path.exists(fp) else 0,
                    "tags": [],
                    "category": "",
                })
            
            # Get AI plan
            plan = request_organization_plan(self.instruction, files)
            
            if not plan:
                logger.warning("AI returned no plan, using fallback categorization")
                return
            
            # Build files_by_id
            files_by_id = {f["id"]: f for f in files}
            
            # Use first watched folder as destination
            if self.watched_folders:
                dest_folder = Path(self.watched_folders[0])
            else:
                dest_folder = Path(os.path.dirname(file_paths[0]))
            
            # Convert to moves
            moves = plan_to_moves(plan, files_by_id, dest_folder)
            
            # Execute moves
            for move in moves:
                self._move_file(
                    move["source_path"],
                    move["destination_path"],
                    move["destination_folder"]
                )
                
        except Exception as e:
            logger.error(f"AI organization failed: {e}")
            # Fallback to simple categorization
            from app.core.smart_categorizer import smart_categorizer
            for file_path in file_paths:
                folder = os.path.dirname(file_path)
                dest_path = smart_categorizer.get_destination_path(file_path, folder)
                category, _ = smart_categorizer.categorize_file(file_path)
                if os.path.dirname(file_path) != os.path.dirname(dest_path):
                    self._move_file(file_path, dest_path, category)


class AutoOrganizeWorker(QThread):
    """
    Background worker for one-time organization of multiple folders.
    """
    
    progress = Signal(int, int)  # current, total
    file_moved = Signal(str, str, str)  # source, destination, category
    finished = Signal(int, int)  # success count, error count
    error = Signal(str)
    
    def __init__(self, folders: List[str], use_ai: bool = False):
        super().__init__()
        self.folders = folders
        self.use_ai = use_ai
        self._cancelled = False
    
    def cancel(self):
        """Cancel the operation."""
        self._cancelled = True
    
    def run(self):
        """Run the organization."""
        from app.core.smart_categorizer import smart_categorizer
        
        success_count = 0
        error_count = 0
        
        # Collect all files to organize
        all_files = []
        for folder in self.folders:
            if not os.path.isdir(folder):
                continue
            
            for entry in os.scandir(folder):
                if entry.is_file():
                    if not smart_categorizer.should_ignore(entry.path):
                        all_files.append((folder, entry.path))
        
        total = len(all_files)
        
        for i, (base_folder, file_path) in enumerate(all_files):
            if self._cancelled:
                break
            
            self.progress.emit(i + 1, total)
            
            try:
                # Get destination
                dest_path = smart_categorizer.get_destination_path(
                    file_path, base_folder, use_ai=self.use_ai
                )
                category, _ = smart_categorizer.categorize_file(file_path, use_ai=self.use_ai)
                
                # Skip if already in correct location
                if os.path.dirname(file_path) == os.path.dirname(dest_path):
                    continue
                
                # Move file
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.move(file_path, dest_path)
                
                success_count += 1
                self.file_moved.emit(file_path, dest_path, category)
                
            except Exception as e:
                error_count += 1
                logger.error(f"Failed to organize {file_path}: {e}")
        
        self.finished.emit(success_count, error_count)


# Global watcher instance
auto_watcher = AutoOrganizeWatcher()
