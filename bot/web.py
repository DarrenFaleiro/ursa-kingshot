"""Website: log in with Discord, then manage the servers you're allowed to manage.

Who sees what:
  - Bot owners (OWNER_IDS): every server the bot is in, plus global gift-code controls.
  - Server managers (Manage Server permission, or the server's bot-manager role):
    that server's players, reminders and settings.
  - Everyone else: their own linked Kingshot accounts.
"""
import asyncio
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import aiohttp
from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from . import config
from .redeemer import Redeemer
from .tasks import spawn
from . import access as acl
from .kvk import compare as kvk_compare, fmt as fmt_power, kingdom_record, parse_players, parse_prep, prep_summary
from .kvk_card import render_kvk
from .plans import PLAN_KINDS, render_plan, validate as validate_plan
from . import unlocks as ul
from .reminders import KINDS, heartbeat_text, next_occurrence, parse_before, parse_when, timeline

COOKIE = "ks_session"
STATE_COOKIE = "ks_oauth_state"
DISCORD_API = "https://discord.com/api/v10"
MANAGE_GUILD, ADMINISTRATOR = 0x20, 0x8

jinja = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                    autoescape=select_autoescape(["html"]))


def _dt(ts, relative=False):
    """<time> tag: shows UTC, and the browser swaps in the viewer's local time (see base.html)."""
    if not ts:
        return "-"
    utc = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%a %d %b %H:%M UTC")
    rel = ' data-rel="1"' if relative else ""
    return Markup(f'<time data-ts="{int(ts)}"{rel} title="{utc}">{utc}</time>')


jinja.filters["dt"] = _dt
jinja.filters["countdown"] = lambda ts: _dt(ts, relative=True)
jinja.filters["power"] = lambda v: fmt_power(v)
jinja.filters["ymd"] = lambda ts: datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")
jinja.filters["hm"] = lambda ts: datetime.fromtimestamp(int(ts), timezone.utc).strftime("%H:%M")


def back(path: str, msg: str = "", err: bool = False, tab: str = "") -> web.HTTPFound:
    q = ("?msg=" + quote(msg) + ("&err=1" if err else "")) if msg else ""
    return web.HTTPFound(path + q + (f"#{tab}" if tab else ""))


def create_app(redeemer: Redeemer, bot) -> web.Application:
    db = redeemer.db
    routes = web.RouteTableDef()
    secure = config.PUBLIC_URL.startswith("https://")
    redirect_uri = f"{config.PUBLIC_URL}/auth/callback"

    def rail_servers(me, is_owner) -> list:
        """Servers for the left rail: ones the bot is in and the user belongs to (access is checked on click)."""
        if not me:
            return []
        member = set((me.get("guilds") or {}).get("member", []))
        out = []
        for g in bot.guilds:
            if is_owner or str(g.id) in member:
                out.append({"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else None})
        return out

    def render(name: str, request=None, **ctx) -> web.Response:
        ctx.setdefault("me", request.get("session") if request else None)
        ctx.setdefault("msg", request.query.get("msg", "") if request else "")
        ctx.setdefault("err", bool(request and request.query.get("err")))
        ctx["invite_url"] = (f"https://discord.com/oauth2/authorize?client_id={config.DISCORD_CLIENT_ID}"
                             f"&scope=bot%20applications.commands&permissions=268454928"
                             if config.DISCORD_CLIENT_ID else "")
        ctx["public_url"] = config.PUBLIC_URL
        ctx["bot_name"] = config.BOT_NAME
        ctx["request_path"] = request.path if request else ""
        ctx["heartbeat"] = heartbeat_text()
        ctx["is_owner"] = bool(ctx["me"] and bot.is_owner(int(ctx["me"]["user_id"])))
        ctx.setdefault("rail", rail_servers(ctx["me"], ctx["is_owner"]))
        ctx["kinds"] = KINDS
        ctx["sections"] = acl.SECTIONS
        ctx["plan_kinds"] = PLAN_KINDS
        return web.Response(text=jinja.get_template(name).render(**ctx), content_type="text/html")

    # ---------- auth middleware ----------
    @web.middleware
    async def auth(request: web.Request, handler):
        public = request.path in ("/", "/login", "/auth/discord", "/auth/callback", "/health") \
            or request.path.startswith("/static/")
        session = db.get_session(request.cookies.get(COOKIE, ""))
        request["session"] = session
        if public or session:
            return await handler(request)
        raise web.HTTPFound("/login")

    def owner(request) -> bool:
        return bot.is_owner(int(request["session"]["user_id"]))

    async def perms(request, gid) -> dict:
        """What this user may see/do in this server (see access.py). Cached per request."""
        cache = request.setdefault("_perms", {})
        if gid not in cache:
            uid = request["session"]["user_id"]
            member, admin, roles = (True, True, set()) if owner(request) else await bot.member_context(gid, uid)
            cache[gid] = acl.resolve(is_owner=owner(request), is_admin=admin, is_member=member, user_id=uid,
                                     role_ids=roles, grants=db.list_grants(gid),
                                     prefs=db.guild_settings(gid)["prefs"])
        return cache[gid]

    async def require(request, section: str, level: int = 2) -> tuple[str, dict]:
        gid = request.match_info["gid"]
        if not gid.isdigit() or not bot.get_guild(int(gid)):
            raise back("/", "The bot isn't in that server.", err=True)
        p = await perms(request, gid)
        if p.get(section, 0) < level:
            need = "change" if level == 2 else "see"
            raise web.HTTPForbidden(text=f"You don't have permission to {need} {acl.SECTIONS.get(section, section)} "
                                         f"in this server. Ask a server admin for dashboard access.")
        return gid, p

    async def compute_guilds(user_id: int, oauth_guilds: list) -> dict:
        """Which bot servers this user is in, and which of those they can manage."""
        manage, member = [], []
        for g in oauth_guilds:
            gid = int(g["id"])
            if not bot.get_guild(gid):
                continue
            member.append(str(gid))
            perms = int(g.get("permissions", 0))
            if g.get("owner") or perms & (MANAGE_GUILD | ADMINISTRATOR) or bot.is_owner(user_id) \
                    or await bot.can_manage(gid, user_id):
                manage.append(str(gid))
        if bot.is_owner(user_id):
            manage = [str(g.id) for g in bot.guilds]
        return {"manage": manage, "member": member}

    # ---------- login with Discord ----------
    @routes.get("/health")
    async def health(_):
        return web.Response(text="ok")

    @routes.get("/login")
    async def login_page(request):
        configured = bool(config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET)
        return render("login.html", request, configured=configured)

    @routes.get("/auth/discord")
    async def auth_start(_):
        state = secrets.token_urlsafe(24)
        url = "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": config.DISCORD_CLIENT_ID, "redirect_uri": redirect_uri,
            "response_type": "code", "scope": "identify guilds", "state": state, "prompt": "none"})
        resp = web.HTTPFound(url)
        resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="Lax", secure=secure)
        raise resp

    @routes.get("/auth/callback")
    async def auth_callback(request):
        state, code = request.query.get("state"), request.query.get("code")
        if not code or not state or state != request.cookies.get(STATE_COOKIE):
            raise back("/login", "Login failed or was cancelled - please try again.", err=True)
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{DISCORD_API}/oauth2/token", data={
                "client_id": config.DISCORD_CLIENT_ID, "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}) as r:
                if r.status != 200:
                    raise back("/login", f"Discord login error ({r.status}). Check the client ID/secret "
                                         "and redirect URL.", err=True)
                token = (await r.json())["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            async with s.get(f"{DISCORD_API}/users/@me", headers=headers) as r:
                user = await r.json()
            async with s.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as r:
                oauth_guilds = await r.json() if r.status == 200 else []
        guilds = await compute_guilds(int(user["id"]), oauth_guilds)
        guilds["names"] = {g["id"]: g["name"] for g in oauth_guilds if g["id"] in guilds["member"]}
        sess = db.create_session(user["id"], user.get("global_name") or user["username"],
                                 user.get("avatar"), guilds)
        resp = web.HTTPFound("/")
        resp.set_cookie(COOKIE, sess, max_age=7 * 86400, httponly=True, samesite="Lax", secure=secure)
        resp.del_cookie(STATE_COOKIE)
        raise resp

    @routes.post("/logout")
    async def logout(request):
        db.delete_session(request.cookies.get(COOKIE, ""))
        resp = web.HTTPFound("/login")
        resp.del_cookie(COOKIE)
        raise resp

    @routes.post("/refresh")
    async def refresh(request):
        """Re-check permissions (e.g. after being given the manager role) by logging in again."""
        db.delete_session(request.cookies.get(COOKIE, ""))
        raise web.HTTPFound("/auth/discord")

    # ---------- home ----------
    @routes.get("/")
    async def home(request):
        s = request["session"]
        if not s:   # public page: active gift codes only - no player data, no activity
            codes = [c for c in db.list_codes() if c["status"] == "active"]
            return render("public.html", request, codes=codes,
                          configured=bool(config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET))
        counts = db.count_players_by_guild()
        servers = []
        gids = [str(g.id) for g in bot.guilds] if owner(request) else s["guilds"].get("member", [])
        for gid in gids:
            g = bot.get_guild(int(gid))
            if not g:
                continue
            pp = await perms(request, gid)
            if not pp["overview"]:
                continue
            servers.append({"id": gid, "name": g.name, "icon": g.icon.url if g.icon else None,
                            "role": pp["role"], "players": counts.get(gid, 0),
                            "reminders": len(db.list_reminders(gid, only_enabled=True))})
        join_options = [{"id": gid, "name": (bot.get_guild(int(gid)).name if bot.get_guild(int(gid))
                                             else s["guilds"].get("names", {}).get(gid, gid))}
                        for gid in s["guilds"].get("member", [])]
        mine = db.list_players(discord_user_id=s["user_id"])
        unassigned = [p for p in db.list_players() if not p["guild_id"]] if owner(request) else []
        return render("home.html", request, servers=servers, mine=mine, join_options=join_options,
                      unassigned=unassigned, salt=redeemer.salt_status, running=redeemer.running,
                      last=redeemer.last_summary, next_run=redeemer.next_run_at, now=time.time())

    @routes.post("/me/players/add")
    async def my_add(request):
        s, form = request["session"], await request.post()
        gid = form.get("guild_id", "")
        if gid not in s["guilds"].get("member", []):
            raise back("/", "Pick a server you're in.", err=True)
        ok, msg = await redeemer.add_player(form.get("fid", ""), form.get("kid", ""),
                                            f"web:{s['username']}", form.get("name"),
                                            guild_id=gid, discord_user_id=s["user_id"])
        if ok:
            spawn(redeemer.run(fids=[form.get("fid", "").strip()]))
        raise back("/", msg.replace("**", "").replace("`", ""), err=not ok)

    @routes.post("/me/players/{fid}/delete")
    async def my_delete(request):
        p = db.get_player(request.match_info["fid"])
        if p and (p["discord_user_id"] == request["session"]["user_id"] or owner(request)):
            db.remove_player(p["fid"])
        raise back("/", "Removed")

    # ---------- one server ----------
    @routes.get("/g/{gid}")
    async def guild_page(request):
        gid, p = await require(request, "overview", 1)
        g = bot.get_guild(int(gid))
        now = time.time()
        settings = db.guild_settings(gid)
        all_rems = db.list_reminders(gid)
        reminders = []
        for rem in all_rems:
            role = g.get_role(int(rem["role_id"])) if rem["role_id"] else None
            ch = g.get_channel(int(rem["channel_id"]))
            reminders.append(dict(rem, next=next_occurrence(rem, now), role_name=role.name if role else None,
                                  channel_name=ch.name if ch else "(deleted channel)"))

        # timeline: next 7 days, grouped by UTC day
        days = {}
        for e in timeline(all_rems, now, 7):
            day = datetime.fromtimestamp(e["occ"], timezone.utc).strftime("%Y-%m-%d")
            days.setdefault(day, []).append(dict(e, kind=KINDS.get(e["rem"]["kind"], KINDS["custom"])))

        # recent claims (masked for members if the server wants that)
        claims = []
        if p["claims"]:
            for c in db.recent_claims(gid, 40):
                c = dict(c)
                if p["mask"]:
                    c["nickname"], c["fid"] = acl.mask_name(c["nickname"]), acl.mask_id(c["fid"])
                claims.append(c)
        log = db.recent_log(30, gid) if p["claims"] and not p["mask"] else []

        players = db.list_players(gid) if p["players"] else []
        codes = [dict(c, stats=db.code_stats(c["code"], gid)) for c in db.list_codes()[:15]]
        auto = settings["auto"]
        auto_done = bool(auto.get("channels"))
        checklist = [
            ("Run Auto setup (makes the channels and alert roles for you) - optional", auto_done, "settings"),
            ("Choose a report channel", bool(settings.get("report_channel_id")), "settings"),
            ("Add players (or ask members to use /add)", bool(db.list_players(gid)), "players"),
            ("Create a Bear Trap or Arena reminder", bool(reminders), "reminders"),
        ] if p["role"] == "admin" else []

        # battle plans + KvK
        plans = db.list_plans(gid) if p["plans"] else []
        kvks = db.list_kvk(gid) if p["kvk"] else []
        sel_kvk = None
        if kvks:
            want = request.query.get("kvk")
            sel_kvk = next((k for k in kvks if str(k["id"]) == want), kvks[0])
            sel_kvk = dict(sel_kvk, result=kvk_compare(sel_kvk["data"].get("ours", []), sel_kvk["data"].get("theirs", []),
                                                        int(sel_kvk["data"].get("top_n") or 10)),
                           prep=prep_summary(sel_kvk["data"].get("prep") or [], sel_kvk["data"].get("prep_result", "auto")))
        kvk_rec = kingdom_record(kvks) if kvks else None
        next_kvk_ts = None
        nk = settings["prefs"].get("next_kvk")
        if nk:
            try:
                next_kvk_ts = int(datetime.strptime(nk, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                next_kvk_ts = None

        unlock = db.unlock_settings(gid)
        unlock_rows = ul.unlock_list(unlock["open_date"], now) if unlock.get("open_date") else []
        unlock_info = {"settings": unlock, "rows": unlock_rows,
                       "day": ul.kingdom_day(unlock["open_date"], now) if unlock.get("open_date") else None,
                       "next": ul.next_unlock(unlock, now)}
        auto_defaults = {k: dict(zip(("channel", "role"), bot.auto_defaults(gid, k))) for k in KINDS}
        return render("guild.html", request, unlock=unlock_info, unlock_cats=ul.CATEGORIES, lead_choices=ul.LEAD_CHOICES, g=g, gid=gid, p=p, players=players, checklist=checklist,
                      auto_done=auto_done, auto_defaults=auto_defaults, reminders=reminders, codes=codes,
                      settings=settings, days=days, claims=claims, log=log, plans=plans, kvks=kvks,
                      sel_kvk=sel_kvk, kvk_rec=kvk_rec, next_kvk_ts=next_kvk_ts, grants=db.list_grants(gid) if p["access"] else [],
                      channels=bot.guild_channels(gid), roles=bot.guild_roles(gid), now=now,
                      today=datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    @routes.post("/g/{gid}/players/add")
    async def g_add_players(request):
        gid, _p = await require(request, "players", 2)
        form = await request.post()
        added, failed = [], []
        for line in form.get("players", "").splitlines():
            parts = line.replace(",", " ").split(maxsplit=2)
            if not parts:
                continue
            if len(parts) < 2:
                failed.append(f"{parts[0]}: missing kingdom.")
                continue
            ok, msg = await redeemer.add_player(parts[0], parts[1], f"web:{request['session']['username']}",
                                                parts[2] if len(parts) > 2 else None, guild_id=gid)
            (added if ok else failed).append(parts[0] if ok else msg.replace("`", ""))
        if added:
            spawn(redeemer.run(fids=added))
        raise back(f"/g/{gid}", f"Added {len(added)} player(s)." + (" Problems: " + " ".join(failed) if failed else ""),
                   err=bool(failed), tab="players")

    def player_in_guild(gid, fid):
        p = db.get_player(fid)
        if not p or p["guild_id"] != gid:
            raise back(f"/g/{gid}", "Player not found in this server", err=True)
        return p

    @routes.post("/g/{gid}/players/{fid}/delete")
    async def g_delete_player(request):
        gid, _p = await require(request, "players", 2)
        p = player_in_guild(gid, request.match_info["fid"])
        db.remove_player(p["fid"])
        db.log(f"Player removed: {p['fid']} by web:{request['session']['username']}", gid)
        raise back(f"/g/{gid}", "Player removed", tab="players")

    @routes.post("/g/{gid}/players/{fid}/toggle")
    async def g_toggle_player(request):
        gid, _p = await require(request, "players", 2)
        p = player_in_guild(gid, request.match_info["fid"])
        db.set_player_enabled(p["fid"], not p["enabled"])
        raise back(f"/g/{gid}", tab="players")

    @routes.post("/g/{gid}/players/{fid}/edit")
    async def g_edit_player(request):
        gid, _p = await require(request, "players", 2)
        p = player_in_guild(gid, request.match_info["fid"])
        form = await request.post()
        kid = form.get("kid", "").strip()
        if not kid.isdigit():
            raise back(f"/g/{gid}", "Kingdom must be a number", err=True)
        db.update_player(p["fid"], form.get("name", "").strip() or None, int(kid))
        db.set_player_enabled(p["fid"], True)
        raise back(f"/g/{gid}", "Player updated and resumed", tab="players")

    @routes.post("/g/{gid}/settings")
    async def g_settings(request):
        gid, _p = await require(request, "settings", 2)
        form = await request.post()
        db.set_guild_setting(gid, "report_channel_id", form.get("report_channel_id") or None)
        db.set_guild_setting(gid, "manager_role_id", form.get("manager_role_id") or None)
        raise back(f"/g/{gid}", "Settings saved", tab="settings")

    # ---------- reminders ----------
    async def _reminder_fields(gid, form) -> dict:
        kind = form.get("kind", "custom")
        if kind not in KINDS:
            kind = "custom"
        try:
            start = parse_when(form.get("time", ""), form.get("date") or None)
        except ValueError:
            raise back(f"/g/{gid}", "Time must be HH:MM (UTC)", err=True)
        try:
            repeat = max(0, int(float(form.get("repeat_hours") or 0) * 60))
        except ValueError:
            repeat = 0
        role_id = form.get("role_id") or None
        if role_id == "__new__":
            role = await bot.create_role(gid, form.get("new_role_name") or f"{KINDS[kind]['label']} alerts")
            if not role:
                raise back(f"/g/{gid}", "Couldn't create the role - give the bot 'Manage Roles'.", err=True)
            role_id = role.id
        if not form.get("channel_id"):
            raise back(f"/g/{gid}", "Pick a channel", err=True)
        return dict(kind=kind, name=(form.get("name") or KINDS[kind]["label"]).strip(),
                    channel_id=form.get("channel_id"), role_id=role_id, start_at=start,
                    repeat_minutes=repeat,
                    before_minutes=",".join(str(b) for b in parse_before(form.get("before") or KINDS[kind]["before"])),
                    message=(form.get("message") or "").strip() or None)

    @routes.post("/g/{gid}/reminders/add")
    async def g_add_reminder(request):
        gid, _p = await require(request, "reminders", 2)
        f = await _reminder_fields(gid, await request.post())
        rid = db.add_reminder(gid, f["kind"], f["name"], f["channel_id"], f["start_at"], f["repeat_minutes"],
                              f["before_minutes"], f["role_id"], f["message"])
        db.log(f"Reminder #{rid} '{f['name']}' created by web:{request['session']['username']}", gid)
        raise back(f"/g/{gid}", f"Reminder '{f['name']}' created", tab="reminders")

    def own_reminder(gid, rid):
        rem = db.get_reminder(int(rid))
        if not rem or rem["guild_id"] != gid:
            raise back(f"/g/{gid}", "Reminder not found", err=True)
        return rem

    @routes.post("/g/{gid}/reminders/{rid}/edit")
    async def g_edit_reminder(request):
        gid, _p = await require(request, "reminders", 2)
        rem = own_reminder(gid, request.match_info["rid"])
        f = await _reminder_fields(gid, await request.post())
        db.update_reminder(rem["id"], **f, last_fire_ts=int(time.time()), enabled=1)
        raise back(f"/g/{gid}", "Reminder updated", tab="reminders")

    @routes.post("/g/{gid}/reminders/{rid}/toggle")
    async def g_toggle_reminder(request):
        gid, _p = await require(request, "reminders", 2)
        rem = own_reminder(gid, request.match_info["rid"])
        db.update_reminder(rem["id"], enabled=0 if rem["enabled"] else 1, last_fire_ts=int(time.time()))
        raise back(f"/g/{gid}", tab="reminders")

    @routes.post("/g/{gid}/reminders/{rid}/delete")
    async def g_delete_reminder(request):
        gid, _p = await require(request, "reminders", 2)
        db.remove_reminder(int(request.match_info["rid"]), gid)
        raise back(f"/g/{gid}", "Reminder deleted", tab="reminders")

    @routes.post("/g/{gid}/reminders/{rid}/test")
    async def g_test_reminder(request):
        gid, _p = await require(request, "reminders", 2)
        rem = own_reminder(gid, request.match_info["rid"])
        nxt = next_occurrence(rem, time.time()) or int(rem["start_at"])
        ok = await bot.send_reminder(rem, nxt, parse_before(rem["before_minutes"])[0], test=True)
        raise back(f"/g/{gid}", "Test sent - check the channel" if ok else
                       "Couldn't post - check the bot's channel permissions", err=not ok, tab="reminders")

    @routes.post("/g/{gid}/autosetup")
    async def g_autosetup(request):
        gid, _p = await require(request, "settings", 2)
        try:
            ok, lines = await bot.auto_setup(gid)
        except Exception as e:   # e.g. Discord refused
            ok, lines = False, [f"Auto setup failed: {e}"]
        text = " · ".join(l.replace("✅ ", "") for l in lines)
        text = text.replace("<#", "#").replace("<@&", "@").replace(">", "")
        raise back(f"/g/{gid}", text, err=not ok, tab="overview")

    @routes.get("/g/{gid}/reminders/{rid}/preview.png")
    async def g_preview_reminder(request):
        from .alert_card import render_card
        from .reminders import build_alert
        gid, _p = await require(request, "overview", 1)
        rem = own_reminder(gid, request.match_info["rid"])
        nxt = next_occurrence(rem, time.time()) or int(rem["start_at"])
        try:
            before = int(request.query.get("before", parse_before(rem["before_minutes"])[0]))
        except ValueError:
            before = 15
        png = await asyncio.to_thread(render_card, build_alert(rem, nxt, before)["card"],
                                      config.PUBLIC_URL.split("://")[-1], config.BOT_NAME)
        if not png:
            raise web.HTTPNotFound(text="Pillow isn't installed")
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})

    @routes.post("/g/{gid}/rolebuttons")
    async def g_role_buttons(request):
        gid, _p = await require(request, "reminders", 2)
        form = await request.post()
        msg = await bot.post_role_panel(gid, form.get("channel_id") or 0)
        raise back(f"/g/{gid}", msg, err=not msg.startswith("Role buttons posted"), tab="reminders")

    # ---------- kingdom unlock reminders ----------
    def _unlock_from_form(form, current: dict) -> dict:
        data = dict(current)
        data["enabled"] = form.get("enabled") == "on"
        data["channel_id"] = form.get("channel_id") or None
        data["role_id"] = form.get("role_id") or None
        data["kingdom"] = (form.get("kingdom") or "").strip().lstrip("#")[:8]
        if form.get("start_mode") == "day" and (form.get("current_day") or "").strip().isdigit():
            data["open_date"] = ul.open_date_from_day(int(form.get("current_day")))
        elif form.get("open_date"):
            try:
                datetime.strptime(form.get("open_date"), "%Y-%m-%d")
                data["open_date"] = form.get("open_date")
            except ValueError:
                pass
        leads = [int(x) for x in form.getall("leads", []) if str(x).isdigit() and int(x) in ul.LEAD_CHOICES]
        data["leads"] = sorted(set(leads), reverse=True) or [1]
        cats = [c for c in form.getall("categories", []) if c in ul.CATEGORIES]
        data["categories"] = cats or list(ul.CATEGORIES)
        return data

    @routes.post("/g/{gid}/unlocks/save")
    async def g_unlocks_save(request):
        gid, _p = await require(request, "reminders", 2)
        data = _unlock_from_form(await request.post(), db.unlock_settings(gid))
        if data["enabled"] and not (data["channel_id"] and data["open_date"]):
            raise back(f"/g/{gid}", "Pick a channel and your kingdom's open date (or current day) first",
                       err=True, tab="unlocks")
        db.save_unlock_settings(gid, data)
        db.log(f"Unlock reminders {'on' if data['enabled'] else 'off'} (kingdom #{data['kingdom']}) "
               f"by web:{request['session']['username']}", gid)
        raise back(f"/g/{gid}", "Unlock reminders saved", tab="unlocks")

    @routes.post("/g/{gid}/unlocks/test")
    async def g_unlocks_test(request):
        gid, _p = await require(request, "reminders", 2)
        settings = db.unlock_settings(gid)
        nxt = ul.next_unlock(settings)
        if not nxt or not settings.get("channel_id"):
            raise back(f"/g/{gid}", "Save a channel and open date first", err=True, tab="unlocks")
        ok = await bot.send_unlock(gid, settings, nxt, (settings.get("leads") or [1])[0], test=True)
        raise back(f"/g/{gid}", "Test posted - check the channel" if ok else "Couldn't post - check the bot's "
                   "channel permissions", err=not ok, tab="unlocks")

    @routes.get("/g/{gid}/unlocks/preview.png")
    async def g_unlocks_preview(request):
        """Live preview: uses the form's current values from the query string, else the saved settings."""
        from .alert_card import render_card
        gid, _p = await require(request, "overview", 1)
        q = request.query
        settings = dict(db.unlock_settings(gid))
        if q.get("kingdom") is not None:
            settings["kingdom"] = q.get("kingdom", "").lstrip("#")[:8]
        if q.get("current_day", "").isdigit():
            settings["open_date"] = ul.open_date_from_day(int(q["current_day"]))
        elif q.get("open_date"):
            try:
                datetime.strptime(q["open_date"], "%Y-%m-%d")
                settings["open_date"] = q["open_date"]
            except ValueError:
                pass
        if q.get("categories") is not None:
            settings["categories"] = [c for c in q.get("categories", "").split(",") if c in ul.CATEGORIES] or None
        nxt = ul.next_unlock(settings) if settings.get("open_date") else None
        if not nxt:   # nothing to show yet: an example card
            nxt = ul.unlock_list(ul.open_date_from_day(252))[-4]
        png = await asyncio.to_thread(render_card, ul.card_for(nxt, settings.get("kingdom")),
                                      config.PUBLIC_URL.split("://")[-1], config.BOT_NAME)
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})

    # ---------- privacy ----------
    @routes.post("/g/{gid}/privacy")
    async def g_privacy(request):
        gid, _p = await require(request, "settings", 2)
        form = await request.post()
        db.set_prefs(gid, members_see_activity=form.get("members_see_activity") == "on",
                     mask_for_members=form.get("mask_for_members") == "on")
        raise back(f"/g/{gid}", "Privacy settings saved", tab="settings")

    # ---------- dashboard access (officers) ----------
    @routes.post("/g/{gid}/access/add")
    async def g_access_add(request):
        gid, _p = await require(request, "access", 2)
        form = await request.post()
        g = bot.get_guild(int(gid))
        ttype = form.get("target_type")
        if ttype == "role":
            role = g.get_role(int(form.get("role_id") or 0))
            if not role:
                raise back(f"/g/{gid}", "Pick a role", err=True, tab="settings")
            tid, label = role.id, "@" + role.name
        else:
            uid = (form.get("user_id") or "").strip().strip("<@!>")
            if not uid.isdigit():
                raise back(f"/g/{gid}", "Enter the person's Discord user ID (right-click them → Copy User ID)",
                           err=True, tab="settings")
            m = g.get_member(int(uid))
            if not m:
                try:
                    m = await g.fetch_member(int(uid))
                except Exception:
                    raise back(f"/g/{gid}", "That user isn't in this server", err=True, tab="settings")
            tid, label, ttype = m.id, m.display_name, "user"
        grant = {sec: form.get(f"perm_{sec}") for sec in acl.SECTIONS
                 if form.get(f"perm_{sec}") in ("view", "edit")}
        if not grant:
            raise back(f"/g/{gid}", "Give at least one section view or edit access", err=True, tab="settings")
        db.set_grant(gid, ttype, tid, label, grant)
        db.log(f"Dashboard access for {label} set by web:{request['session']['username']}", gid)
        raise back(f"/g/{gid}", f"Dashboard access saved for {label}", tab="settings")

    @routes.post("/g/{gid}/access/{aid}/delete")
    async def g_access_delete(request):
        gid, _p = await require(request, "access", 2)
        db.remove_grant(int(request.match_info["aid"]), gid)
        raise back(f"/g/{gid}", "Access removed", tab="settings")

    # ---------- battle plans ----------
    @routes.post("/g/{gid}/plans/save")
    async def g_plan_save(request):
        gid, _p = await require(request, "plans", 2)
        form = await request.post()
        try:
            raw = json.loads(form.get("data") or "{}")
        except ValueError:
            raise back(f"/g/{gid}", "Couldn't read the plan", err=True, tab="plans")
        when = None
        if form.get("time"):
            try:
                when = parse_when(form.get("time"), form.get("date") or None)
            except ValueError:
                pass
        raw["when"] = when
        raw["notes"] = form.get("notes", "")
        data = validate_plan(raw)
        kind = form.get("kind") if form.get("kind") in PLAN_KINDS else "kvk"
        name = (form.get("name") or "").strip()[:60] or "Battle plan"
        pid = form.get("plan_id")
        pid = int(pid) if pid and pid.isdigit() and db.get_plan(int(pid), gid) else None
        pid = db.save_plan(gid, name, kind, data, request["session"]["username"], pid)
        raise back(f"/g/{gid}", f"Plan '{name}' saved", tab="plans")

    @routes.post("/g/{gid}/plans/{pid}/delete")
    async def g_plan_delete(request):
        gid, _p = await require(request, "plans", 2)
        db.delete_plan(int(request.match_info["pid"]), gid)
        raise back(f"/g/{gid}", "Plan deleted", tab="plans")

    @routes.get("/g/{gid}/plans/{pid}.png")
    async def g_plan_png(request):
        gid, _p = await require(request, "plans", 1)
        plan = db.get_plan(int(request.match_info["pid"]), gid)
        if not plan:
            raise web.HTTPNotFound()
        png = await asyncio.to_thread(render_plan, plan["name"], plan["kind"], plan["data"], config.BOT_NAME,
                                      config.PUBLIC_URL.split("://")[-1])
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})

    @routes.post("/g/{gid}/plans/{pid}/post")
    async def g_plan_post(request):
        gid, _p = await require(request, "plans", 2)
        form = await request.post()
        msg = await bot.post_plan(gid, int(request.match_info["pid"]), form.get("channel_id") or 0)
        raise back(f"/g/{gid}", msg, err=not msg.startswith("Posted"), tab="plans")

    # ---------- KvK comparison ----------
    @routes.post("/g/{gid}/kvk/save")
    async def g_kvk_save(request):
        gid, _p = await require(request, "kvk", 2)
        form = await request.post()
        ours, bad1 = parse_players(form.get("ours", ""))
        theirs, bad2 = parse_players(form.get("theirs", ""))
        try:
            top_n = max(3, min(100, int(form.get("top_n") or 10)))
        except ValueError:
            top_n = 10
        prep = parse_prep([(form.get(f"prep_o{i}", ""), form.get(f"prep_t{i}", "")) for i in range(5)])
        kvk_date = (form.get("kvk_date") or "").strip()
        try:
            datetime.strptime(kvk_date, "%Y-%m-%d")
        except ValueError:
            kvk_date = ""
        data = {"our_kingdom": (form.get("our_kingdom") or "").strip().lstrip("#")[:12],
                "their_kingdom": (form.get("their_kingdom") or "").strip().lstrip("#")[:12],
                "kvk_date": kvk_date, "prep": prep,
                "prep_result": form.get("prep_result") if form.get("prep_result") in ("auto", "won", "lost") else "auto",
                "castle_result": form.get("castle_result") if form.get("castle_result") in ("pending", "won", "lost") else "pending",
                "ours": ours[:200], "theirs": theirs[:200], "top_n": top_n,
                "notes": (form.get("notes") or "").strip()[:500]}
        title = (form.get("title") or "").strip()[:60] or \
            f"#{data['our_kingdom'] or '?'} vs #{data['their_kingdom'] or '?'}"
        kid = form.get("kvk_id")
        kid = int(kid) if kid and kid.isdigit() and db.get_kvk(int(kid), gid) else None
        kid = db.save_kvk(gid, title, data, request["session"]["username"], kid)
        skipped = len(bad1) + len(bad2)
        raise back(f"/g/{gid}?kvk={kid}", f"Comparison saved" + (f" - {skipped} line(s) skipped (no power found)"
                                                                  if skipped else ""), err=bool(skipped), tab="kvk")

    @routes.post("/g/{gid}/kvk/next")
    async def g_kvk_next(request):
        gid, _p = await require(request, "kvk", 2)
        form = await request.post()
        val = None
        if form.get("date"):
            try:
                val = datetime.strptime(f"{form.get('date')} {form.get('time') or '00:00'}", "%Y-%m-%d %H:%M") \
                    .strftime("%Y-%m-%d %H:%M")
            except ValueError:
                raise back(f"/g/{gid}", "Date must be a valid date", err=True, tab="kvk")
        db.set_prefs(gid, next_kvk=val)
        raise back(f"/g/{gid}", "Next KvK date saved" if val else "Next KvK date cleared", tab="kvk")

    @routes.get("/g/{gid}/kvk/{kid}.png")
    async def g_kvk_png(request):
        gid, _p = await require(request, "kvk", 1)
        k = db.get_kvk(int(request.match_info["kid"]), gid)
        if not k:
            raise web.HTTPNotFound()
        d = k["data"]
        png = await asyncio.to_thread(render_kvk, d, prep_summary(d.get("prep") or [], d.get("prep_result", "auto")),
                                      kingdom_record(db.list_kvk(gid)), config.BOT_NAME,
                                      config.PUBLIC_URL.split("://")[-1])
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})

    @routes.post("/g/{gid}/kvk/{kid}/delete")
    async def g_kvk_delete(request):
        gid, _p = await require(request, "kvk", 2)
        db.delete_kvk(int(request.match_info["kid"]), gid)
        raise back(f"/g/{gid}", "Comparison deleted", tab="kvk")

    @routes.post("/g/{gid}/kvk/{kid}/post")
    async def g_kvk_post(request):
        gid, _p = await require(request, "kvk", 2)
        form = await request.post()
        msg = await bot.post_kvk(gid, int(request.match_info["kid"]), form.get("channel_id") or 0)
        raise back(f"/g/{gid}?kvk={request.match_info['kid']}", msg, err=not msg.startswith("Posted"), tab="kvk")

    @routes.get("/help")
    async def help_page(request):
        return render("help.html", request)

    # ---------- player history ----------
    @routes.get("/player/{fid}")
    async def player(request):
        s = request["session"]
        p = db.get_player(request.match_info["fid"])
        allowed = p and (owner(request) or p["discord_user_id"] == s["user_id"]
                         or (p["guild_id"] and (await perms(request, p["guild_id"]))["players"] >= 1))
        if not allowed:
            raise back("/", "Player not found", err=True)
        return render("player.html", request, p=p, history=db.history(p["fid"]))

    # ---------- gift codes (global) ----------
    def require_any_manager(request):
        if not (owner(request) or request["session"]["guilds"].get("manage")):
            raise web.HTTPForbidden(text="Only server admins can manage gift codes. "
                                         "Everyone can see active codes on the public page.")

    @routes.get("/codes")
    async def codes_page(request):
        require_any_manager(request)
        codes = [dict(c, stats=db.code_stats(c["code"])) for c in db.list_codes()]
        return render("codes.html", request, codes=codes, salt=redeemer.salt_status,
                      running=redeemer.running, last=redeemer.last_summary)

    @routes.post("/codes/add")
    async def add_code(request):
        require_any_manager(request)
        code = (await request.post()).get("code", "").strip()
        if redeemer.add_code(code, f"web:{request['session']['username']}"):
            spawn(redeemer.run(codes=[code]))
            raise back("/codes", f"Code {code} added - redeeming in the background")
        raise back("/codes", "Code already exists or was empty", err=True)

    @routes.post("/codes/{code}/delete")
    async def delete_code(request):
        if not owner(request):
            raise web.HTTPForbidden(text="Owner only")
        db.remove_code(request.match_info["code"])
        raise back("/codes", "Code removed")

    @routes.post("/codes/{code}/reactivate")
    async def reactivate_code(request):
        if not owner(request):
            raise web.HTTPForbidden(text="Owner only")
        db.set_code_status(request.match_info["code"], "active")
        raise back("/codes", "Code set back to active")

    @routes.post("/check-salt")
    async def check_salt(request):
        require_any_manager(request)
        await redeemer.check_salt()
        raise back("/codes", f"Salt check: {redeemer.salt_status}")

    @routes.post("/redeem")
    async def redeem_now(request):
        require_any_manager(request)
        if redeemer.running:
            raise back("/codes", "A run is already in progress", err=True)

        async def job():
            await redeemer.check_feed()
            await redeemer.run()
        spawn(job())
        raise back("/codes", "Redemption run started - refresh in a minute")

    app = web.Application(middlewares=[auth])
    app.add_routes(routes)
    app.router.add_static("/static/", Path(__file__).parent / "assets", show_index=False)
    return app


async def start_web(redeemer: Redeemer, bot) -> web.AppRunner:
    runner = web.AppRunner(create_app(redeemer, bot))
    await runner.setup()
    await web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT).start()
    return runner
