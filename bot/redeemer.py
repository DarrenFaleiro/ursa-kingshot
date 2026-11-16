"""The redemption engine + background scheduler. Shared by the website and Discord."""
import asyncio
import logging
import time
from typing import Awaitable, Callable

from . import config
from .codes_feed import fetch_codes
from .db import DB

log = logging.getLogger("redeemer")

# When a code returns one of these, stop trying it for everyone.
CODE_DEAD = {"expired": "expired", "invalid": "invalid", "limit": "limit"}


class Redeemer:
    def __init__(self, db: DB, api, delay: float = config.REQUEST_DELAY,
                 max_errors: int = config.MAX_CONSECUTIVE_ERRORS):
        self.db = db
        self.api = api
        self.delay = delay
        self.max_errors = max_errors
        self.lock = asyncio.Lock()
        self.running = False
        self.last_summary: str = "No run yet."
        self.next_run_at: float | None = None
        self.salt_status: str = "not checked yet"
        # callbacks the Discord bot plugs into
        self.on_new_codes: list[Callable[[list[str]], Awaitable[None]]] = []
        self.on_run_finished: list[Callable[[int, str], Awaitable[None]]] = []
        self.on_alert: list[Callable[[str], Awaitable[None]]] = []

    async def _call(self, callbacks, *args):
        for fn in callbacks:
            try:
                await fn(*args)
            except Exception:  # never let a callback crash a run
                log.exception("callback failed")

    # ---------- helpers ----------
    async def check_salt(self) -> bool:
        r = await self.api.check_salt()
        if r.status == "ok":
            self.salt_status = "ok"
        elif r.status == "sign_error":
            self.salt_status = "WRONG - update KS_SALT in .env"
        else:
            self.salt_status = f"couldn't check ({r.message})"
        self.db.log(f"Salt check: {self.salt_status}")
        return r.status == "ok"

    async def add_player(self, fid, kid, added_by: str, nickname=None,
                         guild_id=None, discord_user_id=None):
        """Save a player. Returns (ok, message)."""
        fid, kid = str(fid).strip(), str(kid).strip()
        if not fid.isdigit():
            return False, f"`{fid}`: player ID must be numbers only."
        if not kid.isdigit():
            return False, f"`{fid}`: kingdom must be a number (e.g. 123)."
        if self.db.get_player(fid):
            return False, f"`{fid}` is already registered."
        nickname = (nickname or "").strip() or None
        self.db.add_player(fid, nickname, int(kid), added_by, guild_id, discord_user_id)
        self.db.log(f"Player added: {nickname or fid} ({fid}, kingdom {kid}) by {added_by}", guild_id)
        return True, f"Added {('**' + nickname + '** ') if nickname else ''}(ID `{fid}`, kingdom {kid})."

    def add_code(self, code: str, source: str) -> bool:
        code = code.strip()
        if not code:
            return False
        if self.db.add_code(code, source):
            self.db.log(f"New code {code} (from {source})")
            return True
        return False

    # ---------- the main loop ----------
    async def run(self, codes: list[str] | None = None, fids: list[str] | None = None) -> str:
        """Redeem `codes` (default: all active) for `fids` (default: every enabled player)."""
        if self.lock.locked():
            return "A redemption run is already in progress."
        async with self.lock:
            self.running = True
            started = int(time.time())
            try:
                summary = await self._run(codes, fids)
            finally:
                self.running = False
            self.last_summary = summary
            self.db.log(summary.replace("\n", " | "))
        await self._call(self.on_run_finished, started, summary)
        return summary

    async def _run(self, codes, fids) -> str:
        if codes is None:
            codes = [r["code"] for r in self.db.list_codes(only_active=True)]
        players = [p for p in self.db.list_players(only_enabled=True)
                   if fids is None or p["fid"] in fids]
        if not codes or not players:
            return "Nothing to do (no active codes or no enabled players)."

        lines = []
        errors_in_a_row = 0

        for code in codes:
            counts: dict[str, int] = {}
            for p in players:
                fid = p["fid"]
                if self.db.is_done(fid, code):
                    continue

                r = await self.api.redeem(fid, p["kid"], code)
                for _ in range(3):   # "too frequent": wait a minute and try again
                    if r.status != "rate_limited":
                        break
                    await asyncio.sleep(60)
                    r = await self.api.redeem(fid, p["kid"], code)

                counts[r.status] = counts.get(r.status, 0) + 1
                self.db.record(fid, code, r.status, r.message)

                if r.status in ("success", "already"):
                    errors_in_a_row = 0
                elif r.status in ("wrong_kingdom", "no_player"):
                    self.db.set_player_enabled(fid, False)
                    self.db.log(f"Player {fid} paused: {r.status} (check ID / kingdom)", p["guild_id"])
                    errors_in_a_row = 0
                elif r.status in CODE_DEAD:
                    self.db.set_code_status(code, CODE_DEAD[r.status])
                    break
                elif r.status == "sign_error":
                    self.salt_status = "WRONG - update KS_SALT in .env"
                    return "Stopped: the signing salt (KS_SALT) is wrong or missing."
                elif r.status == "captcha":
                    return "Stopped: the website is asking for a captcha, which can't be automated."
                else:
                    errors_in_a_row += 1
                    if errors_in_a_row >= self.max_errors:
                        lines.append(f"`{code}`: {self._fmt(counts)}")
                        lines.append(f"Stopped early after {errors_in_a_row} errors in a row.")
                        return "\n".join(lines)

                await asyncio.sleep(self.delay)

            if counts:
                lines.append(f"`{code}`: {self._fmt(counts)}")

        return "\n".join(lines) if lines else "Everyone already has every active code."

    @staticmethod
    def _fmt(counts: dict) -> str:
        return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))

    # ---------- scheduler ----------
    async def check_feed(self) -> list[str]:
        try:
            codes = await fetch_codes(config.CODES_FEED_URL)
        except Exception as e:
            self.db.log(f"Code feed error: {e}")
            return []
        return [c for c in codes if self.add_code(c, "feed")]

    async def scheduler(self):
        """Every CHECK_INTERVAL_MIN: check the salt, look for new codes, redeem what's outstanding."""
        while True:
            try:
                if await self.check_salt() or not self.salt_status.startswith("WRONG"):
                    new = await self.check_feed()
                    if new:
                        await self._call(self.on_new_codes, new)
                    if config.AUTO_REDEEM:
                        await self.run()
                else:
                    await self._call(self.on_alert,
                                     "⚠️ The signing salt (KS_SALT) is wrong - gift codes are paused.")
            except Exception:
                log.exception("scheduler cycle failed")
            self.next_run_at = time.time() + config.CHECK_INTERVAL_MIN * 60
            await asyncio.sleep(config.CHECK_INTERVAL_MIN * 60)
