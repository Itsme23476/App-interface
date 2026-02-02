"""
File operations for managing indexed files in the database.
These operations work on the app's index, not the actual files on disk.
"""

import logging
import csv
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class FileOperations:
    """Operations for managing indexed files."""
    
    def __init__(self, file_index):
        """
        Initialize with a FileIndex instance.
        
        Args:
            file_index: The FileIndex database instance
        """
        self.file_index = file_index
    
    def remove_from_index(self, file_ids: List[int]) -> Dict[str, int]:
        """
        Remove files from the index by their IDs.
        Does NOT delete actual files from disk.
        
        Args:
            file_ids: List of file IDs to remove
            
        Returns:
            Dict with 'removed' and 'errors' counts
        """
        import sqlite3
        
        stats = {'removed': 0, 'errors': 0}
        
        if not file_ids:
            return stats
        
        try:
            with sqlite3.connect(self.file_index.db_path) as conn:
                cursor = conn.cursor()
                
                for file_id in file_ids:
                    try:
                        # Remove from FTS index first
                        cursor.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
                        # Remove from embeddings
                        cursor.execute("DELETE FROM embeddings WHERE file_id = ?", (file_id,))
                        # Remove from files table
                        cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
                        stats['removed'] += 1
                    except Exception as e:
                        logger.error(f"Error removing file ID {file_id}: {e}")
                        stats['errors'] += 1
                
                conn.commit()
                logger.info(f"Removed {stats['removed']} files from index")
                
        except Exception as e:
            logger.error(f"Error in remove_from_index: {e}")
            stats['errors'] += len(file_ids)
        
        return stats
    
    def reindex_files(self, file_paths: List[str], progress_callback=None) -> Dict[str, int]:
        """
        Re-index specified files to update their metadata.
        
        Args:
            file_paths: List of file paths to re-index
            progress_callback: Optional callback(current, total) for progress
            
        Returns:
            Dict with 'updated', 'not_found', 'errors' counts
        """
        stats = {'updated': 0, 'not_found': 0, 'errors': 0}
        total = len(file_paths)
        from app.core.search import SearchService
        svc = SearchService()
        
        for i, file_path in enumerate(file_paths):
            try:
                path_obj = Path(file_path)
                
                if not path_obj.exists():
                    stats['not_found'] += 1
                    logger.debug(f"File not found for reindex: {file_path}")
                    continue

                # Force AI re-index using the same pipeline as normal indexing
                result = svc._process_single_file({'source_path': str(path_obj)}, path_obj.parent, force_ai=True)
                err = result.pop('_error', None)
                result.pop('_skipped', None)
                result.pop('_cancelled', None)
                result.pop('_file_path', None)
                if err:
                    raise RuntimeError(err)

                # Update in database (preserves existing fields if AI returns empty)
                if self.file_index.add_file(result):
                    stats['updated'] += 1
                else:
                    stats['errors'] += 1
                    
            except Exception as e:
                logger.error(f"Error reindexing {file_path}: {e}")
                stats['errors'] += 1
            
            if progress_callback and (i % 5 == 0 or i == total - 1):
                progress_callback(i + 1, total)
        
        logger.info(f"Reindex complete: {stats['updated']} updated, {stats['not_found']} not found, {stats['errors']} errors")
        return stats
    
    def batch_add_tags(self, file_ids: List[int], new_tags: List[str]) -> Dict[str, int]:
        """
        Add tags to multiple files (appends to existing tags).
        
        Args:
            file_ids: List of file IDs to update
            new_tags: List of tags to add
            
        Returns:
            Dict with 'updated' and 'errors' counts
        """
        import sqlite3
        import json
        
        stats = {'updated': 0, 'errors': 0}
        
        if not file_ids or not new_tags:
            return stats
        
        try:
            with sqlite3.connect(self.file_index.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                for file_id in file_ids:
                    try:
                        # Get current tags
                        cursor.execute("SELECT tags FROM files WHERE id = ?", (file_id,))
                        row = cursor.fetchone()
                        
                        if row:
                            current_tags = []
                            if row['tags']:
                                try:
                                    # tags may be stored as JSON list or comma-separated string
                                    from app.core.database import _parse_tags_value
                                    current_tags = _parse_tags_value(row['tags']) or []
                                    if not isinstance(current_tags, list):
                                        current_tags = [current_tags] if current_tags else []
                                except:
                                    current_tags = [row['tags']] if row['tags'] else []
                            
                            # Merge tags (avoid duplicates)
                            merged_tags = list(set(current_tags + new_tags))
                            
                            # Update in database
                            cursor.execute(
                                "UPDATE files SET tags = ? WHERE id = ?",
                                (json.dumps(merged_tags), file_id)
                            )
                            
                            # Update FTS index
                            cursor.execute(
                                """INSERT OR REPLACE INTO files_fts (rowid, file_name, file_path, category, ocr_text, caption, tags)
                                   SELECT id, file_name, file_path, category, ocr_text, caption, tags FROM files WHERE id = ?""",
                                (file_id,)
                            )
                            
                            stats['updated'] += 1
                        else:
                            stats['errors'] += 1
                            
                    except Exception as e:
                        logger.error(f"Error adding tags to file ID {file_id}: {e}")
                        stats['errors'] += 1
                
                conn.commit()
                logger.info(f"Added tags to {stats['updated']} files")
                
        except Exception as e:
            logger.error(f"Error in batch_add_tags: {e}")
            stats['errors'] += len(file_ids)
        
        return stats
    
    def get_file_paths(self, file_ids: List[int]) -> List[str]:
        """
        Get file paths for given file IDs.
        
        Args:
            file_ids: List of file IDs
            
        Returns:
            List of file paths
        """
        import sqlite3
        
        if not file_ids:
            return []
        
        paths = []
        try:
            placeholders = ",".join(["?"] * len(file_ids))
            with sqlite3.connect(self.file_index.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT file_path FROM files WHERE id IN ({placeholders})",
                    file_ids
                )
                paths = [row[0] for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error getting file paths: {e}")
        
        return paths
    
    def export_file_list(
        self, 
        files: List[Dict[str, Any]], 
        output_path: str, 
        format: str = 'csv'
    ) -> bool:
        """
        Export file list to CSV or TXT.
        
        Args:
            files: List of file dictionaries
            output_path: Path to save the export
            format: 'csv' or 'txt'
            
        Returns:
            True if successful, False otherwise
        """
        try:
            if format == 'csv':
                with open(output_path, 'w', newline='', encoding='utf-8') as f:
                    if files:
                        # Determine columns
                        columns = ['file_name', 'file_path', 'category', 'file_size', 
                                   'label', 'tags', 'caption', 'created_date', 'modified_date']
                        
                        writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
                        writer.writeheader()
                        
                        for file_data in files:
                            # Convert tags list to string
                            row = dict(file_data)
                            if isinstance(row.get('tags'), list):
                                row['tags'] = ', '.join(row['tags'])
                            writer.writerow(row)
                            
            else:  # txt format
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(f"Exported File List - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write("=" * 80 + "\n\n")
                    
                    for file_data in files:
                        f.write(f"Name: {file_data.get('file_name', 'Unknown')}\n")
                        f.write(f"Path: {file_data.get('file_path', 'Unknown')}\n")
                        f.write(f"Category: {file_data.get('category', 'Unknown')}\n")
                        
                        tags = file_data.get('tags', [])
                        if isinstance(tags, list):
                            tags = ', '.join(tags)
                        if tags:
                            f.write(f"Tags: {tags}\n")
                        
                        if file_data.get('label'):
                            f.write(f"Label: {file_data['label']}\n")
                        if file_data.get('caption'):
                            f.write(f"Caption: {file_data['caption']}\n")
                            
                        f.write("-" * 40 + "\n\n")
            
            logger.info(f"Exported {len(files)} files to {output_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error exporting file list: {e}")
            return False


# Create global instance
def get_file_operations():
    """Get the file operations instance."""
    from app.core.database import file_index
    return FileOperations(file_index)
