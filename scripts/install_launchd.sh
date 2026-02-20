#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH_SRC="${ROOT}/launchd/com.jason.occtl.watch.plist"
WATCH_DST="${HOME}/Library/LaunchAgents/com.jason.occtl.watch.plist"
RELAY_SRC="${ROOT}/launchd/com.jason.occtl.relay.plist"
RELAY_DST="${HOME}/Library/LaunchAgents/com.jason.occtl.relay.plist"

mkdir -p "${HOME}/Library/LaunchAgents"
cp "$WATCH_SRC" "$WATCH_DST"
cp "$RELAY_SRC" "$RELAY_DST"

launchctl unload "$WATCH_DST" >/dev/null 2>&1 || true
launchctl load -w "$WATCH_DST"

launchctl unload "$RELAY_DST" >/dev/null 2>&1 || true
launchctl load -w "$RELAY_DST"

echo "Loaded launchd job: com.jason.occtl.watch"
echo "Logs: /tmp/occtl-watch.out /tmp/occtl-watch.err"
echo "Loaded launchd job: com.jason.occtl.relay"
echo "Logs: /tmp/occtl-relay.out /tmp/occtl-relay.err"
