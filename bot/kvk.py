"""KvK comparison: your kingdom vs an opponent, from player power that officers type in.

Kingshot has no public data source for player power, so each side is a pasted list, one player per line:
    Darkblade, 152.3M
    Nova 98,500,000
    [ABC] Rook | 1.2B
Power can be written as 152300000, 152,300,000, 152.3M, 1.2B or 950K.
"""
import re

_POWER = re.compile(r"(\d[\d,\.]*)\s*([kmb])?\s*$", re.I)
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9}


def parse_power(text: str) -> int | None:
    m = _POWER.search(text.strip())
    if not m:
        return None
    num, unit = m.group(1), (m.group(2) or "").lower()
    if unit:
        num = num.replace(",", "")
        try:
            return int(float(num) * _MULT[unit])
        except ValueError:
            return None
    digits = num.replace(",", "").replace(".", "")
    return int(digits) if digits.isdigit() else None


def parse_players(text: str) -> tuple[list[dict], list[str]]:
    """Returns (players sorted by power, lines that couldn't be read)."""
    players, bad = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _POWER.search(line)
        power = parse_power(line) if m else None
        name = line[:m.start()].strip(" ,|;:-\t") if m else ""
        if power is None or not name:
            bad.append(line)
            continue
        players.append({"name": name[:40], "power": power})
    players.sort(key=lambda p: p["power"], reverse=True)
    return players, bad


def fmt(power: float) -> str:
    power = float(power or 0)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(power) >= size:
            v = power / size
            return f"{v:.2f}{unit}" if v < 10 else f"{v:.1f}{unit}"
    return f"{int(power)}"


def compare(ours: list[dict], theirs: list[dict], top_n: int = 10) -> dict:
    """Totals, top-N matchups and a plain-English verdict."""
    def side(ps, n):
        top = ps[:n]
        total = sum(p["power"] for p in ps)
        return {"count": len(ps), "total": total, "top_total": sum(p["power"] for p in top),
                "avg": total / len(ps) if ps else 0, "top": top}

    n = min(top_n, max(len(ours), len(theirs))) if (ours or theirs) else 0
    a, b = side(ours, n), side(theirs, n)
    pairs, wins = [], 0
    for i in range(n):
        o = ours[i] if i < len(ours) else None
        t = theirs[i] if i < len(theirs) else None
        diff = (o["power"] if o else 0) - (t["power"] if t else 0)
        wins += diff > 0
        biggest = max((o or {}).get("power", 0), (t or {}).get("power", 0)) or 1
        pairs.append({"rank": i + 1, "ours": o, "theirs": t, "diff": diff,
                      "our_pct": round(100 * (o["power"] if o else 0) / biggest),
                      "their_pct": round(100 * (t["power"] if t else 0) / biggest)})

    ratio = (a["top_total"] / b["top_total"]) if b["top_total"] else None
    if ratio is None or not ours:
        verdict, mood = "Add players for both sides to see a comparison.", "neutral"
    elif ratio >= 1.15:
        verdict, mood = f"You're clearly stronger at the top: {ratio:.2f}× their top-{n} power.", "good"
    elif ratio >= 1.0:
        verdict, mood = f"Slight edge to you ({ratio:.2f}×). Coordination will decide it.", "good"
    elif ratio >= 0.87:
        verdict, mood = f"Close fight - they're slightly ahead ({ratio:.2f}×). Focus rallies on their weaker players.", "warn"
    else:
        verdict, mood = f"They're much stronger at the top ({ratio:.2f}×). Shield up, defend, pick targets carefully.", "bad"
    return {"ours": a, "theirs": b, "pairs": pairs, "wins": wins, "n": n, "ratio": ratio,
            "verdict": verdict, "mood": mood}


# ---------------------------------------------------------------------------
# Preparation days, results and the kingdom's KvK record (all entered by officers)
# ---------------------------------------------------------------------------
PREP_DAYS = 5
DAY_NAMES = ["Day I", "Day II", "Day III", "Day IV", "Day V"]


def parse_prep(form_values: list[tuple[str, str]]) -> list[list[int | None]]:
    """[(ours_text, theirs_text) x5] -> [[ours, theirs] x5], None where not entered."""
    out = []
    for ours, theirs in form_values[:PREP_DAYS]:
        out.append([parse_power(ours) if (ours or "").strip() else None,
                    parse_power(theirs) if (theirs or "").strip() else None])
    while len(out) < PREP_DAYS:
        out.append([None, None])
    return out


def prep_summary(prep: list, prep_result: str = "auto") -> dict:
    """Totals, lead %, per-day winners and the preparation result."""
    days, won, lost, entered = [], 0, 0, 0
    tot_o = tot_t = 0
    for i, pair in enumerate((prep or [])[:PREP_DAYS]):
        o, t = (pair + [None, None])[:2] if isinstance(pair, list) else (None, None)
        done = o is not None and t is not None
        if done:
            entered += 1
            tot_o += o
            tot_t += t
            won += o > t
            lost += t > o
        days.append({"name": DAY_NAMES[i], "ours": o, "theirs": t, "done": done,
                     "lead": ((o / t - 1) * 100) if done and t else None,
                     "share": (o / (o + t) * 100) if done and (o + t) else 50})
    while len(days) < PREP_DAYS:
        days.append({"name": DAY_NAMES[len(days)], "ours": None, "theirs": None, "done": False, "lead": None, "share": 50})
    if prep_result in ("won", "lost"):
        result = prep_result
    elif entered == PREP_DAYS:
        result = "won" if won > lost else "lost" if lost > won else "tie"
    elif won >= 3 or lost >= 3:
        result = "won" if won >= 3 else "lost"
    else:
        result = "pending"
    return {"days": days, "ours": tot_o, "theirs": tot_t, "won": won, "lost": lost, "entered": entered,
            "lead": ((tot_o / tot_t - 1) * 100) if tot_t else None,
            "share": (tot_o / (tot_o + tot_t) * 100) if (tot_o + tot_t) else 50, "result": result}


def kingdom_record(kvks: list[dict]) -> dict:
    """Win rates over every saved KvK (oldest -> newest in 'recent')."""
    prep_w = prep_l = castle_w = castle_l = 0
    recent = []
    for k in sorted(kvks, key=lambda x: (x["data"].get("kvk_date") or "", x["id"])):
        d = k["data"]
        p = prep_summary(d.get("prep") or [], d.get("prep_result", "auto"))["result"]
        c = d.get("castle_result", "pending")
        prep_w += p == "won"
        prep_l += p == "lost"
        castle_w += c == "won"
        castle_l += c == "lost"
        recent.append({"id": k["id"], "title": k["title"], "prep": p, "castle": c, "date": d.get("kvk_date")})
    rate = lambda w, l: round(100 * w / (w + l)) if (w + l) else None
    return {"prep_w": prep_w, "prep_l": prep_l, "castle_w": castle_w, "castle_l": castle_l,
            "prep_rate": rate(prep_w, prep_l), "castle_rate": rate(castle_w, castle_l),
            "played": len(recent), "recent": recent[-10:]}
