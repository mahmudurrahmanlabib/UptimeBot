# UptimeBot

A simple Slack bot that lets your support and product teams add and manage
[Uptime Kuma](https://github.com/louislam/uptime-kuma) monitors by chatting in
Slack — in a channel or in a DM. It also handles **retention**: sites are kept
until you remove them, while **client sites are auto-removed after a week**.

```
You:        /monitor app.acme.com
UptimeBot:  ✅ Now monitoring app.acme.com — type permanent
            ♾️ Kept permanently — stays until you remove it.

You:        /monitor client demo.acme.com
UptimeBot:  ✅ Now monitoring demo.acme.com — type client
            ⏳ As a client site it'll be auto-removed in 7d.
```

When a monitored site goes down (or recovers), UptimeBot posts an alert to the
channel where that site was added.

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
- A background **scheduler** does two things:
  - **Status alerts** — every couple of minutes it reads each site's live status
    and, when a site flips between up and down, posts to the owning channel. This
    per-channel routing is the reason the bot polls instead of using Kuma's own
    (global) Slack notifications.
  - **Expiry** — it warns ~24h before a client site expires, then deletes expired
    client sites from both Kuma and the database and notifies the channel.

---

## Prerequisites

- An Uptime Kuma instance and a login for it (see the 2FA note below).
- A Slack workspace where you can install an app.
- Either **Docker** (recommended) or **Python 3.10+**.

---

## 1. Set up the Slack app

You said the **UptimeBot** app already exists — you can either apply the manifest
to it or recreate it from the manifest. Either way:

1. Go to <https://api.slack.com/apps> → your **UptimeBot** app
   → **App Manifest**, paste the contents of [`manifest.yml`](./manifest.yml),
   and save. (Creating a new app from a manifest works too.)
2. **Install / Reinstall** the app to your workspace (OAuth & Permissions →
   *Install to Workspace*). Copy the **Bot User OAuth Token** — it starts with
   `xoxb-`. → this is `SLACK_BOT_TOKEN`.
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
# then edit .env and fill in the four required values:
#   SLACK_BOT_TOKEN, SLACK_APP_TOKEN, KUMA_USERNAME, KUMA_PASSWORD
# KUMA_URL is already set to https://uptime.example.com
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

You should see `UptimeBot is starting (Socket Mode)…`. Invite the bot to a
channel (`/invite @UptimeBot`) or just DM it.

---

## Using the bot

- **In a channel:** `@UptimeBot <command>` — replies and alerts go to that channel.
- **In a DM:** just type a command — or simply **paste a link** and it gets monitored.
- **Slash command:** `/monitor example.com` adds a site (no `https://` needed);
  `/monitor` alone opens an "Add a site" form; `/monitor list` (etc.) runs any command.
- **Home tab:** open UptimeBot in Slack and click **Home** for an at-a-glance
  dashboard — counts of up/down, every site grouped by type with status dots, and
  **Add a site** / **Refresh** buttons. Sites added from Home send their alerts to
  your DM. (Status shown reflects the latest poll, every `STATUS_POLL_SECONDS`.)

| Command | What it does |
| --- | --- |
| `/monitor <url> [type] [name]` | Start monitoring a site. `https://` is optional; type is optional (`permanent`, `prod`, `staging`, `demo`, `client`) and may go before or after the URL. No type → **permanent**. |
| `list [type]` | Show monitored sites, grouped by type, with live status emoji. |
| `status [name]` | Live status of one site, or an overall up/down summary. |
| `extend <name> [days]` | Give a client site more time (default = the standard window). |
| `set <name> <type>` | Re-classify a site, e.g. make a `client` site `permanent`, or a permanent one `client`. |
| `remove <name>` | Stop monitoring (asks for confirmation, then deletes from Kuma). |
| `help` | Show the command list. |

**Examples**

```
/monitor app.acme.com
/monitor client demo.acme.com
/monitor staging stg.acme.com Acme Staging
list client
status Acme Staging
extend demo.acme.com 14
set demo.acme.com permanent
remove demo.acme.com
```

### Retention rules

- **Default (no type), plus prod / staging / demo** → permanent. They stay until
  someone runs `remove`.
- **client** → temporary. Auto-removed after `CLIENT_TTL_DAYS` (default **7**),
  with a heads-up posted ~24h before. Use `extend` to add time or
  `set <name> permanent` to keep it for good.

### Where alerts go

Each site remembers the channel (or DM) it was added in. Up/down alerts and
expiry notices for that site go *there* — so a channel only hears about the sites
it cares about.

### Access control (optional)

Leave `ALLOWED_USER_IDS` / `ALLOWED_CHANNEL_IDS` empty to let anyone in your
workspace use the bot. To restrict it, set either to a comma-separated list of
Slack IDs (e.g. `ALLOWED_USER_IDS=U012ABC,U034DEF`).

---

## Troubleshooting

- **`Couldn't connect to Uptime Kuma` / login fails** — almost always the version
  mismatch described at the top. Check your Kuma version and try a fallback
  (git-install the library, or run a REST bridge). Also re-check
  `KUMA_USERNAME` / `KUMA_PASSWORD` and `KUMA_MFA_SECRET` if 2FA is on.
- **Bot doesn't respond in a channel** — make sure it's invited
  (`/invite @UptimeBot`) and that you actually @mentioned it.
- **DMs do nothing** — confirm the app was (re)installed after applying the
  manifest, so the `message.im` event and `im:*` scopes are active.
- **`not_authed` / `invalid_auth`** — the tokens are wrong or swapped.
  `SLACK_BOT_TOKEN` is the `xoxb-` one; `SLACK_APP_TOKEN` is the `xapp-` one with
  `connections:write`.
- **Slash command says "dispatch_failed"** — the bot process isn't running or
  lost its Socket Mode connection; check `docker compose logs -f`.

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
  monitor directly in Kuma, run `remove` (or `list`) so the bot stays in sync.
- Status alerting is poll-based (default every 120s), so notifications can lag a
  live outage by up to that interval.

---

## Project layout

```
app.py            entry point: init DB, register handlers, start scheduler, run
config.py         loads settings from environment / .env
slack_app.py      Slack commands, the add-site modal, and buttons
scheduler.py      status-change alerts + client-site expiry jobs
database.py       SQLite storage (site type, expiry, owning channel)
kuma_client.py    the single place that talks to Uptime Kuma  ← swap for v2/REST
utils.py          small shared helpers (URL cleanup, formatting)
manifest.yml      Slack app manifest
Dockerfile / docker-compose.yml
.env.example      copy to .env and fill in
```
