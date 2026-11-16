"""Discord side: slash commands, reminder sending, role buttons and reports.
Works across many servers; each server has its own players, reminders and settings."""
import asyncio
import io
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from discord import app_commands

from . import config
from .redeemer import Redeemer
from .tasks import spawn
from . import access as acl
from .alert_card import render_card
from .kvk import compare as kvk_compare, fmt as fmt_power
from .plans import PLAN_KINDS, render_plan
from .reminders import (KINDS, build_alert, heartbeat_text, next_occurrence, parse_before, parse_when,
                        reminder_loop)

log = logging.getLogger("discord")
ROLE_BUTTON_PREFIX = "ksrole:"

# What "Auto setup" creates. Keys line up with reminder kinds so reminders can find their channel/role.
AUTO_CATEGORY = "👑 Kingshot"
AUTO_CHANNELS = [  # (key, name, topic)
    ("gift_codes", "🎁-gift-codes", "Gift codes are found and redeemed automatically - results are posted here."),
    ("bear_trap", "🐻-bear-trap", "Bear Trap reminders."),
    ("arena", "⚔️-arena", "Arena reset reminders."),
    ("custom", "📅-events", "Event reminders."),
    ("alerts", "🔔-pick-your-alerts", "Tap a button to choose which reminders ping you."),
]
AUTO_ROLES = [  # (key, name)
    ("bear_trap", "🐻 • Bear Trap — Group 1"),
    ("bear_trap_2", "🐻 • Bear Trap — Group 2"),
    ("arena", "⚔️ • Arena alerts"),
    ("custom", "📅 • Event alerts"),
    ("manager", "Kingshot Manager"),
]
# which auto-setup channel each reminder kind posts in
KIND_CHANNEL = {"bear_trap": "bear_trap", "bear_trap_2": "bear_trap", "arena": "arena", "custom": "custom"}


class KingshotBot(discord.Client):
    def __init__(self, redeemer: Redeemer):
        super().__init__(intents=discord.Intents.default())
        self.redeemer = redeemer
        self.db = redeemer.db
        self.tree = app_commands.CommandTree(self)
        self._member_cache: dict = {}
        redeemer.on_new_codes.append(self.report_new_codes)
        redeemer.on_run_finished.append(self.report_run)
        redeemer.on_alert.append(self.report_alert)
        register_commands(self)

    # ---------- lifecycle ----------
    async def setup_hook(self):
        await self.tree.sync()

        async def start_reminders():
            await self.wait_until_ready()      # channels aren't known until the bot is connected
            while True:                        # reminder_loop never returns; this is a last-resort guard
                try:
                    await reminder_loop(self.db, self.send_reminder)
                except Exception:
                    log.exception("Reminder checker stopped - restarting in 10s")
                    await asyncio.sleep(10)
        self.reminder_task = spawn(start_reminders(), "reminders")
        log.info("Slash commands synced")

    async def on_ready(self):
        log.info("Logged in as %s, in %d servers", self.user, len(self.guilds))
        for g in self.guilds:
            await self._check_allowed(g)

    async def on_guild_join(self, guild: discord.Guild):
        await self._check_allowed(guild)

    async def _check_allowed(self, guild: discord.Guild):
        if config.ALLOWED_GUILD_IDS and guild.id not in config.ALLOWED_GUILD_IDS:
            log.warning("Leaving non-allowed server %s (%s)", guild.name, guild.id)
            await guild.leave()

    # ---------- permissions ----------
    def is_owner(self, user_id: int) -> bool:
        return int(user_id) in config.OWNER_IDS

    async def can_manage(self, guild_id: int, user_id: int, perms: discord.Permissions | None = None,
                         role_ids: set[int] | None = None) -> bool:
        """Owner, server admins (Manage Server) or members with the server's manager role."""
        if self.is_owner(user_id):
            return True
        guild = self.get_guild(int(guild_id))
        if not guild:
            return False
        if perms is None or role_ids is None:
            try:
                member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
            except discord.HTTPException:
                return False
            perms, role_ids = member.guild_permissions, {r.id for r in member.roles}
        if perms.manage_guild or perms.administrator:
            return True
        role_id = self.db.guild_settings(guild_id).get("manager_role_id")
        return bool(role_id) and int(role_id) in role_ids

    async def member_context(self, guild_id, user_id) -> tuple[bool, bool, set]:
        """(is_member, is_admin, role_ids) for the website. Cached for 60 seconds."""
        import time as _t
        key = (int(guild_id), int(user_id))
        hit = self._member_cache.get(key)
        if hit and _t.time() - hit[0] < 60:
            return hit[1]
        guild = self.get_guild(int(guild_id))
        result = (False, False, set())
        if guild:
            try:
                m = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
                roles = {r.id for r in m.roles}
                mgr = self.db.guild_settings(guild_id).get("manager_role_id")
                admin = (m.guild_permissions.manage_guild or m.guild_permissions.administrator
                         or guild.owner_id == int(user_id) or bool(mgr and int(mgr) in roles))
                result = (True, admin, roles)
            except discord.HTTPException:
                pass
        self._member_cache[key] = (_t.time(), result)
        return result

    # ---------- helpers used by the website ----------
    def guild_channels(self, guild_id) -> list[discord.TextChannel]:
        g = self.get_guild(int(guild_id))
        return sorted(g.text_channels, key=lambda c: c.position) if g else []

    def guild_roles(self, guild_id) -> list[discord.Role]:
        g = self.get_guild(int(guild_id))
        if not g:
            return []
        return [r for r in reversed(g.roles) if not r.is_default() and not r.managed]

    async def create_role(self, guild_id, name: str) -> discord.Role | None:
        g = self.get_guild(int(guild_id))
        if not g:
            return None
        try:
            return await g.create_role(name=name, mentionable=True, reason=f"{config.BOT_NAME} alert role")
        except discord.Forbidden:
            return None

    async def post_role_panel(self, guild_id, channel_id) -> str:
        """Post a message with one button per reminder role, so members can opt in/out."""
        g = self.get_guild(int(guild_id))
        channel = g.get_channel(int(channel_id)) if g else None
        if not channel:
            return "Channel not found."
        candidates = []   # (role_id, kind)
        auto_roles = self.db.guild_settings(guild_id)["auto"].get("roles", {})
        for kind in ("bear_trap", "bear_trap_2", "arena", "custom"):
            if auto_roles.get(kind):
                candidates.append((str(auto_roles[kind]), kind))
        for rem in self.db.list_reminders(guild_id):
            if rem["role_id"]:
                candidates.append((rem["role_id"], rem["kind"]))
        seen, view = set(), discord.ui.View(timeout=None)
        for role_id, kind in candidates:
            role = g.get_role(int(role_id))
            if not role or role_id in seen or len(seen) >= 20:
                continue
            seen.add(role_id)
            emoji = KINDS.get(kind, KINDS["custom"])["emoji"]
            view.add_item(discord.ui.Button(label=role.name, emoji=None if role.name.startswith(emoji) else emoji,
                                            style=discord.ButtonStyle.secondary,
                                            custom_id=f"{ROLE_BUTTON_PREFIX}{role.id}"))
        if not seen:
            return "No alert roles yet. Run Auto setup, or give a reminder a role first."
        try:
            msg = await channel.send("**Pick your alerts** - tap a button to get (or stop getting) pinged.\n"
                                     "Reminders are always posted in their channel either way.", view=view)
        except discord.Forbidden:
            return "I don't have permission to post in that channel."
        self.last_panel_id = msg.id
        return f"Role buttons posted in #{channel.name}."

    async def auto_setup(self, guild_id) -> tuple[bool, list[str]]:
        """Create (or reuse) the Kingshot category, channels and roles, and wire them up.
        Safe to run again: anything already made is reused, nothing is duplicated."""
        g = self.get_guild(int(guild_id))
        if not g:
            return False, ["The bot isn't in that server."]
        perms = g.me.guild_permissions
        missing = [n for n, ok in (("Manage Channels", perms.manage_channels),
                                   ("Manage Roles", perms.manage_roles)) if not ok]
        if missing:
            return False, [f"I need the {' and '.join(missing)} permission. Re-invite me with the "
                           f"'Add bot to a server' link on the website, then try again."]

        settings = self.db.guild_settings(guild_id)
        auto = settings["auto"]
        auto.setdefault("channels", {})
        auto.setdefault("roles", {})
        report, made, reused = [], 0, 0
        reason = f"{config.BOT_NAME} auto setup"

        # roles
        for key, name in AUTO_ROLES:
            role = g.get_role(int(auto["roles"].get(key) or 0)) or discord.utils.get(g.roles, name=name)
            if role:
                reused += 1
            else:
                colour = discord.Colour(int("%02x%02x%02x" % tuple(KINDS[key]["color"]), 16)) \
                    if key in KINDS else discord.Colour.default()
                role = await g.create_role(name=name, mentionable=(key != "manager"), colour=colour, reason=reason)
                made += 1
            auto["roles"][key] = role.id

        # category
        cat = g.get_channel(int(auto.get("category") or 0)) or discord.utils.get(g.categories, name=AUTO_CATEGORY)
        if not cat:
            cat = await g.create_category(AUTO_CATEGORY, reason=reason)
            made += 1
        auto["category"] = cat.id

        # channels
        for key, name, topic in AUTO_CHANNELS:
            ch = g.get_channel(int(auto["channels"].get(key) or 0)) or discord.utils.get(cat.text_channels, name=name)
            if ch:
                reused += 1
            else:
                overwrites = {}
                if key in ("alerts", "gift_codes"):   # read-only for members, the bot can post
                    overwrites = {g.default_role: discord.PermissionOverwrite(send_messages=False),
                                  g.me: discord.PermissionOverwrite(send_messages=True, view_channel=True)}
                ch = await cat.create_text_channel(name, topic=topic, overwrites=overwrites, reason=reason)
                made += 1
            auto["channels"][key] = ch.id

        # wire up settings (don't overwrite choices the admin already made)
        if not settings.get("report_channel_id"):
            self.db.set_guild_setting(guild_id, "report_channel_id", auto["channels"]["gift_codes"])
            report.append(f"Gift-code reports → <#{auto['channels']['gift_codes']}>")
        if not settings.get("manager_role_id"):
            self.db.set_guild_setting(guild_id, "manager_role_id", auto["roles"]["manager"])
            report.append(f"Bot managers → <@&{auto['roles']['manager']}> (give this role to your R4s)")

        # role buttons, once
        alerts = g.get_channel(auto["channels"]["alerts"])
        panel_ok = False
        if auto.get("panel_message"):
            try:
                await alerts.fetch_message(int(auto["panel_message"]))
                panel_ok = True
            except discord.HTTPException:
                pass
        if not panel_ok:
            self.db.set_auto_ids(guild_id, auto)
            msg = await self.post_role_panel(guild_id, alerts.id)
            if msg.startswith("Role buttons posted"):
                auto["panel_message"] = getattr(self, "last_panel_id", None)
                report.append(f"Role buttons posted in <#{alerts.id}>")

        self.db.set_auto_ids(guild_id, auto)
        self.db.log(f"Auto setup: {made} created, {reused} reused", guild_id)
        report.insert(0, f"✅ Auto setup done - {made} created, {reused} already existed.")
        report.append("New reminders will use the matching channel and role automatically.")
        return True, report

    def auto_defaults(self, guild_id, kind: str) -> tuple[str | None, str | None]:
        """(channel_id, role_id) that auto setup made for this kind of reminder, if any."""
        auto = self.db.guild_settings(guild_id)["auto"]
        ch = auto.get("channels", {}).get(KIND_CHANNEL.get(kind, "custom"))
        role = auto.get("roles", {}).get(kind)
        g = self.get_guild(int(guild_id))
        ch = ch if g and ch and g.get_channel(int(ch)) else None
        role = role if g and role and g.get_role(int(role)) else None
        return (str(ch) if ch else None, str(role) if role else None)

    async def on_interaction(self, inter: discord.Interaction):
        """Handle role-button clicks (works even after the bot restarts)."""
        if inter.type != discord.InteractionType.component:
            return
        cid = (inter.data or {}).get("custom_id", "")
        if not cid.startswith(ROLE_BUTTON_PREFIX) or not inter.guild:
            return
        role = inter.guild.get_role(int(cid[len(ROLE_BUTTON_PREFIX):]))
        if not role:
            return await inter.response.send_message("That role no longer exists.", ephemeral=True)
        try:
            if role in inter.user.roles:
                await inter.user.remove_roles(role, reason="Alert opt-out")
                msg = f"You won't be pinged for **{role.name}** any more."
            else:
                await inter.user.add_roles(role, reason="Alert opt-in")
                msg = f"You'll now be pinged for **{role.name}**."
        except discord.Forbidden:
            msg = ("I can't change that role. A server admin needs to give me **Manage Roles** "
                   "and move my role above it.")
        await inter.response.send_message(msg, ephemeral=True)

    # ---------- sending ----------
    async def _send(self, channel_id, text: str, ping_role_id=None) -> bool:
        channel = self.get_channel(int(channel_id)) if channel_id else None
        if not channel:
            return False
        if ping_role_id:
            text = f"<@&{ping_role_id}> {text}"
        try:
            await channel.send(text[:1990], allowed_mentions=discord.AllowedMentions(
                roles=True, users=False, everyone=False))
            return True
        except discord.HTTPException as e:
            log.warning("send to %s failed: %s", channel_id, e)
            return False

    async def send_reminder(self, rem, occ: int, minutes_before: int, test: bool = False) -> bool:
        """Post a rich alert: coloured embed + countdown picture. Always posted in the channel,
        whether or not anyone has the role; the role (if any) is pinged in the message text."""
        channel = self.get_channel(int(rem["channel_id"])) if rem["channel_id"] else None
        if not channel:
            self.db.log(f"Reminder '{rem['name']}' - channel not found", rem["guild_id"])
            return False
        a = build_alert(rem, occ, minutes_before)
        embed = discord.Embed(title=("🧪 TEST · " if test else "") + a["title"], description=a["description"],
                              colour=a["color"], timestamp=datetime.fromtimestamp(occ, timezone.utc))
        embed.set_footer(text=f"{config.BOT_NAME} · Event alerts")
        files = []
        try:
            png = await asyncio.to_thread(render_card, a["card"], urlparse(config.PUBLIC_URL).netloc, config.BOT_NAME)
        except Exception:
            log.exception("alert card failed")
            png = None
        if png:
            files = [discord.File(io.BytesIO(png), filename="alert.png")]
            embed.set_image(url="attachment://alert.png")
        content = f"<@&{rem['role_id']}>" if rem["role_id"] else None
        try:
            await channel.send(content=content, embed=embed, files=files,
                               allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
            return True
        except discord.HTTPException as e:
            log.warning("reminder send failed: %s", e)
            self.db.log(f"Reminder '{rem['name']}' could not be posted (check channel permissions)", rem["guild_id"])
            return False

    # ---------- battle plans + KvK posts ----------
    def _channel_in(self, guild_id, channel_id):
        g = self.get_guild(int(guild_id))
        return g.get_channel(int(channel_id or 0)) if g else None

    async def post_plan(self, guild_id, plan_id: int, channel_id) -> str:
        plan = self.db.get_plan(plan_id, guild_id)
        channel = self._channel_in(guild_id, channel_id)
        if not plan or not channel:
            return "Plan or channel not found."
        k = PLAN_KINDS.get(plan["kind"], PLAN_KINDS["other"])
        png = await asyncio.to_thread(render_plan, plan["name"], plan["kind"], plan["data"], config.BOT_NAME,
                                      urlparse(config.PUBLIC_URL).netloc)
        embed = discord.Embed(title=f"{k['emoji']} {plan['name']}", description=plan["data"].get("notes") or None,
                              colour=int("%02x%02x%02x" % tuple(k["color"]), 16))
        if plan["data"].get("when"):
            w = int(plan["data"]["when"])
            embed.add_field(name="When", value=f"<t:{w}:F> (<t:{w}:R>)")
        embed.set_image(url="attachment://plan.png")
        embed.set_footer(text=f"{config.BOT_NAME} · Battle plan")
        try:
            await channel.send(embed=embed, files=[discord.File(io.BytesIO(png), filename="plan.png")])
        except discord.HTTPException:
            return "I can't post in that channel - check my permissions."
        return f"Posted in #{channel.name}"

    def kvk_embed(self, k: dict) -> discord.Embed:
        d = k["data"]
        r = kvk_compare(d["ours"], d["theirs"], int(d.get("top_n") or 10))
        colour = {"good": 0x30D158, "warn": 0xFF9F0A, "bad": 0xFF453A}.get(r["mood"], 0x8E8E93)
        e = discord.Embed(title=f"⚔️ #{d.get('our_kingdom') or '?'} vs #{d.get('their_kingdom') or '?'} · {k['title']}",
                          description=r["verdict"], colour=colour)
        e.add_field(name=f"Our top {r['n']}", value=fmt_power(r["ours"]["top_total"]))
        e.add_field(name=f"Their top {r['n']}", value=fmt_power(r["theirs"]["top_total"]))
        e.add_field(name="Matchups won", value=f"{r['wins']}/{r['n']}")
        rows = []
        for m in r["pairs"][:15]:
            o, t = m["ours"], m["theirs"]
            sign = "🟢" if m["diff"] > 0 else "🔴" if m["diff"] < 0 else "⚪"
            rows.append(f"`{m['rank']:>2}` {sign} **{(o or {}).get('name', '-')}** {fmt_power((o or {}).get('power', 0))}"
                        f" vs {(t or {}).get('name', '-')} {fmt_power((t or {}).get('power', 0))}")
        if rows:
            e.add_field(name="Rank by rank", value="\n".join(rows)[:1024], inline=False)
        if d.get("notes"):
            e.add_field(name="Notes", value=d["notes"][:1024], inline=False)
        e.set_footer(text=f"{config.BOT_NAME} · KvK comparison")
        return e

    async def post_kvk(self, guild_id, kvk_id: int, channel_id) -> str:
        k = self.db.get_kvk(kvk_id, guild_id)
        channel = self._channel_in(guild_id, channel_id)
        if not k or not channel:
            return "Comparison or channel not found."
        try:
            await channel.send(embed=self.kvk_embed(k))
        except discord.HTTPException:
            return "I can't post in that channel - check my permissions."
        return f"Posted in #{channel.name}"

    async def report_new_codes(self, codes: list[str]):
        text = "🎁 New gift code(s): " + ", ".join(f"`{c}`" for c in codes) + " - redeeming now."
        for s in self.db.all_guild_settings():
            if s["report_channel_id"]:
                await self._send(s["report_channel_id"], text)

    async def report_run(self, started: int, _summary: str):
        for s in self.db.all_guild_settings():
            if not s["report_channel_id"]:
                continue
            results = self.db.guild_results_since(s["guild_id"], started)
            if not results:
                continue
            lines = [f"`{code}`: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items()))
                     for code, c in results.items()]
            await self._send(s["report_channel_id"], "**Gift code run finished**\n" + "\n".join(lines))

    async def report_alert(self, text: str):
        for uid in config.OWNER_IDS:
            try:
                user = self.get_user(uid) or await self.fetch_user(uid)
                await user.send(text)
            except discord.HTTPException:
                pass


# ======================================================================
# Slash commands
# ======================================================================
def register_commands(bot: KingshotBot):
    tree, db, r = bot.tree, bot.db, bot.redeemer

    async def manager_check(inter: discord.Interaction) -> bool:
        if not inter.guild:
            await inter.response.send_message("Use this in a server.", ephemeral=True)
            return False
        if await bot.can_manage(inter.guild_id, inter.user.id, *member_info(inter)):
            return True
        await inter.response.send_message(
            "You need **Manage Server** or this server's bot-manager role.", ephemeral=True)
        return False

    def member_info(inter: discord.Interaction):
        u = inter.user
        return getattr(u, "guild_permissions", None), {x.id for x in getattr(u, "roles", [])}

    def managers_only():
        return app_commands.check(manager_check)

    # ---------- members ----------
    @tree.command(description="Link a Kingshot account for automatic gift codes")
    @app_commands.guild_only()
    @app_commands.describe(player_id="In-game player ID (tap your avatar in Kingshot)",
                           kingdom="Your kingdom number, e.g. 123",
                           name="Optional: in-game name, so the list is easier to read")
    async def add(inter: discord.Interaction, player_id: str, kingdom: int, name: str | None = None):
        if not config.ALLOW_PUBLIC_ADD and not await bot.can_manage(inter.guild_id, inter.user.id,
                                                                     *member_info(inter)):
            return await inter.response.send_message("Only managers can add players here.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        ok, msg = await r.add_player(player_id, kingdom, f"discord:{inter.user}", name,
                                     guild_id=inter.guild_id, discord_user_id=inter.user.id)
        if ok:
            spawn(r.run(fids=[player_id.strip()]))
            msg += " Redeeming active codes for it now."
        await inter.followup.send(msg, ephemeral=True)

    @tree.command(description="Unlink a Kingshot account")
    @app_commands.guild_only()
    async def remove(inter: discord.Interaction, player_id: str):
        p = db.get_player(player_id.strip())
        mine = p and p["discord_user_id"] == str(inter.user.id)
        in_guild = p and p["guild_id"] == str(inter.guild_id)
        if not p or not (in_guild or bot.is_owner(inter.user.id)):
            return await inter.response.send_message("That ID isn't registered in this server.", ephemeral=True)
        if not mine and not await bot.can_manage(inter.guild_id, inter.user.id, *member_info(inter)):
            return await inter.response.send_message("You can only remove your own accounts.", ephemeral=True)
        db.remove_player(p["fid"])
        db.log(f"Player removed: {p['fid']} by discord:{inter.user}", inter.guild_id)
        await inter.response.send_message("Removed.", ephemeral=True)

    @tree.command(name="myaccounts", description="Show the Kingshot accounts you've linked")
    async def myaccounts(inter: discord.Interaction):
        rows = db.list_players(discord_user_id=inter.user.id)
        body = "\n".join(f"{'✅' if p['enabled'] else '⏸️'} {p['nickname'] or ''} `{p['fid']}` (K{p['kid']})"
                         for p in rows) or "None yet - use `/add`."
        await inter.response.send_message(body, ephemeral=True)

    @tree.command(description="Show a player's redemption history")
    async def history(inter: discord.Interaction, player_id: str):
        p = db.get_player(player_id.strip())
        if not p:
            return await inter.response.send_message("That ID isn't registered.", ephemeral=True)
        rows = db.history(p["fid"])[:25]
        body = "\n".join(f"`{h['code']}` → {h['result']}" for h in rows) or "Nothing redeemed yet."
        await inter.response.send_message(
            embed=discord.Embed(title=f"{p['nickname'] or 'Player'} ({p['fid']})", description=body),
            ephemeral=True)

    @tree.command(description="Bot status for this server")
    @app_commands.guild_only()
    async def status(inter: discord.Interaction):
        e = discord.Embed(title=config.BOT_NAME)
        e.add_field(name="Players here", value=str(len(db.list_players(inter.guild_id))))
        e.add_field(name="Active codes", value=str(len(db.list_codes(only_active=True))))
        e.add_field(name="Reminders", value=str(len(db.list_reminders(inter.guild_id, only_enabled=True))))
        if r.next_run_at:
            e.add_field(name="Next code check", value=f"<t:{int(r.next_run_at)}:R>")
        e.add_field(name="Gift-code site", value=r.salt_status)
        e.add_field(name="Reminder checker", value=heartbeat_text(), inline=False)
        e.add_field(name="Website", value=config.PUBLIC_URL, inline=False)
        await inter.response.send_message(embed=e)

    @tree.command(name="help", description="What this bot can do")
    async def help_cmd(inter: discord.Interaction):
        e = discord.Embed(title=f"{config.BOT_NAME} - commands",
                          description=f"Everything here can also be done on the website: {config.PUBLIC_URL}")
        e.add_field(name="Everyone", inline=False, value=(
            "`/add` link your Kingshot account · `/remove` unlink it · `/myaccounts`\n"
            "`/history` codes redeemed · `/code list` · `/reminder list` · `/status`"))
        e.add_field(name="Managers", inline=False, value=(
            "`/autosetup` makes channels + roles · `/setup` · `/settings`\n"
            "`/players` · `/player edit` · `/player pause` · `/player resume`\n"
            "`/reminder add` · `edit` · `pause` · `resume` · `test` · `remove` · `rolebuttons`\n"
            "`/code add` · `/code redeem-now` · `/code check-salt`"))
        e.add_field(name="Plans, KvK & timeline", inline=False, value=(
            "`/timeline` · `/plan list|show|post` · `/kvk list|show|post` · `/claims`"))
        e.add_field(name="Admins", inline=False, value="`/access grant|list` officer access · `/privacy`")
        e.add_field(name="Owner", inline=False, value="`/code remove` · `/code retry`")
        e.set_footer(text="Times are UTC (Kingshot game time).")
        await inter.response.send_message(embed=e, ephemeral=True)

    # ---------- managers: server settings ----------
    @tree.command(description="Set this server up: report channel and (optional) bot-manager role")
    @app_commands.guild_only()
    @managers_only()
    @app_commands.describe(report_channel="Where gift-code results are posted",
                           manager_role="Members with this role can manage the bot (optional)")
    async def setup(inter: discord.Interaction, report_channel: discord.TextChannel,
                    manager_role: discord.Role | None = None):
        db.set_guild_setting(inter.guild_id, "report_channel_id", report_channel.id)
        if manager_role:
            db.set_guild_setting(inter.guild_id, "manager_role_id", manager_role.id)
        await inter.response.send_message(
            f"✅ Reports → {report_channel.mention}"
            + (f"\n✅ Managers → {manager_role.mention}" if manager_role else "")
            + f"\nWebsite: {config.PUBLIC_URL}", ephemeral=True)

    @tree.command(description="Show this server's bot settings")
    @app_commands.guild_only()
    @managers_only()
    async def settings(inter: discord.Interaction):
        s = db.guild_settings(inter.guild_id)
        rc, mr = s.get("report_channel_id"), s.get("manager_role_id")
        await inter.response.send_message(
            f"Report channel: {f'<#{rc}>' if rc else 'not set - use `/setup`'}\n"
            f"Manager role: {f'<@&{mr}>' if mr else 'none (only Manage Server)'}\n"
            f"Players: {len(db.list_players(inter.guild_id))} · "
            f"Reminders: {len(db.list_reminders(inter.guild_id))}", ephemeral=True)

    @tree.command(name="autosetup", description="Create the Kingshot category, channels and alert roles for you")
    @app_commands.guild_only()
    @managers_only()
    async def autosetup(inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        try:
            ok, lines = await bot.auto_setup(inter.guild_id)
        except discord.Forbidden:
            ok, lines = False, ["Discord refused - check my role has Manage Channels and Manage Roles."]
        await inter.followup.send("\n".join(lines), ephemeral=True)

    @tree.command(description="List this server's players")
    @app_commands.guild_only()
    @managers_only()
    async def players(inter: discord.Interaction):
        rows = db.list_players(inter.guild_id)
        if not rows:
            return await inter.response.send_message("No players yet.", ephemeral=True)
        text = "\n".join(f"{'✅' if p['enabled'] else '⏸️'} {p['nickname'] or ''} `{p['fid']}` (K{p['kid']})"
                         for p in rows)
        if len(text) > 1900:
            text = text[:1900] + f"\n… ({len(rows)} total)"
        await inter.response.send_message(
            embed=discord.Embed(title=f"Players ({len(rows)})", description=text), ephemeral=True)

    # ---------- /player edit | pause | resume ----------
    player = app_commands.Group(name="player", description="Edit, pause or resume a linked account",
                                guild_only=True)

    async def find_player(inter, player_id, allow_own=False):
        p = db.get_player(player_id.strip())
        if not p or (p["guild_id"] != str(inter.guild_id) and not bot.is_owner(inter.user.id)):
            await inter.response.send_message("That ID isn't registered in this server.", ephemeral=True)
            return None
        if allow_own and p["discord_user_id"] == str(inter.user.id):
            return p
        if await bot.can_manage(inter.guild_id, inter.user.id, *member_info(inter)):
            return p
        await inter.response.send_message("You can only change your own accounts.", ephemeral=True)
        return None

    @player.command(name="edit", description="Change an account's kingdom or name (also resumes it)")
    @app_commands.describe(kingdom="New kingdom number", name="New name (optional)")
    async def player_edit(inter: discord.Interaction, player_id: str, kingdom: int | None = None,
                          name: str | None = None):
        p = await find_player(inter, player_id, allow_own=True)
        if not p:
            return
        db.update_player(p["fid"], name if name is not None else p["nickname"],
                         kingdom if kingdom is not None else p["kid"])
        db.set_player_enabled(p["fid"], True)
        await inter.response.send_message(
            f"✅ Updated `{p['fid']}` - kingdom {kingdom if kingdom is not None else p['kid']}. It's active again.",
            ephemeral=True)

    @player.command(name="pause", description="Stop redeeming codes for an account")
    async def player_pause(inter: discord.Interaction, player_id: str):
        p = await find_player(inter, player_id)
        if p:
            db.set_player_enabled(p["fid"], False)
            await inter.response.send_message(f"⏸️ Paused `{p['fid']}`.", ephemeral=True)

    @player.command(name="resume", description="Start redeeming codes for an account again")
    async def player_resume(inter: discord.Interaction, player_id: str):
        p = await find_player(inter, player_id)
        if p:
            db.set_player_enabled(p["fid"], True)
            await inter.response.send_message(f"▶️ Resumed `{p['fid']}`.", ephemeral=True)

    tree.add_command(player)

    # ---------- /code ... ----------
    code_group = app_commands.Group(name="code", description="Gift codes")

    @code_group.command(name="list", description="List gift codes")
    async def code_list(inter: discord.Interaction):
        rows = db.list_codes()[:30]
        body = "\n".join(f"{'🟢' if c['status'] == 'active' else '🔴'} `{c['code']}` ({c['status']})"
                         for c in rows) or "No codes yet."
        await inter.response.send_message(embed=discord.Embed(title="Gift codes", description=body))

    @code_group.command(name="add", description="Add a gift code and redeem it for everyone")
    @managers_only()
    async def code_add(inter: discord.Interaction, code: str):
        if not r.add_code(code, f"discord:{inter.user}"):
            return await inter.response.send_message("That code is already saved.", ephemeral=True)
        await inter.response.send_message(f"✅ Added `{code}`. Redeeming - results go to the report channel.",
                                          ephemeral=True)
        spawn(r.run(codes=[code.strip()]))

    @code_group.command(name="redeem-now", description="Check for new codes and redeem everything now")
    @managers_only()
    async def code_redeem(inter: discord.Interaction):
        if r.running:
            return await inter.response.send_message("A run is already in progress.", ephemeral=True)
        await inter.response.send_message("Started - results will be posted in the report channel.",
                                          ephemeral=True)

        async def job():
            await r.check_feed()
            await r.run()
        spawn(job())

    @code_group.command(name="check-salt", description="Test the connection to the gift-code website")
    @managers_only()
    async def code_check_salt(inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        await r.check_salt()
        await inter.followup.send(f"Gift-code site: **{r.salt_status}**", ephemeral=True)

    async def owner_check(inter: discord.Interaction) -> bool:
        if bot.is_owner(inter.user.id):
            return True
        await inter.response.send_message("Only the bot owner can do that.", ephemeral=True)
        return False

    @code_group.command(name="remove", description="Delete a code (owner only)")
    @app_commands.check(owner_check)
    async def code_remove(inter: discord.Interaction, code: str):
        ok = db.remove_code(code.strip())
        await inter.response.send_message("Deleted." if ok else "No such code.", ephemeral=True)

    @code_group.command(name="retry", description="Mark an expired/invalid code as active again (owner only)")
    @app_commands.check(owner_check)
    async def code_retry(inter: discord.Interaction, code: str):
        db.set_code_status(code.strip(), "active")
        await inter.response.send_message(f"`{code}` is active again - it'll be tried on the next run.",
                                          ephemeral=True)

    tree.add_command(code_group)

    # ---------- reminders ----------
    reminder = app_commands.Group(name="reminder", description="Bear Trap, Arena and event reminders",
                                  guild_only=True)

    @reminder.command(name="add", description="Create a reminder (times are UTC, like the game)")
    @managers_only()
    @app_commands.describe(
        kind="Bear Trap, Arena reset or a custom event",
        time="Start time in UTC, HH:MM (e.g. 20:30)",
        date="First date, YYYY-MM-DD (optional - default is the next time that clock time comes round)",
        repeat_hours="Repeat every N hours (Bear Trap default 48, Arena 24, 0 = once)",
        before="Minutes before to alert, comma separated (e.g. 30,10,0)",
        role="Role to ping (default: the alert role from auto setup, if any)",
        channel="Channel to post in (default: the auto-setup channel, or this channel)",
        name="Custom name (optional)",
        message="Extra text added to the alert (optional)")
    @app_commands.choices(kind=[app_commands.Choice(name=v["label"], value=k) for k, v in KINDS.items()])
    async def reminder_add(inter: discord.Interaction, kind: app_commands.Choice[str], time: str,
                           date: str | None = None, repeat_hours: int | None = None,
                           before: str | None = None, role: discord.Role | None = None,
                           channel: discord.TextChannel | None = None, name: str | None = None,
                           message: str | None = None):
        preset = KINDS[kind.value]
        try:
            start = parse_when(time, date)
        except ValueError:
            return await inter.response.send_message("Time must be HH:MM and date YYYY-MM-DD.", ephemeral=True)
        repeat = preset["repeat_minutes"] if repeat_hours is None else max(0, repeat_hours) * 60
        before_text = ",".join(str(b) for b in parse_before(before or preset["before"]))
        auto_ch, auto_role = bot.auto_defaults(inter.guild_id, kind.value)
        ch = channel or (inter.guild.get_channel(int(auto_ch)) if auto_ch else None) or inter.channel
        if role is None and auto_role:
            role = inter.guild.get_role(int(auto_role))
        rid = db.add_reminder(inter.guild_id, kind.value, name or preset["label"], ch.id, start,
                              repeat, before_text, role.id if role else None, message)
        db.log(f"Reminder #{rid} '{name or preset['label']}' created by {inter.user}", inter.guild_id)
        await inter.response.send_message(
            f"{preset['emoji']} Reminder **#{rid}** created: first at <t:{start}:F> (<t:{start}:R>), "
            f"{'every ' + str(repeat // 60) + 'h' if repeat else 'once'}, alerts {before_text} min before, "
            f"in {ch.mention}" + (f", pinging {role.mention}" if role else "") + ".", ephemeral=True)

    @reminder.command(name="list", description="Show this server's reminders")
    async def reminder_list(inter: discord.Interaction):
        import time as _t
        rows = db.list_reminders(inter.guild_id)
        if not rows:
            return await inter.response.send_message("No reminders yet. Use `/reminder add`.", ephemeral=True)
        lines = []
        for rem in rows:
            nxt = next_occurrence(rem, _t.time())
            lines.append(f"**#{rem['id']}** {KINDS.get(rem['kind'], KINDS['custom'])['emoji']} {rem['name']} - "
                         + (f"next <t:{nxt}:R>" if nxt else "finished")
                         + ("" if rem["enabled"] else " (paused)"))
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    @reminder.command(name="remove", description="Delete a reminder")
    @managers_only()
    async def reminder_remove(inter: discord.Interaction, reminder_id: int):
        ok = db.remove_reminder(reminder_id, inter.guild_id)
        await inter.response.send_message("Deleted." if ok else "No reminder with that number here.",
                                          ephemeral=True)

    async def find_reminder(inter, reminder_id):
        rem = db.get_reminder(reminder_id)
        if not rem or rem["guild_id"] != str(inter.guild_id):
            await inter.response.send_message("No reminder with that number here. See `/reminder list`.",
                                              ephemeral=True)
            return None
        return rem

    @reminder.command(name="edit", description="Change a reminder - only fill in what you want to change")
    @managers_only()
    @app_commands.describe(time="New start time UTC, HH:MM", date="New first date, YYYY-MM-DD",
                           repeat_hours="Repeat every N hours (0 = once)", before="e.g. 30,10,0",
                           role="Role to ping", no_role="Stop pinging a role (just post)",
                           channel="Channel", name="Name", message="Extra text")
    async def reminder_edit(inter: discord.Interaction, reminder_id: int, time: str | None = None,
                            date: str | None = None, repeat_hours: int | None = None,
                            before: str | None = None, role: discord.Role | None = None,
                            no_role: bool = False, channel: discord.TextChannel | None = None,
                            name: str | None = None, message: str | None = None):
        import time as _t
        rem = await find_reminder(inter, reminder_id)
        if not rem:
            return
        f = {}
        if time or date:
            from datetime import datetime, timezone
            cur = datetime.fromtimestamp(rem["start_at"], timezone.utc)
            try:
                f["start_at"] = parse_when(time or cur.strftime("%H:%M"), date or cur.strftime("%Y-%m-%d"))
            except ValueError:
                return await inter.response.send_message("Time must be HH:MM and date YYYY-MM-DD.",
                                                         ephemeral=True)
        if repeat_hours is not None:
            f["repeat_minutes"] = max(0, repeat_hours) * 60
        if before:
            f["before_minutes"] = ",".join(str(b) for b in parse_before(before))
        if role:
            f["role_id"] = str(role.id)
        if no_role:
            f["role_id"] = None
        if channel:
            f["channel_id"] = str(channel.id)
        if name:
            f["name"] = name
        if message is not None:
            f["message"] = message or None
        if not f:
            return await inter.response.send_message("Nothing to change - fill in at least one option.",
                                                     ephemeral=True)
        db.update_reminder(rem["id"], **f, enabled=1, last_fire_ts=int(_t.time()))
        new = db.get_reminder(rem["id"])
        nxt = next_occurrence(new, _t.time())
        await inter.response.send_message(
            f"✅ Reminder **#{rem['id']}** updated." + (f" Next: <t:{nxt}:F> (<t:{nxt}:R>)" if nxt else ""),
            ephemeral=True)

    @reminder.command(name="pause", description="Pause a reminder")
    @managers_only()
    async def reminder_pause(inter: discord.Interaction, reminder_id: int):
        rem = await find_reminder(inter, reminder_id)
        if rem:
            db.update_reminder(rem["id"], enabled=0)
            await inter.response.send_message(f"⏸️ Reminder #{rem['id']} paused.", ephemeral=True)

    @reminder.command(name="resume", description="Resume a paused reminder")
    @managers_only()
    async def reminder_resume(inter: discord.Interaction, reminder_id: int):
        import time as _t
        rem = await find_reminder(inter, reminder_id)
        if rem:
            db.update_reminder(rem["id"], enabled=1, last_fire_ts=int(_t.time()))
            await inter.response.send_message(f"▶️ Reminder #{rem['id']} resumed.", ephemeral=True)

    @reminder.command(name="test", description="Send a reminder right now to check it works")
    @managers_only()
    async def reminder_test(inter: discord.Interaction, reminder_id: int):
        rem = db.get_reminder(reminder_id)
        if not rem or rem["guild_id"] != str(inter.guild_id):
            return await inter.response.send_message("No reminder with that number here.", ephemeral=True)
        import time as _t
        nxt = next_occurrence(rem, _t.time()) or int(rem["start_at"])
        ok = await bot.send_reminder(rem, nxt, parse_before(rem["before_minutes"])[0], test=True)
        await inter.response.send_message("Sent." if ok else "Couldn't post - check my channel permissions.",
                                          ephemeral=True)

    @reminder.command(name="rolebuttons", description="Post buttons so members can pick their alert roles")
    @managers_only()
    async def reminder_rolebuttons(inter: discord.Interaction, channel: discord.TextChannel | None = None):
        msg = await bot.post_role_panel(inter.guild_id, (channel or inter.channel).id)
        await inter.response.send_message(msg, ephemeral=True)

    tree.add_command(reminder)

    # ======================================================================
    # Officer access, privacy, timeline, claims, battle plans, KvK
    # ======================================================================
    def perms_for(inter: discord.Interaction) -> dict:
        """Same rules as the website (see access.py), from the roles Discord sends with the command."""
        u = inter.user
        gp = getattr(u, "guild_permissions", None)
        roles = {x.id for x in getattr(u, "roles", [])}
        mgr = db.guild_settings(inter.guild_id).get("manager_role_id")
        admin = bool(gp and (gp.manage_guild or gp.administrator)) or bool(mgr and int(mgr) in roles)
        return acl.resolve(is_owner=bot.is_owner(u.id), is_admin=admin, is_member=True, user_id=u.id,
                           role_ids=roles, grants=db.list_grants(inter.guild_id),
                           prefs=db.guild_settings(inter.guild_id)["prefs"])

    async def need(inter, section: str, level: int) -> dict | None:
        p = perms_for(inter)
        if p.get(section, 0) >= level:
            return p
        word = "change" if level == 2 else "see"
        await inter.response.send_message(
            f"You don't have permission to {word} **{acl.SECTIONS.get(section, section)}** here. "
            "Ask a server admin for dashboard access.", ephemeral=True)
        return None

    section_choices = [app_commands.Choice(name=v, value=k) for k, v in acl.SECTIONS.items()] + \
                      [app_commands.Choice(name="Everything", value="all")]
    level_choices = [app_commands.Choice(name="View", value="view"), app_commands.Choice(name="Edit", value="edit"),
                     app_commands.Choice(name="No access", value="none")]

    # ---------- /access ----------
    access_grp = app_commands.Group(name="access", description="Give officers dashboard access", guild_only=True)

    @access_grp.command(name="grant", description="Let a role or member view/edit part of the dashboard")
    @app_commands.describe(section="Which part", level="View, edit or remove", role="A role (e.g. R4)",
                           member="Or a single member")
    @app_commands.choices(section=section_choices, level=level_choices)
    async def access_grant(inter: discord.Interaction, section: app_commands.Choice[str],
                           level: app_commands.Choice[str], role: discord.Role | None = None,
                           member: discord.Member | None = None):
        if not await need(inter, "access", 2):
            return
        if bool(role) == bool(member):
            return await inter.response.send_message("Pick either a role **or** a member.", ephemeral=True)
        ttype, tid, label = ("role", role.id, "@" + role.name) if role else ("user", member.id, member.display_name)
        current = next((g["perms"] for g in db.list_grants(inter.guild_id)
                        if g["target_type"] == ttype and g["target_id"] == str(tid)), {})
        for sec in (acl.SECTIONS if section.value == "all" else [section.value]):
            if level.value == "none":
                current.pop(sec, None)
            else:
                current[sec] = level.value
        if current:
            db.set_grant(inter.guild_id, ttype, tid, label, current)
        else:
            for g in db.list_grants(inter.guild_id):
                if g["target_type"] == ttype and g["target_id"] == str(tid):
                    db.remove_grant(g["id"], inter.guild_id)
        summary = ", ".join(f"{acl.SECTIONS[k]}: {v}" for k, v in current.items()) or "no access"
        await inter.response.send_message(f"✅ **{label}** → {summary}", ephemeral=True)

    @access_grp.command(name="list", description="Who has dashboard access")
    async def access_list(inter: discord.Interaction):
        if not await need(inter, "access", 2):
            return
        rows = db.list_grants(inter.guild_id)
        body = "\n".join(f"**{g['label']}** - " + ", ".join(f"{acl.SECTIONS.get(k, k)}: {v}" for k, v in g["perms"].items())
                         for g in rows) or "Nobody yet. Admins always have full access."
        await inter.response.send_message(body[:1900], ephemeral=True)

    tree.add_command(access_grp)

    # ---------- /privacy ----------
    @tree.command(description="Who can see recent claims, and whether names are hidden")
    @app_commands.guild_only()
    @app_commands.describe(members_see_claims="Let ordinary members see recent claims",
                           hide_names="Hide player names/IDs from members (3153•••276)")
    async def privacy(inter: discord.Interaction, members_see_claims: bool | None = None,
                      hide_names: bool | None = None):
        if not await need(inter, "settings", 2):
            return
        changes = {}
        if members_see_claims is not None:
            changes["members_see_activity"] = members_see_claims
        if hide_names is not None:
            changes["mask_for_members"] = hide_names
        prefs = db.set_prefs(inter.guild_id, **changes) if changes else db.guild_settings(inter.guild_id)["prefs"]
        await inter.response.send_message(
            f"Members see recent claims: **{'yes' if prefs['members_see_activity'] else 'no'}**\n"
            f"Names hidden from members: **{'yes' if prefs['mask_for_members'] else 'no'}**", ephemeral=True)

    # ---------- /timeline ----------
    @tree.command(name="timeline", description="Everything coming up in the next 7 days")
    @app_commands.guild_only()
    async def timeline_cmd(inter: discord.Interaction):
        import time as _t
        from .reminders import timeline as _timeline
        events = _timeline(db.list_reminders(inter.guild_id), _t.time(), 7)
        lines = [f"{KINDS.get(e['rem']['kind'], KINDS['custom'])['emoji']} <t:{e['occ']}:F> - **{e['rem']['name']}** "
                 f"(<t:{e['occ']}:R>)" for e in events[:20]]
        await inter.response.send_message(embed=discord.Embed(
            title="Next 7 days", description="\n".join(lines) or "Nothing scheduled.", colour=0xFF9F0A),
            ephemeral=True)

    # ---------- /claims ----------
    @tree.command(description="Recent gift-code claims in this server")
    @app_commands.guild_only()
    async def claims(inter: discord.Interaction):
        p = await need(inter, "claims", 1)
        if not p:
            return
        rows = db.recent_claims(inter.guild_id, 20)
        lines = []
        for c in rows:
            name = acl.mask_name(c["nickname"]) if p["mask"] else (c["nickname"] or c["fid"])
            fid = acl.mask_id(c["fid"]) if p["mask"] else c["fid"]
            icon = {"success": "✅", "already": "☑️"}.get(c["result"], "⚠️")
            lines.append(f"{icon} **{name}** `{fid}` · `{c['code']}` · <t:{c['at']}:R>")
        await inter.response.send_message(embed=discord.Embed(
            title="Recent claims", description="\n".join(lines) or "No claims yet.", colour=0x30D158), ephemeral=True)

    # ---------- /plan ----------
    plan_grp = app_commands.Group(name="plan", description="Battle plans (build them on the website)", guild_only=True)

    @plan_grp.command(name="list", description="Show this server's battle plans")
    async def plan_list(inter: discord.Interaction):
        if not await need(inter, "plans", 1):
            return
        rows = db.list_plans(inter.guild_id)
        body = "\n".join(f"**#{pl['id']}** {PLAN_KINDS.get(pl['kind'], PLAN_KINDS['other'])['emoji']} {pl['name']} - "
                         f"{len(pl['data']['squads'])} squads" for pl in rows) or \
            f"No plans yet. Build one on the website: {config.PUBLIC_URL}"
        await inter.response.send_message(body[:1900], ephemeral=True)

    @plan_grp.command(name="show", description="See a plan's image (only you see it)")
    async def plan_show(inter: discord.Interaction, plan_id: int):
        if not await need(inter, "plans", 1):
            return
        plan = db.get_plan(plan_id, inter.guild_id)
        if not plan:
            return await inter.response.send_message("No plan with that number. See `/plan list`.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        png = await asyncio.to_thread(render_plan, plan["name"], plan["kind"], plan["data"], config.BOT_NAME,
                                      urlparse(config.PUBLIC_URL).netloc)
        await inter.followup.send(file=discord.File(io.BytesIO(png), filename="plan.png"), ephemeral=True)

    @plan_grp.command(name="post", description="Post a plan in a channel for everyone")
    async def plan_post(inter: discord.Interaction, plan_id: int, channel: discord.TextChannel | None = None):
        if not await need(inter, "plans", 2):
            return
        await inter.response.defer(ephemeral=True)
        msg = await bot.post_plan(inter.guild_id, plan_id, (channel or inter.channel).id)
        await inter.followup.send(msg, ephemeral=True)

    tree.add_command(plan_grp)

    # ---------- /kvk ----------
    kvk_grp = app_commands.Group(name="kvk", description="KvK comparisons (enter players on the website)",
                                 guild_only=True)

    @kvk_grp.command(name="list", description="Show saved KvK comparisons")
    async def kvk_list(inter: discord.Interaction):
        if not await need(inter, "kvk", 1):
            return
        rows = db.list_kvk(inter.guild_id)
        body = "\n".join(f"**#{k['id']}** {k['title']}" for k in rows) or \
            f"No comparisons yet. Add one on the website: {config.PUBLIC_URL}"
        await inter.response.send_message(body[:1900], ephemeral=True)

    @kvk_grp.command(name="show", description="See a comparison (only you see it)")
    async def kvk_show(inter: discord.Interaction, comparison_id: int):
        if not await need(inter, "kvk", 1):
            return
        k = db.get_kvk(comparison_id, inter.guild_id)
        if not k:
            return await inter.response.send_message("No comparison with that number. See `/kvk list`.", ephemeral=True)
        await inter.response.send_message(embed=bot.kvk_embed(k), ephemeral=True)

    @kvk_grp.command(name="post", description="Post a comparison in a channel for everyone")
    async def kvk_post(inter: discord.Interaction, comparison_id: int, channel: discord.TextChannel | None = None):
        if not await need(inter, "kvk", 2):
            return
        msg = await bot.post_kvk(inter.guild_id, comparison_id, (channel or inter.channel).id)
        await inter.response.send_message(msg, ephemeral=True)

    tree.add_command(kvk_grp)
