"""Pasting through the clipboard, safely. Filect types text into other apps by putting it on the clipboard and pressing
Ctrl+V a moment later (the dictation 90 ms, Quick Search 50-200 ms). Two things went wrong in that moment:

  * something else wrote to the clipboard in between (a clipboard manager, a phone's clipboard syncing over, a copy in
    another app), and THAT was pasted instead - anything private (Mac c143812, found live on the Mac 2026-11-12;
    reproduced on Windows 2026-11-13 with another program writing as Filect pasted).
  * afterwards the dictation put back only the TEXT of what you had copied: a copied picture or file became empty
    text, and a copy made in the 0.4 s after the paste was overwritten with the older one.

put_back_if_changed() goes right before every Ctrl+V; snapshot() / restore_if_still() keep your clipboard."""
import logging

from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)


def snapshot() -> QMimeData:
    """A copy of what's on the clipboard now - its text, HTML, a picture and copied files (the forms other apps read),
    to put back after a paste. Only these, not every format: asking an app for all of its formats (Office offers
    dozens) can take seconds."""
    out = QMimeData()
    try:
        src = QApplication.clipboard().mimeData()
        if src is None:
            return out
        if src.hasImage():
            out.setImageData(src.imageData())
        if src.hasUrls():
            out.setUrls(src.urls())
        if src.hasHtml():
            out.setHtml(src.html())
        if src.hasText():
            out.setText(src.text())
    except Exception as e:
        logger.warning(f"[clipboard] couldn't copy what was on it: {e}")
    return out


def put_back_if_changed(text: str):
    """Right before Ctrl+V: if the clipboard no longer holds `text` (something else wrote to it since Filect put it
    there), put `text` back so that's what gets pasted. Returns a snapshot of what the other program wrote (the newest
    copy - that's what should be on the clipboard afterwards), or None if nothing had changed."""
    try:
        cb = QApplication.clipboard()
        if cb.text() == text:
            return None
        newer = snapshot()
        logger.info("[clipboard] it changed between setting it and the paste - putting Filect's text back")
        cb.setText(text)
        return newer
    except Exception as e:
        logger.warning(f"[clipboard] couldn't check it before the paste: {e}")
        return None


def restore_if_still(text: str, saved: QMimeData) -> bool:
    """After the paste: put back what was on the clipboard before - but only if it still holds Filect's text. If you
    (or another app) copied something in the meantime, that stays."""
    try:
        cb = QApplication.clipboard()
        if cb.text() != text:
            logger.info("[clipboard] something new was copied after the paste - keeping it")
            return False
        cb.setMimeData(saved)
        return True
    except Exception as e:
        logger.warning(f"[clipboard] couldn't put it back: {e}")
        return False
