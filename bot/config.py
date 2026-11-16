"""All settings come from environment variables (or a .env file)."""
import os
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so we don't need python-dotenv."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}


# --- Branding ---
BOT_NAME = os.getenv("BOT_NAME", "Ursa-kingshot")

# --- Discord bot ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_IDS = _ids("OWNER_IDS")                 # you: full control of every server + global codes
ALLOWED_GUILD_IDS = _ids("ALLOWED_GUILD_IDS")  # if set, the bot leaves any other server
ALLOW_PUBLIC_ADD = os.getenv("ALLOW_PUBLIC_ADD", "true").lower() == "true"

# --- Discord login for the website (OAuth2) ---
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080").rstrip("/")

# --- Kingshot gift-code API ---
KS_API_BASE = os.getenv("KS_API_BASE", "https://kingshot-giftcode.centurygame.com/api")
KS_SALT = os.getenv("KS_SALT", "")
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "3"))
MAX_CONSECUTIVE_ERRORS = _int("MAX_CONSECUTIVE_ERRORS", 5)

# --- Code feed ---
CODES_FEED_URL = os.getenv("CODES_FEED_URL", "https://kingshot.net/api/gift-codes")
CHECK_INTERVAL_MIN = _int("CHECK_INTERVAL_MIN", 60)
AUTO_REDEEM = os.getenv("AUTO_REDEEM", "true").lower() == "true"

# --- Web server ---
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = _int("WEB_PORT", 8080)

# --- Storage ---
DB_PATH = os.getenv("DB_PATH", "data/kingshot.db")
