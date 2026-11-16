"""Battle plans: named squads (rally leader, joiners, target, time) for Castle, KvK or Bear Trap,
plus a shareable PNG of the whole plan."""
import io
from datetime import datetime, timezone

from .alert_card import FONTS, Image, ImageDraw, _font, _mix

PLAN_KINDS = {
    "kvk":    {"label": "KvK", "emoji": "⚔️", "color": [255, 69, 58]},
    "castle": {"label": "Castle", "emoji": "🏰", "color": [255, 204, 0]},
    "bear":   {"label": "Bear Trap", "emoji": "🐻", "color": [255, 159, 10]},
    "other":  {"label": "Other", "emoji": "📋", "color": [10, 132, 255]},
}
MAX_SQUADS, MAX_JOINERS = 20, 30


def _clean(v, n=60):
    return str(v or "").strip()[:n]


def validate(raw: dict) -> dict:
    """Keep only expected fields, trimmed to sensible sizes."""
    squads = []
    for sq in (raw.get("squads") or [])[:MAX_SQUADS]:
        joiners = sq.get("joiners") or []
        if isinstance(joiners, str):
            joiners = joiners.splitlines()
        joiners = [_clean(j, 40) for j in joiners if _clean(j, 40)][:MAX_JOINERS]
        squad = {"name": _clean(sq.get("name"), 40) or f"Squad {len(squads) + 1}",
                 "leader": _clean(sq.get("leader"), 40), "target": _clean(sq.get("target"), 60),
                 "time": _clean(sq.get("time"), 20), "notes": _clean(sq.get("notes"), 200), "joiners": joiners}
        squads.append(squad)
    when = raw.get("when")
    try:
        when = int(when) if when else None
    except (TypeError, ValueError):
        when = None
    return {"when": when, "notes": _clean(raw.get("notes"), 500), "squads": squads}


def render_plan(name: str, kind: str, data: dict, brand: str = "URSA-KINGSHOT", footer: str = "") -> bytes | None:
    if Image is None:
        return None
    k = PLAN_KINDS.get(kind, PLAN_KINDS["other"])
    accent = tuple(k["color"])
    squads = data.get("squads") or []
    cols = 3 if len(squads) > 4 else max(1, min(len(squads), 2)) if squads else 1
    W, pad, gap = 1400, 56, 22
    card_w = (W - 2 * pad - (cols - 1) * gap) // cols

    def card_h(sq):
        return 150 + 34 * max(1, len(sq["joiners"])) + (40 if sq.get("notes") else 0)

    rows = [squads[i:i + cols] for i in range(0, len(squads), cols)] or [[]]
    head = 210 + (46 if data.get("notes") else 0)
    body = sum(max((card_h(sq) for sq in r), default=120) + gap for r in rows)
    H = head + body + 80

    img = Image.new("RGB", (W, H), (13, 15, 24))
    d = ImageDraw.Draw(img)
    # header glow band
    for y in range(head):
        d.line([(0, y), (W, y)], fill=_mix((13, 15, 24), accent, 0.20 * (1 - y / head)))
    d.rectangle([0, 0, 8, H], fill=accent)

    tag = f"{k['label'].upper()} PLAN · {len(squads)} SQUAD{'S' if len(squads) != 1 else ''}"
    f = _font("InterDisplay-SemiBold.otf", 9)
    tw = d.textlength(tag, font=f)
    d.rounded_rectangle([pad, 46, pad + tw + 36, 46 + 42], radius=21, fill=_mix((13, 15, 24), accent, .2),
                        outline=_mix((13, 15, 24), accent, .7), width=2)
    d.text((pad + 18, 67), tag, font=f, fill=_mix((255, 255, 255), accent, .4), anchor="lm")
    title_font = _font("InterDisplay-ExtraBold.otf", 30)
    title = name
    while d.textlength(title, font=title_font) > W - 2 * pad and len(title) > 4:
        title = title[:-2]
    d.text((pad, 160), title if title == name else title + "…", font=title_font, fill=(255, 255, 255), anchor="ls")
    if data.get("when"):
        when = datetime.fromtimestamp(int(data["when"]), timezone.utc)
        d.text((W - pad, 160), f"{when:%a %d %b · %H:%M} UTC", font=_font("Inter-Medium.otf", 13),
               fill=(190, 196, 214), anchor="rs")
    if data.get("notes"):
        d.text((pad, 200), data["notes"][:140], font=_font("Inter-Medium.otf", 11), fill=(168, 174, 194), anchor="ls")

    y = head
    for r in rows:
        rh = max((card_h(sq) for sq in r), default=120)
        for i, sq in enumerate(r):
            x = pad + i * (card_w + gap)
            d.rounded_rectangle([x, y, x + card_w, y + rh], radius=22, fill=(22, 25, 38), outline=(42, 46, 64), width=2)
            d.rounded_rectangle([x, y, x + card_w, y + 8], radius=4, fill=accent)
            d.text((x + 24, y + 48), sq["name"][:28], font=_font("InterDisplay-ExtraBold.otf", 15),
                   fill=(255, 255, 255), anchor="ls")
            meta = "  ·  ".join(t for t in (sq.get("target"), sq.get("time")) if t)
            if meta:
                d.text((x + 24, y + 80), meta[:40], font=_font("Inter-Medium.otf", 10), fill=accent, anchor="ls")
            d.text((x + 24, y + 120), "LEADER", font=_font("InterDisplay-SemiBold.otf", 8), fill=(120, 126, 148), anchor="ls")
            d.text((x + 110, y + 120), sq.get("leader") or "-", font=_font("Inter-Bold.otf", 12),
                   fill=(255, 255, 255), anchor="ls")
            jy = y + 160
            for n, j in enumerate(sq["joiners"] or ["(no joiners yet)"], 1):
                d.text((x + 24, jy), f"{n:>2}" if sq["joiners"] else "", font=_font("Inter-Medium.otf", 10),
                       fill=(120, 126, 148), anchor="ls")
                d.text((x + 64, jy), j[:30], font=_font("Inter-Medium.otf", 11), fill=(220, 223, 232), anchor="ls")
                jy += 34
            if sq.get("notes"):
                d.text((x + 24, jy + 6), sq["notes"][:46], font=_font("Inter-Medium.otf", 10),
                       fill=(168, 174, 194), anchor="ls")
        y += rh + gap

    d.rectangle([0, H - 64, W, H], fill=(20, 22, 33))
    d.ellipse([pad, H - 43, pad + 22, H - 21], fill=accent)
    d.text((pad + 36, H - 32), brand.upper(), font=_font("InterDisplay-SemiBold.otf", 11), fill=(235, 237, 245), anchor="lm")
    if footer:
        d.text((W - pad, H - 32), footer, font=_font("Inter-Medium.otf", 10), fill=(120, 126, 148), anchor="rm")

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
