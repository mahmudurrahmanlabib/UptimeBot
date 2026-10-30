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


class TestPersonalDuplicates:
    def test_dm_dupes_match_across_dm_and_user_ids(self, db):
        # Older DM adds were stored under the D… channel, new ones under the user id.
        _add(db, url="https://acme.com", channel_id="D_A", is_dm=True, added_by="U_A")
        assert db.find_duplicate("acme.com", added_by="U_A") is not None
        assert db.find_duplicate("acme.com", added_by="U_B") is None

    def test_channel_site_isnt_a_personal_dupe(self, db):
        _add(db, url="https://acme.com", channel_id="C1", is_dm=False, added_by="U_A")
        assert db.find_duplicate("acme.com", added_by="U_A") is None


class TestOutages:
    def test_open_and_close(self, db):
        rid = _add(db)
        db.open_outage(rid, 100.0, "C1", "1.000")
        s = db.get_by_rowid(rid)
        assert (s["down_since"], s["alert_channel"], s["alert_ts"]) == (100.0, "C1", "1.000")
        db.close_outage(rid)
        s = db.get_by_rowid(rid)
        assert (s["down_since"], s["alert_channel"], s["alert_ts"]) == (None, None, None)


class TestChannelSettings:
    def test_mention_round_trip(self, db):
        assert db.get_mention("C1") == ""
        db.set_mention("C1", "<!here>", "U_A")
        assert db.get_mention("C1") == "<!here>"
        db.set_mention("C1", "", "U_A")
        assert db.get_mention("C1") == ""


class TestMigration:
    OLD_SCHEMA = """
    CREATE TABLE sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER NOT NULL,
        name TEXT NOT NULL, url TEXT NOT NULL, site_type TEXT NOT NULL,
        added_by TEXT, added_by_name TEXT, channel_id TEXT, is_dm INTEGER DEFAULT 0,
        created_at REAL NOT NULL, expires_at REAL, last_status TEXT DEFAULT 'pending',
        warned_expiry INTEGER DEFAULT 0);
    """

    def test_upgrades_old_database_in_place(self, tmp_path, monkeypatch):
        import sqlite3

        import config
        import database
        path = str(tmp_path / "old.db")
        con = sqlite3.connect(path)
        con.executescript(self.OLD_SCHEMA)
        con.execute("INSERT INTO sites (monitor_id, name, url, site_type, created_at, "
                    "last_status, channel_id) VALUES (1, 'a', 'https://a.com', 'permanent', 0, "
                    "'down', 'C1'), (2, 'b', 'https://b.com', 'permanent', 0, 'up', 'C1')")
        con.commit()
        con.close()
        monkeypatch.setattr(config, "DB_PATH", path)

        database.init_db()
        database.init_db()  # idempotent
        rows = {s["name"]: s for s in database.all_sites()}
        # Already-down sites count as an open outage, so the upgrade doesn't re-alert them.
        assert rows["a"]["down_since"] is not None
        assert rows["b"]["down_since"] is None
        assert database.get_mention("C1") == ""
