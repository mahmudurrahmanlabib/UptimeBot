"""Using the bot in any conversation: slash commands, the welcome message, the
add form, shortcuts, mentions, and who may press which buttons."""
import time

import config
import slack_app
from conftest import BOT_USER_ID, Recorder

PUBLIC = {"id": "C_PUB", "is_channel": True, "is_private": False, "is_member": False}
JOINED = {"id": "C1", "is_channel": True, "is_private": False, "is_member": True}


def _slash(handlers, slack, text, channel_id="C1", channel_name="general", user_id="U_A"):
    respond = Recorder()
    body = {"user_id": user_id, "channel_id": channel_id, "channel_name": channel_name,
            "text": text, "trigger_id": "trig"}
    handlers[("command", "/monitor")](ack=Recorder(), body=body, client=slack, respond=respond)
    return respond


def _modal_submission(url, channel_id=None, user_id="U_A", site_type="permanent", name=""):
    values = {
        "url": {"val": {"value": url}},
        "type": {"val": {"selected_option": {"value": site_type}}},
        "channel": {"val": {"selected_conversation": channel_id}},
        "name": {"val": {"value": name or None}},
    }
    view = {"state": {"values": values}, "private_metadata": "{}"}
    return {"user": {"id": user_id}}, view


def _click(action_id, value, channel_id, user_id="U_A", ephemeral=False):
    return {"user": {"id": user_id}, "trigger_id": "trig",
            "channel": {"id": channel_id},
            "container": {"channel_id": channel_id, "is_ephemeral": ephemeral},
            "actions": [{"action_id": action_id, "value": str(value)}]}


class TestSlashInAnyChannel:
    def test_public_channel_joins_and_announces_the_add(self, db, kuma, slack, handlers):
        slack.channels["C_PUB"] = PUBLIC
        respond = _slash(handlers, slack, "<http://acme.com|acme.com>", channel_id="C_PUB")

        assert respond.kwargs["response_type"] == "in_channel"
        assert slack.calls_to("conversations_join") == [{"channel": "C_PUB"}]
        site = db.all_sites()[0]
        assert site["channel_id"] == "C_PUB" and site["url"] == "https://acme.com"
        assert "C_PUB" in slack_app._quiet_joins  # no welcome on top of the confirmation

    def test_private_channel_without_the_bot_is_refused_with_a_fix(self, db, kuma, slack, handlers):
        respond = _slash(handlers, slack, "acme.com", channel_id="C_PRIV",
                         channel_name="privategroup")

        assert "/invite @UptimeBot" in respond.kwargs["text"]
        assert respond.kwargs["response_type"] == "ephemeral"
        assert db.all_sites() == [] and kuma.monitors == {}

    def test_group_dm_site_becomes_personal(self, db, kuma, slack, handlers):
        respond = _slash(handlers, slack, "acme.com", channel_id="G_MPIM",
                         channel_name="mpdm-alice--bob-1")

        site = db.all_sites()[0]
        assert site["channel_id"] == "U_A" and site["is_dm"] == 1
        assert "group DMs" in str(respond.kwargs["blocks"])
        assert respond.kwargs["response_type"] == "ephemeral"

    def test_dm_with_someone_else_alerts_the_adder(self, db, kuma, slack, handlers):
        _slash(handlers, slack, "acme.com", channel_id="D_OTHER", channel_name="directmessage")
        assert db.all_sites()[0]["channel_id"] == "U_A"

    def test_lookups_stay_private(self, db, kuma, slack, handlers):
        slack.channels["C1"] = JOINED
        _slash(handlers, slack, "acme.com")
        respond = _slash(handlers, slack, "list")
        assert respond.kwargs["response_type"] == "ephemeral"
        assert "acme.com" in str(respond.kwargs["blocks"])

    def test_bare_command_opens_the_form_preset_to_this_channel(self, slack, handlers):
        _slash(handlers, slack, "")
        view = slack.calls_to("views_open")[0]["view"]
        picker = view["blocks"][2]["element"]
        assert picker["type"] == "conversations_select"
        assert picker["initial_conversation"] == "C1"

    def test_bare_command_in_a_dm_defaults_to_the_dm(self, slack, handlers):
        _slash(handlers, slack, "", channel_id="D1", channel_name="directmessage")
        picker = slack.calls_to("views_open")[0]["view"]["blocks"][2]["element"]
        assert "initial_conversation" not in picker

    def test_access_control(self, db, kuma, slack, handlers, monkeypatch):
        monkeypatch.setattr(config, "ALLOWED_USER_IDS", {"U_OTHER"})
        respond = _slash(handlers, slack, "acme.com")
        assert "⛔" in respond.text and db.all_sites() == []


class TestBulkAdd:
    def test_adds_several_in_one_kuma_session(self, db, kuma, slack):
        slack.channels["C1"] = JOINED
        ctx = slack_app.make_ctx(slack, "U_A", "C1", False)
        slack_app.cmd_add(["a.com"], ctx)
        kuma.sessions = 0

        resp = slack_app.cmd_add(["client", "a.com", "b.com", "<http://c.com|c.com>"], ctx)

        assert kuma.sessions == 1
        assert "Now monitoring *2 sites* as *client*" in resp["text"]
        assert "Already monitoring" in resp["text"]
        assert sorted(s["url"] for s in db.all_sites()) == \
            ["https://a.com", "https://b.com", "https://c.com"]
        assert resp["_share"] is True


class TestAddFailures:
    def test_kuma_failure_records_nothing(self, db, kuma, slack):
        slack.channels["C1"] = JOINED
        kuma.fail_add = True
        resp = slack_app.cmd_add(["acme.com"], slack_app.make_ctx(slack, "U_A", "C1", False))
        assert "Couldn't add" in resp["text"] and not resp.get("_share")
        assert db.all_sites() == []

    def test_db_failure_rolls_back_the_monitor(self, db, kuma, slack, monkeypatch):
        slack.channels["C1"] = JOINED

        def broken(**kw):
            raise RuntimeError("disk full")
        monkeypatch.setattr(slack_app.db, "add_site", broken)
        resp = slack_app.cmd_add(["acme.com"], slack_app.make_ctx(slack, "U_A", "C1", False))
        assert "Couldn't save" in resp["text"]
        assert kuma.monitors == {}


class TestCommands:
    def test_set_client_keeps_an_extended_expiry(self, db, kuma, slack):
        far = time.time() + 30 * 86400
        db.add_site(monitor_id=kuma.add_monitor("x", "https://a.com"), name="a", url="https://a.com",
                    site_type="client", added_by="U_A", added_by_name="Alice", channel_id="C1",
                    is_dm=False, expires_at=far)
        slack_app.cmd_set(["a", "client"], slack_app.make_ctx(slack, "U_A", "C1", False))
        assert db.all_sites()[0]["expires_at"] == far

    def test_notify(self, db, slack):
        ctx = slack_app.make_ctx(slack, "U_A", "C1", False)
        resp = slack_app.cmd_notify(["<!here>"], ctx)
        assert db.get_mention("C1") == "<!here>"
        assert resp["_private"] and "<!here>" not in resp["text"]  # confirming mustn't ping
        assert "nobody" not in slack_app.cmd_notify([], ctx)["text"]
        slack_app.cmd_notify(["off"], ctx)
        assert db.get_mention("C1") == ""
        assert "couldn't tell" in slack_app.cmd_notify(["@bob"], ctx)["text"]

    def test_notify_is_for_channels(self, db, slack):
        resp = slack_app.cmd_notify(["@here"], slack_app.make_ctx(slack, "U_A", "D1", True))
        assert "In a DM" in resp["text"] and db.get_mention("D1") == ""

    def test_greeting_then_command(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(slack_app, "cmd_list", lambda args, ctx: seen.setdefault("args", args))
        slack_app.handle_command("hey list client", {"is_dm": True})
        assert seen["args"] == ["client"]

    def test_long_lists_stay_within_slack_limits(self, db, slack):
        for i in range(120):
            db.add_site(monitor_id=i, name=f"site-{i}-" + "x" * 60, url=f"https://s{i}.example.com/" + "p" * 80,
                        site_type="permanent", added_by="U_A", added_by_name="Alice",
                        channel_id="C1", is_dm=False, expires_at=None)
        resp = slack_app.cmd_list([], slack_app.make_ctx(slack, "U_A", "C1", False))
        assert len(resp["blocks"]) <= 50
        assert all(len(b["text"]["text"]) <= 3000 for b in resp["blocks"] if b["type"] == "section")

        home = slack_app.build_home_view(db.all_sites())
        assert len(home["blocks"]) <= 100
        assert all(len(b["text"]["text"]) <= 3000 for b in home["blocks"]
                   if b["type"] == "section" and "text" in b)
        assert "<#C1>" in str(home["blocks"])


class TestWelcome:
    def test_bot_joining_posts_a_welcome_with_an_add_button(self, db, slack, handlers, context):
        handlers[("event", "member_joined_channel")](
            event={"user": BOT_USER_ID, "channel": "C9"}, client=slack, context=context)
        post = slack.calls_to("chat_postMessage")[0]
        assert post["channel"] == "C9" and "/monitor example.com" in post["text"]
        button = post["blocks"][2]["elements"][0]
        assert (button["action_id"], button["value"]) == ("channel_add", "C9")

    def test_other_people_joining_is_ignored(self, db, slack, handlers, context):
        handlers[("event", "member_joined_channel")](
            event={"user": "U_A", "channel": "C9"}, client=slack, context=context)
        assert slack.calls_to("chat_postMessage") == []

    def test_quiet_after_joining_by_itself(self, db, slack, handlers, context):
        slack_app._quiet_joins["C9"] = time.time()
        handlers[("event", "member_joined_channel")](
            event={"user": BOT_USER_ID, "channel": "C9"}, client=slack, context=context)
        assert slack.calls_to("chat_postMessage") == [] and "C9" not in slack_app._quiet_joins

    def test_stale_quiet_join_still_welcomes(self, db, slack, handlers, context):
        slack_app._quiet_joins["C9"] = time.time() - 3600  # its join event never came
        handlers[("event", "member_joined_channel")](
            event={"user": BOT_USER_ID, "channel": "C9"}, client=slack, context=context)
        assert slack.calls_to("chat_postMessage")[0]["channel"] == "C9"

    def test_welcome_button_opens_the_form_for_that_channel(self, slack, handlers):
        handlers[("action", "channel_add")](ack=Recorder(), body=_click("channel_add", "C9", "C9"),
                                            client=slack, respond=Recorder())
        picker = slack.calls_to("views_open")[0]["view"]["blocks"][2]["element"]
        assert picker["initial_conversation"] == "C9"


class TestAddForm:
    def _submit(self, handlers, slack, *args, **kwargs):
        ack = Recorder()
        body, view = _modal_submission(*args, **kwargs)
        handlers[("view", "add_site_modal")](ack=ack, body=body, client=slack, view=view)
        return ack

    def test_adds_to_the_picked_channel(self, db, kuma, slack, handlers):
        slack.channels["C1"] = JOINED
        ack = self._submit(handlers, slack, "acme.com", "C1", name="Acme &amp; Co")
        assert ack.calls[0] == ((), {})
        site = db.all_sites()[0]
        assert (site["channel_id"], site["name"]) == ("C1", "Acme & Co")
        assert slack.calls_to("chat_postMessage")[0]["channel"] == "C1"

    def test_no_channel_means_the_users_dm(self, db, kuma, slack, handlers):
        self._submit(handlers, slack, "acme.com")
        assert db.all_sites()[0]["channel_id"] == "U_A"

    def test_unreachable_channel_is_a_field_error(self, db, kuma, slack, handlers):
        ack = self._submit(handlers, slack, "acme.com", "C_PRIV")
        assert ack.kwargs["response_action"] == "errors"
        assert "/invite @UptimeBot" in ack.kwargs["errors"]["channel"]
        assert db.all_sites() == []

    def test_bad_url_and_duplicates_are_field_errors(self, db, kuma, slack, handlers):
        slack.channels["C1"] = JOINED
        assert "url" in self._submit(handlers, slack, "not a url", "C1").kwargs["errors"]
        self._submit(handlers, slack, "acme.com", "C1")
        ack = self._submit(handlers, slack, "https://www.acme.com/", "C1")
        assert "Already monitored" in ack.kwargs["errors"]["url"]
        assert len(db.all_sites()) == 1

    def test_respects_access_control(self, db, kuma, slack, handlers, monkeypatch):
        monkeypatch.setattr(config, "ALLOWED_USER_IDS", {"U_OTHER"})
        ack = self._submit(handlers, slack, "acme.com")
        assert ack.kwargs["response_action"] == "errors" and db.all_sites() == []


class TestShortcuts:
    def test_message_shortcut_prefills_link_and_channel(self, slack, handlers):
        body = {"user": {"id": "U_A"}, "trigger_id": "trig",
                "channel": {"id": "C1", "name": "general"},
                "message": {"text": "is <http://status.acme.com|status.acme.com> down?"}}
        handlers[("shortcut", "monitor_link_shortcut")](ack=Recorder(), body=body, client=slack)
        blocks = slack.calls_to("views_open")[0]["view"]["blocks"]
        assert blocks[0]["element"]["initial_value"] == "https://status.acme.com"
        assert blocks[2]["element"]["initial_conversation"] == "C1"

    def test_global_shortcut_opens_the_form(self, slack, handlers):
        handlers[("shortcut", "add_site_shortcut")](
            ack=Recorder(), body={"user": {"id": "U_A"}, "trigger_id": "trig"}, client=slack)
        assert slack.calls_to("views_open")


class TestMentions:
    def test_mention_anywhere_answers_in_thread(self, db, slack, handlers, context):
        say = Recorder()
        handlers[("event", "app_mention")](
            event={"user": "U_A", "channel": "C1", "text": f"hey <@{BOT_USER_ID}> help",
                   "thread_ts": "9.000"},
            client=slack, say=say, context=context)
        assert say.kwargs["thread_ts"] == "9.000" and say.kwargs["text"] == "UptimeBot help"

    def test_remove_prompt_is_private(self, db, kuma, slack, handlers, context):
        db.add_site(monitor_id=1, name="acme", url="https://acme.com", site_type="permanent",
                    added_by="U_A", added_by_name="Alice", channel_id="C1", is_dm=False,
                    expires_at=None)
        say = Recorder()
        handlers[("event", "app_mention")](
            event={"user": "U_A", "channel": "C1", "text": f"<@{BOT_USER_ID}> remove acme"},
            client=slack, say=say, context=context)
        assert say.calls == []
        assert slack.calls_to("chat_postEphemeral")[0]["user"] == "U_A"


class TestButtons:
    def _site(self, db, kuma, **over):
        base = dict(monitor_id=kuma.add_monitor("x", "https://acme.com"), name="acme",
                    url="https://acme.com", site_type="client", added_by="U_A",
                    added_by_name="Alice", channel_id="C1", is_dm=False,
                    expires_at=time.time() + 3600)
        base.update(over)
        return db.add_site(**base)

    def test_outsiders_cant_remove(self, db, kuma, slack, handlers):
        rid = self._site(db, kuma)
        respond = Recorder()
        handlers[("action", "confirm_remove")](
            ack=Recorder(), body=_click("confirm_remove", rid, "C_ELSEWHERE", user_id="U_B"),
            client=slack, respond=respond)
        assert "⛔" in respond.text and db.get_by_rowid(rid) is not None

    def test_remove_from_a_private_prompt_tells_the_channel(self, db, kuma, slack, handlers):
        rid = self._site(db, kuma)
        handlers[("action", "confirm_remove")](
            ack=Recorder(), body=_click("confirm_remove", rid, "C1", user_id="U_B", ephemeral=True),
            client=slack, respond=Recorder())
        assert db.get_by_rowid(rid) is None and kuma.monitors == {}
        notice = slack.calls_to("chat_postMessage")[0]
        assert notice["channel"] == "C1" and "stopped monitoring" in str(notice["blocks"])

    def test_extend_and_keep_buttons(self, db, kuma, slack, handlers):
        rid = self._site(db, kuma)
        before = db.get_by_rowid(rid)["expires_at"]
        handlers[("action", "expiry_extend")](
            ack=Recorder(), body=_click("expiry_extend", rid, "C1", user_id="U_B"),
            client=slack, respond=Recorder())
        assert db.get_by_rowid(rid)["expires_at"] > before + 6 * 86400

        respond = Recorder()
        handlers[("action", "expiry_keep")](
            ack=Recorder(), body=_click("expiry_keep", rid, "C1", user_id="U_B"),
            client=slack, respond=respond)
        s = db.get_by_rowid(rid)
        assert (s["site_type"], s["expires_at"]) == ("permanent", None)
        assert respond.kwargs["replace_original"] is True


class TestDirectMessages:
    def test_pasting_links_adds_them_as_personal_sites(self, db, kuma, slack, handlers):
        say = Recorder()
        handlers[("event", "message")](
            event={"channel_type": "im", "user": "U_A", "channel": "D_A",
                   "text": "please watch <http://a.com|a.com> and <https://b.com/health>"},
            client=slack, say=say)
        assert {(s["url"], s["channel_id"], s["is_dm"]) for s in db.all_sites()} == \
            {("https://a.com", "U_A", 1), ("https://b.com/health", "U_A", 1)}
        assert "Now monitoring *2 sites*" in say.text

    def test_bot_messages_are_ignored(self, db, slack, handlers):
        say = Recorder()
        handlers[("event", "message")](
            event={"channel_type": "im", "bot_id": "B1", "channel": "D_A", "text": "a.com"},
            client=slack, say=say)
        assert say.calls == []

    def test_how_it_works_button_answers_privately(self, slack, handlers):
        respond = Recorder()
        handlers[("action", "show_help")](ack=Recorder(), body=_click("show_help", "", "C9"),
                                          respond=respond)
        assert respond.kwargs["response_type"] == "ephemeral"
        assert respond.kwargs["replace_original"] is False
