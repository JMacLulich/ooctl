from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class VoiceIntent:
    action: str
    session: str | None = None
    text: str | None = None


SESSION_RE = r"[a-zA-Z0-9._:-]+"


def _normalize_session(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1].strip()
    return value


def parse_voice(phrase: str) -> VoiceIntent:
    p = (phrase or "").strip()
    lc = p.lower()

    # status
    if re.match(r"^(status|what('?s)?\s+my\s+status)\b", lc):
        return VoiceIntent(action="status")

    # list sessions
    if re.match(r"^(list|show)\s+(sessions|tmux|ai)\b", lc):
        return VoiceIntent(action="ls")

    # start <name>
    m = re.match(r"^start\s+(?P<name>.+)", lc)
    if m:
        return VoiceIntent(action="new", session=_normalize_session(m.group("name")))

    # switch to <name>
    m = re.match(r"^switch\s+to\s+(?P<name>.+)", lc)
    if m:
        return VoiceIntent(action="focus", session=_normalize_session(m.group("name")))

    # new session <name>
    m = re.match(r"^(new|create)\s+(session\s+)?(?P<name>.+)", lc)
    if m:
        return VoiceIntent(action="new", session=_normalize_session(m.group("name")))

    # attach/open/go to/focus <name>
    m = re.match(r"^(attach|open|go\s+to|focus)\s+(?P<name>.+)", lc)
    if m:
        verb = m.group(1)
        name = _normalize_session(m.group("name"))
        if verb == "focus":
            return VoiceIntent(action="focus", session=name)
        return VoiceIntent(action="attach_or_focus", session=name)

    # continue / enter
    if re.match(r"^(continue|enter|confirm|submit)$", lc):
        return VoiceIntent(action="enter")

    # tell <session> <text>
    m = re.match(rf"^tell\s+(?P<name>{SESSION_RE})\s+(?P<text>.+)$", p, flags=re.IGNORECASE)
    if m:
        return VoiceIntent(action="say", session=m.group("name"), text=m.group("text").strip())

    # default: say to focused
    return VoiceIntent(action="say", session=None, text=p)
