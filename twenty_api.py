#!/usr/bin/env python3
"""Thin Twenty CRM REST client.

Twenty sits behind Cloudflare, which 403s (error 1010) on urllib's default
User-Agent, so every request sends a curl UA. REST filters are `field[op]:value`
and the brackets must be percent-encoded or the API silently returns nothing.
"""
import json, os, time, urllib.parse, urllib.request, urllib.error

BASE = "https://api.twenty.com/rest"
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


def get(path, params=None, tok=None):
    tok = tok or token()
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("User-Agent", UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise TwentyError(f"{e.code} {path}: {body}")
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise TwentyError(f"network {path}: {e}")
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
    return find_many("opportunities", tok=tok)


def companies_by_id(ids, tok=None, chunk=30):
    """Bulk-fetch company records with `id[in]:[...]`, keyed by id.

    One call per 30 ids; resolving them singly took ~0.3s each, which stalled
    the first poll for two minutes."""
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        try:
            d = get("/companies",
                    {"filter": "id[in]:[" + ",".join(batch) + "]", "limit": chunk},
                    tok=tok)
            for rec in d.get("data", {}).get("companies", []) or []:
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
