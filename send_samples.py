#!/usr/bin/env python3
"""Send one of every notification and report to the Telegram chat, rendered
from real deals, each marked as a sample. For reviewing the formats:

    python3 send_samples.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot, reports, twenty_api as tw

TAG = "\U0001f9ea <i>sample — not a real move</i>\n"


def main():
    cfg, tg = bot.load_config(), bot.tg_config()
    caches = ({}, {})
    opps = [o for o in tw.opportunities() if bot.watched(o, cfg)]

    def pick(stage):
        return next((o for o in opps if o.get("stage") == stage), opps[0])

    won, sent, recv = pick("CLOSED_WON"), pick("QUOTE_SENT"), pick("QUOTE_RECEIVED")
    ctx = bot.build_ctx([won, sent, recv], caches)

    samples = [
        bot.stage_change_msg(cfg, sent, "MEETING_BOOKED", "QUOTE_RECEIVED", ctx),
        bot.stage_change_msg(cfg, recv, "QUOTE_RECEIVED", "QUOTE_SENT", ctx),
        bot.stage_change_msg(cfg, sent, "QUOTE_SENT", "PRODUCER_AGREEMENT_SIGNED", ctx),
        bot.stage_change_msg(cfg, won, "PRODUCER_AGREEMENT_SIGNED", "CLOSED_WON", ctx),
        bot.stage_change_msg(cfg, sent, "CLOSED_WON", "QUOTE_SENT", ctx),      # moved back
        bot.new_deal_msg(cfg, recv, ctx),
        bot.removed_msg({"name": won.get("name"), "pipeline": "ES_CARRIER",
                         "stage": "QUOTE_SENT"}),
    ]
    for msg in samples:
        bot.send(tg, TAG + msg)

    for period in ("daily", "weekly", "monthly"):
        bot.send(tg, TAG + reports.report(cfg, period, caches))
    bot.send(tg, TAG + bot.cmd_pipeline(cfg, caches))
    bot.send(tg, TAG + bot.cmd_recent(cfg, caches, 5))
    bot.send(tg, TAG + bot.cmd_status(cfg, bot.load_json(bot.STATE_FILE, {})))
    bot.send(tg, bot.HELP)
    print("sent 14 samples")


if __name__ == "__main__":
    main()
