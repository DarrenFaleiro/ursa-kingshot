"""Kingdom unlock reminders: what unlocks at which kingdom age, and when to announce it.

Kingshot content unlocks by server (kingdom) age, counted from the kingdom's open date. The schedule
below comes from public community guides (kingshotguide.com, kingshot.net, kingshotmastery.com).
Guides disagree by a few days, so each unlock has a day range and posts say "around".
Reminders post at 12:00 UTC on the chosen day(s) before the start of each range.
"""
import math
import time
from datetime import datetime, timedelta, timezone

CATEGORIES = {
    "heroes":   {"label": "Heroes",   "emoji": "🦸", "color": [255, 69, 95]},
    "pets":     {"label": "Pets",     "emoji": "🐾", "color": [48, 209, 88]},
    "truegold": {"label": "Truegold", "emoji": "🪙", "color": [255, 204, 0]},
    "buildings": {"label": "Buildings", "emoji": "🏛️", "color": [255, 159, 10]},
    "pvp":      {"label": "PvP",      "emoji": "⚔️", "color": [191, 90, 242]},
    "features": {"label": "Features", "emoji": "✨", "color": [10, 132, 255]},
}

# key, category, day_from, day_to, title, details
SCHEDULE = [
    ("plains-fog", "features", 14, 14, "Plains fog lifted", "More of the map opens up"),
    ("fertile-fog", "features", 38, 38, "Fertile Land fog lifted", "The Fertile Land opens up"),
    ("gen2-heroes", "heroes", 40, 50, "Generation 2 heroes", "Save hero summons and gems"),
    ("res-exchange", "features", 45, 45, "Alliance Resource Exchange", ""),
    ("castle-1", "pvp", 54, 54, "First Castle Battle", "Prepare troops and healing speedups"),
    ("gen1-pets", "pets", 55, 60, "Generation 1 pets", "Gray Wolf, Lynx, Bison"),
    ("truegold", "truegold", 70, 70, "Age of Truegold", "Truegold items become available"),
    ("kvk-eligible", "pvp", 70, 80, "Kingdom of Power (KvK) eligibility", "Your kingdom can be matched for KvK"),
    ("gen2-pets", "pets", 75, 80, "Generation 2 pets", "Cheetah, Moose"),
    ("gen3-pets", "pets", 105, 110, "Generation 3 pets", "Lion, Grizzly Bear"),
    ("gen3-heroes", "heroes", 105, 120, "Generation 3 heroes", "Eric, Petra, Jaeger"),
    ("truegold-5", "truegold", 150, 150, "Truegold Lv. 5 & Truegold Crucible", ""),
    ("artisans", "features", 170, 170, "Artisan's Vision & Vikings Vengeance Lv. 12-20", "Alliance Championship store"),
    ("gen4", "heroes", 190, 200, "Generation 4 heroes & pets", "Giant Rhino, Mighty Bison"),
    ("war-academy", "buildings", 220, 225, "War Academy", "The big resource gate - stock up"),
    ("gen5", "heroes", 270, 280, "Generation 5 heroes & pets", "Long Fei, Thrud, Vivian · Great Moose, Alpha Black Panther"),
    ("truegold-8", "truegold", 310, 320, "Tempered Truegold (Lv. 8)", ""),
    ("gen6", "heroes", 350, 360, "Generation 6 heroes & pets", "Regal White Lion, Ironclad War Elephant"),
    ("gen7", "heroes", 440, 450, "Generation 7 heroes & pet", "Ironclad War Bear"),
]

LEAD_CHOICES = [30, 14, 7, 3, 1, 0]          # days before; 0 = on the day
POST_HOUR = 12                               # 12:00 UTC, like other kingdom tools
GRACE = 12 * 3600                            # if the bot was off at 12:00, still post later that day

DEFAULTS = {"enabled": False, "channel_id": None, "kingdom": "", "open_date": None,
            "leads": [1], "categories": list(CATEGORIES), "role_id": None}


def open_ts(open_date: str) -> int:
    d = datetime.strptime(open_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp())


def open_date_from_day(current_day: int, now: float | None = None) -> str:
    """If you only know 'we're on day 123', work out the open date."""
    today = datetime.fromtimestamp(now or time.time(), timezone.utc).date()
    return (today - timedelta(days=int(current_day))).isoformat()


def kingdom_day(open_date: str, now: float | None = None) -> int:
    return int(((now or time.time()) - open_ts(open_date)) // 86400)


def unlock_list(open_date: str, now: float | None = None, categories=None) -> list[dict]:
    """Every unlock with its real dates for this kingdom, and whether it has passed."""
    now = now or time.time()
    base = open_ts(open_date)
    out = []
    for key, cat, d1, d2, title, details in SCHEDULE:
        if categories is not None and cat not in categories:
            continue
        start = base + d1 * 86400
        out.append({"key": key, "cat": cat, "day_from": d1, "day_to": d2, "title": title, "details": details,
                    "start": start, "end": base + d2 * 86400, "days_left": max(0, math.ceil((start - now) / 86400)),
                    "passed": base + d2 * 86400 + 86400 <= now, "live": start <= now < base + d2 * 86400 + 86400,
                    **CATEGORIES[cat]})
    return out


def next_unlock(settings: dict, now: float | None = None) -> dict | None:
    if not settings.get("open_date"):
        return None
    upcoming = [u for u in unlock_list(settings["open_date"], now, settings.get("categories")) if not u["passed"] and not u["live"]]
    return upcoming[0] if upcoming else None


def due_posts(settings: dict, sent: set, now: float) -> list[tuple[dict, int, str]]:
    """Reminders that should be posted now: (unlock, lead_days, sent_key)."""
    if not (settings.get("enabled") and settings.get("open_date") and settings.get("channel_id")):
        return []
    out = []
    for u in unlock_list(settings["open_date"], now, settings.get("categories")):
        for lead in settings.get("leads") or [1]:
            day = datetime.fromtimestamp(u["start"], timezone.utc).date() - timedelta(days=int(lead))
            fire = int(datetime(day.year, day.month, day.day, POST_HOUR, tzinfo=timezone.utc).timestamp())
            key = f"{u['key']}:{lead}"
            if key not in sent and fire <= now < fire + GRACE:
                out.append((u, int(lead), key))
    return out


def card_for(u: dict, kingdom: str, now: float | None = None) -> dict:
    """Details for the unlock picture (see alert_card.render_card)."""
    now = now or time.time()
    days = max(0, math.ceil((u["start"] - now) / 86400))
    rng = f"Day {u['day_from']}" if u["day_from"] == u["day_to"] else f"Day {u['day_from']}-{u['day_to']}"
    when = datetime.fromtimestamp(u["start"], timezone.utc).strftime("%b %d, %Y")
    return {"title": u["title"], "tag": f"KINGDOM {kingdom or '?'} · {u['label'].upper()}",
            "subtitle": f"{when} · {rng}", "detail": u["details"], "color": u["color"],
            "big": "NOW" if days == 0 else str(days), "unit": "TODAY" if days == 0 else ("DAY" if days == 1 else "DAYS"),
            "progress": 1.0 if days == 0 else max(0.06, 1 - days / 30), "occ": u["start"],
            "minutes_before": days * 1440, "max_before": 30 * 1440}


def message_for(u: dict, lead: int, kingdom: str) -> str:
    when = "today" if lead == 0 else ("tomorrow" if lead == 1 else f"in about {lead} days")
    rng = f"day {u['day_from']}" if u["day_from"] == u["day_to"] else f"days {u['day_from']}-{u['day_to']}"
    text = f"{u['emoji']} **{u['title']}** unlocks {when} in kingdom #{kingdom or '?'} (around {rng}, <t:{u['start']}:D>)."
    if u["details"]:
        text += f"\n{u['details']}"
    return text
