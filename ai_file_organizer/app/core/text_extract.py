"""
Text extraction utilities for various file types.
Converts files to clean, readable text suitable for AI analysis.
"""

import csv
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
import io
import os

logger = logging.getLogger(__name__)

# Maximum limits to prevent memory issues
MAX_ROWS_TO_READ = 100
MAX_CHARS = 8000
MAX_CELL_LENGTH = 200
FONT_EXTENSIONS = {'.ttf', '.otf', '.ttc', '.woff', '.woff2'}


def extract_csv_text(file_path: Path, max_rows: int = MAX_ROWS_TO_READ) -> Optional[str]:
    """
    Extract readable text from a CSV file.
    
    Creates a summary including:
    - Column headers
    - Sample rows (formatted as readable text)
    - Basic statistics (row count, column count)
    
    Args:
        file_path: Path to the CSV file
        max_rows: Maximum number of sample rows to include
        
    Returns:
        Formatted text summary of the CSV, or None on error
    """
    try:
        # Try different encodings
        content = None
        for encoding in ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252']:
            try:
                with open(file_path, 'r', encoding=encoding, newline='') as f:
                    content = f.read(500000)  # Read up to 500KB
                break
            except UnicodeDecodeError:
                continue
        
        if content is None:
            logger.warning(f"Could not decode CSV file: {file_path}")
            return None
        
        # Parse CSV
        reader = csv.reader(io.StringIO(content))
        rows: List[List[str]] = []
        
        for i, row in enumerate(reader):
            if i >= max_rows + 1:  # +1 for header
                break
            # Truncate long cells
            row = [cell[:MAX_CELL_LENGTH] + '...' if len(cell) > MAX_CELL_LENGTH else cell for cell in row]
            rows.append(row)
        
        if not rows:
            return None
        
        # Extract headers and data
        headers = rows[0] if rows else []
        data_rows = rows[1:] if len(rows) > 1 else []
        
        # Count total rows (approximate for large files)
        total_rows = content.count('\n')
        
        # Build readable summary
        parts: List[str] = []
        
        # Header info
        parts.append(f"CSV File: {file_path.name}")
        parts.append(f"Columns ({len(headers)}): {', '.join(headers[:20])}")
        if len(headers) > 20:
            parts.append(f"  ... and {len(headers) - 20} more columns")
        parts.append(f"Approximate rows: {total_rows}")
        parts.append("")
        
        # Sample data as readable text
        parts.append("Sample Data:")
        for i, row in enumerate(data_rows[:15]):  # Show up to 15 sample rows
            if headers and len(row) == len(headers):
                # Format as "Header: Value" pairs
                pairs = [f"{h}: {v}" for h, v in zip(headers, row) if v.strip()]
                if pairs:
                    parts.append(f"  Row {i+1}: {'; '.join(pairs[:8])}")
                    if len(pairs) > 8:
                        parts.append(f"    ... ({len(pairs) - 8} more fields)")
            else:
                # Just list values
                parts.append(f"  Row {i+1}: {', '.join(row[:10])}")
        
        if len(data_rows) > 15:
            parts.append(f"  ... ({len(data_rows) - 15} more sample rows)")
        
        # Add column analysis
        if headers and data_rows:
            parts.append("")
            parts.append("Column Analysis:")
            for j, header in enumerate(headers[:10]):
                # Get unique values from this column
                col_values = [row[j] for row in data_rows if j < len(row) and row[j].strip()]
                unique_count = len(set(col_values))
                sample_values = list(set(col_values))[:5]
                if sample_values:
                    parts.append(f"  {header}: {unique_count} unique values. Examples: {', '.join(sample_values)}")
        
        result = '\n'.join(parts)
        
        # Ensure we don't exceed max chars
        if len(result) > MAX_CHARS:
            result = result[:MAX_CHARS] + "\n... (truncated)"
        
        logger.info(f"Extracted {len(result)} chars from CSV: {file_path.name}")
        return result
        
    except Exception as e:
        logger.error(f"Error extracting CSV text from {file_path}: {e}")
        return None


def extract_text_file_content(file_path: Path, max_chars: int = MAX_CHARS) -> Optional[str]:
    """
    Extract text content from plain text files (.txt, .md, .json, .xml, etc).
    
    Args:
        file_path: Path to the text file
        max_chars: Maximum characters to extract
        
    Returns:
        Text content or None on error
    """
    try:
        for encoding in ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252']:
            try:
                with open(file_path, 'r', encoding=encoding, errors='ignore') as f:
                    content = f.read(max_chars)
                return content
            except UnicodeDecodeError:
                continue
        return None
    except Exception as e:
        logger.error(f"Error reading text file {file_path}: {e}")
        return None


def extract_spreadsheet_text(file_path: Path) -> Optional[str]:
    """
    Extract text from Excel files (.xlsx, .xls).
    Requires openpyxl for .xlsx files.
    
    Args:
        file_path: Path to the spreadsheet
        
    Returns:
        Formatted text summary or None
    """
    ext = file_path.suffix.lower()
    
    if ext == '.xlsx':
        try:
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            
            parts: List[str] = []
            parts.append(f"Excel File: {file_path.name}")
            parts.append(f"Sheets: {', '.join(wb.sheetnames)}")
            parts.append("")
            
            for sheet_name in wb.sheetnames[:3]:  # First 3 sheets
                sheet = wb[sheet_name]
                parts.append(f"Sheet: {sheet_name}")
                
                rows_data: List[List[str]] = []
                for i, row in enumerate(sheet.iter_rows(max_row=20, values_only=True)):
                    if i >= 20:
                        break
                    row_text = [str(cell)[:100] if cell is not None else '' for cell in row[:15]]
                    rows_data.append(row_text)
                
                if rows_data:
                    headers = rows_data[0]
                    parts.append(f"  Columns: {', '.join([h for h in headers if h])}")
                    for j, row in enumerate(rows_data[1:10]):
                        if headers:
                            pairs = [f"{h}: {v}" for h, v in zip(headers, row) if v]
                            if pairs:
                                parts.append(f"    Row {j+1}: {'; '.join(pairs[:6])}")
                parts.append("")
            
            wb.close()
            
            result = '\n'.join(parts)
            if len(result) > MAX_CHARS:
                result = result[:MAX_CHARS] + "\n... (truncated)"
            return result
            
        except ImportError:
            logger.warning("openpyxl not installed - cannot read .xlsx files")
            return None
        except Exception as e:
            logger.error(f"Error reading Excel file {file_path}: {e}")
            return None

    if ext == '.xls':                       # the old binary Excel format (Excel 97–2003)
        try:
            import xlrd
            book = xlrd.open_workbook(str(file_path), on_demand=True)
            parts = [f"Excel File: {file_path.name}", f"Sheets: {', '.join(book.sheet_names())}", ""]
            for sheet_name in book.sheet_names()[:3]:
                sheet = book.sheet_by_name(sheet_name)
                parts.append(f"Sheet: {sheet_name}")
                rows = [[str(v)[:100] if v not in (None, "") else '' for v in sheet.row_values(r)[:15]]
                        for r in range(min(sheet.nrows, 20))]
                if rows:
                    headers = rows[0]
                    parts.append(f"  Columns: {', '.join([h for h in headers if h])}")
                    for j, row in enumerate(rows[1:10]):
                        pairs = [f"{h}: {v}" for h, v in zip(headers, row) if v]
                        if pairs:
                            parts.append(f"    Row {j+1}: {'; '.join(pairs[:6])}")
                parts.append("")
            book.release_resources()
            result = '\n'.join(parts)
            return result[:MAX_CHARS] + "\n... (truncated)" if len(result) > MAX_CHARS else result
        except ImportError:
            logger.warning("xlrd not installed - cannot read .xls files")
            return None
        except Exception as e:
            logger.error(f"Error reading Excel file {file_path}: {e}")
            return None

    return None


def extract_docx_text(file_path: Path, max_chars: int = 20000) -> Optional[str]:
    """Extract text from a Word .docx (paragraphs + table cells)."""
    try:
        import docx
        doc = docx.Document(str(file_path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        text = "\n".join(parts).strip()
        return text[:max_chars] if text else None
    except Exception as e:
        logger.warning(f"docx extraction failed for {file_path.name}: {e}")
        return None


def extract_pdf_text(file_path: Path, max_pages: int = 10, max_chars: int = 20000) -> Optional[str]:
    """Extract the text layer of a PDF (the first `max_pages` pages). pypdfium2 bundles its own PDF
    engine, so nothing external is needed (the old path needed poppler, which Windows doesn't have).
    A scanned PDF has no text layer -> None; the indexer then looks at it as an image instead."""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(file_path))
        try:
            parts = []
            for i in range(min(len(doc), max_pages)):
                page = doc[i]
                tp = page.get_textpage()
                t = (tp.get_text_range() or "").replace("\r\n", "\n").strip()
                tp.close()
                page.close()
                if t:
                    parts.append(t)
                if sum(len(p) for p in parts) >= max_chars:
                    break
        finally:
            doc.close()
    except Exception as e:
        logger.warning(f"pdf text extraction failed for {file_path.name}: {e}")
        return None
    text = "\n\n".join(parts).strip()
    return text[:max_chars] if text else None


ZIP_MAX_MEMBERS = 15              # documents read from one archive
ZIP_MAX_MEMBER_BYTES = 25 << 20   # a single document, uncompressed
ZIP_MAX_TOTAL_BYTES = 60 << 20    # everything read from one archive
ZIP_MAX_RATIO = 200               # uncompressed / compressed above this (and > 1 MB) = a zip bomb


def extract_zip_text(file_path: Path, max_chars: int = 20000) -> Optional[str]:
    """What's IN a .zip: the list of its files plus the text of the documents inside (any type
    extract_file_text reads, PDFs included). A compressed archive used to give only the names
    (its contents are unreadable raw bytes). Encrypted entries, nested archives, oversized and
    bomb-like entries are skipped; only up to ZIP_MAX_MEMBERS documents are opened."""
    import tempfile
    import zipfile
    try:
        zf = zipfile.ZipFile(file_path)
    except Exception as e:
        logger.warning(f"zip open failed for {file_path.name}: {e}")
        return None
    readable = set(get_supported_text_formats()) | {'.pdf'}
    parts, read, total = [], 0, 0
    with zf:
        files = [i for i in zf.infolist() if not i.is_dir()]
        names = [i.filename for i in files]
        parts.append(f"ZIP archive {file_path.name} with {len(files)} file(s): " + ", ".join(names[:60])
                     + (" …" if len(names) > 60 else ""))
        for info in files:
            if read >= ZIP_MAX_MEMBERS or total >= ZIP_MAX_TOTAL_BYTES:
                break
            ext = Path(info.filename).suffix.lower()
            if ext not in readable or ext == '.zip' or info.flag_bits & 0x1:   # 0x1 = encrypted
                continue
            if info.file_size > ZIP_MAX_MEMBER_BYTES or (
                    info.file_size > (1 << 20) and info.file_size > ZIP_MAX_RATIO * max(info.compress_size, 1)):
                continue
            fd, tmp = tempfile.mkstemp(suffix=ext)
            try:
                with os.fdopen(fd, "wb") as out, zf.open(info) as src:
                    out.write(src.read(ZIP_MAX_MEMBER_BYTES + 1))
                text = extract_file_text(Path(tmp))
            except Exception as e:
                logger.debug(f"zip member {info.filename} skipped: {e}")
                text = None
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
            read += 1
            total += info.file_size
            if text and text.strip():
                parts.append(f"\n--- {info.filename} ---\n{text.strip()[:4000]}")
    result = "\n".join(parts).strip()
    return result[:max_chars] if result else None


def extract_font_text(file_path: Path) -> Optional[str]:
    """A font file's real name and style ("Georgia Bold Italic") — read from the font itself.
    Its bytes are binary tables, so reading them as text used to give junk tags (glyf, cmap…)."""
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(str(file_path), 24)
        family, style = font.getname()
    except Exception as e:
        logger.warning(f"font read failed for {file_path.name}: {e}")
        return None
    kind = {'.ttf': 'TrueType', '.otf': 'OpenType', '.ttc': 'TrueType collection',
            '.woff': 'Web (WOFF)', '.woff2': 'Web (WOFF2)'}.get(file_path.suffix.lower(), '')
    return (f"Font file. Typeface: {family}. Style: {style}. Format: {kind} font. "
            f"Font family name: {family} {style}.").strip()


def pdf_has_real_text(text: Optional[str]) -> bool:
    """Enough real words to analyze as a document (a scanned PDF can carry a few stray glyphs)."""
    return bool(text) and sum(c.isalnum() for c in text) >= 25


def extract_pptx_text(file_path: Path, max_chars: int = 20000) -> Optional[str]:
    """Extract text from a PowerPoint .pptx (slide shapes + speaker notes)."""
    try:
        from pptx import Presentation
        prs = Presentation(str(file_path))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            bits = [sh.text_frame.text.strip() for sh in slide.shapes
                    if sh.has_text_frame and sh.text_frame.text.strip()]
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                note = slide.notes_slide.notes_text_frame.text.strip()
                if note:
                    bits.append(f"[notes] {note}")
            if bits:
                parts.append(f"Slide {i}: " + "\n".join(bits))
        text = "\n\n".join(parts).strip()
        return text[:max_chars] if text else None
    except Exception as e:
        logger.warning(f"pptx extraction failed for {file_path.name}: {e}")
        return None


def extract_rtf_text(file_path: Path, max_chars: int = 20000) -> Optional[str]:
    """Extract plain text from a .rtf file."""
    try:
        from striprtf.striprtf import rtf_to_text
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        text = rtf_to_text(raw).strip()
        return text[:max_chars] if text else None
    except Exception as e:
        logger.warning(f"rtf extraction failed for {file_path.name}: {e}")
        return None


def extract_eml_text(file_path: Path, max_chars: int = 20000) -> Optional[str]:
    """Extract subject + sender + body from an .eml email (stdlib; strips HTML if needed)."""
    try:
        import re as _re
        from email import policy
        from email.parser import BytesParser
        with open(file_path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
        header = [f"{h}: {msg.get(h)}" for h in ("From", "To", "Subject", "Date") if msg.get(h)]
        body = ""
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            if part.get_content_subtype() == "html":
                content = _re.sub(r"<[^>]+>", " ", content)
            body = _re.sub(r"[ \t]+", " ", content).strip()
        text = ("\n".join(header) + "\n\n" + body).strip()
        return text[:max_chars] if text else None
    except Exception as e:
        logger.warning(f"eml extraction failed for {file_path.name}: {e}")
        return None


def extract_epub_text(file_path: Path, max_chars: int = 20000) -> Optional[str]:
    """Extract text from an .epub ebook (HTML chapters -> plain text)."""
    try:
        import ebooklib
        from ebooklib import epub
        from bs4 import BeautifulSoup
        book = epub.read_epub(str(file_path))
        parts, total = [], 0
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            t = BeautifulSoup(item.get_content(), "html.parser").get_text(separator=" ", strip=True)
            if t:
                parts.append(t); total += len(t)
            if total > max_chars:
                break
        text = "\n".join(parts).strip()
        return text[:max_chars] if text else None
    except Exception as e:
        logger.warning(f"epub extraction failed for {file_path.name}: {e}")
        return None


def extract_odf_text(file_path: Path, max_chars: int = 20000) -> Optional[str]:
    """Extract text from an OpenDocument file (.odt/.ods/.odp) via odfpy."""
    try:
        from odf.opendocument import load
        from odf.text import P, H
        from odf import teletype
        doc = load(str(file_path))
        parts = []
        for tag in (H, P):
            for el in doc.getElementsByType(tag):
                t = teletype.extractText(el).strip()
                if t:
                    parts.append(t)
        text = "\n".join(parts).strip()
        return text[:max_chars] if text else None
    except Exception as e:
        logger.warning(f"odf extraction failed for {file_path.name}: {e}")
        return None


def extract_file_text(file_path: Path) -> Optional[str]:
    """
    Extract readable text from a file based on its type.
    
    Dispatcher function that chooses the appropriate extractor.
    
    Args:
        file_path: Path to the file
        
    Returns:
        Extracted text content or None
    """
    if not file_path.exists():
        return None
    
    ext = file_path.suffix.lower()
    
    # CSV files
    if ext == '.csv':
        return extract_csv_text(file_path)
    
    # Excel files
    if ext in {'.xlsx', '.xls'}:
        return extract_spreadsheet_text(file_path)

    # PDF: its text layer (None for a scanned PDF — never its raw bytes)
    if ext == '.pdf':
        return extract_pdf_text(file_path)

    # Archives: the documents inside. Fonts: their real name (never their binary tables).
    if ext == '.zip':
        return extract_zip_text(file_path)
    if ext in FONT_EXTENSIONS:
        return extract_font_text(file_path)

    # Office documents + email (reuse the same text -> analyze_text pipeline)
    if ext == '.docx':
        return extract_docx_text(file_path)
    if ext == '.pptx':
        return extract_pptx_text(file_path)
    if ext == '.rtf':
        return extract_rtf_text(file_path)
    if ext == '.eml':
        return extract_eml_text(file_path)
    if ext == '.epub':
        return extract_epub_text(file_path)
    if ext in {'.odt', '.ods', '.odp'}:
        return extract_odf_text(file_path)

    # Plain text files
    text_extensions = {
        '.txt', '.md', '.json', '.xml', '.html', '.htm',
        '.log', '.ini', '.cfg', '.yaml', '.yml', '.toml',
        '.py', '.js', '.ts', '.jsx', '.tsx', '.css', '.scss',
        '.java', '.cpp', '.c', '.h', '.hpp', '.cs', '.go',
        '.rb', '.php', '.swift', '.kt', '.rs', '.sql',
        '.sh', '.bat', '.ps1', '.env', '.gitignore'
    }
    if ext in text_extensions:
        return extract_text_file_content(file_path)
    
    # For other files, try to read as text
    try:
        return extract_text_file_content(file_path, max_chars=4000)
    except Exception:
        return None


def get_supported_text_formats() -> List[str]:
    """Return list of file extensions that can be text-extracted."""
    return [
        '.csv', '.xlsx', '.xls',
        '.docx', '.pptx', '.rtf', '.eml', '.epub', '.odt', '.ods', '.odp',
        '.txt', '.md', '.json', '.xml', '.html', '.htm',
        '.log', '.ini', '.cfg', '.yaml', '.yml', '.toml',
        '.py', '.js', '.ts', '.jsx', '.tsx', '.css', '.scss',
        '.java', '.cpp', '.c', '.h', '.hpp', '.cs', '.go',
        '.rb', '.php', '.swift', '.kt', '.rs', '.sql',
        '.sh', '.bat', '.ps1'
    ]
