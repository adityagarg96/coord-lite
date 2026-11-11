"""Schema evolution: additive migrations, and the version gate that keeps mixed coordlite versions safe.
Every test builds a fresh SQLite file under pytest's tmp_path. Nothing here touches a real store."""
import sqlite3

import pytest

from coordlite import db


def test_a_new_kind_widens_the_check_and_keeps_rows_and_search(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    c = db.connect(p)
    with db.tx(c):
        for i in range(50):
            db.insert_node(c, kind="concept", title=f"widget {i}", body="gearbox", state="active")
    c.close()
    monkeypatch.setattr(db, "KINDS", set(db.KINDS) | {"newkind"})
    c = db.connect(p)
    assert c.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 50
    assert c.execute("SELECT COUNT(*) FROM node_fts WHERE node_fts MATCH 'gearbox'").fetchone()[0] == 50
    with db.tx(c):
        db.insert_node(c, kind="newkind", title="x", state="active")
    triggers = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"receipt_no_update", "receipt_no_delete", "node_fts_ai", "node_fts_au", "journal_no_delete"} <= triggers
    c.close()


def test_an_older_binary_does_not_narrow_a_store_that_holds_a_newer_kind(tmp_path, monkeypatch):
    """The failure that motivated the additive migration: a binary that has never heard of a kind opened a store
    containing it, tried to narrow the CHECK list, and died on an IntegrityError."""
    p = tmp_path / "s.db"
    db.connect(p).close()                                  # the store as the current release created it
    with monkeypatch.context() as m:                       # a newer binary widens it, then writes a kind this one lacks
        m.setattr(db, "KINDS", set(db.KINDS) | {"newkind"})
        c = db.connect(p)
        with db.tx(c):
            db.insert_node(c, kind="newkind", title="from the future", state="active")
        c.close()
    c = db.connect(p)                                      # the 'older' binary: KINDS without 'newkind'
    assert c.execute("SELECT kind FROM node").fetchone()[0] == "newkind"
    with db.tx(c):                                         # and the store still accepts that kind
        db.insert_node(c, kind="newkind", title="still allowed", state="active")
    c.close()


def test_the_edge_rel_list_is_additive_too(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    db.connect(p).close()
    with monkeypatch.context() as m:
        m.setattr(db, "RELS", set(db.RELS) | {"newrel"})
        c = db.connect(p)
        with db.tx(c):
            a = db.insert_node(c, kind="concept", title="a", state="active")
            b = db.insert_node(c, kind="concept", title="b", state="active")
            db.add_edge(c, a, b, "newrel")
        c.close()
    c = db.connect(p)
    assert c.execute("SELECT rel FROM edge").fetchone()[0] == "newrel"
    c.close()


def test_reopening_is_a_no_op(tmp_path):
    p = tmp_path / "s.db"
    db.connect(p).close()
    c = db.connect(p)
    assert db.migrate_checks(c) is False
    c.close()


def test_the_store_is_stamped_with_its_schema_version(tmp_path):
    c = db.connect(tmp_path / "s.db")
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    c.close()


def test_an_existing_unstamped_store_gets_stamped_without_losing_anything(tmp_path):
    p = tmp_path / "s.db"
    c = db.connect(p)
    with db.tx(c):
        db.insert_node(c, kind="concept", title="keep me", state="active")
    c.execute("PRAGMA user_version = 0")                  # a store from before the stamp existed
    c.close()
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert c.execute("SELECT title FROM node").fetchone()[0] == "keep me"
    c.close()


def test_a_binary_refuses_a_store_newer_than_it_understands(tmp_path):
    p = tmp_path / "s.db"
    c = db.connect(p)
    c.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    c.close()
    with pytest.raises(db.StoreVersionError, match="Update coord-lite"):
        db.connect(p)
    c = sqlite3.connect(p)                                 # and it left the store exactly as it found it
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION + 1
    c.close()
