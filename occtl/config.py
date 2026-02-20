from __future__ import annotations

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "occtl"
MAPPINGS_FILE = CONFIG_DIR / "mappings.toml"
STATE_FILE = CONFIG_DIR / "state.json"


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not MAPPINGS_FILE.exists():
        MAPPINGS_FILE.write_text("[map]\n", encoding="utf-8")
    if not STATE_FILE.exists():
        STATE_FILE.write_text(
            json.dumps(
                {
                    "focus": "",
                    "webhook_url": "",
                    "alert_router_url": "",
                    "relay_token": "",
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def load_state() -> dict:
    ensure_config_dir()
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    ensure_config_dir()
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def set_focus(name: str) -> None:
    state = load_state()
    state["focus"] = name
    save_state(state)


def get_focus() -> str:
    state = load_state()
    return (state.get("focus") or "").strip()


def set_webhook(url: str) -> None:
    state = load_state()
    state["webhook_url"] = url.strip()
    save_state(state)


def get_webhook() -> str:
    state = load_state()
    return (state.get("webhook_url") or "").strip()


def set_alert_router(url: str) -> None:
    state = load_state()
    state["alert_router_url"] = url.strip()
    save_state(state)


def get_alert_router() -> str:
    state = load_state()
    return (state.get("alert_router_url") or "").strip()


def set_relay_token(token: str) -> None:
    state = load_state()
    state["relay_token"] = token.strip()
    save_state(state)


def get_relay_token() -> str:
    state = load_state()
    return (state.get("relay_token") or "").strip()


def _parse_toml_map(text: str) -> dict[str, str]:
    """
    Minimal TOML parsing for:
    [map]
    key = "value"
    """

    in_map = False
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[map]":
            in_map = True
            continue
        if line.startswith("[") and line.endswith("]") and line != "[map]":
            in_map = False
            continue
        if not in_map:
            continue
        if "=" not in line:
            continue
        k, v = [x.strip() for x in line.split("=", 1)]
        if v.startswith(("'", '"')) and v.endswith(("'", '"')) and len(v) >= 2:
            v = v[1:-1]
        out[k] = v
    return out


def load_mappings() -> dict[str, str]:
    ensure_config_dir()
    return _parse_toml_map(MAPPINGS_FILE.read_text(encoding="utf-8"))


def write_mappings(m: dict[str, str]) -> None:
    ensure_config_dir()
    lines = ["[map]"]
    for k in sorted(m.keys()):
        v = m[k].replace('"', '\\"')
        lines.append(f'{k} = "{v}"')
    MAPPINGS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_mapping(name: str, path: str) -> None:
    m = load_mappings()
    m[name] = str(Path(path).expanduser().resolve())
    write_mappings(m)


def get_mapping(name: str) -> str | None:
    m = load_mappings()
    return m.get(name)
