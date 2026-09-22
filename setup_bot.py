#!/usr/bin/env python3
"""One-time Telegram wiring.

    python3 setup_bot.py <BOT_TOKEN>

Create the bot with @BotFather, send it any message, then run this. It finds
the chat that messaged the bot and writes ~/.telegram_twenty_bot.
To use a group: add the bot to the group, send it /chatid, and add the id to
`allowed_chats` in that file.
"""
import json, os, sys, urllib.parse, urllib.request

TG_FILE = os.path.expanduser("~/.telegram_twenty_bot")


def api(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params or {}).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python3 setup_bot.py <BOT_TOKEN>")
    token = sys.argv[1].strip()

    me = api(token, "getMe")
    if not me.get("ok"):
        sys.exit(f"Telegram rejected that token: {me}")
    username = me["result"]["username"]
    print(f"Connected to @{username}")

    updates = api(token, "getUpdates", {"timeout": 0}).get("result", [])
    chats = []
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") and chat["id"] not in [c["id"] for c in chats]:
            chats.append({"id": chat["id"],
                          "title": chat.get("title") or chat.get("first_name") or "?"})
    if not chats:
        sys.exit(f"No messages yet. Send @{username} any message in Telegram, then run this again.")

    print("Chats that have messaged the bot:")
    for c in chats:
        print(f"  {c['id']}  {c['title']}")

    cfg = {"bot_token": token, "chat_id": str(chats[0]["id"]),
           "allowed_chats": [str(c["id"]) for c in chats[1:]]}
    with open(TG_FILE, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(TG_FILE, 0o600)
    print(f"\nWrote {TG_FILE} (primary chat {cfg['chat_id']}).")

    import bot
    api(token, "setMyCommands", {"commands": json.dumps(
        [{"command": c, "description": d} for c, d in bot.COMMANDS])})
    print("Command menu registered. Next:  python3 bot.py seed  then  python3 bot.py serve")


if __name__ == "__main__":
    main()
