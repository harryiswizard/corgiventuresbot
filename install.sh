#!/bin/bash
# Run the bot as a resident launchd agent on this Mac (instant command replies).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.harrypalmer.twentybot.plist"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.harrypalmer.twentybot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$HERE/bot.py</string>
        <string>serve</string>
    </array>
    <key>WorkingDirectory</key><string>$HERE</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>$HERE/bot.log</string>
    <key>StandardErrorPath</key><string>$HERE/bot.err</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/com.harrypalmer.twentybot" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Loaded com.harrypalmer.twentybot. Logs: $HERE/bot.log"
