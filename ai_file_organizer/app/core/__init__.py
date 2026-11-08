# Core business logic package

# iPhone photos (.heic/.heif): teach Pillow to open them, once, before any core module opens an
# image (analysis, EXIF dates, OCR) — without it they were never analyzed, only named.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass
