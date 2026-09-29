"""Wrenchworks invoice drafting: approval-gated, draft-only by default.

Trust model
  * Grok gets exactly two tools: get_labor_catalog and create_invoice_draft.
  * There is NO save tool and NO email tool. The model cannot save or send anything.
  * A draft is validated here (never trust the model's arithmetic or rates), shown to the
    human on the page, and saved only when the human taps APPROVE in that browser session.
  * Saving is additionally off unless `wrenchworks_save_enabled` is true, and refused
    when the draft has open items (TBD prices), a sample catalog, or missing credentials.
  * Nothing here ever emails a customer.

Wrenchworks integration (verified against the shop source, local add-on `wrenchworks` 1.1.126)
  * Internal URL: http://local-wrenchworks:8099 (Supervisor DNS `local-<slug>`, shop listens on 8099).
  * SAVE goes through the shop's own agent endpoint POST /api/mcp (JSON-RPC `tools/call`), which
    is bearer-token authenticated (`mcp_token` add-on option of Wrenchworks) and is DRAFTS-ONLY by
    design: tools `search_customers` and `create_draft_invoice` never email or charge anything.
  * CATALOG: the shop keeps its labor catalog in the shop database (`labor` rows) and exposes it only
    through the session-protected GET /api/db. So the catalog needs the shop password
    (POST /api/auth/login -> `ww_session` cookie). Only the `labor` rows are read; everything else in
    the response is discarded, and this module never writes to /api/db.
"""
import asyncio
import difflib
import json
import re
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

TAX_RATE = Decimal("0.1025")
NEGOTIATED_RATE = Decimal("150")
DEFAULT_RATE = Decimal("180")
NEGOTIATED_CUSTOMERS = ("recology", "charter")
HOURS_TOL = Decimal("0.011")  # line hours are rounded to 0.01 each, so allow one hundredth of drift
SAMPLE_CATALOG = Path(__file__).resolve().parent / "data" / "labor_catalog_sample.json"
CENT = Decimal("0.01")


def _d(v) -> Decimal:
    return Decimal(str(v))


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def _num(v):
    """Finite Decimal from a JSON number/numeric string, else None (bool, None, 'abc', NaN, inf -> None)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip().replace(",", ""))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None


def _show(v) -> str:
    return "nothing" if v is None or v == "" else repr(str(v)[:30])


def _as_list(v) -> list:
    """Grok sometimes sends an array as a JSON string; accept both."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def rate_for(customer: str) -> Decimal:
    c = (customer or "").lower()
    return NEGOTIATED_RATE if any(n in c for n in NEGOTIATED_CUSTOMERS) else DEFAULT_RATE


TOOLS = [
    {
        "type": "function",
        "name": "get_labor_catalog",
        "description": "Return the shop's real labor codes (code + description). Call this before building any invoice. Never use a code that is not returned here. The catalog is large: pass a short `query` (words such as 'cummins', 'diagnosis', 'DEF', 'call-out', 'road test') to search by code or description; call it several times to cover call-out, scan/diagnosis, repair, calibration and road test.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Optional search words (all must match code or description)."}}},
    },
    {
        "type": "function",
        "name": "create_invoice_draft",
        "description": (
            "Create a DRAFT invoice for Benjamin to review on screen. This does NOT save or send anything. "
            "Only call after you have asked for and received the truck/unit number, the customer, the total hours "
            "and what work was done. Split the total hours across several real labor codes from get_labor_catalog; "
            "line hours must add up exactly to total_hours. Do not compute rates or tax - the system does that. "
            "If a part price is unknown, omit unit_price (it will be marked TBD)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "unit_number": {"type": "string", "description": "Truck / unit number. Required."},
                "customer": {"type": "string"},
                "work_date": {"type": "string", "description": "YYYY-MM-DD, optional"},
                "po_number": {"type": "string", "description": "Optional; leave out rather than guess"},
                "total_hours": {"type": "number"},
                "labor_lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "Exact code from get_labor_catalog"},
                            "hours": {"type": "number"},
                            "description": {"type": "string", "description": "What was actually done on this line"},
                        },
                        "required": ["code", "hours"],
                    },
                },
                "parts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "part_number": {"type": "string"},
                            "description": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit_price": {"type": "number", "description": "Omit if unknown"},
                        },
                        "required": ["description", "quantity"],
                    },
                },
                "notes": {"type": "string"},
            },
            "required": ["unit_number", "customer", "total_hours", "labor_lines"],
        },
    },
]

INVOICE_PROMPT = """
INVOICING (Wrenchworks shop invoices). You can prepare DRAFT invoices only.
1. ALWAYS ask for the truck / unit number first, before anything else about an invoice. Never guess it.
2. Then confirm customer, work date, total hours, what was done, engine/unit type if it matters, and PO number if the customer uses POs (leave blank rather than guess).
3. Call get_labor_catalog and split the total hours across several real labor codes that follow the flow of the job (call-out, scan, diagnosis, repair, calibration or recharge, road test). Never invent a code. If a needed code does not exist, say so and suggest adding it to the catalog. Line hours must add up exactly to the total.
4. Rates and tax are applied by the system: $150/hr for Recology and Charter, $180/hr for everyone else; 10.25 percent tax on parts only. Never make up a part price; leave it out so it shows as TBD, and ask.
5. Call create_invoice_draft. It only shows a draft on screen. You cannot save or email anything. Tell Benjamin the draft is on screen and waiting for his approval, and read back the total briefly. Never say an invoice was saved or sent.
6. Never offer to email a customer unless Benjamin asks; even then you cannot do it from here.
""".strip()


DEFAULT_BASE_URL = "http://local-wrenchworks:8099"
CATALOG_MAX_UNFILTERED = 80


class WrenchworksError(Exception):
    pass


def _us_date(iso: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(2)}/{m.group(3)}/{m.group(1)}" if m else (iso or "")


class CatalogError(Exception):
    pass


# ------------------------------------------------------------------ log safety
_SECRET_KEYS = r"password|passwd|token|api[_-]?key|secret|authorization|cookie|ww_session"


def redact(text, secrets=(), limit: int = 300) -> str:
    """Make a string safe for the add-on log: known secret values, bearer tokens, xai keys,
    password/token/cookie assignments, e-mail addresses and phone numbers are masked; whitespace is
    collapsed and the text is truncated. Never pass whole customer records through here on purpose."""
    s = str(text)
    for sec in secrets:
        if sec and len(str(sec)) >= 4:
            s = s.replace(str(sec), "***")
    s = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", s)
    s = re.sub(r"\bxai-[A-Za-z0-9_\-]+", "xai-***", s)
    s = re.sub(r"(?i)\b(" + _SECRET_KEYS + r")\b(\s*[=:]\s*)[^\s,;&'\"]+", r"\1\2***", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", s)
    s = re.sub(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]?\d{4}(?!\d)", "<phone>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def describe_exc(exc: BaseException) -> str:
    """Short, specific, secret-free description of an exception (never empty)."""
    if isinstance(exc, (WrenchworksError, CatalogError)):
        return str(exc) or type(exc).__name__
    if isinstance(exc, aiohttp.ClientResponseError):
        return f"HTTP {exc.status} {exc.message}".strip()
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out waiting for Wrenchworks"
    if isinstance(exc, aiohttp.ClientConnectorError):
        return f"cannot connect to Wrenchworks ({type(exc).__name__}: {getattr(exc, 'os_error', exc)})"
    if isinstance(exc, aiohttp.ClientError):
        return f"connection problem ({type(exc).__name__})"
    msg = str(exc).strip()
    return type(exc).__name__ + (f": {msg}" if msg else "")


async def _raise_for_http(r, what: str) -> None:
    """Raise WrenchworksError('<what>: HTTP <status> (<shop error code>)') for a non-2xx reply.
    Only the shop's own short `error`/`detail`/`hint` fields are used (never the whole body)."""
    if r.status < 400:
        return
    detail = ""
    try:
        j = json.loads(await r.text())
        if isinstance(j, dict):
            parts = [str(j.get(k)) for k in ("error", "detail", "hint") if j.get(k)]
            detail = " - ".join(parts)
    except Exception:  # noqa: BLE001
        detail = ""
    raise WrenchworksError(f"{what}: HTTP {r.status}" + (f" ({detail[:140]})" if detail else ""))


_NAME_STOP = {"inc", "llc", "co", "corp", "corporation", "company", "ltd", "the"}


def _name_words(s) -> list:
    return re.findall(r"[a-z0-9]+", str(s or "").lower().replace("&", " and "))


def _name_key(s) -> str:
    return " ".join(w for w in _name_words(s) if w not in _NAME_STOP)


def _cust_label(r: dict) -> str:
    return str(r.get("name") or r.get("company") or "").strip()


def _phone_tail(r: dict) -> str:
    d = re.sub(r"\D", "", str(r.get("phone") or ""))
    return d[-4:] if len(d) >= 4 else ""


def _rank_customers(spoken: str, rows: list):
    """Rank shop customers against the spoken name using only name/company (not address/email/phone).
    -> (tier, [rows]) for the best non-empty tier: exact > prefix > all-words > fuzzy."""
    key = _name_key(spoken)
    if not key:
        return "none", []
    words = key.split()
    fields = lambda r: [k for k in (_name_key(r.get("name")), _name_key(r.get("company"))) if k]
    exact = [r for r in rows if key in fields(r)]
    if exact:
        return "exact", exact
    pre = [r for r in rows if any(f.startswith(key) or key.startswith(f) and len(f) >= 4 for f in fields(r))]
    if pre:
        return "prefix", pre
    allw = [r for r in rows if any(all(w in f.split() for w in words) for f in fields(r))]
    if allw:
        return "words", allw
    fuzzy = [r for r in rows if any(difflib.SequenceMatcher(None, key, f).ratio() >= 0.85 for f in fields(r))]
    return ("fuzzy", fuzzy) if fuzzy else ("none", [])


def _fail(reason: str, errors, log: str = "") -> dict:
    errs = [errors] if isinstance(errors, str) else list(errors)
    out = {"ok": False, "error": " | ".join(errs), "reason": reason}
    if len(errs) > 1:
        out["errors"] = errs
    out["_log"] = log or ("; ".join(errs))
    return out


def _code_index(cat: dict) -> dict:
    idx = cat.get("_idx")
    if idx is None:
        upper, loose = {}, {}
        for c in cat["codes"]:
            upper.setdefault(c.upper(), []).append(c)
            loose.setdefault(re.sub(r"[^A-Z0-9]+", "", c.upper()), []).append(c)
        idx = cat["_idx"] = {"upper": upper, "loose": loose}
    return idx


def resolve_code(cat: dict, code: str):
    """-> ("ok", canonical_code) | ("ambiguous", [codes]) | ("none", [suggestions]).
    Exact match first, then case-insensitive, then ignoring spaces/dashes/underscores."""
    codes = cat["codes"]
    code = (code or "").strip()
    if code in codes:
        return "ok", code
    idx = _code_index(cat)
    for table, key in ((idx["upper"], code.upper()), (idx["loose"], re.sub(r"[^A-Z0-9]+", "", code.upper()))):
        hit = table.get(key) if key else None
        if hit:
            return ("ok", hit[0]) if len(hit) == 1 else ("ambiguous", hit[:6])
    up = list(idx["upper"])
    loose_key = re.sub(r"[^A-Z0-9]+", "", code.upper())
    sugg = [u for u in up if loose_key and (loose_key in re.sub(r"[^A-Z0-9]+", "", u) )][:5]
    for m in difflib.get_close_matches(code.upper(), up, n=5, cutoff=0.5):
        if m not in sugg:
            sugg.append(m)
    return "none", [idx["upper"][u][0] for u in sugg[:5]]


class Invoicing:
    def __init__(self, opts: dict):
        self.enabled = bool(opts.get("wrenchworks_enabled"))
        self.base = (opts.get("wrenchworks_base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.mcp_token = opts.get("wrenchworks_mcp_token") or ""
        self.password = opts.get("wrenchworks_password") or ""
        self.save_enabled = bool(opts.get("wrenchworks_save_enabled"))
        self._catalog = None
        self._catalog_at = 0.0
        self._cookie = ""

    def secret_values(self) -> list:
        """Values that must never reach the log (passwords, tokens, the live session cookie)."""
        vals = [self.password, self.mcp_token, self._cookie, self._cookie.split("=", 1)[-1] if self._cookie else ""]
        return [v for v in vals if v]

    @property
    def catalog_configured(self) -> bool:
        """Real catalog available (needs the shop password)."""
        return bool(self.base and self.password)

    @property
    def can_save(self) -> bool:
        """Saving possible at all (needs the Wrenchworks mcp_token and a real catalog)."""
        return bool(self.base and self.mcp_token and self.catalog_configured)

    @property
    def configured(self) -> bool:  # kept for server.py / older callers
        return self.can_save

    # ---------------------------------------------------------------- catalog
    async def catalog(self, force: bool = False) -> dict:
        """-> {"codes": {CODE: description}, "source": "wrenchworks"|"sample", "default_rate": Decimal}"""
        if self._catalog and not force and time.time() - self._catalog_at < 300:
            return self._catalog
        if self.catalog_configured:
            try:
                cat = await self.fetch_catalog()
                self._catalog, self._catalog_at = cat, time.time()
                return cat
            except Exception as exc:  # noqa: BLE001
                raise CatalogError(f"Could not load the labor catalog from Wrenchworks: {describe_exc(exc)}. Not building an invoice without the real catalog. Tell Benjamin about this problem; retrying immediately will not help unless it was a timeout.")
        raw = json.loads(SAMPLE_CATALOG.read_text())
        cat = {"codes": {c["code"]: c["description"] for c in raw["codes"]}, "source": "sample", "default_rate": _d(raw["default_rate"])}
        self._catalog, self._catalog_at = cat, time.time()
        return cat

    async def _login(self, s: aiohttp.ClientSession) -> None:
        """POST /api/auth/login {password}; the shop replies with an HttpOnly `ww_session` cookie (12 h).
        The shop's CSRF check needs an Origin equal to the URL we call, so send exactly that."""
        p = urlparse(self.base)
        origin = f"{p.scheme}://{p.netloc}"
        async with s.post(self.base + "/api/auth/login", json={"password": self.password},
                          headers={"Origin": origin, "Accept": "application/json"}) as r:
            if r.status == 401:
                raise WrenchworksError("shop login: HTTP 401, shop password rejected (check wrenchworks_password)")
            if r.status == 503:
                raise WrenchworksError("shop login: HTTP 503, login is not configured on the shop (access_password empty)")
            if r.status == 403:
                raise WrenchworksError("shop login: HTTP 403 csrf_rejected (the shop refused the Origin header; check wrenchworks_base_url)")
            await _raise_for_http(r, "shop login")
            ck = r.cookies.get("ww_session")
            if not ck or not ck.value:
                raise WrenchworksError("shop login: HTTP %d but no ww_session cookie was returned" % r.status)
            self._cookie = "ww_session=" + ck.value

    async def _get_db_json(self) -> dict:
        p = urlparse(self.base)
        origin = f"{p.scheme}://{p.netloc}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
            for attempt in (0, 1):
                if not self._cookie:
                    await self._login(s)
                async with s.get(self.base + "/api/db", headers={"Cookie": self._cookie, "Accept": "application/json", "Origin": origin}) as r:
                    if r.status == 401 and attempt == 0:
                        self._cookie = ""  # expired: log in once more
                        continue
                    await _raise_for_http(r, "GET /api/db")
                    try:
                        return await r.json(content_type=None)
                    except ValueError:
                        raise WrenchworksError("GET /api/db: HTTP %d but the reply was not JSON" % r.status)
        raise WrenchworksError("GET /api/db: not authorized after re-login (HTTP 401)")

    async def fetch_catalog(self) -> dict:
        """GET /api/db (session cookie) -> keep ONLY the `labor` rows: {code, name, hours, rate}."""
        data = await self._get_db_json()
        rows = data.get("labor") if isinstance(data, dict) else None
        del data  # drop customers/invoices/etc. immediately
        codes: dict = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            if code and code not in codes:
                codes[code] = str(row.get("name") or row.get("desc") or row.get("description") or "")
        if not codes:
            raise WrenchworksError("GET /api/db worked but the shop has no labor rows with a `code` (labor catalog is empty)")
        return {"codes": codes, "source": "wrenchworks", "default_rate": DEFAULT_RATE}

    async def tool_get_labor_catalog(self, args: dict) -> dict:
        cat = await self.catalog()
        items = [{"code": k, "description": v} for k, v in cat["codes"].items()]
        q = str((args or {}).get("query") or "").lower().split()
        if q:
            items = [i for i in items if all(w in (i["code"] + " " + i["description"]).lower() for w in q)]
        total = len(items)
        out = {"ok": True, "source": cat["source"], "total_matches": total}
        if q and total == 0:
            out["hint"] = "No code matches all of those words. Try fewer or shorter words (one keyword), or call without a query."
        if not q and total > CATALOG_MAX_UNFILTERED:
            items = items[:CATALOG_MAX_UNFILTERED]
            out["note"] = f"Catalog has {len(cat['codes'])} codes; showing the first {CATALOG_MAX_UNFILTERED}. Call again with a `query` (e.g. 'cummins', 'diagnosis', 'DEF') to find the right codes."
        out["codes"] = items
        return out

    # -------------------------------------------------------------- shop API
    async def _mcp_call(self, name: str, arguments: dict) -> dict:
        """One JSON-RPC tools/call to the shop's /api/mcp (bearer mcp_token). Returns the parsed tool payload."""
        if not self.mcp_token:
            raise WrenchworksError("wrenchworks_mcp_token is not set")
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        what = f"shop /api/mcp {name}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
            async with s.post(self.base + "/api/mcp", json=body,
                              headers={"Authorization": f"Bearer {self.mcp_token}", "Accept": "application/json, text/event-stream"}) as r:
                if r.status == 401:
                    raise WrenchworksError(f"{what}: HTTP 401, mcp_token rejected (wrenchworks_mcp_token must equal the Wrenchworks mcp_token option)")
                if r.status == 503:
                    raise WrenchworksError(f"{what}: HTTP 503, mcp_token not configured on the shop or equal to access_password (set a distinct mcp_token in Wrenchworks and restart it)")
                if r.status == 429:
                    raise WrenchworksError(f"{what}: HTTP 429, rate limited by the shop")
                await _raise_for_http(r, what)
                try:
                    msg = await r.json(content_type=None)
                except ValueError:
                    raise WrenchworksError(f"{what}: HTTP {r.status} but the reply was not JSON")
        if not isinstance(msg, dict):
            raise WrenchworksError(f"{what}: unexpected reply shape ({type(msg).__name__})")
        if msg.get("error"):
            e = msg["error"]
            code = e.get("code", "") if isinstance(e, dict) else ""
            raise WrenchworksError(f"{what}: JSON-RPC error {code} {str(e.get('message') if isinstance(e, dict) else e)[:140]}".replace("  ", " "))
        res = msg.get("result") or {}
        text = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
        if res.get("isError"):
            raise WrenchworksError(f"{what}: tool error: {text[:160] or 'no message'}")
        try:
            out = json.loads(text)
        except ValueError:
            raise WrenchworksError(f"{what}: unreadable tool reply (not JSON)")
        if not isinstance(out, dict):
            raise WrenchworksError(f"{what}: unexpected tool reply shape ({type(out).__name__})")
        return out

    async def _search_customers(self, q: str) -> list:
        # The shop matches `q` as a lower-case substring of the WHOLE customer record JSON, limit cap is 50.
        res = await self._mcp_call("search_customers", {"q": q, "limit": 50})
        if res.get("error"):
            raise WrenchworksError(f"search_customers: {res.get('error')}")
        rows = res.get("customers")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def resolve_customer(self, name: str) -> dict:
        """-> {"status": "found", "id", "name"} | {"status": "none", "similar": [names]}
              | {"status": "ambiguous", "candidates": [names]}

        search_customers does a raw substring match over the whole record, so a spoken name can miss
        ("Acme Trucking, Inc." vs "Acme Trucking Inc") or over-match (matches in address/email). So: try the
        name, then the punctuation/suffix-free name, then its longest word; then rank locally on the customer's
        name/company only (exact > starts-with > all words > fuzzy)."""
        name = str(name or "").strip()
        m_tail = re.search(r"\(phone ending (\d{4})\)\s*$", name)
        tail = m_tail.group(1) if m_tail else ""
        if m_tail:
            name = name[: m_tail.start()].strip()
        key = _name_key(name)
        words = key.split()
        queries = []
        for cand in (name, key, max(words, key=len) if words else ""):
            if cand and cand.lower() not in [x.lower() for x in queries]:
                queries.append(cand)
        rows: list = []
        seen: set = set()
        for q in queries:
            for r in await self._search_customers(q):
                rid = str(r.get("id") or "")
                if rid and rid not in seen:
                    seen.add(rid)
                    rows.append(r)
            if rows and _rank_customers(name, rows)[1]:
                break
        tier, ranked = _rank_customers(name, rows)
        if tail and len(ranked) > 1:
            ranked = [r for r in ranked if _phone_tail(r) == tail] or ranked
        if tier == "fuzzy":
            # Sound-alike only: never pick a customer on a fuzzy match, let Grok confirm with Benjamin.
            similar = [_cust_label(r) for r in ranked[:5]]
            return {"status": "none", "similar": [x for x in similar if x]}
        if not ranked:
            similar = [_cust_label(r) for r in rows[:5]]
            return {"status": "none", "similar": [x for x in similar if x]}
        if len(ranked) == 1:
            r = ranked[0]
            return {"status": "found", "id": str(r.get("id")), "name": _cust_label(r) or name, "company": str(r.get("company") or "")}
        labels = [_cust_label(r) for r in ranked[:6]]
        if len(set(l.lower() for l in labels)) < len(labels):  # identical names: tell them apart by phone tail
            labels = [f"{_cust_label(r)} (phone ending {_phone_tail(r)})" if _phone_tail(r) else _cust_label(r) for r in ranked[:6]]
        return {"status": "ambiguous", "candidates": labels}

    # ------------------------------------------------------------------ draft
    async def tool_create_invoice_draft(self, args: dict, drafts: dict) -> dict:
        """Validate and store a draft. Returns a tool result for the model; on success the
        draft object is placed in result['_draft'] (stripped before sending to the model).
        On failure: {"ok": False, "error": <specific text for Grok>, "reason": <short code>, "_log": <redacted text for the
        add-on log, stripped by the server>}."""
        if not isinstance(args, dict):
            return _fail("invalid_arguments", "The tool arguments were not a JSON object. Call create_invoice_draft again with unit_number, customer, total_hours and labor_lines.")
        problems = []  # (reason, text for Grok, text for log)
        unit = str(args.get("unit_number") or "").strip()
        if not unit:
            return _fail("missing_unit_number", "unit_number is required. Ask Benjamin for the truck/unit number first, then call again.")
        customer = str(args.get("customer") or "").strip()
        if not customer:
            problems.append(("missing_customer", "customer is required: ask Benjamin which customer this is for.", "customer missing"))
        total = _num(args.get("total_hours"))
        if total is None or total <= 0:
            return _fail("invalid_total_hours", f"total_hours must be a positive number of hours (got {_show(args.get('total_hours'))}).")
        cat = await self.catalog()  # may raise CatalogError
        shop_customer = None
        if customer and self.can_save and cat["source"] == "wrenchworks":
            try:
                shop_customer = await self.resolve_customer(customer)
            except Exception as exc:  # noqa: BLE001
                d = describe_exc(exc)
                return _fail("customer_lookup_failed", f"Could not look up the customer in Wrenchworks: {d}. Nothing was drafted. Tell Benjamin about this problem; do not guess the customer.",
                             f"customer lookup failed: {d}")
            if shop_customer["status"] == "none":
                sim = shop_customer.get("similar") or []
                hint = (" Close names in the shop: " + ", ".join(sim) + ".") if sim else ""
                return _fail("customer_not_found", f"No customer matching '{customer}' exists in Wrenchworks.{hint} Ask Benjamin for the customer name exactly as it appears in the shop (new customers must be added in Wrenchworks first).",
                             f"customer not found for spoken name {redact(customer, limit=40)!r} ({len(sim)} similar)")
            if shop_customer["status"] == "ambiguous":
                cands = shop_customer["candidates"]
                return _fail("customer_ambiguous", f"'{customer}' matches several Wrenchworks customers: {', '.join(cands)}. Ask Benjamin which one, then use that exact name.",
                             f"customer ambiguous for spoken name {redact(customer, limit=40)!r} ({len(cands)} candidates)")
            spoken = customer + " " + shop_customer.get("company", "")
            customer = shop_customer["name"]  # exact shop spelling shown on the draft
        else:
            spoken = customer
        lines, hsum = [], Decimal(0)
        rate = rate_for(spoken + " " + customer)  # Recology/Charter rule matches either spelling
        raw_lines = _as_list(args.get("labor_lines"))
        if not raw_lines:
            problems.append(("missing_labor_lines", "labor_lines is required: an array of {code, hours, description}. Call get_labor_catalog, then split the hours over at least 2 real codes.", "labor_lines missing/empty"))
        line_problems = 0
        for i, ln in enumerate(raw_lines or []):
            if not isinstance(ln, dict):
                problems.append(("invalid_labor_line", f"labor_lines[{i}] must be an object with code and hours.", f"labor_lines[{i}] not an object")); line_problems += 1
                continue
            code_in = str(ln.get("code") or "").strip()
            kind, hit = resolve_code(cat, code_in)
            if kind == "ambiguous":
                problems.append(("code_ambiguous", f"labor code '{code_in}' matches several catalog codes ({', '.join(hit)}). Use one exact code from get_labor_catalog.", f"code {code_in!r} ambiguous ({len(hit)})")); line_problems += 1
                continue
            if kind == "none":
                sug = (" Did you mean: " + ", ".join(hit) + "?") if hit else ""
                problems.append(("code_not_in_catalog", f"labor code '{code_in}' is not in the shop catalog.{sug} Call get_labor_catalog (use a query) and use only listed codes.", f"code {code_in!r} not in catalog")); line_problems += 1
                continue
            code = hit
            h = _num(ln.get("hours"))
            if h is None:
                problems.append(("invalid_hours", f"hours for {code} must be a number (got {_show(ln.get('hours'))}).", f"hours for {code} not a number")); line_problems += 1
                continue
            if h <= 0:
                problems.append(("invalid_hours", f"hours for {code} must be positive (got {h}).", f"hours for {code} <= 0")); line_problems += 1
                continue
            h = h.quantize(CENT, rounding=ROUND_HALF_UP)  # invoices carry hours to 2 decimals
            if h <= 0:
                problems.append(("invalid_hours", f"hours for {code} round to 0.00; use at least 0.01.", f"hours for {code} round to 0")); line_problems += 1
                continue
            hsum += h
            lines.append({"code": code, "description": (str(ln.get("description") or "").strip() or cat["codes"][code] or code)[:200],
                          "hours": float(h), "rate": float(rate), "amount": float(money(h * rate))})
        if not line_problems and raw_lines:
            if len({l["code"] for l in lines}) < 2:
                problems.append(("too_few_codes", "split the hours across several (at least 2 different) real labor codes, not one big line", f"only {len({l['code'] for l in lines})} distinct code(s)"))
            if abs(hsum - total) > HOURS_TOL:
                per = ", ".join(f"{l['code']}={l['hours']:g}" for l in lines)
                problems.append(("hours_mismatch", f"labor line hours add up to {hsum} ({per}) but total_hours is {total}; they must match (difference {hsum - total:+}). Adjust the line hours so they add up to {total}.",
                                 f"hours mismatch: lines={hsum} total={total}"))
        parts, open_items = [], []
        for pi, p in enumerate(_as_list(args.get("parts"))):
            if not isinstance(p, dict):
                problems.append(("invalid_part", f"parts[{pi}] must be an object with description and quantity.", f"parts[{pi}] not an object")); continue
            desc = str(p.get("description") or "").strip()
            if not desc:
                problems.append(("invalid_part", f"parts[{pi}] needs a description.", f"parts[{pi}] no description")); continue
            qty = _num(p.get("quantity"))
            if qty is None or qty <= 0:
                problems.append(("invalid_part", f"quantity for part '{desc[:40]}' must be a positive number (got {_show(p.get('quantity'))}).", f"parts[{pi}] bad quantity")); continue
            price = p.get("unit_price")
            row = {"part_number": str(p.get("part_number") or "")[:60], "description": desc[:200],
                   "quantity": float(qty), "unit_price": None, "amount": None}
            if price is None or (isinstance(price, str) and not price.strip()):
                open_items.append(f"price TBD for part '{row['description']}'")
            else:
                pr = _num(price)
                if pr is None or pr < 0:
                    problems.append(("invalid_part", f"unit_price for part '{desc[:40]}' must be a non-negative number or omitted (got {_show(price)}).", f"parts[{pi}] bad unit_price")); continue
                row["unit_price"], row["amount"] = float(pr), float(money(qty * pr))
            parts.append(row)
        if problems:
            return _fail(problems[0][0], [p[1] for p in problems], "; ".join(p[2] for p in problems))
        labor_sub = money(sum((_d(l["amount"]) for l in lines), Decimal(0)))
        parts_sub = money(sum((_d(p["amount"]) for p in parts if p["amount"] is not None), Decimal(0)))
        tax = money(parts_sub * TAX_RATE)
        total_amt = labor_sub + parts_sub + tax
        if not str(args.get("po_number") or "").strip():
            open_items.append("PO number blank")
        draft = {
            "id": uuid.uuid4().hex[:10], "created": time.time(), "status": "pending",
            "unit_number": unit, "customer": customer, "work_date": str(args.get("work_date") or ""),
            "po_number": str(args.get("po_number") or ""), "notes": str(args.get("notes") or "")[:500],
            "total_hours": float(hsum), "rate": float(rate), "labor_lines": lines, "parts": parts,
            "labor_subtotal": float(labor_sub), "parts_subtotal": float(parts_sub),
            "tax_rate": float(TAX_RATE), "tax": float(tax), "total": float(total_amt),
            "open_items": open_items, "catalog_source": cat["source"],
            "customer_id": shop_customer["id"] if shop_customer else "",
        }

        blockers = []
        if cat["source"] != "wrenchworks":
            blockers.append("sample catalog in use (Wrenchworks not configured)")
        if any(p["unit_price"] is None for p in parts):
            blockers.append("part price TBD")
        if cat["source"] == "wrenchworks" and not self.mcp_token:
            blockers.append("wrenchworks_mcp_token is not set (needed to create invoices)")
        if not self.save_enabled:
            blockers.append("saving is disabled in add-on options (dry run)")
        draft["save_blockers"] = blockers
        drafts[draft["id"]] = draft
        while len(drafts) > 10:
            drafts.pop(next(iter(drafts)))
        summary = {k: draft[k] for k in ("id", "unit_number", "customer", "rate", "labor_subtotal", "parts_subtotal", "tax", "total", "open_items")}
        return {"ok": True, "message": "Draft is on Benjamin's screen awaiting his approval. NOT saved, NOT emailed. Do not say it was saved.", "draft": summary, "_draft": draft}

    # ------------------------------------------------------------------- save
    async def approve(self, draft: dict) -> dict:
        """Called only from a human tap in the browser. Returns {"ok": bool, "message": str}."""
        if draft["status"] != "pending":
            return {"ok": False, "message": f"Draft already {draft['status']}."}
        if draft["save_blockers"]:
            draft["status"] = "approved_not_saved"
            return {"ok": True, "saved": False, "message": "Approved, but NOT saved: " + "; ".join(draft["save_blockers"]) + "."}
        try:
            ref = await self.save_invoice(draft)
        except Exception as exc:  # noqa: BLE001
            d = describe_exc(exc)
            return {"ok": False, "message": f"Wrenchworks save failed: {redact(d, self.secret_values(), 200)}. Nothing was saved. You can tap Approve again.", "log": f"save failed: {d}"}
        draft["status"] = "saved"
        warn = (" " + draft["shop_total_mismatch"]) if draft.get("shop_total_mismatch") else ""
        return {"ok": True, "saved": True, "ref": ref, "message": f"Saved to Wrenchworks{(' as ' + ref) if ref else ''} as an unsent draft. Nothing was emailed.{warn}"}

    async def save_invoice(self, draft: dict) -> str:
        """Create the invoice in Wrenchworks as an UNSENT DRAFT via its /api/mcp `create_draft_invoice`
        tool (bearer mcp_token). That tool never emails or charges; status is always 'draft'.
        Returns the shop's invoice number (e.g. INV-1042)."""
        notes = []
        if draft["po_number"]:
            notes.append("PO #: " + draft["po_number"])  # the shop tool has no PO field
        if draft["notes"]:
            notes.append(draft["notes"])
        args = {
            "kind": "invoice",
            "vehicle": {"unitId": draft["unit_number"]},
            "laborLines": [{"code": l["code"], "description": l["description"], "hours": l["hours"], "rate": l["rate"]} for l in draft["labor_lines"]],
            "partsLines": [{"sku": p["part_number"], "description": p["description"], "qty": p["quantity"], "price": p["unit_price"]} for p in draft["parts"]],
            "notes": "\n".join(notes),
        }
        if draft.get("customer_id"):
            args["customerId"] = draft["customer_id"]
        else:
            raise WrenchworksError("no Wrenchworks customer id on this draft")
        if draft["work_date"]:
            args["serviceDate"] = _us_date(draft["work_date"])
        res = await self._mcp_call("create_draft_invoice", args)
        if res.get("error"):
            raise WrenchworksError(str(res.get("error")) + (" " + str(res.get("hint")) if res.get("hint") else ""))
        if not res.get("created"):
            raise WrenchworksError("shop did not confirm creation")
        ref = str(res.get("number") or res.get("id") or "")
        try:
            shop_total = _d(res.get("total"))
            if abs(shop_total - _d(draft["total"])) > CENT:
                draft["shop_total_mismatch"] = f"Wrenchworks total {shop_total} differs from draft total {draft['total']:.2f}; check the invoice in the shop."
        except Exception:  # noqa: BLE001
            pass
        return ref

