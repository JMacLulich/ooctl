#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="${ROOT}/launchd/com.jason.occtl.watch.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.jason.occtl.watch.plist"

mkdir -p "${HOME}/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"

launchctl unload "$PLIST_DST" >/dev/null 2>&1 || true
launchctl load -w "$PLIST_DST"

echo "Loaded launchd job: com.jason.occtl.watch"
echo "Logs: /tmp/occtl-watch.out /tmp/occtl-watch.err"
