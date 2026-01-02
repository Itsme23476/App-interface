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

class FileIndex:
    """SQLite database for file indexing and search."""
    
    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the file index database."""
        if db_path is None:
            db_path = settings.get_app_data_dir() / "file_index.db"
        
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()
    
    def _init_database(self):
        """Initialize database tables."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
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
                for stmt in to_add:
                    cursor.execute(stmt)
            except Exception as e:
                logger.warning(f"Schema migration warning: {e}")
            
            # Create full-text search index
            # Recreate FTS with latest schema (drop if exists)
            try:
                cursor.execute("DROP TABLE IF EXISTS files_fts")
            except Exception:
                pass
            cursor.execute(
                """
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
                """
            )

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
                # update FTS mirror for edited fields
                if field in {"caption", "tags", "label"}:
                    cursor.execute(
                        "INSERT OR REPLACE INTO files_fts (rowid, file_name, file_path, category, ocr_text, caption, tags) "
                        "SELECT id, file_name, file_path, category, ocr_text, caption, tags FROM files WHERE id = ?",
                        (file_id,)
                    )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Error updating {field} for {file_id}: {e}")
            return False
    
    def add_file(self, file_data: Dict[str, Any]) -> bool:
        """
        Add or update a file in the index.
        
        Args:
            file_data: Dictionary containing file metadata
            
        Returns:
            True if successful, False otherwise
        """
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
                
                # Insert or update file
                cursor.execute("""
                    INSERT OR REPLACE INTO files (
                        file_path, file_name, file_extension, file_size,
                        mime_type, category, created_date, modified_date,
                        indexed_date, has_ocr, ocr_text,
                        label, tags, caption, vision_confidence,
                        content_hash, last_indexed_at, ai_source, user_tags,
                        metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(metadata)
                ))
                
                # Get the rowid for FTS update
                rowid = cursor.lastrowid
                if rowid == 0:  # If it was an UPDATE, get the existing rowid
                    cursor.execute("SELECT id FROM files WHERE file_path = ?", (file_path,))
                    rowid = cursor.fetchone()[0]
                
                # Update FTS index
                cursor.execute("""
                    INSERT OR REPLACE INTO files_fts (
                        rowid, file_name, file_path, category, ocr_text, caption, tags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    rowid, file_name, file_path, category, ocr_text,
                    file_data.get('caption', None),
                    (", ".join(file_data.get('tags')) if isinstance(file_data.get('tags'), list) else file_data.get('tags'))
                ))
                
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
                        'has_ocr': bool(row['has_ocr']),
                        'ocr_text': row['ocr_text'],
                        'label': row['label'] if 'label' in row.keys() else None,
                        'tags': json.loads(row['tags']) if row['tags'] else None,
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
                if fts_terms:
                    # Use FTS5 prefix matching without quotes: token*
                    tokens = [f"{t}*" for t in fts_terms]
                    match = " OR ".join(tokens)
                else:
                    match = None

                # Base FTS query
                sql = (
                    "SELECT f.*, 1 as rank FROM files f "
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

                sql += " ORDER BY f.file_name LIMIT ?"
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
                            'has_ocr': bool(row['has_ocr']),
                            'ocr_text': row['ocr_text'],
                            'label': row['label'] if 'label' in row.keys() else None,
                            'tags': json.loads(row['tags']) if row['tags'] else None,
                            'caption': row['caption'] if 'caption' in row.keys() else None,
                            'ai_source': row['ai_source'] if 'ai_source' in row.keys() else None,
                            'vision_confidence': row['vision_confidence'] if 'vision_confidence' in row.keys() else None,
                            'metadata': json.loads(row['metadata']) if row['metadata'] else {},
                            'rank': 0,
                        })
                    except Exception:
                        # If any column missing/malformed, skip gracefully
                        continue
                return results
        except Exception as e:
            logger.error(f"Advanced search error: {e}")
            return []
    
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
                        'has_ocr': bool(row['has_ocr']),
                        'ocr_text': row['ocr_text'],
                        'label': row['label'] if 'label' in row.keys() else None,
                        'tags': json.loads(row['tags']) if row['tags'] else None,
                        'caption': row['caption'] if 'caption' in row.keys() else None,
                        'vision_confidence': row['vision_confidence'] if 'vision_confidence' in row.keys() else None,
                        'metadata': json.loads(row['metadata']) if row['metadata'] else {}
                    }
                return None
                
        except Exception as e:
            logger.error(f"Error getting file {file_path}: {e}")
            return None

    # ---------- Embeddings helpers ----------
    def upsert_embedding(self, file_id: int, model: str, vector: List[float]) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO embeddings(file_id, model, dim, vector, updated_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        model=excluded.model,
                        dim=excluded.dim,
                        vector=excluded.vector,
                        updated_at=excluded.updated_at
                    """,
                    (file_id, model, len(vector), json.dumps(vector), datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error upserting embedding for {file_id}: {e}")

    def get_all_embeddings(self) -> List[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM embeddings")
                rows = cursor.fetchall()
                return [
                    {
                        'file_id': r['file_id'],
                        'model': r['model'],
                        'dim': r['dim'],
                        'vector': json.loads(r['vector']) if r['vector'] else [],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"Error reading embeddings: {e}")
            return []

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
                        'has_ocr': bool(row['has_ocr']),
                        'ocr_text': row['ocr_text'],
                        'label': row['label'],
                        'tags': json.loads(row['tags']) if row['tags'] else None,
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
        """Clear all indexed files."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM files")
                cursor.execute("DELETE FROM files_fts")
                conn.commit()
                logger.info("File index cleared")
        except Exception as e:
            logger.error(f"Error clearing index: {e}")


# Global file index instance
file_index = FileIndex()
