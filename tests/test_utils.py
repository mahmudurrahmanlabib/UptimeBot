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
