"""
Database management for file indexing and search functionality.
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from .settings import settings

logger = logging.getLogger(__name__)

def _parse_tags_value(raw: Any) -> Optional[List[str]]:
    """Parse tags stored in DB.

    Historically tags were stored either as JSON list text or a comma-separated string.
    Return a list of strings (lowercased) or None.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Try JSON list first
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return [str(t).strip() for t in v if str(t).strip()]
        if isinstance(v, str) and v.strip():
            # Sometimes legacy stored a JSON string
            return [t.strip() for t in v.split(",") if t.strip()]
    except Exception:
        pass
    # Fallback: comma-separated string
    return [t.strip() for t in s.split(",") if t.strip()]

def _fts_term(word: str) -> str:
    """One search word as a safe FTS5 prefix term. Quoted, so punctuation can't break the whole query: the hyphen in
    "x-rays" or "self-raising" was a syntax error ("no such column"), and search fell back to an A-Z list of loose
    matches. A plural searches its singular as a prefix, which finds both: "birds" -> "bird"* (photos are tagged
    "bird"). Ported from the Mac (pack §6.1, item 6)."""
    w = str(word).lower()
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3]
    elif len(w) > 3 and w.endswith("es") and w[:-2].endswith(("s", "x", "z", "ch", "sh")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    return '"' + w.replace('"', '""') + '"*'


class FileIndex:
    """SQLite database for file indexing and search."""
    
    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the file index database."""
        if db_path is None:
            db_path = settings.get_app_data_dir() / "file_index.db"
        
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()
        self._ensure_fts_healthy()

    def _init_database(self):
        """Initialize database tables."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Enable WAL journalling once — it persists in the DB file, so every
            # future connection inherits it. WAL lets a search read the index
            # while an organize's batch path-update is writing (no reader/writer
            # lock contention — the lag we flagged on big folders). Safe on local
            # storage (the DB lives in AppData); creates .db-wal/.db-shm sidecars.
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except Exception as _wal_err:
                logger.warning(f"Could not enable WAL journal mode: {_wal_err}")

            # Create files table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE NOT NULL,
                    file_name TEXT NOT NULL,
                    file_extension TEXT,
                    file_size INTEGER,
                    mime_type TEXT,
                    category TEXT,
                    created_date TEXT,
                    modified_date TEXT,
                    indexed_date TEXT,
                    has_ocr BOOLEAN DEFAULT FALSE,
                    ocr_text TEXT,
                    label TEXT,
                    tags TEXT,
                    caption TEXT,
                    vision_confidence REAL,
                    content_hash TEXT,
                    last_indexed_at TEXT,
                    ai_source TEXT,
                    user_tags TEXT,
                    metadata TEXT,
                    UNIQUE(file_path)
                )
            """)

            # Migrate existing schema: ensure new columns exist
            try:
                cursor.execute("PRAGMA table_info(files)")
                cols = {row[1] for row in cursor.fetchall()}
                to_add = []
                if 'label' not in cols:
                    to_add.append("ALTER TABLE files ADD COLUMN label TEXT")
                if 'tags' not in cols:
                    to_add.append("ALTER TABLE files ADD COLUMN tags TEXT")
                if 'caption' not in cols:
                    to_add.append("ALTER TABLE files ADD COLUMN caption TEXT")
                if 'vision_confidence' not in cols:
                    to_add.append("ALTER TABLE files ADD COLUMN vision_confidence REAL")
                if 'content_hash' not in cols:
                    to_add.append("ALTER TABLE files ADD COLUMN content_hash TEXT")
                if 'last_indexed_at' not in cols:
                    to_add.append("ALTER TABLE files ADD COLUMN last_indexed_at TEXT")
                if 'ai_source' not in cols:
                    to_add.append("ALTER TABLE files ADD COLUMN ai_source TEXT")
                if 'user_tags' not in cols:
                    to_add.append("ALTER TABLE files ADD COLUMN user_tags TEXT")
                if 'original_date' not in cols:
                    to_add.append("ALTER TABLE files ADD COLUMN original_date TEXT")
                for stmt in to_add:
                    cursor.execute(stmt)
            except Exception as e:
                logger.warning(f"Schema migration warning: {e}")
            
            # Full-text search index. FTS5 can't be ALTER-ed, so the ONLY way to change its columns
            # is drop+recreate. Doing that on every start — and, worse, without repopulating an
            # external-content index — silently empties search on every launch. Instead: recreate
            # ONLY when the columns actually differ from what we expect, and ALWAYS repopulate when
            # we do. Normal launches leave the index intact (no wipe, no rebuild, no lag). On any
            # detection error we fall back to a full recreate+repopulate — never worse than before.
            _fts_cols = ("file_name", "file_path", "category", "ocr_text", "caption", "tags")
            _create_fts = (
                "CREATE VIRTUAL TABLE files_fts USING fts5("
                + ", ".join(_fts_cols)
                + ", content='files', content_rowid='id')"
            )
            def _recreate_fts():
                cursor.execute("DROP TABLE IF EXISTS files_fts")
                cursor.execute(_create_fts)
                # external-content FTS5 is empty after CREATE — 'rebuild' fills the inverted index.
                cursor.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
            try:
                _existing = [r[1] for r in cursor.execute("PRAGMA table_info(files_fts)").fetchall()]
                if set(_existing) == set(_fts_cols):
                    logger.debug("FTS index schema current — preserved (no rebuild).")
                else:
                    _recreate_fts()
                    logger.info("FTS index (re)created + repopulated (schema was %s).", _existing or "missing")
            except Exception as e:
                logger.warning("FTS schema check failed (%s); doing a full recreate+repopulate.", e)
                try:
                    _recreate_fts()
                except Exception as e2:
                    logger.error("FTS recreate failed: %s", e2)

            # Keep the FTS index in sync automatically via triggers on `files`. This is the ONLY
            # FTS maintenance — the app writes to `files`, triggers mirror it (correct old terms on
            # delete, new terms on insert), so moves/edits/deletes can never leave stale terms.
            self._create_fts_triggers(cursor)

            # Embeddings table for semantic search
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    file_id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vector TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
                )
                """
            )
            
            # Create search history table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    results_count INTEGER
                )
            """)
            
            conn.commit()
            logger.info(f"Database initialized at {self.db_path}")

    # --- Update helpers for user edits ---
    def update_file_field(self, file_id: int, field: str, value: Any) -> bool:
        """Update a single editable field for a file. Returns True on success.
        Allowed fields: label, caption, tags, user_tags, metadata.
        """
        # DIAGNOSTIC: Log every database write
        import traceback
        stack_summary = ''.join(traceback.format_stack()[-5:-1])
        logger.warning(f"[DB_WRITE] update_file_field called: file_id={file_id}, field='{field}', value={repr(value)[:100]}")
        logger.warning(f"[DB_WRITE] Call stack:\n{stack_summary}")
        
        allowed = {"label", "caption", "tags", "user_tags", "metadata"}
        if field not in allowed:
            return False
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                val = value
                if field in {"tags", "user_tags", "metadata"}:
                    # store as JSON text
                    import json as _json
                    val = _json.dumps(value)
                cursor.execute(f"UPDATE files SET {field} = ? WHERE id = ?", (val, file_id))
                # FTS mirror for edited fields is maintained by the files_fts_au trigger.
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Error updating {field} for {file_id}: {e}")
            return False
    
    def set_tags_by_user(self, file_id: int, tags: List[str]) -> bool:
        """The person edited a file's tags (the Tags cell): save them, and remember the ones they typed in (user_tags)
        so a re-read keeps them; a tag they deleted is forgotten there too. (Mac a9cad3d.)"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT tags, user_tags FROM files WHERE id = ?", (file_id,)).fetchone()
            old = (_parse_tags_value(row[0]) or []) if row else []
            user = (_parse_tags_value(row[1]) or []) if row else []
            user = [t for t in user if t in tags] + [t for t in tags if t not in old and t not in user]
            return self.update_file_field(file_id, 'tags', tags) and self.update_file_field(file_id, 'user_tags', user)
        except Exception as e:
            logger.error(f"Error saving edited tags for {file_id}: {e}")
            return False

    def update_file_path(self, file_id: int, new_path: str) -> bool:
        """
        Update only the file_path for a file after it has been moved.
        Preserves all metadata (tags, labels, captions, etc.) - only path changes.
        
        Args:
            file_id: The ID of the file in the database
            new_path: The new file path after moving
            
        Returns:
            True if successful, False otherwise
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # IMPORTANT: Before updating, remove any stale entry that has the same path
                # This prevents UNIQUE constraint errors when a file is moved to a path
                # that was previously occupied by another file (now moved/deleted)
                cursor.execute(
                    "DELETE FROM files WHERE file_path = ? AND id != ?",
                    (new_path, file_id)
                )
                stale_deleted = cursor.rowcount
                if stale_deleted > 0:
                    logger.info(f"Removed {stale_deleted} stale entry/entries for path: {new_path}")
                    # (The files_fts_ad trigger already removed their FTS entries.)

                # Update file_path in main table
                cursor.execute(
                    "UPDATE files SET file_path = ? WHERE id = ?",
                    (new_path, file_id)
                )
                rows_updated = cursor.rowcount
                # The files_fts_au trigger mirrors the path change into the FTS index (correct old
                # terms removed, new terms added) — no manual FTS maintenance needed here.
                conn.commit()
                
                if rows_updated > 0:
                    logger.info(f"Updated file path for ID {file_id} to: {new_path}")
                    return True
                else:
                    logger.warning(f"No file found with ID {file_id}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error updating file path for {file_id}: {e}")
            return False

    def update_file_paths_batch(self, updates: List[tuple]) -> int:
        """Update file_path for MANY files in one connection + one transaction.

        Behaves like calling update_file_path() for each ``(file_id, new_path)``
        pair, but without the per-file connect→commit→close round-trip — the main
        cost when an organize moves thousands of files. Mirrors the per-file logic
        exactly: stale-path cleanup, main-table update, and the external-content
        FTS delete+reinsert. Per-file ``update_file_path`` is left untouched (the
        auto-watcher still uses it). Returns the number of main-table rows updated.
        """
        if not updates:
            return 0
        rows_updated_total = 0
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # NORMAL is safe under WAL and removes an fsync per commit — fine
                # for bulk path updates (worst case on power loss = redo the move).
                try:
                    cursor.execute("PRAGMA synchronous=NORMAL")
                except Exception:
                    pass
                for file_id, new_path in updates:
                    try:
                        # Remove any stale entry occupying the new path (different id).
                        cursor.execute(
                            "DELETE FROM files WHERE file_path = ? AND id != ?",
                            (new_path, file_id)
                        )
                        # (The stale DELETE above fires the files_fts_ad trigger, which removes its
                        # FTS entry — no manual files_fts cleanup needed.)
                        # Update main table.
                        cursor.execute(
                            "UPDATE files SET file_path = ? WHERE id = ?",
                            (new_path, file_id)
                        )
                        if cursor.rowcount > 0:
                            rows_updated_total += 1
                        # The files_fts_au trigger mirrors each path change into the FTS index.
                    except Exception as row_err:
                        logger.warning(f"Batch path update failed for id {file_id}: {row_err}")
                        continue
                conn.commit()
            # FTS maintenance is handled by the files_fts_* triggers on `files` — no manual sync
            # or post-commit rebuild needed here.
            logger.info(f"Batch-updated {rows_updated_total}/{len(updates)} file paths in database")
            return rows_updated_total
        except Exception as e:
            logger.error(f"Error in batch path update: {e}")
            return rows_updated_total

    def update_file_path_by_old_path(self, old_path: str, new_path: str) -> bool:
        """Update a file's path when we only know its CURRENT (old) path.

        Used by the Revert flow on the Organize page: we know the move we
        applied earlier (from-path → to-path), and we want to move the
        file's DB row back from to-path → from-path. We don't track DB ids
        in the move log, so we look the row up by its current path.

        Returns True only when the row was found AND updated.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM files WHERE file_path = ?", (old_path,))
                row = cursor.fetchone()
                if not row:
                    logger.debug(f"update_file_path_by_old_path: no row at {old_path}")
                    return False
                file_id = row[0]
        except Exception as e:
            logger.error(f"update_file_path_by_old_path lookup failed for {old_path}: {e}")
            return False
        return self.update_file_path(file_id, new_path)

    def delete_file(self, file_id: int) -> bool:
        """
        Delete a file entry from the database.

        Args:
            file_id: The ID of the file to delete
            
        Returns:
            True if successful, False otherwise
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Delete from main table (the files_fts_ad trigger removes its FTS entry)
                cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
                deleted = cursor.rowcount

                # Delete from embeddings if exists
                cursor.execute("DELETE FROM embeddings WHERE file_id = ?", (file_id,))
                
                conn.commit()
                
                if deleted > 0:
                    logger.info(f"Deleted file entry with ID {file_id}")
                    return True
                return False
                
        except Exception as e:
            logger.error(f"Error deleting file {file_id}: {e}")
            return False
    
    def delete_file_by_path(self, file_path: str) -> bool:
        """
        Delete a file entry from the database by path.
        
        Args:
            file_path: The path of the file to delete
            
        Returns:
            True if successful, False otherwise
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Get the file ID first
                cursor.execute("SELECT id FROM files WHERE file_path = ?", (file_path,))
                row = cursor.fetchone()
                if not row:
                    return False
                
                file_id = row[0]
                
                # Delete from all tables (the files_fts_ad trigger removes the FTS entry)
                cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
                cursor.execute("DELETE FROM embeddings WHERE file_id = ?", (file_id,))
                
                conn.commit()
                logger.info(f"Deleted file entry for path: {file_path}")
                return True
                
        except Exception as e:
            logger.error(f"Error deleting file by path {file_path}: {e}")
            return False
    
    def cleanup_stale_entries(self, progress_callback=None) -> Dict[str, int]:
        """
        Remove database entries for files that no longer exist on disk.
        This helps prevent UNIQUE constraint errors and keeps the database clean.
        
        Args:
            progress_callback: Optional callback(current, total) for progress updates
            
        Returns:
            Dictionary with 'checked', 'removed', and 'errors' counts
        """
        stats = {'checked': 0, 'removed': 0, 'errors': 0}
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Get all file paths and IDs
                cursor.execute("SELECT id, file_path FROM files")
                all_files = cursor.fetchall()
                stats['checked'] = len(all_files)
                
                stale_ids = []
                
                for i, (file_id, file_path) in enumerate(all_files):
                    if progress_callback and i % 100 == 0:
                        progress_callback(i, len(all_files))
                    
                    # Check if file exists
                    if not Path(file_path).exists():
                        stale_ids.append(file_id)
                
                # Batch delete stale entries
                if stale_ids:
                    placeholders = ','.join('?' * len(stale_ids))
                    
                    cursor.execute(f"DELETE FROM files WHERE id IN ({placeholders})", stale_ids)
                    # (the files_fts_ad trigger removes each row's FTS entry)
                    cursor.execute(f"DELETE FROM embeddings WHERE file_id IN ({placeholders})", stale_ids)
                    
                    stats['removed'] = len(stale_ids)
                    conn.commit()
                    logger.info(f"Cleanup: removed {len(stale_ids)} stale entries out of {len(all_files)} checked")
                
                if progress_callback:
                    progress_callback(len(all_files), len(all_files))
                    
        except Exception as e:
            logger.error(f"Error during stale entry cleanup: {e}")
            stats['errors'] = 1
        
        return stats
    
    def add_file(self, file_data: Dict[str, Any]) -> bool:
        """
        Add or update a file in the index.
        
        Args:
            file_data: Dictionary containing file metadata
            
        Returns:
            True if successful, False otherwise
        """
        # DIAGNOSTIC: Log add_file calls with tags info
        file_name = file_data.get('name', 'unknown')
        incoming_tags = file_data.get('tags')
        logger.warning(f"[DB_WRITE] add_file called: file='{file_name}', incoming_tags={repr(incoming_tags)[:100]}")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Prepare data
                file_path = file_data.get('source_path', '')
                file_name = file_data.get('name', '')
                file_extension = file_data.get('extension', '')
                file_size = file_data.get('size', 0)
                mime_type = file_data.get('mime_type', '')
                category = file_data.get('category', 'Misc')
                has_ocr = file_data.get('has_ocr', False)
                ocr_text = file_data.get('ocr_text', '')

                # Preserve existing AI/user-enriched fields if this update doesn't provide them.
                # This prevents accidental wiping when a "refresh/reindex" path only recomputes basic metadata.
                try:
                    cursor.execute(
                        "SELECT label, tags, caption, ocr_text, has_ocr, ai_source, vision_confidence, metadata, user_tags "
                        "FROM files WHERE file_path = ?",
                        (file_path,),
                    )
                    existing = cursor.fetchone()
                except Exception:
                    existing = None

                # label/caption: preserve if incoming empty
                if existing is not None:
                    if not (file_data.get('label') or '').strip():
                        file_data['label'] = existing['label'] if 'label' in existing.keys() else file_data.get('label')
                    if not (file_data.get('caption') or '').strip():
                        file_data['caption'] = existing['caption'] if 'caption' in existing.keys() else file_data.get('caption')

                    # tags: preserve if incoming missing/empty
                    incoming_tags = file_data.get('tags')
                    incoming_list = incoming_tags if isinstance(incoming_tags, list) else _parse_tags_value(incoming_tags)
                    if not incoming_list:
                        prev_list = _parse_tags_value(existing['tags']) if 'tags' in existing.keys() else None
                        if prev_list:
                            file_data['tags'] = prev_list

                    # tags the person added (Add Tags, or typed in the Tags cell) survive a re-read, first - Re-index
                    # used to replace them with the fresh AI tags (Mac a9cad3d)
                    user = _parse_tags_value(file_data.get('user_tags')) or (
                        _parse_tags_value(existing['user_tags']) if 'user_tags' in existing.keys() else None) or []
                    if user:
                        cur = file_data.get('tags')
                        cur = cur if isinstance(cur, list) else (_parse_tags_value(cur) or [])
                        mine = {t.lower() for t in user}
                        file_data['tags'] = user + [t for t in cur if t.lower() not in mine]
                        file_data['user_tags'] = user

                    # ocr_text: preserve if incoming empty but existing has OCR text
                    if (not (ocr_text or '').strip()) and ('ocr_text' in existing.keys()) and (existing['ocr_text'] or '').strip():
                        ocr_text = existing['ocr_text']
                        has_ocr = bool(existing['has_ocr']) if 'has_ocr' in existing.keys() else has_ocr
                        file_data['ocr_text'] = ocr_text
                        file_data['has_ocr'] = has_ocr

                    # ai_source / vision_confidence: preserve if incoming missing
                    if file_data.get('ai_source') is None and 'ai_source' in existing.keys():
                        file_data['ai_source'] = existing['ai_source']
                    if file_data.get('vision_confidence') is None and 'vision_confidence' in existing.keys():
                        file_data['vision_confidence'] = existing['vision_confidence']
                
                # Get file dates
                try:
                    file_path_obj = Path(file_path)
                    if file_path_obj.exists():
                        stat = file_path_obj.stat()
                        created_date = datetime.fromtimestamp(stat.st_ctime).isoformat()
                        modified_date = datetime.fromtimestamp(stat.st_mtime).isoformat()
                    else:
                        created_date = modified_date = datetime.now().isoformat()
                except:
                    created_date = modified_date = datetime.now().isoformat()
                
                # Try to get original date from file metadata (EXIF, Office docs, PDFs, etc.)
                original_date = None
                try:
                    from app.core.metadata_utils import get_file_original_date
                    orig_dt = get_file_original_date(file_path)
                    if orig_dt:
                        original_date = orig_dt.isoformat()
                        logger.debug(f"Original date for {file_name}: {original_date}")
                except Exception as e:
                    logger.debug(f"Could not get original date for {file_name}: {e}")
                
                indexed_date = datetime.now().isoformat()
                
                # Store additional metadata as JSON
                metadata = {
                    'is_file': file_data.get('is_file', False),
                    'is_dir': file_data.get('is_dir', False),
                    'error': file_data.get('error', None),
                    # Persist extra AI details (not in main schema) for UI debug
                    'ai_type': file_data.get('type', None) or file_data.get('label', None),
                    'purpose': file_data.get('purpose', None),
                    'suggested_filename': file_data.get('suggested_filename', None),
                    'detected_text': file_data.get('detected_text', None),
                    'description': file_data.get('description', None),
                }

                # Merge existing metadata if present and new values are None
                if existing is not None and 'metadata' in existing.keys() and existing['metadata']:
                    try:
                        prev_meta = json.loads(existing['metadata']) if isinstance(existing['metadata'], str) else {}
                        if isinstance(prev_meta, dict):
                            for k, v in prev_meta.items():
                                if metadata.get(k) is None and v is not None:
                                    metadata[k] = v
                    except Exception:
                        pass
                
                # add_file re-inserts via INSERT OR REPLACE, which changes the row id; recursive
                # triggers make that REPLACE fire the delete trigger so the old id's FTS entry is
                # removed instead of orphaned. (Per-connection.)
                cursor.execute("PRAGMA recursive_triggers=ON")
                # Insert or update file
                cursor.execute("""
                    INSERT OR REPLACE INTO files (
                        file_path, file_name, file_extension, file_size,
                        mime_type, category, created_date, modified_date,
                        indexed_date, has_ocr, ocr_text,
                        label, tags, caption, vision_confidence,
                        content_hash, last_indexed_at, ai_source, user_tags,
                        metadata, original_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    file_path, file_name, file_extension, file_size,
                    mime_type, category, created_date, modified_date,
                    indexed_date, has_ocr, ocr_text,
                    file_data.get('label', None),
                    json.dumps(file_data.get('tags', [])) if isinstance(file_data.get('tags'), list) else (file_data.get('tags') if isinstance(file_data.get('tags'), str) else None),
                    file_data.get('caption', None),
                    float(file_data.get('vision_confidence', 0)) if file_data.get('vision_confidence') is not None else None,
                    file_data.get('content_hash', None),
                    file_data.get('last_indexed_at', None),
                    file_data.get('ai_source', None),
                    json.dumps(file_data.get('user_tags', [])) if isinstance(file_data.get('user_tags'), list) else (file_data.get('user_tags') if isinstance(file_data.get('user_tags'), str) else None),
                    json.dumps(metadata),
                    original_date
                ))
                
                # FTS is mirrored automatically by the files_fts_* triggers on `files`.
                conn.commit()
                logger.debug(f"Indexed file: {file_path}")
                return True
                
        except Exception as e:
            logger.error(f"Error indexing file {file_data.get('name', 'unknown')}: {e}")
            return False
    
    def search_files(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Search files using full-text search.
        
        Args:
            query: Search query string
            limit: Maximum number of results
            
        Returns:
            List of matching file dictionaries
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # Use FTS5 bm25() ranking (lower is better). Fall back to LIKE on error.
                try:
                    cursor.execute(
                        """
                        SELECT f.*, bm25(files_fts) AS rank
                        FROM files f
                        JOIN files_fts ON f.id = files_fts.rowid
                        WHERE files_fts MATCH ?
                        ORDER BY rank ASC
                        LIMIT ?
                        """,
                        (query, limit),
                    )
                    rows = cursor.fetchall()
                except Exception:
                    # Fallback: simple LIKE across several columns
                    like = f"%{query}%"
                    cursor.execute(
                        """
                        SELECT *, 0 AS rank FROM files
                        WHERE file_name LIKE ? OR category LIKE ? OR ocr_text LIKE ? OR caption LIKE ? OR tags LIKE ?
                        ORDER BY file_name
                        LIMIT ?
                        """,
                        (like, like, like, like, like, limit),
                    )
                    rows = cursor.fetchall()

                results = []
                for row in rows:
                    file_dict = {
                        'id': row['id'],
                        'file_path': row['file_path'],
                        'file_name': row['file_name'],
                        'file_extension': row['file_extension'],
                        'file_size': row['file_size'],
                        'mime_type': row['mime_type'],
                        'category': row['category'],
                        'created_date': row['created_date'],
                        'modified_date': row['modified_date'],
                        'indexed_date': row['indexed_date'],
                        'original_date': row['original_date'] if 'original_date' in row.keys() else None,
                        'has_ocr': bool(row['has_ocr']),
                        'ocr_text': row['ocr_text'],
                        'label': row['label'] if 'label' in row.keys() else None,
                        'tags': _parse_tags_value(row['tags']),
                        'caption': row['caption'] if 'caption' in row.keys() else None,
                        'vision_confidence': row['vision_confidence'] if 'vision_confidence' in row.keys() else None,
                        'metadata': json.loads(row['metadata']) if row['metadata'] else {},
                        'rank': row['rank'] if 'rank' in row.keys() else 0,
                    }
                    results.append(file_dict)

                self._log_search(query, len(results))
                logger.info(f"Search for '{query}' returned {len(results)} results")
                return results

        except Exception as e:
            logger.error(f"Error searching files: {e}")
            return []

    def search_files_advanced(
        self, fts_terms: List[str], filters: Dict[str, Any], limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Search with parsed terms/filters, with robust fallbacks."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # Build FTS MATCH string using prefix queries per token
                # Example: thumbnail -> 'thumbnail*'
                # Join with OR to broaden matches across tokens
                tokens = [_fts_term(t) for t in fts_terms or [] if any(ch.isalnum() for ch in str(t))]
                match = " OR ".join(tokens) if tokens else None

                # Base FTS query. Rank text matches by bm25 relevance (bm25 is negative, lower =
                # better match) so the most relevant files come first. For a no-match browse
                # (match is None) there is nothing to score — use a constant and keep name order.
                # rank_expr is a fixed literal chosen by `match` (no user input) — safe to inline.
                rank_expr = "bm25(files_fts)" if match else "0.0"
                sql = (
                    f"SELECT f.*, {rank_expr} AS rank FROM files f "
                    "JOIN files_fts ON f.id = files_fts.rowid "
                )
                params: List[Any] = []
                if match:
                    sql += "WHERE files_fts MATCH ?"
                    params.append(match)
                else:
                    sql += "WHERE 1=1"

                # Filters
                if filters.get("label"):
                    sql += " AND (f.label = ? OR f.label LIKE ?)"
                    lbl = filters["label"]
                    params.extend([lbl, f"%{lbl}%"])
                if filters.get("has_ocr"):
                    sql += " AND f.has_ocr = 1"
                if filters.get("has_vision"):
                    sql += " AND (f.label IS NOT NULL OR f.caption IS NOT NULL)"
                if filters.get("tags"):
                    # simple LIKE match on serialized tags
                    for tg in filters["tags"]:
                        sql += " AND f.tags LIKE ?"
                        params.append(f"%{tg}%")

                # Relevance order when we have a text match (bm25 ASC = best first); otherwise name.
                sql += (" ORDER BY rank ASC LIMIT ?" if match else " ORDER BY f.file_name LIMIT ?")
                params.append(limit)

                try:
                    cursor.execute(sql, params)
                    rows = cursor.fetchall()
                except Exception:
                    rows = []

                # If FTS returns nothing or was skipped, fallback to LIKE
                if not rows:
                    sql2 = "SELECT * FROM files WHERE 1=1"
                    p2: List[Any] = []
                    if fts_terms:
                        # Build ORs per token for broader LIKE search
                        like_clauses = []
                        for _ in fts_terms:
                            like_clauses.append("file_name LIKE ?")
                            like_clauses.append("category LIKE ?")
                            like_clauses.append("ocr_text LIKE ?")
                            like_clauses.append("caption LIKE ?")
                            like_clauses.append("tags LIKE ?")
                        if like_clauses:
                            sql2 += " AND (" + " OR ".join(like_clauses) + ")"
                        for term in fts_terms:
                            pattern = f"%{term}%"
                            p2.extend([pattern, pattern, pattern, pattern, pattern])
                    # Filters
                    if filters.get("label"):
                        sql2 += " AND (label = ? OR label LIKE ?)"
                        lbl = filters["label"]
                        p2.extend([lbl, f"%{lbl}%"])
                    if filters.get("has_ocr"):
                        sql2 += " AND has_ocr = 1"
                    if filters.get("has_vision"):
                        sql2 += " AND (label IS NOT NULL OR caption IS NOT NULL)"
                    sql2 += " ORDER BY file_name LIMIT ?"
                    p2.append(limit)
                    cursor.execute(sql2, p2)
                    rows = cursor.fetchall()

                results = []
                for row in rows:
                    # row is sqlite3.Row; access by column names to avoid index drift
                    try:
                        results.append({
                            'id': row['id'],
                            'file_path': row['file_path'],
                            'file_name': row['file_name'],
                            'file_extension': row['file_extension'],
                            'file_size': row['file_size'],
                            'mime_type': row['mime_type'],
                            'category': row['category'],
                            'created_date': row['created_date'],
                            'modified_date': row['modified_date'],
                            'indexed_date': row['indexed_date'],
                            'original_date': row['original_date'] if 'original_date' in row.keys() else None,
                            'has_ocr': bool(row['has_ocr']),
                            'ocr_text': row['ocr_text'],
                            'label': row['label'] if 'label' in row.keys() else None,
                            'tags': _parse_tags_value(row['tags']),
                            'caption': row['caption'] if 'caption' in row.keys() else None,
                            'ai_source': row['ai_source'] if 'ai_source' in row.keys() else None,
                            'vision_confidence': row['vision_confidence'] if 'vision_confidence' in row.keys() else None,
                            'metadata': json.loads(row['metadata']) if row['metadata'] else {},
                            # bm25 is negative (lower = better); flip to positive higher-is-better so
                            # the service-layer sort (desc) and relevance_score work. The LIKE-fallback
                            # rows (sql2) have no 'rank' column → default 0.
                            'rank': (-row['rank']) if ('rank' in row.keys() and row['rank'] is not None) else 0,
                        })
                    except Exception:
                        # If any column missing/malformed, skip gracefully
                        continue
                return results
        except Exception as e:
            logger.error(f"Advanced search error: {e}")
            return []
    
    def get_file_by_name(self, file_name: str) -> Optional[Dict[str, Any]]:
        """
        Get file information by filename (not full path).
        Useful when a file has been moved but we want to find it by name.
        
        Args:
            file_name: The filename (without path)
            
        Returns:
            File dictionary or None if not found
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM files WHERE file_name = ?", (file_name,))
                row = cursor.fetchone()
                
                if row:
                    return {
                        'id': row['id'],
                        'file_path': row['file_path'],
                        'file_name': row['file_name'],
                        'file_extension': row['file_extension'],
                        'file_size': row['file_size'],
                        'mime_type': row['mime_type'],
                        'category': row['category'],
                        'created_date': row['created_date'],
                        'modified_date': row['modified_date'],
                        'indexed_date': row['indexed_date'],
                        'original_date': row['original_date'] if 'original_date' in row.keys() else None,
                        'has_ocr': bool(row['has_ocr']),
                        'ocr_text': row['ocr_text'],
                        'label': row['label'] if 'label' in row.keys() else None,
                        'tags': _parse_tags_value(row['tags']),
                        'caption': row['caption'] if 'caption' in row.keys() else None,
                        'vision_confidence': row['vision_confidence'] if 'vision_confidence' in row.keys() else None,
                        'content_hash': row['content_hash'] if 'content_hash' in row.keys() else None,
                        'metadata': json.loads(row['metadata']) if row['metadata'] else {}
                    }
                return None
                
        except Exception as e:
            logger.error(f"Error getting file by name {file_name}: {e}")
            return None

    def get_file_by_hash(self, content_hash: str) -> Optional[Dict[str, Any]]:
        """Look up a file by its SHA-256 content hash.

        Returns the first match (there can be duplicates if the same content
        exists at multiple paths). Used by the Organize-New-Only watcher to
        recognize a moved/renamed/copied pre-existing file by its bytes
        instead of by its path.
        """
        if not content_hash:
            return None
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, file_path, file_name, content_hash FROM files WHERE content_hash = ? LIMIT 1",
                    (content_hash,)
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return {
                    'id': row['id'],
                    'file_path': row['file_path'],
                    'file_name': row['file_name'],
                    'content_hash': row['content_hash'],
                }
        except Exception as e:
            logger.error(f"Error fetching file by hash: {e}")
            return None

    def get_file_by_path(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Get file information by path.
        
        Args:
            file_path: Path to the file
            
        Returns:
            File dictionary or None if not found
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM files WHERE file_path = ?", (file_path,))
                row = cursor.fetchone()
                
                if row:
                    return {
                        'id': row['id'],
                        'file_path': row['file_path'],
                        'file_name': row['file_name'],
                        'file_extension': row['file_extension'],
                        'file_size': row['file_size'],
                        'mime_type': row['mime_type'],
                        'category': row['category'],
                        'created_date': row['created_date'],
                        'modified_date': row['modified_date'],
                        'indexed_date': row['indexed_date'],
                        'original_date': row['original_date'] if 'original_date' in row.keys() else None,
                        'has_ocr': bool(row['has_ocr']),
                        'ocr_text': row['ocr_text'],
                        'label': row['label'] if 'label' in row.keys() else None,
                        'tags': _parse_tags_value(row['tags']),
                        'caption': row['caption'] if 'caption' in row.keys() else None,
                        'vision_confidence': row['vision_confidence'] if 'vision_confidence' in row.keys() else None,
                        'content_hash': row['content_hash'] if 'content_hash' in row.keys() else None,
                        'metadata': json.loads(row['metadata']) if row['metadata'] else {}
                    }
                return None
                
        except Exception as e:
            logger.error(f"Error getting file {file_path}: {e}")
            return None

    def get_filenames_with_tags(self) -> set:
        """
        Get a set of all filenames that have tags in the database.
        Used for quick lookup to determine if a file needs indexing.
        
        Returns:
            Set of filenames (without path) that have non-empty tags
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Get filenames where tags is not null and not empty
                cursor.execute("""
                    SELECT file_name FROM files 
                    WHERE tags IS NOT NULL AND tags != '' AND tags != '[]'
                """)
                rows = cursor.fetchall()
                return {row[0] for row in rows}
        except Exception as e:
            logger.error(f"Error getting filenames with tags: {e}")
            return set()

    def get_file_count(self) -> int:
        """
        Get the total count of indexed files.
        
        Returns:
            Number of files in the database
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM files")
                return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Error getting file count: {e}")
            return 0

    # ---------- Embeddings helpers ----------

    def get_files_by_ids(self, ids: List[int]) -> List[Dict[str, Any]]:
        if not ids:
            return []
        try:
            placeholders = ",".join(["?"] * len(ids))
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"SELECT * FROM files WHERE id IN ({placeholders})", ids)
                rows = cursor.fetchall()
                out: List[Dict[str, Any]] = []
                for row in rows:
                    out.append({
                        'id': row['id'],
                        'file_path': row['file_path'],
                        'file_name': row['file_name'],
                        'file_extension': row['file_extension'],
                        'file_size': row['file_size'],
                        'mime_type': row['mime_type'],
                        'category': row['category'],
                        'created_date': row['created_date'],
                        'modified_date': row['modified_date'],
                        'indexed_date': row['indexed_date'],
                        'original_date': row['original_date'] if 'original_date' in row.keys() else None,
                        'has_ocr': bool(row['has_ocr']),
                        'ocr_text': row['ocr_text'],
                        'label': row['label'],
                        'tags': _parse_tags_value(row['tags']),
                        'caption': row['caption'],
                        'vision_confidence': row['vision_confidence'],
                        'metadata': json.loads(row['metadata']) if row['metadata'] else {},
                    })
                return out
        except Exception as e:
            logger.error(f"Error fetching files by ids: {e}")
            return []
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Get database statistics.
        
        Returns:
            Dictionary with statistics
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Total files
                cursor.execute("SELECT COUNT(*) FROM files")
                total_files = cursor.fetchone()[0]
                
                # Files with OCR
                cursor.execute("SELECT COUNT(*) FROM files WHERE has_ocr = 1")
                files_with_ocr = cursor.fetchone()[0]
                
                # Total size
                cursor.execute("SELECT SUM(file_size) FROM files")
                total_size = cursor.fetchone()[0] or 0
                
                # Categories
                cursor.execute("SELECT category, COUNT(*) FROM files GROUP BY category")
                categories = dict(cursor.fetchall())
                
                return {
                    'total_files': total_files,
                    'files_with_ocr': files_with_ocr,
                    'total_size': total_size,
                    'total_size_mb': round(total_size / (1024 * 1024), 2),
                    'categories': categories
                }
                
        except Exception as e:
            logger.error(f"Error getting statistics: {e}")
            return {}
    
    def _log_search(self, query: str, results_count: int):
        """Log search query."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO search_history (query, timestamp, results_count)
                    VALUES (?, ?, ?)
                """, (query, datetime.now().isoformat(), results_count))
                conn.commit()
        except Exception as e:
            logger.error(f"Error logging search: {e}")
    
    def get_search_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get recent search history.
        
        Args:
            limit: Maximum number of recent searches
            
        Returns:
            List of search history entries
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT query, timestamp, results_count 
                    FROM search_history 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (limit,))
                
                return [
                    {
                        'query': row[0],
                        'timestamp': row[1],
                        'results_count': row[2]
                    }
                    for row in cursor.fetchall()
                ]
                
        except Exception as e:
            logger.error(f"Error getting search history: {e}")
            return []
    
    def clear_index(self):
        """Clear all indexed files, FTS index, and embeddings."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Drop the FTS sync triggers so a full wipe doesn't fire them once per row, clear
                # every table, then recreate the triggers. ('delete-all' empties the FTS index.)
                for _t in ("files_fts_ai", "files_fts_ad", "files_fts_au"):
                    cursor.execute(f"DROP TRIGGER IF EXISTS {_t}")
                cursor.execute("DELETE FROM files")
                try:
                    cursor.execute("INSERT INTO files_fts(files_fts) VALUES('delete-all')")
                except Exception:
                    cursor.execute("DELETE FROM files_fts")
                cursor.execute("DELETE FROM embeddings")
                self._create_fts_triggers(cursor)
                conn.commit()
                logger.info("File index cleared (files, files_fts, embeddings)")
        except Exception as e:
            logger.error(f"Error clearing index: {e}")
            raise  # Re-raise to let caller handle it
    
    def resync_file_dates(self, progress_callback=None) -> Dict[str, int]:
        """
        Re-read file creation/modification dates from Windows filesystem
        and extract EXIF dates for images.
        
        Args:
            progress_callback: Optional callback(current, total) for progress updates
            
        Returns:
            Dict with 'updated', 'not_found', 'errors', 'exif_found' counts
        """
        stats = {'updated': 0, 'not_found': 0, 'errors': 0, 'exif_found': 0}
        
        try:
            from app.core.metadata_utils import get_file_original_date
        except ImportError:
            get_file_original_date = None
            logger.warning("Metadata utils not available, skipping metadata extraction")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Get all file paths
                cursor.execute("SELECT id, file_path FROM files")
                files = cursor.fetchall()
                total = len(files)
                
                logger.info(f"Resyncing dates for {total} files (including EXIF extraction)...")
                
                for i, (file_id, file_path) in enumerate(files):
                    try:
                        path_obj = Path(file_path)
                        if path_obj.exists():
                            stat = path_obj.stat()
                            created_date = datetime.fromtimestamp(stat.st_ctime).isoformat()
                            modified_date = datetime.fromtimestamp(stat.st_mtime).isoformat()
                            
                            # Try to extract original date from file metadata
                            original_date = None
                            if get_file_original_date:
                                try:
                                    orig_dt = get_file_original_date(file_path)
                                    if orig_dt:
                                        original_date = orig_dt.isoformat()
                                        stats['exif_found'] += 1  # Keep stat name for compatibility
                                        logger.debug(f"Metadata date for {path_obj.name}: {original_date}")
                                except Exception as e:
                                    logger.debug(f"Metadata extraction failed for {file_path}: {e}")
                            
                            cursor.execute("""
                                UPDATE files 
                                SET created_date = ?, modified_date = ?, original_date = ?
                                WHERE id = ?
                            """, (created_date, modified_date, original_date, file_id))
                            
                            stats['updated'] += 1
                        else:
                            stats['not_found'] += 1
                            logger.debug(f"File not found: {file_path}")
                    except Exception as e:
                        stats['errors'] += 1
                        logger.warning(f"Error updating dates for {file_path}: {e}")
                    
                    if progress_callback and (i % 10 == 0 or i == total - 1):
                        progress_callback(i + 1, total)
                
                conn.commit()
                logger.info(f"Resync complete: {stats['updated']} updated, {stats['exif_found']} with metadata dates, {stats['not_found']} not found, {stats['errors']} errors")
                
        except Exception as e:
            logger.error(f"Error resyncing file dates: {e}")
            stats['errors'] += 1
        
        return stats
    
    def _create_fts_triggers(self, cursor):
        """(Re)create the triggers that mirror `files` into the external-content FTS index.

        They use old.* on delete (so the correct terms are removed even after the row's content has
        changed) and new.* on insert — so a move/edit/delete can never leave stale terms behind.
        Dropped+recreated so they always match the current column set. IMPORTANT: with these in
        place the app must write ONLY to `files`; it must NOT write to files_fts directly (that
        would double-apply). The one exception is bulk rebuilds (drop triggers, rebuild, recreate).
        """
        cols = "file_name, file_path, category, ocr_text, caption, tags"
        newv = "new.file_name, new.file_path, new.category, new.ocr_text, new.caption, new.tags"
        oldv = "old.file_name, old.file_path, old.category, old.ocr_text, old.caption, old.tags"
        cursor.executescript(f"""
            DROP TRIGGER IF EXISTS files_fts_ai;
            DROP TRIGGER IF EXISTS files_fts_ad;
            DROP TRIGGER IF EXISTS files_fts_au;
            CREATE TRIGGER files_fts_ai AFTER INSERT ON files BEGIN
              INSERT INTO files_fts(rowid, {cols}) VALUES (new.id, {newv});
            END;
            CREATE TRIGGER files_fts_ad AFTER DELETE ON files BEGIN
              INSERT INTO files_fts(files_fts, rowid, {cols}) VALUES('delete', old.id, {oldv});
            END;
            CREATE TRIGGER files_fts_au AFTER UPDATE ON files BEGIN
              INSERT INTO files_fts(files_fts, rowid, {cols}) VALUES('delete', old.id, {oldv});
              INSERT INTO files_fts(rowid, {cols}) VALUES (new.id, {newv});
            END;
        """)

    def _ensure_fts_healthy(self):
        """Safety net for the external-content FTS5 index.

        files_fts can silently desync — its content rows stay present but the inverted index goes
        empty, so MATCH returns nothing and every search degrades to the LIKE fallback (alphabetical,
        unranked). Detect that cheaply and rebuild via the canonical FTS5 'rebuild' command. Purely
        best-effort: ANY failure is swallowed so app startup / indexing is never affected.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                try:
                    n = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                except Exception:
                    return  # files table not ready
                if not n:
                    return  # empty index — nothing to verify
                # Broad common-letter prefix probe: matches filenames/paths/extensions of virtually
                # any non-empty corpus. 0 hits while `files` has rows ⇒ the FTS index is empty/stale.
                probe = "e* OR a* OR t* OR o* OR i* OR n* OR s* OR p* OR f*"
                try:
                    hit = cur.execute(
                        "SELECT 1 FROM files_fts WHERE files_fts MATCH ? LIMIT 1", (probe,)
                    ).fetchone()
                except Exception:
                    hit = None  # MATCH itself errored ⇒ treat as broken
                if hit:
                    return  # healthy
                logger.warning("FTS index empty/stale (%d files, probe found nothing) — rebuilding.", n)
                try:
                    cur.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
                    conn.commit()
                    logger.info("FTS index rebuilt via FTS5 'rebuild'.")
                    return
                except Exception as e:
                    logger.warning("FTS 'rebuild' failed (%s); trying drop+repopulate.", e)
            # 'rebuild' failed (e.g. corrupt schema) → full drop/recreate path (own connection).
            self._auto_rebuild_fts()
        except Exception as e:
            logger.error("FTS health check failed: %s", e)

    def _auto_rebuild_fts(self):
        """
        Automatically rebuild FTS index when corruption is detected.
        This is called internally and doesn't require user interaction.
        Uses a flag to prevent multiple rebuilds in a short period.
        """
        # Check if we've already rebuilt recently (within this session)
        if hasattr(self, '_fts_rebuilt_this_session') and self._fts_rebuilt_this_session:
            logger.debug("FTS already rebuilt this session, skipping")
            return
        
        logger.info("Auto-rebuilding corrupted FTS index...")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Drop corrupted FTS table
                try:
                    cursor.execute("DROP TABLE IF EXISTS files_fts")
                except Exception as e:
                    logger.warning(f"Error dropping FTS table: {e}")
                
                # Recreate FTS table
                cursor.execute("""
                    CREATE VIRTUAL TABLE files_fts USING fts5(
                        file_name,
                        file_path,
                        category,
                        ocr_text,
                        caption,
                        tags,
                        content='files',
                        content_rowid='id'
                    )
                """)
                
                # Populate from main table
                cursor.execute("""
                    INSERT INTO files_fts(rowid, file_name, file_path, category, ocr_text, caption, tags)
                    SELECT id, file_name, file_path, category, ocr_text, caption, tags 
                    FROM files
                """)
                
                conn.commit()
                
                # Mark as rebuilt this session
                self._fts_rebuilt_this_session = True
                
                # Count rows
                cursor.execute("SELECT COUNT(*) FROM files")
                count = cursor.fetchone()[0]
                logger.info(f"FTS index auto-rebuilt successfully with {count} files")
                
        except Exception as e:
            logger.error(f"Auto-rebuild FTS failed: {e}")
    
    def rebuild_fts_index(self, progress_callback=None) -> Dict[str, int]:
        """
        Rebuild the FTS5 full-text search index from scratch.
        
        This is useful when the FTS index becomes corrupted (e.g., "database disk image is malformed").
        The main files table remains intact - only the FTS index is rebuilt.
        
        Returns:
            Dict with 'total', 'indexed', 'errors' counts
        """
        stats = {'total': 0, 'indexed': 0, 'errors': 0}
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Get total count
                cursor.execute("SELECT COUNT(*) FROM files")
                stats['total'] = cursor.fetchone()[0]
                
                if stats['total'] == 0:
                    logger.info("No files to index")
                    return stats
                
                logger.info(f"Rebuilding FTS index for {stats['total']} files...")
                
                # Drop and recreate FTS table
                try:
                    cursor.execute("DROP TABLE IF EXISTS files_fts")
                except Exception as e:
                    logger.warning(f"Error dropping FTS table: {e}")
                
                # Recreate FTS table
                cursor.execute("""
                    CREATE VIRTUAL TABLE files_fts USING fts5(
                        file_name,
                        file_path,
                        category,
                        ocr_text,
                        caption,
                        tags,
                        content='files',
                        content_rowid='id'
                    )
                """)
                
                # Get all files
                cursor.execute("""
                    SELECT id, file_name, file_path, category, ocr_text, caption, tags
                    FROM files
                """)
                rows = cursor.fetchall()
                
                # Insert into FTS index
                for i, row in enumerate(rows):
                    try:
                        cursor.execute("""
                            INSERT INTO files_fts(rowid, file_name, file_path, category, ocr_text, caption, tags)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, row)
                        stats['indexed'] += 1
                    except Exception as e:
                        stats['errors'] += 1
                        logger.warning(f"Error indexing file {row[0]}: {e}")
                    
                    if progress_callback and (i % 50 == 0 or i == len(rows) - 1):
                        progress_callback(i + 1, len(rows))
                
                conn.commit()
                logger.info(f"FTS rebuild complete: {stats['indexed']}/{stats['total']} indexed, {stats['errors']} errors")
                
        except Exception as e:
            logger.error(f"Error rebuilding FTS index: {e}")
            stats['errors'] += 1
        
        return stats


# Global file index instance
file_index = FileIndex()
