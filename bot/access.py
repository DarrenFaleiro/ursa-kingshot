"""Who can see / change which part of a server's dashboard.

Levels per section: 0 = hidden, 1 = view, 2 = edit.

  Owner (OWNER_IDS) or server admin (Manage Server)  -> edit everything, incl. dashboard access
  Officers (granted by role or by user)               -> whatever was granted, per section
  Ordinary members of the server                      -> view overview/timeline, battle plans, KvK;
                                                         claims only if the server allows it
  Not in the server                                   -> nothing
"""

SECTIONS = {
    "overview": "Overview & timeline",
    "claims": "Recent claims & activity",
    "players": "Players",
    "reminders": "Reminders",
    "plans": "Battle plans",
    "kvk": "KvK comparison",
    "settings": "Settings",
}
LEVEL = {"none": 0, "view": 1, "edit": 2}
LEVEL_NAME = {0: "none", 1: "view", 2: "edit"}


def resolve(*, is_owner: bool, is_admin: bool, is_member: bool, user_id, role_ids: set,
            grants: list[dict], prefs: dict) -> dict:
    """Returns {section: level, "access": level, "mask": bool, "role": "admin"|"officer"|"member"|None}."""
    if is_owner or is_admin:
        out = {s: 2 for s in SECTIONS}
        out.update(access=2, mask=False, role="admin")
        return out
    if not is_member:
        out = {s: 0 for s in SECTIONS}
        out.update(access=0, mask=True, role=None)
        return out

    out = {s: 0 for s in SECTIONS}
    out.update(overview=1, plans=1, kvk=1, claims=1 if prefs.get("members_see_activity") else 0)

    officer = False
    role_ids = {str(r) for r in role_ids}
    for g in grants:
        hit = (g["target_type"] == "user" and str(g["target_id"]) == str(user_id)) or \
              (g["target_type"] == "role" and str(g["target_id"]) in role_ids)
        if not hit:
            continue
        officer = True
        for section, lvl in g["perms"].items():
            if section in SECTIONS:
                out[section] = max(out[section], LEVEL.get(lvl, 0))

    out["access"] = 0                          # only admins manage who has access
    # Officers who can see players see real names; everyone else gets masked names if the server wants it.
    out["mask"] = bool(prefs.get("mask_for_members", True)) and out["players"] == 0
    out["role"] = "officer" if officer else "member"
    return out


def mask_id(fid) -> str:
    fid = str(fid or "")
    return fid[:4] + "•••" + fid[-3:] if len(fid) > 7 else (fid[:2] + "•••" if fid else "")


def mask_name(name) -> str:
    name = str(name or "").strip()
    return (name[0] + "•••") if name else "•••"
