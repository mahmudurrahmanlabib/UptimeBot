"""Central configuration, loaded from environment variables (.env supported)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _get(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


def _csv_set(name):
    raw = _get(name, default="") or ""
    return {x for x in (p.strip() for p in raw.split(",")) if x}


# --- Slack ---
SLACK_BOT_TOKEN = _get("SLACK_BOT_TOKEN", required=True)   # xoxb-...
SLACK_APP_TOKEN = _get("SLACK_APP_TOKEN", required=True)   # xapp-... (Socket Mode)

# --- Uptime Kuma ---
KUMA_URL = _get("KUMA_URL", required=True)
KUMA_USERNAME = _get("KUMA_USERNAME", required=True)
KUMA_PASSWORD = _get("KUMA_PASSWORD", required=True)
# Optional. If the Kuma account uses 2FA, store the TOTP *secret* here (not the
# 6-digit code). The bot generates the current code at login time.
KUMA_MFA_SECRET = _get("KUMA_MFA_SECRET", default="") or ""

# --- Storage ---
DB_PATH = _get("DB_PATH", default="uptimebot.db")

# --- Behaviour ---
CLIENT_TTL_DAYS = int(_get("CLIENT_TTL_DAYS", default="7"))
MONITOR_INTERVAL = int(_get("MONITOR_INTERVAL", default="60"))     # seconds
MONITOR_RETRIES = int(_get("MONITOR_RETRIES", default="2"))
STATUS_POLL_SECONDS = int(_get("STATUS_POLL_SECONDS", default="120"))
EXPIRY_POLL_SECONDS = int(_get("EXPIRY_POLL_SECONDS", default="3600"))
EXPIRY_WARN_HOURS = int(_get("EXPIRY_WARN_HOURS", default="24"))

# --- Optional access control (comma separated Slack IDs). Empty = allow all. ---
ALLOWED_USER_IDS = _csv_set("ALLOWED_USER_IDS")
ALLOWED_CHANNEL_IDS = _csv_set("ALLOWED_CHANNEL_IDS")

# --- Site type rules ---
# "permanent" is the generic kept-forever bucket and the default when no type
# is given; prod/staging/demo are optional finer labels that also never expire.
PERMANENT_TYPES = {"permanent", "prod", "staging", "demo"}
EXPIRING_TYPES = {"client"}
VALID_TYPES = PERMANENT_TYPES | EXPIRING_TYPES
DEFAULT_TYPE = "permanent"
