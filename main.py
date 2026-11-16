"""Starts everything in one process: Discord bot, website, gift-code scheduler, reminders."""
import asyncio
import logging
import sys

from bot import config
from bot.db import DB
from bot.ks_api import KingshotAPI
from bot.redeemer import Redeemer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def main():
    if not config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN is not set in .env - the bot and website need it.")
        sys.exit(1)
    if not (config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET):
        log.warning("DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET not set - website login won't work.")
    if not config.KS_SALT:
        log.warning("KS_SALT is empty - gift codes will fail until you set it.")

    from bot.discord_bot import KingshotBot
    from bot.web import start_web

    db = DB(config.DB_PATH)
    api = KingshotAPI(config.KS_API_BASE, config.KS_SALT)
    redeemer = Redeemer(db, api)
    bot = KingshotBot(redeemer)

    runner = await start_web(redeemer, bot)
    log.info("Website on port %s, public address %s", config.WEB_PORT, config.PUBLIC_URL)
    scheduler = asyncio.create_task(redeemer.scheduler())
    try:
        await bot.start(config.DISCORD_TOKEN)
    finally:
        scheduler.cancel()
        await api.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
