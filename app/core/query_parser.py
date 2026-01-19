"""
Natural Language Query Parser for Search Filters.
Extracts date ranges and file types from user queries.
Supports complex date expressions like "previous Thursday" or "January 3, 2023".
Includes fuzzy matching and spell checking for typo tolerance.
"""

import re
import logging
from typing import Dict, Optional, Tuple, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Try to import dateparser for advanced date parsing
try:
    import dateparser
    HAS_DATEPARSER = True
except ImportError:
    HAS_DATEPARSER = False

# Try to import rapidfuzz for fuzzy matching
try:
    from rapidfuzz import fuzz, process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False
    logger.warning("rapidfuzz not installed - fuzzy matching disabled. Run: pip install rapidfuzz")

# Try to import spellchecker
try:
    from spellchecker import SpellChecker
    HAS_SPELLCHECKER = True
    _spell_checker = SpellChecker()
    # Add custom words that might not be in dictionary
    _spell_checker.word_frequency.load_words([
        'thumbnail', 'thumbnails', 'screenshot', 'screenshots', 
        'pdf', 'pdfs', 'jpeg', 'png', 'webp', 'avif',
        'docx', 'xlsx', 'pptx', 'csv', 'json', 'yaml'
    ])
except ImportError:
    HAS_SPELLCHECKER = False
    _spell_checker = None
    logger.warning("pyspellchecker not installed - spell check disabled. Run: pip install pyspellchecker")


# Keywords for fuzzy matching (date-related)
DATE_KEYWORDS = [
    'today', 'yesterday', 'week', 'month', 'year',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december',
    'last', 'this', 'previous', 'next', 'past', 'ago', 'days'
]

# Keywords for fuzzy matching (type-related)
TYPE_KEYWORDS = [
    'image', 'images', 'photo', 'photos', 'picture', 'pictures',
    'screenshot', 'screenshots', 'thumbnail', 'thumbnails',
    'document', 'documents', 'pdf', 'pdfs', 'video', 'videos',
    'audio', 'music', 'code', 'spreadsheet', 'spreadsheets'
]

# All keywords for fuzzy matching
ALL_KEYWORDS = DATE_KEYWORDS + TYPE_KEYWORDS


def fuzzy_correct_word(word: str, threshold: int = 80) -> str:
    """
    Try to correct a misspelled word using fuzzy matching against known keywords.
    
    Args:
        word: The potentially misspelled word
        threshold: Minimum similarity score (0-100) to accept a match
        
    Returns:
        Corrected word if a good match found, otherwise original word
    """
    if not HAS_RAPIDFUZZ or len(word) < 3:
        return word
    
    word_lower = word.lower()
    
    # Skip if it's already a known keyword
    if word_lower in ALL_KEYWORDS:
        return word
    
    # Find best match among keywords
    result = process.extractOne(word_lower, ALL_KEYWORDS, scorer=fuzz.ratio)
    
    if result and result[1] >= threshold:
        matched_word, score, _ = result
        logger.debug(f"Fuzzy matched '{word}' -> '{matched_word}' (score: {score})")
        return matched_word
    
    return word


def spell_check_query(query: str) -> str:
    """
    Apply spell checking to correct common typos in the query.
    
    Args:
        query: The user's search query
        
    Returns:
        Query with spelling corrections applied
    """
    if not HAS_SPELLCHECKER or not _spell_checker:
        return query
    
    words = query.split()
    corrected_words = []
    
    for word in words:
        # Skip short words and words with special characters
        if len(word) < 3 or not word.isalpha():
            corrected_words.append(word)
            continue
        
        # Check if word is misspelled
        if word.lower() not in _spell_checker:
            correction = _spell_checker.correction(word.lower())
            if correction and correction != word.lower():
                logger.debug(f"Spell corrected '{word}' -> '{correction}'")
                corrected_words.append(correction)
            else:
                corrected_words.append(word)
        else:
            corrected_words.append(word)
    
    return ' '.join(corrected_words)


def apply_fuzzy_corrections(query: str) -> str:
    """
    Apply fuzzy matching to correct typos in date/type keywords.
    
    Args:
        query: The user's search query
        
    Returns:
        Query with fuzzy corrections applied
    """
    if not HAS_RAPIDFUZZ:
        return query
    
    words = query.split()
    corrected_words = []
    
    for word in words:
        corrected = fuzzy_correct_word(word)
        corrected_words.append(corrected)
    
    return ' '.join(corrected_words)

# Day name to weekday number mapping (Monday=0, Sunday=6)
DAY_NAME_TO_WEEKDAY = {
    'monday': 0,
    'tuesday': 1,
    'wednesday': 2,
    'thursday': 3,
    'friday': 4,
    'saturday': 5,
    'sunday': 6,
}


# Date patterns and their corresponding filter values (simple keywords)
DATE_PATTERNS = {
    # Today
    r'\btoday\b': 'today',
    r'\bthis day\b': 'today',
    
    # Yesterday
    r'\byesterday\b': 'yesterday',
    
    # This week / Last week
    r'\bthis week\b': 'this_week',
    r'\blast week\b': 'last_week',
    r'\bpast week\b': 'last_week',
    r'\bpast 7 days\b': 'last_week',
    r'\blast 7 days\b': 'last_week',
    
    # This month / Last month
    r'\bthis month\b': 'this_month',
    r'\blast month\b': 'last_month',
    r'\bpast month\b': 'last_month',
    r'\bpast 30 days\b': 'last_month',
    r'\blast 30 days\b': 'last_month',
    
    # This year
    r'\bthis year\b': 'this_year',
    r'\blast year\b': 'last_year',
}

# Patterns for complex date expressions that dateparser should handle
# These help identify when to try dateparser
COMPLEX_DATE_PATTERNS = [
    # Day names with modifiers
    r'\b(previous|last|this|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
    # Specific dates with various formats
    r'\b\d{1,2}(st|nd|rd|th)?\s+(of\s+)?(january|february|march|april|may|june|july|august|september|october|november|december)\s*\d{0,4}\b',
    r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(st|nd|rd|th)?\s*,?\s*\d{0,4}\b',
    # Numeric dates
    r'\b\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}\b',
    r'\b\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2}\b',
    # Relative expressions
    r'\b\d+\s+(days?|weeks?|months?|years?)\s+ago\b',
]

# File type patterns and their corresponding filter values
TYPE_PATTERNS = {
    # Images
    r'\bimages?\b': 'images',
    r'\bphotos?\b': 'images',
    r'\bpictures?\b': 'images',
    r'\bscreenshots?\b': 'images',
    r'\bthumbnails?\b': 'images',
    r'\bjpe?gs?\b': 'images',
    r'\bpngs?\b': 'images',
    r'\bgifs?\b': 'images',
    r'\bwebps?\b': 'images',
    
    # Documents
    r'\bdocuments?\b': 'documents',
    r'\bdocs?\b': 'documents',
    r'\bword\b': 'documents',
    r'\bdocx?\b': 'documents',
    r'\btexts?\b': 'documents',
    r'\btxt\b': 'documents',
    
    # PDFs
    r'\bpdfs?\b': 'pdfs',
    r'\bpdf files?\b': 'pdfs',
    
    # Videos
    r'\bvideos?\b': 'videos',
    r'\bmovies?\b': 'videos',
    r'\bmp4s?\b': 'videos',
    r'\bmkvs?\b': 'videos',
    r'\bavis?\b': 'videos',
    
    # Audio
    r'\baudios?\b': 'audio',
    r'\bmusic\b': 'audio',
    r'\bsongs?\b': 'audio',
    r'\bmp3s?\b': 'audio',
    r'\bwavs?\b': 'audio',
    
    # Code
    r'\bcode\b': 'code',
    r'\bscripts?\b': 'code',
    r'\bpython\b': 'code',
    r'\bjavascript\b': 'code',
    r'\bhtml\b': 'code',
    r'\bcss\b': 'code',
}

# File extensions for each type
TYPE_EXTENSIONS = {
    'images': ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.ico', '.svg', '.heic', '.heif', '.avif', '.raw', '.cr2', '.nef', '.arw'],
    'documents': ['.doc', '.docx', '.txt', '.rtf', '.odt', '.md', '.tex'],
    'pdfs': ['.pdf'],
    'videos': ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v'],
    'audio': ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a'],
    'code': ['.py', '.js', '.ts', '.html', '.css', '.java', '.cpp', '.c', '.h', '.cs', '.go', '.rs', '.rb', '.php', '.swift', '.kt'],
}


def get_date_range(filter_value: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Convert a date filter value to a datetime range.
    
    Returns:
        Tuple of (start_date, end_date) or (None, None) if no filter
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    if filter_value == 'today':
        return today_start, now
    
    elif filter_value == 'yesterday':
        yesterday_start = today_start - timedelta(days=1)
        return yesterday_start, today_start
    
    elif filter_value == 'this_week':
        # Start of this week (Monday)
        week_start = today_start - timedelta(days=today_start.weekday())
        return week_start, now
    
    elif filter_value == 'last_week':
        return today_start - timedelta(days=7), now
    
    elif filter_value == 'this_month':
        month_start = today_start.replace(day=1)
        return month_start, now
    
    elif filter_value == 'last_month':
        return today_start - timedelta(days=30), now
    
    elif filter_value == 'this_year':
        year_start = today_start.replace(month=1, day=1)
        return year_start, now
    
    elif filter_value == 'last_year':
        return today_start - timedelta(days=365), now
    
    return None, None


def get_date_range_for_specific_date(parsed_date: datetime) -> Tuple[datetime, datetime]:
    """
    Get the date range for a specific date (midnight to midnight).
    
    Args:
        parsed_date: The parsed datetime
        
    Returns:
        Tuple of (start_of_day, end_of_day)
    """
    start_of_day = parsed_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)
    return start_of_day, end_of_day


def calculate_day_date(modifier: str, day_name: str) -> Optional[datetime]:
    """
    Calculate the date for expressions like "last thursday", "previous monday", etc.
    
    Args:
        modifier: "last", "previous", "this", or "next"
        day_name: The day name (monday, tuesday, etc.)
        
    Returns:
        The calculated datetime or None if invalid
    """
    day_name = day_name.lower()
    modifier = modifier.lower()
    
    if day_name not in DAY_NAME_TO_WEEKDAY:
        return None
    
    target_weekday = DAY_NAME_TO_WEEKDAY[day_name]
    today = datetime.now()
    current_weekday = today.weekday()
    
    if modifier in ('last', 'previous'):
        # Calculate the most recent occurrence of that day (before today)
        days_ago = (current_weekday - target_weekday) % 7
        if days_ago == 0:
            days_ago = 7  # If today is that day, go back a full week
        result = today - timedelta(days=days_ago)
        
    elif modifier == 'this':
        # This week's occurrence of that day
        days_diff = target_weekday - current_weekday
        result = today + timedelta(days=days_diff)
        
    elif modifier == 'next':
        # Next week's occurrence of that day
        days_diff = target_weekday - current_weekday
        if days_diff <= 0:
            days_diff += 7
        result = today + timedelta(days=days_diff)
        
    else:
        return None
    
    return result.replace(hour=0, minute=0, second=0, microsecond=0)


def try_parse_complex_date(query: str) -> Tuple[Optional[str], Optional[Tuple[datetime, datetime]], Optional[str]]:
    """
    Try to parse complex date expressions using multiple methods.
    
    Args:
        query: The search query
        
    Returns:
        Tuple of (filter_label, date_range, matched_text) or (None, None, None) if not found
    """
    query_lower = query.lower()
    logger.debug(f"[DATE_PARSER] Trying to parse: '{query_lower}'")
    
    # Method 1: Try manual calculation for day names (most reliable)
    # Match "last/previous/this/next + day name"
    day_pattern = r'\b(last|previous|this|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b'
    day_match = re.search(day_pattern, query_lower)
    if day_match:
        modifier = day_match.group(1)
        day_name = day_match.group(2)
        matched_text = day_match.group(0)
        logger.info(f"[DATE_PARSER] Found day pattern: modifier='{modifier}', day='{day_name}'")
        
        calculated_date = calculate_day_date(modifier, day_name)
        if calculated_date:
            filter_label = f"specific_date:{calculated_date.strftime('%Y-%m-%d')}"
            date_range = get_date_range_for_specific_date(calculated_date)
            logger.info(f"[DATE_PARSER] Calculated date: {calculated_date.strftime('%Y-%m-%d')}")
            return filter_label, date_range, matched_text
    
    # Method 2: Try "X days/weeks/months ago" pattern (manual calculation)
    ago_pattern = r'\b(\d+)\s+(days?|weeks?|months?|years?)\s+ago\b'
    ago_match = re.search(ago_pattern, query_lower)
    if ago_match:
        amount = int(ago_match.group(1))
        unit = ago_match.group(2).rstrip('s')  # Remove plural 's'
        matched_text = ago_match.group(0)
        logger.info(f"[DATE_PARSER] Found ago pattern: {amount} {unit} ago")
        
        today = datetime.now()
        if unit == 'day':
            calculated_date = today - timedelta(days=amount)
        elif unit == 'week':
            calculated_date = today - timedelta(weeks=amount)
        elif unit == 'month':
            calculated_date = today - timedelta(days=amount * 30)  # Approximate
        elif unit == 'year':
            calculated_date = today - timedelta(days=amount * 365)  # Approximate
        else:
            calculated_date = None
        
        if calculated_date:
            calculated_date = calculated_date.replace(hour=0, minute=0, second=0, microsecond=0)
            filter_label = f"specific_date:{calculated_date.strftime('%Y-%m-%d')}"
            date_range = get_date_range_for_specific_date(calculated_date)
            logger.info(f"[DATE_PARSER] Calculated date: {calculated_date.strftime('%Y-%m-%d')}")
            return filter_label, date_range, matched_text
    
    # Method 3: Try dateparser for other complex expressions (month names, specific dates)
    if HAS_DATEPARSER:
        # List of date-related phrases to try extracting
        date_phrases_to_try = []
        
        # Try regex patterns
        for pattern in COMPLEX_DATE_PATTERNS:
            match = re.search(pattern, query_lower, re.IGNORECASE)
            if match:
                date_phrases_to_try.append(match.group(0))
        
        # Month name patterns
        month_names = ['january', 'february', 'march', 'april', 'may', 'june', 
                       'july', 'august', 'september', 'october', 'november', 'december',
                       'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
        for month in month_names:
            if month in query_lower:
                # Try to extract the date portion
                date_match = re.search(rf'\d{{1,2}}\s*(?:st|nd|rd|th)?\s*(?:of\s+)?{month}\s*\d{{0,4}}', query_lower)
                if date_match:
                    date_phrases_to_try.append(date_match.group(0))
                date_match2 = re.search(rf'{month}\s+\d{{1,2}}(?:st|nd|rd|th)?\s*,?\s*\d{{0,4}}', query_lower)
                if date_match2:
                    date_phrases_to_try.append(date_match2.group(0))
                break
        
        logger.debug(f"[DATE_PARSER] Phrases to try with dateparser: {date_phrases_to_try}")
        
        # Try each phrase with dateparser
        for phrase in date_phrases_to_try:
            try:
                parsed = dateparser.parse(
                    phrase,
                    settings={
                        'PREFER_DATES_FROM': 'past',
                        'RELATIVE_BASE': datetime.now(),
                    }
                )
                
                if parsed:
                    filter_label = f"specific_date:{parsed.strftime('%Y-%m-%d')}"
                    date_range = get_date_range_for_specific_date(parsed)
                    logger.info(f"[DATE_PARSER] dateparser parsed '{phrase}' -> {parsed.strftime('%Y-%m-%d')}")
                    return filter_label, date_range, phrase
                else:
                    logger.debug(f"[DATE_PARSER] dateparser returned None for '{phrase}'")
                    
            except Exception as e:
                logger.debug(f"[DATE_PARSER] dateparser error for '{phrase}': {e}")
                continue
    
    logger.debug(f"[DATE_PARSER] No date pattern found in query")
    return None, None, None


def parse_query(query: str) -> Dict:
    """
    Parse a natural language query and extract filters.
    
    Args:
        query: The user's search query
        
    Returns:
        Dict with:
            - clean_query: Query with filter terms removed
            - date_filter: Detected date filter (or None)
            - type_filter: Detected type filter (or None)
            - date_range: Tuple of (start_date, end_date) if date_filter detected
            - extensions: List of file extensions if type_filter detected
            - specific_date: The specific date if a complex date was parsed (for display)
            - corrections_applied: List of corrections made (for UI feedback)
    """
    # Import settings to check if features are enabled
    from .settings import settings
    
    result = {
        'clean_query': query,
        'date_filter': None,
        'type_filter': None,
        'date_range': (None, None),
        'extensions': None,
        'specific_date': None,
        'corrections_applied': [],
    }
    
    # Apply corrections if enabled
    processed_query = query
    
    # Single toggle: Spell Check (controls both fuzzy + spell correction)
    if settings.enable_spell_check:
        # Step 1: Fuzzy match common keywords (dates/types)
        if HAS_RAPIDFUZZ:
            corrected = apply_fuzzy_corrections(processed_query)
            if corrected != processed_query:
                result['corrections_applied'].append(f"Fuzzy: '{processed_query}' → '{corrected}'")
                processed_query = corrected
        # Step 2: Spell check general words (dictionary-based)
        if HAS_SPELLCHECKER:
            corrected = spell_check_query(processed_query)
            if corrected != processed_query:
                result['corrections_applied'].append(f"Spell: '{processed_query}' → '{corrected}'")
                processed_query = corrected
    
    clean_query = processed_query.lower()
    date_matched_text = None
    
    # First, try simple date patterns (today, yesterday, etc.)
    for pattern, filter_value in DATE_PATTERNS.items():
        if re.search(pattern, clean_query, re.IGNORECASE):
            result['date_filter'] = filter_value
            result['date_range'] = get_date_range(filter_value)
            # Remove the matched pattern from query
            clean_query = re.sub(pattern, '', clean_query, flags=re.IGNORECASE)
            date_matched_text = filter_value
            break  # Only use first match
    
    # If no simple pattern matched, try complex date parsing with dateparser
    if result['date_filter'] is None:
        filter_label, date_range, matched_text = try_parse_complex_date(processed_query)
        if filter_label and date_range:
            result['date_filter'] = filter_label
            result['date_range'] = date_range
            date_matched_text = matched_text
            # Extract the specific date for display
            if filter_label.startswith('specific_date:'):
                result['specific_date'] = filter_label.split(':')[1]
            # Remove the matched text from query
            if matched_text:
                clean_query = re.sub(re.escape(matched_text), '', clean_query, flags=re.IGNORECASE)
    
    # Detect type patterns
    for pattern, filter_value in TYPE_PATTERNS.items():
        if re.search(pattern, clean_query, re.IGNORECASE):
            result['type_filter'] = filter_value
            result['extensions'] = TYPE_EXTENSIONS.get(filter_value, [])
            # Only remove the pattern if there are OTHER words in the query
            # This prevents "thumbnail" from being stripped when it's the only search term
            temp_query = re.sub(pattern, '', clean_query, flags=re.IGNORECASE).strip()
            if temp_query:  # Only strip if something remains
                clean_query = temp_query
            # If nothing remains, keep the original as search term (don't strip)
            break  # Only use first match
    
    # Clean up the query (remove extra spaces, common words)
    clean_query = re.sub(r'\b(i|the|a|an|my|from|created|made|that|which|were|was|in|on)\b', '', clean_query, flags=re.IGNORECASE)
    clean_query = re.sub(r'\s+', ' ', clean_query).strip()
    
    # Keep empty string if we extracted filters - this enables date-only searches
    # Only fall back to original query if NO filters were detected and query became empty
    if clean_query or result['date_filter'] or result['type_filter']:
        result['clean_query'] = clean_query  # Can be empty string for date-only searches
    else:
        result['clean_query'] = processed_query  # No filters detected, keep processed query
    
    # Log corrections if any were made
    if result['corrections_applied']:
        logger.info(f"Query corrections applied: {result['corrections_applied']}")
    
    return result


def get_filter_display_name(filter_type: str, filter_value: str) -> str:
    """Get a human-readable name for a filter value."""
    if filter_type == 'date':
        names = {
            'today': 'Today',
            'yesterday': 'Yesterday',
            'this_week': 'This Week',
            'last_week': 'Last 7 Days',
            'this_month': 'This Month',
            'last_month': 'Last 30 Days',
            'this_year': 'This Year',
            'last_year': 'Last Year',
        }
        return names.get(filter_value, 'Any Time')
    
    elif filter_type == 'type':
        names = {
            'images': 'Images',
            'documents': 'Documents',
            'pdfs': 'PDFs',
            'videos': 'Videos',
            'audio': 'Audio',
            'code': 'Code',
        }
        return names.get(filter_value, 'All Types')
    
    return filter_value


# UI dropdown values to internal filter values mapping
UI_DATE_MAPPING = {
    'Any Time': None,
    'Today': 'today',
    'Yesterday': 'yesterday',
    'This Week': 'this_week',
    'This Month': 'this_month',
    'This Year': 'this_year',
}

UI_TYPE_MAPPING = {
    'All Types': None,
    'Images': 'images',
    'Documents': 'documents',
    'PDFs': 'pdfs',
    'Videos': 'videos',
    'Audio': 'audio',
    'Code': 'code',
}

# Reverse mappings for updating UI from detected filters
FILTER_TO_UI_DATE = {v: k for k, v in UI_DATE_MAPPING.items() if v is not None}
FILTER_TO_UI_TYPE = {v: k for k, v in UI_TYPE_MAPPING.items() if v is not None}

