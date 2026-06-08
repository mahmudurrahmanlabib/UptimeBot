"""Slack interface: command parsing, Block Kit responses, modal, and handlers.

Interaction model
-----------------
* DM the bot       -> free-form chat (any message is treated as a command)
* @mention in a    -> the bot replies in that channel; the channel "owns" the
  channel              site, so its up/down + expiry notifications go there
* /uptime          -> opens an "Add a site" modal (or runs a text command)

Notifications are routed by the scheduler to each site's `channel_id`, which is
the channel it was added in (or the user's DM).
"""
import json
import logging
import re
import time

import config
import database as db
import utils
from kuma_client import KumaClient, KumaError

log = logging.getLogger("uptimebot.slack")

_name_cache = {}


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


def make_ctx(client, user_id, channel_id, is_dm):
    return {
        "client": client,
        "user_id": user_id,
        "channel_id": channel_id,
        "is_dm": is_dm,
        "user_name": display_name(client, user_id),
    }


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


def help_response():
    intro = ("Hi! I watch your websites and tell you the moment one goes *down* — "
             "and again when it *recovers*.")
    add = ("*Add a site*\n"
           "• `/monitor example.com` — kept until you remove it\n"
           f"• `/monitor client example.com` — auto-removed after {config.CLIENT_TTL_DAYS} days\n"
           "_No need for https:// — and in a DM you can just paste a link._")
    manage = ("*Manage*\n"
              "• `list` — everything I'm monitoring\n"
              "• `status <name>` — is a site up right now?\n"
              "• `remove <name>` — stop monitoring a site\n"
              "• `extend <name>` · `set <name> <type>` — adjust a site")
    return {
        "text": "UptimeBot help",
        "blocks": [
            header("🤖  UptimeBot"),
            section(intro),
            {"type": "divider"},
            section(add),
            section(manage),
            context_block("Open my *Home* tab for a live dashboard of every site."),
        ],
    }


# --------------------------------------------------------------------------- #
# Command dispatch
# --------------------------------------------------------------------------- #
def handle_command(text, ctx):
    text = (text or "").strip()
    if not text:
        return help_response()
    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("help", "?", "hi", "hello", "hey"):
        return help_response()
    if cmd in ("monitor", "mon", "watch", "track", "add"):
        return cmd_add(args, ctx)
    if cmd in ("list", "ls", "sites"):
        return cmd_list(args, ctx)
    if cmd in ("status", "check"):
        return cmd_status(args, ctx)
    if cmd in ("remove", "rm", "delete", "del"):
        return cmd_remove(args, ctx)
    if cmd in ("extend", "renew"):
        return cmd_extend(args, ctx)
    if cmd in ("set", "reclassify", "type", "promote"):
        return cmd_set(args, ctx)
    # No command verb — if the message contains a URL, treat it as "add this site".
    if any(utils.looks_like_url(utils.normalize_url(p)) for p in parts):
        return cmd_add(parts, ctx)
    return simple("I didn't recognise that. Type *help*, or just send me a link "
                  "like `example.com` and I'll start monitoring it.")


def _ambiguous(matches, identifier):
    lines = [f"• *{m['name']}* — <{m['url']}> ({m['site_type']})" for m in matches[:10]]
    extra = "" if len(matches) <= 10 else f"\n…and {len(matches) - 10} more."
    return simple(
        f"Multiple sites match “{identifier}”. Please be more specific:\n"
        + "\n".join(lines) + extra
    )


def cmd_add(args, ctx):
    if not args:
        return simple(
            "To add a site: `/monitor example.com` (kept until removed) or "
            f"`/monitor client example.com` (auto-removed after {config.CLIENT_TTL_DAYS} days).\n"
            "_No https:// needed. You can also paste a link to me in a DM._"
        )

    # Locate the URL anywhere in the args.
    url_idx = None
    for i, tok in enumerate(args):
        if utils.looks_like_url(utils.normalize_url(tok)):
            url_idx = i
            break
    if url_idx is None:
        return simple(
            f"“{' '.join(args)}” doesn't include a URL I recognise. "
            "Try `/monitor example.com`."
        )
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

    name = " ".join(args[i] for i in range(len(args)) if i not in consumed).strip()
    name = name or utils.derive_name(url)
    return _do_add(url, name, site_type, ctx)


def _do_add(url, name, site_type, ctx):
    monitor_name = f"{site_type.upper()} | {name}"
    expires_at = None
    if site_type in config.EXPIRING_TYPES:
        expires_at = time.time() + config.CLIENT_TTL_DAYS * 86400

    try:
        with KumaClient() as kc:
            monitor_id = kc.add_monitor(monitor_name, url)
    except KumaError as exc:
        return simple(f":warning: Couldn't add the monitor.\n```{exc}```")

    db.add_site(
        monitor_id=monitor_id, name=name, url=url, site_type=site_type,
        added_by=ctx["user_id"], added_by_name=ctx["user_name"],
        channel_id=ctx["channel_id"], is_dm=ctx["is_dm"], expires_at=expires_at,
    )

    if expires_at:
        when = utils.humanize(expires_at - time.time())
        tail = (f"\n:hourglass_flowing_sand: As a *client* site it'll be auto-removed in "
                f"*{when}*. Use `extend {name}` to keep it longer, or `set {name} permanent` "
                f"to keep it for good.")
    else:
        tail = "\n:infinity: Kept *permanently* — stays until you `remove` it."

    return simple(
        f":white_check_mark: Now monitoring *{name}*\n<{url}> · type *{site_type}*{tail}"
    )


def cmd_list(args, ctx):
    flt = utils.normalize_type(args[0]) if args else None
    sites = db.list_sites(flt)
    if not sites:
        msg = "No sites are being monitored yet. Add one with `monitor <url>`."
        if flt:
            msg = f"No *{flt}* sites are being monitored yet."
        return simple(msg)

    blocks = [header("📊 Monitored sites")]
    for t in TYPE_ORDER:
        group = [s for s in sites if s["site_type"] == t]
        if not group:
            continue
        emoji, label = TYPE_META.get(t, ("•", t.title()))
        lines = []
        for s in group:
            line = f"{utils.status_emoji(s['last_status'])} *{s['name']}* — <{s['url']}>"
            if s["expires_at"]:
                line += f"  · _expires in {utils.humanize(s['expires_at'] - time.time())}_"
            lines.append(line)
        blocks.append(section(f"{emoji} *{label}*  ({len(group)})\n" + "\n".join(lines)))
    blocks.append(context_block(STATUS_LEGEND))
    return {"text": "Monitored sites", "blocks": blocks}


def cmd_status(args, ctx):
    if not args:
        sites = db.all_sites()
        if not sites:
            return simple("Nothing is being monitored yet. Add a site with `add <url> [type]`.")
        counts = {}
        for s in sites:
            counts[s["last_status"]] = counts.get(s["last_status"], 0) + 1
        up, down = counts.get("up", 0), counts.get("down", 0)
        line = f"*{len(sites)}* sites monitored — 🟢 {up} up · 🔴 {down} down"
        others = ", ".join(f"{utils.status_emoji(k)} {v} {k}"
                           for k, v in counts.items() if k not in ("up", "down"))
        if others:
            line += f" · {others}"
        return simple(line + "\nUse `status <name>` for one site, or `list` to see all.")

    identifier = " ".join(args)
    matches = db.find_sites(identifier)
    if not matches:
        return simple(f"I couldn't find a site matching “{identifier}”.")
    if len(matches) > 1:
        return _ambiguous(matches, identifier)
    s = matches[0]

    try:
        with KumaClient() as kc:
            st = kc.get_status(s["monitor_id"])
    except KumaError as exc:
        return simple(f":warning: Couldn't fetch live status.\n```{exc}```")

    db.set_status(s["id"], st["status"])
    ping = f" · {int(st['ping'])} ms" if st.get("ping") else ""
    line = (f"{utils.status_emoji(st['status'])} *{s['name']}* is "
            f"*{st['status'].upper()}*{ping}\n<{s['url']}> · type *{s['site_type']}*")
    if s["expires_at"]:
        line += f"\n:hourglass_flowing_sand: expires in {utils.humanize(s['expires_at'] - time.time())}"
    return simple(line)


def cmd_remove(args, ctx):
    if not args:
        return simple("Usage: *remove <name or url>*")
    identifier = " ".join(args)
    matches = db.find_sites(identifier)
    if not matches:
        return simple(f"I couldn't find a site matching “{identifier}”.")
    if len(matches) > 1:
        return _ambiguous(matches, identifier)
    s = matches[0]
    blocks = [
        section(f":wastebasket: Remove *{s['name']}* (<{s['url']}>, type *{s['site_type']}*)?\n"
                f"This stops monitoring and deletes it from Uptime Kuma."),
        {"type": "actions", "elements": [
            {"type": "button", "style": "danger",
             "text": {"type": "plain_text", "text": "Remove"},
             "action_id": "confirm_remove", "value": str(s["id"])},
            {"type": "button",
             "text": {"type": "plain_text", "text": "Cancel"},
             "action_id": "cancel_remove", "value": str(s["id"])},
        ]},
    ]
    return {"text": f"Remove {s['name']}?", "blocks": blocks}


def cmd_extend(args, ctx):
    if not args:
        return simple("Usage: *extend <name or url> [days]*")
    days = config.CLIENT_TTL_DAYS
    if args[-1].isdigit():
        days = int(args[-1])
        identifier = " ".join(args[:-1])
    else:
        identifier = " ".join(args)
    if not identifier:
        return simple("Usage: *extend <name or url> [days]*")

    matches = db.find_sites(identifier)
    if not matches:
        return simple(f"I couldn't find a site matching “{identifier}”.")
    if len(matches) > 1:
        return _ambiguous(matches, identifier)
    s = matches[0]
    if not s["expires_at"]:
        label = s["site_type"] if s["site_type"] != "permanent" else "permanent"
        return simple(f"*{s['name']}* is a *{label}* site that's kept until removed — "
                      f"nothing to extend. (Make it temporary with `set {s['name']} client`.)")
    new_exp = max(s["expires_at"], time.time()) + days * 86400
    db.set_expiry(s["id"], new_exp)
    return simple(f":hourglass_flowing_sand: Extended *{s['name']}* — now expires in "
                  f"*{utils.humanize(new_exp - time.time())}*.")


def cmd_set(args, ctx):
    if len(args) < 2:
        return simple("Usage: *set <name or url> <type>*  (type: `permanent`, `prod`, `staging`, `demo`, `client`)")
    new_type = utils.normalize_type(args[-1])
    if not new_type:
        return simple(f"“{args[-1]}” isn't a valid type. Use `permanent`, `prod`, `staging`, `demo`, or `client`.")
    identifier = " ".join(args[:-1])
    matches = db.find_sites(identifier)
    if not matches:
        return simple(f"I couldn't find a site matching “{identifier}”.")
    if len(matches) > 1:
        return _ambiguous(matches, identifier)
    s = matches[0]

    expires_at = None
    if new_type in config.EXPIRING_TYPES:
        expires_at = time.time() + config.CLIENT_TTL_DAYS * 86400
    db.reclassify(s["id"], new_type, expires_at)

    try:
        with KumaClient() as kc:
            kc.rename_monitor(s["monitor_id"], f"{new_type.upper()} | {s['name']}")
    except KumaError:
        pass  # the Kuma display name is cosmetic; our DB is the source of truth

    if expires_at:
        return simple(f":arrows_counterclockwise: *{s['name']}* is now a *{new_type}* site — "
                      f"will auto-remove in *{utils.humanize(expires_at - time.time())}*.")
    kept = f"*{new_type}*" if new_type == "permanent" else f"permanent *{new_type}*"
    return simple(f":arrows_counterclockwise: *{s['name']}* is now a {kept} site — kept until removed.")


# --------------------------------------------------------------------------- #
# Add-site modal
# --------------------------------------------------------------------------- #
def _opt(value, text=None):
    return {"text": {"type": "plain_text", "text": text or value, "emoji": True}, "value": value}


def build_add_modal(channel_id, is_dm):
    return {
        "type": "modal",
        "callback_id": "add_site_modal",
        "private_metadata": json.dumps({"channel_id": channel_id, "is_dm": is_dm}),
        "title": {"type": "plain_text", "text": "Add a site"},
        "submit": {"type": "plain_text", "text": "Add"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "url",
             "label": {"type": "plain_text", "text": "URL"},
             "element": {"type": "plain_text_input", "action_id": "val",
                         "placeholder": {"type": "plain_text", "text": "https://example.com"}}},
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
            {"type": "input", "block_id": "name", "optional": True,
             "label": {"type": "plain_text", "text": "Name (optional)"},
             "element": {"type": "plain_text_input", "action_id": "val",
                         "placeholder": {"type": "plain_text", "text": "Defaults to the domain"}}},
        ],
    }


# --------------------------------------------------------------------------- #
# Home tab
# --------------------------------------------------------------------------- #
def _home_actions():
    return {"type": "actions", "elements": [
        {"type": "button", "style": "primary", "action_id": "home_add",
         "text": {"type": "plain_text", "text": "➕  Add a site", "emoji": True}},
        {"type": "button", "action_id": "home_refresh",
         "text": {"type": "plain_text", "text": "🔄  Refresh", "emoji": True}},
    ]}


_HOME_INTRO = (
    "*Uptime monitoring for your team's sites — right from Slack.*\n"
    "I check each site continuously and post here the moment one goes down, "
    "then again when it recovers."
)

_HOME_COMMANDS = (
    "*Add*  `/monitor example.com`  ·  *temporary*  `/monitor client example.com`\n"
    "*Manage*  `list`  ·  `status <name>`  ·  `remove <name>`  ·  `extend <name>`  ·  `set <name> <type>`\n"
    "_You can also just paste a link to me in a direct message._"
)


def build_home_view():
    """Compose the App Home dashboard from the database (fast; no live Kuma call)."""
    sites = db.all_sites()
    blocks = [header("🤖  UptimeBot"), section(_HOME_INTRO)]

    if not sites:
        blocks += [
            {"type": "divider"},
            section("*Get started*\nNothing is being monitored yet — add your first site:"),
            _home_actions(),
            section(_HOME_COMMANDS),
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

    for t in TYPE_ORDER:
        group = sorted((s for s in sites if s["site_type"] == t),
                       key=lambda x: x["name"].lower())
        if not group:
            continue
        emoji, label = TYPE_META.get(t, ("•", t.title()))
        lines = []
        for s in group:
            line = f"{utils.status_emoji(s['last_status'])}  *{s['name']}*  —  <{s['url']}>"
            if s["expires_at"]:
                line += f"  · _expires in {utils.humanize(s['expires_at'] - time.time())}_"
            lines.append(line)
        head = f"{emoji}  *{label}*  ·  {len(group)}"
        # Split into <=20-line sections to stay within Block Kit's per-section limit.
        for i in range(0, len(lines), 20):
            text = (head + "\n" if i == 0 else "") + "\n".join(lines[i:i + 20])
            blocks.append(section(text))

    blocks += [
        {"type": "divider"},
        section(_HOME_COMMANDS),
        context_block(f"{STATUS_LEGEND}   ·   updated {time.strftime('%H:%M %Z')} · "
                      f"status refreshes every {config.STATUS_POLL_SECONDS}s"),
    ]
    return {"type": "home", "blocks": blocks}


def publish_home(client, user_id):
    """Push the Home view to a user; never raise into a handler."""
    if not user_id:
        return
    try:
        client.views_publish(user_id=user_id, view=build_home_view())
    except Exception:
        log.exception("couldn't publish Home tab for %s", user_id)


# --------------------------------------------------------------------------- #
# Handler registration
# --------------------------------------------------------------------------- #
def register(app):
    @app.event("app_home_opened")
    def on_home_opened(event, client):
        if event.get("tab") != "home":
            return
        publish_home(client, event.get("user"))

    @app.action("home_add")
    def home_add(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        # Sites added from Home are owned by the user's DM, so alerts reach them there.
        try:
            client.views_open(trigger_id=body["trigger_id"],
                              view=build_add_modal(user_id, True))
        except Exception:
            log.exception("couldn't open add modal from Home")

    @app.action("home_refresh")
    def home_refresh(ack, body, client):
        ack()
        publish_home(client, body["user"]["id"])

    _MGMT = {"list", "ls", "sites", "status", "check", "remove", "rm", "delete",
             "del", "extend", "renew", "set", "reclassify", "type", "promote",
             "help", "?", "hi", "hello", "hey"}

    def _run_slash(ack, body, client, respond):
        ack()
        user_id = body["user_id"]
        channel_id = body.get("channel_id")
        is_dm = body.get("channel_name") == "directmessage"
        text = (body.get("text") or "").strip()

        if not allowed(user_id, channel_id, is_dm):
            respond("⛔ You don't have access to UptimeBot. Ask an admin to add you.")
            return

        # No argument -> open the add form.
        if text == "" or text.lower() in ("add", "monitor"):
            try:
                client.views_open(trigger_id=body["trigger_id"],
                                  view=build_add_modal(channel_id, is_dm))
            except Exception as exc:
                respond(f"Couldn't open the form: {exc}")
            return

        ctx = make_ctx(client, user_id, channel_id, is_dm)
        words = text.split()
        first = words[0].lower()
        if first in _MGMT:
            resp = handle_command(text, ctx)
        else:
            # Anything else is "add this site"; drop a leading add/monitor verb if present.
            if first in ("add", "monitor", "watch", "track"):
                words = words[1:]
            resp = cmd_add(words, ctx)
        respond(blocks=resp.get("blocks"), text=resp.get("text"))
        publish_home(client, user_id)

    app.command("/monitor")(_run_slash)
    app.command("/uptime")(_run_slash)

    @app.event("app_mention")
    def on_mention(event, client, say):
        user_id = event.get("user")
        channel_id = event.get("channel")
        if not allowed(user_id, channel_id, is_dm=False):
            say("⛔ You don't have access to UptimeBot here.")
            return
        text = re.sub(r"^\s*<@[^>]+>\s*", "", event.get("text", "")).strip()
        resp = handle_command(text, make_ctx(client, user_id, channel_id, False))
        say(blocks=resp.get("blocks"), text=resp.get("text"))

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
        say(blocks=resp.get("blocks"), text=resp.get("text"))

    @app.action("confirm_remove")
    def confirm_remove(ack, body, client, respond):
        ack()
        rowid = int(body["actions"][0]["value"])
        s = db.get_by_rowid(rowid)
        if not s:
            respond(text="That site is no longer tracked.", replace_original=True)
            return
        try:
            with KumaClient() as kc:
                kc.delete_monitor(s["monitor_id"])
        except KumaError as exc:
            respond(text=f":warning: Couldn't delete it from Uptime Kuma: {exc}\n"
                         f"It's still tracked — try again in a moment.",
                    replace_original=True)
            return
        db.remove_site(rowid)
        respond(text=f":wastebasket: Removed *{s['name']}* (<{s['url']}>). Monitoring stopped.",
                replace_original=True)
        publish_home(client, body["user"]["id"])

    @app.action("cancel_remove")
    def cancel_remove(ack, respond):
        ack()
        respond(text="Okay, nothing was removed.", replace_original=True)

    @app.view("add_site_modal")
    def on_add_modal(ack, body, client, view):
        vals = view["state"]["values"]
        url = utils.normalize_url(vals["url"]["val"]["value"])
        site_type = vals["type"]["val"]["selected_option"]["value"]
        name = (vals["name"]["val"].get("value") or "").strip()

        if not utils.looks_like_url(url):
            ack(response_action="errors",
                errors={"url": "Enter a valid URL like https://example.com"})
            return
        ack()

        meta = json.loads(view.get("private_metadata") or "{}")
        channel_id = meta.get("channel_id")
        is_dm = bool(meta.get("is_dm"))
        user_id = body["user"]["id"]
        ctx = make_ctx(client, user_id, channel_id, is_dm)
        if not name:
            name = utils.derive_name(url)

        resp = _do_add(url, name, site_type, ctx)
        target = channel_id or user_id
        try:
            client.chat_postMessage(channel=target, blocks=resp.get("blocks"), text=resp.get("text"))
        except Exception:
            try:  # private channel the bot isn't in, etc. -> fall back to a DM
                client.chat_postMessage(channel=user_id, blocks=resp.get("blocks"), text=resp.get("text"))
            except Exception:
                log.exception("could not post add confirmation")
        publish_home(client, user_id)
