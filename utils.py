"""Small, dependency-free helpers shared across the bot."""
import re
from urllib.parse import urlparse

# Map a bunch of human spellings to our canonical site types.
TYPE_SYNONYMS = {
    "permanent": "permanent", "perm": "permanent", "keep": "permanent",
    "forever": "permanent", "fixed": "permanent", "internal": "permanent",
    "prod": "prod", "production": "prod", "live": "prod",
    "staging": "staging", "stage": "staging", "stg": "staging",
    "demo": "demo", "demos": "demo",
    "client": "client", "clients": "client", "customer": "client",
    "temp": "client", "temporary": "client",
}

STATUS_EMOJI = {"up": "🟢", "down": "🔴", "pending": "🟡", "maintenance": "🔧"}


def normalize_type(s):
    """Return canonical type or None if not a recognised type word."""
    if not s:
        return None
    return TYPE_SYNONYMS.get(s.strip().lower())


def status_emoji(status):
    return STATUS_EMOJI.get(status, "⚪")


def clean_slack_url(u):
    """Slack wraps links as <https://x.com> or <https://x.com|label>. Unwrap them."""
    u = (u or "").strip()
    if u.startswith("<") and u.endswith(">"):
        u = u[1:-1]
    if "|" in u:
        u = u.split("|", 1)[0]
    return u.strip()


def normalize_url(u):
    """Clean Slack formatting and ensure a scheme is present."""
    u = clean_slack_url(u)
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def looks_like_url(u):
    return bool(re.match(r"^https?://[^\s/.]+\.[^\s]+", u or "", re.I))


def clean_identifier(s):
    """Normalise a user-typed site reference for matching: unwrap Slack links
    (<http://x|x>), drop the scheme and any trailing slash. Plain names pass
    through unchanged so `remove Acme App` still works."""
    s = clean_slack_url(s or "")
    s = re.sub(r"^https?://", "", s, flags=re.I)
    return s.rstrip("/").strip()


def derive_name(url):
    """A sensible default monitor name: the hostname."""
    try:
        host = urlparse(url).netloc or url
        return host or url
    except Exception:
        return url


def humanize(seconds):
    """Turn a number of seconds into '6d 3h' / '2h 10m' / '<1m'."""
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days and minutes:
        parts.append(f"{minutes}m")
    if not parts:
        return "<1m"
    return " ".join(parts)
