"""The scheduler's outage lifecycle, delivery fallbacks, expiry, and reconcile."""
import time

import scheduler
import slack_app


def _add(db, kuma, **over):
    """Track a site whose monitor exists in the fake Kuma; returns its row id."""
    mid = kuma.add_monitor("PERMANENT | acme", over.get("url", "https://acme.com"))
    base = dict(monitor_id=mid, name="acme", url="https://acme.com", site_type="permanent",
                added_by="U_A", added_by_name="Alice", channel_id="C1", is_dm=False,
                expires_at=None)
    base.update(over)
    return db.add_site(**base)


def _down(kuma, rid, db, msg="Request failed with status code 503"):
    kuma.statuses[db.get_by_rowid(rid)["monitor_id"]] = {"status": "down", "ping": None, "msg": msg}


def _set(kuma, rid, db, status):
    kuma.statuses[db.get_by_rowid(rid)["monitor_id"]] = {"status": status, "ping": 42, "msg": ""}


class TestOutageLifecycle:
    def test_down_alert_posts_once_with_reason_and_mention(self, db, kuma, slack):
        rid = _add(db, kuma)
        db.set_mention("C1", "<!here>", "U_A")
        _down(kuma, rid, db, msg="boom & <bang>")

        scheduler._status_job(slack)
        scheduler._status_job(slack)  # still down: no repeat

        posts = slack.calls_to("chat_postMessage")
        assert len(posts) == 1
        assert posts[0]["channel"] == "C1"
        assert posts[0]["text"].startswith("<!here> 🔴 acme is DOWN")
        body = posts[0]["blocks"][0]["text"]["text"]
        assert "boom &amp; &lt;bang&gt;" in body
        s = db.get_by_rowid(rid)
        assert s["down_since"] and s["alert_channel"] == "C1" and s["alert_ts"] == "1.000"

    def test_recovery_survives_pending_and_threads_under_the_alert(self, db, kuma, slack):
        rid = _add(db, kuma)
        _down(kuma, rid, db)
        scheduler._status_job(slack)
        # A wobble through pending/maintenance between polls must not lose the recovery.
        for status in ("pending", "maintenance", "up"):
            _set(kuma, rid, db, status)
            scheduler._status_job(slack)

        update = slack.calls_to("chat_update")[0]
        assert update["ts"] == "1.000" and "recovered" in update["text"]
        reply = slack.calls_to("chat_postMessage")[-1]
        assert reply["thread_ts"] == "1.000" and reply["reply_broadcast"] is True
        assert "back up after" in reply["text"]
        s = db.get_by_rowid(rid)
        assert s["down_since"] is None and s["last_status"] == "up"

    def test_status_command_does_not_swallow_the_alert(self, db, kuma, slack):
        rid = _add(db, kuma)
        _down(kuma, rid, db)
        ctx = slack_app.make_ctx(slack, "U_A", "C1", False)
        assert "DOWN" in slack_app.cmd_status(["acme"], ctx)["text"]

        scheduler._status_job(slack)
        assert len(slack.calls_to("chat_postMessage")) == 1

    def test_unreachable_channel_falls_back_to_the_adder(self, db, kuma, slack):
        rid = _add(db, kuma)
        slack.post_errors["C1"] = "not_in_channel"
        _down(kuma, rid, db)

        scheduler._status_job(slack)

        dm = slack.calls_to("chat_postMessage")[-1]
        assert dm["channel"] == "U_A"
        assert "couldn't post this in <#C1>" in dm["blocks"][-1]["elements"][0]["text"]
        assert db.get_by_rowid(rid)["alert_channel"] == "D_A"

    def test_failed_post_is_retried_next_poll(self, db, kuma, slack):
        rid = _add(db, kuma)
        slack.post_errors["C1"] = "ratelimited"  # transient; not a reason to DM instead
        _down(kuma, rid, db)
        scheduler._status_job(slack)
        assert db.get_by_rowid(rid)["down_since"] is None

        del slack.post_errors["C1"]
        scheduler._status_job(slack)
        assert db.get_by_rowid(rid)["down_since"] is not None

    def test_personal_site_alerts_its_owner_without_mentions(self, db, kuma, slack):
        rid = _add(db, kuma, channel_id="U_A", is_dm=True)
        db.set_mention("U_A", "<!channel>", "U_A")  # shouldn't apply to DMs
        _down(kuma, rid, db)
        scheduler._status_job(slack)
        post = slack.calls_to("chat_postMessage")[0]
        assert post["channel"] == "U_A" and not post["text"].startswith("<!")


class TestExpiry:
    def test_warning_offers_one_click_fixes(self, db, kuma, slack):
        rid = _add(db, kuma, site_type="client", expires_at=time.time() + 3600)
        scheduler._expiry_job(slack)
        post = slack.calls_to("chat_postMessage")[0]
        buttons = post["blocks"][1]["elements"]
        assert {b["action_id"] for b in buttons} == {"expiry_extend", "expiry_keep"}
        assert {b["value"] for b in buttons} == {str(rid)}
        assert db.get_by_rowid(rid)["warned_expiry"] == 1

    def test_expired_site_is_removed_everywhere(self, db, kuma, slack):
        rid = _add(db, kuma, site_type="client", expires_at=time.time() - 1)
        mid = db.get_by_rowid(rid)["monitor_id"]
        scheduler._expiry_job(slack)
        assert db.get_by_rowid(rid) is None and mid not in kuma.monitors
        assert "/monitor client acme.com" in slack.calls_to("chat_postMessage")[0]["blocks"][0]["text"]["text"]


class TestReconcile:
    def test_drops_sites_deleted_in_kuma(self, db, kuma, slack):
        rid = _add(db, kuma)
        kuma.monitors.clear()
        # Pretend it was added a while ago, past the grace window.
        with db._conn() as con:
            con.execute("UPDATE sites SET created_at = 0 WHERE id = ?", (rid,))
        scheduler._reconcile_job(slack)
        assert db.get_by_rowid(rid) is None
        assert "deleted in Uptime Kuma" in slack.calls_to("chat_postMessage")[0]["blocks"][0]["text"]["text"]


class TestKumaClient:
    def test_reuses_session_and_reads_only_recent_beats(self, monkeypatch):
        import kuma_client

        class FakeApi:
            def __init__(self, url):
                self.calls = []
                apis.append(self)

            def login(self, username, password, token=""):
                self.calls.append("login")
                return {"token": "jwt-1"}

            def login_by_token(self, token):
                self.calls.append("token")
                if token != "jwt-1":
                    raise RuntimeError("Invalid token")

            def get_monitor_beats(self, monitor_id, hours):
                self.calls.append(("beats", hours))
                # Deliberately out of order: the newest beat must still win.
                return [{"status": 1, "time": "2026-01-01 10:02:00", "id": 3, "ping": 9, "msg": "ok"},
                        {"status": 0, "time": "2026-01-01 10:01:00", "id": 2, "msg": "boom"}]

            def disconnect(self):
                pass

        apis = []
        monkeypatch.setattr(kuma_client, "UptimeKumaApi", FakeApi)
        monkeypatch.setattr(kuma_client, "_session_token", None)

        with kuma_client.KumaClient() as kc:
            status = kc.get_status(7)
        with kuma_client.KumaClient():
            pass
        assert apis[0].calls[0] == "login" and apis[1].calls == ["token"]
        assert status == {"status": "up", "ping": 9, "msg": "ok"}
        assert apis[0].calls[1] == ("beats", 1)  # an hour, not a day, per site per poll

        monkeypatch.setattr(kuma_client, "_session_token", "stale")
        with kuma_client.KumaClient():
            pass
        assert apis[2].calls == ["token", "login"]
        assert kuma_client._session_token == "jwt-1"

    def test_2fa_account_without_secret_fails_clearly(self, monkeypatch):
        import pytest

        import kuma_client

        class TwoFactorApi:
            def __init__(self, url):
                pass

            def login(self, username, password, token=""):
                return {"tokenRequired": True}  # what Kuma sends: no "ok": false

            def disconnect(self):
                pass

        monkeypatch.setattr(kuma_client, "UptimeKumaApi", TwoFactorApi)
        monkeypatch.setattr(kuma_client, "_session_token", None)
        with pytest.raises(kuma_client.KumaError, match="KUMA_MFA_SECRET"):
            with kuma_client.KumaClient():
                pass
