"""Bear Trap / Arena / custom event reminders.

Each reminder has a first start time (UTC), an optional repeat interval, and a list of
"minutes before" to send alerts at, e.g. "30,10,0" = 30 min before, 10 min before, at start.

The loop checks every 30 seconds. `last_fire_ts` in the database remembers the last alert
sent, so nothing is sent twice - even after a restart. Alerts more than GRACE seconds late
(e.g. the bot was offline) are skipped instead of spamming old pings.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .db import DB

log = logging.getLogger("reminders")

GRACE = 300          # seconds; send late alerts only if they are at most 5 minutes late
TICK = 30            # seconds between checks

KINDS = {
    # label = full name, short = big title on the alert card, color = RGB accent
    "bear_trap":   {"label": "Bear Trap — Group 1", "short": "Bear Trap", "group": "Group 1", "emoji": "🐻",
                    "repeat_minutes": 48 * 60, "before": "30,10,0", "color": [255, 159, 10],
                    "line": "Get ready to rally."},
    "bear_trap_2": {"label": "Bear Trap — Group 2", "short": "Bear Trap", "group": "Group 2", "emoji": "🐻",
                    "repeat_minutes": 48 * 60, "before": "30,10,0", "color": [191, 90, 242],
                    "line": "Get ready to rally."},
    "arena":       {"label": "Arena reset", "short": "Arena", "group": "", "emoji": "⚔️",
                    "repeat_minutes": 24 * 60, "before": "60,15", "color": [10, 132, 255],
                    "line": "Use your remaining arena attacks."},
    "custom":      {"label": "Event", "short": "Event", "group": "", "emoji": "📅",
                    "repeat_minutes": 0, "before": "15,0", "color": [48, 209, 88], "line": ""},
}


def parse_before(text: str) -> list[int]:
    """'30, 10, 0' -> [30, 10, 0]  (largest first, no duplicates, no negatives)."""
    vals = set()
    for part in str(text).replace(" ", "").split(","):
        if part.isdigit():
            vals.add(int(part))
    return sorted(vals, reverse=True) or [0]


def due_alerts(rem, now: float) -> list[tuple[int, int, int]]:
    """Alerts that should be sent now: list of (fire_ts, occurrence_ts, minutes_before).

    An alert is due if fire_ts is after the last alert we sent, not in the future,
    and not more than GRACE seconds old.
    """
    befores = parse_before(rem["before_minutes"])
    start, period = int(rem["start_at"]), int(rem["repeat_minutes"]) * 60
    max_before = max(befores) * 60

    if period > 0:
        k = max(0, (int(now) + max_before - start) // period)
        occurrences = [start + i * period for i in (k - 1, k, k + 1) if i >= 0]
    else:
        occurrences = [start]

    out = []
    for occ in occurrences:
        for b in befores:
            fire = occ - b * 60
            if rem["last_fire_ts"] < fire <= now and now - fire <= GRACE:
                out.append((fire, occ, b))
    return sorted(out)


def next_occurrence(rem, now: float) -> int | None:
    start, period = int(rem["start_at"]), int(rem["repeat_minutes"]) * 60
    if start >= now:
        return start
    if period <= 0:
        return None
    k = (int(now) - start) // period + 1
    return start + k * period


def format_alert(rem, occ: int, minutes_before: int) -> str:
    kind = KINDS.get(rem["kind"], KINDS["custom"])
    name = rem["name"] or kind["label"]
    if minutes_before == 0:
        when = "is starting **now**!"
    elif minutes_before < 60 or minutes_before % 60:
        when = f"starts in **{minutes_before} minutes** (<t:{occ}:t>)"
    else:
        h = minutes_before // 60
        when = f"starts in **{h} hour{'s' if h > 1 else ''}** (<t:{occ}:t>)"
    text = f"{kind['emoji']} **{name}** {when}"
    if rem["message"]:
        text += f"\n{rem['message']}"
    return text


def parse_when(time_text: str, date_text: str | None = None) -> int:
    """'20:30' (+ optional '2026-11-20') in UTC -> unix seconds.
    Without a date, uses the next time that clock time comes round."""
    hh, mm = (int(x) for x in time_text.strip().split(":"))
    now = datetime.now(timezone.utc)
    if date_text:
        d = datetime.strptime(date_text.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(d.replace(hour=hh, minute=mm).timestamp())
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return int(t.timestamp())



def when_text(minutes_before: int) -> str:
    if minutes_before == 0:
        return "now"
    if minutes_before < 60 or minutes_before % 60:
        return f"{minutes_before} minutes"
    h = minutes_before // 60
    return f"{h} hour{'s' if h > 1 else ''}"


def build_alert(rem, occ: int, minutes_before: int) -> dict:
    """Everything needed for a rich alert: title, description, colour, card details."""
    kind = KINDS.get(rem["kind"], KINDS["custom"])
    name = rem["name"] or kind["label"]
    if minutes_before == 0:
        headline = f"{kind['emoji']} **{name}** is starting **now**!"
    else:
        headline = f"{kind['emoji']} **{name}** starts in **{when_text(minutes_before)}**!"
    extra = rem["message"] or kind["line"]
    desc = headline + (f" {extra}" if extra else "")
    desc += f"\n\n⏳ Starts <t:{occ}:R> · 🕒 <t:{occ}:t> your time"
    r, g, b = kind["color"]
    repeat = int(rem["repeat_minutes"] or 0)
    tags = [t for t in (kind["group"], f"every {repeat // 60} h" if repeat and repeat % 60 == 0 else
                        (f"every {repeat} min" if repeat else "one-off")) if t]
    return {
        "title": f"{kind['emoji']} {name}",
        "description": desc,
        "color": (r << 16) + (g << 8) + b,
        "occ": occ,
        "card": {"title": kind["short"] if rem["name"] in (kind["label"], kind["short"], None, "") else name,
                 "tag": " · ".join(tags).upper(), "occ": occ, "minutes_before": minutes_before,
                 "max_before": max(parse_before(rem["before_minutes"])), "color": kind["color"]},
    }


Sender = Callable[[object, int, int], Awaitable[bool]]   # (reminder_row, occurrence, minutes_before)


async def reminder_loop(db: DB, send: Sender):
    """Runs forever. `send` is provided by the Discord bot."""
    while True:
        now = time.time()
        for rem in db.list_reminders(only_enabled=True):
            try:
                alerts = due_alerts(rem, now)
                for fire, occ, before in alerts:
                    await send(rem, occ, before)
                if alerts:
                    db.update_reminder(rem["id"], last_fire_ts=alerts[-1][0])
                # one-off reminder that has fully passed -> switch it off
                if int(rem["repeat_minutes"]) == 0 and now > int(rem["start_at"]) + GRACE:
                    db.update_reminder(rem["id"], enabled=0)
            except Exception:
                log.exception("reminder %s failed", rem["id"])
        await asyncio.sleep(TICK)


def timeline(reminders, now: float, days: int = 7) -> list[dict]:
    """Every event start (and its alert times) in the next `days`, soonest first."""
    end = now + days * 86400
    out = []
    for rem in reminders:
        if not rem["enabled"]:
            continue
        start, period = int(rem["start_at"]), int(rem["repeat_minutes"]) * 60
        befores = parse_before(rem["before_minutes"])
        if period > 0:
            k = max(0, int((now - start) // period))
            occ = start + k * period
            while occ < now:
                occ += period
            occs = []
            while occ <= end and len(occs) < 50:
                occs.append(occ)
                occ += period
        else:
            occs = [start] if now <= start <= end else []
        for o in occs:
            out.append({"rem": rem, "occ": o, "alerts": [o - b * 60 for b in befores if o - b * 60 >= now]})
    return sorted(out, key=lambda e: e["occ"])
