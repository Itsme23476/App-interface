"""The notch's Details feed: a conversation as structured entries, rendered to rich HTML.

Spec agreed with Damianos (2026-11-02): Details should read like the real Claude Code conversation —
what you said, Claude's replies (formatted), and every action WITH its substance. A file it wrote shows a
preview, its size, "Show full" and Open (Open in browser for HTML); an edit shows a mini diff; a command
shows ✓/✗ and its last output lines (errors in red); a web search its query; a plan its checklist.
Failures say so (✗ + reason) instead of a misleading "Writing…". Long replies preview with "Show full".

Entries are plain dicts — they live on AgentSession and are built the same way from live agent steps
and from saved transcripts, so a past chat reads exactly like one you watched run:
    {"id", "kind": "user", "text"}          what you said
    {"id", "kind": "text", "text"}          Claude's reply (markdown)
    {"id", "kind": "tool", "tool", "input", "tool_id", "result"}
                                             result: None while running, else {"text", "error"}
Links in the HTML: file:///… → open the file; http(s) → open the page; full:<id> → toggle full view.

Windows port: the colours below are the dark card's; the Agents page (light or dark app theme) renders
inside `palette(...)`, which swaps them for the page's. Monospace = Consolas.
"""
from __future__ import annotations
import html
import os
import re
from contextlib import contextmanager
from functools import lru_cache

from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextCursor, QTextDocument

TEXT = "#D6D6DE"
MUTED = "#8A8A92"
ACCENT = "#D97757"
GREEN = "#6FCF7F"
RED = "#F2645A"
USER = "#ECECEC"           # what you said (bold line)
MONO_FONT = "Consolas,Menlo,monospace"
MONO = f"font-family:{MONO_FONT}; font-size:10.5px; white-space:pre-wrap;"


@contextmanager
def palette(text=None, muted=None, accent=None, green=None, red=None, user=None):
    """Render with the page's colours (light or dark theme) instead of the dark card's."""
    global TEXT, MUTED, ACCENT, GREEN, RED, USER
    saved = (TEXT, MUTED, ACCENT, GREEN, RED, USER)
    TEXT, MUTED, ACCENT = text or TEXT, muted or MUTED, accent or ACCENT
    GREEN, RED, USER = green or GREEN, red or RED, user or (text or USER)
    try:
        yield
    finally:
        TEXT, MUTED, ACCENT, GREEN, RED, USER = saved
PREVIEW_LINES = 8          # lines of a file / diff / reply shown before "Show full"
REPLY_CHARS = 700          # a reply longer than this previews too
OUTPUT_LINES = 3           # last lines of a command's output shown
_PROSE = {".md", ".markdown", ".txt", ".rst", ".text"}
_WEB = {".html", ".htm"}


# -- building entries ---------------------------------------------------------------------------
def add_step(entries, kind, text="", tool="", input=None, tool_id="", is_error=False, images=None):
    """Append one step to `entries`; a "result" attaches to its tool call instead of adding a row.
    A "user" step can carry `images` — the fn + ⌃ + Space screenshots sent with it."""
    if kind == "result":
        for e in reversed(entries):
            if e["kind"] == "tool" and e["tool_id"] == tool_id:
                e["result"] = {"text": text or "", "error": bool(is_error)}
                break
        return
    e = {"id": str(len(entries)), "kind": kind}
    if kind == "tool":
        e.update(tool=tool, input=dict(input or {}), tool_id=tool_id, result=None)
    else:
        e["text"] = text or ""
        if images:
            e["images"] = list(images)
    entries.append(e)


def _last_turn(entries):
    for i in range(len(entries) - 1, -1, -1):
        if entries[i]["kind"] == "user":
            return entries[i + 1:]
    return entries


def final_reply(entries) -> str:
    """Claude's last message in the latest turn — its closing summary."""
    for e in reversed(_last_turn(entries)):
        if e["kind"] == "text" and e["text"].strip():
            return e["text"]
    return ""


def visible_entries(entries, hide_final=False, hide_prompt=False):
    """Details minus what the card already shows (VoiceOS never shows a thing twice): the latest
    turn's final reply once the done card carries it, and the latest prompt when the top bubble shows
    it in full (kept if it carries screenshots — the bubble can't show those)."""
    hide = set()
    if hide_final:
        for e in reversed(_last_turn(entries)):
            if e["kind"] == "text" and e["text"].strip():
                hide.add(e["id"])
                break
    if hide_prompt:
        for e in reversed(entries):
            if e["kind"] == "user":
                if not e.get("images"):
                    hide.add(e["id"])
                break
    return [e for e in entries if e["id"] not in hide]


def deliverable(entries):
    """(path, size label) of the file the latest turn produced — its last successful write — or None."""
    for e in reversed(_last_turn(entries)):
        if e["kind"] == "tool" and e["tool"] == "Write" and not (e["result"] or {}).get("error"):
            path = e["input"].get("file_path", "")
            if path:
                return path, size_label(path, e["input"].get("content", "") or "")
    return None


def size_label(path, content) -> str:
    """'912 words' for prose files, '1,240 lines' for code."""
    if os.path.splitext(path)[1].lower() in _PROSE:
        n = len(content.split())
        return f"{n:,} word" + ("" if n == 1 else "s")
    n = len(content.splitlines())
    return f"{n:,} line" + ("" if n == 1 else "s")


def open_label(path) -> str:
    return "Open in browser ↗" if os.path.splitext(path)[1].lower() in _WEB else "Open ↗"


def file_url(path) -> str:
    return QUrl.fromLocalFile(path).toString()


# -- rendering ----------------------------------------------------------------------------------
def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


@lru_cache(maxsize=512)
def _md(text: str) -> str:
    """Markdown → an HTML fragment via Qt's own markdown support (no extra dependency). Cached: the
    feed re-renders on every agent step."""
    doc = QTextDocument()
    doc.setMarkdown(text)
    b = doc.begin()                      # code blocks come in as non-wrapping lines — in a 400px feed
    while b.isValid():                   # that forced a horizontal scrollbar, so let them wrap
        if b.blockFormat().nonBreakableLines():
            fmt = b.blockFormat()
            fmt.setNonBreakableLines(False)
            QTextCursor(b).setBlockFormat(fmt)
        b = b.next()
    m = re.search(r"<body[^>]*>(.*)</body>", doc.toHtml(), re.S)
    if not m:
        return _esc(text)
    # Headings at document size (an essay's "# Title" in xx-large) swamp a ~400px feed — keep them
    # bold but compact. They must become plain paragraphs too: Qt's importer gives <h1> its own built-in
    # size (18pt) that overrides the span's font-size.
    frag = re.sub(r"font-size:\s*(xx-large|x-large|large)",
                  lambda s: "font-size:" + {"xx-large": "14px", "x-large": "13px", "large": "12.5px"}[s.group(1)],
                  m.group(1))
    return re.sub(r"<(/?)h[1-6]\b", r"<\1p", frag)


def _head(text, lines=PREVIEW_LINES, chars=None):
    """(preview, truncated?) — the first `lines` lines, and at most `chars` characters if given."""
    rows = text.splitlines()
    out = "\n".join(rows[:lines])
    if chars and len(out) > chars:
        out = out[:chars].rsplit(" ", 1)[0] + " …"
    return out, out != text and (len(rows) > lines or bool(chars and len(text) > chars))


def _close_fence(md: str) -> str:
    return md + "\n```" if md.count("```") % 2 else md   # a preview cut inside a code block


def _link(href, label) -> str:
    return f'<a href="{_esc(href)}" style="color:{ACCENT}; text-decoration:none;">{label}</a>'


def _full_link(eid, is_open) -> str:
    return (f'<p style="margin:0 0 4px 14px;">'
            f'{_link(f"full:{eid}", "Show less ⌃" if is_open else "Show full ⌄")}</p>')


def _row(icon, body, icon_color=None, color=None) -> str:
    # (colours resolved at CALL time, so palette(...) applies)
    return (f'<p style="margin:6px 0 2px 0; color:{color or TEXT};">'
            f'<span style="color:{icon_color or MUTED};">{icon}</span>&nbsp; {body}</p>')


def _mono(text, color=None) -> str:
    return f'<p style="{MONO} color:{color or MUTED}; margin:1px 0 3px 14px;">{_esc(text)}</p>'


def _open(path) -> str:
    if not path:
        return ""
    if not os.path.exists(path):
        return f' <span style="color:{MUTED};">· moved or deleted</span>'
    return " · " + _link(file_url(path), open_label(path))


def _reason(res) -> str:
    t = re.sub(r"</?tool_use_error>", "", (res or {}).get("text", "")).strip()
    first = next((ln.strip() for ln in t.splitlines() if ln.strip()), "it failed")
    return first[:180]


_SHOT = re.compile(r"^\[screenshot: (.+)\]\s*$", re.M)
_SHOT_NOTE = re.compile(r"^\((?:Screenshot|\d+ screenshots) of my screen attached[^\n]*\)\s*$", re.M)


def split_screenshots(text: str):
    """(your words, [screenshot paths]) — a sent turn's text carries the note + "[screenshot: …]" lines
    (see agent_runner.user_message); show the words, and the screenshots as thumbnails."""
    shots = _SHOT.findall(text or "")
    return _SHOT_NOTE.sub("", _SHOT.sub("", text or "")).strip(), shots


def _thumbs(paths) -> str:
    cells = []
    for p in paths:
        if not os.path.exists(p):
            cells.append(f'<span style="color:{MUTED};">🖼 screenshot moved or deleted</span>')
            continue
        t = p[:-4] + ".thumb.png"                      # screen_ink saves '<name>.thumb.png' beside it
        src = file_url(t if os.path.exists(t) else p)
        cells.append(f'<a href="{_esc(file_url(p))}"><img src="{_esc(src)}" width="176"></a>')
    return f'<p style="margin:2px 0 6px 0;">{"&nbsp; ".join(cells)}</p>'


def _render(e, is_open) -> str:
    k, eid = e["kind"], e["id"]
    if k == "user":
        words, shots = split_screenshots(e["text"])
        shots = list(e.get("images") or []) + [s for s in shots if s not in (e.get("images") or [])]
        out = f'<p style="margin:10px 0 4px 0; color:{USER};"><b>▸ {_esc(words)}</b></p>'
        return out + (_thumbs(shots) if shots else "")
    if k == "text":
        shown, more = _head(e["text"], 12, REPLY_CHARS)
        body = _md(e["text"] if is_open or not more else _close_fence(shown))
        return body + (_full_link(eid, is_open) if more else "")

    tool, inp, res = e["tool"], e["input"], e["result"]
    err = bool(res and res.get("error"))
    path = inp.get("file_path", "") or inp.get("notebook_path", "")
    name = _esc(os.path.basename(path))

    if tool == "Write":
        if err:
            return _row("✗", f"Couldn't write <b>{name}</b>", RED, RED) + _mono(_reason(res), RED)
        content = inp.get("content", "") or ""
        verb = "Writing" if res is None else "Wrote"
        out = _row("✎", f"{verb} <b>{name}</b> · {size_label(path, content)}{_open(path)}")
        shown, more = _head(content)
        shown = content if is_open else shown
        prose = os.path.splitext(path)[1].lower() in _PROSE
        out += (f'<div style="margin-left:14px;">{_md(_close_fence(shown))}</div>' if prose
                else _mono("\n".join(ln[:120] for ln in shown.splitlines())))
        return out + (_full_link(eid, is_open) if more else "")

    if tool in ("Edit", "MultiEdit"):
        if err:
            return _row("✗", f"Couldn't edit <b>{name}</b>", RED, RED) + _mono(_reason(res), RED)
        edits = inp.get("edits") or [inp]
        old = [ln for x in edits for ln in (x.get("old_string") or "").splitlines()]
        new = [ln for x in edits for ln in (x.get("new_string") or "").splitlines()]
        out = _row("✎", f'Edited <b>{name}</b> · <span style="color:{GREEN};">+{len(new)}</span> '
                        f'<span style="color:{RED};">−{len(old)}</span>{_open(path)}')
        diff = [("−", ln, RED) for ln in old] + [("+", ln, GREEN) for ln in new]
        shown = diff if is_open else diff[:PREVIEW_LINES]
        out += (f'<p style="{MONO} margin:1px 0 3px 14px;">'
                + "<br>".join(f'<span style="color:{c};">{s} {_esc(ln[:120])}</span>' for s, ln, c in shown)
                + "</p>")
        return out + (_full_link(eid, is_open) if len(diff) > PREVIEW_LINES else "")

    if tool in ("Bash", "PowerShell"):          # (Windows Claude Code runs commands in PowerShell without Git Bash)
        mark, color = ("…", MUTED) if res is None else (("✗", RED) if err else ("✓", GREEN))
        cmd = inp.get("command", "")
        desc = (inp.get("description") or "").strip()
        out = _row(mark, _esc(desc), color) + _mono(f"$ {cmd[:200]}", TEXT) if desc \
            else _row(mark, f'<span style="font-family:{MONO_FONT}; font-size:10.5px;">$ {_esc(cmd[:200])}</span>', color)
        lines = (res or {}).get("text", "").strip().splitlines()
        if lines:
            shown = lines if is_open else lines[-OUTPUT_LINES:]
            out += _mono("\n".join(ln[:120] for ln in shown), RED if err else MUTED)
            out += _full_link(eid, is_open) if len(lines) > OUTPUT_LINES else ""
        return out

    if err:                                                   # any other tool that failed
        return _row("✗", f"{_esc(tool)} failed", RED, RED) + _mono(_reason(res), RED)
    if tool == "WebSearch":
        return _row("⌕", f'Searched the web for “{_esc(inp.get("query", ""))}”')
    if tool == "WebFetch":
        url = inp.get("url", "")
        return _row("↗", "Read " + _link(url, _esc(url[:80])))
    if tool == "TodoWrite":
        marks = {"completed": "☑", "in_progress": "◐"}
        items = "<br>".join(f'{marks.get(t.get("status"), "☐")} {_esc(t.get("content", ""))}'
                            for t in inp.get("todos") or [])
        return _row("☰", "Plan") + f'<p style="margin:1px 0 3px 14px; color:{MUTED};">{items}</p>'
    if tool == "Read":
        return _row("·", f"Read {name}", color=MUTED)
    if tool in ("Grep", "Glob"):
        return _row("·", f'Searched for “{_esc(inp.get("pattern", ""))}”', color=MUTED)
    if tool in ("Agent", "Task"):                              # a subagent ("Task" in older Claude Code)
        mark = "…" if res is None else "✓"
        return _row("↳", f'{mark} Delegated: {_esc(inp.get("description", ""))}')
    return _row("·", f"Used {_esc(tool)}", color=MUTED)


def deliverable_html(entries) -> str:
    """The done card's 'what it made' line — ✎ essay.md · 912 words · Open ↗ — or '' if no file."""
    d = deliverable(entries)
    if not d:
        return ""
    path, size = d
    return f'✎ <b>{_esc(os.path.basename(path))}</b> · {size}{_open(path)}'


def render_html(entries, expanded=frozenset()) -> str:
    """The whole Details feed. `expanded` = ids of entries whose "Show full" is open."""
    return "".join(_render(e, e["id"] in expanded) for e in entries)
