"""REAL end-to-end test against api.x.ai using the add-on backend.  Needs XAI_API_KEY in the environment.
    python tests/real_e2e.py
The key is only passed to the server subprocess via its environment; it is never printed or written.
Server log goes to a temp dir OUTSIDE the repo and is scanned for the key at the end.
Costs a few cents of API usage (a handful of short text turns)."""
import asyncio, json, os, subprocess, sys, tempfile, time
import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "grok_voice", "app", "server.py")
KEY = os.environ.get("XAI_API_KEY", "")
results = []

def redact(s):
    return s.replace(KEY, "<KEY>") if KEY else s

def check(name, ok, extra=""):
    results.append(bool(ok)); print(("PASS " if ok else "FAIL ") + name + (f"  [{redact(str(extra))[:200]}]" if extra else ""), flush=True)

async def collect(ws, until, timeout):
    """Collect browser-side events until predicate(event) or timeout. Returns (events, audio_bytes)."""
    evs, audio, end = [], 0, time.time() + timeout
    while time.time() < end:
        try: m = await ws.receive(timeout=max(0.1, end - time.time()))
        except asyncio.TimeoutError: break
        if m.type == aiohttp.WSMsgType.TEXT:
            evs.append(json.loads(m.data))
            if until(evs): break
        elif m.type == aiohttp.WSMsgType.BINARY: audio += len(m.data)
        else: break
    return evs, audio

def text_of(evs): return "".join(e.get("text", "") for e in evs if e["type"] == "assistant_delta")

async def raw_captions_probe():
    """Talk to xAI directly to see exactly how audio.input.transcription is answered."""
    out = {}
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("wss://api.x.ai/v1/realtime?model=grok-voice-latest", headers={"Authorization": f"Bearer {KEY}"}, heartbeat=20) as ws:
            first = json.loads((await ws.receive(timeout=15)).data); out["first_event"] = first.get("type")
            await ws.send_json({"type": "session.update", "session": {"audio": {"input": {"transcription": {"model": "grok-transcribe"}}}}})
            for _ in range(5):
                try: m = await ws.receive(timeout=8)
                except asyncio.TimeoutError: break
                ev = json.loads(m.data); out.setdefault("events", []).append(ev.get("type"))
                if ev.get("type") == "error": out["error"] = redact(json.dumps(ev.get("error"))[:300])
                if ev.get("type") == "session.updated":
                    out["transcription_in_session"] = ((ev.get("session") or {}).get("audio") or {}).get("input", {}).get("transcription"); break
    return out

async def main():
    if not KEY:
        print("XAI_API_KEY is not set in this shell (length 0) - cannot run real test."); return 2
    tmp = tempfile.mkdtemp(prefix="gv_e2e_")
    opts = os.path.join(tmp, "options.json")
    json.dump({"model": "grok-voice-latest", "voice": "eve", "reasoning_effort": "high", "system_prompt": "You are Grok, a concise voice assistant. Answer in one short sentence.",
               "wrenchworks_enabled": True}, open(opts, "w"))   # NOTE: no key in the file
    log = open(os.path.join(tmp, "server.log"), "w")
    env = dict(os.environ, OPTIONS_PATH=opts, INGRESS_PORT="18401", INGRESS_ALLOWED="127.0.0.1", LOG_LEVEL="DEBUG")
    proc = subprocess.Popen([sys.executable, SERVER], env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
    try:
        await asyncio.sleep(1.5)
        base = "http://127.0.0.1:18401"
        async with aiohttp.ClientSession() as s:
            # (1) connect + session
            async with s.ws_connect(base + "/ws?mode=ptt") as ws:
                evs, _ = await collect(ws, lambda e: any(x["type"] in ("ready", "error") for x in e), 20)
                check("(1) WS to wss://api.x.ai/v1/realtime connects and session starts (ready)", any(e["type"] == "ready" for e in evs), [e for e in evs if e["type"] == "error"])
                # captions result as seen by add-on (notice = rejected)
                more, _ = await collect(ws, lambda e: any(x["type"] in ("notice", "error") for x in e), 4)
                notice = [e for e in more + evs if e["type"] == "notice"]
                check("(4a) add-on: captions update accepted (no 'captions unavailable' notice)", not notice, notice)
                # (2) typed message
                await ws.send_json({"type": "text", "text": "Say hello in one short sentence"})
                evs, audio = await collect(ws, lambda e: any(x["type"] == "assistant_done" for x in e) or any(x["type"] == "error" for x in e), 30)
                check("(2) typed message -> transcript text", len(text_of(evs)) > 0, text_of(evs))
                check("(2) typed message -> audio bytes", audio > 0, f"{audio} bytes")
                check("(2) response completed", any(e["type"] == "assistant_done" for e in evs), [e for e in evs if e["type"] == "error"])
            # (3) invoice flow, fresh session
            async with s.ws_connect(base + "/ws?mode=ptt") as ws:
                await collect(ws, lambda e: any(x["type"] == "ready" for x in e), 20)
                await ws.send_json({"type": "text", "text": "Make an invoice for Recology"})
                evs, _ = await collect(ws, lambda e: any(x["type"] == "assistant_done" for x in e) or any(x["type"] == "error" for x in e), 40)
                t = text_of(evs).lower()
                check("(3a) Grok asks for truck/unit number first", any(w in t for w in ("truck", "unit")) and "?" in t, text_of(evs))
                check("(3a) no draft created before truck number", not any(e["type"] == "draft" for e in evs))
                await ws.send_json({"type": "text", "text": "Truck 4471, four hours total, replaced the NOx sensor after a scan and diagnosis, no PO, no parts prices yet"})
                evs2, _ = await collect(ws, lambda e: sum(1 for x in e if x["type"] == "assistant_done") >= 2 or any(x["type"] == "error" for x in e), 90)
                drafts = [e["draft"] for e in evs2 if e["type"] == "draft"]
                print("     turn2 assistant text:", redact(text_of(evs2))[:300])
                if drafts:
                    d = drafts[0]
                    check("(3b) draft rules: unit set, Recology $150, >=2 labor lines, hours sum to 4", d.get("unit_number") and d["rate"] == 150 and len(d["labor_lines"]) >= 2 and abs(sum(l["hours"] for l in d["labor_lines"]) - 4) < 0.001, f"unit={d.get('unit_number')} rate={d['rate']} lines={[(l['code'], l['hours']) for l in d['labor_lines']]}")
                    check("(3b) sample catalog draft not saveable", bool(d["save_blockers"]))
                else:
                    check("(3b) Grok produced a draft after being given details", False, "no draft event; assistant may still be asking questions")
        # (4b) raw probe
        probe = await raw_captions_probe()
        check("(4b) direct xAI: audio.input.transcription.model=grok-transcribe accepted", "error" not in probe and probe.get("transcription_in_session") is not None, json.dumps(probe))
    finally:
        proc.terminate(); proc.wait(5); log.close()
        logtxt = open(os.path.join(tmp, "server.log")).read()
        check("key does not appear in server log", KEY not in logtxt, f"log lines={logtxt.count(chr(10))}")
        print("server log tail (redacted):"); print(redact("\n".join(logtxt.splitlines()[-15:])))
        print("temp dir (outside repo):", tmp)
    print(f"\n{sum(results)}/{len(results)} passed"); return 0 if all(results) else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
