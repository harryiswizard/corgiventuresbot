#!/usr/bin/env python3
"""/daily, /weekly and /monthly digests.

Movements come from the event log the poller appends to (state/events.jsonl).
Money is read live from Twenty rather than from the event, because an amount is
usually typed in after the deal has been moved.
"""
import html, json, os
from datetime import datetime, timedelta

import twenty_api as tw
from bot import amount_value, appointments_line, fmt_money
from stages import STAGE_ORDER, STAGE_EMOJI, stage_label, stage_rank

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(HERE, "state", "events.jsonl")

BULLET = "•"
ARROW = "→"
TROPHY = "\U0001f3c6"
OUTBOX = "\U0001f4e4"
NEW = "\U0001f195"
BIN = "\U0001f5d1"

PERIODS = {"daily": ("Today", 0), "weekly": ("Last 7 days", 7),
           "monthly": ("Last 30 days", 30)}
MAX_NAMES = 8          # deals listed per stage before the rest are summarised


def _tz(cfg):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg.get("timezone") or "America/New_York")
    except Exception:
        return None


def window(cfg, period):
    """(start, end, title) in the configured timezone."""
    now = datetime.now(_tz(cfg))
    title, days = PERIODS.get(period, PERIODS["daily"])
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = now.strftime("%A %-d %B")
    else:
        start = (now - timedelta(days=days)).replace(hour=0, minute=0,
                                                     second=0, microsecond=0)
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


def _watched(opp, cfg):
    p = opp.get("pipeline")
    if p is None:
        return bool(cfg.get("include_unassigned"))
    pipes = cfg.get("pipelines") or []
    return (not pipes) or (p in pipes)


def snapshot(cfg):
    """Deals per stage and their value right now, straight from Twenty."""
    opps = [o for o in tw.opportunities() if _watched(o, cfg)]
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


def _names(evs, by_id):
    """Deal names, each with its value, so the money is visible per deal."""
    out = []
    for e in evs[:MAX_NAMES]:
        live = by_id.get(e.get("id")) or {}
        val = amount_value(live.get("amount"))
        if val is None:
            val = e.get("amount_value") or 0.0
        out.append(f"{html.escape(e.get('name') or '(unnamed)')} "
                   f"{fmt_money(val, e.get('currency'))}")
    extra = len(evs) - len(out)
    return "\n   ".join(out) + (f"\n   +{extra} more" if extra > 0 else "")


def report(cfg, period, caches=None):
    start, end, title = window(cfg, period)
    evs = load_events(cfg, start, end)
    moves = [e for e in evs if e.get("kind") == "stage" and not e.get("bulk")]
    bulk = [e for e in evs if e.get("kind") == "stage" and e.get("bulk")]
    new = [e for e in evs if e.get("kind") == "new"]
    gone = [e for e in evs if e.get("kind") == "removed"]
    appointed = [e for e in evs if e.get("kind") == "appointed"]
    unappointed = [e for e in evs if e.get("kind") == "unappointed"]

    try:
        counts, total, values, by_id = snapshot(cfg)
    except tw.TwentyError as e:
        counts, total, values, by_id = {}, 0, {}, {}
        snapshot_error = str(e)
    else:
        snapshot_error = None

    label = {"daily": "Daily", "weekly": "Weekly",
             "monthly": "Monthly"}.get(period, "Report")
    lines = [f"<b>{label} — {title}</b>"]

    # Headline money: what was won, and what was quoted out, in the period.
    won_evs = [e for e in moves if e.get("to") == "CLOSED_WON"]
    sent_evs = [e for e in moves if e.get("to") == "QUOTE_SENT"]
    if won_evs:
        val, cur, missing = period_value(won_evs, by_id)
        lines.append(f"\n{TROPHY} <b>Closed won: {len(won_evs)} · "
                     f"{fmt_money(val, cur)}</b>"
                     + (f"\n   <i>{missing} with no amount set</i>" if missing else ""))
    if sent_evs:
        val, cur, missing = period_value(sent_evs, by_id)
        lines.append(f"{OUTBOX} <b>Quotes sent: {len(sent_evs)} · "
                     f"{fmt_money(val, cur)}</b>"
                     + (f"\n   <i>{missing} with no amount set</i>" if missing else ""))

    if bulk:
        lines.append(f"\n\u26a0\ufe0f <i>{len(bulk)} deal(s) moved by a pipeline edit in "
                     f"Twenty, not by a rep \u2014 left out of the counts below.</i>")

    who = []
    for e in appointed[:MAX_NAMES]:
        by = f" (by {html.escape(e['by'])})" if e.get("by") else ""
        who.append(f"{html.escape(e.get('name') or '(unnamed)')}{by}")
    extra = len(appointed) - len(who)
    block = f"\n\U0001f91d <b>Agencies appointed: {len(appointed)}</b>"
    if who:
        block += "\n   " + "\n   ".join(who) + (f"\n   +{extra} more" if extra > 0 else "")
    lines.append(block)
    if unappointed:
        lines.append(f"\u21a9 <b>Appointments removed: {len(unappointed)}</b>\n   "
                     + ", ".join(html.escape(e.get("name") or "(unnamed)")
                                 for e in unappointed[:MAX_NAMES]))

    if moves:
        by_stage = {}
        for e in moves:
            by_stage.setdefault(e.get("to"), []).append(e)
        lines.append(f"\n<b>Stage changes: {len(moves)}</b>")
        for s in sorted(by_stage, key=stage_rank):
            lines.append(f"{STAGE_EMOJI.get(s, BULLET)} {ARROW} {stage_label(s)}: "
                         f"<b>{len(by_stage[s])}</b>\n   {_names(by_stage[s], by_id)}")
    else:
        lines.append("\nNo stage changes in this period.")

    if new:
        lines.append(f"\n{NEW} <b>New deals: {len(new)}</b>\n   {_names(new, by_id)}")
    if gone:
        lines.append(f"\n{BIN} <b>Left the pipeline: {len(gone)}</b>"
                     f"\n   {_names(gone, by_id)}")

    if moves and period in ("weekly", "monthly"):
        by_owner = {}
        for e in moves:
            k = e.get("owner") or "Unassigned"
            by_owner[k] = by_owner.get(k, 0) + 1
        top = sorted(by_owner.items(), key=lambda x: -x[1])[:8]
        lines.append("\n<b>By owner</b>\n   " +
                     " · ".join(f"{html.escape(k)} {v}" for k, v in top))

        by_day = {}
        for e in moves:
            by_day[e["_ts"].date()] = by_day.get(e["_ts"].date(), 0) + 1
        lines.append("\n<b>By day</b>\n   " +
                     " · ".join(f"{d.strftime('%-d %b')} {by_day[d]}"
                                     for d in sorted(by_day)[-14:]))

    if snapshot_error:
        lines.append(f"\n(Pipeline snapshot unavailable: {html.escape(snapshot_error)})")
    else:
        order = list(STAGE_ORDER) + [s for s in counts if s not in STAGE_ORDER]
        lines.append(f"\n<b>Pipeline now</b> — {total} deals")
        for s in order:
            lines.append(f"{STAGE_EMOJI.get(s, BULLET)} {stage_label(s)}: "
                         f"<b>{counts.get(s, 0)}</b> · {fmt_money(values.get(s) or 0)}")
        open_val = sum(v for k, v in values.items() if k != "CLOSED_WON")
        lines.append(f"Open pipeline value: <b>{fmt_money(open_val)}</b>")
        if not any(values.values()):
            lines.append("<i>Every Amount in Twenty is blank, so these read $0 "
                         "until one is filled in.</i>")

    lines.append("")
    lines.append(appointments_line(cfg))

    if not os.path.exists(EVENTS_FILE):
        lines.append("\n<i>No event history yet — movements are logged from the "
                     "moment the bot started.</i>")
    return "\n".join(lines)
