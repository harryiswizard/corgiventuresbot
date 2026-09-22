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


def fake_ctx():
    """Company and member lookups the cards render from."""
    companies = {
        "co-1": {"id": "co-1", "name": "Customer First Insurance",
                 "leadType": "INSURANCE_BROKER", "region": "Pennsylvania",
                 "address": {"addressState": "Pennsylvania"},
                 "domainName": {"primaryLinkUrl": "customerfirstins.com"},
                 "phone": {"primaryPhoneNumber": "2155550147"},
                 "updatedAt": bot.now_local(bot.load_config()).isoformat(),
                 "updatedBy": {"name": "Eddie Graczyk"}},
    }
    return {"members": {"m-ae": "Karson Kitchen", "m-bdr": "Abush Jones"},
            "companies": companies}


def fake_deal(name, stage, dollars=None, days_out=8):
    now = datetime.now()
    return {"id": "11111111-2222-3333-4444-555555555555", "name": name,
            "stage": stage, "pipeline": "ES_CARRIER", "companyId": "co-1",
            "ownerId": "m-ae", "bdrId": "m-bdr", "dealSource": "COLD_CALL",
            "amount": micros(dollars) if dollars else {},
            "updatedAt": now.isoformat(),
            "closeDate": (now + timedelta(days=days_out)).isoformat()}


def mock_cards(cfg):
    """One of every notification, with invented but realistic detail."""
    ctx = fake_ctx()
    submitted = fake_deal("113 W Girard LLC", "QUOTE_RECEIVED")
    quoted = fake_deal("113 W Girard LLC", "QUOTE_SENT", 48_500)
    won = fake_deal("113 W Girard LLC", "CLOSED_WON", 43_080.63)
    return [
        bot.new_deal_msg(cfg, submitted, ctx),
        bot.stage_change_msg(cfg, quoted, "QUOTE_RECEIVED", "QUOTE_SENT", ctx),
        bot.stage_change_msg(cfg, won, "QUOTE_SENT", "CLOSED_WON", ctx),
        bot.stage_change_msg(cfg, submitted, "QUOTE_SENT", "QUOTE_RECEIVED", ctx),
        bot.company_card(cfg, ctx["companies"]["co-1"], appointed=True),
    ]


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

    day_snapshot = lambda deals: (
        {"QUOTE_RECEIVED": 64, "QUOTE_SENT": 21, "CLOSED_WON": 11}, 96,
        {"QUOTE_RECEIVED": 1_180_000, "QUOTE_SENT": 1_640_000, "CLOSED_WON": 487_500},
        {f"deal-{i}": {"id": f"deal-{i}", "amount": v} for i, v in
         enumerate(deals.values())})

    wanted = [a for a in sys.argv[1:] if not a.startswith("--")] or \
             ["daily", "weekly", "monthly"]
    if "cards" in wanted:
        out = mock_cards(cfg)
        if "--send" in sys.argv:
            tg = bot.tg_config()
            for m in out:
                bot.send(tg, "\U0001f9ea <i>mock-up \u2014 invented deal</i>\n" + m)
            print(f"sent {len(out)} cards")
        else:
            import re
            for m in out:
                print(re.sub(r"<a href=\"[^\"]+\">([^<]+)</a>", r"\1",
                             re.sub(r"</?b>|</?i>", "", m)).replace("&amp;", "&"))
                print("\n" + "-" * 40 + "\n")
        return
    plans = {
        "daily":   lambda: mock(cfg, "daily", 1, (6, 4, 2, 3), day_snapshot, 74, (9, 31)),
        "weekly":  lambda: mock(cfg, "weekly", 7, (23, 14, 6, 9), week_snapshot, 74, (9, 31)),
        "monthly": lambda: mock(cfg, "monthly", 30, (88, 51, 24, 31), month_snapshot, 74, (9, 31)),
    }
    out = [plans[w]() for w in wanted if w in plans]

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
