# Grok Voice (Home Assistant app) - DRAFT

A live Grok (xAI) voice assistant with a huge tap-to-talk page, built for the Tesla in-car browser
(HTTPS required for the microphone). Optional: Grok can prepare **draft** Wrenchworks invoices that
only save after you tap APPROVE on screen (saving is always on, always as an unsent draft).

**Status: unpublished draft (v0.2.2).** See `DOCS.md` for setup and design notes.

> **Driving safety:** use it while parked. Tesla disables or restricts the browser while driving in
> some regions, and you are responsible for following local law. The page is designed for glance-free
> use, but it does not make hands-on use of a screen safe.

Quick facts
- API: xAI Voice Agent / Speech-to-Speech WebSocket, default model `grok-voice-latest`, default voice `eve`.
- Add-on backend: Python 3 + aiohttp, ~one process, no database, nothing persisted.
- Arch: aarch64, amd64. Base image pinned to `ghcr.io/home-assistant/base:3.24-2026.08.0`.
- Local test suite: `python tests/run_tests.py` (uses mock xAI + fake Wrenchworks; no real key needed).
