"""Small, dependency-free helpers shared across the bot."""
import ipaddress
import re
import time
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

_SCHEME = re.compile(r"^https?://", re.I)
# Slack link markup: <https://x.com> or <https://x.com|label>.
_SLACK_LINK = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")
_SLACK_HTTP_LINK = re.compile(r"<https?://[^<>|\s]+(?:\|[^<>]*)?>", re.I)
# A dotted hostname whose last label is a real-looking TLD (letters or punycode).
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$", re.I)


def normalize_type(s):
    """Return canonical type or None if not a recognised type word."""
    if not s:
        return None
    return TYPE_SYNONYMS.get(s.strip().lower())


def status_emoji(status):
    return STATUS_EMOJI.get(status, "⚪")


def slack_unescape(s):
    """Undo Slack's escaping of &, < and > in message text."""
    return (s or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def slack_escape(s):
    """Make arbitrary text safe to drop into Slack mrkdwn."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean_slack_url(u):
    """Turn whatever the user typed into a bare URL string.

    Unwraps Slack link markup (<https://x.com> or <https://x.com|label>), even
    with punctuation stuck to it. Slack auto-links a bare `example.com` as
    <http://example.com|example.com>; in that case return what the user typed,
    so normalize_url() defaults it to https instead of plain http.
    """
    u = (u or "").strip()
    m = _SLACK_LINK.search(u)
    if m:
        target, label = m.group(1).strip(), (m.group(2) or "").strip()
        typed = _SCHEME.sub("", target).rstrip("/").lower() == label.rstrip("/").lower()
        if label and not _SCHEME.match(label) and typed:
            target = label
        return slack_unescape(target)
    if "|" in u:
        u = u.split("|", 1)[0]
    return slack_unescape(u).strip().strip("\"'").rstrip(".,;:!?")


def normalize_url(u):
    """Clean Slack formatting and ensure a scheme is present (https by default)."""
    u = clean_slack_url(u)
    if u and not _SCHEME.match(u):
        u = "https://" + u
    return u


def looks_like_url(u):
    """True for an http(s) URL whose host is a real-looking domain or an IP.

    Strict on purpose, so words like `v1.2`, `e.g.` or an email address in a
    message aren't mistaken for sites to monitor.
    """
    u = (u or "").strip()
    if not _SCHEME.match(u) or any(c.isspace() for c in u):
        return False
    try:
        parts = urlparse(u)
        host = parts.hostname or ""
        parts.port  # raises ValueError for a malformed port
    except ValueError:
        return False
    if not host or "@" in parts.netloc:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(host))


def tokenize(text):
    """Split a Slack message into words, keeping each link as one cleaned token
    (a labelled link like <https://x.com|our site> would otherwise split)."""
    text = _SLACK_HTTP_LINK.sub(lambda m: f" {clean_slack_url(m.group(0))} ", text or "")
    return text.split()


def extract_urls(text):
    """Every URL in `text`, normalised, in order, without repeats."""
    seen, urls = set(), []
    for tok in tokenize(text):
        url = normalize_url(tok)
        if looks_like_url(url) and canonical_url(url) not in seen:
            seen.add(canonical_url(url))
            urls.append(url)
    return urls


def canonical_url(u):
    """A normalised key for duplicate detection only (not for storage).

    Folds away the differences that mean "the same site": scheme, a leading
    `www.`, letter case, and a trailing slash. So `example.com`,
    `https://EXAMPLE.com/`, and `http://www.example.com` all collapse to
    `example.com`.
    """
    u = normalize_url(u).lower().rstrip("/")
    return re.sub(r"^https?://(www\.)?", "", u)


def clean_identifier(s):
    """Normalise a user-typed site reference for matching: unwrap Slack links
    (<http://x|x>), drop the scheme and any trailing slash. Plain names pass
    through unchanged so `remove Acme App` still works."""
    s = clean_slack_url(s or "")
    s = re.sub(r"^https?://", "", s, flags=re.I)
    return s.rstrip("/").strip()


def clean_name(s, limit=80):
    """A tidy one-line display name with no Slack markup characters."""
    s = slack_unescape(s or "")
    s = " ".join(re.sub(r"[<>`*]", "", s).split())
    return s[:limit].rstrip()


def derive_name(url):
    """A sensible default monitor name: the hostname."""
    try:
        host = urlparse(url).netloc or url
        return host or url
    except Exception:
        return url


def link(url, limit=60):
    """A compact Slack link that shows the address without its scheme."""
    url = (url or "").replace("|", "%7C").replace("<", "%3C").replace(">", "%3E")
    label = _SCHEME.sub("", url).rstrip("/")
    if len(label) > limit:
        label = label[:limit - 1] + "…"
    return f"<{url}|{slack_escape(label)}>"


def slack_date(ts, fmt="{date_short_pretty} at {time}"):
    """A timestamp Slack renders in each viewer's own timezone (UTC fallback)."""
    ts = int(ts)
    fallback = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
    return f"<!date^{ts}^{fmt}|{fallback}>"


def strip_bot_mention(text, bot_user_id=None):
    """Remove @UptimeBot from a message wherever it appears ("hey @UptimeBot
    list" works too). Without the bot's id, only a leading mention is removed."""
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", " ", text or "")
    else:
        text = re.sub(r"^\s*<@[^>]+>", " ", text or "")
    return " ".join(text.split())


_OFF_WORDS = {"off", "none", "nobody", "no", "quiet", "silent", "clear"}
_MENTION_TOKEN = re.compile(r"^<(@[UW][A-Z0-9]+|!subteam\^[A-Z0-9]+|!here|!channel)(?:\|[^>]*)?>$")


def parse_mentions(tokens):
    """Who a channel's DOWN alerts should ping, as Slack markup.

    Accepts @here, @channel, people and user groups (as Slack sends them when
    picked from autocomplete), or `off`. Returns "" for off, or None if any
    token isn't something we can ping.
    """
    if len(tokens) == 1 and tokens[0].lower() in _OFF_WORDS:
        return ""
    found = []
    for tok in tokens:
        low = tok.lower()
        if low in ("@here", "here"):
            mention = "<!here>"
        elif low in ("@channel", "channel"):
            mention = "<!channel>"
        else:
            m = _MENTION_TOKEN.match(tok)
            if not m:
                return None
            mention = f"<{m.group(1)}>"
        if mention not in found:
            found.append(mention)
    return " ".join(found) or None


def describe_mentions(mention):
    """Show a stored mention without pinging anyone (@here stays plain text)."""
    if not mention:
        return "nobody"
    return mention.replace("<!here>", "`@here`").replace("<!channel>", "`@channel`")


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
