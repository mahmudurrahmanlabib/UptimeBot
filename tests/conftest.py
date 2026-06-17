"""Shared test setup.

`config` reads required environment variables at import time, so we seed dummy
values here (before any app module is imported) to let it load without a real
`.env`. The `db` fixture points the database at a throwaway file per test.
"""
import os

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("KUMA_URL", "https://kuma.test")
os.environ.setdefault("KUMA_USERNAME", "test-user")
os.environ.setdefault("KUMA_PASSWORD", "test-pass")
os.environ.setdefault("DB_PATH", "uptimebot-test.db")

import pytest


@pytest.fixture
def db(tmp_path):
    """A fresh, isolated database for one test."""
    import config
    import database as db_mod
    config.DB_PATH = str(tmp_path / "test.db")
    db_mod.init_db()
    return db_mod
