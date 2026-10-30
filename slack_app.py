"""Slack interface: command parsing, Block Kit responses, the add form, and handlers.

Ways in
-------
* /monitor ...      -> works in any channel or DM, even before the bot is invited
* @UptimeBot ...    -> in channels the bot is in; answers in the thread if asked in one
* DM the bot        -> any message is a command, or just paste links
* Shortcuts         -> "Monitor a website" (global) and "Monitor this link" (message)
* Invite the bot    -> it posts a short welcome with an "Add a site" button
* Home tab          -> a personal dashboard of the sites you can see

Where alerts go
---------------
Every site has an owner: a channel, or a person (alerts arrive in their DM with
the bot). Adding a site first checks the bot can really post there: it joins
public channels by itself, asks to be invited to private ones, and makes sites
added in DMs with other people or in group DMs personal. If it later loses
access to a channel, alerts fall back to whoever added the site instead of
vanishing. The scheduler posts every notice through `post_to_owner`.
"""
import json
import logging
import time
from collections import namedtuple

from slack_sdk.errors import SlackApiError

import config
import database as db
import utils
from kuma_client import KumaClient, KumaError

log = logging.getLogger("uptimebot.slack")

_name_cache = {}

# Short-lived channel-membership cache so opening/refreshing Home doesn't call
# conversations.members on every open. Keyed by (channel_id, user_id).
_MEMBER_TTL = 300  # seconds
_member_cache = {}

# Slack caps a published view at 100 blocks and a message at 50; stay under.
_MAX_HOME_BLOCKS = 95
_MAX_MESSAGE_BLOCKS = 45
# ...and a section's text at 3000 characters.
_MAX_SECTION_CHARS = 2800

# Adding more than this in one go makes for a slow reply and a wall of text.
_MAX_BULK_ADD = 20


# --------------------------------------------------------------------------- #
# Block Kit helpers
# --------------------------------------------------------------------------- #
def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def header(text):
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def context_block(text):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def simple(text):
    return {"text": text, "blocks": [section(text)]}


def button(text, action_id, value=None, style=None):
    b = {"type": "button", "action_id": action_id,
         "text": {"type": "plain_text", "text": text, "emoji": True}}
    if value is not None:
        b["value"] = str(value)
    if style:
        b["style"] = style
    return b


def actions(*elements):
    return {"type": "actions", "elements": list(elements)}


def chunked_sections(lines, head=None):
    """Section blocks holding `lines` (under a bold `head`), split so that each
    stays within Slack's per-section length limit."""
    blocks, chunk, size = [], [], 0
    if head:
        chunk, size = [head], len(head)
    for line in lines:
        if chunk and (len(chunk) >= 20 or size + len(line) + 1 > _MAX_SECTION_CHARS):
            blocks.append(section("\n".join(chunk)))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        blocks.append(section("\n".join(chunk)))
    return blocks


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #
def display_name(client, user_id):
    if not user_id:
        return "someone"
    if user_id in _name_cache:
        return _name_cache[user_id]
    name = f"<@{user_id}>"
    try:
        prof = client.users_info(user=user_id)["user"]["profile"]
        name = prof.get("display_name") or prof.get("real_name") or name
    except Exception:
        pass
    _name_cache[user_id] = name
    return name


def allowed(user_id, channel_id, is_dm):
    if config.ALLOWED_USER_IDS and user_id not in config.ALLOWED_USER_IDS:
        return False
    if not is_dm and config.ALLOWED_CHANNEL_IDS and channel_id not in config.ALLOWED_CHANNEL_IDS:
        return False
    return True


def can_manage(site, user_id, channel_id):
    """Whoever added a site can manage it from anywhere; a channel's site can
    also be managed by anyone acting from inside that channel."""
    return (site.get("added_by") == user_id
            or (not site.get("is_dm") and site.get("channel_id") == channel_id))


def is_personal_conversation(channel_id, channel_name=""):
    """DMs and group DMs: places the bot can't post in, so sites added there
    belong to the person and alert them in their DM with the bot."""
    channel_name = channel_name or ""
    return ((channel_id or "").startswith("D") or channel_name == "directmessage"
            or channel_name.startswith("mpdm-"))


def make_ctx(client, user_id, channel_id, is_dm, is_group_dm=False):
    return {
        "client": client,
        "user_id": user_id,
        "channel_id": channel_id,
        "is_dm": is_dm,
        "is_group_dm": is_group_dm,
        "user_name": display_name(client, user_id),
    }


def _personal(ctx):
    return bool(ctx.get("is_dm") or ctx.get("is_group_dm"))


def _scope(ctx):
    """Limit lists and lookups to what the caller can see: this channel's sites
    when used in a channel, or the caller's own sites when used in a DM."""
    if _personal(ctx):
        return {"added_by": ctx["user_id"]}
    return {"channel_id": ctx["channel_id"]}


def _dm(client, user_id, text):
    try:
        client.chat_postMessage(channel=user_id, text=text)
    except Exception:
        log.exception("couldn't DM %s", user_id)


# Display order + emoji/label for each site type (used by `list` and the Home tab).
TYPE_ORDER = ("permanent", "prod", "staging", "demo", "client")
TYPE_META = {
    "permanent": ("♾️", "Permanent"),
    "prod": ("🚀", "Prod"),
    "staging": ("🧪", "Staging"),
    "demo": ("🎬", "Demo"),
    "client": ("👤", "Client"),
}
STATUS_LEGEND = "🟢 up · 🔴 down · 🟡 pending · 🔧 maintenance · ⚪ unknown"
_NO_ACCESS = "⛔ You don't have access to UptimeBot here. Ask an admin to add you."


def _site_line(s, show_owner=False):
    """One site as a list line: status, name, link, and anything time-bound."""
    line = f"{utils.status_emoji(s['last_status'])}  *{s['name']}*  —  {utils.link(s['url'])}"
    if s["last_status"] == "down" and s.get("down_since"):
        line += f"  · down {utils.humanize(time.time() - s['down_since'])}"
    if s["expires_at"]:
        line += f"  · _expires in {utils.humanize(s['expires_at'] - time.time())}_"
    if show_owner:
        line += "  · DM" if s["is_dm"] else f"  · <#{s['channel_id']}>"
    return line


def help_response(ctx=None):
    in_dm = _personal(ctx or {})
    where = "to you here" if in_dm else "in this channel"
    intro = (f"I watch websites and post {where} the moment one goes *down* — "
             "and again when it's *back up*.")
    add = ("*Add a site*\n"
           "• `/monitor example.com` — kept until you remove it\n"
           f"• `/monitor client example.com` — auto-removed after {config.CLIENT_TTL_DAYS} days\n"
           "• `/monitor a.com b.com c.com` — several at once\n"
           "• `/monitor` — open the form and pick any channel for the alerts")
    manage = ("*Manage*\n"
              f"• `/monitor list` — everything monitored {'for you' if in_dm else 'here'}\n"
              "• `/monitor status <name>` — is it up right now?\n"
              "• `/monitor remove <name>` — stop monitoring it\n"
              "• `/monitor extend <name> [days]` · `/monitor set <name> <type>` — adjust it")
    if not in_dm:
        manage += "\n• `/monitor notify @here` — who to ping when a site here goes down"
    if in_dm:
        tip = "In our DM you can skip `/monitor` — type `list`, or just paste a link."
    else:
        tip = ("`/monitor` works in any channel, even before you invite me. "
               "Private channel? `/invite @UptimeBot` first.")
    return {
        "text": "UptimeBot help",
        "blocks": [
            header("🤖  UptimeBot"),
            section(intro),
            {"type": "divider"},
            section(add),
            section(manage),
            context_block(f"{tip}   ·   Types: `permanent` (default), `prod`, `staging`, "
                          f"`demo`, `client` (temporary)   ·   My *Home* tab shows every site you can see."),
        ],
    }


def welcome_message(channel_id):
    """What the bot says when it's invited to a channel."""
    if config.ALLOWED_CHANNEL_IDS and channel_id not in config.ALLOWED_CHANNEL_IDS:
        return simple("👋 Thanks for the invite! I'm not enabled in this channel yet — an admin "
                      "can add it to `ALLOWED_CHANNEL_IDS`.")
    count = len(db.list_sites(channel_id=channel_id))
    start = ("*Watch a site from this channel:*  `/monitor example.com`   ·   "
             "several at once:  `/monitor a.com b.com`")
    if count:
        start = (f"I'm already watching *{_plural(count, 'site')}* here — `/monitor list` "
                 f"shows them.\n{start}")
    return {
        "text": "Hi, I'm UptimeBot! Type /monitor example.com to watch a site from this channel.",
        "blocks": [
            section("👋 *Hi, I'm UptimeBot.* I watch websites and post here the moment one goes "
                    "*down* — and again when it's *back up*."),
            section(start),
            actions(button("➕  Add a site", "channel_add", channel_id, "primary"),
                    button("How it works", "show_help")),
            context_block("Ping people on outages: `/monitor notify @here`   ·   Temporary client "
                          f"sites expire after {config.CLIENT_TTL_DAYS} days: "
                          "`/monitor client demo.example.com`"),
        ],
    }


# --------------------------------------------------------------------------- #
# Where alerts go
# --------------------------------------------------------------------------- #
Dest = namedtuple("Dest", "channel_id is_dm note")


class Undeliverable(Exception):
    """The bot can't post in the conversation a site's alerts were meant for."""


class SaveError(Exception):
    """A Kuma monitor was created but couldn't be recorded (it was rolled back)."""


# Channels the bot just joined by itself while adding a site there (channel id
# -> when). The add confirmation already says what's going on, so skip the
# welcome message that the join triggers.
_quiet_joins = {}
_QUIET_JOIN_TTL = 120  # seconds

_NOT_IN_CHANNEL = ("I'm not in that channel, so I can't post its alerts there. "
                   "Invite me with /invite @UptimeBot, then try again.")

# chat.postMessage errors that mean "the bot can't post in that conversation".
_CANT_POST = {
    "not_in_channel", "channel_not_found", "is_archived", "team_access_not_granted",
    "restricted_action", "restricted_action_read_only_channel",
    "restricted_action_thread_only_channel", "restricted_action_non_threadable_channel",
}


def check_channel(client, channel_id):
    """Make sure the bot can post in `channel_id`, joining it if it's public.
    Raises Undeliverable, with how to fix it, when it can't."""
    try:
        info = client.conversations_info(channel=channel_id)["channel"]
    except SlackApiError as exc:
        if exc.response.get("error") in ("channel_not_found", "missing_scope"):
            raise Undeliverable(_NOT_IN_CHANNEL) from None
        log.warning("couldn't check channel %s: %s", channel_id, exc)
        return  # a Slack hiccup shouldn't block the add; alerts fall back to a DM
    except Exception:
        log.warning("couldn't check channel %s", channel_id, exc_info=True)
        return
    if info.get("is_archived"):
        raise Undeliverable("That channel is archived, so nobody would see its alerts.")
    if info.get("is_member") or info.get("is_im"):
        return
    if info.get("is_private") or info.get("is_mpim"):
        raise Undeliverable(_NOT_IN_CHANNEL)
    _quiet_joins[channel_id] = time.time()
    try:
        client.conversations_join(channel=channel_id)
    except Exception:
        # Not fatal: chat:write.public still lets the bot post in public channels.
        _quiet_joins.pop(channel_id, None)
        log.info("couldn't join %s; will post there without joining", channel_id, exc_info=True)


def _destination(ctx):
    """Where alerts for a site added in this conversation should go."""
    if ctx.get("dest"):
        return ctx["dest"]  # chosen explicitly in the add form
    if ctx["is_dm"] or not ctx["channel_id"]:
        return Dest(ctx["user_id"], True, None)
    if ctx.get("is_group_dm"):
        return Dest(ctx["user_id"], True,
                    "I can't post in group DMs, so this site's alerts will come to you directly.")
    check_channel(ctx["client"], ctx["channel_id"])
    return Dest(ctx["channel_id"], False, None)


def _where(dest):
    return "alerts come to you in a DM" if dest.is_dm else f"alerts post in <#{dest.channel_id}>"


def post_to_owner(client, site, text, blocks=None):
    """Post a notice about `site` where its alerts belong. If the bot can no
    longer post in the owning channel (removed from it, archived, …), DM whoever
    added the site rather than lose the message. Returns (channel, ts) or None."""
    try:
        resp = client.chat_postMessage(channel=site["channel_id"], text=text, blocks=blocks)
        return resp["channel"], resp["ts"]
    except SlackApiError as exc:
        error = exc.response.get("error")
        if site.get("is_dm") or not site.get("added_by") or error not in _CANT_POST:
            log.error("couldn't post to %s about %s: %s", site["channel_id"], site["name"], error)
            return None
    except Exception:
        log.exception("couldn't post to %s about %s", site["channel_id"], site["name"])
        return None
    note = context_block(f":information_source: I couldn't post this in <#{site['channel_id']}> "
                         f"(`{error}`), so it came to you instead. Invite me back with "
                         f"`/invite @UptimeBot` to get alerts there again.")
    try:
        resp = client.chat_postMessage(channel=site["added_by"], text=text,
                                       blocks=(blocks or [section(text)]) + [note])
        return resp["channel"], resp["ts"]
    except Exception:
        log.exception("couldn't DM %s about %s either", site["added_by"], site["name"])
        return None


# --------------------------------------------------------------------------- #
# Command dispatch
# --------------------------------------------------------------------------- #
_HELP_WORDS = {"help", "?", "start", "commands"}
_GREETINGS = {"hi", "hello", "hey", "yo", "please", "pls"}
_ADD_WORDS = {"monitor", "mon", "watch", "track", "add"}
_LIST_WORDS = {"list", "ls", "sites"}
_STATUS_WORDS = {"status", "check"}
_REMOVE_WORDS = {"remove", "rm", "delete", "del", "unwatch"}
_EXTEND_WORDS = {"extend", "renew"}
_SET_WORDS = {"set", "reclassify", "type", "promote"}
_NOTIFY_WORDS = {"notify", "mention", "mentions"}
# First words that mean "run a command"; anything else after /monitor is a site.
MANAGE_WORDS = (_HELP_WORDS | _GREETINGS | _LIST_WORDS | _STATUS_WORDS | _REMOVE_WORDS
                | _EXTEND_WORDS | _SET_WORDS | _NOTIFY_WORDS)


def handle_command(text, ctx):
    """Run a text command. The reply may carry two hints for the caller:
    `_share` (worth showing the whole channel) and `_private` (only for the
    person who asked, e.g. a confirmation prompt)."""
    parts = utils.tokenize(text)
    if not parts:
        return help_response(ctx)
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in _GREETINGS and args:
        return handle_command(" ".join(args), ctx)  # "hey list" -> "list"
    if cmd in _HELP_WORDS or cmd in _GREETINGS:
        return help_response(ctx)
    if cmd in _ADD_WORDS:
        return cmd_add(args, ctx)
    if cmd in _LIST_WORDS:
        return cmd_list(args, ctx)
    if cmd in _STATUS_WORDS:
        return cmd_status(args, ctx)
    if cmd in _REMOVE_WORDS:
        return cmd_remove(args, ctx)
    if cmd in _EXTEND_WORDS:
        return cmd_extend(args, ctx)
    if cmd in _SET_WORDS:
        return cmd_set(args, ctx)
    if cmd in _NOTIFY_WORDS:
        return cmd_notify(args, ctx)
    # No command verb — if the message contains a URL, treat it as "add this site".
    if any(utils.looks_like_url(utils.normalize_url(p)) for p in parts):
        return cmd_add(parts, ctx)
    return simple("I didn't recognise that. Type *help*, or just send me a link "
                  "like `example.com` and I'll start monitoring it.")


def _ambiguous(matches, identifier):
    lines = [f"• *{m['name']}* — {utils.link(m['url'])} ({m['site_type']})" for m in matches[:10]]
    extra = "" if len(matches) <= 10 else f"\n…and {len(matches) - 10} more."
    return simple(
        f"Multiple sites match “{utils.slack_escape(identifier)}”. Please be more specific:\n"
        + "\n".join(lines) + extra
    )


def _find_one(args, ctx):
    """Resolve a site reference to exactly one visible site, or an error reply."""
    identifier = utils.clean_identifier(" ".join(args))
    matches = db.find_sites(identifier, **_scope(ctx))
    if not matches:
        return None, simple(f"I couldn't find a site matching “{utils.slack_escape(identifier)}”.")
    if len(matches) > 1:
        return None, _ambiguous(matches, identifier)
    return matches[0], None


def cmd_add(args, ctx):
    if not args:
        return simple(
            "To add a site: `/monitor example.com` (kept until removed) or "
            f"`/monitor client example.com` (auto-removed after {config.CLIENT_TTL_DAYS} days).\n"
            "_No https:// needed. You can also paste a link to me in a DM._"
        )

    url_idx = [i for i, tok in enumerate(args) if utils.looks_like_url(utils.normalize_url(tok))]
    if not url_idx:
        return simple(
            f"“{utils.slack_escape(' '.join(args))}” doesn't include a URL I recognise. "
            "Try `/monitor example.com`."
        )

    if len(url_idx) > 1:
        # Several sites at once: names come from the domains, one shared type.
        site_type = next((utils.normalize_type(a) for i, a in enumerate(args)
                          if i not in url_idx and utils.normalize_type(a)), None)
        urls = []
        for i in url_idx:
            url = utils.normalize_url(args[i])
            if all(utils.canonical_url(url) != utils.canonical_url(u) for u in urls):
                urls.append(url)
        site_type = site_type or config.DEFAULT_TYPE
        if len(urls) > 1:
            return _do_add_many(urls, site_type, ctx)
        return _do_add(urls[0], utils.derive_name(urls[0]), site_type, ctx)

    url_idx = url_idx[0]
    url = utils.normalize_url(args[url_idx])

    # A type word may sit right before the URL, right after it, or as "as <type>".
    consumed = {url_idx}
    site_type = None
    before = args[url_idx - 1] if url_idx - 1 >= 0 else None
    after = args[url_idx + 1] if url_idx + 1 < len(args) else None
    if before is not None and utils.normalize_type(before):
        site_type = utils.normalize_type(before)
        consumed.add(url_idx - 1)
    elif after == "as" and url_idx + 2 < len(args) and utils.normalize_type(args[url_idx + 2]):
        site_type = utils.normalize_type(args[url_idx + 2])
        consumed.update({url_idx + 1, url_idx + 2})
    elif after is not None and utils.normalize_type(after):
        site_type = utils.normalize_type(after)
        consumed.add(url_idx + 1)

    if site_type is None:
        site_type = config.DEFAULT_TYPE

    name = utils.clean_name(" ".join(args[i] for i in range(len(args)) if i not in consumed))
    name = name or utils.derive_name(url)
    return _do_add(url, name, site_type, ctx)


def _already_monitoring(s):
    where = "for you" if s["is_dm"] else f"in <#{s['channel_id']}>"
    line = (f":eyes: Already monitoring *{s['name']}* {where} — {utils.link(s['url'])}  ·  "
            f"{utils.status_emoji(s['last_status'])} {s['last_status']}.\n"
            f"Manage it with `/monitor status {s['name']}`, `/monitor extend {s['name']}`, "
            f"or `/monitor remove {s['name']}`.")
    if s.get("added_by_name"):
        line += f"\n_Added by {s['added_by_name']}._"
    return simple(line)


def _find_dup(url, dest):
    if dest.is_dm:
        return db.find_duplicate(url, added_by=dest.channel_id)
    return db.find_duplicate(url, dest.channel_id)


def _create_site(kc, url, name, site_type, ctx, dest):
    """Create the Kuma monitor and record it; returns the expiry (or None).
    The DB is the source of truth, so a monitor we can't record is deleted
    again rather than left orphaned in Kuma."""
    expires_at = None
    if site_type in config.EXPIRING_TYPES:
        expires_at = time.time() + config.CLIENT_TTL_DAYS * 86400
    monitor_id = kc.add_monitor(f"{site_type.upper()} | {name}", url)
    try:
        db.add_site(
            monitor_id=monitor_id, name=name, url=url, site_type=site_type,
            added_by=ctx["user_id"], added_by_name=ctx["user_name"],
            channel_id=dest.channel_id, is_dm=dest.is_dm, expires_at=expires_at,
        )
    except Exception as exc:
        log.exception("DB insert failed after creating Kuma monitor %s; rolling back", monitor_id)
        try:
            kc.delete_monitor(monitor_id)
        except Exception:
            log.exception("couldn't roll back orphaned Kuma monitor %s", monitor_id)
        raise SaveError() from exc
    return expires_at


def _do_add(url, name, site_type, ctx):
    try:
        dest = _destination(ctx)
    except Undeliverable as exc:
        return simple(f":no_entry: {exc}")

    # Don't add the same site twice for the same owner (a channel, or a
    # person's DM). Different channels/people may still watch the same URL.
    dup = _find_dup(url, dest)
    if dup:
        return _already_monitoring(dup)

    try:
        with KumaClient() as kc:
            expires_at = _create_site(kc, url, name, site_type, ctx, dest)
    except KumaError as exc:
        return simple(f":warning: Couldn't add the monitor.\n```{exc}```")
    except SaveError:
        return simple(":warning: Couldn't save the site just now — please try again.")

    if expires_at:
        tail = (f":hourglass_flowing_sand: As a *client* site it'll be auto-removed in "
                f"*{utils.humanize(expires_at - time.time())}*. Keep it longer with "
                f"`/monitor extend {name}`, or for good with `/monitor set {name} permanent`.")
    else:
        tail = ":infinity: Kept *permanently* — stays until you remove it."
    lines = [f":white_check_mark: Now monitoring *{name}* — {utils.link(url)}",
             f"Type *{site_type}* · {_where(dest)}", tail]
    if dest.note:
        lines.append(f"_{dest.note}_")
    return {"text": f"Now monitoring {name}",
            "blocks": [section("\n".join(lines)), context_block(f"Added by {ctx['user_name']}")],
            "_share": True}


def _do_add_many(urls, site_type, ctx):
    try:
        dest = _destination(ctx)
    except Undeliverable as exc:
        return simple(f":no_entry: {exc}")

    skipped = urls[_MAX_BULK_ADD:]
    urls = urls[:_MAX_BULK_ADD]
    dupes = [u for u in urls if _find_dup(u, dest)]
    added, failed = [], []
    todo = [u for u in urls if u not in dupes]
    if todo:
        try:
            with KumaClient() as kc:  # one Kuma session for the whole batch
                for url in todo:
                    try:
                        _create_site(kc, url, utils.derive_name(url), site_type, ctx, dest)
                        added.append(url)
                    except (KumaError, SaveError) as exc:
                        log.warning("couldn't add %s: %s", url, exc)
                        failed.append(url)
        except KumaError as exc:
            return simple(f":warning: Couldn't reach Uptime Kuma, so nothing was added.\n```{exc}```")

    parts = []
    if added:
        parts.append(f":white_check_mark: Now monitoring *{_plural(len(added), 'site')}* as "
                     f"*{site_type}* · {_where(dest)}\n"
                     + "\n".join(f"• {utils.link(u)}" for u in added))
        if site_type in config.EXPIRING_TYPES:
            parts.append(f":hourglass_flowing_sand: As *client* sites they'll be auto-removed in "
                         f"*{config.CLIENT_TTL_DAYS}d* — `/monitor extend <name>` keeps one longer.")
        else:
            parts.append(":infinity: Kept *permanently* — they stay until you remove them.")
    if dupes:
        parts.append(":eyes: Already monitoring: " + ", ".join(utils.link(u) for u in dupes))
    if failed:
        parts.append(":warning: Couldn't add " + ", ".join(utils.link(u) for u in failed)
                     + " — please try those again in a moment.")
    if skipped:
        parts.append(f"_That's more than {_MAX_BULK_ADD} at once, so I skipped "
                     f"{_plural(len(skipped), 'site')} — send them in another message._")
    if dest.note and added:
        parts.append(f"_{dest.note}_")
    resp = simple("\n\n".join(parts))
    resp["_share"] = bool(added)
    return resp


def cmd_list(args, ctx):
    flt = utils.normalize_type(args[0]) if args else None
    sites = db.list_sites(flt, **_scope(ctx))
    personal = _personal(ctx)
    if not sites:
        where = "yet" if personal else "here"
        msg = f"No sites are being monitored {where}. Add one with `/monitor example.com`."
        if flt:
            scope = "for you" if personal else "in this channel"
            msg = f"No *{flt}* sites are being monitored {scope} yet."
        return simple(msg)

    blocks = [header("📊 Monitored sites")]
    for t in TYPE_ORDER:
        group = [s for s in sites if s["site_type"] == t]
        if not group:
            continue
        emoji, label = TYPE_META.get(t, ("•", t.title()))
        blocks += chunked_sections([_site_line(s, show_owner=personal) for s in group],
                                   head=f"{emoji} *{label}*  ({len(group)})")
    if len(blocks) > _MAX_MESSAGE_BLOCKS:
        blocks = blocks[:_MAX_MESSAGE_BLOCKS]
        blocks.append(context_block("…and more — too many to show in one message. "
                                    "My *Home* tab shows the rest."))
    footer = STATUS_LEGEND
    if not personal:
        mention = utils.describe_mentions(db.get_mention(ctx["channel_id"]))
        footer += f"   ·   Alerts post in this channel and ping {mention}"
    blocks.append(context_block(footer))
    return {"text": "Monitored sites", "blocks": blocks}


def cmd_status(args, ctx):
    if not args:
        sites = db.list_sites(**_scope(ctx))
        if not sites:
            return simple("Nothing is being monitored here yet. Add a site with "
                          "`/monitor example.com`.")
        counts = {}
        for s in sites:
            counts[s["last_status"]] = counts.get(s["last_status"], 0) + 1
        up, down = counts.get("up", 0), counts.get("down", 0)
        line = f"*{len(sites)}* sites monitored — 🟢 {up} up · 🔴 {down} down"
        others = ", ".join(f"{utils.status_emoji(k)} {v} {k}"
                           for k, v in counts.items() if k not in ("up", "down"))
        if others:
            line += f" · {others}"
        down_sites = [s for s in sites if s["last_status"] == "down"]
        if down_sites:
            names = ", ".join(
                f"*{s['name']}*" + (f" ({utils.humanize(time.time() - s['down_since'])})"
                                    if s.get("down_since") else "")
                for s in down_sites[:10])
            line += f"\n🔴 Down now: {names}"
        return simple(line + "\nUse `/monitor status <name>` for one site, or `/monitor list` "
                             "to see all.")

    s, error = _find_one(args, ctx)
    if error:
        return error

    try:
        with KumaClient() as kc:
            st = kc.get_status(s["monitor_id"])
    except KumaError as exc:
        return simple(f":warning: Couldn't fetch live status.\n```{exc}```")

    # Deliberately not saved: the poller alerts when a reading differs from the
    # last one *it* saw, so recording this one would swallow the channel's alert.
    ping = f" · {int(st['ping'])} ms" if st.get("ping") else ""
    line = (f"{utils.status_emoji(st['status'])} *{s['name']}* is "
            f"*{st['status'].upper()}*{ping}\n{utils.link(s['url'])} · type *{s['site_type']}*")
    if st.get("msg") and st["status"] != "up":
        line += f"\n> {utils.slack_escape(st['msg'][:300])}"
    if s["expires_at"]:
        line += f"\n:hourglass_flowing_sand: expires in {utils.humanize(s['expires_at'] - time.time())}"
    return simple(line)


def cmd_remove(args, ctx):
    if not args:
        return simple("Usage: `/monitor remove <name or url>`")
    s, error = _find_one(args, ctx)
    if error:
        return error
    blocks = [
        section(f":wastebasket: Remove *{s['name']}* ({utils.link(s['url'])}, type "
                f"*{s['site_type']}*)?\nThis stops monitoring and deletes it from Uptime Kuma."),
        actions(button("Remove", "confirm_remove", s["id"], "danger"),
                button("Cancel", "cancel_remove", s["id"])),
    ]
    return {"text": f"Remove {s['name']}?", "blocks": blocks, "_private": True}


def cmd_extend(args, ctx):
    if not args:
        return simple("Usage: `/monitor extend <name or url> [days]`")
    days = config.CLIENT_TTL_DAYS
    if args[-1].isdigit():
        days = int(args[-1])
        args = args[:-1]
    if not utils.clean_identifier(" ".join(args)):
        return simple("Usage: `/monitor extend <name or url> [days]`")

    s, error = _find_one(args, ctx)
    if error:
        return error
    if not s["expires_at"]:
        return simple(f"*{s['name']}* is a *{s['site_type']}* site that's kept until removed — "
                      f"nothing to extend. (Make it temporary with `/monitor set {s['name']} client`.)")
    new_exp = max(s["expires_at"], time.time()) + days * 86400
    db.set_expiry(s["id"], new_exp)
    resp = simple(f":hourglass_flowing_sand: Extended *{s['name']}* — now expires in "
                  f"*{utils.humanize(new_exp - time.time())}*.")
    resp["_share"] = True
    return resp


def _reclassify(s, new_type, expires_at):
    db.reclassify(s["id"], new_type, expires_at)
    try:
        with KumaClient() as kc:
            kc.rename_monitor(s["monitor_id"], f"{new_type.upper()} | {s['name']}")
    except KumaError:
        pass  # the Kuma display name is cosmetic; our DB is the source of truth


def cmd_set(args, ctx):
    usage = ("Usage: `/monitor set <name or url> <type>`  (type: `permanent`, `prod`, "
             "`staging`, `demo`, `client`)")
    if len(args) < 2:
        return simple(usage)
    new_type = utils.normalize_type(args[-1])
    if not new_type:
        return simple(f"“{utils.slack_escape(args[-1])}” isn't a valid type. Use `permanent`, "
                      "`prod`, `staging`, `demo`, or `client`.")
    s, error = _find_one(args[:-1], ctx)
    if error:
        return error

    expires_at = None
    if new_type in config.EXPIRING_TYPES:
        # Already temporary? Keep its clock (`extend` is for adding time).
        expires_at = s["expires_at"] or time.time() + config.CLIENT_TTL_DAYS * 86400
    _reclassify(s, new_type, expires_at)

    if expires_at:
        resp = simple(f":arrows_counterclockwise: *{s['name']}* is now a *{new_type}* site — "
                      f"will auto-remove in *{utils.humanize(expires_at - time.time())}*.")
    else:
        kept = f"*{new_type}*" if new_type == "permanent" else f"permanent *{new_type}*"
        resp = simple(f":arrows_counterclockwise: *{s['name']}* is now a {kept} site — "
                      "kept until removed.")
    resp["_share"] = True
    return resp


def cmd_notify(args, ctx):
    """Who a channel's DOWN alerts ping: @here, @channel, people, groups, or nobody."""
    if _personal(ctx):
        return simple("`notify` picks who gets pinged in a *channel* when one of its sites goes "
                      "down. In a DM, I always message you directly.")
    channel_id = ctx["channel_id"]
    usage = ("Set it with `/monitor notify @here`, `@channel`, a person or a user group "
             "(pick them from the @ autocomplete), or `/monitor notify off`.")
    if not args:
        current = utils.describe_mentions(db.get_mention(channel_id))
        return {**simple(f":bell: Down alerts in this channel ping *{current}*.\n{usage}"),
                "_private": True}
    mention = utils.parse_mentions(args)
    if mention is None:
        return {**simple(f"I couldn't tell who to ping from that. {usage}"), "_private": True}
    db.set_mention(channel_id, mention, ctx["user_id"])
    if mention:
        text = (f":bell: Done — down alerts in this channel will ping "
                f"{utils.describe_mentions(mention)}. Recoveries stay quiet.")
    else:
        text = ":no_bell: Done — down alerts in this channel won't ping anyone."
    return {**simple(text), "_private": True}


# --------------------------------------------------------------------------- #
# Add-site form
# --------------------------------------------------------------------------- #
def _opt(value, text=None):
    return {"text": {"type": "plain_text", "text": text or value, "emoji": True}, "value": value}


def build_add_modal(channel_id=None, url=""):
    """The "Add a site" form. `channel_id` preselects where alerts go; with none
    (from Home, a DM, or a global shortcut) alerts default to the user's DM."""
    url_input = {"type": "plain_text_input", "action_id": "val",
                 "placeholder": {"type": "plain_text", "text": "example.com — https:// optional"}}
    if url:
        url_input["initial_value"] = url
    channel_select = {"type": "conversations_select", "action_id": "val",
                      "placeholder": {"type": "plain_text", "text": "Your DM with me"},
                      "filter": {"include": ["public", "private"]}}
    if channel_id:
        channel_select["initial_conversation"] = channel_id
    return {
        "type": "modal",
        "callback_id": "add_site_modal",
        "private_metadata": json.dumps({"channel_id": channel_id}),
        "title": {"type": "plain_text", "text": "Add a site"},
        "submit": {"type": "plain_text", "text": "Add"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "url",
             "label": {"type": "plain_text", "text": "URL"},
             "element": url_input},
            {"type": "input", "block_id": "type",
             "label": {"type": "plain_text", "text": "Type"},
             "element": {"type": "static_select", "action_id": "val",
                         "initial_option": _opt("permanent", "♾️ Permanent (kept until removed)"),
                         "options": [
                             _opt("permanent", "♾️ Permanent (kept until removed)"),
                             _opt("prod", "🚀 Prod (permanent)"),
                             _opt("staging", "🧪 Staging (permanent)"),
                             _opt("demo", "🎬 Demo (permanent)"),
                             _opt("client", f"👤 Client (auto-removed after {config.CLIENT_TTL_DAYS}d)"),
                         ]}},
            {"type": "input", "block_id": "channel", "optional": True,
             "label": {"type": "plain_text", "text": "Post alerts in"},
             "hint": {"type": "plain_text",
                      "text": "Leave empty to get alerts in a DM. Private channel? "
                              "Invite me there first: /invite @UptimeBot"},
             "element": channel_select},
            {"type": "input", "block_id": "name", "optional": True,
             "label": {"type": "plain_text", "text": "Name (optional)"},
             "element": {"type": "plain_text_input", "action_id": "val",
                         "placeholder": {"type": "plain_text", "text": "Defaults to the domain"}}},
        ],
    }


def _open_add_modal(client, trigger_id, channel_id=None, url=""):
    """Open the add form; False if Slack refused (e.g. the trigger expired)."""
    try:
        client.views_open(trigger_id=trigger_id, view=build_add_modal(channel_id, url))
        return True
    except Exception:
        log.exception("couldn't open the add form")
        return False


def _modal_channel(view):
    """The channel picked in a submitted add form (None = the user's DM)."""
    values = view["state"]["values"]
    if "channel" in values:
        return values["channel"]["val"].get("selected_conversation")
    # A form opened before the channel picker existed.
    meta = json.loads(view.get("private_metadata") or "{}")
    return None if meta.get("is_dm") else meta.get("channel_id")


# --------------------------------------------------------------------------- #
# Home tab
# --------------------------------------------------------------------------- #
def _home_actions(add_label="➕  Add a site"):
    return actions(button(add_label, "home_add", style="primary"),
                   button("🔄  Refresh", "home_refresh"))


_HOME_INTRO = (
    "*Know the second a site goes down.*\n"
    "I watch your sites around the clock and alert the right channel the moment one drops — "
    "and again when it's back."
)

_HOME_HOWTO = (
    "*Use me in any channel*\n"
    "• Type `/monitor example.com` in the channel — alerts for that site post right there.\n"
    "• Private channel? Invite me first: `/invite @UptimeBot`.\n"
    "• Want people pinged on outages? `/monitor notify @here` in that channel.\n"
    "• From anywhere: the ⚡ shortcuts *Monitor a website* and *Monitor this link*."
)

_HOME_COMMANDS = (
    "*Manage*  `/monitor list`  ·  `status <name>`  ·  `remove <name>`  ·  `extend <name>`  ·  "
    "`set <name> <type>`  ·  `help`\n"
    "_In a DM with me you can skip `/monitor` — or just paste a link._"
)


def build_home_view(sites):
    """Compose the App Home dashboard from an already-filtered list of sites
    (fast; no live Kuma call). The caller decides which sites the viewer sees."""
    blocks = [header("🤖  UptimeBot"), section(_HOME_INTRO)]

    if not sites:
        blocks += [
            {"type": "divider"},
            section("*You're not watching any sites yet.*\n"
                    "Add one here, or type `/monitor example.com` in any channel."),
            _home_actions("➕  Add your first site"),
            {"type": "divider"},
            section(_HOME_HOWTO),
        ]
        return {"type": "home", "blocks": blocks}

    up = sum(1 for s in sites if s["last_status"] == "up")
    down = sum(1 for s in sites if s["last_status"] == "down")
    other = len(sites) - up - down
    blocks += [
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*📡 Monitored*\n{len(sites)}"},
            {"type": "mrkdwn", "text": f"*🟢 Up*\n{up}"},
            {"type": "mrkdwn", "text": f"*🔴 Down*\n{down}"},
            {"type": "mrkdwn", "text": f"*🟡 Pending / other*\n{other}"},
        ]},
        _home_actions(),
        {"type": "divider"},
    ]

    # Surface anything that's down right now, above the per-type breakdown.
    down_sites = sorted((s for s in sites if s["last_status"] == "down"),
                        key=lambda x: x["name"].lower())
    body = []
    if down_sites:
        body += chunked_sections([_site_line(s, show_owner=True) for s in down_sites],
                                 head=":rotating_light:  *Needs attention*")
        body.append({"type": "divider"})
    for t in TYPE_ORDER:
        group = sorted((s for s in sites if s["site_type"] == t),
                       key=lambda x: x["name"].lower())
        if group:
            emoji, label = TYPE_META.get(t, ("•", t.title()))
            body += chunked_sections([_site_line(s, show_owner=True) for s in group],
                                     head=f"{emoji}  *{label}*  ·  {len(group)}")

    # Leave room for the footer (divider + how-to + commands + context) and an
    # "and more" note so a big workspace never blows past Slack's 100-block cap.
    budget = _MAX_HOME_BLOCKS - len(blocks) - 5
    if len(body) > budget:
        body = body[:budget] + [context_block("…and more — use `/monitor list` in a channel "
                                              "to see its sites.")]
    blocks += body
    blocks += [
        {"type": "divider"},
        section(_HOME_HOWTO),
        section(_HOME_COMMANDS),
        context_block(f"{STATUS_LEGEND}   ·   Showing sites you added or in your channels   ·   "
                      f"updated {utils.slack_date(time.time(), '{time}')} · status refreshes "
                      f"every {config.STATUS_POLL_SECONDS}s"),
    ]
    return {"type": "home", "blocks": blocks}


def _is_member(client, channel_id, user_id):
    """Is `user_id` a member of `channel_id`? Cached for `_MEMBER_TTL` seconds.
    Fails closed (False) on any error so the Home tab never leaks sites the
    viewer shouldn't see."""
    if not channel_id:
        return False
    key = (channel_id, user_id)
    cached = _member_cache.get(key)
    if cached is not None and time.time() - cached[1] < _MEMBER_TTL:
        return cached[0]
    result = False
    try:
        cursor = None
        while True:
            resp = client.conversations_members(channel=channel_id, cursor=cursor, limit=200)
            if user_id in resp.get("members", []):
                result = True
                break
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        log.debug("membership check failed for %s in %s", user_id, channel_id, exc_info=True)
        return False  # don't cache transient failures
    _member_cache[key] = (result, time.time())
    return result


def _visible_sites(client, user_id):
    """Sites this user should see on Home: ones they added, plus sites added in
    channels they belong to. Channel membership is looked up once per channel."""
    visible = []
    member_of = {}  # channel_id -> bool, memoised within this call
    for s in db.all_sites():
        if s["added_by"] == user_id:
            visible.append(s)
            continue
        if s["is_dm"]:
            continue  # someone else's DM site — never visible to others
        cid = s["channel_id"]
        if cid not in member_of:
            member_of[cid] = _is_member(client, cid, user_id)
        if member_of[cid]:
            visible.append(s)
    return visible


def publish_home(client, user_id):
    """Push the Home view to a user; never raise into a handler."""
    if not user_id:
        return
    try:
        view = build_home_view(_visible_sites(client, user_id))
        client.views_publish(user_id=user_id, view=view)
    except Exception:
        log.exception("couldn't publish Home tab for %s", user_id)


# --------------------------------------------------------------------------- #
# Button helpers
# --------------------------------------------------------------------------- #
def _action_channel(body):
    return ((body.get("channel") or {}).get("id")
            or (body.get("container") or {}).get("channel_id"))


def _site_for_action(body, respond):
    """The site a button refers to, if it still exists and the clicker may change it."""
    user_id = body["user"]["id"]
    channel_id = _action_channel(body)
    try:
        s = db.get_by_rowid(int(body["actions"][0]["value"]))
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not s:
        respond(text="That site is no longer tracked.", replace_original=True)
        return None
    is_dm = (channel_id or "").startswith("D")
    if not allowed(user_id, channel_id, is_dm) or not can_manage(s, user_id, channel_id):
        respond(text="⛔ Only people in the channel that owns this site, or whoever added it, "
                     "can change it.", response_type="ephemeral", replace_original=False)
        return None
    return s


# --------------------------------------------------------------------------- #
# Handler registration
# --------------------------------------------------------------------------- #
def register(app):
    @app.event("app_home_opened")
    def on_home_opened(event, client):
        if event.get("tab") != "home":
            return
        publish_home(client, event.get("user"))

    @app.event("member_joined_channel")
    def on_member_joined(event, client, context):
        channel_id = event.get("channel")
        if event.get("user") != context.get("bot_user_id"):
            return  # someone else joined a channel the bot is in
        joined_quietly = _quiet_joins.pop(channel_id, None)
        if joined_quietly and time.time() - joined_quietly < _QUIET_JOIN_TTL:
            return
        msg = welcome_message(channel_id)
        try:
            client.chat_postMessage(channel=channel_id, text=msg["text"], blocks=msg["blocks"])
        except Exception:
            log.exception("couldn't post the welcome message in %s", channel_id)

    @app.action("home_add")
    def home_add(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        if not allowed(user_id, None, True):
            _dm(client, user_id, _NO_ACCESS)
            return
        _open_add_modal(client, body["trigger_id"])

    @app.action("home_refresh")
    def home_refresh(ack, body, client):
        ack()
        publish_home(client, body["user"]["id"])

    @app.action("channel_add")
    def channel_add(ack, body, client, respond):
        ack()
        user_id = body["user"]["id"]
        channel_id = body["actions"][0].get("value") or _action_channel(body)
        if not allowed(user_id, channel_id, False):
            respond(text=_NO_ACCESS, response_type="ephemeral", replace_original=False)
            return
        _open_add_modal(client, body["trigger_id"], channel_id)

    @app.action("show_help")
    def show_help(ack, body, respond):
        ack()
        channel_id = _action_channel(body)
        resp = help_response({"is_dm": (channel_id or "").startswith("D")})
        respond(text=resp["text"], blocks=resp["blocks"], response_type="ephemeral",
                replace_original=False)

    @app.shortcut("add_site_shortcut")
    def add_site_shortcut(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        if not allowed(user_id, None, True):
            _dm(client, user_id, _NO_ACCESS)
            return
        _open_add_modal(client, body["trigger_id"])

    @app.shortcut("monitor_link_shortcut")
    def monitor_link_shortcut(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        channel = body.get("channel") or {}
        channel_id = channel.get("id")
        personal = is_personal_conversation(channel_id, channel.get("name"))
        if not allowed(user_id, channel_id, personal):
            _dm(client, user_id, _NO_ACCESS)
            return
        urls = utils.extract_urls((body.get("message") or {}).get("text", ""))
        _open_add_modal(client, body["trigger_id"], None if personal else channel_id,
                        urls[0] if urls else "")

    def _run_slash(ack, body, client, respond):
        ack()
        user_id = body["user_id"]
        channel_id = body.get("channel_id")
        channel_name = body.get("channel_name") or ""
        is_group_dm = channel_name.startswith("mpdm-")
        is_dm = not is_group_dm and is_personal_conversation(channel_id, channel_name)
        text = (body.get("text") or "").strip()

        if not allowed(user_id, channel_id, is_dm or is_group_dm):
            respond(_NO_ACCESS)
            return

        # No argument -> open the add form, preset to post alerts right here.
        words = utils.tokenize(text)
        if not words or (len(words) == 1 and words[0].lower() in _ADD_WORDS):
            preset = None if (is_dm or is_group_dm) else channel_id
            if not _open_add_modal(client, body["trigger_id"], preset):
                respond("Couldn't open the form — try `/monitor example.com` instead.")
            return

        ctx = make_ctx(client, user_id, channel_id, is_dm, is_group_dm)
        first = words[0].lower()
        if first in MANAGE_WORDS:
            resp = handle_command(text, ctx)
        else:
            # Anything else is "add these sites"; drop a leading add/monitor verb if present.
            if first in _ADD_WORDS:
                words = words[1:]
            resp = cmd_add(words, ctx)
        # Adds and changes are announced to the channel; everything else is just for you.
        share = resp.get("_share") and not (is_dm or is_group_dm)
        respond(blocks=resp.get("blocks"), text=resp.get("text"),
                response_type="in_channel" if share else "ephemeral")
        publish_home(client, user_id)

    app.command("/monitor")(_run_slash)
    app.command("/uptime")(_run_slash)  # older alias, if your app still defines it

    @app.event("app_mention")
    def on_mention(event, client, say, context):
        user_id = event.get("user")
        channel_id = event.get("channel")
        thread_ts = event.get("thread_ts")
        if not allowed(user_id, channel_id, is_dm=False):
            say(text="⛔ You don't have access to UptimeBot here.", thread_ts=thread_ts)
            return
        text = utils.strip_bot_mention(event.get("text", ""), context.get("bot_user_id"))
        resp = handle_command(text, make_ctx(client, user_id, channel_id, False))
        if resp.get("_private"):
            try:
                client.chat_postEphemeral(channel=channel_id, user=user_id, thread_ts=thread_ts,
                                          blocks=resp.get("blocks"), text=resp.get("text"))
                return
            except Exception:
                log.exception("couldn't reply privately in %s; replying in the channel", channel_id)
        say(blocks=resp.get("blocks"), text=resp.get("text"), thread_ts=thread_ts)

    @app.event("message")
    def on_message(event, client, say):
        # Only react to direct messages, and ignore bot/edited/system messages.
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        user_id = event.get("user")
        channel_id = event.get("channel")
        if not allowed(user_id, channel_id, is_dm=True):
            say("⛔ You don't have access to UptimeBot.")
            return
        text = (event.get("text") or "").strip()
        resp = handle_command(text, make_ctx(client, user_id, channel_id, True))
        say(blocks=resp.get("blocks"), text=resp.get("text"), thread_ts=event.get("thread_ts"))

    @app.action("confirm_remove")
    def confirm_remove(ack, body, client, respond):
        ack()
        s = _site_for_action(body, respond)
        if not s:
            return
        try:
            with KumaClient() as kc:
                kc.delete_monitor(s["monitor_id"])
        except KumaError as exc:
            respond(text=f":warning: Couldn't delete it from Uptime Kuma: {exc}\n"
                         f"It's still tracked — try again in a moment.",
                    replace_original=True)
            return
        db.remove_site(s["id"])
        user_id = body["user"]["id"]
        respond(text=f":wastebasket: Removed *{s['name']}* ({utils.link(s['url'])}). "
                     "Monitoring stopped.", replace_original=True)
        # The prompt was private, so let the owning channel know too.
        private_prompt = (body.get("container") or {}).get("is_ephemeral")
        if not s["is_dm"] and (private_prompt or _action_channel(body) != s["channel_id"]):
            post_to_owner(client, s, f"{s['name']} is no longer monitored",
                          [section(f":wastebasket: {display_name(client, user_id)} stopped "
                                   f"monitoring *{s['name']}* ({utils.link(s['url'])}).")])
        publish_home(client, user_id)

    @app.action("cancel_remove")
    def cancel_remove(ack, respond):
        ack()
        respond(text="Okay, nothing was removed.", replace_original=True)

    @app.action("expiry_extend")
    def expiry_extend(ack, body, client, respond):
        ack()
        s = _site_for_action(body, respond)
        if not s:
            return
        if not s["expires_at"]:
            respond(text=f"*{s['name']}* is kept until removed — nothing to extend.",
                    replace_original=True)
            return
        new_exp = max(s["expires_at"], time.time()) + config.CLIENT_TTL_DAYS * 86400
        db.set_expiry(s["id"], new_exp)
        respond(text=f":hourglass_flowing_sand: {display_name(client, body['user']['id'])} extended "
                     f"*{s['name']}* — it now expires in *{utils.humanize(new_exp - time.time())}*.",
                replace_original=True)

    @app.action("expiry_keep")
    def expiry_keep(ack, body, client, respond):
        ack()
        s = _site_for_action(body, respond)
        if not s:
            return
        _reclassify(s, "permanent", None)
        respond(text=f":infinity: {display_name(client, body['user']['id'])} made *{s['name']}* "
                     "permanent — it stays until someone removes it.",
                replace_original=True)

    @app.view("add_site_modal")
    def on_add_modal(ack, body, client, view):
        user_id = body["user"]["id"]
        vals = view["state"]["values"]
        url = utils.normalize_url(vals["url"]["val"]["value"])
        site_type = vals["type"]["val"]["selected_option"]["value"]
        name = utils.clean_name(vals["name"]["val"].get("value") or "")
        channel_id = _modal_channel(view)
        channel_field = "channel" if "channel" in vals else "url"

        if not utils.looks_like_url(url):
            ack(response_action="errors",
                errors={"url": "Enter a valid address, like example.com"})
            return
        if not allowed(user_id, channel_id, is_dm=not channel_id):
            ack(response_action="errors",
                errors={channel_field: "UptimeBot isn't enabled there. Ask an admin to allow it."})
            return
        if channel_id:
            try:
                check_channel(client, channel_id)
            except Undeliverable as exc:
                ack(response_action="errors", errors={channel_field: str(exc)})
                return
            dest = Dest(channel_id, False, None)
        else:
            dest = Dest(user_id, True, None)
        dup = _find_dup(url, dest)
        if dup:
            ack(response_action="errors",
                errors={"url": f"Already monitored there as “{dup['name']}”."})
            return
        ack()

        ctx = make_ctx(client, user_id, dest.channel_id, dest.is_dm)
        ctx["dest"] = dest
        resp = _do_add(url, name or utils.derive_name(url), site_type, ctx)
        # Success goes where the alerts will; a problem goes to the person who asked.
        target = dest.channel_id if resp.get("_share") else user_id
        try:
            client.chat_postMessage(channel=target, blocks=resp.get("blocks"), text=resp.get("text"))
        except Exception:
            log.exception("couldn't post the add confirmation to %s", target)
        publish_home(client, user_id)
