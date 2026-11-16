"""Live daily submissions leaderboard (GitHub Pages).

After each poll the bot works out today's submissions (ET), and when the
tally changes it dispatches board.yml with the snapshot ENCRYPTED (AES-GCM,
key = BOARD_KEY secret). board.yml deploys it next to site/index.html on
GitHub Pages; the page holds the key only in its URL #fragment, so the public
repo, run logs and Pages site never carry readable names.

A submission = a watched, non-hidden deal created today (ET) that the bot has
announced, i.e. exactly the deals that got a Telegram card today.
"""
import base64, json, os, time, urllib.request
from datetime import datetime

import bot

_last = {"digest": None, "sent": 0.0, "day": None}
MIN_GAP = 15          # seconds between deploys at most
HEARTBEAT = 30 * 60   # republish this often anyway so the page can show "live"


def _key():
    raw = os.environ.get("BOARD_KEY") or ""
    if not raw:
        return None
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad)


def snapshot(cfg, state, live, caches):
    now = bot.now_local(cfg)
    today = now.date()
    since = bot.parse_iso(cfg.get("history_since"))
    subs = []
    for oid, opp in live.items():
        if bot.hidden(opp) or not (state.get(oid) or {}).get("announced"):
            continue
        made = bot.parse_iso(opp.get("createdAt"))
        if not made or (since and made < since):
            continue
        made = made.astimezone(now.tzinfo)
        if made.date() == today:
            subs.append((made, opp))
    ctx = bot.build_ctx([o for _, o in subs], caches)
    reps = {}
    feed = []
    for made, opp in sorted(subs, key=lambda x: x[0]):
        rep = bot.rep_of(ctx, opp)
        if bot.rep_hidden(rep):
            continue
        first = str(rep).split()[0] if rep else "Unassigned"
        r = reps.setdefault(first, {"name": first, "count": 0, "value": 0.0, "last": None})
        r["count"] += 1
        r["value"] += bot.amount_value(opp.get("amount")) or 0.0
        r["last"] = made.isoformat(timespec="seconds")
        feed.append({"rep": first, "at": made.isoformat(timespec="seconds"),
                     "value": bot.amount_value(opp.get("amount")) or 0.0,
                     "company": bot.company_field(opp, ctx, "name") or bot.deal_title(opp, ctx),
                     "stage": bot.stage_label(opp.get("stage"))})
    ranked = sorted(reps.values(), key=lambda r: (-r["count"], -r["value"], r["name"]))
    return {"day": today.isoformat(), "reps": ranked, "feed": feed[-12:][::-1],
            "total": sum(r["count"] for r in ranked),
            "totalValue": round(sum(r["value"] for r in ranked), 2)}


def _encrypt(obj, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, separators=(",", ":")).encode(), None)
    return base64.b64encode(iv + ct).decode()


def _dispatch(blob):
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    if not (repo and tok):
        return False
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/board.yml/dispatches",
        data=json.dumps({"ref": "main", "inputs": {"data": blob}}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 204


def publish(cfg, state, live, caches):
    key = _key()
    if not key:
        return
    snap = snapshot(cfg, state, live, caches)
    digest = json.dumps(snap, sort_keys=True)
    t = time.time()
    changed = digest != _last["digest"]
    if not changed and t - _last["sent"] < HEARTBEAT:
        return
    if changed and t - _last["sent"] < MIN_GAP and snap["day"] == _last["day"]:
        return          # caught on a later poll
    snap["generatedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if _dispatch(_encrypt(snap, key)):
        _last.update(digest=digest, sent=t, day=snap["day"])
        bot.log(f"board: published {snap['total']} submissions today")
