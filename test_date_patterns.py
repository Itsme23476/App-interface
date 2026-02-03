"""
Test script for date pattern parsing.
Run with: python test_date_patterns.py
"""

import sys
import os

# Ensure we can import from the app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Disable spell check for tests (we don't want corrections interfering)
os.environ['AI_FILE_ORG_NO_SPELLCHECK'] = '1'

from app.core.query_parser import parse_query

# Test cases: (query, expected_filter_prefix, description)
TEST_CASES = [
    # Days
    ("screenshot today", "today", "Today"),
    ("files yesterday", "yesterday", "Yesterday"),
    ("photos monday", "specific_date:", "Standalone day name"),
    ("docs last thursday", "specific_date:", "Last + day"),
    ("notes this friday", "specific_date:", "This + day"),
    ("files 3 days ago", "specific_date:", "N days ago"),
    
    # Weeks
    ("docs this week", "this_week", "This week"),
    ("files last week", "last_week", "Last week"),
    ("photos previous week", "last_week", "Previous week"),
    ("notes 2 weeks ago", "specific_date:", "N weeks ago"),
    
    # Months  
    ("docs this month", "this_month", "This month"),
    ("files last month", "last_month", "Last month"),
    ("photos previous month", "last_month", "Previous month"),
    ("screenshot december", "month:", "Standalone month"),
    ("files last december", "month:", "Last + month"),
    ("docs december 2024", "month:", "Month + year"),
    ("notes 3 months ago", "specific_date:", "N months ago"),
    
    # Years
    ("files this year", "this_year", "This year"),
    ("docs last year", "last_year", "Last year"),
    ("photos previous year", "last_year", "Previous year"),
    ("screenshot the previous year", "last_year", "The previous year"),
    ("files 2025", "year:", "Year alone"),
    ("docs 2024", "year:", "Year alone (past)"),
    
    # Relative ranges
    ("recent files", "last_week", "Recent"),
    ("files past 14 days", "range:", "Past N days"),
    ("docs last 30 days", "range:", "Last N days"),
    ("photos within 7 days", "range:", "Within N days"),
    
    # CRITICAL: Specific dates must return single day, NOT month range
    ("screenshot 27th december", "specific_date:", "Day + month (27th december)"),
    ("files december 27", "specific_date:", "Month + day (december 27)"),
    ("docs 27 december 2025", "specific_date:", "Full date (27 december 2025)"),
    ("notes 15th of january", "specific_date:", "Day of month (15th of january)"),
    ("screenshot 1st december", "specific_date:", "1st + month"),
]

def run_tests():
    print("=" * 70)
    print("DATE PATTERN PARSING TESTS")
    print("=" * 70)
    
    passed = 0
    failed = 0
    failed_tests = []
    
    for query, expected_filter_prefix, description in TEST_CASES:
        result = parse_query(query)
        date_filter = result.get('date_filter')
        date_range = result.get('date_range')
        
        # Check if filter matches expected prefix
        is_pass = False
        if date_filter and date_filter.startswith(expected_filter_prefix):
            is_pass = True
        elif date_filter == expected_filter_prefix:
            is_pass = True
            
        if is_pass:
            status = "PASS"
            passed += 1
        else:
            status = "FAIL"
            failed += 1
            failed_tests.append((query, expected_filter_prefix, date_filter, description))
        
        # Print result
        range_str = ""
        if date_range and date_range[0] and date_range[1]:
            range_str = f" | {date_range[0].strftime('%Y-%m-%d')} to {date_range[1].strftime('%Y-%m-%d')}"
        print(f"[{status}] {description}: '{query}' -> {date_filter}{range_str}")
    
    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed, {len(TEST_CASES)} total")
    
    if failed_tests:
        print("\nFAILED TESTS:")
        for query, expected, got, desc in failed_tests:
            print(f"  - '{query}' expected '{expected}' but got '{got}'")
    
    print("=" * 70)
    
    return failed == 0

if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
