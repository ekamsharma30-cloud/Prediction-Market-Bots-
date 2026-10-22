#!/usr/bin/env bash
# Keeps the paper bot running: restarts it if it crashes, and (on macOS) stops the Mac
# from idle-sleeping while it runs. Ctrl-C twice to stop.
cd "$(dirname "$0")"
KEEP_AWAKE=""
command -v caffeinate >/dev/null 2>&1 && KEEP_AWAKE="caffeinate -i"
while true; do
  $KEEP_AWAKE python3 kalshi_mentions_bot.py run
  echo "bot exited - restarting in 15s (Ctrl-C to stop)"
  sleep 15
done
