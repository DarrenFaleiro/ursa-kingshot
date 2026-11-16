"""Fetch currently active gift codes from a public feed (default: kingshot.net).

The feed's exact JSON shape isn't documented, so we read it defensively:
any string under a 'code'-like key, inside any list, is treated as a code,
unless the same object says it is expired / inactive.
"""
from datetime import datetime, timezone

import aiohttp

EXPIRY_KEYS = ("expiresAt", "expires_at", "expiry", "expireAt", "validUntil")


def _is_past(value) -> bool:
    """True if value is a date/timestamp in the past. Unknown formats count as not past."""
    if value in (None, ""):
        return False
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e12 else value
            return ts < datetime.now(timezone.utc).timestamp()
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except (ValueError, OverflowError):
        return False

CODE_KEYS = ("code", "giftCode", "gift_code", "cdk", "name")
INACTIVE_FLAGS = (("isActive", False), ("active", False), ("is_expired", True),
                  ("isExpired", True), ("expired", True), ("status", "expired"))


def extract_codes(payload) -> list[str]:
    found: list[str] = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                if isinstance(item, str) and 3 <= len(item) <= 30 and " " not in item:
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(node, dict):
            if any(node.get(k) == v for k, v in INACTIVE_FLAGS):
                return
            if any(_is_past(node.get(k)) for k in EXPIRY_KEYS):
                return
            for key in CODE_KEYS:
                val = node.get(key)
                if isinstance(val, str) and val.strip():
                    found.append(val.strip())
                    return
            for val in node.values():
                if isinstance(val, (list, dict)):
                    walk(val)

    walk(payload)
    # de-duplicate, keep order
    return list(dict.fromkeys(found))


async def fetch_codes(url: str) -> list[str]:
    if not url:
        return []
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, headers={"User-Agent": "kingshot-bot"}) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
    return extract_codes(payload)
