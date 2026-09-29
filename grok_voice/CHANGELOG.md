# Changelog

## 0.2.2
Labor codes via the read-only MCP tool, saving always on, "start a new draft" recovery.
- Catalog: `fetch_catalog()` now calls the Wrenchworks bearer tool `search_labor_codes` (added in Wrenchworks 1.1.127; returns only `{id, code, name, hours, rate}`, paged). The shop password is no longer needed. If the shop answers `Unknown tool: search_labor_codes` (older Wrenchworks) it falls back to the old shop login + `GET /api/db` path (needs `wrenchworks_password`); other errors (401 token, 503, timeouts) are reported, not silently rerouted. Duplicate codes are collapsed (first wins).
- REMOVED option `wrenchworks_save_enabled` (config, translations, docs, code, tests). Saving is always on, but ONLY after the on-screen APPROVE tap; no autosave, no email path. APPROVE still refuses with a clear on-screen reason ("NOT saved: ...") for a sample catalog, TBD part prices, or a missing `wrenchworks_mcp_token` (no more silent dry run). `wrenchworks_password` is now optional.
- New-draft-on-problem: on any problem (save failed, refused, tool error, discard) nothing is edited or retried; Grok is told to create a brand NEW draft from scratch (system prompt rule 7, tool error texts, on-screen messages). Draft statuses `not_saved` / `save_failed` / `duplicate` lock the old draft's buttons.
- Duplicate protection: before every save the add-on runs the read-only `search_invoices` (status draft) for the same customer, unit and total; if one exists it is not saved again and the existing number is shown; if the check fails nothing is saved. A save that fails is never retried automatically, since it may have gone through.
- FULL catalogs: `get_labor_catalog` now searches/pages the COMPLETE Wrenchworks labor catalog (`query` + `offset` + `limit`, default 80, max 200; reports `catalog_size`, `total_matches`, `has_more` and tells Grok the next `offset`). A catalog too large to page completely is refused rather than used partially.
- Parts: new tool `get_parts_catalog` (same paging) backed by the new read-only Wrenchworks tool `search_parts` (1.1.127+; fallback to `/api/db` `parts` rows on older Wrenchworks with `wrenchworks_password`). Draft parts must use a real catalog part number; the catalog price always wins (no price = TBD, blocks save); unknown parts are refused ("tell Benjamin, never invent"). Tax stays 10.25% on parts only.
- AI-created marker: every saved invoice gets the first notes line "Created by Grok Voice (AI) - review before sending" and `aiCreated: true` (Wrenchworks 1.1.128+ shows an AI-created badge; older versions ignore the flag). The on-screen draft shows the same "AI-CREATED" tag.
- On-screen catalog status line ("Labor codes: N loaded from Wrenchworks." or the exact reason the SAMPLE catalog / an error applies) via `GET /api/catalog-status`.
- Tests: fake Wrenchworks now has `search_labor_codes` / `search_invoices`; added tool-preferred catalog, paging, old-Wrenchworks fallback, no-login-when-tool-present, always-on save, blocker messages, duplicate check, failed-save-not-retried checks.

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
