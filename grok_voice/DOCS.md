# Grok Voice - documentation (DRAFT)

## What it does
Open **Grok Voice** in the HA sidebar (ingress) - or the optional direct HTTPS page - tap the big
button, talk, tap again to send. Grok answers by voice and shows a large-print transcript. There is
also a typed-message box and a hands-free toggle (server-side voice-activity detection).

## Options
| Option | Notes |
| --- | --- |
| `xai_api_key` (password) | From console.x.ai. Never sent to the browser. |
| `model` | Default `grok-voice-latest` (alias of `grok-voice-think-fast-2.0` per docs). |
| `voice` | `eve` (default), `ara`, `rex`, `sal`, `leo`, ... or a custom voice ID. Full list: `GET https://api.x.ai/v1/tts/voices`. |
| `reasoning_effort` | `high` (default) or `none`. |
| `system_prompt` | Default: concise, spoken-friendly, mentions the car. |
| `allow_ha_control` | **Not implemented.** Reserved. Future step: expose a small allow-listed set of HA services as Grok function tools via `homeassistant_api: true`, with the same on-screen approval pattern used for invoices. |
| `access_token` (password, optional) | Enables the direct listener on port 8100 (see below). |
| `wrenchworks_*` | Invoice drafting via your local Wrenchworks app, see below. |

## Microphone + HTTPS (important)
`getUserMedia` only works on HTTPS pages. Your `home.mobileccs.com` HTTPS address qualifies.
**But** Home Assistant shows apps in a cross-origin iframe with no `allow="microphone"`, so the browser
may refuse the mic inside the sidebar view (a fix was merged in HA frontend PR #52068 and then reverted
in #52229 because of ingress cookie/sandbox side effects). Therefore, for the car:

1. Set `access_token` (long random string), start the app, and map host port 8100 in the app's Network tab.
2. Publish port 8100 on its own HTTPS hostname (e.g. a Cloudflare Tunnel public hostname such as
   `grok.mobileccs.com` -> `http://<HA host>:8100`). Do not expose it over plain HTTP.
3. In the Tesla browser open `https://grok.mobileccs.com/?token=YOUR_TOKEN` once (the token is then kept
   in a Secure, HttpOnly, SameSite=Strict cookie) and bookmark `https://grok.mobileccs.com/`.
The sidebar/ingress page still works for typed messages and for desktop/phone browsers that allow the mic.
Consider adding Cloudflare Access in front of the hostname as a second lock.

## xAI API used
See "API research" below. Browser <-> add-on: one WebSocket at `./ws` (relative URL, so it works under the ingress
prefix); binary frames are 24 kHz mono PCM16 both ways, text frames are JSON control/transcript events.
Add-on <-> xAI: `wss://api.x.ai/v1/realtime?model=...` authenticated with the API key in the `Authorization` header.

Modes: push-to-talk sets `turn_detection` to `null` and the page sends `input_audio_buffer.commit` + `response.create`;
hands-free uses `server_vad`. The page is half-duplex (mic audio is not streamed while Grok speaks) to avoid car-speaker echo.

## Wrenchworks invoice drafting (draft-only, approval-gated)
Enable with `wrenchworks_enabled`. Grok gets exactly two tools: `get_labor_catalog` and `create_invoice_draft`.
**There is no save tool and no email tool.** The rules are enforced in code (`app/invoicing.py`), not just in the prompt:

- Unit/truck number is required; the prompt makes Grok ask for it first, and the tool rejects a draft without it.
- Only labor codes present in the catalog are accepted (case-insensitive); hours must be split over at least 2 different codes and add up to the total (to within 0.01 after rounding each line to 2 decimals).
- Failed tool calls are logged as `tool <name> -> ok=False reason=<code>: <detail>` (secrets, tokens and customer records are never logged); Grok gets the same specific reason so it can correct itself.
- Rate is applied by the add-on: $150/hr if the customer name contains "Recology" or "Charter", otherwise $180/hr.
- Tax 10.25% on parts only (labor untaxed), rounded half-up to the cent. Unknown part price = "TBD" (excluded from totals, listed as open item, blocks saving).
- A draft appears on screen with APPROVE / DISCARD buttons. Only the button (a browser action, not a model action) can trigger a save.
- Saving additionally requires: Wrenchworks configured with a real catalog, no TBD prices, and `wrenchworks_save_enabled: true`.
  Otherwise APPROVE reports "approved but NOT saved" and writes nothing. Default is dry-run.
- Saved invoices are created as **unsent drafts** in Wrenchworks (status `draft`, never emailed or charged). The add-on has no email code at all.
- Customer must already exist in Wrenchworks: the add-on looks it up (exact shop spelling is shown on the draft); no match or several matches = the draft is refused and Grok asks Benjamin.
- Recipe followed: `build-mcc-shop-invoice` skill (truck first, hours split across real codes, 10.25% parts tax, draft for approval).

### Connecting to Wrenchworks (verified against the shop source, v1.1.126)
- Wrenchworks is the local add-on `wrenchworks` (slug from its `config.yaml`, port 8099 = direct shop listener; 8098 is ingress only). Other add-ons reach it at **`http://local-wrenchworks:8099`** (`{repo}-{slug}`, repo = `local`). This is the default `wrenchworks_base_url`. If the name does not resolve on your system, open Settings > Apps > Wrenchworks and use the hostname shown there (often `local-wrenchworks`), or the HA host IP with port 8099 if you mapped it.
- **Labor catalog**: the shop has no dedicated catalog endpoint. The catalog is the `labor` list in the shop database, served only by `GET /api/db`, which needs a shop login. The add-on logs in with `POST /api/auth/login {"password": ...}` (gets the 12-hour `ww_session` cookie, re-logs in on 401), reads **only** the `labor` rows and discards everything else (customers/invoices are never kept, logged or shown). It never writes to `/api/db`.
- **Customers + invoice creation**: the shop's bearer-token agent endpoint `POST /api/mcp` (JSON-RPC 2.0 `tools/call`). Tools used: `search_customers` and `create_draft_invoice`. Both are draft-only by design.
- Invoice fields sent to `create_draft_invoice`: `customerId`, `kind: "invoice"`, `vehicle.unitId` (truck/unit number), `laborLines[] {code, description, hours, rate}`, `partsLines[] {sku, description, qty, price}`, `serviceDate` (MM/DD/YYYY), `notes` (PO number goes here as "PO #: ..." because the shop tool has no PO field). Tax: the shop applies its own `taxRate` (10.25 in the shop settings) to parts only, and the add-on warns if the shop's total differs from the draft total.
- The shop's `create_draft_invoice` exists only in recent Wrenchworks versions (1.1.126 has it). The shop app is never modified by this add-on.

### What you need to set (Grok Voice > Configuration)
| Option | Value |
| --- | --- |
| `wrenchworks_enabled` | `true` |
| `wrenchworks_base_url` | leave default `http://local-wrenchworks:8099` |
| `wrenchworks_password` | The password you type on the Wrenchworks login page (Wrenchworks option `access_password`). Used only to read the labor catalog. Without it a 7-code SAMPLE catalog is used and nothing can be saved. |
| `wrenchworks_mcp_token` | Wrenchworks add-on option **`mcp_token`**. If it is empty, first set a long random string there (it must differ from `access_password`) and restart Wrenchworks, then paste the same value here. Used only for customer lookup and creating draft invoices. |
| `wrenchworks_save_enabled` | `false` = dry run (Approve writes nothing). Set `true` so that tapping Approve creates the unsent draft in Wrenchworks. |

Neither secret is stored in this repo, logged, or sent to the browser.

## API research (docs.x.ai, checked 2026-09-29)
- Realtime (chosen): `wss://api.x.ai/v1/realtime?model=grok-voice-latest` - https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech
  - Models: `grok-voice-latest` (alias of `grok-voice-think-fast-2.0`), `grok-voice-think-fast-2.0`; session parameter `reasoning.effort` (`high`|`none`), sent in `session.update`.
  - Auth: API key header (server side) or ephemeral token via `POST https://api.x.ai/v1/realtime/client_secrets` (browser subprotocol `xai-client-secret.<token>`) - https://docs.x.ai/developers/model-capabilities/audio/ephemeral-tokens
  - Audio: `audio/pcm` (16-bit LE, 8-48 kHz, default 24 kHz), `audio/pcmu`, `audio/pcma`, `audio/opus`; JSON base64 or binary transport.
  - Voices: built-in IDs incl. eve (default), ara, rex, sal, leo, plus ~20 more; custom voices via `/v1/custom-voices`. REST reference: https://docs.x.ai/developers/rest-api-reference/inference/voice
  - Events used: `session.update`, `input_audio_buffer.append|commit|clear`, `conversation.item.create`, `response.create|cancel`, `response.output_audio.delta`, `response.output_audio_transcript.delta`, `conversation.item.input_audio_transcription.updated|completed` (needs `audio.input.transcription.model = grok-transcribe`), `response.function_call_arguments.done`.
  - Tool calling: send `function_call_output` for every call, then exactly one `response.create` after playback/all outputs.
- Fallback pipeline (not implemented, available): `POST /v1/stt` (`grok-voice-transcribe-2.0`), chat completions, `POST /v1/tts` (`voice_id`, mp3/wav/pcm).
- Why realtime: lowest latency, barge-in, live captions, tool calling, one connection; the docs recommend ephemeral tokens for browsers, but proxying through the add-on keeps the key server-side and allows server-enforced invoice rules.

## Security notes
- Ingress listener accepts only `172.30.32.2` (Supervisor). Direct listener needs `access_token` and checks Origin.
- The xAI key and the Wrenchworks password/token are read from `/data/options.json` and never logged or sent to the browser.
- Model-generated text is rendered with `textContent` only.
- Max 2 concurrent sessions, 30 minute session cap.

## Known limitations
See README and the report: Tesla browser support for mic/audio is recent (2026.26, AMD Ryzen cars) and unverified on the real car;
no echo cancellation guarantee; `ScriptProcessorNode` is deprecated but chosen for widest support; sessions are in-memory only.
