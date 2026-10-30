# UptimeBot: audit and upgrade report

22 October 2026 · branch `per-user-home-and-dedup` · not committed yet

## Summary

UptimeBot now works in any channel with one command: type `/monitor example.com`
in a channel, and alerts for that site post in that channel. In a public channel
this works even if nobody invited the bot. In a private channel the bot asks to
be invited first, instead of accepting the site and then losing its alerts.

The audit also found and fixed several bugs, the worst being alerts that were
never delivered. 100 automated tests pass, up from 33. Nothing has been tried
against a real Slack workspace or Uptime Kuma server yet.

## What you need to do

1. Rebuild and restart the bot:

   ```sh
   docker compose up -d --build
   ```

   The database upgrades itself on startup, and sites that are already down
   won't alert a second time. No `.env` changes are needed.

2. At [api.slack.com/apps](https://api.slack.com/apps), open UptimeBot, paste the
   new `manifest.yml` into **App Manifest** and save. Then reinstall the app:
   **OAuth & Permissions → Reinstall to Workspace**.

The reinstall turns on:

- auto-joining public channels (the new `channels:join` permission)
- the welcome message when the bot is invited to a channel
- the two shortcuts
- pinging a specific person or user group with `/monitor notify`

Everything else works before you reinstall. Until then, the bot posts in public
channels without joining them, using the `chat:write.public` permission it
already has.

## Issues found and fixed

- **Alerts were silently lost.** The bot accepted sites added in a private
  channel it wasn't in, a DM between two people, or a group DM. It can't post in
  any of those places, so every alert for those sites failed and only showed up
  in the logs. The bot now checks that it can post before adding a site (see
  [Where alerts go](#where-alerts-go)).
- **`status <name>` swallowed alerts.** Checking a site by hand saved its live
  status, so the poller never saw the change and the channel never got the DOWN
  alert. Manual checks no longer change the saved status.
- **Recoveries were missed.** If a site went down, then pending, then up between
  polls, no "back up" message was sent. The bot now tracks each outage from the
  DOWN alert until the site is up again, whatever readings come in between.
- **Sites were checked over plain HTTP.** Slack turns a typed `example.com` into
  an `http://` link, so sites added by DM or @mention were monitored over HTTP.
  An `&` in a URL also arrived as `&amp;`. Both are cleaned up now, and an
  address typed without a scheme gets `https://`.
- **Access checks were missing.** The Home tab button, the add form and the
  Remove buttons skipped the `ALLOWED_USER_IDS` check, and anyone who could see a
  Remove button could click it. All of them check access now. A site can be
  changed by the person who added it, or by anyone acting from inside the channel
  that owns it.
- **Polling was heavy.** Every poll downloaded 24 hours of heartbeats for every
  site. It now reads only the last hour, or longer if the check interval is over
  20 minutes.
- **Logins failed with 2FA.** Every Kuma connection logged in from scratch. With
  2FA on, two logins within the same 30 seconds failed, because Kuma rejects a
  reused 2FA code. The bot now logs in once and reuses the session. A 2FA account
  without `KUMA_MFA_SECRET` set now gets a clear error, instead of seeming to log
  in and failing later.
- **Long lists broke.** `list` failed once a single type had more than about 20
  sites, because Slack caps a message section at 3,000 characters. Long lists are
  now split across sections.
- **Smaller issues:**
  - `hey @UptimeBot list` didn't work, because the mention had to come first.
  - Replies to a mention inside a thread went to the channel instead of the
    thread.
  - Words like `v1.2` and email addresses were treated as URLs.
  - Running `set <site> client` on a client site reset its extended expiry date.
  - The same personal site could be added twice, once from the Home tab and once
    from a DM, because the two were saved under different IDs.
  - The help text showed channel users commands that only work in DMs.

## What's new

- **An add form with a channel picker.** You can open it from:
  - `/monitor` with nothing after it (the picker starts on the current channel)
  - the Home tab
  - the *Monitor a website* shortcut
  - the *Monitor this link* shortcut on any message, which fills in the link
- **A welcome message** with *Add a site* and *How it works* buttons when the bot
  is invited to a channel.
- **Clearer alerts.** DOWN alerts include Kuma's reason (for example "Request
  failed with status code 503") and when the outage was detected. On recovery,
  the original alert changes to "recovered" with how long the site was down, and
  a "back up" reply is posted in its thread and to the channel. Times show in
  each reader's own timezone.
- **Per-channel pings.** `/monitor notify @here` makes that channel's alerts ping
  `@here`. It also accepts `@channel`, a person, a user group, or `off`.
- **Several sites at once.** `/monitor a.com b.com c.com` adds up to 20 sites in
  one go.
- **One-click expiry buttons.** Expiry warnings for client sites have *Extend*
  and *Keep permanently* buttons.
- **Shared results.** With `/monitor`, adds and changes are posted for the whole
  channel to see. `list`, `status` and errors are shown only to you.
- **A DM fallback.** If the bot later loses access to a channel (removed from it,
  channel archived, posting restricted), alerts go to whoever added the site,
  with a note saying why.
- **Rate-limit retries.** Slack API calls that hit a rate limit are retried up to
  twice.

## Where alerts go

| Added in… | Alerts go to |
| --- | --- |
| A channel the bot is in | That channel |
| A public channel the bot isn't in yet | That channel. The bot joins it. |
| A private channel the bot isn't in | Nowhere. The bot asks you to `/invite @UptimeBot`, and nothing is added. |
| A DM with the bot, the Home tab, or the form with no channel picked | Your DM with the bot |
| A DM with someone else, or a group DM | Your DM with the bot, which tells you so |

## Changes by file

| File | What changed |
| --- | --- |
| `slack_app.py` | Mostly rewritten: delivery checks and the DM fallback, command parsing, bulk add, `notify`, the add form, shortcuts, the welcome message, access checks on buttons, the Home tab. |
| `scheduler.py` | Outage tracking, DOWN and recovery messages, expiry buttons. Every post uses the DM fallback. |
| `database.py` | Outage columns on `sites`, a `channel_settings` table for `notify`, and the automatic migration. |
| `kuma_client.py` | Session reuse, the shorter heartbeat window, the 2FA error. |
| `utils.py` | Slack link and URL cleanup, stricter URL checks, mention parsing, formatting helpers. |
| `app.py` | Retries Slack calls that hit a rate limit. |
| `manifest.yml` | The `channels:join` permission, the `member_joined_channel` event, two shortcuts, and `should_escape: true` on `/monitor`. |
| `README.md` | New "Add it to any channel in 10 seconds", "Where alerts go" and "Upgrading" sections. Usage rewritten around `/monitor`. |
| `.gitignore`, `.dockerignore` | Ignore `.DS_Store` and `.pytest_cache/`, and keep tests out of the Docker image. |
| `tests/` | New `test_channels.py` and `test_alerts.py`. `conftest.py`, `test_utils.py` and `test_database.py` extended. |

## How it was checked

- **Unit tests.** `pytest` reports 100 passed, up from 33. The tests use
  stand-ins for Slack and Kuma. They cover adding from every kind of
  conversation, the form and shortcuts, button permissions, the full alert
  lifecycle and the database migration.
- **Lint.** `pyflakes` reports nothing.
- **Real Slack Bolt.** A separate script, kept outside the repo, sent realistic
  Slack payloads through the real Slack Bolt framework, with a mock standing in
  for Slack's API. All 17 checks passed.
- **Not tested.** A real Slack workspace or Uptime Kuma server. Docker wasn't
  running, so Kuma couldn't be started locally. Try the bot in a test channel
  after you reinstall.

## Still open

- Nothing is committed yet.
- `.DS_Store` is tracked in git and was already modified before this work. It's
  now in `.gitignore`, but git keeps tracking it until you run
  `git rm --cached .DS_Store`.
