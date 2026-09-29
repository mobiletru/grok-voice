# Grok Voice - Home Assistant add-on

Live Grok (xAI) voice assistant with a big tap-to-talk page, made for the in-car browser, plus optional
approval-gated Wrenchworks draft invoicing.

## Install
1. In Home Assistant: **Settings > Add-ons > Add-on Store > ⋮ > Repositories**.
2. Add `https://github.com/mobiletru/grok-voice` and press **Add**.
3. Install **Grok Voice**, open its **Configuration** tab, set `xai_api_key`, start it.

Your API key and Wrenchworks credentials stay in the add-on options on your own Home Assistant; nothing in this
repository contains any secret. See `grok_voice/DOCS.md` for setup and `grok_voice/CHANGELOG.md` for changes.

## Tests
`python tests/run_tests.py` (needs `aiohttp`; uses local mocks, no real keys).
