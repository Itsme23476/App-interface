# Filect MCP server (experimental, Windows) — read-only v1

Exposes Filect's local search + analysis to an MCP client (Claude Desktop/Code) as a
separate process. It imports `app.core.*` from the sibling `ai_file_organizer/` package and
calls the same functions the app uses. It **never** edits app code and is **not** in the
PyInstaller build yet.

**Read-only:** it never moves/deletes files, never writes app settings, never re-indexes.

## Tools
- `search_files(query, limit=20)` — natural-language search over the indexed files (local, no auth).
- `get_file_details(path)` — stored index details (tags/caption/category/dates) for one file (local, no auth).
- `analyze_image(path)` — vision label/tags/caption for an image. Paid AI call; needs a signed-in session.
- `transcribe_file(path)` — transcribe an audio file. Paid AI call; needs a signed-in session.

(The macOS `suggest_custom_words` tool is omitted until Windows has an `app.core.vocab` equivalent.)

## Run (dev)
From the `win/` directory, with the app's venv:

```
set QT_QPA_PLATFORM=offscreen
.\ai_file_organizer\.venv\Scripts\python -m filect_mcp
```

It speaks the MCP protocol over stdio. Register it with a client (e.g. Claude Desktop's
`claude_desktop_config.json`) pointing `command` at that venv python, `args` at
`["-m", "filect_mcp"]`, and `cwd` at the `win/` directory. The "Connect to Claude/Codex"
settings UI that writes this automatically is a later phase.

## Requires
`mcp>=2.3,<3` (installed in the app venv).
