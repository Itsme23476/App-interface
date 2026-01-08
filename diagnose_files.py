"""Diagnose what files are indexed and why metadata extraction isn't working."""
import sqlite3
import os
from pathlib import Path
from collections import Counter

db_path = os.path.join(os.environ['APPDATA'], 'ai-file-organizer', 'file_index.db')
print(f"Database: {db_path}\n")

conn = sqlite3.connect(db_path)
c = conn.cursor()

# File type distribution
print("=== File Types in Your Index ===")
c.execute("SELECT file_extension, COUNT(*) as cnt FROM files GROUP BY file_extension ORDER BY cnt DESC")
for row in c.fetchall():
    print(f"  {row[0] or 'no extension'}: {row[1]} files")

# Check for metadata dates
print("\n=== Metadata Date Status ===")
c.execute("SELECT COUNT(*) FROM files WHERE original_date IS NOT NULL")
with_dates = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM files WHERE original_date IS NULL")
without_dates = c.fetchone()[0]
print(f"  With original_date: {with_dates}")
print(f"  Without original_date: {without_dates}")

# Sample filenames to check for date patterns
print("\n=== Sample Filenames (to check for date patterns) ===")
c.execute("SELECT file_name FROM files LIMIT 15")
for row in c.fetchall():
    print(f"  {row[0]}")

# Check modified dates distribution
print("\n=== Modified Date Distribution ===")
c.execute("""
    SELECT substr(modified_date, 1, 10) as date, COUNT(*) 
    FROM files 
    GROUP BY date 
    ORDER BY date DESC 
    LIMIT 10
""")
for row in c.fetchall():
    print(f"  {row[0]}: {row[1]} files")

conn.close()

print("\n=== Analysis ===")
print("If all files have similar modified dates, it means:")
print("1. Files were all modified around the same time")
print("2. OR files were synced/copied together (OneDrive)")
print("\nThe modified_date is the best available date for these files.")

