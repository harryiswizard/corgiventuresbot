#!/usr/bin/env python3
"""Thin Twenty CRM REST client.

Twenty sits behind Cloudflare, which 403s (error 1010) on urllib's default
User-Agent, so every request sends a curl UA. REST filters are `field[op]:value`
and the brackets must be percent-encoded or the API silently returns nothing.
"""
import http.client, json, os, threading, time, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

# TWENTY_BASE switches workspaces: the E&S bot reads the new Twenty
# (corgi-ventures host, London team only); the Reinsurance bot keeps the old one.
BASE = os.environ.get("TWENTY_BASE", "https://api.twenty.com").rstrip("/") + "/rest"
TEAM_ID = os.environ.get("TWENTY_TEAM_ID", "").strip()
TOKEN_FILE = os.path.expanduser(os.environ.get("TWENTY_TOKEN_FILE", "~/.twenty_team_token"))
UA = "curl/8.7.1"


class TwentyError(RuntimeError):
    pass


def token():
    """Env var first (GitHub Actions secret), then the local token file."""
    env = os.environ.get("TWENTY_TOKEN")
    if env:
        return env.strip()
    with open(TOKEN_FILE) as f:
        return f.read().strip()


_local = threading.local()


def _conn():
    """One kept-alive HTTPS connection per thread. A fresh TLS handshake per
    request was about half of every call's ~0.6s."""
    c = getattr(_local, "conn", None)
    if c is None:
        u = urllib.parse.urlsplit(BASE)
        c = _local.conn = http.client.HTTPSConnection(u.netloc, timeout=45)
    return c


def _drop_conn():
    c = getattr(_local, "conn", None)
    _local.conn = None
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def get(path, params=None, tok=None):
    tok = tok or token()
    url = urllib.parse.urlsplit(BASE).path + path
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    headers = {"Authorization": "Bearer " + tok, "User-Agent": UA}
    for attempt in range(4):
        try:
            c = _conn()
            c.request("GET", url, headers=headers)
            r = c.getresponse()
            body = r.read()
        except (OSError, http.client.HTTPException) as e:
            _drop_conn()        # stale keep-alive socket, DNS blip, timeout
            if attempt < 3:
                time.sleep(2 ** attempt if attempt else 0)
                continue
            raise TwentyError(f"network {path}: {e}")
        if r.status == 200:
            return json.loads(body)
        snippet = body[:200].decode("utf-8", "replace")
        if r.status == 429 and attempt < 3:
            # Twenty allows 100 requests per 60s, so a couple of seconds is
            # never enough — wait out a meaningful slice of the window.
            time.sleep([20, 40, 65][attempt])
            continue
        if r.status in (500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        raise TwentyError(f"{r.status} {path}: {snippet}")
    raise TwentyError("unreachable")


def find_many(obj, params=None, page_size=60, tok=None, max_pages=200):
    """Paginate /rest/<obj> with cursor paging; returns a list of records."""
    tok = tok or token()
    out, after, pages = [], None, 0
    while pages < max_pages:
        p = dict(params or {})
        p["limit"] = page_size
        if after:
            p["starting_after"] = after
        d = get("/" + obj, p, tok)
        recs = d.get("data", {}).get(obj, [])
        out.extend(recs)
        info = d.get("pageInfo", {}) or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
        if not after:
            break
        pages += 1
    return out


def opportunities(tok=None):
    """Every opportunity, with `stage` taken from this bot's stage field.

    The Reinsurance bot reads `reinsuranceStage`; copying it onto `stage` here
    means the rest of the code never needs to know which field it is."""
    import instance
    params = {"filter": f"teamId[eq]:{TEAM_ID}"} if TEAM_ID else None
    recs = find_many("opportunities", params, tok=tok)
    if TEAM_ID:
        # In the new Twenty, Companies hold insureds and the broker is the
        # Partner (brokerFirm), which is what the cards' company line means.
        for o in recs:
            o["companyId"] = o.get("brokerFirmId")
    if instance.STAGE_FIELD != "stage":
        # A deal with a reinsurance company but no Reinsurance Stage (say it
        # sits on the E&S pipeline) falls back to its shared Stage.
        shared = {"QUOTE_RECEIVED": "SUBMISSION_RECEIVED"}
        for o in recs:
            own = o.get(instance.STAGE_FIELD)
            o["stage"] = own or shared.get(o.get("stage"), o.get("stage"))
    return recs


_TL_CACHE = {}
_TL_LOCK = threading.Lock()
_TL_TTL = 90


def opportunity_timeline(since_iso, tok=None, ids=None):
    """Opportunity timeline entries (creates and edits) since `since_iso`.

    Each carries `properties.diff` (edits) or `properties.after` (creates), so
    this is Twenty's own record of when a deal reached a stage - it does not
    depend on the bot having been running, or on the deal being submitted.
    A report reads it twice (quotes, then closed won) for the same window, so
    results are cached briefly; id batches are fetched in parallel."""
    key = (since_iso, tuple(sorted(i for i in (ids or []) if i)) if ids else None)
    with _TL_LOCK:
        hit = _TL_CACHE.get(key)
        if hit and time.time() - hit[0] < _TL_TTL:
            return list(hit[1])
    if ids:
        # The new Twenty holds every team's deal history; asking only for our
        # deals keeps the query small and under the page cap.
        ids = [i for i in dict.fromkeys(ids) if i]
        batches = [ids[i:i + 40] for i in range(0, len(ids), 40)]
        with ThreadPoolExecutor(max_workers=4) as ex:
            parts = ex.map(lambda b: find_many(
                "timelineActivities",
                {"filter": f'and(targetOpportunityId[in]:[{",".join(b)}],'
                           f'happensAt[gte]:"{since_iso}")'},
                tok=tok, max_pages=100), batches)
            out = [t for part in parts for t in part]
    else:
        out = find_many("timelineActivities",
                        {"filter": f'and(targetOpportunityId[is]:NOT_NULL,'
                                   f'happensAt[gte]:"{since_iso}")'},
                        tok=tok, max_pages=100)
    with _TL_LOCK:
        for k in [k for k, v in _TL_CACHE.items() if time.time() - v[0] >= _TL_TTL]:
            _TL_CACHE.pop(k, None)
        _TL_CACHE[key] = (time.time(), out)
    return list(out)


def companies_by_id(ids, tok=None, chunk=30):
    """Bulk-fetch company records with `id[in]:[...]`, keyed by id.

    One call per 30 ids; resolving them singly took ~0.3s each, which stalled
    the first poll for two minutes."""
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        try:
            obj = "brokerFirms" if TEAM_ID else "companies"
            d = get("/" + obj,
                    {"filter": "id[in]:[" + ",".join(batch) + "]", "limit": chunk},
                    tok=tok)
            for rec in d.get("data", {}).get(obj, []) or []:
                out[rec["id"]] = rec
        except TwentyError:
            continue
    return out


def workspace_members(tok=None):
    out = {}
    for m in find_many("workspaceMembers", tok=tok):
        name = m.get("name") or {}
        full = " ".join(x for x in [name.get("firstName"), name.get("lastName")] if x).strip()
        out[m["id"]] = full or m.get("userEmail") or m["id"][:8]
    return out


def people_by_id(pid, tok=None):
    if not pid:
        return None
    try:
        d = get(f"/people/{pid}", tok=tok)
        rec = d.get("data", {}).get("person") or {}
        nm = rec.get("name") or {}
        return " ".join(x for x in [nm.get("firstName"), nm.get("lastName")] if x).strip() or None
    except TwentyError:
        return None


def broker_label(bid, tok=None):
    """A deal's broker contact (new Twenty `brokers`): name, else email."""
    if not bid:
        return None
    try:
        rec = get(f"/brokers/{bid}", tok=tok).get("data", {}).get("broker") or {}
    except TwentyError:
        return None
    nm = rec.get("name") or {}
    full = " ".join(x for x in [nm.get("firstName"), nm.get("lastName")] if x).strip()
    return full or (rec.get("emails") or {}).get("primaryEmail") or None


def patch(path, body, tok=None):
    """PATCH a single record, e.g. patch('/opportunities/<id>', {'stage': 'QUOTE_SENT'})."""
    tok = tok or token()
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="PATCH")
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body_txt = e.read()[:200].decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise TwentyError(f"{e.code} PATCH {path}: {body_txt}")
    raise TwentyError("unreachable")
