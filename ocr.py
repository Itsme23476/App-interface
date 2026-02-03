"""
Lightweight OCR using Tesseract (pytesseract) and pdf2image.
"""

import logging
import os
from pathlib import Path
from typing import Optional, List

from PIL import Image
import pytesseract
from pdf2image import convert_from_path

logger = logging.getLogger(__name__)


def _ensure_tesseract_path_on_windows() -> None:
    """Best-effort: add common Tesseract install path to PATH on Windows."""
    if os.name != 'nt':
        return
    candidates = [
        r"C:\\Program Files\\Tesseract-OCR",
        r"C:\\Program Files (x86)\\Tesseract-OCR",
    ]
    for base in candidates:
        exe = Path(base) / "tesseract.exe"
        if exe.exists():
            # Ensure Tesseract can find language data. It lives under the 'tessdata' subfolder.
            tessdata_dir = Path(base) / "tessdata"
            if tessdata_dir.exists():
                # Prefer explicitly pointing to tessdata to avoid resolution issues
                os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)
            # Ensure tesseract.exe is on PATH for the current process
            os.environ["PATH"] = f"{base};" + os.environ.get("PATH", "")
            break


def extract_text_from_image(image_path: Path, lang: str = "eng") -> Optional[str]:
    """Extract text from an image using Tesseract.

    Returns None if nothing meaningful is found or on error.
    """
    try:
        _ensure_tesseract_path_on_windows()
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            text = pytesseract.image_to_string(img, lang=lang, config="--psm 3")
        text = (text or "").strip()
        return text if text else None
    except Exception as e:
        logger.error(f"Error extracting text from image {image_path}: {e}")
        return None


def extract_text_from_pdf(pdf_path: Path, lang: str = "eng", max_pages: int = 3) -> Optional[str]:
    """OCR the first few pages of a PDF rendered to images.

    max_pages keeps runtime reasonable. Increase if needed.
    """
    try:
        _ensure_tesseract_path_on_windows()
        pages = convert_from_path(str(pdf_path))
        extracted: List[str] = []
        for i, page_img in enumerate(pages[:max_pages]):
            text = pytesseract.image_to_string(page_img.convert("RGB"), lang=lang, config="--psm 3")
            text = (text or "").strip()
            if text:
                extracted.append(f"Page {i+1}: {text}")
        combined = "\n\n".join(extracted).strip()
        return combined if combined else None
    except Exception as e:
        logger.error(f"Error extracting text from PDF {pdf_path}: {e}")
        return None


def extract_text_from_file(file_path: Path) -> Optional[str]:
    """Dispatch OCR by file type (images, PDFs)."""
    if not file_path.exists():
        logger.warning(f"File does not exist: {file_path}")
        return None

    extension = file_path.suffix.lower()
    image_extensions = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".gif", ".webp", ".avif", ".heic", ".heif", ".ico", ".raw", ".cr2", ".nef", ".arw"}

    if extension == ".pdf":
        return extract_text_from_pdf(file_path)
    if extension in image_extensions:
        return extract_text_from_image(file_path)

    logger.debug(f"OCR not supported for file type: {extension}")
    return None


def get_supported_formats() -> List[str]:
    return [".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".gif", ".webp", ".avif", ".heic", ".heif", ".ico", ".raw", ".cr2", ".nef", ".arw"]
