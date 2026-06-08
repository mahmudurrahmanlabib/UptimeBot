"""Background jobs: status-change notifications + client-site expiry.

Two periodic jobs run on a background scheduler:

* status poller  - every STATUS_POLL_SECONDS, read each monitored site's live
  status from Uptime Kuma. When a site flips between *up* and *down*, post an
  alert to the Slack channel (or DM) the site was added in.
* expiry sweeper - every EXPIRY_POLL_SECONDS, warn shortly before a client
  site expires, then delete client sites whose time is up (from Uptime Kuma
  *and* from our database) and tell the owning channel.

Why poll instead of using Uptime Kuma's own Slack notifications? Kuma's
notifications are global and can't route a given monitor to the specific Slack
channel that asked for it. Polling lets us send the right alert to the right
place, and lets the bot own client-site retention, which Kuma knows nothing
about.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

import config
import database as db
import utils
from kuma_client import KumaClient, KumaError
from slack_app import context_block, section

log = logging.getLogger("uptimebot.scheduler")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _post(slack_client, channel, text, blocks=None):
    """Post to a channel/DM, swallowing errors so a job never dies on a bad post."""
    if not channel:
        return
    try:
        slack_client.chat_postMessage(channel=channel, text=text, blocks=blocks)
    except Exception:
        log.exception("failed to post UptimeBot notification to %s", channel)


def _safe(fn):
    """Wrap a job so an unhandled exception is logged instead of killing the job."""
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("scheduled job '%s' failed", getattr(fn, "__name__", "job"))
    wrapper.__name__ = getattr(fn, "__name__", "job")
    return wrapper


# --------------------------------------------------------------------------- #
# Status polling
# --------------------------------------------------------------------------- #
def _handle_status(slack_client, site, st):
    """Compare a freshly-read status to what we last saw and alert on up<->down."""
    new = st.get("status", "pending")
    prev = site.get("last_status") or "pending"
    if new == prev:
        return
    db.set_status(site["id"], new)

    going_down = new == "down" and prev != "down"
    recovered = new == "up" and prev == "down"
    if not (going_down or recovered):
        return  # e.g. pending->up, ->maintenance: record it, don't shout about it

    emoji = utils.status_emoji(new)
    ping = f" · {int(st['ping'])} ms" if st.get("ping") else ""
    when = time.strftime("%Y-%m-%d %H:%M %Z")
    if going_down:
        text = f"{emoji} {site['name']} is DOWN"
        body = (f"{emoji} *{site['name']}* is *DOWN*{ping}\n"
                f"<{site['url']}> · type *{site['site_type']}*")
    else:
        text = f"{emoji} {site['name']} recovered"
        body = (f"{emoji} *{site['name']}* is back *UP*{ping}\n"
                f"<{site['url']}> · type *{site['site_type']}*")
    _post(slack_client, site["channel_id"], text,
          [section(body), context_block(f"UptimeBot · {when}")])


def _status_job(slack_client):
    sites = db.all_sites()
    if not sites:
        return
    try:
        with KumaClient() as kc:
            for s in sites:
                try:
                    st = kc.get_status(s["monitor_id"])
                except KumaError as exc:
                    # One bad/missing monitor shouldn't abort the whole sweep.
                    log.debug("status check failed for %s (monitor %s): %s",
                              s["name"], s["monitor_id"], exc)
                    continue
                _handle_status(slack_client, s, st)
    except KumaError as exc:
        # Couldn't even connect/log in this cycle; try again next interval.
        log.warning("status poll skipped this cycle: %s", exc)


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
def _expiry_job(slack_client):
    now = time.time()
    horizon = config.EXPIRY_WARN_HOURS * 3600

    # 1) Warn about client sites expiring soon (once each).
    for s in db.get_expiring(now, horizon):
        left = utils.humanize((s["expires_at"] or now) - now)
        text = f"⏳ {s['name']} expires in {left}"
        body = (f":hourglass_flowing_sand: *{s['name']}* (<{s['url']}>) is a *client* site "
                f"and will be auto-removed in *{left}*.\n"
                f"Keep it: `extend {s['name']}`  ·  make it permanent: `set {s['name']} prod`.")
        _post(slack_client, s["channel_id"], text, [section(body)])
        db.mark_warned(s["id"])

    # 2) Delete client sites whose time is up.
    expired = db.get_expired(now)
    if not expired:
        return
    try:
        with KumaClient() as kc:
            for s in expired:
                try:
                    kc.delete_monitor(s["monitor_id"])
                except KumaError as exc:
                    # Leave it in the DB and retry next cycle rather than losing
                    # track of a monitor that may still exist in Kuma.
                    log.warning("couldn't delete expired monitor %s (%s): %s",
                                s["monitor_id"], s["name"], exc)
                    continue
                db.remove_site(s["id"])
                text = f"🗑️ Removed {s['name']} (client site expired)"
                body = (f":wastebasket: *{s['name']}* (<{s['url']}>) was a *client* site and its "
                        f"{config.CLIENT_TTL_DAYS}-day window is up, so monitoring has stopped.\n"
                        f"Need it back? `add {s['url']} client`.")
                _post(slack_client, s["channel_id"], text, [section(body)])
    except KumaError as exc:
        log.warning("expiry deletion skipped this cycle: %s", exc)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def start(slack_client):
    """Start the background scheduler and return it (call .shutdown() to stop)."""
    sched = BackgroundScheduler(timezone="UTC")

    sched.add_job(
        _safe(_status_job), "interval",
        seconds=max(15, config.STATUS_POLL_SECONDS),
        args=[slack_client], id="status_poll",
        max_instances=1, coalesce=True,
    )
    sched.add_job(
        _safe(_expiry_job), "interval",
        seconds=max(60, config.EXPIRY_POLL_SECONDS),
        args=[slack_client], id="expiry_sweep",
        max_instances=1, coalesce=True,
    )
    # Run an expiry sweep shortly after boot so a restart doesn't leave expired
    # client sites lingering until the first interval fires. Scheduled (not
    # inline) so it never blocks startup if Kuma is briefly unreachable.
    sched.add_job(
        _safe(_expiry_job), "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=10),
        args=[slack_client], id="expiry_boot",
    )

    sched.start()
    log.info("scheduler started: status poll every %ss, expiry sweep every %ss",
             config.STATUS_POLL_SECONDS, config.EXPIRY_POLL_SECONDS)
    return sched
