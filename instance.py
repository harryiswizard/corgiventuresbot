#!/usr/bin/env python3
"""Which copy of the bot this process is.

The same code runs two bots: the E&S deal bot (the default) and the
Reinsurance deal bot. Each has its own config file, its own state and log
folders, and reads its own stage field on the opportunity:

    BOT_CONFIG     config file              default config.json
    BOT_STATE_DIR  state folder             default state/
    BOT_LOG_DIR    deal roster / history    default logs/
    BOT_TG_FILE    local Telegram creds     default ~/.telegram_twenty_bot

The stage field comes from `stage_field` in the config (default "stage").
The Reinsurance pipeline has its own SELECT, `reinsuranceStage`, so its deals
never share stage options with the other pipelines.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))


def _path(env, default):
    p = os.environ.get(env)
    if not p:
        return default
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(HERE, p)


CONFIG_FILE = _path("BOT_CONFIG", os.path.join(HERE, "config.json"))
STATE_DIR = _path("BOT_STATE_DIR", os.path.join(HERE, "state"))
LOG_DIR = _path("BOT_LOG_DIR", os.path.join(HERE, "logs"))
TG_FILE = os.path.expanduser(os.environ.get("BOT_TG_FILE") or "~/.telegram_twenty_bot")

try:
    with open(CONFIG_FILE) as f:
        STAGE_FIELD = json.load(f).get("stage_field") or "stage"
except (OSError, ValueError):
    STAGE_FIELD = "stage"
