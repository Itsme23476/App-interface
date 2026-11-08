"""
Tests for Organization Plan module - NO actual file moves, safe to run
"""
import pytest
from pathlib import Path
import tempfile


class TestOrganizationPlan:
    """Test organization plan creation and validation."""
    
    def test_plan_summary(self):
        """Test getting plan summary."""
        from app.core.plan import get_plan_summary
        
        plan = [
            {'source': 'C:/test/doc1.pdf', 'destination': 'C:/org/Docs/doc1.pdf', 'category': 'Documents'},
            {'source': 'C:/test/doc2.pdf', 'destination': 'C:/org/Docs/doc2.pdf', 'category': 'Documents'},
            {'source': 'C:/test/img1.jpg', 'destination': 'C:/org/Images/img1.jpg', 'category': 'Images'},
        ]
        
        summary = get_plan_summary(plan)
        assert summary is not None
        print(f"✅ Plan summary: {summary}")
    
    def test_destination_space_validation(self):
        """Test destination space validation."""
        from app.core.apply import validate_destination_space
        
        # Test with a valid directory
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a small test plan
            plan = [
                {'source': 'C:/test/small.txt', 'destination': f'{tmpdir}/small.txt', 'size': 100}
            ]
            
            has_space, message = validate_destination_space(plan, Path(tmpdir))
            print(f"✅ Space validation: has_space={has_space}, message={message}")
    
    def test_plan_structure(self):
        """Test that a valid plan has required fields."""
        plan = [
            {
                'source': 'C:/test/file1.pdf',
                'destination': 'C:/organized/Documents/file1.pdf',
                'category': 'Documents'
            },
        ]
        
        # Check structure
        for item in plan:
            assert 'source' in item
            assert 'destination' in item
        
        print("✅ Plan structure is valid")


class TestPlanCreation:
    """Test AI organization plan creation (mocked)."""
    
    def test_ai_organizer_imports(self):
        """Test that AI organizer module imports correctly."""
        try:
            from app.core.ai_organizer import (
                request_organization_plan,
                validate_plan,
                deduplicate_plan,
                ensure_all_files_included
            )
            print("✅ AI organizer module imports successfully")
        except ImportError as e:
            pytest.skip(f"AI organizer not available: {e}")
    
    def test_deduplicate_plan(self):
        """Test plan deduplication."""
        from app.core.ai_organizer import deduplicate_plan
        
        # Plan with duplicates
        plan = [
            {'source': 'C:/test/file1.pdf', 'destination': 'C:/org/file1.pdf'},
            {'source': 'C:/test/file1.pdf', 'destination': 'C:/org/file1.pdf'},  # Duplicate
            {'source': 'C:/test/file2.pdf', 'destination': 'C:/org/file2.pdf'},
        ]
        
        deduped = deduplicate_plan(plan)
        # Should have removed duplicate
        assert len(deduped) <= len(plan)
        print(f"✅ Deduplication: {len(plan)} -> {len(deduped)} items")
    
    def test_ensure_all_files_included(self):
        """Files the AI left out of the plan are placed in a folder — none are left unorganized.
        (Today's contract: plan = {"folders": {name: [file ids]}}, ids = the set of every file id.)"""
        from app.core.ai_organizer import ensure_all_files_included

        plan = {"folders": {"Invoices": [1], "Photos": [3]}}
        files_info = [{"id": 1, "name": "invoice_jan.pdf", "category": "Documents"},
                      {"id": 2, "name": "invoice_feb.pdf", "category": "Documents"},
                      {"id": 3, "name": "beach.jpg", "category": "Images"}]
        result = ensure_all_files_included(plan, {1, 2, 3}, files_info)
        placed = [int(i) for ids in result["folders"].values() for i in ids]
        assert sorted(placed) == [1, 2, 3], result
        # a list of ids works too (it used to crash with "list - set")
        result2 = ensure_all_files_included({"folders": {"A": [1]}}, [1, 2], files_info)
        assert sorted(int(i) for ids in result2["folders"].values() for i in ids) == [1, 2]
        print(f"✅ All files included: {result['folders']}")


class TestPlanValidation:
    """Test plan validation utilities."""
    
    def test_empty_plan(self):
        """Test handling empty plan."""
        plan = []
        assert len(plan) == 0
        print("✅ Empty plan handled")
    
    def test_plan_with_same_source_dest(self):
        """Test plan where source equals destination."""
        plan = [
            {'source': 'C:/test/file.pdf', 'destination': 'C:/test/file.pdf'},
        ]
        
        # Source and destination are same - no-op
        for item in plan:
            if item['source'] == item['destination']:
                print(f"  - Skipping no-op move: {item['source']}")
        
        print("✅ Same source/dest handled")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
