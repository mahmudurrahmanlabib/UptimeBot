# UptimeBot

A simple Slack bot that lets your support and product teams add and manage
[Uptime Kuma](https://github.com/louislam/uptime-kuma) monitors from **any Slack
channel** (or a DM). It also handles **retention**: sites are kept until you
remove them, while **client sites are auto-removed after a week**.

```
#ops   You:        /monitor app.acme.com
       UptimeBot:  ✅ Now monitoring app.acme.com — type permanent · alerts post in #ops

#ops   You:        /monitor notify @here
       UptimeBot:  🔔 Done — down alerts in this channel will ping @here.

#ops   UptimeBot:  @here 🔴 app.acme.com is DOWN
                   > Request failed with status code 503
       (12 minutes later, in the alert's thread, also shown in the channel)
       UptimeBot:  🟢 app.acme.com is back UP after 12m
```

When a monitored site goes down, UptimeBot alerts the channel where that site was
added. When it recovers, the alert is marked ✅ resolved with how long the site
was down, and the recovery is posted in the alert's thread.

## Add it to any channel in 10 seconds

1. In the channel, type **`/monitor example.com`**. That's it — alerts for that
   site now post in this channel. (Works in public channels even before you
   invite the bot; it joins by itself.)
2. **Private channel?** Invite it first: `/invite @UptimeBot`. If you forget,
   it tells you instead of silently dropping alerts.
3. Optional: **`/monitor notify @here`** so outages ping the channel (or
   `@someone`, a user group like `@oncall`, or `off`).

Other ways in, all of which let you pick the channel for alerts:

- **Invite the bot** (`/invite @UptimeBot`) — it says hello with an **Add a site** button.
- **`/monitor`** with nothing after it opens the **Add a site** form, preset to the current channel.
- **⚡ Shortcuts:** *Monitor a website* (from the composer's shortcut menu) and
  *Monitor this link* (from any message's ⋮ menu — the link is filled in for you).
- **Home tab → Add a site** — choose any channel, or leave it empty to get alerts in your DM.
- **Several at once:** `/monitor a.com b.com c.com`.

---

## ⚠️ Version compatibility — read this first

Uptime Kuma has **no official REST API**. UptimeBot talks to it through the
community Python library [`uptime-kuma-api`](https://github.com/lucasheld/uptime-kuma-api),
which speaks Kuma's Socket.IO protocol.

That library's latest release (**1.2.1**) officially targets Uptime Kuma
**≤ 1.23.2**. Current Uptime Kuma is **v2.x**. In practice the basic
monitor operations this bot uses (add / delete / rename / read status) work
against v2 for many people, but it is **not officially supported**, and a future
Kuma release could break it.

**So, before you rely on this in production, check your Kuma version**
(bottom of the Uptime Kuma settings page) and pick a path:

| Your Uptime Kuma | What to do |
| --- | --- |
| **1.x (≤ 1.23.2)** | Nothing — `requirements.txt` is already correct. |
| **2.x** | Try it as-is first. If add/login fails, use one of the fallbacks below. |

**Fallbacks if the pinned library doesn't work with your Kuma version**

1. **Install the library from git** (often tracks newer Kuma than the PyPI
   release). In `requirements.txt`, replace the `uptime-kuma-api==1.2.1` line with:
   ```
   uptime-kuma-api @ git+https://github.com/lucasheld/uptime-kuma-api.git
   ```
   then rebuild.
2. **Run a REST bridge** in front of Kuma, e.g.
   [Uptime-Kuma-Web-API](https://github.com/MedAziz11/Uptime-Kuma-Web-API), and
   re-point the bot at it. Everything that touches Kuma lives in **one file**,
   `kuma_client.py` — that's the only file you'd rewrite to use a different
   integration. The rest of the bot is unaffected.

---

## How it works

```
Slack  ──(Socket Mode)──►  app.py
                              ├── slack_app.py   commands, modal, buttons
                              ├── scheduler.py   status alerts + expiry
                              ├── database.py    SQLite: who/what/when/expiry
                              └── kuma_client.py the ONLY thing that calls Kuma
                                       │
                                       ▼
                               Uptime Kuma  (uptime.example.com)
```

- **Socket Mode** means the bot connects out to Slack — you don't need to expose
  a public URL or webhook.
- **SQLite** is the source of truth for each site's *type*, *expiry*, and which
  Slack channel "owns" it (because Kuma can't track any of that). It's a single
  file; back it up if you care about history.
- A background **scheduler** does three things:
  - **Status alerts** — every couple of minutes it reads each site's live status.
    The first *down* reading posts a DOWN alert (with Kuma's reason) to the owning
    channel; the first *up* reading after that resolves it, however the status
    wobbled in between. This per-channel routing is the reason the bot polls
    instead of using Kuma's own (global) Slack notifications.
  - **Expiry** — it warns ~24h before a client site expires (with **Extend** and
    **Keep permanently** buttons), then deletes expired client sites from both
    Kuma and the database and notifies the channel.
  - **Reconcile** — it drops sites whose monitor was deleted directly in Kuma.

---

## Prerequisites

- An Uptime Kuma instance and a login for it (see the 2FA note below).
- A Slack workspace where you can install an app.
- Either **Docker** (recommended) or **Python 3.10+**.

---

## 1. Set up the Slack app

Create the app from the manifest, or apply the manifest to an existing
**UptimeBot** app:

1. Go to <https://api.slack.com/apps> → **Create New App → From a manifest**
   (or open your existing app → **App Manifest**), paste the contents of
   [`manifest.yml`](./manifest.yml), and save.
2. **Install / Reinstall** the app to your workspace (OAuth & Permissions →
   *Install to Workspace*). Copy the **Bot User OAuth Token** — it starts with
   `xoxb-`. → this is `SLACK_BOT_TOKEN`. **Upgrading an existing install?**
   Re-apply the manifest and **reinstall** — see [Upgrading](#upgrading).
3. **Basic Information → App-Level Tokens → Generate Token and Scopes.** Add the
   **`connections:write`** scope, generate it, and copy the token — it starts
   with `xapp-`. → this is `SLACK_APP_TOKEN`. (This is what enables Socket Mode.)

That's it — no request URLs to configure, because Socket Mode handles the
connection.

## 2. Uptime Kuma credentials

Put your Kuma URL, username, and password in `.env` (next step).

**About 2FA:** the API logs in with username + password (+ a TOTP code if 2FA is
on). If your account uses 2FA, set `KUMA_MFA_SECRET` to the **TOTP secret** (the
string behind the QR code), *not* a 6-digit code — the bot generates the current
code at login. Recommended instead: create a **dedicated Kuma user** for the bot
**with 2FA disabled** on that account, so you're not putting a shared secret in
an env file.

## 3. Configure `.env`

```bash
cp .env.example .env
# then edit .env and fill in the required values:
#   SLACK_BOT_TOKEN, SLACK_APP_TOKEN, KUMA_URL, KUMA_USERNAME, KUMA_PASSWORD
```

Everything else has sensible defaults. See the comments in `.env.example`.

## 4. Run it

**With Docker (recommended):**

```bash
docker compose up -d --build
docker compose logs -f        # watch it connect
```

The database persists in a named Docker volume (`uptimebot-data`), so restarts
and rebuilds keep your sites.

**Bare metal:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# point the DB at a local file rather than /data:
echo 'DB_PATH=uptimebot.db' >> .env
python app.py
```

You should see `UptimeBot is starting (Socket Mode)…`. Then go to any channel and
type `/monitor example.com` — or DM the bot a link.

---

## Using the bot

- **Slash command (any channel or DM):** `/monitor example.com` adds a site (no
  `https://` needed); `/monitor` alone opens the **Add a site** form;
  `/monitor list` (etc.) runs any command. Adds and changes are announced in the
  channel; lookups like `list` and `status` are shown only to you.
- **In a channel the bot is in:** `@UptimeBot <command>` works too, and it answers
  in the thread if you ask in one.
- **In a DM:** just type a command — or simply **paste one or more links**.
- **Home tab:** open UptimeBot in Slack and click **Home** for an at-a-glance
  dashboard — counts of up/down, a **Needs attention** list of anything currently
  down (and for how long), every site grouped by type with the channel its alerts
  go to, and **Add a site** / **Refresh** buttons. (Status shown reflects the
  latest poll, every `STATUS_POLL_SECONDS`.)

  The Home tab is **personal**: it shows only the sites *you* added plus sites
  added in **channels you belong to** — not every site in the workspace.
  Likewise, `list` in a channel shows that channel's sites, and `list` in a DM
  shows your own. (This is why the bot needs the `channels:read` / `groups:read`
  scopes; see step 1.)

In a DM you can drop the `/monitor` prefix (e.g. just `list`).

| Command | What it does |
| --- | --- |
| `/monitor <url> [type] [name]` | Start monitoring a site. `https://` is optional; type is optional (`permanent`, `prod`, `staging`, `demo`, `client`) and may go before or after the URL. No type → **permanent**. |
| `/monitor <url> <url> … [type]` | Add up to 20 sites at once (names come from the domains). |
| `/monitor list [type]` | Show monitored sites, grouped by type, with live status emoji. |
| `/monitor status [name]` | Live status of one site (with Kuma's reason if it's down), or an overall summary. |
| `/monitor extend <name> [days]` | Give a client site more time (default = the standard window). |
| `/monitor set <name> <type>` | Re-classify a site, e.g. make a `client` site `permanent`, or a permanent one `client`. |
| `/monitor remove <name>` | Stop monitoring (asks you privately to confirm, then deletes from Kuma and tells the channel). |
| `/monitor notify <who>` | Who a channel's DOWN alerts ping: `@here`, `@channel`, people, user groups, or `off`. |
| `/monitor help` | Show the command list. |

**Examples**

```
/monitor app.acme.com
/monitor client demo.acme.com
/monitor staging stg.acme.com Acme Staging
/monitor api.acme.com docs.acme.com status.acme.com
/monitor notify @here
/monitor list client
/monitor status Acme Staging
/monitor extend demo.acme.com 14
/monitor set demo.acme.com permanent
/monitor remove demo.acme.com
```

### Retention rules

- **Default (no type), plus prod / staging / demo** → permanent. They stay until
  someone runs `remove`.
- **client** → temporary. Auto-removed after `CLIENT_TTL_DAYS` (default **7**),
  with a heads-up posted ~24h before that has **Extend** and **Keep permanently**
  buttons. You can also use `extend` to add time or `set <name> permanent`.

### Where alerts go

Each site has an owner: the channel it was added in (or the one picked in the
form), or a person. Up/down alerts and expiry notices go *there*, so a channel
only hears about the sites it cares about. Before adding a site, the bot checks
it can actually post there:

| Added in… | Alerts go to |
| --- | --- |
| A channel the bot is in | That channel. |
| A public channel it isn't in yet | That channel — the bot joins it automatically. |
| A private channel it isn't in | Nowhere yet: you're asked to `/invite @UptimeBot` first. Nothing is added. |
| A DM with the bot, the Home tab, or the form with no channel picked | Your DM with the bot. |
| A DM with someone else, or a group DM | Your DM with the bot (it can't post in those), and it tells you so. |

If the bot later loses access to a channel (removed from a private channel,
channel archived, posting restricted), alerts go to whoever added the site, with
a note explaining why, instead of being lost.

**Alert anatomy:** a DOWN alert includes Kuma's reason (e.g. `Request failed
with status code 503`) and pings whoever `/monitor notify` set for the channel.
When the site recovers, that same message changes to *✅ recovered — down for
12m*, and a recovery note is posted in its thread and also shown in the
channel. Recoveries never ping anyone. Times show in each reader's own timezone.

### Duplicate sites

Adding a site that's already monitored **by the same owner** (the same channel,
or your DM) won't create a second monitor — the bot tells you it's already
watching it and points you to `status` / `extend` / `remove`. URLs are matched
loosely, so `example.com`, `https://www.example.com`, and `https://example.com/`
all count as the same site. Different channels (or different people's DMs) can
still each monitor the same URL for their own alerts.

### Access control (optional)

Leave `ALLOWED_USER_IDS` / `ALLOWED_CHANNEL_IDS` empty to let anyone in your
workspace use the bot. To restrict it, set either to a comma-separated list of
Slack IDs (e.g. `ALLOWED_USER_IDS=U012ABC,U034DEF`). The limits apply everywhere:
commands, the form, shortcuts, the Home tab, and buttons. Buttons on a site
(Remove, Extend, Keep) work only for people in the channel that owns it, or
whoever added it.

---

## Upgrading

Pull the new code and rebuild (`docker compose up -d --build`). The database
updates itself on startup; sites that were already down won't alert a second time.

Then, in your Slack app, **re-apply [`manifest.yml`](./manifest.yml) and
reinstall the app** (OAuth & Permissions → *Reinstall to Workspace*). That
enables the parts that need new permissions:

- the `channels:join` scope (auto-joining public channels),
- the `member_joined_channel` event (the welcome message when invited),
- the two shortcuts, and escaped `/monitor` text (so `notify @someone` can ping them).

Until you reinstall, everything else keeps working: adding sites in public
channels still works through `chat:write.public`.

---

## Troubleshooting

- **`Couldn't connect to Uptime Kuma` / login fails** — almost always the version
  mismatch described at the top. Check your Kuma version and try a fallback
  (git-install the library, or run a REST bridge). Also re-check
  `KUMA_USERNAME` / `KUMA_PASSWORD` and `KUMA_MFA_SECRET` if 2FA is on.
- **Bot doesn't respond in a channel** — use `/monitor …`, which works
  everywhere. For `@UptimeBot …`, make sure it's invited (`/invite @UptimeBot`).
- **"I'm not in that channel"** — it's a private channel; `/invite @UptimeBot`
  there, then run the command again.
- **No welcome message / shortcuts missing / `notify @someone` doesn't work** —
  re-apply the manifest and reinstall (see [Upgrading](#upgrading)).
- **DMs do nothing** — confirm the app was (re)installed after applying the
  manifest, so the `message.im` event and `im:*` scopes are active.
- **`not_authed` / `invalid_auth`** — the tokens are wrong or swapped.
  `SLACK_BOT_TOKEN` is the `xoxb-` one; `SLACK_APP_TOKEN` is the `xapp-` one with
  `connections:write`.
- **Slash command says "dispatch_failed"** — the bot process isn't running or
  lost its Socket Mode connection; check `docker compose logs -f`.

## Maintenance

### Start fresh (wipe everything)

To clear **all** tracked sites for everyone and start clean, use `reset.py`. It
deletes the monitors the bot created from Uptime Kuma **and** clears the bot's
database. It's destructive and irreversible, so it runs as a **dry run** unless
you pass `--yes`.

```bash
# Bare metal (with your .env in place):
python reset.py                 # dry run — shows what would be deleted
python reset.py --yes           # delete tracked Kuma monitors, then clear the DB
python reset.py --yes --keep-kuma   # clear the DB only, leave Kuma monitors

# Docker (run inside the service so it sees the same env + volume):
docker compose run --rm uptimebot python reset.py --yes
```

Open Home tabs refresh on next open or **Refresh**. (Prefer `remove <name>` for
deleting a single site — `reset.py` is the nuclear option.)

### Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Tests cover URL/type parsing, the database (duplicate detection, owner scoping,
upgrades), command routing, adding from every kind of conversation, the add form,
shortcuts, button permissions, and the full outage lifecycle (alert, fallback,
threaded recovery, expiry) — using in-memory fakes, so no Slack or Kuma
connection is required.

## Security notes

- `.env` holds Slack tokens and Kuma credentials — never commit it (a
  `.gitignore` is included). Prefer a dedicated, least-privilege Kuma account.
- Anyone allowed to talk to the bot can add/remove monitors. Use the access
  controls above for sensitive workspaces.

## Limitations

- Monitors are created as simple **HTTP(S)** checks. Fancier monitor types
  (TCP, keyword, push, etc.) and per-monitor notification settings are managed
  in the Uptime Kuma UI.
- The bot's database is the source of truth for type/expiry. If you delete a
  monitor directly in Kuma, a periodic **reconcile** job (every
  `RECONCILE_POLL_SECONDS`, default 6h, plus once shortly after startup) notices
  the monitor is gone, drops the stale entry, and tells the owning channel — so
  you don't have to run `remove` yourself.
- Status alerting is poll-based (default every 120s), so notifications can lag a
  live outage by up to that interval.
- Each channel that adds the same URL gets its own Kuma monitor, so the same
  site is checked once per owner.

---

## Project layout

```
app.py            entry point: init DB, register handlers, start scheduler, run
config.py         loads settings from environment / .env
slack_app.py      Slack commands, the add form, shortcuts, welcome, and buttons
scheduler.py      outage alerts, client-site expiry, and reconcile jobs
database.py       SQLite storage (sites, outages, per-channel settings)
kuma_client.py    the single place that talks to Uptime Kuma  ← swap for v2/REST
utils.py          small shared helpers (URL cleanup, formatting)
reset.py          wipe all tracked sites (dry run unless --yes)
tests/            pytest suite with in-memory Slack/Kuma fakes
manifest.yml      Slack app manifest
Dockerfile / docker-compose.yml
.env.example      copy to .env and fill in
```
