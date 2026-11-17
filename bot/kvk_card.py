"""Shareable KvK summary picture: both kingdoms, preparation scores per day, result and record."""
import io

from .alert_card import Image, ImageDraw, _font, _mix
from .kvk import fmt

BLUE, RED, BG = (64, 156, 255), (255, 77, 94), (13, 15, 24)


def render_kvk(d: dict, prep: dict, record: dict, brand: str = "URSA-KINGSHOT", footer: str = "") -> bytes | None:
    if Image is None:
        return None
    W, H, pad = 1400, 900, 64
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)
    # split glow: blue left, red right
    for x in range(W):
        t = x / W
        col = _mix(_mix(BG, BLUE, 0.22 * (1 - t) ** 2), RED, 0.22 * t ** 2)
        dr.line([(x, 0), (x, 330)], fill=col)

    ours, theirs = f"#{d.get('our_kingdom') or '?'}", f"#{d.get('their_kingdom') or '?'}"
    big = _font("InterDisplay-ExtraBold.otf", 52)
    dr.text((W * 0.27, 120), ours, font=big, fill=BLUE, anchor="mm")
    dr.text((W * 0.73, 120), theirs, font=big, fill=RED, anchor="mm")
    dr.text((W * 0.27, 190), "YOU", font=_font("InterDisplay-SemiBold.otf", 11), fill=(190, 196, 214), anchor="mm")
    dr.text((W * 0.73, 190), "THEM", font=_font("InterDisplay-SemiBold.otf", 11), fill=(190, 196, 214), anchor="mm")
    dr.text((W / 2, 100), "VS", font=_font("InterDisplay-ExtraBold.otf", 22), fill=(255, 204, 0), anchor="mm")
    if d.get("kvk_date"):
        dr.text((W / 2, 150), d["kvk_date"], font=_font("Inter-Medium.otf", 12), fill=(190, 196, 214), anchor="mm")
    res = prep["result"]
    label = {"won": "Preparation won", "lost": "Preparation lost", "tie": "Preparation tied"}.get(res, "In progress")
    castle = d.get("castle_result", "pending")
    if castle in ("won", "lost"):
        label += f" · Castle {castle}"
    colr = (48, 209, 88) if res == "won" else RED if res == "lost" else (190, 196, 214)
    f = _font("InterDisplay-SemiBold.otf", 11)
    tw = dr.textlength(label, font=f)
    dr.rounded_rectangle([W / 2 - tw / 2 - 18, 180, W / 2 + tw / 2 + 18, 216], radius=18, fill=_mix(BG, colr, .2))
    dr.text((W / 2, 198), label, font=f, fill=colr, anchor="mm")

    # advantage bar
    bx0, bx1, by = pad + 120, W - pad - 120, 262
    share = prep["share"] / 100
    dr.rounded_rectangle([bx0, by, bx1, by + 14], radius=7, fill=RED)
    dr.rounded_rectangle([bx0, by, bx0 + (bx1 - bx0) * share, by + 14], radius=7, fill=BLUE)
    lead = prep["lead"]
    msg = (f"You lead {lead:+.1f}%" if lead is not None and lead >= 0 else
           f"They lead {(-lead / (1 + lead / 100)):.1f}%" if lead is not None else "No preparation scores yet")
    dr.text((W / 2, 305), f"{msg}  ·  {prep['won']}/{prep['entered'] or 5} days won", font=_font("Inter-Bold.otf", 12),
            fill=(235, 237, 245), anchor="mm")

    # totals
    dr.text((pad, 390), "PREPARATION", font=_font("InterDisplay-SemiBold.otf", 10), fill=(120, 126, 148), anchor="ls")
    dr.text((pad, 450), fmt(prep["ours"]), font=_font("InterDisplay-ExtraBold.otf", 30), fill=BLUE, anchor="ls")
    dr.text((W - pad, 450), fmt(prep["theirs"]), font=_font("InterDisplay-ExtraBold.otf", 30), fill=RED, anchor="rs")

    # day cards
    cw, gap, cy = (W - 2 * pad - 4 * 18) / 5, 18, 490
    for i, day in enumerate(prep["days"]):
        x = pad + i * (cw + gap)
        dr.rounded_rectangle([x, cy, x + cw, cy + 170], radius=18, fill=(22, 25, 38), outline=(42, 46, 64), width=2)
        dr.text((x + 20, cy + 38), day["name"], font=_font("Inter-Bold.otf", 11), fill=(235, 237, 245), anchor="ls")
        if day["done"]:
            up = day["lead"] is not None and day["lead"] >= 0
            dr.text((x + cw - 20, cy + 38), f"{'▲' if up else '▼'} {day['lead']:+.1f}%" if day["lead"] is not None else "",
                    font=_font("Inter-Bold.otf", 9), fill=(48, 209, 88) if up else RED, anchor="rs")
            dr.text((x + 20, cy + 92), fmt(day["ours"]), font=_font("InterDisplay-ExtraBold.otf", 13), fill=BLUE, anchor="ls")
            dr.text((x + cw - 20, cy + 92), fmt(day["theirs"]), font=_font("InterDisplay-ExtraBold.otf", 13), fill=RED, anchor="rs")
            dr.rounded_rectangle([x + 20, cy + 116, x + cw - 20, cy + 124], radius=4, fill=RED)
            dr.rounded_rectangle([x + 20, cy + 116, x + 20 + (cw - 40) * day["share"] / 100, cy + 124], radius=4, fill=BLUE)
        else:
            dr.text((x + cw / 2, cy + 100), "not entered", font=_font("Inter-Medium.otf", 10), fill=(120, 126, 148), anchor="mm")

    # record
    ry = 730
    rec = (f"KvK record ({record['played']} played):  Preparation {record['prep_w']}-{record['prep_l']}"
           f"   ·   Castle {record['castle_w']}-{record['castle_l']}")
    dr.text((pad, ry), rec, font=_font("Inter-Bold.otf", 12), fill=(235, 237, 245), anchor="ls")
    x = pad
    for r in record["recent"][-10:]:
        for kind, val in (("P", r["prep"]), ("C", r["castle"])):
            col = (48, 209, 88) if val == "won" else RED if val == "lost" else (60, 64, 82)
            dr.rounded_rectangle([x, ry + 24, x + 44, ry + 64], radius=10, fill=_mix(BG, col, .35), outline=col, width=2)
            dr.text((x + 22, ry + 44), kind, font=_font("Inter-Bold.otf", 10), fill=(255, 255, 255), anchor="mm")
            x += 52
        x += 14

    dr.rectangle([0, H - 64, W, H], fill=(20, 22, 33))
    dr.ellipse([pad, H - 43, pad + 22, H - 21], fill=(255, 204, 0))
    dr.text((pad + 36, H - 32), brand.upper(), font=_font("InterDisplay-SemiBold.otf", 11), fill=(235, 237, 245), anchor="lm")
    if footer:
        dr.text((W - pad, H - 32), footer, font=_font("Inter-Medium.otf", 10), fill=(120, 126, 148), anchor="rm")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
