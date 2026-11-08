"""Filect MCP server — EXPERIMENTAL (Windows port).

Exposes Filect's capabilities to an MCP client (Claude Desktop/Code) WITHOUT touching the
app's code. It imports `app.core.*` and calls the same functions the app uses; it never
edits app code and is not in the PyInstaller build.

Read tools (search, file details, analyze) only read the local index and make the same paid
AI calls the app makes. Write tools organize files: apply_organization_plan MOVES files and
undo_last_organize reverses the most recent move. Nothing here DELETES a file. Cloud AI calls
need the saved Supabase session (sign into Filect once).

Ported from macOS filect_mcp (Mac 088d904 → 2661a78 + #6 e541bcb), mcp 2.x API. Windows
adaptations: subscription gate uses check_subscription(); the macOS TCC "bridge" is dropped
(Windows has no Full Disk Access gate, so file ops are direct); #6 index-update-after-organize
is inlined into apply/undo; custom-words add is implemented inline (no app.core.vocab yet, so
suggest_custom_words is omitted).

Run:  set QT_QPA_PLATFORM=offscreen && <venv>\\Scripts\\python -m filect_mcp
(registered in Claude via claude config — see README.md)
"""
import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger("filect_mcp")


def _setup_logging():
    """Log to <app data>/logs/filect_mcp.log (stdout is the MCP stdio channel, so never there).
    Attached to the root logger so app.core.* warnings/errors land in the same file."""
    from logging.handlers import RotatingFileHandler
    base = Path(os.environ.get("APPDATA") or Path.home()) / "ai-file-organizer" / "logs"
    try:
        base.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(base / "filect_mcp.log", maxBytes=2_000_000, backupCount=2,
                                encoding="utf-8")
    except Exception:
        return
    h.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)


_setup_logging()


class _LogCapture(logging.Handler):
    """Collect WARNING+ records emitted while a tool runs, so a bare None from app code can be
    returned to the client with the real reason (proxy 401/403, parse error, ...)."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        try:
            self.messages.append(f"{record.name}: {record.getMessage()}")
        except Exception:
            pass

# Make Filect's own package importable (app lives in ../ai_file_organizer).
_APP_DIR = Path(__file__).resolve().parent.parent / "ai_file_organizer"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

# Headless: we import only app.core.* (never app.ui.*), but some core modules pull in Qt
# transitively — offscreen keeps that from needing a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    "filect",
    instructions=(
        "Filect: local semantic file search + AI content analysis + file organizing. Use "
        "analyze_file to understand the contents of ANY file — images, video (frames + spoken "
        "transcript), audio, PDFs, and documents (Word/PowerPoint/RTF/email/ebook/OpenDocument). "
        "search_files/get_file_details read the index; save_to_index stores your analysis so "
        "search can find the file. add_custom_words saves dictation words. The organize tools "
        "MOVE files: generate_organization_plan previews, apply_organization_plan moves them, "
        "undo_last_organize reverses the most recent move; enable_auto_organize turns on background "
        "auto-sorting for a folder. Tools that change app settings (add_custom_words, "
        "enable_auto_organize) must be confirmed with the user first. Nothing deletes files. "
        "A file that's ALREADY analyzed is not analyzed again or overwritten: analyze_file returns "
        "its saved analysis, and save_to_index won't replace it. Pass reanalyze=true ONLY when the "
        "user explicitly asks to re-analyze a file (a file that changed since is analyzed anew)."
    ),
    version="0.1.0",
)

_auth_ok = False


def _persist_live_tokens():
    """Write the live (possibly just-refreshed) session tokens back to settings.json.

    restore_session() calls gotrue set_session(), which auto-refreshes a near-expiry access token
    and ROTATES the single-use refresh token on the server — so the refresh token still sitting on
    disk is now dead. If we don't persist the fresh pair, the Filect app's next launch reuses the
    dead token and fails with 'Invalid Refresh Token: Already Used'. Writing the fresh tokens keeps
    disk current. _save_config is merge-only-changed + atomic + locked (settings.py #7), so this
    writes just the two auth keys without clobbering anything the app changed. Best-effort."""
    try:
        from app.core.settings import settings
        from app.core.supabase_client import supabase_auth
        toks = supabase_auth.get_session_tokens() or {}
        at, rt = toks.get("access_token"), toks.get("refresh_token")
        changed = False
        if at and at != settings.auth_access_token:
            settings.auth_access_token = at
            changed = True
        if rt and rt != settings.auth_refresh_token:
            settings.auth_refresh_token = rt
            changed = True
        if changed:
            settings._save_config()
            logger.info("filect_mcp: persisted refreshed auth tokens to settings")
    except Exception as e:
        logger.warning("filect_mcp: could not persist refreshed tokens: %s", e)


def _ensure_auth() -> bool:
    """Restore the user's saved Supabase session (same tokens the app stores). Needed for the
    cloud AI calls; local search does not need it."""
    global _auth_ok
    if _auth_ok:
        return True
    try:
        from app.core.settings import settings
        from app.core.supabase_client import supabase_auth
        # Re-read settings from disk: this long-running MCP process cached settings at startup, but
        # the Filect app may have written fresh tokens since (e.g. you just signed in). Without this
        # the MCP keeps using the stale cached token and fails even though disk is current.
        try:
            settings._load_config()
        except Exception:
            pass
        settings.ai_provider = "openai"   # the MCP always analyzes via the cloud backend
        if supabase_auth.is_authenticated:
            _auth_ok = True
        elif settings.has_stored_session():
            r = supabase_auth.restore_session(settings.auth_access_token,
                                              settings.auth_refresh_token)
            _auth_ok = bool(r.get("success"))
            if _auth_ok:
                _persist_live_tokens()   # keep disk tokens valid after a refresh rotated them
    except Exception:
        _auth_ok = False
    return _auth_ok


def _open_filect_app() -> bool:
    """Bring up the Filect app so the user can sign in. Uses the app's own single-instance deep
    link `filect://open`: if the app is already running it forwards to it and that instance brings
    its window to the front; otherwise a fresh instance starts. The MCP is headless and must NOT
    own a login flow — the app does. Best-effort; never raises."""
    try:
        import subprocess
        flags = (0x00000008 | 0x08000000) if sys.platform == "win32" else 0  # DETACHED | NO_WINDOW
        if getattr(sys, "frozen", False):
            env = {k: v for k, v in os.environ.items() if k != "FILECT_MODE"}  # GUI mode, not MCP
            subprocess.Popen([sys.executable, "filect://open"], env=env,
                             creationflags=flags, close_fds=True)
        else:
            main_py = _APP_DIR / "main.py"
            if not main_py.exists():
                return False
            pyexe = Path(sys.executable)
            pyw = pyexe.with_name("pythonw.exe")   # no console flash for the GUI
            launcher = str(pyw) if pyw.exists() else str(pyexe)
            subprocess.Popen([launcher, str(main_py), "filect://open"], cwd=str(_APP_DIR),
                             creationflags=flags, close_fds=True)
        logger.info("filect_mcp: opened Filect app for sign-in")
        return True
    except Exception as e:
        logger.warning("filect_mcp: could not open Filect app: %s", e)
        return False


_access_ok_until = 0.0        # monotonic timestamp; a grant is cached until this time
_ACCESS_TTL = 600.0           # re-verify the subscription at most every 10 minutes
_auth_prompt_at = 0.0         # last time we opened the app for login
_AUTH_PROMPT_COOLDOWN = 120.0  # don't relaunch the app more than once every 2 minutes


def _require_access():
    """Gate EVERY tool. Three outcomes:
      - None            → access granted (signed in + active/trialing subscription).
      - needs_login     → NOT signed in: open the Filect app so the user can log in, and return an
                          error telling them to sign in and retry (the MCP never shows its own login).
      - subscription    → signed in but no active subscription (e.g. past_due): a manage-plan
                          message, NOT a login relaunch (they're already signed in).
    A grant is cached for _ACCESS_TTL so a long-running server isn't re-checking every call, but a
    lapsed subscription / dead session is re-checked once the TTL passes (never cached forever)."""
    global _access_ok_until, _auth_prompt_at
    import time
    if time.monotonic() < _access_ok_until:
        return None
    _ensure_auth()  # restore the saved session first (loads tokens, sets provider)
    try:
        from app.core.supabase_client import supabase_auth
        authed = bool(supabase_auth.is_authenticated)
    except Exception as e:
        authed = False
    if not authed:
        # No valid session → open Filect for login (rate-limited so repeated tool calls don't
        # spawn it over and over). The app focuses its existing window or starts fresh.
        _access_ok_until = 0.0
        opened = False
        if time.monotonic() - _auth_prompt_at > _AUTH_PROMPT_COOLDOWN:
            opened = _open_filect_app()
            _auth_prompt_at = time.monotonic()
        return {"error": ("You're not signed into Filect. I've opened the Filect app — sign in "
                          "there, then ask me again." if opened else
                          "You're not signed into Filect. Open the Filect app, sign in, then try again."),
                "needs_login": True}
    try:
        from app.core.supabase_client import supabase_auth
        sub = supabase_auth.check_subscription() or {}
    except Exception as e:
        return {"error": f"Could not verify your Filect subscription ({e}). Open Filect and try again."}
    if sub.get("has_subscription"):
        _access_ok_until = time.monotonic() + _ACCESS_TTL
        return None
    _access_ok_until = 0.0     # denied → force a fresh check next call
    reason = sub.get("trial_blocked_reason") or sub.get("status") or sub.get("error") or "no active subscription"
    return {"error": "You're signed in, but an active Filect subscription is required to use this. "
                     "Your plan must be active or trialing (a failed/past-due payment is locked out) "
                     f"(current status: {reason}). Open Filect to manage your subscription.",
            "needs_subscription": True}


def _coerce(v):
    """Make a value JSON-serializable: pass natives through, stringify Path/datetime/Decimal/etc."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_coerce(x) for x in v]
    if isinstance(v, dict):
        return {k: _coerce(x) for k, x in v.items()}
    return str(v)


def _jsonsafe(d):
    """Keep a result row small + serializable: drop vector/binary fields, coerce the rest."""
    if not isinstance(d, dict):
        return _coerce(d)
    out = {}
    for k, v in d.items():
        if k in ("embedding", "vector", "thumbnail", "thumb", "blob"):
            continue
        if isinstance(v, (bytes, bytearray)):
            continue
        out[k] = _coerce(v)
    return out


# ---- "already analyzed" rule ----------------------------------------------------------------------
# A file Filect (or an AI client) has already analyzed is NOT analyzed again or overwritten unless the
# user explicitly asks for a re-analysis (reanalyze=true). A file that CHANGED since its analysis is a new
# file: it's analyzed and saved as usual.
_REANALYZE_HINT = ("It's already analyzed, so it wasn't analyzed again. Only if the user explicitly asks "
                   "for a re-analysis: call this again with reanalyze=true (then save_to_index with "
                   "reanalyze=true to replace the saved analysis).")


def _file_sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _saved_analysis(p: Path):
    """The analysis already saved for this file, or None: none saved yet, an empty one, or the file
    changed since (its saved fingerprint no longer matches)."""
    from app.core.database import file_index
    row = file_index.get_file_by_path(str(p))
    if not row or not (row.get("tags") or row.get("caption") or row.get("ocr_text")):
        return None
    if row.get("content_hash"):
        try:
            if _file_sha256(p) != row["content_hash"]:
                return None
        except Exception:
            pass
    return row


def _saved_summary(p: Path, row: dict) -> dict:
    import sqlite3
    from app.core.database import file_index
    source = None
    try:
        with sqlite3.connect(file_index.db_path) as con:
            r = con.execute("SELECT ai_source FROM files WHERE file_path = ?", (str(p),)).fetchone()
            source = r[0] if r else None
    except Exception:
        pass
    return _jsonsafe({
        "file": p.name,
        "mode": "already_analyzed",
        "type": row.get("label"),
        "tags": row.get("tags") or [],
        "caption": row.get("caption"),
        "transcript_or_text": (row.get("ocr_text") or "")[:4000] or None,
        "analyzed_by": "an AI client via MCP" if str(source or "").startswith("mcp:") else "Filect",
        "analyzed_on": row.get("indexed_date"),
        "note": _REANALYZE_HINT,
    })


@server.tool(description="Search your indexed files with a natural-language query (e.g. "
                         "'tax PDFs from last year', 'screenshots of invoices'). Returns "
                         "matching files with their path, name, type and relevance. Read-only.")
def search_files(query: str, limit: int = 20) -> list:
    try:
        gate = _require_access()
        if gate:
            return [gate]
        from app.core.search import search_service
        rows = search_service.search_files(query, limit=max(1, min(limit, 100))) or []
        return [_jsonsafe(r) for r in rows]
    except Exception as e:
        return [{"error": f"search failed: {e}"}]


@server.tool(description="Get the stored index details for one file by absolute path "
                         "(tags, caption, category, dates). Read-only.")
def get_file_details(path: str) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        from app.core.search import search_service
        d = search_service.get_file_details(path)
        return _jsonsafe(d) if d else {"error": "not found in index"}
    except Exception as e:
        return {"error": f"lookup failed: {e}"}


@server.tool(description="Analyze an IMAGE file (png/jpg/etc.) with the vision model and "
                         "return a label, tags and caption. Makes a paid AI call. Does NOT "
                         "work on video. Read-only. An image that's ALREADY analyzed returns its "
                         "saved analysis instead — pass reanalyze=true ONLY if the user explicitly "
                         "asks to re-analyze it.")
def analyze_image(path: str, reanalyze: bool = False) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        from app.core.vision import analyze_image as _ai
        p = Path(path)
        if not p.exists():
            return {"error": "file not found"}
        saved = None if reanalyze else _saved_analysis(p)
        if saved:
            return _saved_summary(p, saved)
        cap = _LogCapture()
        logging.getLogger().addHandler(cap)
        try:
            res = _ai(p)
        finally:
            logging.getLogger().removeHandler(cap)
        if res:
            return _jsonsafe(res)
        logger.warning("analyze_image returned no result for %s: %s", p, cap.messages)
        return {"error": "no result from Filect's vision backend",
                "details": cap.messages[-5:] or ["no warning was logged — the backend reply "
                                                 "was likely empty or not valid JSON"]}
    except Exception as e:
        logger.exception("analyze_image failed for %s", path)
        return {"error": f"analyze failed: {e}"}


@server.tool(description="Transcribe an AUDIO file to text via Filect's speech backend. "
                         "Makes a paid AI call. Read-only. A file that's ALREADY transcribed returns "
                         "its saved transcript instead — pass reanalyze=true ONLY if the user "
                         "explicitly asks to transcribe it again.")
def transcribe_file(path: str, reanalyze: bool = False) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        from app.core.transcription import transcribe_audio
        p = Path(path)
        saved = None if reanalyze or not p.exists() else _saved_analysis(p)
        if saved and saved.get("ocr_text"):
            return {"ok": True, "text": saved["ocr_text"], "mode": "already_analyzed",
                    "note": _REANALYZE_HINT}
        return transcribe_audio(path)
    except Exception as e:
        return {"error": f"transcribe failed: {e}"}


@server.tool(description="Add dictation custom words to the user's Filect app (persists to "
                         "settings — case-insensitive dedup, capped at 100). Use whenever the user "
                         "asks to add dictation vocabulary. Returns how many were actually added.")
def add_custom_words(words: list) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        from app.core.settings import settings
        before = settings.live_dictation_custom_terms()      # from disk: the app may have added some
        had = {t.lower() for t in before}
        requested = []
        for w in (words or []):
            w = str(w).strip()
            if w and w.lower() not in {r.lower() for r in requested}:
                requested.append(w)
        settings.add_custom_terms(requested)                  # same rules as the app: dedup, ≤50 chars, cap 100
        now = settings.dictation_custom_terms
        added = [t for t in now if t.lower() not in had]
        already = [w for w in requested if w.lower() in had]
        skipped = [w for w in requested if w not in added and w not in already]
        if added:
            note = f"Added {len(added)} word(s): {', '.join(added)}."
        else:
            note = "Nothing new was added."
        if already:
            note += f" Already in the list: {', '.join(already)}."
        if skipped:
            note += f" Not added (over 50 characters, or the 100-word list is full): {', '.join(skipped)}."
        return {"added": len(added), "added_words": added, "already_present": already,
                "not_added": skipped, "total_now": len(now), "note": note}
    except Exception as e:
        return {"error": f"add failed: {e}"}


@server.tool(description="Analyze ANY file and return what YOU (the client model) need to "
                         "understand and tag it, WITHOUT Filect's paid backend: documents & text "
                         "PDFs come back as extracted TEXT; images & video keyframes come back as "
                         "IMAGES for your own vision; audio comes back as a speech transcript (via "
                         "Filect's speech backend). You then produce the label/tags/caption "
                         "yourself. Unreadable/scanned files fall back to Filect's own analyzer. "
                         "This only READS — call save_to_index afterward with your tags to make the "
                         "file searchable; it never moves or deletes the file. A file that's ALREADY "
                         "analyzed (and unchanged since) returns its saved analysis instead and isn't "
                         "analyzed again — pass reanalyze=true ONLY if the user explicitly asks to "
                         "re-analyze it.")
def analyze_file(path: str, reanalyze: bool = False):
    try:
        gate = _require_access()
        if gate:
            return gate
        from app.core.search import search_service
        p = Path(path)
        if not p.exists():
            return {"error": "file not found"}
        saved = None if reanalyze else _saved_analysis(p)
        if saved:
            return _saved_summary(p, saved)
        # Readability probe — fail with a clear message instead of a silent empty analysis.
        try:
            with open(p, "rb") as _f:
                _f.read(16)
        except Exception as e:
            return {"error": f"Can't read this file ({e}). Check that the path exists and isn't "
                             "locked by another program, then try again."}
        # Path B — "use the client's AI, not Filect's backend": for a TEXT document, hand the
        # locally-extracted text straight back so the MCP client does the understanding on ITS OWN
        # usage. No paid backend call, nothing persisted. Non-text files fall through to Path A.
        from app.core.text_extract import extract_file_text, get_supported_text_formats
        if p.suffix.lower() in set(get_supported_text_formats()) or p.suffix.lower() == ".pdf":
            doc_text = (extract_file_text(p) or "").strip()
            if doc_text:
                return {
                    "file": p.name,
                    "mode": "raw_text_for_client",
                    "note": ("Text extracted locally — NO AI backend was used. You (the client "
                             "model) read this text and produce the label/tags/caption yourself."),
                    "text": doc_text[:200000],
                }

        suffix = p.suffix.lower()

        # IMAGE (or a scanned PDF's first page) → hand a downscaled image to the CLIENT's vision.
        # NO backend call.
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".pdf"}:
            try:
                import io
                from PIL import Image as _PILImage
                from mcp.server.mcpserver import Image as _McpImage
                if suffix == ".pdf":
                    from app.core.vision import render_pdf_page
                    im = render_pdf_page(p, 0, 1024)
                    if im is None:
                        raise ValueError("couldn't render the PDF")
                else:
                    im = _PILImage.open(p)
                    im.load()
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGB")
                im.thumbnail((1024, 1024))
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                note = (f"image_for_client — downscaled '{p.name}' below; NO AI backend was used. "
                        "You (the client model) look at it and produce the label/tags/caption.")
                return [note, _McpImage(data=buf.getvalue(), format="png")]
            except Exception:
                pass

        # VIDEO → hand the keyframes (local ffmpeg extraction, NO backend) to the client.
        try:
            from app.core.video_analysis import VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, extract_keyframes
        except Exception:
            VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, extract_keyframes = set(), set(), None
        if extract_keyframes and suffix in VIDEO_EXTENSIONS:
            frames = extract_keyframes(p, n=4) or []
            if frames:
                from mcp.server.mcpserver import Image as _McpImage
                out = [(f"video_frames_for_client — {len(frames)} keyframes of '{p.name}' below; "
                        "NO AI backend was used. You (the client model) look at them for the "
                        "label/tags/caption. For the spoken audio, call transcribe_file on this path.")]
                out += [_McpImage(data=f, format="png") for f in frames]
                return out

        # AUDIO → the ONE backend call: speech-to-text, returned for the client to tag.
        if suffix in AUDIO_EXTENSIONS:
            from app.core.transcription import transcribe_audio
            tr = transcribe_audio(str(p)) or {}
            if tr.get("ok") and (tr.get("text") or "").strip():
                return {"file": p.name, "mode": "transcript_for_client",
                        "transcript": tr["text"], "duration_seconds": tr.get("duration"),
                        "note": "Transcript via Filect's speech backend. You (the client model) "
                                "produce the label/tags/caption from it."}

        # Path A — same per-file router the indexer uses, but we DON'T persist.
        meta = search_service._process_single_file(
            {"source_path": str(p), "name": p.name}, p.parent, force_ai=True)
        if not meta or meta.get("_error"):
            return {"error": (meta.get("_error") if meta else "could not analyze this file")}
        out = {
            "file": p.name,
            "type": meta.get("label"),
            "tags": meta.get("tags") or [],
            "caption": meta.get("caption"),
            "ai_source": meta.get("ai_source"),
            "transcript_or_text": meta.get("ocr_text") or None,
            "duration_seconds": meta.get("duration"),
        }
        if not out["tags"] and not out["transcript_or_text"] and not out["caption"]:
            return {"error": "Analysis produced no content — the file may be unreadable, empty, "
                             "or an unsupported format."}
        return _jsonsafe(out)
    except Exception as e:
        return {"error": f"analyze failed: {e}"}


@server.tool(description="Save a file you just analyzed into Filect's index so it becomes "
                         "SEARCHABLE (search_files will find it). Call AFTER analyze_file, passing "
                         "the label/tags/caption YOU produced plus any extracted text/transcript. "
                         "Writes to the local index only — never moves or changes the file. Images "
                         "and videos count against the plan's media index limit, same as the app. "
                         "A file that ALREADY has an analysis (and is unchanged since) is NOT "
                         "overwritten — pass reanalyze=true ONLY if the user explicitly asked to "
                         "re-analyze it.")
def save_to_index(path: str, label: str = "", tags: list = None, caption: str = "", text: str = "",
                  reanalyze: bool = False) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        import mimetypes
        from datetime import datetime
        from app.core.database import file_index
        from app.core.search import is_media_file
        p = Path(path)
        if not p.exists():
            return {"error": "file not found"}
        saved = None if reanalyze else _saved_analysis(p)
        if saved:
            out = _saved_summary(p, saved)
            out.update({"saved": False, "note": "Not saved: this file already has an analysis (shown "
                        "here), and the user didn't ask to re-analyze it. Only if they did: call "
                        "save_to_index again with reanalyze=true."})
            return out
        existing = file_index.get_file_by_path(str(p))
        if is_media_file(p) and not existing:           # a re-analysis isn't a new media file
            from app.core.supabase_client import supabase_auth
            chk = supabase_auth.can_index_media(1) or {}
            if not chk.get("allowed"):
                return {"error": chk.get("reason") or "media index limit reached for your plan",
                        "remaining": chk.get("remaining"), "limit": chk.get("limit")}
        if (existing or {}).get("category"):
            category = existing["category"]                 # keep the file's real category
        else:
            try:
                from app.core.categorize import categorize_file
                category = categorize_file(p) or "Misc"
            except Exception:
                category = "Misc"
        try:
            content_hash = _file_sha256(p)                  # so a later change is recognized
        except Exception:
            content_hash = None
        file_data = {
            "source_path": str(p),
            "name": p.name,
            "extension": p.suffix.lower(),
            "size": p.stat().st_size,
            "mime_type": mimetypes.guess_type(str(p))[0] or "",
            "category": category,
            "content_hash": content_hash,
            "last_indexed_at": datetime.utcnow().isoformat(),
            "label": (label or "").strip(),
            "tags": tags or [],
            "caption": (caption or "").strip(),
            "ocr_text": (text or "").strip(),
            "has_ocr": bool((text or "").strip()),
            "ai_source": "mcp:client-model",
            "metadata": {"is_file": True, "is_dir": False},
        }
        ok = file_index.add_file(file_data)
        if ok and is_media_file(p) and not existing:
            try:
                from app.core.supabase_client import supabase_auth
                supabase_auth.increment_index_usage(1)
            except Exception:
                pass
        if not ok:
            note = "could not save to index"
        elif reanalyze and existing:
            note = "Re-analyzed — the new analysis replaced the old one; search_files finds it now."
        else:
            note = "Indexed — search_files can find it now."
        return {"saved": bool(ok), "file": p.name, "note": note}
    except Exception as e:
        return {"error": f"save failed: {e}"}


def _mcp_plans_dir():
    from app.core.settings import settings
    d = settings.get_app_data_dir() / "mcp_plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _files_under(folder: str):
    """Indexed files whose path is under `folder`, as dicts the plan functions expect. Matches
    both the given path and its resolved form. Uses the OS path separator for the LIKE prefix."""
    import sqlite3
    import json as _json
    from app.core.database import file_index
    cands = {str(Path(folder)).rstrip("\\/"), str(Path(folder).resolve()).rstrip("\\/")}
    conn = sqlite3.connect(file_index.db_path)
    conn.row_factory = sqlite3.Row
    seen, out = set(), []
    for c in cands:
        for r in conn.execute("SELECT * FROM files WHERE file_path LIKE ?", (c + os.sep + "%",)).fetchall():
            d = dict(r)
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            try:
                d["tags"] = _json.loads(d.get("tags") or "[]")
            except Exception:
                d["tags"] = []
            try:
                d["metadata"] = _json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            out.append(d)
    conn.close()
    return out


_METADATA_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _prune_empty_dirs(dirs) -> list:
    """Delete folders left empty by a move (ignoring OS metadata files like Thumbs.db/desktop.ini),
    mirroring the app's post-organize cleanup. Returns the folders actually removed. Never raises."""
    removed, seen = [], set()
    for d in dirs:
        try:
            folder = Path(d)
        except Exception:
            continue
        key = str(folder)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not folder.is_dir():
                continue
            for meta in list(folder.iterdir()):
                if meta.is_file() and meta.name in _METADATA_FILES:
                    try:
                        meta.unlink()
                    except Exception:
                        pass
            if any(folder.iterdir()):   # still has real contents → keep it
                continue
            folder.rmdir()
            removed.append(str(folder))
        except Exception:
            pass
    return removed


def _index_apply_moves(moves: list) -> dict:
    """Move files on disk AND keep the search index in sync (Mac #6, e541bcb — inlined here since
    Windows has no bridge). Resolve each file's index id by its CURRENT (pre-move) path BEFORE
    apply mutates destination_path, run the move, then batch-update the index to the landed paths
    (the #4 triggers mirror those path changes into the FTS automatically)."""
    from app.core.apply import apply_moves as _apply
    from app.core.database import file_index
    moves = moves or []
    ids = {}
    for m in moves:
        src = m.get("source_path")
        if src:
            rec = file_index.get_file_by_path(src)
            if rec:
                ids[src] = rec["id"]
    success, errors, log_file, renamed = _apply(moves)
    db_updates = []
    for m in moves:
        fid, dest = ids.get(m.get("source_path")), m.get("destination_path")
        if fid and dest:
            try:
                if Path(dest).exists():
                    db_updates.append((fid, dest))
            except Exception:
                pass
    paths_updated = 0
    if db_updates:
        try:
            paths_updated = file_index.update_file_paths_batch(db_updates)
        except Exception as e:
            logger.warning("MCP apply_moves: index path update failed: %s", e)
    # Clean up folders the move left empty (mirrors the app's post-organize cleanup).
    removed_dirs = _prune_empty_dirs(Path(m["source_path"]).parent for m in moves if m.get("source_path"))
    return {"success": success, "moved": len(moves) - len(errors),
            "errors": errors[:10], "log_file": log_file,
            "renamed_to_avoid_collision": renamed, "paths_updated": paths_updated,
            "removed_empty_folders": removed_dirs}


@server.tool(description="Preview how Filect would organize a folder. READ-ONLY — moves nothing. "
                         "Returns a plan_id and the proposed folders (which files go where). The "
                         "folder must already be indexed in Filect. Review the plan, then call "
                         "apply_organization_plan(plan_id) to actually move the files.")
def generate_organization_plan(folder: str, instructions: str = "") -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        import json as _json
        import uuid
        from app.core.ai_organizer import request_organization_plan, plan_to_moves
        files = _files_under(folder)
        if not files:
            return {"error": "No indexed files under this folder — index it in Filect first."}
        files_by_id = {f["id"]: f for f in files}
        plan = request_organization_plan(
            instructions or "Organize these files into sensible folders by content and type.", files)
        if not plan or not plan.get("folders"):
            return {"error": "Could not generate a plan."}
        moves = plan_to_moves(plan, files_by_id, Path(folder))
        if not moves:
            return {"error": "The plan produced no moves."}
        plan_id = uuid.uuid4().hex[:12]
        (_mcp_plans_dir() / f"plan-{plan_id}.json").write_text(_json.dumps(moves), encoding="utf-8")
        summary = {}
        for m in moves:
            folder_name = Path(m["destination_path"]).parent.name
            summary.setdefault(folder_name, []).append(Path(m["source_path"]).name)
        return _jsonsafe({
            "plan_id": plan_id,
            "total_files": len(moves),
            "proposed_folders": {k: v[:20] for k, v in summary.items()},
            "note": "Nothing moved yet. Call apply_organization_plan(plan_id) to apply.",
        })
    except Exception as e:
        return {"error": f"plan failed: {e}"}


@server.tool(description="Apply a plan from generate_organization_plan: MOVES the files into the "
                         "proposed folders AND updates the search index so they stay findable at "
                         "their new paths. Reversible with undo_last_organize. Never deletes "
                         "anything. Needs the plan_id from a prior generate call. SAFETY: a call "
                         "WITHOUT confirm=true moves nothing — it returns the exact moves for you to "
                         "show the user. Only after the user EXPLICITLY approves do you call again "
                         "with confirm=true. Never set confirm=true on your own.")
def apply_organization_plan(plan_id: str, confirm: bool = False) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        import json as _json
        pf = _mcp_plans_dir() / f"plan-{plan_id}.json"
        if not pf.exists():
            return {"error": "Unknown plan_id — generate a plan first."}
        moves = _json.loads(pf.read_text(encoding="utf-8"))
        if not confirm:
            # Safe-by-default: summarize the exact moves and move nothing until the user approves.
            summary = {}
            for m in moves:
                folder_name = Path(m["destination_path"]).parent.name
                summary.setdefault(folder_name, []).append(Path(m["source_path"]).name)
            return _jsonsafe({
                "confirmation_required": True,
                "action": "move files",
                "plan_id": plan_id,
                "total_files": len(moves),
                "proposed_folders": {k: v[:20] for k, v in summary.items()},
                "warning": ("This MOVES these files on disk now (reversible with undo_last_organize). "
                            "Show the user this plan and get an EXPLICIT yes, then call again with "
                            "confirm=true. Do NOT set confirm=true without the user's approval."),
            })
        res = _index_apply_moves(moves)
        res["note"] = "Call undo_last_organize() to reverse this."
        return _jsonsafe(res)
    except Exception as e:
        return {"error": f"apply failed: {e}"}


@server.tool(description="Reverse the MOST RECENT organize operation — moves every file back to "
                         "where it was and updates the index. Never deletes anything. Use right "
                         "after an apply_organization_plan you want to take back.")
def undo_last_organize() -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        import json as _json
        import shutil
        from app.core.settings import settings
        from app.core.database import file_index
        logs = sorted(settings.get_moves_dir().glob("moves-*.json"))
        if not logs:
            return {"error": "No organize operation to undo."}
        latest = logs[-1]
        moves = _json.loads(latest.read_text(encoding="utf-8")).get("moves", [])
        reverted, skipped, db_updates = 0, [], []
        for m in reversed(moves):
            src = Path(m.get("to", ""))      # file is currently at the destination
            dst = Path(m.get("from", ""))    # move it back to the original
            if not src.exists():
                skipped.append(f"missing: {src.name}")
                continue
            if dst.exists():
                skipped.append(f"original occupied: {dst.name}")
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                reverted += 1
                # keep the index in sync: resolve id by the current (destination) path, point it back
                rec = file_index.get_file_by_path(str(src))
                if rec:
                    db_updates.append((rec["id"], str(dst)))
            except Exception as e:
                skipped.append(f"{src.name}: {e}")
        paths_updated = 0
        if db_updates:
            try:
                paths_updated = file_index.update_file_paths_batch(db_updates)
            except Exception as e:
                logger.warning("MCP undo: index path update failed: %s", e)
        # Remove destination folders that undo left empty (mirrors the app's cleanup).
        removed_dirs = _prune_empty_dirs(Path(m.get("to", "")).parent for m in moves if m.get("to"))
        return {"reverted": reverted, "skipped": skipped[:10],
                "paths_updated": paths_updated, "from_log": latest.name,
                "removed_empty_folders": removed_dirs}
    except Exception as e:
        return {"error": f"undo failed: {e}"}


@server.tool(description="Turn ON auto-organize for a folder (ongoing background sorting). There are "
                         "THREE modes and you MUST ask the user which one they want before calling — "
                         "do NOT pick a default: mode=1 'Re-organize All' (sorts ALL existing files "
                         "AND new ones), mode=2 'Organize As-Is' (sorts files only into the folder's "
                         "EXISTING subfolders), mode=3 'New files only' (leaves existing files alone, "
                         "only sorts files added from now on). Required flow: (1) ASK the user which "
                         "mode; (2) preview with generate_organization_plan and show it; (3) ask "
                         "'confirm? this will auto-organize your files based on this'; (4) only on an "
                         "explicit YES, call this with confirm=true. Takes effect when the Filect "
                         "app next (re)starts; removable from the app's Auto-Organize tab. SAFETY: a "
                         "call WITHOUT confirm=true changes nothing — it returns what WOULD be turned "
                         "on; only call with confirm=true after the user explicitly agrees.")
def enable_auto_organize(folder: str, mode: int, instruction: str = "", confirm: bool = False) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        from app.core.settings import settings
        if mode not in (1, 2, 3):
            return {"error": "mode must be 1 (re-organize all), 2 (organize as-is), or 3 (new files only)"}
        p = Path(folder)
        if not p.exists() or not p.is_dir():
            return {"error": "folder not found"}
        mode_name = {1: "re-organize all files", 2: "organize into existing subfolders only",
                     3: "new files only (existing untouched)"}[mode]
        if not confirm:
            # Safe-by-default: describe what would be enabled and change nothing until approved.
            return {
                "confirmation_required": True,
                "action": "enable auto-organize",
                "folder": str(p),
                "mode": mode_name,
                "warning": ("This turns ON ongoing background auto-organizing: Filect will move "
                            "files in this folder AUTOMATICALLY whenever the app runs. Show the user "
                            "exactly this and get an EXPLICIT yes, then call again with confirm=true. "
                            "Do NOT set confirm=true without the user's approval."),
            }
        settings.add_auto_organize_folder(str(p), instruction or "")
        settings.update_auto_organize_action(str(p), mode)
        return {
            "enabled": True,
            "folder": str(p),
            "mode": mode_name,
            "note": "Registered. Filect starts watching this folder when the app next launches. "
                    "Remove it anytime from the app's Auto-Organize tab.",
        }
    except Exception as e:
        return {"error": f"setup failed: {e}"}


if __name__ == "__main__":
    server.run("stdio")
