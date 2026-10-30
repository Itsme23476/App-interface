"""Filect MCP server (EXPERIMENTAL) — isolated from the app.

This package ONLY imports from `app.core.*` and calls the same functions the app uses. It
never edits app code and is not part of the shipped build. Runs as its own process.

Windows port of the macOS filect_mcp read-only server (Mac commit 088d904), mcp 2.x API.
"""
