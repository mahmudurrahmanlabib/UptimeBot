import utils


class TestCanonicalUrl:
    def test_variants_fold_to_one_key(self):
        canon = utils.canonical_url("https://example.com")
        for variant in (
            "example.com",
            "EXAMPLE.com",
            "https://example.com/",
            "http://example.com",
            "https://www.example.com",
            "www.example.com/",
        ):
            assert utils.canonical_url(variant) == canon == "example.com"

    def test_path_is_significant(self):
        assert utils.canonical_url("example.com/app") != utils.canonical_url("example.com")

    def test_subdomain_is_significant(self):
        assert utils.canonical_url("api.example.com") != utils.canonical_url("example.com")


class TestNormalizeUrl:
    def test_adds_scheme(self):
        assert utils.normalize_url("example.com") == "https://example.com"

    def test_keeps_existing_scheme(self):
        assert utils.normalize_url("http://example.com") == "http://example.com"

    def test_unwraps_slack_link(self):
        assert utils.normalize_url("<https://example.com|example.com>") == "https://example.com"


class TestLooksLikeUrl:
    def test_accepts_real_urls(self):
        assert utils.looks_like_url("https://example.com")

    def test_rejects_bare_word(self):
        assert not utils.looks_like_url("https://localhost")  # no dot
        assert not utils.looks_like_url("notaurl")


class TestNormalizeType:
    def test_synonyms(self):
        assert utils.normalize_type("production") == "prod"
        assert utils.normalize_type("temp") == "client"
        assert utils.normalize_type("PERM") == "permanent"

    def test_unknown_returns_none(self):
        assert utils.normalize_type("banana") is None
        assert utils.normalize_type("") is None


class TestCleanIdentifier:
    def test_strips_scheme_and_slash(self):
        assert utils.clean_identifier("https://example.com/") == "example.com"

    def test_plain_name_passes_through(self):
        assert utils.clean_identifier("Acme App") == "Acme App"


class TestDeriveName:
    def test_uses_hostname(self):
        assert utils.derive_name("https://app.acme.com/path") == "app.acme.com"


class TestHumanize:
    def test_days_and_hours(self):
        assert utils.humanize(6 * 86400 + 3 * 3600) == "6d 3h"

    def test_minutes_only(self):
        assert utils.humanize(10 * 60) == "10m"

    def test_floor(self):
        assert utils.humanize(30) == "<1m"


class TestSlackLinks:
    def test_autolinked_bare_domain_becomes_https(self):
        # Slack turns a typed `example.com` into <http://example.com|example.com>.
        assert utils.normalize_url("<http://example.com|example.com>") == "https://example.com"
        assert utils.normalize_url("<http://example.com/app|example.com/app>") == \
            "https://example.com/app"

    def test_explicit_http_is_kept(self):
        assert utils.normalize_url("<http://example.com>") == "http://example.com"

    def test_labelled_link_uses_target(self):
        assert utils.normalize_url("<https://example.com/x|our site>") == "https://example.com/x"

    def test_unescapes_ampersands(self):
        assert utils.normalize_url("<https://example.com/?a=1&amp;b=2>") == \
            "https://example.com/?a=1&b=2"

    def test_trailing_punctuation(self):
        assert utils.normalize_url("<http://example.com|example.com>.") == "https://example.com"
        assert utils.normalize_url("example.com,") == "https://example.com"

    def test_tokenize_keeps_labelled_links_whole(self):
        assert utils.tokenize("watch <https://acme.com/a|the acme app> please") == \
            ["watch", "https://acme.com/a", "please"]

    def test_extract_urls(self):
        text = "see <http://a.com|a.com> and <https://b.com/x> (and a.com again), v1.2 isn't one"
        assert utils.extract_urls(text) == ["https://a.com", "https://b.com/x"]


class TestStrictUrls:
    def test_rejects_lookalikes(self):
        for word in ("v1.2", "e.g.", "a@b.com", "<mailto:a@b.com|a@b.com>", "<#C123|general>",
                     "<@U123>", "example.com:99999"):
            assert not utils.looks_like_url(utils.normalize_url(word)), word

    def test_accepts_ips_ports_and_paths(self):
        for url in ("http://10.0.0.5:8080/health", "https://[::1]:8443", "https://sub.example.co.uk/a?b=1"):
            assert utils.looks_like_url(url), url


class TestMentions:
    def test_strip_bot_mention_anywhere(self):
        assert utils.strip_bot_mention("hey <@U_BOT> list", "U_BOT") == "hey list"
        assert utils.strip_bot_mention("<@U_BOT|uptimebot> status x", "U_BOT") == "status x"
        assert utils.strip_bot_mention("<@U_BOT> list") == "list"

    def test_parse_mentions(self):
        assert utils.parse_mentions(["@here"]) == "<!here>"
        assert utils.parse_mentions(["<!channel>"]) == "<!channel>"
        assert utils.parse_mentions(["<@U123|alice>", "<!subteam^S9|@oncall>"]) == \
            "<@U123> <!subteam^S9>"
        assert utils.parse_mentions(["off"]) == ""
        assert utils.parse_mentions(["@alice"]) is None  # plain text: no id to ping

    def test_describe_never_pings_here(self):
        assert "<!here>" not in utils.describe_mentions("<!here>")
        assert utils.describe_mentions("") == "nobody"


class TestFormatting:
    def test_clean_name(self):
        assert utils.clean_name("  Acme &amp; <Co>  `prod` ") == "Acme & Co prod"
        assert len(utils.clean_name("x" * 500)) == 80

    def test_link_hides_scheme_and_truncates(self):
        assert utils.link("https://example.com/") == "<https://example.com/|example.com>"
        assert utils.link("https://example.com/" + "a" * 100).endswith("…>")

    def test_slack_date_has_fallback(self):
        assert utils.slack_date(0, "{time}") == "<!date^0^{time}|1970-01-01 00:00 UTC>"
