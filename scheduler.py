"""Background jobs: status-change alerts, client-site expiry, and reconciliation.

* status poller  - every STATUS_POLL_SECONDS, read each monitored site's live
  status from Uptime Kuma. The first *down* reading opens an outage and alerts
  the site's owner (a channel, pinging whoever `/monitor notify` chose, or a
  DM). The first *up* reading after that closes it: the DOWN alert is edited to
  show it recovered and how long it was down, and the recovery is posted in its
  thread. Readings in between (pending, maintenance) don't end an outage, so
  every DOWN alert gets exactly one recovery however the status wobbles.
* expiry sweeper - every EXPIRY_POLL_SECONDS, warn shortly before a client
  site expires (with one-click Extend / Keep buttons), then delete client sites
  whose time is up (from Uptime Kuma *and* from our database) and tell the owner.
* reconciler     - every RECONCILE_POLL_SECONDS, drop sites whose Kuma monitor
  was deleted outside the bot.

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
from slack_app import actions, button, context_block, post_to_owner, section

log = logging.getLogger("uptimebot.scheduler")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _safe(fn):
    """Wrap a job so an unhandled exception is logged instead of killing the job."""
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("scheduled job '%s' failed", getattr(fn, "__name__", "job"))
    wrapper.__name__ = getattr(fn, "__name__", "job")
    return wrapper


def _ping(st):
    return f" · {int(st['ping'])} ms" if st.get("ping") else ""


# --------------------------------------------------------------------------- #
# Status polling
# --------------------------------------------------------------------------- #
def _handle_status(slack_client, site, st):
    """Record a fresh status reading and alert on the edges of an outage."""
    new = st.get("status") or "pending"
    if new != site.get("last_status"):
        db.set_status(site["id"], new)
    if new == "down" and not site.get("down_since"):
        _alert_down(slack_client, site, st)
    elif new == "up" and site.get("down_since"):
        _alert_recovered(slack_client, site, st)


def _alert_down(slack_client, site, st):
    now = time.time()
    mention = "" if site.get("is_dm") else db.get_mention(site["channel_id"])
    lead = f"{mention} " if mention else ""
    body = f"{lead}🔴 *{site['name']}* is *DOWN*\n{utils.link(site['url'])}"
    reason = (st.get("msg") or "")[:300]
    if reason:
        body += f"\n> {utils.slack_escape(reason)}"
    blocks = [section(body),
              context_block(f"{site['site_type']} site · detected "
                            f"{utils.slack_date(now, '{time}')}")]
    posted = post_to_owner(slack_client, site, f"{lead}🔴 {site['name']} is DOWN", blocks)
    # If nothing could be posted, leave the outage unopened so the next poll retries.
    if posted:
        db.open_outage(site["id"], now, *posted)


def _alert_recovered(slack_client, site, st):
    now = time.time()
    took = utils.humanize(now - site["down_since"])
    text = f"🟢 {site['name']} is back up after {took}"
    reply = [section(f"🟢 *{site['name']}* is back *UP* after *{took}*{_ping(st)}\n"
                     f"{utils.link(site['url'])}")]
    channel, ts = site.get("alert_channel"), site.get("alert_ts")
    if channel and ts:
        resolved = [
            section(f"✅ *{site['name']}* recovered — it was down for *{took}*\n"
                    f"{utils.link(site['url'])}"),
            context_block(f"{site['site_type']} site · down "
                          f"{utils.slack_date(site['down_since'], '{time}')} → up "
                          f"{utils.slack_date(now, '{time}')}"),
        ]
        try:
            slack_client.chat_update(channel=channel, ts=ts, text=f"✅ {site['name']} recovered",
                                     blocks=resolved)
        except Exception:
            log.info("couldn't update the DOWN alert for %s", site["name"], exc_info=True)
        try:
            # Threaded so the incident reads as one story; broadcast so the
            # channel still sees it without opening the thread.
            slack_client.chat_postMessage(channel=channel, thread_ts=ts, reply_broadcast=True,
                                          text=text, blocks=reply)
            db.close_outage(site["id"])
            return
        except Exception:
            log.info("couldn't reply in the DOWN alert's thread for %s; posting it fresh",
                     site["name"], exc_info=True)
    post_to_owner(slack_client, site, text, reply)
    db.close_outage(site["id"])


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

    # 1) Warn about client sites expiring soon (once each), with one-click fixes.
    for s in db.get_expiring(now, horizon):
        left = utils.humanize((s["expires_at"] or now) - now)
        text = f"⏳ {s['name']} will be auto-removed in {left}"
        blocks = [
            section(f":hourglass_flowing_sand: *{s['name']}* ({utils.link(s['url'])}) is a "
                    f"*client* site and will be auto-removed in *{left}*."),
            actions(button(f"Extend {config.CLIENT_TTL_DAYS} days", "expiry_extend", s["id"],
                           "primary"),
                    button("Keep permanently", "expiry_keep", s["id"])),
        ]
        post_to_owner(slack_client, s, text, blocks)
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
                body = (f":wastebasket: *{s['name']}* ({utils.link(s['url'])}) was a *client* "
                        f"site and its time is up, so I've stopped monitoring it.\n"
                        f"Need it back? `/monitor client {utils.clean_identifier(s['url'])}`")
                post_to_owner(slack_client, s, text, [section(body)])
    except KumaError as exc:
        log.warning("expiry deletion skipped this cycle: %s", exc)


# --------------------------------------------------------------------------- #
# Reconciliation (heal drift between our DB and Kuma)
# --------------------------------------------------------------------------- #
def _reconcile_job(slack_client):
    """Drop DB rows whose Kuma monitor no longer exists (e.g. deleted directly
    in the Kuma UI), so we stop polling ghosts and the dashboard stays honest."""
    sites = db.all_sites()
    if not sites:
        return
    try:
        with KumaClient() as kc:
            live = kc.list_monitor_ids()
    except KumaError as exc:
        log.warning("reconcile skipped this cycle: %s", exc)
        return

    now = time.time()
    untracked = len(live) - sum(1 for s in sites if s["monitor_id"] in live)
    if untracked > 0:
        # Monitors in Kuma we don't track (could be created outside the bot).
        # Log only — we never delete monitors we didn't create.
        log.info("reconcile: %d Kuma monitor(s) not tracked by UptimeBot", untracked)

    for s in sites:
        if s["monitor_id"] in live:
            continue
        if now - (s.get("created_at") or 0) < 120:
            continue  # brand-new add; give Kuma a moment before judging it gone
        db.remove_site(s["id"])
        log.info("reconcile: dropped %s (monitor %s gone from Kuma)",
                 s["name"], s["monitor_id"])
        text = f"⚠️ Stopped tracking {s['name']}"
        body = (f":warning: Stopped tracking *{s['name']}* ({utils.link(s['url'])}) — its "
                f"monitor was deleted in Uptime Kuma.\n"
                f"Re-add it with `/monitor {utils.clean_identifier(s['url'])}`.")
        post_to_owner(slack_client, s, text, [section(body)])


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
    sched.add_job(
        _safe(_reconcile_job), "interval",
        seconds=max(300, config.RECONCILE_POLL_SECONDS),
        args=[slack_client], id="reconcile",
        max_instances=1, coalesce=True,
    )
    # Run an expiry sweep + a reconcile shortly after boot so a restart doesn't
    # leave expired client sites lingering or keep polling monitors that were
    # deleted in Kuma while we were down. Scheduled (not inline) so they never
    # block startup if Kuma is briefly unreachable.
    sched.add_job(
        _safe(_expiry_job), "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=10),
        args=[slack_client], id="expiry_boot",
    )
    sched.add_job(
        _safe(_reconcile_job), "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=15),
        args=[slack_client], id="reconcile_boot",
    )

    sched.start()
    log.info("scheduler started: status poll every %ss, expiry sweep every %ss, "
             "reconcile every %ss",
             config.STATUS_POLL_SECONDS, config.EXPIRY_POLL_SECONDS,
             config.RECONCILE_POLL_SECONDS)
    return sched
