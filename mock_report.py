#!/usr/bin/env python3
"""Render /weekly and /monthly against invented data, to see the shape of a
busy period. Sends to Telegram with `--send`; otherwise prints.

    python3 mock_report.py [--send]
"""
import random, sys, os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot, reports

REPS = ["Zarret Mills", "Kristian Fraser", "Ayush Chavan", "Malik Djotni",
        "Joshua Byrne", "Dinesh Sharma", "Harry Palmer"]


def micros(dollars):
    return {"amountMicros": str(int(dollars * 1_000_000)), "currencyCode": "USD"}


def fake_events(cfg, days, submissions, quotes, wins, appointments):
    """One event per movement, spread over the period."""
    now = bot.now_local(cfg)
    evs, deals = [], {}
    def ev(kind, to, frm=None, value=None):
        did = f"deal-{len(deals)}"
        when = now - timedelta(days=random.uniform(0, days - 0.2),
                               hours=random.uniform(0, 8))
        rec = {"ts": when.isoformat(timespec="seconds"), "kind": kind, "id": did,
               "name": f"Agency {len(deals)}", "pipeline": "ES_CARRIER",
               "from": frm, "to": to, "owner": random.choice(REPS),
               "amount_value": value, "currency": "USD"}
        deals[did] = micros(value) if value else {}
        evs.append(rec)

    for _ in range(submissions):
        ev("stage", "QUOTE_RECEIVED", "QUOTE_RECEIVED")
    for _ in range(quotes):
        ev("stage", "QUOTE_SENT", "QUOTE_RECEIVED", round(random.uniform(8, 140)) * 1000)
    for _ in range(wins):
        ev("stage", "CLOSED_WON", "QUOTE_SENT", round(random.uniform(12, 90)) * 1000)
    for i in range(appointments):
        when = now - timedelta(days=random.uniform(0, days - 0.2))
        evs.append({"ts": when.isoformat(timespec="seconds"), "kind": "appointed",
                    "id": f"co-{i}", "name": f"Agency {i}",
                    "by": random.choice(REPS), "state": "California"})
    return evs, deals


def mock(cfg, period, days, numbers, snapshot, appointed_total, recent):
    evs, deals = fake_events(cfg, days, *numbers)
    reports.appointments_line = lambda c: (
        f"\U0001f91d <b>Appointed agencies: {appointed_total}</b>\n"
        f"   {recent[0]} in the last 7 days \u00b7 {recent[1]} in the last 30")
    reports.load_events = lambda c, s, e: [dict(x, _ts=datetime.fromisoformat(x["ts"]))
                                           for x in evs]
    reports.snapshot = lambda c: snapshot(deals)
    return reports.report(cfg, period)


def main():
    random.seed(7)
    cfg = bot.load_config()

    week_snapshot = lambda deals: (
        {"QUOTE_RECEIVED": 64, "QUOTE_SENT": 21, "CLOSED_WON": 11}, 96,
        {"QUOTE_RECEIVED": 1_180_000, "QUOTE_SENT": 1_640_000, "CLOSED_WON": 487_500},
        {f"deal-{i}": {"id": f"deal-{i}", "amount": v} for i, v in
         enumerate(deals.values())})
    month_snapshot = lambda deals: (
        {"QUOTE_RECEIVED": 131, "QUOTE_SENT": 38, "CLOSED_WON": 29}, 198,
        {"QUOTE_RECEIVED": 2_460_000, "QUOTE_SENT": 3_120_000, "CLOSED_WON": 1_284_000},
        {f"deal-{i}": {"id": f"deal-{i}", "amount": v} for i, v in
         enumerate(deals.values())})

    out = [mock(cfg, "weekly", 7, (23, 14, 6, 9), week_snapshot, 74, (9, 31)),
           mock(cfg, "monthly", 30, (88, 51, 24, 31), month_snapshot, 74, (9, 31))]

    if "--send" in sys.argv:
        tg = bot.tg_config()
        for m in out:
            bot.send(tg, "\U0001f9ea <i>mock-up — invented numbers</i>\n" + m)
        print("sent")
    else:
        import re
        for m in out:
            print(re.sub(r"</?b>|</?i>", "", m).replace("&amp;", "&"))
            print("\n" + "=" * 40 + "\n")


if __name__ == "__main__":
    main()
