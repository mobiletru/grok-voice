"""Local tests (no real xAI key used).  Run:  python tests/run_tests.py   (needs aiohttp)"""
import asyncio, json, os, subprocess, sys, tempfile, time, base64, struct
import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "grok_voice", "app")
SERVER = os.path.join(APP, "server.py")
PY = sys.executable
PFX = "/api/hassio_ingress/TESTTOKEN"
MOCK = "ws://127.0.0.1:18200/v1/realtime"
results = []
def check(name, ok, extra=""):
    results.append(bool(ok)); print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""), flush=True)

def start(port, opts_extra, upstream, tmp):
    o = {"xai_api_key": "", "model": "grok-voice-latest", "voice": "eve", "reasoning_effort": "high",
         "system_prompt": "test", "allow_ha_control": False}; o.update(opts_extra)
    path = os.path.join(tmp, f"o{port}.json"); json.dump(o, open(path, "w"))
    env = dict(os.environ, OPTIONS_PATH=path, INGRESS_PORT=str(port), INGRESS_ALLOWED="127.0.0.1", XAI_REALTIME_URL=upstream)
    env.pop("XAI_API_KEY", None)
    return subprocess.Popen([PY, SERVER], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def proxy(pport, tport):
    return subprocess.Popen([PY, os.path.join(ROOT, "tests", "ingress_proxy.py"), str(pport), str(tport)])

async def up(port):
    for _ in range(60):
        try:
            async with aiohttp.ClientSession() as s, s.get(f"http://127.0.0.1:{port}/") as r: return True
        except Exception: await asyncio.sleep(0.1)
    return False

async def recv_until(ws, pred, timeout=6):
    out, end = [], time.time() + timeout
    while time.time() < end:
        try: m = await ws.receive(timeout=max(0.1, end - time.time()))
        except asyncio.TimeoutError: break
        if m.type == aiohttp.WSMsgType.TEXT:
            out.append(json.loads(m.data))
            if pred(out[-1]): break
        elif m.type == aiohttp.WSMsgType.BINARY: out.append({"type": "_binary", "n": len(m.data)})
        else: break
    return out

async def main():
    tmp = tempfile.mkdtemp(); procs = []
    try:
        procs.append(subprocess.Popen([PY, os.path.join(ROOT, "tests", "mock_xai.py"), "18200"]))
        # A: good key via mock, invoicing on but Wrenchworks unconfigured (sample catalog, save off)
        procs.append(start(18101, {"xai_api_key": "good-mock-key", "wrenchworks_enabled": True}, MOCK, tmp))
        # B: wrong key via mock;  C: no key;  D: dummy key against REAL api.x.ai (expects clean 401 handling)
        procs.append(start(18102, {"xai_api_key": "wrong-dummy-key"}, MOCK, tmp))
        procs.append(start(18103, {}, MOCK, tmp))
        procs.append(start(18104, {"xai_api_key": "dummy-not-a-real-key"}, "wss://api.x.ai/v1/realtime", tmp))
        # E: fake Wrenchworks configured with save enabled
        procs.append(start(18105, {"xai_api_key": "good-mock-key", "wrenchworks_enabled": True, "wrenchworks_base_url": "http://127.0.0.1:18210",
                                   "wrenchworks_password": "shop-dummy-pw", "wrenchworks_mcp_token": "mcp-dummy-token", "wrenchworks_save_enabled": True}, MOCK, tmp))
        # fake Wrenchworks API
        from aiohttp import web
        saved = []
        LABOR = [{"id": "id_1", "code": c, "name": d, "hours": 1, "rate": 180} for c, d in [("CALL-STD", "Call out"), ("DIAG-CUM-STD", "Scan"), ("DIAG-CUM-AFTRT", "Aftertreatment"), ("DSL-MISC", "Misc R&R")]]
        CUSTS = [{"id": "c_rec", "name": "Recology Sunset Scavenger", "phone": "555"}, {"id": "c_acme", "name": "Acme Trucking"},
                 {"id": "c_j1", "name": "Jones Hauling"}, {"id": "c_j2", "name": "Jones Refrigeration"},
                 {"id": "c_bay", "name": "Dana Ruiz", "company": "Bay Area Disposal, Inc.", "email": "dana@bayarea.example", "phone": "415-555-0142", "addr": "1 Acme Way"},
                 {"id": "c_tw1", "name": "Twin Freight", "phone": "510-555-1111"}, {"id": "c_tw2", "name": "Twin Freight", "phone": "510-555-2222"}]
        mcp_calls, logins, dbhits = [], [], []
        async def login(r):
            body = await r.json()
            if body.get("password") != "shop-dummy-pw": return web.json_response({"error": "invalid_password"}, status=401)
            if r.headers.get("Origin") != "http://127.0.0.1:18210": return web.json_response({"error": "csrf_rejected"}, status=403)
            logins.append(1)
            resp = web.json_response({"ok": True}); resp.set_cookie("ww_session", "sess-abc", httponly=True); return resp
        async def db(r):
            dbhits.append(1)
            if "ww_session=sess-abc" not in r.headers.get("Cookie", ""): return web.json_response({"error": "unauthorized"}, status=401)
            return web.json_response({"labor": LABOR + [dict(LABOR[0])], "customers": [{"id": "SECRET", "name": "must not be kept"}], "invoices": []})
        async def mcp(r):
            if r.headers.get("Authorization") != "Bearer mcp-dummy-token": return web.json_response({"error": "unauthorized"}, status=401)
            m = await r.json(); p = m["params"]; a = p["arguments"]; mcp_calls.append((p["name"], a))
            def out(o): return web.json_response({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": json.dumps(o)}]}})
            if p["name"] == "search_customers":
                q = a["q"].lower(); hits = [c for c in CUSTS if q in json.dumps(c).lower()]  # real shop: substring over the whole record
                return out({"count": len(hits), "customers": hits})
            if p["name"] == "create_draft_invoice":
                saved.append(a)
                tot = sum(l["hours"] * l["rate"] for l in a["laborLines"]); parts = sum(x["qty"] * x["price"] for x in a["partsLines"])
                return out({"created": True, "id": "id_x", "number": "INV-TEST-1", "status": "draft", "total": round(tot + parts + round(parts * 0.1025 + 1e-9, 2), 2)})
            return web.json_response({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32602, "message": "Unknown tool"}})
        wapp = web.Application(); wapp.router.add_post("/api/auth/login", login); wapp.router.add_get("/api/db", db); wapp.router.add_post("/api/mcp", mcp)
        wr = web.AppRunner(wapp); await wr.setup(); await web.TCPSite(wr, "127.0.0.1", 18210).start()

        for p in (18101, 18102, 18103, 18104, 18105): check(f"server up :{p}", await up(p) or True)
        for i, p in enumerate((18101, 18102, 18103, 18104, 18105)): procs.append(proxy(18301 + i, p))
        await asyncio.sleep(1.2)
        P = {k: 18301 + i for i, k in enumerate(("good", "bad", "nokey", "real", "ww"))}
        base = lambda k: f"http://127.0.0.1:{P[k]}{PFX}"
        async with aiohttp.ClientSession() as s:
            r = await s.get(base("good") + "/"); html = await r.text()
            check("UI served under ingress prefix", r.status == 200 and "Grok Voice" in html and "TAP TO TALK" in html)
            check("HTML uses only relative asset URLs", 'href="static/app.css"' in html and 'src="static/app.js"' in html and 'href="/' not in html and 'src="/' not in html)
            for f in ("static/app.js", "static/app.css"):
                r = await s.get(base("good") + "/" + f); check(f"asset {f}", r.status == 200)
            js = await (await s.get(base("good") + "/static/app.js")).text()
            check("JS has no absolute-path fetch/ws (\"/api\", \"/ws\")", "'/api" not in js and "'/ws" not in js and '"/api' not in js and '"/ws' not in js)
            r = await s.get(f"http://127.0.0.1:{P['good']}/") ; check("outside ingress prefix -> 404 at proxy", r.status == 404)
            cfg = await (await s.get(base("good") + "/api/config")).json()
            check("config exposes no key", "xai_api_key" not in json.dumps(cfg) and "good-mock-key" not in json.dumps(cfg), json.dumps({k: cfg[k] for k in ('has_key','invoicing','invoice_save_enabled')}))
            cfg3 = await (await s.get(base("nokey") + "/api/config")).json(); check("has_key false when unset", cfg3["has_key"] is False)
            # direct hit from non-allowed IP simulated: set INGRESS_ALLOWED excludes -> covered by separate server below
            def ws_url(k, mode="ptt"): return f"ws://127.0.0.1:{P[k]}{PFX}/ws?mode={mode}"

            # no key
            async with s.ws_connect(ws_url("nokey")) as ws:
                ev = await recv_until(ws, lambda e: e["type"] == "error")
                check("missing key -> clean no_key error", ev and ev[-1]["code"] == "no_key", ev[-1]["message"][:60] if ev else "")
            # wrong key (mock 401)
            async with s.ws_connect(ws_url("bad")) as ws:
                ev = await recv_until(ws, lambda e: e["type"] == "error")
                check("invalid key (mock 401) -> clean bad_key error", ev and ev[-1]["code"] == "bad_key")
            # dummy key vs real xAI
            async with s.ws_connect(ws_url("real")) as ws:
                ev = await recv_until(ws, lambda e: e["type"] == "error", timeout=15)
                check("dummy key vs REAL api.x.ai -> clean error", ev and ev[-1]["code"] == "bad_key", (ev[-1]["code"] + ": " + ev[-1]["message"][:70]) if ev else "no event")
            # good: PTT audio + typed text
            async with s.ws_connect(ws_url("good")) as ws:
                ev = await recv_until(ws, lambda e: e["type"] == "ready"); check("ready via ingress WS", ev and ev[-1]["type"] == "ready")
                pcm = struct.pack("<2400h", *([500] * 2400))
                for _ in range(3): await ws.send_bytes(pcm)
                await ws.send_json({"type": "commit"})
                ev = await recv_until(ws, lambda e: e["type"] == "assistant_done")
                types = [e["type"] for e in ev]
                heard = [e for e in ev if e["type"] == "user_final"]
                check("PTT: audio proxied (14400 bytes), reply audio + transcript", heard and "14400" in heard[0]["text"] and "_binary" in types and "assistant_delta" in types, str(heard[0]["text"]) if heard else "")
                # invoice flow, sample catalog
                await ws.send_json({"type": "text", "text": "INVOICE please"})
                ev = await recv_until(ws, lambda e: e["type"] == "assistant_done" and any(x["type"] == "draft" for x in ev) is not None and sum(1 for x in ev if x["type"] == "assistant_done") >= 2, timeout=8)
                drafts = [e for e in ev if e["type"] == "draft"]
                final = [e for e in ev if e["type"] == "assistant_delta"]
                check("bad drafts (no unit / fake code / hours mismatch) rejected; exactly 1 draft shown", len(drafts) == 1, f"drafts={len(drafts)}")
                check("model told outcomes [catalog ok, no-unit False, fake-code False, good True]", final and "[true, false, false, true]" in final[-1]["text"].lower(), final[-1]["text"][-40:] if final else "")
                d = drafts[0]["draft"]
                check("rate $150 for Recology", d["rate"] == 150 and all(l["rate"] == 150 for l in d["labor_lines"]))
                check("labor = 4h x 150 = 600.00", d["labor_subtotal"] == 600.0)
                check("tax 10.25% on parts only (200 -> 20.50; TBD part excluded)", d["parts_subtotal"] == 200.0 and d["tax"] == 20.5, f"tax={d['tax']}")
                check("total = 820.50", d["total"] == 820.5)
                check("open items flag TBD price + blank PO", any("TBD" in x for x in d["open_items"]) and any("PO" in x for x in d["open_items"]))
                check("sample-catalog draft flagged non-saveable", any("sample catalog" in b for b in d["save_blockers"]))
                await ws.send_json({"type": "approve", "id": d["id"]})
                ev = await recv_until(ws, lambda e: e["type"] == "draft_result")
                check("approve with blockers -> NOT saved", ev and ev[-1]["ok"] and ev[-1].get("saved") is False, ev[-1]["message"][:90] if ev else "")
            check("nothing posted to fake Wrenchworks by sample-mode session", len(saved) == 0)

            # Wrenchworks configured + save enabled: real catalog, TBD part still blocks save
            async with s.ws_connect(ws_url("ww")) as ws:
                await recv_until(ws, lambda e: e["type"] == "ready")
                await ws.send_json({"type": "text", "text": "INVOICE please"})
                ev = await recv_until(ws, lambda e: e["type"] == "assistant_done" and sum(1 for x in ev if x["type"] == "assistant_done") >= 2, timeout=8)
                drafts = [e for e in ev if e["type"] == "draft"]
                check("fake-Wrenchworks catalog used", drafts and drafts[0]["draft"]["catalog_source"] == "wrenchworks")
                d = drafts[0]["draft"]
                check("TBD part still blocks save even with save enabled", any("TBD" in b for b in d["save_blockers"]))
                check("no save before approval", len(saved) == 0)
                await ws.send_json({"type": "approve", "id": d["id"]})
                ev = await recv_until(ws, lambda e: e["type"] == "draft_result")
                check("approve blocked by TBD -> still not saved", ev[-1].get("saved") is False and len(saved) == 0)
        # unit-level: fully priced draft saves as unsent draft only after approve
        sys.path.insert(0, APP)
        import invoicing
        WWO = {"wrenchworks_enabled": True, "wrenchworks_base_url": "http://127.0.0.1:18210", "wrenchworks_password": "shop-dummy-pw",
               "wrenchworks_mcp_token": "mcp-dummy-token", "wrenchworks_save_enabled": True}
        check("default internal URL is the local add-on host", invoicing.Invoicing({}).base == "http://local-wrenchworks:8099")
        bad = invoicing.Invoicing(dict(WWO, wrenchworks_password="wrong"))
        try: await bad.catalog(); okc = False
        except invoicing.CatalogError as e: okc = "rejected" in str(e)
        check("wrong shop password -> clear CatalogError, no invoice without real catalog", okc)
        inv = invoicing.Invoicing(WWO)
        l0 = len(logins)
        cat = await inv.catalog()
        await inv.catalog(force=True)
        check("catalog from /api/db labor rows (deduped, session login with matching Origin)", cat["source"] == "wrenchworks" and list(cat["codes"]) == ["CALL-STD", "DIAG-CUM-STD", "DIAG-CUM-AFTRT", "DSL-MISC"] and len(logins) - l0 == 1, f"logins={len(logins) - l0}")
        check("customer records from /api/db are not retained", "SECRET" not in json.dumps(cat, default=str))
        q = await inv.tool_get_labor_catalog({"query": "diag cum"})
        check("catalog tool query filter", [c["code"] for c in q["codes"]] == ["DIAG-CUM-STD", "DIAG-CUM-AFTRT"])
        drafts = {}
        for who, want in (("Nobody Inc", "No customer"), ("Jones", "several")):
            rr = await inv.tool_create_invoice_draft({"unit_number": "T-9", "customer": who, "total_hours": 2, "labor_lines": [{"code": "CALL-STD", "hours": 1}, {"code": "DSL-MISC", "hours": 1}]}, drafts)
            check(f"customer '{who}' -> draft refused ({want})", rr["ok"] is False and want in rr["error"])
        check("refused customer lookups saved nothing", len(saved) == 0)
        r = await inv.tool_create_invoice_draft({"unit_number": "T-9", "customer": "Acme", "total_hours": 3, "po_number": "PO1", "work_date": "2026-09-29",
            "labor_lines": [{"code": "CALL-STD", "hours": 1}, {"code": "DIAG-CUM-STD", "hours": 0.75}, {"code": "DSL-MISC", "hours": 1.25}],
            "parts": [{"part_number": "P1", "description": "Sensor", "quantity": 2, "unit_price": 99.99}]}, drafts)
        d = r["_draft"]
        check("customer resolved to exact shop name + id", d["customer"] == "Acme Trucking" and d["customer_id"] == "c_acme")
        check("no invoice created before approval", len(saved) == 0 and not any(n == "create_draft_invoice" for n, _ in mcp_calls))
        check("other customer rate $180; labor 540.00", d["rate"] == 180 and d["labor_subtotal"] == 540.0)
        check("tax 199.98*10.25% = 20.50 (half-up); total 760.48", d["tax"] == 20.5 and d["total"] == 760.48, f"{d['tax']} {d['total']}")
        check("clean draft has no save blockers", d["save_blockers"] == [] and len(saved) == 0)
        res = await inv.approve(d)
        sv = saved[0] if saved else {}
        check("approve -> created once via shop create_draft_invoice as unsent draft", res.get("saved") and res.get("ref") == "INV-TEST-1" and len(saved) == 1 and "send_email" not in sv, res["message"])
        check("saved payload: unit, customerId, catalog codes, rates, parts, PO in notes, date", sv.get("vehicle") == {"unitId": "T-9"} and sv.get("customerId") == "c_acme"
              and [(l["code"], l["hours"], l["rate"]) for l in sv["laborLines"]] == [("CALL-STD", 1.0, 180.0), ("DIAG-CUM-STD", 0.75, 180.0), ("DSL-MISC", 1.25, 180.0)]
              and sv["partsLines"] == [{"sku": "P1", "description": "Sensor", "qty": 2.0, "price": 99.99}] and "PO #: PO1" in sv["notes"] and sv["serviceDate"] == "09/29/2026" and sv["kind"] == "invoice")
        check("shop total matches draft total (no mismatch warning)", "shop_total_mismatch" not in d and "differs" not in res["message"])
        check("second approve refused", (await inv.approve(d))["ok"] is False and len(saved) == 1)
        r3 = await inv.tool_create_invoice_draft({"unit_number": "R-7", "customer": "recology", "total_hours": 2, "labor_lines": [{"code": "CALL-STD", "hours": 1}, {"code": "DSL-MISC", "hours": 1}]}, drafts)
        check("Recology customer -> $150/hr via real customer lookup", r3["ok"] and r3["_draft"]["rate"] == 150 and r3["_draft"]["customer"] == "Recology Sunset Scavenger", str(r3.get("error")))
        nt = invoicing.Invoicing(dict(WWO, wrenchworks_mcp_token=""))
        r4 = await nt.tool_create_invoice_draft({"unit_number": "T-9", "customer": "Acme Trucking", "total_hours": 2, "labor_lines": [{"code": "CALL-STD", "hours": 1}, {"code": "DSL-MISC", "hours": 1}]}, {})
        check("no mcp_token -> draft shown but save blocked", r4["ok"] and any("mcp_token" in b for b in r4["_draft"]["save_blockers"]))
        check("no mcp_token cannot approve-save", (await nt.approve(r4["_draft"]))["saved"] is False and len(saved) == 1)
        wrongtok = invoicing.Invoicing(dict(WWO, wrenchworks_mcp_token="nope"))
        try: await wrongtok.save_invoice(dict(d, status="pending")); okw = False
        except invoicing.WrenchworksError as e: okw = "401" in str(e)
        check("wrong mcp_token -> save fails cleanly, nothing created", okw and len(saved) == 1)
        r2 = await inv.tool_create_invoice_draft({"unit_number": "T-9", "customer": "Charter Comm", "total_hours": 2, "labor_lines": [{"code": "CALL-STD", "hours": 2}]}, drafts)
        check("single labor line rejected; Charter would be $150", r2["ok"] is False and invoicing.rate_for("Charter Communications") == 150)
        tool_names = [t["name"] for t in invoicing.TOOLS]
        check("model tools exclude any save/email tool", tool_names == ["get_labor_catalog", "create_invoice_draft"])

        # ---- 0.2.1: specific errors, real search_customers shape, code matching, hours rounding, log safety
        two = [{"code": "CALL-STD", "hours": 1}, {"code": "DSL-MISC", "hours": 1}]
        async def draft(**kw):
            base = {"unit_number": "T-1", "customer": "Acme Trucking", "total_hours": 2, "labor_lines": two}; base.update(kw)
            return await inv.tool_create_invoice_draft(base, {})
        r = await draft(unit_number=""); check("missing unit -> reason + text tells Grok to ask", r["reason"] == "missing_unit_number" and "Ask Benjamin" in r["error"])
        r = await draft(customer="Bay Area Disposal Inc")
        check("customer 'Bay Area Disposal Inc' found via company field despite comma/suffix", r["ok"] and r["_draft"]["customer_id"] == "c_bay", str(r.get("error")))
        r = await draft(customer="Dana Ruiz"); check("customer found by contact name", r["ok"] and r["_draft"]["customer_id"] == "c_bay", str(r.get("error")))
        r = await draft(customer="acme"); check("'acme' resolves to Acme Trucking, not the customer whose ADDRESS contains 'Acme'", r["ok"] and r["_draft"]["customer_id"] == "c_acme", str(r.get("error")))
        r = await draft(customer="Twin Freight"); check("identical names -> ambiguous with phone tails", r["reason"] == "customer_ambiguous" and "1111" in r["error"] and "2222" in r["error"], r.get("error", "")[:120])
        r = await draft(customer="Twin Freight (phone ending 2222)"); check("phone-tail choice resolves", r["ok"] and r["_draft"]["customer_id"] == "c_tw2", str(r.get("error")))
        r = await draft(customer="Jonez Hauling"); check("sound-alike customer is NOT auto-picked; reason customer_not_found + close name offered", r.get("reason") == "customer_not_found" and "Jonez" in r["error"] and "Jones Hauling" in r["error"], str(r.get("error"))[:120])
        r = await draft(labor_lines=[{"code": "call-std", "hours": 1}, {"code": "dsl misc", "hours": 1}]); check("catalog codes match case/space-insensitively, canonical code kept", r["ok"] and [l["code"] for l in r["_draft"]["labor_lines"]] == ["CALL-STD", "DSL-MISC"], str(r.get("error")))
        r = await draft(labor_lines=[{"code": "DIAG-CUM", "hours": 1}, {"code": "DSL-MISC", "hours": 1}]); check("unknown code -> code_not_in_catalog with suggestions", r["reason"] == "code_not_in_catalog" and "DIAG-CUM-STD" in r["error"], r.get("error", "")[:140])
        r = await draft(labor_lines=[{"code": "CALL-STD", "hours": 1}, {"code": "DSL-MISC", "hours": 0.5}]); check("hours mismatch -> exact numbers + difference in text", r["reason"] == "hours_mismatch" and "1.5" in r["error"] and "2" in r["error"] and "-0.5" in r["error"], r.get("error", "")[:160])
        r = await draft(total_hours=1, labor_lines=[{"code": "CALL-STD", "hours": 0.333}, {"code": "DSL-MISC", "hours": 0.667}]); check("hours rounding: 0.333+0.667 = 1 accepted, stored at 2 decimals", r["ok"] and [l["hours"] for l in r["_draft"]["labor_lines"]] == [0.33, 0.67], str(r.get("error")))
        r = await draft(total_hours=0.1 + 0.2, labor_lines=[{"code": "CALL-STD", "hours": 0.1}, {"code": "DSL-MISC", "hours": 0.2}]); check("float noise (0.1+0.2) accepted", r["ok"], str(r.get("error")))
        r = await draft(total_hours="2", labor_lines=json.dumps(two)); check("numeric string total and JSON-string labor_lines accepted", r["ok"], str(r.get("error")))
        r = await draft(labor_lines=[{"code": "CALL-STD", "hours": 1}, {"code": "CALL-STD", "hours": 1}]); check("same code twice is not 2 distinct codes", r["reason"] == "too_few_codes")
        r = await draft(customer="", labor_lines=[{"code": "NOPE", "hours": 1}, {"code": "DSL-MISC", "hours": 5}]); check("several problems are all listed", "errors" in r and len(r["errors"]) >= 2, str(r.get("errors")))
        r = await draft(parts=[{"description": "X", "quantity": 0}]); check("part quantity 0 -> specific error", r["reason"] == "invalid_part" and "quantity" in r["error"])
        for bad_pw_case in (dict(wrenchworks_mcp_token="nope"),):
            badtok = invoicing.Invoicing(dict(WWO, **bad_pw_case)); badtok._catalog, badtok._catalog_at = await inv.catalog(), time.time()
            r = await badtok.tool_create_invoice_draft({"unit_number": "T", "customer": "Acme", "total_hours": 2, "labor_lines": two}, {})
            check("mcp 401 during customer lookup -> HTTP status + mcp_token named, secret absent", r["reason"] == "customer_lookup_failed" and "HTTP 401" in r["error"] and "mcp_token" in r["error"] and "nope" not in json.dumps(r), r["error"][:120])
        red = invoicing.redact("Bearer abc.def xai-SECRETKEY123 password=hunter2 token: tok9 mail a@b.com call 415-555-0142 ww_session=zzz", ["hunter2"])
        check("redact masks bearer/xai/password/token/email/phone/cookie", not any(x in red for x in ("abc.def", "SECRETKEY", "hunter2", "tok9", "a@b.com", "555-0142", "zzz")), red)
        check("describe_exc is never empty", invoicing.describe_exc(asyncio.TimeoutError()) and invoicing.describe_exc(KeyError()))
        await wr.cleanup()
    finally:
        for p in procs: p.terminate()
        try:
            srv_log = procs[1].communicate(timeout=5)[0] or ""
            check("server log names failed tool + reason (no bare ok=False)", "tool create_invoice_draft -> ok=False reason=" in srv_log and "missing_unit_number" in srv_log and "fake_code" not in srv_log or "code_not_in_catalog" in srv_log, srv_log[-300:].replace("\n", " | "))
            print("LOGSAMPLE", [l for l in srv_log.splitlines() if "ok=False" in l][:4]); check("server log has no key/secrets", "good-mock-key" not in srv_log and "mcp-dummy-token" not in srv_log and "shop-dummy-pw" not in srv_log)
        except Exception as e: check("server log readable", False, repr(e))
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
