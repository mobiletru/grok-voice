"""Minimal fake of wss://api.x.ai/v1/realtime for offline protocol tests (no real key needed).
Accepts only 'Bearer good-mock-key'. Typed text containing 'INVOICE' triggers a scripted tool-call
sequence (catalog, then a bad draft, then a good draft) to exercise validation + approval gating."""
import base64, json, struct, sys
from aiohttp import web

async def rt(request):
    if request.headers.get("Authorization", "") != "Bearer good-mock-key":
        return web.Response(status=401, text="bad key")
    ws = web.WebSocketResponse(); await ws.prepare(request)
    await ws.send_json({"type": "session.created"})
    buf, step, creates = 0, 0, 0
    outputs = []
    async def call(name, args, cid):
        await ws.send_json({"type": "response.function_call_arguments.done", "name": name, "call_id": cid, "arguments": json.dumps(args)})
    async for m in ws:
        ev = json.loads(m.data); t = ev["type"]
        if t == "session.update":
            s = ev["session"]
            if "instructions" in s:
                await ws.send_json({"type": "_mock_seen", "tools": [x["name"] for x in s.get("tools", [])], "has_invoice_prompt": "INVOICING" in s["instructions"]}) if False else None
            await ws.send_json({"type": "session.updated", "session": {}})
        elif t == "input_audio_buffer.append":
            buf += len(base64.b64decode(ev["audio"]))
        elif t == "input_audio_buffer.commit":
            await ws.send_json({"type": "conversation.item.input_audio_transcription.completed", "transcript": f"[mock heard {buf} bytes]"})
        elif t == "conversation.item.create":
            item = ev["item"]
            if item["type"] == "function_call_output":
                outputs.append(json.loads(item["output"]))
            else:
                creates += 1
                txt = item["content"][0]["text"]
                if "INVOICE" in txt: step = 1
        elif t == "response.create":
            await ws.send_json({"type": "response.created"})
            if step == 1:
                step = 2
                await call("get_labor_catalog", {}, "c1")
                await call("create_invoice_draft", {"unit_number": "", "customer": "Recology", "total_hours": 4, "labor_lines": []}, "c2")
                await ws.send_json({"type": "response.done"})
            elif step == 2:
                step = 3   # after tool outputs: try invalid code + wrong hours, then a valid one
                await call("create_invoice_draft", {"unit_number": "R-1234", "customer": "Recology", "total_hours": 4,
                    "labor_lines": [{"code": "MADE-UP", "hours": 4}]}, "c3")
                await call("create_invoice_draft", {"unit_number": "R-1234", "customer": "Recology", "total_hours": 4, "po_number": "",
                    "labor_lines": [{"code": "CALL-STD", "hours": 1}, {"code": "DIAG-CUM-STD", "hours": 0.5}, {"code": "DIAG-CUM-AFTRT", "hours": 1.5}, {"code": "DSL-MISC", "hours": 1}],
                    "parts": [{"part_number": "A123", "description": "NOx sensor", "quantity": 1, "unit_price": 200}, {"description": "Gasket", "quantity": 2}]}, "c4")
                await ws.send_json({"type": "response.done"})
            elif step == 3:
                step = 0
                await ws.send_json({"type": "response.output_audio_transcript.delta", "delta": "Draft is on screen. " + json.dumps([o.get("ok") for o in outputs])})
                await ws.send_json({"type": "response.done"})
            else:
                pcm = struct.pack("<240h", *([1000, -1000] * 120))
                await ws.send_json({"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()})
                await ws.send_json({"type": "response.output_audio_transcript.delta", "delta": "Hello from the mock."})
                await ws.send_json({"type": "response.done"})
    return ws
app = web.Application(); app.router.add_get("/v1/realtime", rt)
web.run_app(app, host="127.0.0.1", port=int(sys.argv[1]), print=None)
