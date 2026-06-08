"""UptimeBot entry point.

Initialises the database, registers the Slack handlers, starts the background
scheduler, and runs the bot in Socket Mode (no public URL required).
"""
import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config
import database as db
import scheduler
import slack_app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("uptimebot")

    db.init_db()
    log.info("database ready at %s", config.DB_PATH)

    app = App(token=config.SLACK_BOT_TOKEN)
    slack_app.register(app)

    sched = scheduler.start(app.client)

    handler = SocketModeHandler(app, config.SLACK_APP_TOKEN)
    log.info("UptimeBot is starting (Socket Mode)…")
    try:
        handler.start()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting down…")
        try:
            sched.shutdown(wait=False)
        except Exception:
            pass


if __name__ == "__main__":
    main()
