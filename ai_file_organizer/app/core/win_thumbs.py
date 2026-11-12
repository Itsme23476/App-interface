"""Windows: the small picture of a file, as Explorer shows it (IShellItemImageFactory), saved as a PNG for the search
panel's rows. A PDF Windows has no picture for gets its first page drawn instead. Nothing here reads a OneDrive file
that's only in the cloud (the caller checks that first) and nothing is ever written next to the file."""
import ctypes
import logging
import os
import threading
from ctypes import POINTER, byref, c_void_p, wintypes

logger = logging.getLogger(__name__)

_SIIGBF_BIGGERSIZEOK = 0x01
_SIIGBF_THUMBNAILONLY = 0x08     # a real picture of the file, or nothing (never the generic file-type icon)
_BI_RGB = 0
_DIB_RGB_COLORS = 0


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


_iface = None
_iface_lock = threading.Lock()


def _factory_iface():
    """The COM interface, defined once (comtypes)."""
    global _iface
    with _iface_lock:
        if _iface is None:
            import comtypes
            from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

            class IShellItemImageFactory(IUnknown):
                _iid_ = GUID("{bcc18b79-ba16-442f-80c4-8a59c30c463b}")
                _methods_ = [COMMETHOD([], HRESULT, "GetImage", (["in"], _SIZE, "size"), (["in"], ctypes.c_int, "flags"),
                                       (["out"], POINTER(wintypes.HBITMAP), "phbm"))]
            _iface = (comtypes, IShellItemImageFactory)
    return _iface


def _hbitmap_png(hbm, out_path: str) -> bool:
    gdi32 = ctypes.windll.gdi32
    bm = _BITMAP()
    if not gdi32.GetObjectW(wintypes.HANDLE(hbm), ctypes.sizeof(bm), byref(bm)):
        return False
    w, h = bm.bmWidth, bm.bmHeight
    if w <= 0 or h <= 0:
        return False
    bih = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32,
                            biCompression=_BI_RGB)
    buf = ctypes.create_string_buffer(w * h * 4)
    hdc = ctypes.windll.user32.GetDC(None)
    try:
        got = gdi32.GetDIBits(hdc, wintypes.HANDLE(hbm), 0, h, buf, byref(bih), _DIB_RGB_COLORS)
    finally:
        ctypes.windll.user32.ReleaseDC(None, hdc)
    if got != h:
        return False
    from PIL import Image
    im = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
    if im.getextrema()[3][1] == 0:       # (no alpha at all: an opaque picture)
        im = im.convert("RGB")
    im.save(out_path, "PNG")
    return True


def _shell_picture(path: str, out_path: str, px: int) -> bool:
    comtypes, IFactory = _factory_iface()
    comtypes.CoInitialize()
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, c_void_p, POINTER(comtypes.GUID),
                                                        POINTER(c_void_p)]
        item = c_void_p()
        iid = IFactory._iid_
        if shell32.SHCreateItemFromParsingName(os.path.normpath(path), None, byref(iid), byref(item)) != 0 or \
                not item.value:
            return False
        factory = ctypes.cast(item, POINTER(IFactory))
        try:
            hbm = factory.GetImage(_SIZE(px, px), _SIIGBF_THUMBNAILONLY | _SIIGBF_BIGGERSIZEOK)
        except Exception:
            return False                 # (no picture for this kind of file)
        try:
            return _hbitmap_png(hbm, out_path)
        finally:
            ctypes.windll.gdi32.DeleteObject(wintypes.HANDLE(hbm))
    finally:
        try:
            del factory
        except Exception:
            pass
        comtypes.CoUninitialize()


def _pdf_page(path: str, out_path: str, px: int) -> bool:
    """A PDF's first page (Windows often has no PDF thumbnails)."""
    try:
        from app.core.organize_reader import _pdf_page as first_page
        data = first_page(path)
    except Exception:
        return False
    if not data:
        return False
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(data))
    im.thumbnail((px * 2, px * 2))
    im.save(out_path, "PNG")
    return True


def save_thumbnail(path: str, out_path: str, px: int = 96) -> bool:
    """Write a small picture of `path` to `out_path` (PNG). False if there isn't one."""
    try:
        if _shell_picture(path, out_path, px):
            return True
    except Exception as e:
        logger.debug("[thumbs] shell picture failed for %s: %s", path, e)
    if path.lower().endswith(".pdf"):
        try:
            return _pdf_page(path, out_path, px)
        except Exception as e:
            logger.debug("[thumbs] PDF page failed for %s: %s", path, e)
    return False
