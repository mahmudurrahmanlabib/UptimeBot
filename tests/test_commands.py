import slack_app


def _ctx(is_dm=False):
    return {"client": None, "user_id": "U_A", "channel_id": "C1",
            "is_dm": is_dm, "user_name": "Alice"}


class TestRouting:
    def test_verbs_dispatch(self, monkeypatch):
        calls = {}
        for verb, fn in [("list", "cmd_list"), ("status", "cmd_status"),
                         ("remove", "cmd_remove"), ("extend", "cmd_extend"),
                         ("set", "cmd_set"), ("monitor", "cmd_add")]:
            monkeypatch.setattr(slack_app, fn,
                                lambda args, ctx, _n=fn: calls.setdefault(_n, args))
        for text, fn in [("list client", "cmd_list"), ("status x", "cmd_status"),
                         ("remove x", "cmd_remove"), ("extend x 3", "cmd_extend"),
                         ("set x client", "cmd_set"), ("monitor x.com", "cmd_add")]:
            slack_app.handle_command(text, _ctx())
        assert set(calls) == {"cmd_list", "cmd_status", "cmd_remove",
                              "cmd_extend", "cmd_set", "cmd_add"}

    def test_bare_link_falls_back_to_add(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(slack_app, "cmd_add",
                            lambda args, ctx: seen.setdefault("args", args))
        slack_app.handle_command("example.com", _ctx())
        assert seen["args"] == ["example.com"]

    def test_unknown_text_is_help_not_add(self, monkeypatch):
        monkeypatch.setattr(slack_app, "cmd_add",
                            lambda *a: _fail("should not add"))
        resp = slack_app.handle_command("what is this", _ctx())
        assert "didn't recognise" in resp["text"].lower()


def _fail(msg):
    raise AssertionError(msg)


class TestAddParsing:
    def _capture(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(slack_app, "_do_add",
                            lambda url, name, site_type, ctx: captured.update(
                                url=url, name=name, site_type=site_type) or {"text": "ok"})
        return captured

    def test_default_type_and_derived_name(self, monkeypatch):
        c = self._capture(monkeypatch)
        slack_app.cmd_add(["example.com"], _ctx())
        assert c["url"] == "https://example.com"
        assert c["site_type"] == "permanent"
        assert c["name"] == "example.com"

    def test_type_before_url(self, monkeypatch):
        c = self._capture(monkeypatch)
        slack_app.cmd_add(["client", "demo.acme.com"], _ctx())
        assert c["site_type"] == "client"
        assert c["url"] == "https://demo.acme.com"

    def test_type_after_url_and_name(self, monkeypatch):
        c = self._capture(monkeypatch)
        slack_app.cmd_add(["stg.acme.com", "staging", "Acme", "Staging"], _ctx())
        assert c["site_type"] == "staging"
        assert c["name"] == "Acme Staging"

    def test_as_type_syntax(self, monkeypatch):
        c = self._capture(monkeypatch)
        slack_app.cmd_add(["acme.com", "as", "prod"], _ctx())
        assert c["site_type"] == "prod"

    def test_no_url_is_error(self, monkeypatch):
        self._capture(monkeypatch)
        resp = slack_app.cmd_add(["just", "words"], _ctx())
        assert "url" in resp["text"].lower()
