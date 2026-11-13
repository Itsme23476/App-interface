"""
Tests for Search Service module - Uses temp database, safe to run
"""
import pytest
import tempfile
from pathlib import Path


# The index's current field names (add_file reads source_path / name / extension / size) — these tests were
# written with the first API (file_path / file_name …) and never updated, so every row went in with an empty
# path. Same checks, today's API (ported from the Mac fix, 2026-11-07).
_OLD_TO_NEW = {"file_path": "source_path", "file_name": "name", "file_extension": "extension", "file_size": "size"}


def _add(db, data):
    return db.add_file({_OLD_TO_NEW.get(k, k): v for k, v in data.items()})


def _all(db):
    """Every indexed file (the old get_all_files)."""
    import sqlite3
    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM files")]


class TestSearchService:
    """Test search functionality."""
    
    @pytest.fixture
    def search_db_with_data(self, tmp_path):
        """Create database with test data."""
        from app.core.database import FileIndex
        
        # Create temp database
        db_path = tmp_path / "test_search.db"
        db = FileIndex(db_path)
        
        # Add test files using file_data parameter
        test_files = [
            {
                'file_path': 'C:/photos/vacation_beach.jpg',
                'file_name': 'vacation_beach.jpg',
                'file_extension': '.jpg',
                'file_size': 5000,
                'category': 'Images',
                'has_ocr': False,
                'label': 'Beach vacation photo with family',
                'caption': 'Beautiful sunset at the beach',
                'tags': ['vacation', 'beach', 'family'],
            },
            {
                'file_path': 'C:/documents/tax_return_2023.pdf',
                'file_name': 'tax_return_2023.pdf',
                'file_extension': '.pdf',
                'file_size': 2000,
                'category': 'Documents',
                'has_ocr': True,
                'ocr_text': 'Internal Revenue Service Form 1040 Tax Return',
                'label': 'Tax return document',
                'caption': '2023 annual tax filing',
            },
            {
                'file_path': 'C:/work/project_proposal.docx',
                'file_name': 'project_proposal.docx',
                'file_extension': '.docx',
                'file_size': 3000,
                'category': 'Documents',
                'has_ocr': False,
                'label': 'Project proposal for Q4',
                'caption': 'Business proposal document',
            },
            {
                'file_path': 'C:/music/favorite_song.mp3',
                'file_name': 'favorite_song.mp3',
                'file_extension': '.mp3',
                'file_size': 8000,
                'category': 'Audio',
                'has_ocr': False,
                'label': 'Rock music track',
            },
        ]
        
        for f in test_files:
            _add(db, f)
        
        return db
    
    def test_search_by_filename(self, search_db_with_data):
        """Test searching by filename."""
        db = search_db_with_data
        
        results = db.search_files('vacation')
        assert len(results) >= 1
        assert any('vacation' in r['file_name'].lower() for r in results)
        print(f"✅ Search by filename: found {len(results)} results")
    
    def test_search_by_label(self, search_db_with_data):
        """Test searching by label/description."""
        db = search_db_with_data
        
        results = db.search_files('beach')
        assert len(results) >= 1
        print(f"✅ Search by label: found {len(results)} results")
    
    def test_search_by_category(self, search_db_with_data):
        """Test filtering by category."""
        db = search_db_with_data
        
        # Get all documents
        all_files = _all(db)
        documents = [f for f in all_files if f.get('category') == 'Documents']
        assert len(documents) >= 2
        print(f"✅ Category filter: found {len(documents)} documents")
    
    def test_search_no_results(self, search_db_with_data):
        """Test search with no matching results."""
        db = search_db_with_data
        
        results = db.search_files('xyznonexistent123')
        assert len(results) == 0
        print("✅ No results returns empty list")
    
    def test_search_ocr_content(self, search_db_with_data):
        """Test searching OCR text content."""
        db = search_db_with_data
        
        # Search for text that's only in OCR
        results = db.search_files('1040')  # From tax form OCR
        # May or may not find depending on search implementation
        print(f"✅ OCR content search: found {len(results)} results")
    
    def test_search_case_insensitive(self, search_db_with_data):
        """Test that search is case insensitive."""
        db = search_db_with_data
        
        results_lower = db.search_files('vacation')
        results_upper = db.search_files('VACATION')
        results_mixed = db.search_files('VaCaTiOn')
        
        # All should return same results
        assert len(results_lower) == len(results_upper) == len(results_mixed)
        print("✅ Search is case insensitive")
    
    def test_empty_search(self, search_db_with_data):
        """Test empty search query."""
        db = search_db_with_data
        
        results = db.search_files('')
        # Empty search might return all files or empty
        print(f"✅ Empty search: returned {len(results)} results")
    
    def test_get_all_files(self, search_db_with_data):
        """Test getting all indexed files."""
        db = search_db_with_data
        
        all_files = _all(db)
        assert len(all_files) == 4  # We added 4 test files
        print(f"✅ Get all files: {len(all_files)} files")


class TestSearchPauseCancel:
    """Test search pause/cancel functionality."""
    
    def test_cancel_flag(self):
        """Test cancel flag functionality."""
        from app.core.search import SearchService
        
        service = SearchService()
        
        # Initially not cancelled
        assert not service._cancel_flag.is_set()
        
        # Cancel
        service.cancel_indexing()
        assert service._cancel_flag.is_set()
        
        print("✅ Cancel flag works correctly")
    
    def test_pause_resume(self):
        """Test pause/resume functionality."""
        from app.core.search import SearchService
        
        service = SearchService()
        
        # Initially not paused
        assert not service.is_paused()
        
        # Pause
        service.pause_indexing()
        assert service.is_paused()
        
        # Resume
        service.resume_indexing()
        assert not service.is_paused()
        
        print("✅ Pause/resume works correctly")
    
    def test_service_singleton(self):
        """Test that search_service is available."""
        from app.core.search import search_service
        
        assert search_service is not None
        assert hasattr(search_service, 'search_files')
        assert hasattr(search_service, 'index_directory')
        
        print("✅ Search service singleton available")


class TestSearchWords:
    """Ported from the Mac (pack §6.1, item 6): any word with a hyphen ("x-rays", "self-raising", "part-time") was an
    FTS5 syntax error, so search fell back to an A-Z list of loose matches; "birds" didn't find photos tagged "bird"."""

    @pytest.fixture
    def db(self, tmp_path):
        from app.core.database import FileIndex
        d = FileIndex(tmp_path / "words.db")
        for name, text, tags in (("invoice.pdf", "Cleaning and two X-rays. Total due 180", []),
                                 ("recipe.txt", "200 g self-raising flour, pour lemon syrup", []),
                                 ("notes.md", "Hire one part-time designer in July", []),
                                 ("owl.jpg", "", ["bird", "owl"]),
                                 ("aaa_first_by_name.txt", "two and two cleaning flour designer", [])):
            _add(d, {"file_path": f"C:/t/{name}", "file_name": name, "file_extension": Path(name).suffix,
                     "file_size": 10, "category": "Documents", "ocr_text": text, "has_ocr": bool(text), "tags": tags})
        return d

    @pytest.mark.parametrize("terms, want", [
        (["cleaning", "two", "x-rays"], "invoice.pdf"),
        (["lemon", "syrup", "self-raising"], "recipe.txt"),
        (["part-time", "designer"], "notes.md"),
        (["birds"], "owl.jpg"),
    ])
    def test_words_with_hyphens_and_plurals_find_the_file_first(self, db, terms, want):
        got = [r["file_name"] for r in db.search_files_advanced(terms, {}, 10)]
        assert got and got[0] == want, got

    def test_quotes_and_punctuation_never_break_the_query(self, db):
        for terms in (['say "hi"'], ["-"], ["c++"], ["what?"], ["2025-03"]):
            db.search_files_advanced(terms, {}, 10)          # (no exception, no crash)


def test_a_weekday_in_a_sentence_falls_back_to_any_time():
    """"please sign the lease by friday": the weekday made it a date search that found nothing. If the date finds
    nothing but there are words, search everything typed at any time and say so (the owner's choice, Mac)."""
    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
    i = src.index("    def search_files(self):")
    body = src[i:src.index("\n    def ", i + 10)]
    assert "if not results and search_query and parsed.get('date_filter') and (date_start or date_end):" in body
    assert "date_start=None, date_end=None" in body and "from any time" in body


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
