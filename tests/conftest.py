"""Shared test setup.

`config` reads required environment variables at import time, so we seed dummy
values here (before any app module is imported) to let it load without a real
`.env`. The `db` fixture points the database at a throwaway file per test, and
the fakes below stand in for Slack, Uptime Kuma, and the Bolt app.
"""
import os

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("KUMA_URL", "https://kuma.test")
os.environ.setdefault("KUMA_USERNAME", "test-user")
os.environ.setdefault("KUMA_PASSWORD", "test-pass")
os.environ.setdefault("DB_PATH", "uptimebot-test.db")

import pytest
from slack_sdk.errors import SlackApiError

BOT_USER_ID = "U_BOT"


@pytest.fixture
def db(tmp_path):
    """A fresh, isolated database for one test."""
    import config
    import database as db_mod
    config.DB_PATH = str(tmp_path / "test.db")
    db_mod.init_db()
    return db_mod


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Module-level caches would otherwise leak between tests."""
    import slack_app
    slack_app._name_cache.clear()
    slack_app._member_cache.clear()
    slack_app._quiet_joins.clear()
    yield


class FakeSlack:
    """Records Slack Web API calls. `channels` maps a channel id to its
    conversations.info payload, or to an error code string; unknown channels
    are `channel_not_found`, like a private channel the bot isn't in.
    `post_errors` makes chat.postMessage to a channel fail with that error."""

    def __init__(self):
        self.calls = []
        self.channels = {}
        self.post_errors = {}
        self._ts = 0

    def calls_to(self, method):
        return [kw for m, kw in self.calls if m == method]

    def chat_postMessage(self, channel, **kw):
        self.calls.append(("chat_postMessage", dict(channel=channel, **kw)))
        if channel in self.post_errors:
            raise SlackApiError("error", {"ok": False, "error": self.post_errors[channel]})
        self._ts += 1
        # Posting to a user id lands in the bot's DM with them (a D… channel).
        real = "D" + channel[1:] if channel.startswith("U") else channel
        return {"ok": True, "channel": real, "ts": f"{self._ts}.000"}

    def chat_update(self, **kw):
        self.calls.append(("chat_update", kw))
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.calls.append(("chat_postEphemeral", kw))
        return {"ok": True}

    def conversations_info(self, channel):
        self.calls.append(("conversations_info", {"channel": channel}))
        info = self.channels.get(channel, "channel_not_found")
        if isinstance(info, str):
            raise SlackApiError("error", {"ok": False, "error": info})
        return {"ok": True, "channel": info}

    def conversations_join(self, channel):
        self.calls.append(("conversations_join", {"channel": channel}))
        return {"ok": True}

    def conversations_members(self, channel, cursor=None, limit=None):
        return {"members": []}

    def users_info(self, user):
        return {"user": {"profile": {"display_name": "Alice"}}}

    def views_open(self, **kw):
        self.calls.append(("views_open", kw))

    def views_publish(self, **kw):
        self.calls.append(("views_publish", kw))


class FakeKuma:
    """Stands in for KumaClient: in-memory monitors with settable statuses.
    `KumaClient()` returns this same object, and `sessions` counts the
    `with` blocks (i.e. Kuma logins)."""

    def __init__(self):
        self.monitors = {}
        self.statuses = {}
        self.next_id = 1
        self.fail_add = False
        self.sessions = 0

    def __call__(self):
        return self

    def __enter__(self):
        self.sessions += 1
        return self

    def __exit__(self, *exc):
        return False

    def add_monitor(self, name, url):
        from kuma_client import KumaError
        if self.fail_add:
            raise KumaError("Kuma said no")
        mid = self.next_id
        self.next_id += 1
        self.monitors[mid] = {"name": name, "url": url}
        return mid

    def delete_monitor(self, monitor_id):
        self.monitors.pop(int(monitor_id), None)

    def rename_monitor(self, monitor_id, new_name):
        self.monitors[int(monitor_id)]["name"] = new_name

    def get_status(self, monitor_id):
        return self.statuses.get(monitor_id, {"status": "pending", "ping": None, "msg": ""})

    def list_monitor_ids(self):
        return set(self.monitors)


class FakeApp:
    """Captures what slack_app.register() wires up so tests can call handlers."""

    def __init__(self):
        self.handlers = {}

    def _capture(self, kind, key):
        def deco(fn):
            self.handlers[(kind, key)] = fn
            return fn
        return deco

    def event(self, name):
        return self._capture("event", name)

    def action(self, name):
        return self._capture("action", name)

    def command(self, name):
        return self._capture("command", name)

    def shortcut(self, name):
        return self._capture("shortcut", name)

    def view(self, name):
        return self._capture("view", name)


class Recorder:
    """A stand-in for Bolt's ack / respond / say that remembers its calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    @property
    def kwargs(self):
        return self.calls[-1][1]

    @property
    def text(self):
        args, kwargs = self.calls[-1]
        return kwargs.get("text") or (args[0] if args else "")


@pytest.fixture
def slack():
    return FakeSlack()


@pytest.fixture
def kuma(monkeypatch):
    import scheduler
    import slack_app
    fake = FakeKuma()
    monkeypatch.setattr(slack_app, "KumaClient", fake)
    monkeypatch.setattr(scheduler, "KumaClient", fake)
    return fake


@pytest.fixture
def handlers():
    import slack_app
    app = FakeApp()
    slack_app.register(app)
    return app.handlers


@pytest.fixture
def context():
    return {"bot_user_id": BOT_USER_ID}
