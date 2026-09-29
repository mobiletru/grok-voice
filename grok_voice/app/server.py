#!/usr/bin/env python3
"""Grok Voice - Home Assistant app backend.

Serves a single-page tap-to-talk UI and proxies one browser WebSocket to the
xAI Voice Agent realtime API (wss://api.x.ai/v1/realtime). The xAI API key
lives only in this process; the browser never sees it.

Browser <-> this server protocol (all URLs relative to the ingress prefix):
  GET  ./                 UI
  GET  ./api/config       non-secret settings (JSON)
  GET  ./ws?mode=ptt|vad  WebSocket
     browser -> server:  binary  = PCM16LE mono 24 kHz microphone audio
                         text    = {"type":"text","text":"..."}   typed message
                                   {"type":"commit"}              end of push-to-talk turn
                                   {"type":"clear"}               drop uncommitted audio
                                   {"type":"interrupt"}           cancel current answer
     server -> browser:  binary  = PCM16LE mono 24 kHz assistant audio
                         text    = {"type": ready|thinking|speech_started|speech_stopped|
                                    user_partial|user_final|assistant_delta|assistant_done|
                                    notice|error, ...}
"""
import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from invoicing import INVOICE_PROMPT, TOOLS, CatalogError, Invoicing, describe_exc, redact

LOG = logging.getLogger("grok_voice")

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
XAI_REALTIME_URL = os.environ.get("XAI_REALTIME_URL", "wss://api.x.ai/v1/realtime")
INGRESS_PORT = int(os.environ.get("INGRESS_PORT", "8099"))
DIRECT_PORT = int(os.environ.get("DIRECT_PORT", "8100"))
# Only the Supervisor ingress proxy may talk to the ingress listener.
INGRESS_ALLOWED = [
    ipaddress.ip_network(x.strip())
    for x in os.environ.get("INGRESS_ALLOWED", "172.30.32.2").split(",")
    if x.strip()
]
MAX_SESSION_SECONDS = int(os.environ.get("MAX_SESSION_SECONDS", "1800"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
MAX_TEXT_CHARS = 2000
SAMPLE_RATE = 24000
VERSION = os.environ.get("BUILD_VERSION", "dev")


def load_options() -> dict:
    opts: dict = {}
    if OPTIONS_PATH.is_file():
        try:
            opts = json.loads(OPTIONS_PATH.read_text())
        except Exception as exc:  # noqa: BLE001
            LOG.error("Could not read %s: %s", OPTIONS_PATH, exc)
    key = (opts.get("xai_api_key") or os.environ.get("XAI_API_KEY") or "").strip()
    return {
        "xai_api_key": key,
        "model": (opts.get("model") or "grok-voice-latest").strip(),
        "voice": (opts.get("voice") or "eve").strip().lower(),
        "reasoning_effort": opts.get("reasoning_effort") or "high",
        "system_prompt": opts.get("system_prompt") or "You are a concise, spoken-friendly voice assistant.",
        "allow_ha_control": bool(opts.get("allow_ha_control", False)),
        "access_token": (opts.get("access_token") or "").strip(),
        "wrenchworks_enabled": bool(opts.get("wrenchworks_enabled", False)),
        "wrenchworks_base_url": (opts.get("wrenchworks_base_url") or "http://local-wrenchworks:8099").strip(),
        "wrenchworks_password": (opts.get("wrenchworks_password") or "").strip(),
        "wrenchworks_mcp_token": (opts.get("wrenchworks_mcp_token") or "").strip(),
        "wrenchworks_save_enabled": bool(opts.get("wrenchworks_save_enabled", False)),
    }


OPTS = load_options()
INV = Invoicing(OPTS)
ACTIVE = {"n": 0}


# ----------------------------------------------------------------- middleware
@web.middleware
async def security_headers(request, handler):
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@web.middleware
async def ingress_guard(request, handler):
    """Ingress listener: accept only the Supervisor proxy."""
    try:
        ip = ipaddress.ip_address(request.remote or "")
    except ValueError:
        raise web.HTTPForbidden(text="forbidden")
    if not any(ip in net for net in INGRESS_ALLOWED):
        raise web.HTTPForbidden(text="forbidden")
    return await handler(request)


def _is_https(request) -> bool:
    return request.headers.get("X-Forwarded-Proto", request.scheme) == "https"


@web.middleware
async def token_guard(request, handler):
    """Direct listener: require access_token (cookie, header or ?token=)."""
    expected = OPTIONS_TOKEN()
    supplied = (
        request.cookies.get("gv_token")
        or request.headers.get("X-Access-Token")
        or request.query.get("token")
        or ""
    )
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise web.HTTPUnauthorized(
            text="Unauthorized. Open this page once with ?token=YOUR_ACCESS_TOKEN.",
        )
    # Same-site check so another website cannot drive the WebSocket with our cookie.
    origin = request.headers.get("Origin")
    if origin:
        host = request.headers.get("X-Forwarded-Host") or request.host
        if origin.split("://", 1)[-1] != host:
            raise web.HTTPForbidden(text="bad origin")
    resp = await handler(request)
    if request.query.get("token") and not request.cookies.get("gv_token"):
        resp.set_cookie(
            "gv_token", expected, max_age=60 * 60 * 24 * 90, httponly=True,
            secure=_is_https(request), samesite="Strict", path="/",
        )
    return resp


def OPTIONS_TOKEN() -> str:
    return OPTS["access_token"]


# ------------------------------------------------------------------- handlers
async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html")


async def api_config(request: web.Request) -> web.Response:
    return web.json_response({
        "version": VERSION,
        "has_key": bool(OPTS["xai_api_key"]),
        "model": OPTS["model"],
        "voice": OPTS["voice"],
        "allow_ha_control": OPTS["allow_ha_control"],
        "invoicing": OPTS["wrenchworks_enabled"],
        "invoice_save_enabled": bool(INV.can_save and OPTS["wrenchworks_save_enabled"]),
        "ingress_path": request.headers.get("X-Ingress-Path", ""),
        "sample_rate": SAMPLE_RATE,
    })


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "has_key": bool(OPTS["xai_api_key"])})


def session_update(mode: str) -> dict:
    instructions = OPTS["system_prompt"]
    if OPTS["wrenchworks_enabled"]:
        instructions += "\n\n" + INVOICE_PROMPT
    sess = {
        "type": "session.update",
        "session": {
            "voice": OPTS["voice"],
            "instructions": instructions,
            "turn_detection": {"type": "server_vad"} if mode == "vad" else {"type": None},
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
                "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
            },
        },
    }
    if OPTS["reasoning_effort"] in ("high", "none"):
        sess["session"]["reasoning"] = {"effort": OPTS["reasoning_effort"]}
    if OPTS["wrenchworks_enabled"]:
        sess["session"]["tools"] = TOOLS
    return sess


def transcription_update() -> dict:
    # Separate message so that a rejection only costs live captions, not the session.
    return {
        "type": "session.update",
        "session": {"audio": {"input": {"transcription": {"model": "grok-transcribe"}}}},
    }


async def _send_json(ws: web.WebSocketResponse, obj: dict) -> None:
    if not ws.closed:
        await ws.send_str(json.dumps(obj))


async def _fail(ws: web.WebSocketResponse, message: str, code: str = "error") -> web.WebSocketResponse:
    await _send_json(ws, {"type": "error", "code": code, "message": message})
    await ws.close(code=aiohttp.WSCloseCode.OK, message=b"error")
    return ws


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)

    if not OPTS["xai_api_key"]:
        return await _fail(ws, "No xAI API key is configured. Add it in the Grok Voice app Configuration tab, then restart the app.", "no_key")
    if ACTIVE["n"] >= MAX_CONCURRENT:
        return await _fail(ws, "Too many active voice sessions. Close the other Grok Voice page first.", "busy")

    mode = "vad" if request.query.get("mode") == "vad" else "ptt"
    # Only `model` goes in the URL; reasoning effort is a session parameter (docs: Session Parameters).
    params = {"model": OPTS["model"]}

    ACTIVE["n"] += 1
    started = time.monotonic()
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=15))
    upstream = None
    try:
        try:
            upstream = await session.ws_connect(
                XAI_REALTIME_URL,
                params=params,
                headers={"Authorization": f"Bearer {OPTS['xai_api_key']}"},
                heartbeat=20,
                max_msg_size=8 << 20,
            )
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in (400, 401, 403):
                # Verified against the live API: an invalid key returns HTTP 400 with
                # "Incorrect API key provided" (not 401), so 400 is reported as a key problem too.
                msg = f"xAI rejected the connection (HTTP {exc.status}) - most likely an invalid xai_api_key; also check the model option."
                code = "bad_key"
            elif exc.status == 429:
                msg, code = "xAI rate limit or concurrent-session limit reached (HTTP 429). Try again shortly.", "rate_limited"
            else:
                msg, code = f"xAI refused the connection (HTTP {exc.status}). Check the model option.", "upstream"
            LOG.warning("upstream handshake failed: HTTP %s", exc.status)
            return await _fail(ws, msg, code)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            LOG.warning("upstream connect error: %s", type(exc).__name__)
            return await _fail(ws, "Could not reach api.x.ai (network/DNS problem on the Home Assistant host).", "network")

        await upstream.send_json(session_update(mode))
        # stage 0: waiting for first session.updated; 1: optional captions update in flight; 2: done
        state = {"ready": False, "stage": 0, "upstream": upstream, "drafts": {}, "tasks": set(),
                 "pending": 0, "resp_done": True, "owe": False}

        async def up_to_browser():
            async for msg in upstream:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        ev = json.loads(msg.data)
                    except ValueError:
                        continue
                    await handle_upstream_event(ev, ws, state)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            await _send_json(ws, {"type": "error", "code": "upstream_closed", "message": "The xAI session ended. Tap to start again."})

        async def browser_to_up():
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if state["ready"]:
                        await upstream.send_json({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(msg.data).decode("ascii"),
                        })
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                    except ValueError:
                        continue
                    await handle_browser_command(cmd, upstream, ws, state)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break

        async def watchdog():
            await asyncio.sleep(MAX_SESSION_SECONDS)
            await _send_json(ws, {"type": "error", "code": "max_duration", "message": "Session time limit reached. Tap to start a new one."})

        tasks = [asyncio.create_task(t()) for t in (up_to_browser, browser_to_up, watchdog)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        ACTIVE["n"] -= 1
        if upstream is not None and not upstream.closed:
            await upstream.close()
        await session.close()
        if not ws.closed:
            await ws.close()
        LOG.info("session ended after %.0fs", time.monotonic() - started)
    return ws


async def handle_upstream_event(ev: dict, ws: web.WebSocketResponse, state: dict) -> None:
    t = ev.get("type", "")
    if t == "session.updated":
        if state["stage"] == 0:
            state["ready"] = True
            state["stage"] = 1
            await _send_json(ws, {"type": "ready"})
            # Optional live captions of the user's speech; failure is non-fatal.
            await state["upstream"].send_json(transcription_update())
        else:
            state["stage"] = 2
    elif t in ("response.output_audio.delta", "response.audio.delta"):
        b64 = ev.get("delta") or ev.get("audio")
        if b64:
            await ws.send_bytes(base64.b64decode(b64))
    elif t == "response.output_audio_transcript.delta":
        await _send_json(ws, {"type": "assistant_delta", "text": ev.get("delta", "")})
    elif t == "response.created":
        state["resp_done"] = False
        await _send_json(ws, {"type": "thinking"})
    elif t == "response.function_call_arguments.done":
        state["pending"] += 1
        task = asyncio.create_task(run_tool_call(ev, ws, state))
        state["tasks"].add(task)
        task.add_done_callback(state["tasks"].discard)
    elif t == "response.done":
        state["resp_done"] = True
        await _send_json(ws, {"type": "assistant_done"})
        await maybe_respond(state)
    elif t == "input_audio_buffer.speech_started":
        await _send_json(ws, {"type": "speech_started"})
    elif t == "input_audio_buffer.speech_stopped":
        await _send_json(ws, {"type": "speech_stopped"})
    elif t == "conversation.item.input_audio_transcription.updated":
        await _send_json(ws, {"type": "user_partial", "text": ev.get("transcript", "")})
    elif t == "conversation.item.input_audio_transcription.completed":
        await _send_json(ws, {"type": "user_final", "text": ev.get("transcript", "")})
    elif t == "error":
        err = ev.get("error") or {}
        message = err.get("message") if isinstance(err, dict) else str(err)
        LOG.warning("upstream error event: %s", (message or "")[:200])
        if state["stage"] == 1:
            # Error while the optional captions update was in flight: not fatal.
            state["stage"] = 2
            await _send_json(ws, {"type": "notice", "message": "Live captions of your speech may be unavailable."})
            return
        await _send_json(ws, {"type": "error", "code": "upstream_event", "message": message or "xAI reported an error."})


async def maybe_respond(state: dict) -> None:
    """Exactly one response.create, only after ALL tool outputs were sent and the
    current response finished (xAI docs: parallel tool calling / audio overlap)."""
    if state["owe"] and state["pending"] == 0 and state["resp_done"]:
        state["owe"] = False
        state["resp_done"] = False
        await state["upstream"].send_json({"type": "response.create"})


def _log_secrets() -> list:
    return [OPTS.get("xai_api_key"), OPTS.get("access_token"), *INV.secret_values()]


async def run_tool_call(ev: dict, ws: web.WebSocketResponse, state: dict) -> None:
    name, call_id = ev.get("name"), ev.get("call_id")
    log_reason = ""
    args, args_error = {}, ""
    try:
        args = json.loads(ev.get("arguments") or "{}")
        if not isinstance(args, dict):
            args, args_error = {}, "the arguments were JSON but not an object"
    except ValueError:
        args_error = "the arguments were not valid JSON"
    try:
        if not OPTS["wrenchworks_enabled"]:
            result = {"ok": False, "reason": "invoicing_disabled", "error": "Invoicing is disabled in the add-on options. Tell Benjamin."}
        elif args_error:
            result = {"ok": False, "reason": "bad_arguments", "error": f"{args_error}. Call {name} again with a valid JSON object of arguments."}
        elif name == "get_labor_catalog":
            result = await INV.tool_get_labor_catalog(args)
        elif name == "create_invoice_draft":
            result = await INV.tool_create_invoice_draft(args, state["drafts"])
            draft = result.pop("_draft", None)
            if draft:
                await _send_json(ws, {"type": "draft", "draft": draft})
        else:
            result = {"ok": False, "reason": "unknown_tool", "error": f"Unknown tool {name}. Only get_labor_catalog and create_invoice_draft exist."}
    except CatalogError as exc:
        result = {"ok": False, "reason": "catalog_unavailable", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        detail = describe_exc(exc)
        log_reason = f"unexpected {detail}"
        LOG.warning("tool %s crashed: %s", name, redact(detail, _log_secrets(), 400))
        result = {"ok": False, "reason": "internal_error", "error": f"The tool crashed internally ({redact(detail, _log_secrets(), 160)}). Nothing was drafted. Tell Benjamin something went wrong; you may retry once."}
    log_reason = result.pop("_log", "") or log_reason
    ok = result.get("ok", True)
    if ok:
        LOG.info("tool %s -> ok=True", name)
    else:
        if not log_reason:
            log_reason = str(result.get("error") or "no reason given")
        LOG.warning("tool %s -> ok=False reason=%s: %s", name, result.get("reason", "unspecified"), redact(log_reason, _log_secrets(), 400))
    await state["upstream"].send_json({
        "type": "conversation.item.create",
        "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps(result)},
    })
    state["pending"] -= 1
    state["owe"] = True
    await maybe_respond(state)


async def handle_approval(cmd: dict, ws: web.WebSocketResponse, state: dict) -> None:
    """Human-only actions, reachable solely from the browser buttons (never from the model)."""
    draft = state["drafts"].get(str(cmd.get("id")))
    if not draft:
        await _send_json(ws, {"type": "draft_result", "id": cmd.get("id"), "ok": False, "message": "Draft not found (it may have expired)."})
        return
    if cmd["type"] == "reject":
        draft["status"] = "rejected"
        await _send_json(ws, {"type": "draft_result", "id": draft["id"], "ok": True, "status": "rejected", "message": "Draft discarded. Nothing saved."})
        return
    res = await INV.approve(draft)
    if not res.get("ok"):
        LOG.warning("approve draft %s failed: %s", draft.get("id"), redact(res.get("log") or res.get("message"), _log_secrets(), 400))
    res.pop("log", None)
    await _send_json(ws, {"type": "draft_result", "id": draft["id"], "status": draft["status"], **res})


async def handle_browser_command(cmd: dict, upstream, ws=None, state=None) -> None:
    t = cmd.get("type")
    if t in ("approve", "reject") and ws is not None:
        await handle_approval(cmd, ws, state)
    elif t == "text":
        text = str(cmd.get("text", "")).strip()[:MAX_TEXT_CHARS]
        if not text:
            return
        await upstream.send_json({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
        })
        await upstream.send_json({"type": "response.create"})
    elif t == "commit":
        await upstream.send_json({"type": "input_audio_buffer.commit"})
        await upstream.send_json({"type": "response.create"})
    elif t == "clear":
        await upstream.send_json({"type": "input_audio_buffer.clear"})
    elif t == "interrupt":
        await upstream.send_json({"type": "response.cancel"})


# ------------------------------------------------------------------------ app
def build_app(kind: str) -> web.Application:
    mws = [security_headers, ingress_guard if kind == "ingress" else token_guard]
    app = web.Application(middlewares=mws)
    app.router.add_get("/", index)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/health", health)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", STATIC, follow_symlinks=False)
    return app


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    runners = []
    ingress_runner = web.AppRunner(build_app("ingress"), access_log=None)
    await ingress_runner.setup()
    await web.TCPSite(ingress_runner, "0.0.0.0", INGRESS_PORT).start()
    runners.append(ingress_runner)
    LOG.info("Ingress listener on :%d (allowed: %s)", INGRESS_PORT, ", ".join(map(str, INGRESS_ALLOWED)))
    if OPTS["access_token"]:
        direct_runner = web.AppRunner(build_app("direct"), access_log=None)
        await direct_runner.setup()
        await web.TCPSite(direct_runner, "0.0.0.0", DIRECT_PORT).start()
        runners.append(direct_runner)
        LOG.info("Direct token-protected listener on :%d", DIRECT_PORT)
    if not OPTS["xai_api_key"]:
        LOG.warning("No xAI API key configured.")
    LOG.info("model=%s voice=%s reasoning=%s", OPTS["model"], OPTS["voice"], OPTS["reasoning_effort"])
    try:
        await asyncio.Event().wait()
    finally:
        for r in runners:
            await r.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
