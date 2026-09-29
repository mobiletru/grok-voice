"""Wrenchworks invoice drafting: approval-gated, saves UNSENT drafts only.

Trust model
  * Grok gets exactly two tools: get_labor_catalog and create_invoice_draft.
  * There is NO save tool and NO email tool. The model cannot save or send anything.
  * A draft is validated here (never trust the model's arithmetic or rates), shown to the
    human on the page, and saved only when the human taps APPROVE in that browser session.
  * Saving is always on, but only ever after that APPROVE tap. It is refused (with a clear on-screen
    reason) when the draft has open items (TBD prices), a sample catalog, or missing credentials.
  * On ANY problem (failed save, discard, tool error) Grok makes a brand NEW draft from scratch; a failed
    draft is never edited or re-tried, and before a save the shop is checked for an identical unsent
    draft so a second copy is never created silently.
  * Nothing here ever emails a customer.

Wrenchworks integration (verified against the shop source, local add-on `wrenchworks` 1.1.126)
  * Internal URL: http://local-wrenchworks:8099 (Supervisor DNS `local-<slug>`, shop listens on 8099).
  * SAVE goes through the shop's own agent endpoint POST /api/mcp (JSON-RPC `tools/call`), which
    is bearer-token authenticated (`mcp_token` add-on option of Wrenchworks) and is DRAFTS-ONLY by
    design: tools `search_customers` and `create_draft_invoice` never email or charge anything.
  * CATALOG (Wrenchworks >= 1.1.127): read-only bearer tool `search_labor_codes` on POST /api/mcp returns
    only {id, code, name, hours, rate} of the `labor` list. No shop password needed.
    FALLBACK (older Wrenchworks: "Unknown tool: search_labor_codes"): shop login (POST /api/auth/login ->
    `ww_session` cookie, needs the shop password) + GET /api/db, keeping only the `labor` rows and discarding
    everything else. This module never writes to /api/db.
"""
import asyncio
import difflib
import json
import logging
import re
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

LOG = logging.getLogger("grok_voice")
AI_MARKER = "Created by Grok Voice (AI) - review before sending"

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
        "description": "Search the shop's COMPLETE labor catalog (every code in Wrenchworks: code + description). Call this before building any invoice. Never use a code that is not returned here. The catalog is large: pass a short `query` (words such as 'cummins', 'diagnosis', 'DEF', 'call-out', 'road test') to search by code or description; call it several times to cover call-out, scan/diagnosis, repair, calibration and road test. Results are paged: when `has_more` is true call again with `offset` to see the rest. Without a query it lists all codes page by page.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Optional search words (all must match code or description)."},
                                                       "offset": {"type": "integer", "description": "Skip this many matches (paging)."},
                                                       "limit": {"type": "integer", "description": "Page size (default 80, max 200)."}}},
    },
    {
        "type": "function",
        "name": "get_parts_catalog",
        "description": "Search the shop's COMPLETE parts catalog (every part in Wrenchworks: part_number, description, price). Call this before putting any part on an invoice. Only parts returned here may be used, with the exact part_number; the system fills in the catalog price. Pass a short `query` (part number or words such as 'filter drier', 'TK-5071'). Results are paged: when `has_more` is true call again with `offset`.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Optional search words (all must match part number, description or category)."},
                                                       "offset": {"type": "integer", "description": "Skip this many matches (paging)."},
                                                       "limit": {"type": "integer", "description": "Page size (default 80, max 200)."}}},
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
                            "part_number": {"type": "string", "description": "Exact part_number from get_parts_catalog (required)"},
                            "description": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit_price": {"type": "number", "description": "Omit: the catalog price is used. Never guess a price."},
                        },
                        "required": ["part_number", "quantity"],
                    },
                },
                "notes": {"type": "string"},
            },
            "required": ["unit_number", "customer", "total_hours", "labor_lines"],
        },
    },
]

INVOICE_PROMPT = """
INVOICING (Wrenchworks shop invoices). You can prepare DRAFT invoices only; Benjamin approves or discards each one on screen.
1. ALWAYS ask for the truck / unit number first, before anything else about an invoice. Never guess it.
2. Then confirm customer, work date, total hours, what was done, engine/unit type if it matters, and PO number if the customer uses POs (leave blank rather than guess).
3. Call get_labor_catalog (it searches the complete Wrenchworks labor catalog; use several queries and its `offset` paging, do not stop at the first page or guess) and split the total hours across several real labor codes that follow the flow of the job (call-out, scan, diagnosis, repair, calibration or recharge, road test). Never invent a code. If a needed code does not exist, say so and suggest adding it to the catalog. Line hours must add up exactly to the total.
4. Rates and tax are applied by the system: $150/hr for Recology and Charter, $180/hr for everyone else; 10.25 percent tax on parts only. Parts: call get_parts_catalog (complete Wrenchworks parts catalog, paged) and use only real part numbers from it; the system fills in the catalog price. If a needed part is not in the catalog, say so and ask Benjamin; never invent a part number or price. If the catalog has no price for a part it shows as TBD.
5. Call create_invoice_draft. It only shows a draft on screen. You cannot save or email anything. Tell Benjamin the draft is on screen and waiting for his approval, and read back the total briefly. Never say an invoice was saved or sent.
6. Never offer to email a customer unless Benjamin asks; even then you cannot do it from here.
7. If anything goes wrong (a tool error, Benjamin discards a draft, or the screen says a draft could not be saved), never edit or retry the old draft. Tell Benjamin briefly what went wrong and, when he says to go on, create a brand NEW draft from scratch with a fresh create_invoice_draft call containing all the details. Never claim an earlier draft was saved unless Benjamin says the screen confirmed it.
""".strip()


DEFAULT_BASE_URL = "http://local-wrenchworks:8099"
CATALOG_MAX_UNFILTERED = 80
CATALOG_MAX_PAGE = 200


class WrenchworksError(Exception):
    pass


class ToolNotFound(WrenchworksError):
    """The shop's /api/mcp does not have the requested tool (older Wrenchworks)."""


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
        self._catalog = None
        self._catalog_at = 0.0
        self._cookie = ""
        self._last_error = ""  # why the last real-catalog load failed (secret-free)
        self._parts = None
        self._parts_at = 0.0
        self._parts_error = ""

    def secret_values(self) -> list:
        """Values that must never reach the log (passwords, tokens, the live session cookie)."""
        vals = [self.password, self.mcp_token, self._cookie, self._cookie.split("=", 1)[-1] if self._cookie else ""]
        return [v for v in vals if v]

    @property
    def catalog_configured(self) -> bool:
        """Real catalog available: the mcp_token (search_labor_codes) or, for older Wrenchworks, the shop password."""
        return bool(self.base and (self.mcp_token or self.password))

    @property
    def can_save(self) -> bool:
        """Saving possible at all (needs the Wrenchworks mcp_token and a real catalog). Always on: no separate switch."""
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
                self._last_error = ""
                LOG.info("labor catalog: %d codes loaded from Wrenchworks", len(cat["codes"]))
                return cat
            except Exception as exc:  # noqa: BLE001
                self._catalog = None
                self._last_error = redact(describe_exc(exc), self.secret_values(), 300)
                LOG.warning("labor catalog: could not load from Wrenchworks: %s", self._last_error)
                raise CatalogError(f"Could not load the labor catalog from Wrenchworks: {describe_exc(exc)}. Not building an invoice without the real catalog. Tell Benjamin about this problem; retrying immediately will not help unless it was a timeout, and then start a brand new draft from scratch.")
        raw = json.loads(SAMPLE_CATALOG.read_text())
        cat = {"codes": {c["code"]: c["description"] for c in raw["codes"]}, "source": "sample", "default_rate": _d(raw["default_rate"])}
        self._catalog, self._catalog_at = cat, time.time()
        return cat

    async def catalog_status(self, force: bool = False) -> dict:
        """Secret-free status for the on-screen line: {"state": loaded|sample|error|disabled, "count", "message"}."""
        if not self.enabled:
            return {"state": "disabled", "count": 0, "message": "Invoicing is turned off (wrenchworks_enabled is false)."}
        try:
            cat = await self.catalog(force=force)
        except CatalogError:
            return {"state": "error", "count": 0, "message": "Labor codes NOT loaded from Wrenchworks: " + (self._last_error or "unknown error") + ". Invoices cannot be drafted until this is fixed."}
        n = len(cat["codes"])
        if cat["source"] == "wrenchworks":
            return {"state": "loaded", "count": n, "message": f"Labor codes: {n} loaded from Wrenchworks."}
        return {"state": "sample", "count": n, "message": f"Only the {n}-code SAMPLE labor catalog is in use, not your Wrenchworks codes, because wrenchworks_mcp_token is not set in the Grok Voice configuration (set it to the Wrenchworks mcp_token option; older Wrenchworks needs wrenchworks_password instead). Nothing can be saved until then."}

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

    @staticmethod
    def _codes_from_rows(rows) -> dict:
        codes: dict = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            if code and code not in codes:
                codes[code] = str(row.get("name") or row.get("desc") or row.get("description") or "")
        return codes

    async def fetch_catalog(self) -> dict:
        """Prefer the read-only `search_labor_codes` MCP tool (bearer mcp_token, only labor rows).
        Fall back to shop login + GET /api/db ONLY when the shop does not have that tool (older Wrenchworks)."""
        if self.mcp_token:
            try:
                codes = await self._fetch_codes_via_tool()
                return {"codes": codes, "source": "wrenchworks", "default_rate": DEFAULT_RATE}
            except ToolNotFound:
                LOG.warning("Wrenchworks has no search_labor_codes tool (older than 1.1.127); falling back to shop login + /api/db")
                if not self.password:
                    raise WrenchworksError("this Wrenchworks has no search_labor_codes tool (update Wrenchworks to 1.1.127 or newer, or set wrenchworks_password so the older login + /api/db path can be used)")
        return {"codes": await self._fetch_codes_via_db(), "source": "wrenchworks", "default_rate": DEFAULT_RATE}

    async def _fetch_codes_via_tool(self) -> dict:
        codes: dict = {}
        offset = 0
        complete = False
        for _ in range(60):  # 60 pages x 500 = 30000 codes max
            res = await self._mcp_call("search_labor_codes", {"limit": 500, "offset": offset})
            if res.get("error"):
                raise WrenchworksError(f"search_labor_codes: {res.get('error')}")
            rows = res.get("codes")
            if not isinstance(rows, list):
                raise WrenchworksError("search_labor_codes: unexpected reply shape (no `codes` list)")
            for c, d in self._codes_from_rows(rows).items():
                codes.setdefault(c, d)
            if not res.get("hasMore") or not rows:
                complete = True
                break
            offset += len(rows)
        if not complete:
            raise WrenchworksError("the labor catalog is larger than the paging limit (30000 codes); refusing to use a partial catalog")
        if not codes:
            raise WrenchworksError("search_labor_codes worked but the shop has no labor rows with a `code` (labor catalog is empty)")
        return codes

    async def _fetch_codes_via_db(self) -> dict:
        """OLD PATH: GET /api/db (session cookie) -> keep ONLY the `labor` rows."""
        data = await self._get_db_json()
        rows = data.get("labor") if isinstance(data, dict) else None
        del data  # drop customers/invoices/etc. immediately
        codes = self._codes_from_rows(rows)
        if not codes:
            raise WrenchworksError("GET /api/db worked but the shop has no labor rows with a `code` (labor catalog is empty)")
        return codes

    # ------------------------------------------------------------ parts catalog
    @staticmethod
    def _parts_from_rows(rows) -> dict:
        parts: dict = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            sku = str(row.get("sku") or row.get("code") or "").strip()
            if not sku or sku in parts:
                continue
            price = _num(row.get("price") if row.get("price") not in (None, "") else row.get("sell"))
            parts[sku] = {"description": str(row.get("name") or row.get("desc") or row.get("description") or ""),
                          "price": price if (price is not None and price >= 0) else None,
                          "category": str(row.get("category") or "")}
        return parts

    async def parts_catalog(self, force: bool = False) -> dict:
        """-> {"parts": {SKU: {description, price(Decimal|None), category}}, "source": "wrenchworks"|"sample"}.
        Same route as labor: search_parts tool first, old shop login + /api/db `parts` only if the tool is missing."""
        pc = getattr(self, "_parts", None)
        if pc and not force and time.time() - self._parts_at < 300:
            return pc
        if not self.catalog_configured:
            pc = {"parts": {}, "source": "sample"}
        else:
            try:
                parts = None
                if self.mcp_token:
                    try:
                        parts = await self._fetch_parts_via_tool()
                    except ToolNotFound:
                        LOG.warning("Wrenchworks has no search_parts tool (older than 1.1.127); falling back to shop login + /api/db")
                        if not self.password:
                            raise WrenchworksError("this Wrenchworks has no search_parts tool (update Wrenchworks to 1.1.127 or newer, or set wrenchworks_password so the older login + /api/db path can be used)")
                if parts is None:
                    data = await self._get_db_json()
                    rows = data.get("parts") if isinstance(data, dict) else None
                    del data
                    parts = self._parts_from_rows(rows)
                pc = {"parts": parts, "source": "wrenchworks"}
            except Exception as exc:  # noqa: BLE001
                self._parts = None
                self._parts_error = redact(describe_exc(exc), self.secret_values(), 300)
                LOG.warning("parts catalog: could not load from Wrenchworks: %s", self._parts_error)
                raise CatalogError(f"Could not load the parts catalog from Wrenchworks: {describe_exc(exc)}. Not putting parts on an invoice without it. Tell Benjamin; then start a brand new draft from scratch once it works.")
            self._parts_error = ""
            LOG.info("parts catalog: %d parts loaded from Wrenchworks", len(pc["parts"]))
        self._parts, self._parts_at = pc, time.time()
        return pc

    async def _fetch_parts_via_tool(self) -> dict:
        parts: dict = {}
        offset = 0
        complete = False
        for _ in range(60):
            res = await self._mcp_call("search_parts", {"limit": 500, "offset": offset})
            if res.get("error"):
                raise WrenchworksError(f"search_parts: {res.get('error')}")
            rows = res.get("parts")
            if not isinstance(rows, list):
                raise WrenchworksError("search_parts: unexpected reply shape (no `parts` list)")
            for k, v in self._parts_from_rows(rows).items():
                parts.setdefault(k, v)
            if not res.get("hasMore") or not rows:
                complete = True
                break
            offset += len(rows)
        if not complete:
            raise WrenchworksError("the parts catalog is larger than the paging limit; refusing to use a partial catalog")
        return parts

    async def tool_get_parts_catalog(self, args: dict) -> dict:
        args = args or {}
        pc = await self.parts_catalog()
        if pc["source"] != "wrenchworks":
            return {"ok": False, "reason": "parts_catalog_unavailable", "error": "The real parts catalog is not available (wrenchworks_mcp_token is not set). Tell Benjamin; do not invent parts."}
        items = [{"part_number": k, "description": v["description"], "price": (float(v["price"]) if v["price"] is not None else None), "category": v["category"]} for k, v in pc["parts"].items()]
        q = str(args.get("query") or "").lower().split()
        if q:
            items = [i for i in items if all(w in (i["part_number"] + " " + i["description"] + " " + i["category"]).lower() for w in q)]
        total = len(items)
        lim = _num(args.get("limit"))
        limit = CATALOG_MAX_UNFILTERED if lim is None or lim <= 0 else min(int(lim), CATALOG_MAX_PAGE)
        off = _num(args.get("offset"))
        offset = max(int(off), 0) if off is not None else 0
        page = items[offset:offset + limit]
        out = {"ok": True, "catalog_size": len(pc["parts"]), "total_matches": total, "offset": offset, "returned": len(page), "has_more": offset + len(page) < total}
        if q and total == 0:
            out["hint"] = "No part matches all of those words. Try fewer or shorter words, or a part number fragment."
        if out["has_more"]:
            out["note"] = f"{total} parts match; showing {offset + 1}-{offset + len(page)}. Call again with offset={offset + len(page)} or narrow with a `query`."
        out["parts"] = page
        return out

    async def tool_get_labor_catalog(self, args: dict) -> dict:
        """Search or page through the FULL catalog. With `query`: every matching code (paged). Without: all codes,
        `limit` (default 80, max 200) at a time via `offset`; `total_matches`/`has_more` say when the list is complete."""
        args = args or {}
        cat = await self.catalog()
        items = [{"code": k, "description": v} for k, v in cat["codes"].items()]
        q = str(args.get("query") or "").lower().split()
        if q:
            items = [i for i in items if all(w in (i["code"] + " " + i["description"]).lower() for w in q)]
        total = len(items)
        lim = _num(args.get("limit"))
        limit = CATALOG_MAX_UNFILTERED if lim is None or lim <= 0 else min(int(lim), CATALOG_MAX_PAGE)
        off = _num(args.get("offset"))
        offset = max(int(off), 0) if off is not None else 0
        page = items[offset:offset + limit]
        out = {"ok": True, "source": cat["source"], "catalog_size": len(cat["codes"]), "total_matches": total, "offset": offset,
               "returned": len(page), "has_more": offset + len(page) < total}
        if q and total == 0:
            out["hint"] = "No code matches all of those words. Try fewer or shorter words (one keyword), or call without a query."
        if out["has_more"]:
            out["note"] = f"{total} codes match; showing {offset + 1}-{offset + len(page)}. Call again with offset={offset + len(page)} for the next page, or narrow with a `query`. The full catalog is available this way; use only codes it lists."
        out["codes"] = page
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
            emsg = str(e.get("message") if isinstance(e, dict) else e)
            if code == -32602 and emsg.startswith("Unknown tool"):
                raise ToolNotFound(f"{what}: {emsg[:100]}")
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
                return _fail("customer_lookup_failed", f"Could not look up the customer in Wrenchworks: {d}. Nothing was drafted. Tell Benjamin about this problem; do not guess the customer. When he says to continue, create a brand new draft from scratch.",
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
        raw_parts = _as_list(args.get("parts"))
        pcat = None
        if raw_parts and cat["source"] == "wrenchworks":
            try:
                pcat = await self.parts_catalog()
            except CatalogError as exc:
                return _fail("parts_catalog_unavailable", str(exc), "parts catalog unavailable: " + (getattr(self, "_parts_error", "") or "unknown"))
        for pi, p in enumerate(raw_parts):
            if not isinstance(p, dict):
                problems.append(("invalid_part", f"parts[{pi}] must be an object with description and quantity.", f"parts[{pi}] not an object")); continue
            desc = str(p.get("description") or "").strip()
            pn_in = str(p.get("part_number") or "").strip()
            cat_part = None
            if pcat is not None:
                if not pn_in:
                    problems.append(("part_not_in_catalog", f"parts[{pi}] needs a part_number from the shop parts catalog. Call get_parts_catalog (use a query) and use only listed parts.", f"parts[{pi}] no part_number")); continue
                pk, phit = resolve_code({"codes": pcat["parts"]}, pn_in)
                if pk == "ambiguous":
                    problems.append(("part_ambiguous", f"part number '{pn_in}' matches several catalog parts ({', '.join(phit)}). Use one exact part_number from get_parts_catalog.", f"part {pn_in!r} ambiguous")); continue
                if pk == "none":
                    sug = (" Did you mean: " + ", ".join(phit) + "?") if phit else ""
                    problems.append(("part_not_in_catalog", f"part number '{pn_in}' is not in the shop parts catalog.{sug} Call get_parts_catalog (use a query) and use only listed parts; if the part really is not there, tell Benjamin and ask.", f"part {pn_in!r} not in catalog")); continue
                pn_in = phit
                cat_part = pcat["parts"][phit]
                desc = desc or cat_part["description"] or phit
            if not desc:
                problems.append(("invalid_part", f"parts[{pi}] needs a description.", f"parts[{pi}] no description")); continue
            qty = _num(p.get("quantity"))
            if qty is None or qty <= 0:
                problems.append(("invalid_part", f"quantity for part '{desc[:40]}' must be a positive number (got {_show(p.get('quantity'))}).", f"parts[{pi}] bad quantity")); continue
            price = p.get("unit_price")
            if cat_part is not None:
                price = float(cat_part["price"]) if cat_part["price"] is not None else None  # the shop catalog price always wins
            row = {"part_number": pn_in[:60], "description": desc[:200],
                   "quantity": float(qty), "unit_price": None, "amount": None, "from_catalog": cat_part is not None}
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
            "open_items": open_items, "catalog_source": cat["source"], "ai_marker": AI_MARKER,
            "customer_id": shop_customer["id"] if shop_customer else "",
        }

        blockers = []
        if cat["source"] != "wrenchworks":
            blockers.append("sample catalog in use (Wrenchworks not configured)")
        if any(p["unit_price"] is None for p in parts):
            blockers.append("part price TBD")
        if cat["source"] == "wrenchworks" and not self.mcp_token:
            blockers.append("wrenchworks_mcp_token is not set (needed to create invoices)")
        draft["save_blockers"] = blockers
        drafts[draft["id"]] = draft
        while len(drafts) > 10:
            drafts.pop(next(iter(drafts)))
        summary = {k: draft[k] for k in ("id", "unit_number", "customer", "rate", "labor_subtotal", "parts_subtotal", "tax", "total", "open_items")}
        return {"ok": True, "message": "Draft is on Benjamin's screen awaiting his approval. NOT saved, NOT emailed. Do not say it was saved.", "draft": summary, "_draft": draft}

    # ------------------------------------------------------------------- save
    async def approve(self, draft: dict) -> dict:
        """Called only from a human tap on APPROVE in the browser. Returns {"ok": bool, "message": str}.
        Saving is always on; the only gate is that tap plus the safety checks below. Whatever happens, the
        draft is never retried: on any problem Grok/Benjamin start a brand NEW draft from scratch."""
        if draft["status"] != "pending":
            return {"ok": False, "message": f"Draft already {draft['status']}. Nothing more was done."}
        if draft["save_blockers"]:
            draft["status"] = "not_saved"
            return {"ok": False, "saved": False, "message": "NOT saved: " + "; ".join(draft["save_blockers"]) + ". Nothing was written to Wrenchworks. Ask Grok to create a new draft once that is fixed.",
                    "log": "approve refused: " + "; ".join(draft["save_blockers"])}
        # Duplicate check first: never create a second saved invoice if an earlier attempt may have gone through.
        try:
            dup = await self._find_duplicate_draft(draft)
        except Exception as exc:  # noqa: BLE001
            d = describe_exc(exc)
            draft["status"] = "save_failed"
            return {"ok": False, "saved": False, "message": f"NOT saved: could not check Wrenchworks for an existing copy first ({redact(d, self.secret_values(), 200)}). Nothing was written. Not retried; ask Grok to create a new draft.", "log": f"duplicate check failed: {d}"}
        if dup:
            draft["status"] = "duplicate"
            return {"ok": False, "saved": False, "message": f"NOT saved again: Wrenchworks already has an unsent draft {dup} for this customer, unit and total. Check it in Wrenchworks. Nothing new was written.", "log": f"duplicate of {dup}"}
        try:
            ref = await self.save_invoice(draft)
        except Exception as exc:  # noqa: BLE001
            d = describe_exc(exc)
            draft["status"] = "save_failed"
            return {"ok": False, "saved": False, "message": f"Save FAILED: {redact(d, self.secret_values(), 200)}. This draft will not be retried, and it may or may not have reached Wrenchworks. Ask Grok to create a brand new draft; the add-on checks Wrenchworks for an existing copy before saving, so it will not be saved twice.", "log": f"save failed: {d}"}
        draft["status"] = "saved"
        warn = (" " + draft["shop_total_mismatch"]) if draft.get("shop_total_mismatch") else ""
        return {"ok": True, "saved": True, "ref": ref, "message": f"Saved to Wrenchworks{(' as ' + ref) if ref else ''} as an unsent draft. Nothing was emailed.{warn}"}

    async def _find_duplicate_draft(self, draft: dict) -> str:
        """Ask the shop (read-only `search_invoices`, status draft) whether an unsent draft with the same
        customer, unit and total already exists. Returns its number/id, or ''. Errors propagate (fail closed)."""
        res = await self._mcp_call("search_invoices", {"q": draft["unit_number"], "status": "draft", "limit": 50})
        if res.get("error"):
            raise WrenchworksError(f"search_invoices: {res.get('error')}")
        want_unit = re.sub(r"\s+", "", draft["unit_number"]).lower()
        for inv in res.get("invoices") or []:
            if not isinstance(inv, dict):
                continue
            if str(inv.get("kind") or "invoice") != "invoice" or str(inv.get("status") or "draft").lower() != "draft":
                continue
            if draft.get("customer_id") and str(inv.get("customerId") or "") != draft["customer_id"]:
                continue
            if re.sub(r"\s+", "", str(inv.get("vehicle") or "")).lower() != want_unit:
                continue
            try:
                if abs(_d(inv.get("total")) - _d(draft["total"])) <= CENT:
                    return str(inv.get("number") or inv.get("id") or "existing draft")
            except Exception:  # noqa: BLE001
                continue
        return ""

    async def save_invoice(self, draft: dict) -> str:
        """Create the invoice in Wrenchworks as an UNSENT DRAFT via its /api/mcp `create_draft_invoice`
        tool (bearer mcp_token). That tool never emails or charges; status is always 'draft'.
        Returns the shop's invoice number (e.g. INV-1042)."""
        notes = [AI_MARKER]  # every Grok Voice invoice is visibly marked as AI-created
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
            "aiCreated": True,  # Wrenchworks >= 1.1.128 shows an AI-created badge; older versions ignore it
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

