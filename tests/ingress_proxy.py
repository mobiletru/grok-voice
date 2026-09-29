"""Simulates the HA Supervisor ingress proxy: strips /api/hassio_ingress/<token> and sets X-Ingress-Path."""
import sys, aiohttp
from aiohttp import web
PREFIX = "/api/hassio_ingress/TESTTOKEN"
TARGET = f"http://127.0.0.1:{sys.argv[2]}"
async def proxy(request):
    if not request.path.startswith(PREFIX):
        return web.Response(status=404, text="outside ingress prefix")
    path = request.path[len(PREFIX):] or "/"
    headers = {"X-Ingress-Path": PREFIX, "X-Remote-User-Name": "tester"}
    async with aiohttp.ClientSession() as s:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse(); await ws.prepare(request)
            async with s.ws_connect(TARGET + path, params=request.query, headers=headers) as up:
                import asyncio
                async def a():
                    async for m in ws:
                        if m.type == aiohttp.WSMsgType.TEXT: await up.send_str(m.data)
                        elif m.type == aiohttp.WSMsgType.BINARY: await up.send_bytes(m.data)
                async def b():
                    async for m in up:
                        if m.type == aiohttp.WSMsgType.TEXT: await ws.send_str(m.data)
                        elif m.type == aiohttp.WSMsgType.BINARY: await ws.send_bytes(m.data)
                t = [asyncio.create_task(a()), asyncio.create_task(b())]
                await asyncio.wait(t, return_when=asyncio.FIRST_COMPLETED)
                for x in t: x.cancel()
            return ws
        async with s.request(request.method, TARGET + path, params=request.query, headers=headers) as r:
            return web.Response(status=r.status, body=await r.read(), content_type=r.content_type)
app = web.Application()
app.router.add_route("*", "/{tail:.*}", proxy)
web.run_app(app, host="127.0.0.1", port=int(sys.argv[1]), print=None)
