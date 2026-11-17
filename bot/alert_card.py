"""Draws the picture attached to reminder alerts (a PNG made with Pillow).

    ┌───────────────────────────────────────────────────────┐
    │ [GROUP 2 · EVERY 48 H]                        ╭────╮   │
    │ Bear Trap                                    │ 15 │   │
    │ Starts at 17:00 UTC · Mon 16 Nov             │MIN │   │
    │ ███████████████░░░░░                          ╰────╯   │
    ├───────────────────────────────────────────────────────┤
    │ URSA-KINGSHOT                         your.website     │
    └───────────────────────────────────────────────────────┘
If Pillow isn't installed, alerts are still sent - just without the picture.
"""
import io
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:          # pragma: no cover
    Image = None

FONTS = Path(__file__).parent / "assets" / "fonts"
PORTRAITS = Path(__file__).parent / "assets" / "portraits"   # drop <name>.png here, e.g. long-fei.png
GOLD = (226, 178, 72)
W, H, FOOT = 1200, 520, 74
SCALE = 2   # draw at 2x then shrink = smooth edges


def _font(name: str, size: int):
    try:
        return ImageFont.truetype(str(FONTS / name), size * SCALE)
    except OSError:
        return ImageFont.load_default()


def _mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def slug(name: str) -> str:
    """'Long Fei' -> 'long-fei', 'Wee & Woo' -> 'wee-woo' (portrait file names)."""
    out = "".join(c if c.isalnum() else "-" for c in name.lower())
    return "-".join(p for p in out.split("-") if p)


def portrait_path(name: str):
    for ext in (".png", ".webp", ".jpg", ".jpeg"):
        f = PORTRAITS / (slug(name) + ext)
        if f.is_file():
            return f
    return None


def _draw_faces(img, d, names, x, y, accent):
    """Gold-framed square tiles: the portrait if you've added one, otherwise the initials."""
    s = SCALE
    size, gap = 80 * s, 26 * s
    f_init = _font("InterDisplay-ExtraBold.otf", 30)
    f_name = _font("InterDisplay-SemiBold.otf", 16)
    for i, name in enumerate(names[:5]):
        x0 = x + i * (size + gap)
        box = [x0, y, x0 + size, y + size]
        d.rounded_rectangle(box, radius=16 * s, fill=_mix((20, 22, 33), accent, 0.30), outline=GOLD, width=4 * s)
        pic = portrait_path(name)
        if pic:
            try:
                im = Image.open(pic).convert("RGB")
                side = min(im.size)
                im = im.crop(((im.width - side) // 2, 0, (im.width + side) // 2, side))   # keep the face (top)
                inner = size - 10 * s
                im = im.resize((inner, inner), Image.LANCZOS)
                mask = Image.new("L", (inner, inner), 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, inner - 1, inner - 1], radius=12 * s, fill=255)
                img.paste(im, (x0 + 5 * s, y + 5 * s), mask)
            except OSError:
                pic = None
        if not pic:
            initials = "".join(w[0] for w in name.replace("&", " ").split()[:2]).upper()
            d.text((x0 + size // 2, y + size // 2), initials, font=f_init, fill=_mix((255, 255, 255), accent, 0.25), anchor="mm")
        label = name
        while d.textlength(label, font=f_name) > size + gap - 6 * s and len(label) > 4:
            label = label[:-2] + "…"
        d.text((x0 + size // 2, y + size + 20 * s), label, font=f_name, fill=(225, 228, 238), anchor="mm")


def render_card(card: dict, footer: str = "", brand: str = "URSA-KINGSHOT") -> bytes | None:
    if Image is None:
        return None
    s = SCALE
    accent = tuple(card["color"])
    w, h, foot = W * s, H * s, FOOT * s

    # --- background: deep navy with a soft glow of the event colour on the right
    img = Image.new("RGB", (w, h), (13, 15, 24))
    glow = Image.new("RGB", (w, h), (13, 15, 24))
    gd = ImageDraw.Draw(glow)
    cx, cy = int(w * 0.80), int((h - foot) * 0.50)
    for i, r in enumerate(range(int(h * 0.95), 0, -18 * s)):
        t = 1 - r / (h * 0.95)
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_mix((13, 15, 24), accent, 0.42 * t ** 1.6))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(40 * s)), 1.0)
    d = ImageDraw.Draw(img)

    # faint rings behind the countdown
    for k in range(1, 4):
        r = int(150 * s + k * 46 * s)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=_mix((13, 15, 24), accent, 0.10), width=2 * s)

    pad = 64 * s
    up = 22 * s if card.get("faces") else 0     # lift the text a little to make room for portraits

    # --- tag pill
    tag = card.get("tag") or ""
    if tag:
        f = _font("InterDisplay-SemiBold.otf", 19)
        tw = d.textlength(tag, font=f)
        x0, y0 = pad, 58 * s - up // 2
        d.rounded_rectangle([x0, y0, x0 + tw + 36 * s, y0 + 44 * s], radius=22 * s,
                            fill=_mix((13, 15, 24), accent, 0.18), outline=_mix((13, 15, 24), accent, 0.65), width=2 * s)
        d.text((x0 + 18 * s, y0 + 22 * s), tag, font=f, fill=_mix((255, 255, 255), accent, 0.45), anchor="lm")

    # --- big title (shrinks if long)
    title = card["title"]
    size = 96
    while size > 48 and d.textlength(title, font=_font("InterDisplay-ExtraBold.otf", size)) > w * 0.56:
        size -= 6
    d.text((pad, 196 * s - up), title, font=_font("InterDisplay-ExtraBold.otf", size), fill=(255, 255, 255), anchor="ls")

    # --- start time
    occ = datetime.fromtimestamp(int(card["occ"]), timezone.utc)
    sub = card.get("subtitle") or f"Starts at {occ:%H:%M} UTC · {occ:%a %d %b}"
    d.text((pad, 252 * s - up), sub, font=_font("Inter-Medium.otf", 28), fill=(168, 174, 194), anchor="ls")
    if card.get("faces"):                      # optional row of hero / pet portraits with names
        _draw_faces(img, d, card["faces"], pad, 310 * s, accent)
    elif card.get("detail"):                   # optional extra line (e.g. hero names)
        detail = card["detail"]
        f_det = _font("Inter-Medium.otf", 22)
        while d.textlength(detail, font=f_det) > w * 0.55 and len(detail) > 10:
            detail = detail[:-4] + "…"
        d.text((pad, 352 * s), detail, font=f_det, fill=_mix((168, 174, 194), accent, 0.35), anchor="ls")

    # --- progress bar: fills up as the start gets closer
    mb, mx = int(card["minutes_before"]), max(int(card["max_before"]), 1)
    progress = card.get("progress") or (1.0 if mb == 0 else max(0.06, 1 - mb / (mx * 1.15)))
    bx0, by0, bx1 = pad, 300 * s - up * 2 + (6 * s if up else 0), int(w * 0.50)
    d.rounded_rectangle([bx0, by0, bx1, by0 + 12 * s], radius=6 * s, fill=(38, 42, 58))
    fill_to = bx0 + int((bx1 - bx0) * progress)
    layer = Image.new("RGB", (w, h))
    ld = ImageDraw.Draw(layer)
    for x in range(bx0, fill_to, 2 * s):      # gradient: accent -> lighter
        t = (x - bx0) / max(bx1 - bx0, 1)
        ld.rectangle([x, by0, x + 2 * s, by0 + 12 * s], fill=_mix(accent, (255, 255, 255), 0.35 * t))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([bx0, by0, fill_to, by0 + 12 * s], radius=6 * s, fill=255)
    img.paste(layer, mask=mask)
    d = ImageDraw.Draw(img)

    # --- countdown ring
    r = 132 * s
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(16, 18, 28))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(44, 48, 66), width=12 * s)
    d.arc([cx - r, cy - r, cx + r, cy + r], start=-90, end=-90 + 360 * progress,
          fill=accent, width=12 * s)
    if card.get("big"):
        num, unit = card["big"], card.get("unit", "")
    elif mb == 0:
        num, unit = "NOW", "STARTING"
    elif mb < 60 or mb % 60:
        num, unit = str(mb), "MIN"
    else:
        num, unit = str(mb // 60), "HOUR" if mb == 60 else "HOURS"
    nsize = 112 if len(num) <= 2 else 70
    d.text((cx, cy - 6 * s), num, font=_font("InterDisplay-ExtraBold.otf", nsize), fill=(255, 255, 255), anchor="mm")
    d.text((cx, cy + 66 * s), unit, font=_font("InterDisplay-SemiBold.otf", 24), fill=accent, anchor="mm")

    # --- footer strip
    d.rectangle([0, h - foot, w, h], fill=(20, 22, 33))
    d.line([0, h - foot, w, h - foot], fill=(38, 42, 58), width=2 * s)
    d.ellipse([pad, h - foot // 2 - 11 * s, pad + 22 * s, h - foot // 2 + 11 * s], fill=accent)
    d.text((pad + 36 * s, h - foot // 2), brand.upper(), font=_font("InterDisplay-SemiBold.otf", 22),
           fill=(235, 237, 245), anchor="lm")
    if footer:
        d.text((w - pad, h - foot // 2), footer, font=_font("Inter-Medium.otf", 21), fill=(120, 126, 148), anchor="rm")

    # accent edge on the left, like a Discord embed
    d.rectangle([0, 0, 8 * s, h], fill=accent)

    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()
