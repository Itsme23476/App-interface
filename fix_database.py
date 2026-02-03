"""
Script to fix corrupted database by deleting it and creating a fresh one.
Run this when you see "database disk image is malformed" errors.
"""

import os
import shutil
from pathlib import Path

def fix_database():
    # Database location
    app_data = os.environ.get('APPDATA', '')
    db_path = Path(app_data) / 'ai-file-organizer' / 'file_index.db'
    
    print(f"Database location: {db_path}")
    
    if db_path.exists():
        # Create backup first
        backup_path = db_path.with_suffix('.db.corrupted')
        try:
            shutil.copy(db_path, backup_path)
            print(f"Backup created: {backup_path}")
        except Exception as e:
            print(f"Could not create backup: {e}")
        
        # Delete the corrupted database
        try:
            os.remove(db_path)
            print("✓ Corrupted database deleted successfully!")
            print("\nPlease restart the app and re-index your folders.")
        except Exception as e:
            print(f"✗ Error deleting database: {e}")
            print(f"\nTry manually deleting: {db_path}")
    else:
        print("Database file not found. Nothing to fix.")

if __name__ == "__main__":
    fix_database()
