from __future__ import annotations

import contextlib
import json
import subprocess
import urllib.request


def mac_notify(title: str, body: str) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(
            ["/usr/bin/osascript", "-e", f'display notification "{body}" with title "{title}"'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def discord_webhook(webhook_url: str, content: str) -> None:
    if not webhook_url:
        return
    try:
        data = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        # best-effort only
        pass
