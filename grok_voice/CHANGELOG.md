# Changelog

## 0.2.1
Diagnosability and reliability of `create_invoice_draft` (the log used to say only `tool create_invoice_draft -> ok=False`).
- Logging: every failed tool call now logs `tool <name> -> ok=False reason=<code>: <detail>` at WARNING level. Reasons include `missing_unit_number`, `customer_not_found`, `customer_ambiguous`, `customer_lookup_failed` (with HTTP status, e.g. `HTTP 401` / `HTTP 503` / connection refused / timeout), `catalog_unavailable` (shop login/`/api/db` HTTP status), `code_not_in_catalog`, `code_ambiguous`, `hours_mismatch`, `too_few_codes`, `invalid_hours`, `invalid_part`, `bad_arguments`, `internal_error`. Failed Approve/save attempts are logged the same way.
- Log safety: all log text goes through `redact()` (passwords, mcp token, session cookie, xAI key, `Bearer ...`, `xai-...`, `password=`/`token=` values, e-mail addresses, phone numbers are masked; truncated to 400 chars). Customer records are never logged; only the spoken name (max 40 chars) and counts.
- Errors returned to Grok are specific and actionable: exact hour sums and difference, catalog-code suggestions ("Did you mean ..."), close customer names, phone tails to tell identical names apart, all validation problems listed at once, HTTP status and the option to check for Wrenchworks login/MCP failures. Tool crashes no longer collapse to `Tool failed: <ExceptionName>`.
- Fix: customer lookup. `search_customers` in the shop is a raw substring match over the whole customer record (it also hits address/e-mail/phone), returns at most `limit` rows, and its `name`/`company` are not what was searched. The lookup now retries with a punctuation/suffix-free name and its longest word, requests up to 50 rows, ranks locally on name/company only (exact > prefix > all words > close spelling), matches the `company` field, and never auto-picks a sound-alike name.
- Fix: labor code matching is now case-insensitive and ignores spaces/dashes/underscores (canonical shop code is kept); ambiguous matches are reported instead of guessed.
- Fix: hours. Line hours are rounded to 0.01 and the total may drift by 0.011 (voice-given values like 0.333 + 0.667, or 0.1 + 0.2 float noise, used to fail the exact-match check). The same code repeated does not count as several codes. Numeric strings and JSON-string arrays from the model are accepted.
- Fix: GET `/api/db` now also sends the Origin header (CSRF check on session requests), non-2xx replies show the HTTP status and the shop's short error code, an empty labor catalog is reported as such.
- Tests: 76 checks (added error reasons, real `search_customers` record shape, code matching, hours rounding, redaction, log content).

## 0.2.0 (draft, unpublished)
- Wrenchworks integration now uses the real shop API (verified from the Wrenchworks 1.1.126 source) instead of stubs.
  - Default internal URL `http://local-wrenchworks:8099` (`wrenchworks_base_url`).
  - Labor catalog: shop login (`POST /api/auth/login`) + `GET /api/db`, only the `labor` rows are kept.
  - Customer lookup and invoice creation via the shop's bearer-authenticated `POST /api/mcp` tools `search_customers` / `create_draft_invoice` (draft-only, never emails).
  - New options: `wrenchworks_password` (shop login password, catalog) and `wrenchworks_mcp_token` (Wrenchworks `mcp_token`). Removed: `wrenchworks_api_token`, `wrenchworks_catalog_path`, `wrenchworks_save_path`.
- Drafts now require the customer to exist in Wrenchworks (exact name resolved; unknown/ambiguous customers are refused so Grok asks).
- `get_labor_catalog` accepts an optional `query` (the real catalog is large); PO number is saved in the invoice notes.
- Kept: truck/unit first, >=2 real catalog codes, $150 Recology/Charter else $180, 10.25% tax on parts only, on-screen Approve/Discard, save only after Approve, save off by default, no email path.
- Tests: fake Wrenchworks now mirrors the real endpoints (login cookie + Origin check, `/api/db`, `/api/mcp`).

## 0.1.1 (draft, unpublished)
- Fix: `reasoning_effort` is now sent as the `reasoning: {effort}` session parameter in `session.update` (per the xAI Speech to Speech docs) instead of a `reasoning.effort` URL query parameter, which the docs do not list.
- Add `tests/real_e2e.py`: opt-in real end-to-end test against api.x.ai (connect/session, typed message text+audio, invoice flow asking for the truck number first, captions/`audio.input.transcription` acceptance, key-not-in-log check). Reads `XAI_API_KEY` from the environment only.
- NOTE: the live run has NOT been performed yet (no key was available in the test shell); the fix above is docs-based and unverified against the live API.

## 0.1.0 (draft, unpublished)
- Initial draft: ingress tap-to-talk page, push-to-talk and hands-free (server VAD) modes, typed-message fallback.
- Backend proxies to the xAI Voice Agent realtime WebSocket (`wss://api.x.ai/v1/realtime`); API key stays server-side.
- Optional token-protected direct listener (port 8100) for use behind HTTPS outside the HA iframe.
- Wrenchworks invoice drafting via Grok tool-calling: draft-only, approval-gated, save disabled by default. Catalog/save endpoints are unverified stubs.
- `allow_ha_control` option reserved (not implemented).
