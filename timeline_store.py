"""A local copy of the team's deal timeline, kept in sync in the background.

Reports used to pull Twenty's timeline on every command (40+ requests for
/monthly). The London key shares a 600-per-minute budget with every other job
on it, so those requests were often 429'd and a command sat on "typing..." for
minutes. Now a background thread keeps this copy current and reports read it
with no Twenty calls at all.

Sync: once, every entry since `history_since` for all the team's deals (kept in
state/, so the Actions cache carries it to the next run); after that, only
deals whose updatedAt moved since the last sync, which is usually one request.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import instance

PATH = os.path.join(instance.STATE_DIR, "timeline.json")
OVERLAP = timedelta(minutes=10)     # timeline rows land a little after the edit
FULL_EVERY = 6 * 3600               # re-pull everything now and then (deletes, drift)


def _slim(t):
    """Only what reports read: keeps the file small."""
    props = t.get("properties") or {}
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except ValueError:
            props = {}
    field = instance.STAGE_FIELD
    keep = {}
    d = (props.get("diff") or {}).get(field)
    if d:
        keep["diff"] = {field: d}
    a = (props.get("after") or {}).get(field) if isinstance(props.get("after"), dict) else None
    if a:
        keep["after"] = {field: a}
    return {"id": t.get("id"), "happensAt": t.get("happensAt"),
            "targetOpportunityId": t.get("targetOpportunityId"),
            "name": t.get("name") or "",
            "timelineActivityTypeSnapshot": {"name": (t.get("timelineActivityTypeSnapshot") or {}).get("name") or ""},
            "properties": keep}


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {}           # entry id -> slim entry
        self.horizon = None      # oldest happensAt covered
        self.synced = None       # datetime of last successful sync start
        self.full_at = 0.0
        self.ready = False
        self._load()

    def _load(self):
        try:
            with open(PATH) as f:
                d = json.load(f)
            self.rows = {r["id"]: r for r in d.get("rows", []) if r.get("id")}
            self.horizon = d.get("horizon")
            self.synced = datetime.fromisoformat(d["synced"]) if d.get("synced") else None
            self.full_at = float(d.get("full_at") or 0)
            # A copy restored from cache is trusted only if it is recent enough
            # for an incremental sync to close the gap.
            self.ready = bool(self.synced and self.horizon
                              and datetime.now(timezone.utc) - self.synced < timedelta(days=3))
        except (OSError, ValueError, KeyError):
            pass

    def save(self):
        with self.lock:
            d = {"horizon": self.horizon, "full_at": self.full_at,
                 "synced": self.synced.isoformat() if self.synced else None,
                 "rows": list(self.rows.values())}
        tmp = PATH + ".tmp"
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(d, f, separators=(",", ":"))
        os.replace(tmp, PATH)

    def sync(self, opps, horizon_iso):
        """One sync pass. `opps` is the bot's live deal list."""
        import twenty_api as tw
        started = datetime.now(timezone.utc)
        ids = [o["id"] for o in opps if o.get("id")]
        full = (not self.ready or self.horizon != horizon_iso
                or time.time() - self.full_at > FULL_EVERY)
        if full:
            got = tw._fetch_timeline(horizon_iso, ids=ids)
            rows = {r["id"]: r for r in map(_slim, got) if r.get("id")}
            with self.lock:
                self.rows, self.horizon = rows, horizon_iso
                self.full_at, self.synced, self.ready = time.time(), started, True
            return len(rows), True
        cut = self.synced - OVERLAP
        cut_s = cut.strftime("%Y-%m-%dT%H:%M:%S")
        changed = [o["id"] for o in opps if o.get("id")
                   and (o.get("updatedAt") or "9")[:19] >= cut_s]
        got = tw._fetch_timeline(_iso(cut), ids=changed) if changed else []
        with self.lock:
            for r in map(_slim, got):
                if r.get("id"):
                    self.rows[r["id"]] = r
            self.synced = started
        return len(got), False

    def query(self, since_iso, ids=None):
        """Entries since `since_iso` (for `ids` if given), or None when the
        copy can't answer (not synced yet, or asked for older history)."""
        with self.lock:
            if not self.ready or not self.horizon or since_iso < self.horizon:
                return None
            want = set(ids) if ids else None
            return [dict(r) for r in self.rows.values()
                    if (r.get("happensAt") or "") >= since_iso
                    and (want is None or r.get("targetOpportunityId") in want)]


STORE = Store()


def start(get_opps, horizon_iso, log, every=30):
    """Background sync loop. `get_opps()` returns the live deals (or [])."""
    def run():
        last_save = 0.0
        while True:
            try:
                opps = get_opps()
                if opps:
                    n, full = STORE.sync(opps, horizon_iso)
                    if full:
                        log(f"timeline copy: {n} entries since {horizon_iso[:10]}")
                    if full or time.time() - last_save > 300:
                        STORE.save()
                        last_save = time.time()
            except Exception as e:      # a failed pass just retries next round
                log(f"timeline sync failed: {e}")
            time.sleep(every)
    threading.Thread(target=run, name="timeline-sync", daemon=True).start()
