"""SQLite storage. Everything server-specific is keyed by Discord guild_id.

Gift codes and redemption results are global: a code redeemed for a player
is done, whichever server that player belongs to.
"""
import json
import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    fid             TEXT PRIMARY KEY,
    guild_id        TEXT,                -- which Discord server this player belongs to
    nickname        TEXT,
    kid             INTEGER,
    enabled         INTEGER NOT NULL DEFAULT 1,
    added_by        TEXT,
    discord_user_id TEXT,                -- who linked it (lets members manage their own)
    added_at        INTEGER
);
CREATE TABLE IF NOT EXISTS codes (
    code      TEXT PRIMARY KEY,
    status    TEXT NOT NULL DEFAULT 'active',   -- active | expired | invalid | limit
    source    TEXT,
    added_at  INTEGER
);
CREATE TABLE IF NOT EXISTS redemptions (
    fid       TEXT NOT NULL,
    code      TEXT NOT NULL,
    result    TEXT NOT NULL,
    message   TEXT,
    at        INTEGER,
    PRIMARY KEY (fid, code)
);
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id          TEXT PRIMARY KEY,
    report_channel_id TEXT,              -- where redemption reports go
    manager_role_id   TEXT,              -- members with this role can manage the server's bot
    auto_ids          TEXT,              -- JSON: channels/roles made by auto setup
    prefs             TEXT               -- JSON: privacy and other per-server options
);
CREATE TABLE IF NOT EXISTS access_grants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    target_type TEXT NOT NULL,           -- role | user
    target_id   TEXT NOT NULL,
    label       TEXT,                    -- role name / user name, for display
    perms       TEXT NOT NULL,           -- JSON {section: "view" | "edit"}
    UNIQUE (guild_id, target_type, target_id)
);
CREATE TABLE IF NOT EXISTS battle_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'kvk',   -- castle | kvk | bear | other
    data        TEXT NOT NULL,                 -- JSON: when, notes, squads
    updated_by  TEXT,
    updated_at  INTEGER
);
CREATE TABLE IF NOT EXISTS kvk_compare (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    data        TEXT NOT NULL,                 -- JSON: our/their kingdom, players, notes
    updated_by  TEXT,
    updated_at  INTEGER
);
CREATE TABLE IF NOT EXISTS reminders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       TEXT NOT NULL,
    kind           TEXT NOT NULL,        -- bear_trap | arena | custom
    name           TEXT NOT NULL,
    channel_id     TEXT NOT NULL,
    role_id        TEXT,                 -- optional role to ping
    start_at       INTEGER NOT NULL,     -- first occurrence, unix seconds (UTC)
    repeat_minutes INTEGER NOT NULL DEFAULT 0,  -- 0 = one-off
    before_minutes TEXT NOT NULL DEFAULT '10,0',
    message        TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_fire_ts   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    username   TEXT,
    avatar     TEXT,
    guilds     TEXT,                     -- JSON {manage: [...], member: [...]} guild ids
    expires    INTEGER
);
CREATE TABLE IF NOT EXISTS unlock_settings (
    guild_id   TEXT PRIMARY KEY,
    data       TEXT NOT NULL                  -- JSON, see unlocks.DEFAULTS
);
CREATE TABLE IF NOT EXISTS unlock_sent (
    guild_id   TEXT NOT NULL,
    key        TEXT NOT NULL,                 -- "<unlock>:<days before>"
    at         INTEGER,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        INTEGER,
    guild_id  TEXT,
    text      TEXT
);
"""

DONE_RESULTS = ("success", "already")

# Per-server privacy defaults: activity is managers-only, and names/IDs are masked for members.
DEFAULT_PREFS = {
    "members_see_activity": False,   # can ordinary members see recent claims + activity?
    "mask_for_members": True,        # if they can, hide player names/IDs (3153•••276)
    "next_kvk": None,                # "YYYY-MM-DD HH:MM" UTC, for the countdown on the KvK page
}


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._migrate_old()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _migrate_old(self):
        """Upgrade a database made by the first (single-server) version."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(players)")}
        if cols and "guild_id" not in cols:
            self.conn.execute("ALTER TABLE players ADD COLUMN guild_id TEXT")
            self.conn.execute("ALTER TABLE players ADD COLUMN discord_user_id TEXT")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(guild_settings)")}
        if cols and "auto_ids" not in cols:
            self.conn.execute("ALTER TABLE guild_settings ADD COLUMN auto_ids TEXT")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(guild_settings)")}
        if cols and "prefs" not in cols:
            self.conn.execute("ALTER TABLE guild_settings ADD COLUMN prefs TEXT")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(log)")}
        if cols and "guild_id" not in cols:
            self.conn.execute("ALTER TABLE log ADD COLUMN guild_id TEXT")
        self.conn.commit()

    def _exec(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    # ---------- players ----------
    def add_player(self, fid, nickname, kid, added_by, guild_id=None, discord_user_id=None) -> bool:
        try:
            self._exec(
                "INSERT INTO players (fid, guild_id, nickname, kid, added_by, discord_user_id, added_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (fid, str(guild_id) if guild_id else None, nickname, kid, added_by,
                 str(discord_user_id) if discord_user_id else None, int(time.time())),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def update_player(self, fid, nickname, kid) -> None:
        self._exec("UPDATE players SET nickname=?, kid=? WHERE fid=?", (nickname, kid, fid))

    def remove_player(self, fid: str) -> bool:
        self._exec("DELETE FROM redemptions WHERE fid=?", (fid,))
        return self._exec("DELETE FROM players WHERE fid=?", (fid,)).rowcount > 0

    def set_player_enabled(self, fid: str, enabled: bool) -> None:
        self._exec("UPDATE players SET enabled=? WHERE fid=?", (1 if enabled else 0, fid))

    def get_player(self, fid: str):
        return self.conn.execute("SELECT * FROM players WHERE fid=?", (fid,)).fetchone()

    def list_players(self, guild_id=None, only_enabled=False, discord_user_id=None):
        sql, args = "SELECT * FROM players WHERE 1=1", []
        if guild_id is not None:
            sql += " AND guild_id=?"
            args.append(str(guild_id))
        if discord_user_id is not None:
            sql += " AND discord_user_id=?"
            args.append(str(discord_user_id))
        if only_enabled:
            sql += " AND enabled=1"
        return self.conn.execute(sql + " ORDER BY kid, nickname", args).fetchall()

    def count_players_by_guild(self) -> dict:
        rows = self.conn.execute("SELECT guild_id, COUNT(*) n FROM players GROUP BY guild_id")
        return {r["guild_id"]: r["n"] for r in rows}

    # ---------- codes ----------
    def add_code(self, code: str, source: str) -> bool:
        try:
            self._exec("INSERT INTO codes (code, source, added_at) VALUES (?,?,?)",
                       (code, source, int(time.time())))
            return True
        except sqlite3.IntegrityError:
            return False

    def set_code_status(self, code: str, status: str) -> None:
        self._exec("UPDATE codes SET status=? WHERE code=?", (status, code))

    def remove_code(self, code: str) -> bool:
        self._exec("DELETE FROM redemptions WHERE code=?", (code,))
        return self._exec("DELETE FROM codes WHERE code=?", (code,)).rowcount > 0

    def list_codes(self, only_active: bool = False):
        sql = "SELECT * FROM codes"
        if only_active:
            sql += " WHERE status='active'"
        return self.conn.execute(sql + " ORDER BY added_at DESC").fetchall()

    # ---------- redemptions ----------
    def record(self, fid, code, result, message) -> None:
        self._exec(
            "INSERT INTO redemptions (fid, code, result, message, at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(fid, code) DO UPDATE SET result=excluded.result, "
            "message=excluded.message, at=excluded.at",
            (fid, code, result, message, int(time.time())),
        )

    def is_done(self, fid, code) -> bool:
        row = self.conn.execute("SELECT result FROM redemptions WHERE fid=? AND code=?",
                                (fid, code)).fetchone()
        return bool(row and row["result"] in DONE_RESULTS)

    def history(self, fid):
        return self.conn.execute("SELECT * FROM redemptions WHERE fid=? ORDER BY at DESC",
                                 (fid,)).fetchall()

    def code_stats(self, code: str, guild_id=None) -> dict:
        sql = "SELECT r.result, COUNT(*) n FROM redemptions r"
        args = [code]
        if guild_id is not None:
            sql += " JOIN players p ON p.fid=r.fid WHERE r.code=? AND p.guild_id=?"
            args.append(str(guild_id))
        else:
            sql += " WHERE r.code=?"
        rows = self.conn.execute(sql + " GROUP BY r.result", args).fetchall()
        return {r["result"]: r["n"] for r in rows}

    def guild_results_since(self, guild_id, since: int) -> dict:
        """{code: {result: count}} for this server's players since a time."""
        rows = self.conn.execute(
            "SELECT r.code, r.result, COUNT(*) n FROM redemptions r JOIN players p ON p.fid=r.fid "
            "WHERE p.guild_id=? AND r.at>=? GROUP BY r.code, r.result",
            (str(guild_id), since)).fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["code"], {})[r["result"]] = r["n"]
        return out

    # ---------- guild settings ----------
    def guild_settings(self, guild_id) -> dict:
        row = self.conn.execute("SELECT * FROM guild_settings WHERE guild_id=?",
                                (str(guild_id),)).fetchone()
        d = dict(row) if row else {"guild_id": str(guild_id), "report_channel_id": None,
                                   "manager_role_id": None, "auto_ids": None, "prefs": None}
        d["auto"] = json.loads(d.get("auto_ids") or "{}")
        d["prefs"] = {**DEFAULT_PREFS, **json.loads(d.get("prefs") or "{}")}
        return d

    def set_prefs(self, guild_id, **changes) -> dict:
        prefs = self.guild_settings(guild_id)["prefs"]
        prefs.update({k: v for k, v in changes.items() if k in DEFAULT_PREFS})
        self.set_guild_setting(guild_id, "prefs", json.dumps(prefs))
        return prefs

    def set_auto_ids(self, guild_id, ids: dict) -> None:
        self.set_guild_setting(guild_id, "auto_ids", json.dumps(ids))

    def set_guild_setting(self, guild_id, key: str, value) -> None:
        if key not in ("report_channel_id", "manager_role_id", "auto_ids", "prefs"):
            raise ValueError(key)
        self._exec("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (str(guild_id),))
        self._exec(f"UPDATE guild_settings SET {key}=? WHERE guild_id=?",
                   (str(value) if value else None, str(guild_id)))

    def all_guild_settings(self):
        return self.conn.execute("SELECT * FROM guild_settings").fetchall()

    # ---------- reminders ----------
    def add_reminder(self, guild_id, kind, name, channel_id, start_at, repeat_minutes,
                     before_minutes, role_id=None, message=None) -> int:
        cur = self._exec(
            "INSERT INTO reminders (guild_id, kind, name, channel_id, role_id, start_at, "
            "repeat_minutes, before_minutes, message, last_fire_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(guild_id), kind, name, str(channel_id), str(role_id) if role_id else None,
             int(start_at), int(repeat_minutes), before_minutes, message or None, int(time.time())),
        )
        return cur.lastrowid

    def update_reminder(self, rid: int, **fields) -> None:
        allowed = {"kind", "name", "channel_id", "role_id", "start_at", "repeat_minutes",
                   "before_minutes", "message", "enabled", "last_fire_ts"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE reminders SET {sets} WHERE id=?", (*fields.values(), rid))

    def remove_reminder(self, rid: int, guild_id) -> bool:
        return self._exec("DELETE FROM reminders WHERE id=? AND guild_id=?",
                          (rid, str(guild_id))).rowcount > 0

    def get_reminder(self, rid: int):
        return self.conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()

    def list_reminders(self, guild_id=None, only_enabled=False):
        sql, args = "SELECT * FROM reminders WHERE 1=1", []
        if guild_id is not None:
            sql += " AND guild_id=?"
            args.append(str(guild_id))
        if only_enabled:
            sql += " AND enabled=1"
        return self.conn.execute(sql + " ORDER BY start_at", args).fetchall()

    # ---------- web sessions ----------
    def create_session(self, user_id, username, avatar, guilds: dict, days: int = 7) -> str:
        token = secrets.token_urlsafe(32)
        self._exec("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
        self._exec("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                   (token, str(user_id), username, avatar, json.dumps(guilds),
                    int(time.time()) + days * 86400))
        return token

    def get_session(self, token: str):
        if not token:
            return None
        row = self.conn.execute("SELECT * FROM sessions WHERE token=? AND expires>?",
                                (token, int(time.time()))).fetchone()
        if not row:
            return None
        s = dict(row)
        s["guilds"] = json.loads(s["guilds"] or "{}")
        return s

    def update_session_guilds(self, token: str, guilds: dict) -> None:
        self._exec("UPDATE sessions SET guilds=? WHERE token=?", (json.dumps(guilds), token))

    def delete_session(self, token: str) -> None:
        self._exec("DELETE FROM sessions WHERE token=?", (token,))

    def recent_claims(self, guild_id, limit: int = 30):
        """Latest redemption results for this server's players, newest first."""
        return self.conn.execute(
            "SELECT r.*, p.nickname, p.kid FROM redemptions r JOIN players p ON p.fid=r.fid "
            "WHERE p.guild_id=? ORDER BY r.at DESC LIMIT ?", (str(guild_id), limit)).fetchall()

    # ---------- dashboard access (officers) ----------
    def set_grant(self, guild_id, target_type: str, target_id, label: str, perms: dict) -> None:
        self._exec(
            "INSERT INTO access_grants (guild_id, target_type, target_id, label, perms) VALUES (?,?,?,?,?) "
            "ON CONFLICT(guild_id, target_type, target_id) DO UPDATE SET label=excluded.label, perms=excluded.perms",
            (str(guild_id), target_type, str(target_id), label, json.dumps(perms)))

    def remove_grant(self, grant_id: int, guild_id) -> bool:
        return self._exec("DELETE FROM access_grants WHERE id=? AND guild_id=?",
                          (grant_id, str(guild_id))).rowcount > 0

    def list_grants(self, guild_id) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM access_grants WHERE guild_id=? ORDER BY target_type, label",
                                 (str(guild_id),)).fetchall()
        return [dict(r, perms=json.loads(r["perms"])) for r in rows]

    # ---------- battle plans ----------
    def save_plan(self, guild_id, name, kind, data: dict, by: str, plan_id: int | None = None) -> int:
        now = int(time.time())
        if plan_id:
            self._exec("UPDATE battle_plans SET name=?, kind=?, data=?, updated_by=?, updated_at=? "
                       "WHERE id=? AND guild_id=?", (name, kind, json.dumps(data), by, now, plan_id, str(guild_id)))
            return plan_id
        return self._exec("INSERT INTO battle_plans (guild_id, name, kind, data, updated_by, updated_at) "
                          "VALUES (?,?,?,?,?,?)", (str(guild_id), name, kind, json.dumps(data), by, now)).lastrowid

    def get_plan(self, plan_id: int, guild_id):
        r = self.conn.execute("SELECT * FROM battle_plans WHERE id=? AND guild_id=?", (plan_id, str(guild_id))).fetchone()
        return dict(r, data=json.loads(r["data"])) if r else None

    def list_plans(self, guild_id) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM battle_plans WHERE guild_id=? ORDER BY updated_at DESC",
                                 (str(guild_id),)).fetchall()
        return [dict(r, data=json.loads(r["data"])) for r in rows]

    def delete_plan(self, plan_id: int, guild_id) -> bool:
        return self._exec("DELETE FROM battle_plans WHERE id=? AND guild_id=?", (plan_id, str(guild_id))).rowcount > 0

    # ---------- KvK comparisons ----------
    def save_kvk(self, guild_id, title, data: dict, by: str, kvk_id: int | None = None) -> int:
        now = int(time.time())
        if kvk_id:
            self._exec("UPDATE kvk_compare SET title=?, data=?, updated_by=?, updated_at=? WHERE id=? AND guild_id=?",
                       (title, json.dumps(data), by, now, kvk_id, str(guild_id)))
            return kvk_id
        return self._exec("INSERT INTO kvk_compare (guild_id, title, data, updated_by, updated_at) VALUES (?,?,?,?,?)",
                          (str(guild_id), title, json.dumps(data), by, now)).lastrowid

    def get_kvk(self, kvk_id: int, guild_id):
        r = self.conn.execute("SELECT * FROM kvk_compare WHERE id=? AND guild_id=?", (kvk_id, str(guild_id))).fetchone()
        return dict(r, data=json.loads(r["data"])) if r else None

    def list_kvk(self, guild_id) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM kvk_compare WHERE guild_id=? ORDER BY updated_at DESC",
                                 (str(guild_id),)).fetchall()
        return [dict(r, data=json.loads(r["data"])) for r in rows]

    def delete_kvk(self, kvk_id: int, guild_id) -> bool:
        return self._exec("DELETE FROM kvk_compare WHERE id=? AND guild_id=?", (kvk_id, str(guild_id))).rowcount > 0

    # ---------- kingdom unlock reminders ----------
    def unlock_settings(self, guild_id) -> dict:
        from .unlocks import DEFAULTS
        row = self.conn.execute("SELECT data FROM unlock_settings WHERE guild_id=?", (str(guild_id),)).fetchone()
        return {**DEFAULTS, **(json.loads(row["data"]) if row else {})}

    def save_unlock_settings(self, guild_id, data: dict) -> None:
        self._exec("INSERT INTO unlock_settings VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data",
                   (str(guild_id), json.dumps(data)))

    def all_unlock_settings(self) -> list[tuple[str, dict]]:
        from .unlocks import DEFAULTS
        return [(r["guild_id"], {**DEFAULTS, **json.loads(r["data"])})
                for r in self.conn.execute("SELECT * FROM unlock_settings")]

    def unlock_sent_keys(self, guild_id) -> set:
        return {r["key"] for r in self.conn.execute("SELECT key FROM unlock_sent WHERE guild_id=?", (str(guild_id),))}

    def mark_unlock_sent(self, guild_id, key: str) -> None:
        self._exec("INSERT OR IGNORE INTO unlock_sent VALUES (?,?,?)", (str(guild_id), key, int(time.time())))

    # ---------- activity log ----------
    def log(self, text: str, guild_id=None) -> None:
        self._exec("INSERT INTO log (at, guild_id, text) VALUES (?,?,?)",
                   (int(time.time()), str(guild_id) if guild_id else None, text))
        self._exec("DELETE FROM log WHERE id NOT IN (SELECT id FROM log ORDER BY id DESC LIMIT 1000)")

    def recent_log(self, limit: int = 50, guild_id=None):
        if guild_id is None:
            return self.conn.execute("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return self.conn.execute("SELECT * FROM log WHERE guild_id=? OR guild_id IS NULL "
                                 "ORDER BY id DESC LIMIT ?", (str(guild_id), limit)).fetchall()
