#!/usr/bin/env python3
"""Read-only lookups in the OLD Twenty (api.twenty.com) for deals that were
copied into the new one with the same id: their stage history before the move,
and the rep who owned them (Atlas leaves Owner blank in the new Twenty).

Only used when the bot runs against the new Twenty (TWENTY_TEAM_ID set) and an
old-Twenty key is on disk (TWENTY_OLD_TOKEN_FILE, default ~/.twenty_token)."""
import json, os, urllib.parse, urllib.request

BASE = "https://api.twenty.com/rest"
TOKEN_FILE = os.path.expanduser(os.environ.get("TWENTY_OLD_TOKEN_FILE", "~/.twenty_token"))
_OWNERS = None


def available():
    return bool(os.environ.get("TWENTY_TEAM_ID")) and os.path.exists(TOKEN_FILE)


def _get(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + open(TOKEN_FILE).read().strip(), "User-Agent": "curl/8.7.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _many(obj, flt):
    out, after = [], None
    for _ in range(100):
        p = {"limit": 60, "filter": flt}
        if after:
            p["starting_after"] = after
        d = _get("/" + obj, p)
        out += d.get("data", {}).get(obj, []) or []
        info = d.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        after = info["endCursor"]
    return out


def timeline(since_iso, ids):
    out, ids = [], [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), 40):
        out += _many("timelineActivities",
                     f'and(targetOpportunityId[in]:[{",".join(ids[i:i + 40])}],happensAt[gte]:"{since_iso}")')
    return out


def owners():
    """{deal id: owner name} from the old Twenty."""
    global _OWNERS
    if _OWNERS is None:
        members = {}
        for m in _many("workspaceMembers", "id[is]:NOT_NULL"):
            n = m.get("name") or {}
            members[m["id"]] = " ".join(x for x in [n.get("firstName"), n.get("lastName")] if x).strip()
        _OWNERS = {o["id"]: members.get(o.get("ownerId")) for o in _many("opportunities", "id[is]:NOT_NULL")
                   if members.get(o.get("ownerId"))}
    return _OWNERS
