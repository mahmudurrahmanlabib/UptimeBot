def _add(db, **over):
    base = dict(
        monitor_id=1, name="Acme", url="https://acme.com", site_type="permanent",
        added_by="U_A", added_by_name="Alice", channel_id="C1", is_dm=False,
        expires_at=None,
    )
    base.update(over)
    return db.add_site(**base)


class TestListSitesFilters:
    def test_filter_by_type(self, db):
        _add(db, monitor_id=1, name="A", site_type="permanent")
        _add(db, monitor_id=2, name="B", site_type="client", url="https://b.com",
             expires_at=9999999999)
        assert len(db.list_sites(site_type="client")) == 1
        assert len(db.list_sites()) == 2

    def test_filter_by_channel_and_user(self, db):
        _add(db, monitor_id=1, url="https://a.com", channel_id="C1", added_by="U_A")
        _add(db, monitor_id=2, url="https://b.com", channel_id="C2", added_by="U_B")
        assert len(db.list_sites(channel_id="C1")) == 1
        assert len(db.list_sites(added_by="U_B")) == 1


class TestFindDuplicate:
    def test_detects_canonical_variants_same_owner(self, db):
        _add(db, url="https://acme.com", channel_id="C1")
        for variant in ("acme.com", "http://www.ACME.com/", "https://acme.com/"):
            assert db.find_duplicate(variant, "C1") is not None

    def test_allows_same_url_different_owner(self, db):
        _add(db, url="https://acme.com", channel_id="C1")
        assert db.find_duplicate("acme.com", "C2") is None

    def test_no_false_positive(self, db):
        _add(db, url="https://acme.com", channel_id="C1")
        assert db.find_duplicate("other.com", "C1") is None


class TestFindSitesScoping:
    def test_scoped_to_channel(self, db):
        _add(db, monitor_id=1, name="Acme", url="https://acme.com", channel_id="C1")
        _add(db, monitor_id=2, name="Acme2", url="https://acme.org", channel_id="C2")
        assert [s["name"] for s in db.find_sites("acme", channel_id="C1")] == ["Acme"]
        assert db.find_sites("acme", channel_id="C3") == []

    def test_scoped_to_user(self, db):
        _add(db, monitor_id=1, name="Mine", url="https://mine.com", added_by="U_A")
        _add(db, monitor_id=2, name="Theirs", url="https://theirs.com", added_by="U_B")
        assert [s["name"] for s in db.find_sites("", added_by="U_A")] == ["Mine"]


class TestClearAll:
    def test_clears_and_counts(self, db):
        _add(db, monitor_id=1, url="https://a.com")
        _add(db, monitor_id=2, url="https://b.com")
        assert db.clear_all() == 2
        assert db.all_sites() == []


class TestMutations:
    def test_reclassify_and_set_expiry(self, db):
        rid = _add(db, site_type="permanent", expires_at=None)
        db.reclassify(rid, "client", 12345.0)
        s = db.get_by_rowid(rid)
        assert s["site_type"] == "client" and s["expires_at"] == 12345.0
        db.set_expiry(rid, 99999.0)
        assert db.get_by_rowid(rid)["expires_at"] == 99999.0
