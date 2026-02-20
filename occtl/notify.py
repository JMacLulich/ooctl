from __future__ import annotations

import contextlib
import json
import subprocess
import urllib.request
from datetime import datetime, timezone


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


def alert_router_webhook(
    alert_router_url: str,
    *,
    service_name: str,
    severity: str,
    status: str,
    host_name: str,
    message: str,
    fingerprint: str,
) -> None:
    if not alert_router_url:
        return
    try:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = {
            "source": "infra-health-agent",
            "event-type": "service.health",
            "severity": severity,
            "status": status,
            "host": {"name": host_name, "ip": "127.0.0.1"},
            "service": {"name": service_name, "kind": "other"},
            "check": {"name": "oc-watch", "observed-at": now, "message": message},
            "fingerprint": fingerprint,
            "meta": {"source": "occtl"},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            alert_router_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        # best-effort only
        pass
