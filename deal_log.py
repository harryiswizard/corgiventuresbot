#!/usr/bin/env python3
"""Roster of every deal and the stage it sits on, plus a history of moves.

    python3 deal_log.py            write the log and print a summary
    python3 deal_log.py --quiet    write only (used by the bot each poll)

Writes:
    logs/deals.csv          one row per deal as it stands now, overwritten
    logs/stage_history.csv  appended: every time a deal's stage changes
"""
import csv, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twenty_api as tw
from stages import STAGE_ORDER, stage_label, stage_rank

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
DEALS_CSV = os.path.join(LOG_DIR, "deals.csv")
HISTORY_CSV = os.path.join(LOG_DIR, "stage_history.csv")

FIELDS = ["company", "deal", "stage", "owner", "bdr", "amount", "submitted",
          "pipeline", "created", "updated", "id"]


def money(amount):
    micros = (amount or {}).get("amountMicros")
    if micros in (None, ""):
        return ""
    try:
        return f"{int(micros) / 1_000_000:.2f}"
    except (TypeError, ValueError):
        return ""


def collect(opps=None):
    """Every opportunity, with its company and the people on it.

    `opps` lets the bot pass the deals it has just polled instead of fetching
    them again."""
    opps = tw.opportunities() if opps is None else opps
    companies = tw.companies_by_id({o.get("companyId") for o in opps if o.get("companyId")})
    members = tw.workspace_members()
    rows = []
    for o in opps:
        rows.append({
            "company": (companies.get(o.get("companyId")) or {}).get("name", ""),
            "deal": (o.get("name") or "").strip() or "(unnamed)",
            "stage": stage_label(o.get("stage")),
            "owner": members.get(o.get("ownerId")) or o.get("hubspotOwner") or "Unassigned",
            "bdr": members.get(o.get("bdrId")) or "Unassigned",
            "amount": money(o.get("amount")),
            "submitted": "yes" if o.get("submitted") else "no",
            "pipeline": o.get("pipeline") or "unset",
            "created": (o.get("createdAt") or "")[:10],
            "updated": (o.get("updatedAt") or "")[:16].replace("T", " "),
            "id": o.get("id"),
        })
    rows.sort(key=lambda r: (stage_rank(next((v for v in STAGE_ORDER
                                              if stage_label(v) == r["stage"]), "")),
                             r["company"].lower(), r["deal"].lower()))
    return rows


def previous_stages():
    """What the last run recorded, so only real moves are appended."""
    if not os.path.exists(DEALS_CSV):
        return {}
    with open(DEALS_CSV) as f:
        return {r["id"]: r["stage"] for r in csv.DictReader(f)}


def write(rows):
    os.makedirs(LOG_DIR, exist_ok=True)
    before = previous_stages()
    now = datetime.now().isoformat(timespec="seconds")

    moves = [r for r in rows if r["id"] in before and before[r["id"]] != r["stage"]]
    new = [r for r in rows if r["id"] not in before]
    if before:                       # first run is the baseline, not a move
        fresh = not os.path.exists(HISTORY_CSV)
        with open(HISTORY_CSV, "a", newline="") as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(["when", "company", "deal", "from", "to", "owner", "amount", "id"])
            for r in moves:
                w.writerow([now, r["company"], r["deal"], before[r["id"]], r["stage"],
                            r["owner"], r["amount"], r["id"]])
            for r in new:
                w.writerow([now, r["company"], r["deal"], "(new)", r["stage"],
                            r["owner"], r["amount"], r["id"]])

    with open(DEALS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return moves, new


def summary(rows):
    by_stage = {}
    for r in rows:
        by_stage.setdefault(r["stage"], []).append(r)
    order = [stage_label(v) for v in STAGE_ORDER]
    order += [s for s in by_stage if s not in order]
    out = [f"{len(rows)} deals"]
    for st in order:
        deals = by_stage.get(st, [])
        out.append(f"\n{st}: {len(deals)}")
        for r in deals:
            who = r["company"] or r["deal"]
            amt = f"  ${float(r['amount']):,.0f}" if r["amount"] else ""
            out.append(f"   {who[:46]:48} {r['owner'][:18]:20}{amt}")
    return "\n".join(out)


def main():
    rows = collect()
    moves, new = write(rows)
    if "--quiet" not in sys.argv:
        print(summary(rows))
        print(f"\nwritten: {DEALS_CSV}")
        if moves or new:
            print(f"history: {len(moves)} moved, {len(new)} new -> {HISTORY_CSV}")
    return rows


if __name__ == "__main__":
    main()
