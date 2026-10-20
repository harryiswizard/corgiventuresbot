#!/usr/bin/env python3
"""/daily, /weekly and /monthly digests.

Everything about deals comes from Twenty: stage arrivals from its timeline,
new deals from createdAt, money and the pipeline snapshot from the live records.
The bot's own event log (state/events.jsonl) only feeds the appointed-Partner
line. Harry 2026-10-12: Twenty is the source of truth, not the bot's records.
"""
import html, json, os
from datetime import datetime, timedelta, timezone

import twenty_api as tw
import instance
from bot import amount_value, appointments_line, fmt_money, leaderboard
from stages import STAGE_ORDER, STAGE_EMOJI, stage_label, stage_rank

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(instance.STATE_DIR, "events.jsonl")

BULLET = "•"
ARROW = "→"
TROPHY = "\U0001f3c6"
OUTBOX = "\U0001f4e4"
NEW = "\U0001f195"
BIN = "\U0001f5d1"

PERIODS = {"daily": "Today", "weekly": "This week", "monthly": "This month"}
MAX_NAMES = 8          # deals listed per stage before the rest are summarised


def _tz(cfg):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg.get("timezone") or "America/New_York")
    except Exception:
        return None


def period_start(now, period):
    """Calendar start of the period: midnight today, Monday this week, the 1st."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        start -= timedelta(days=start.weekday())
    elif period == "monthly":
        start = start.replace(day=1)
    return start


def window(cfg, period, previous=False):
    """(start, end, title) in the configured timezone. previous=True gives the
    last complete period instead (the scheduled Monday / 1st-of-month posts)."""
    now = datetime.now(_tz(cfg))
    if period not in PERIODS:
        period = "daily"
    start = period_start(now, period)
    if previous:
        now = start
        start = period_start(start - timedelta(seconds=1), period)
        if period == "daily":
            return start, now, start.strftime("%A %-d %B")
        if period == "monthly":
            return start, now, start.strftime("%B %Y")
    if period == "daily":
        title = now.strftime("%A %-d %B")
    elif period == "weekly":
        title = f"Week of {start.strftime('%a %-d %b')}"
    else:
        title = now.strftime("%B %Y")
    return start, now, title


def load_events(cfg, start, end):
    if not os.path.exists(EVENTS_FILE):
        return []
    out, tz = [], _tz(cfg)
    with open(EVENTS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                ts = datetime.fromisoformat(ev["ts"])
            except (ValueError, KeyError):
                continue
            if ts.tzinfo is None and tz is not None:
                ts = ts.replace(tzinfo=tz)
            if start <= ts <= end:
                ev["_ts"] = ts
                out.append(ev)
    return out


def by_stage_ok(counts):
    """Stages shown in reports: every live stage except the hidden ones."""
    from stages import HIDDEN_STAGES
    return (set(STAGE_ORDER) | set(counts)) - set(HIDDEN_STAGES)


def _watched(opp, cfg):
    from bot import watched
    return watched(opp, cfg)


def snapshot(cfg):
    """Deals per stage and their value right now, straight from Twenty."""
    from stages import HIDDEN_STAGES
    opps = [o for o in tw.opportunities()
            if _watched(o, cfg) and o.get("stage") not in HIDDEN_STAGES]
    counts, values = {}, {}
    for o in opps:
        st = o.get("stage")
        counts[st] = counts.get(st, 0) + 1
        values[st] = values.get(st, 0.0) + (amount_value(o.get("amount")) or 0.0)
    return counts, len(opps), values, {o["id"]: o for o in opps}


def period_value(events, by_id):
    """(total, currency, missing) for a set of events.

    Takes the live amount first — an amount typed in after the move still
    counts — then whatever was recorded at the time. A deal with no amount
    contributes 0 and is counted as missing."""
    total, missing, currency = 0.0, 0, "USD"
    for e in events:
        live = by_id.get(e.get("id")) or {}
        val = amount_value(live.get("amount"))
        if val is None:
            val = e.get("amount_value")
        if val:
            total += val
            currency = ((live.get("amount") or {}).get("currencyCode")
                        or e.get("currency") or currency)
        else:
            missing += 1
    return total, currency, missing


def _value_of(evs, by_id):
    """Total value behind a set of events. Reports are counts and money only —
    no deal names."""
    total, currency = 0.0, "USD"
    for e in evs:
        live = by_id.get(e.get("id")) or {}
        val = amount_value(live.get("amount"))
        if val is None:
            val = e.get("amount_value")
        if val:
            total += val
            currency = ((live.get("amount") or {}).get("currencyCode")
                        or e.get("currency") or currency)
    return total, currency


def _reached(entry, stage):
    """True when a timeline entry is a deal landing on `stage`."""
    props = entry.get("properties") or {}
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except ValueError:
            return False
    diff = (props.get("diff") or {}).get(instance.STAGE_FIELD) or {}
    if diff.get("after") == stage and diff.get("before") != stage:
        return True
    return (props.get("after") or {}).get(instance.STAGE_FIELD) == stage


def _since(cfg, start):
    """Window start, clamped to `history_since`: timeline entries before it are
    the old->new Twenty migration (copies created and set to their stage in bulk
    on 6-7 Oct), not reps moving deals."""
    floor = cfg.get("history_since")
    if floor:
        f = datetime.fromisoformat(floor)
        if f > start:
            return f
    return start


def arrivals(cfg, start, end, by_id):
    """{stage: {deal id: when}} - the first time in the window each deal landed
    on each stage, from Twenty's own timeline.

    Most quotes are keyed straight in at Quote Sent (a create, not a move), so a
    deal created at a stage counts as reaching it. A deal created straight at a
    stage has an empty create entry in the new Twenty, so its starting stage is
    taken from its first stage edit (or its current stage if never edited)."""
    start = _since(cfg, start)
    out = {}
    if start > end or not by_id:
        return out
    since = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    tl = tw.opportunity_timeline(since, ids=list(by_id) if tw.TEAM_ID else None)
    created, first_before = {}, {}
    for t in sorted(tl, key=lambda t: t.get("happensAt") or ""):
        oid = t.get("targetOpportunityId")
        if oid not in by_id:
            continue
        try:
            when = datetime.fromisoformat(t["happensAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if not (start <= when <= end):
            continue
        name = (t.get("timelineActivityTypeSnapshot") or {}).get("name") or t.get("name") or ""
        props = t.get("properties") or {}
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except ValueError:
                props = {}
        d = (props.get("diff") or {}).get(instance.STAGE_FIELD)
        if name.endswith("Created") or name.endswith(".created"):
            created.setdefault(oid, when)
        if d and oid not in first_before:
            first_before[oid] = d.get("before")
        for st in {d.get("after") if d else None,
                   (props.get("after") or {}).get(instance.STAGE_FIELD)} - {None}:
            if _reached(t, st):
                out.setdefault(st, {}).setdefault(oid, when)
    for oid, when in created.items():
        st = first_before.get(oid, (by_id.get(oid) or {}).get("stage"))
        if st:
            out.setdefault(st, {}).setdefault(oid, when)
    return out


def reached_stage(cfg, start, end, by_id, stage):
    """[(opp, when)] for every deal that landed on `stage` in the window."""
    hits = arrivals(cfg, start, end, by_id).get(stage) or {}
    return [(by_id[o], w) for o, w in hits.items()]


def new_deals(cfg, start, end, by_id):
    """Deals created in Twenty in the window (migration copies excluded)."""
    start = _since(cfg, start)
    out = []
    for o in by_id.values():
        try:
            made = datetime.fromisoformat((o.get("createdAt") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if start <= made <= end:
            out.append(o)
    return out


def quotes_sent(cfg, start, end, by_id):
    return reached_stage(cfg, start, end, by_id, "QUOTE_SENT")


def quotes_section(cfg, start, end, by_id, caches, sent_evs):
    """Quotes sent in the window, with a rep leaderboard."""
    import bot
    ctx = bot.build_ctx([], caches or ({}, {}))
    try:
        rows = [(o, bot.rep_of(ctx, o)) for o, _ in quotes_sent(cfg, start, end, by_id)]
    except tw.TwentyError as e:
        bot.log(f"quote timeline failed, using the event log: {e}")
        rows = [(by_id.get(e.get("id")) or {"amount": None}, e.get("rep") or e.get("owner")
                 or "Unassigned") for e in sent_evs]
    if not rows:
        return [f"{OUTBOX} <b>Quotes sent: 0</b>"]
    tally, rep_value, total = {}, {}, 0.0
    for o, rep in rows:
        v = amount_value(o.get("amount")) or 0.0
        tally[rep] = tally.get(rep, 0) + 1
        rep_value[rep] = rep_value.get(rep, 0.0) + v
        total += v
    return ([f"{OUTBOX} <b>Quotes sent: {len(rows)} · {fmt_money(total)}</b>"]
            + leaderboard(tally, rep_value))


def report(cfg, period, caches=None, previous=False):
    start, end, title = window(cfg, period, previous)
    evs = load_events(cfg, start, end)
    appointed = [e for e in evs if e.get("kind") == "appointed"]
    unappointed = [e for e in evs if e.get("kind") == "unappointed"]

    label = {"daily": "Daily", "weekly": "Weekly",
             "monthly": "Monthly"}.get(period, "Report")
    lines = [f"<b>{label} \u2014 {title}</b>"]
    try:
        counts, total, values, by_id = snapshot(cfg)
        arr = arrivals(cfg, start, end, by_id)
    except tw.TwentyError as e:
        lines.append(f"\n(Twenty unavailable: {html.escape(str(e))})")
        return "\n".join(lines)

    import bot
    ctx = bot.build_ctx([], caches or ({}, {}))

    # Closed won: deals that reached Closed Won in the window and are still
    # there now (moved back or deleted = not closed business).
    # Closed won in the period = Closed Won deals whose Close date in Twenty
    # falls in it (a future Close date counts from today). No Close date: the
    # day it landed on Closed Won, from the timeline.
    landed = arr.get("CLOSED_WON") or {}
    won = []
    for o in by_id.values():
        if o.get("stage") != "CLOSED_WON":
            continue
        try:
            when = datetime.fromisoformat((o.get("closeDate") or "").replace("Z", "+00:00"))
        except ValueError:
            when = landed.get(o["id"])
        if when and when >= start:
            won.append({"id": o["id"], "to": "CLOSED_WON"})
    if won:
        val, cur, _ = period_value(won, by_id)
        lines.append(f"\n{TROPHY} <b>Closed won: {len(won)} \u00b7 {fmt_money(val, cur)}</b>")
        tally, rep_value = {}, {}
        for e in won:
            o = by_id[e["id"]]
            rep = bot.rep_of(ctx, o)
            tally[rep] = tally.get(rep, 0) + 1
            rep_value[rep] = rep_value.get(rep, 0.0) + (amount_value(o.get("amount")) or 0.0)
        lines += leaderboard(tally, rep_value)
    else:
        lines.append("")
    lines += quotes_section(cfg, start, end, by_id, caches, [])

    if cfg.get("show_appointments", True):
        lines.append(f"\n\U0001f91d <b>Agencies appointed: {len(appointed)}</b>")
    if appointed and cfg.get("show_appointments", True):
        by_rep = {}
        for e in appointed:
            if bot.rep_hidden(e.get("by")):
                continue
            by_rep[e.get("by") or "Unassigned"] = by_rep.get(e.get("by") or "Unassigned", 0) + 1
        lines.append("   " + " \u00b7 ".join(f"{html.escape(k)} {v}"
                                             for k, v in sorted(by_rep.items(), key=lambda x: -x[1])))
    if unappointed and cfg.get("show_appointments", True):
        lines.append(f"\u21a9 <b>Appointments removed: {len(unappointed)}</b>")

    moved = {st: hits for st, hits in arr.items() if st in by_stage_ok(counts)}
    n_moves = sum(len(h) for h in moved.values())
    if n_moves:
        lines.append(f"\n<b>Reached stage: {n_moves}</b>")
        for s in sorted(moved, key=stage_rank):
            evs_s = [{"id": oid} for oid in moved[s]]
            val, cur = _value_of(evs_s, by_id)
            money = f" \u00b7 {fmt_money(val, cur)}" if val else ""
            lines.append(f"{STAGE_EMOJI.get(s, BULLET)} {ARROW} {stage_label(s)}: "
                         f"<b>{len(evs_s)}</b>{money}")
    else:
        lines.append("\nNo deals reached a stage in this period.")

    fresh = new_deals(cfg, start, end, by_id)
    if fresh:
        lines.append(f"\n{NEW} <b>New deals: {len(fresh)}</b>")

    if n_moves and period in ("weekly", "monthly"):
        by_owner = {}
        for hits in moved.values():
            for oid in hits:
                k = bot.rep_of(ctx, by_id[oid])
                if bot.rep_hidden(k):
                    continue
                by_owner[k] = by_owner.get(k, 0) + 1
        top = sorted(by_owner.items(), key=lambda x: -x[1])[:8]
        lines.append("\n<b>By rep</b>\n   " +
                     " \u00b7 ".join(f"{html.escape(k)} {v}" for k, v in top))

    order = list(STAGE_ORDER) + [s for s in counts if s not in STAGE_ORDER]
    lines.append(f"\n<b>Pipeline now</b> \u2014 {total} deals")
    for s in order:
        lines.append(f"{STAGE_EMOJI.get(s, BULLET)} {stage_label(s)}: "
                     f"<b>{counts.get(s, 0)}</b> \u00b7 {fmt_money(values.get(s) or 0)}")
    open_val = sum(v for k, v in values.items() if k != "CLOSED_WON")
    lines.append(f"Open pipeline value: <b>{fmt_money(open_val)}</b>")
    lines.append(f"Closed won to date: <b>{counts.get('CLOSED_WON', 0)} \u00b7 "
                 f"{fmt_money(values.get('CLOSED_WON') or 0)}</b>")

    if cfg.get("show_appointments", True):
        lines.append("")
        lines.append(appointments_line(cfg))

    return "\n".join(lines)
