# -*- coding: utf-8 -*-
"""Startup migrations: pre-Alembic DBs adopted without data loss, empty DBs
get the schema, repeat startups change nothing, and the baseline matches
the create_all schema of pre-Alembic main (semantically, via inspector)."""
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.db import (Answer, BASE_DIR, Base, Brand, Hit, Run, make_engine,
                    migrate_database)
from app.main import create_app


@pytest.fixture(autouse=True)
def env(monkeypatch):
    # same contract as test_app: never read the real .env
    monkeypatch.setattr("geo_tracker.load_env", lambda: None)
    for var in ("APP_PASSWORD", "APP_ALLOWED_HOSTS", "APP_SECRET", "APP_HTTPS_ONLY"):
        monkeypatch.delenv(var, raising=False)


def make_v1_db(path, *, versioned):
    """A database exactly at baseline 0001, with data — built by the
    migration itself, not create_all (the current models already carry
    columns 0001 does not have). versioned=False also drops alembic_version:
    the state pre-Alembic main left behind."""
    from alembic import command
    from alembic.config import Config

    engine = make_engine(path)
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
    engine.dispose()
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO brands (name, domains, aliases, niches, articles, geo,"
        " engines, models, samples, schedule, created_at)"
        " VALUES ('OldBrand', '[\"old.io\"]', '{}',"
        " '{\"n\": {\"title\": \"N\", \"primary_domain\": \"old.io\","
        " \"queries\": [\"q\"]}}', '{}', '', '[\"gemini\"]', '{}', 1, '',"
        " '2026-11-17 10:00:00')")
    raw.execute(
        "INSERT INTO runs (brand_id, status, trigger, error, total_calls,"
        " done_calls, active, config, started_at, finished_at)"
        " VALUES (1, 'done', 'manual', '', 1, 1, NULL,"
        " '{\"domains\": [\"old.io\"]}', '2026-11-17 10:00:00',"
        " '2026-11-17 10:01:00')")
    raw.execute(
        "INSERT INTO answers (id, run_id, engine, niche, query, model,"
        " model_requested, geo, geo_applied, sample, status, answer_chars,"
        " n_sources, searches, usage, receipt, all_source_domains, run_ts)"
        " VALUES ('oldrow000001', 1, 'gemini', 'n', 'q', 'm', '', '', 0, 1,"
        " 'ok', 5, 1, NULL, NULL, NULL, '', '2026-11-17 10:00:30')")
    raw.execute("INSERT INTO hits (answer_id, domain, src, txt)"
                " VALUES ('oldrow000001', 'old.io', 1, 0)")
    if not versioned:
        raw.execute("DROP TABLE alembic_version")
    raw.commit()
    raw.close()
    return path


def make_v2_db(path):
    """A database exactly at 0002, with data — built by the migrations
    themselves (0001 + data, then the 0002 upgrade)."""
    from alembic import command
    from alembic.config import Config

    make_v1_db(path, versioned=True)
    engine = make_engine(path)
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0002")
    engine.dispose()
    return path


def db_snapshot(path):
    conn = sqlite3.connect(path)
    try:
        master = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name").fetchall()
        counts = {}
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            counts[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return master, counts
    finally:
        conn.close()


def test_old_main_db_adopted_with_data_and_backup(tmp_path):
    path = make_v1_db(tmp_path / "old.db", versioned=False)
    app = create_app(db_path=path)
    with TestClient(app, base_url="http://localhost") as c:
        assert "OldBrand" in c.get("/").text  # service works on the adopted DB
        Session = app.state.Session
        with Session() as s:
            assert s.get(Brand, 1).name == "OldBrand"
            assert s.get(Brand, 1).share_token is None
            assert s.get(Run, 1).status == "done"
            assert s.get(Answer, "oldrow000001").query == "q"
            assert s.query(Hit).count() == 1
        conn = sqlite3.connect(path)
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        conn.close()
        assert version == "0003"  # stamped 0001, then upgraded to head

    backups = list((tmp_path / "backups").glob("old-*.db"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])  # valid SQLite copy with the data
    try:
        assert conn.execute("SELECT name FROM brands").fetchone()[0] == "OldBrand"
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        # the copy was taken BEFORE any migration touched the file
        assert "alembic_version" not in tables
        cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
        assert "cancel_requested" not in cols
        bcols = [r[1] for r in conn.execute("PRAGMA table_info(brands)")]
        assert "share_token" not in bcols
    finally:
        conn.close()


def test_upgrade_0001_with_data_to_0002(tmp_path):
    """A versioned 0001 DB with data → startup → upgrades to head (0003):
    data intact, old rows get cancel_requested=0 and share_token=None, and
    the backup preserves the pre-migration (0001) state."""
    path = make_v1_db(tmp_path / "v1.db", versioned=True)
    app = create_app(db_path=path)
    with TestClient(app, base_url="http://localhost") as c:
        assert "OldBrand" in c.get("/").text
        Session = app.state.Session
        with Session() as s:
            assert s.get(Brand, 1).name == "OldBrand"
            assert s.get(Brand, 1).share_token is None
            run = s.get(Run, 1)
            assert run.status == "done"
            assert run.cancel_requested == 0  # server_default filled old rows
            assert s.get(Answer, "oldrow000001").query == "q"
            assert s.query(Hit).count() == 1
    conn = sqlite3.connect(path)
    version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    conn.close()
    assert version == "0003"

    backups = list((tmp_path / "backups").glob("v1-*.db"))
    assert len(backups) == 1
    bconn = sqlite3.connect(backups[0])
    try:
        assert bconn.execute(
            "SELECT version_num FROM alembic_version").fetchone()[0] == "0001"
        assert bconn.execute("SELECT name FROM brands").fetchone()[0] == "OldBrand"
        cols = [r[1] for r in bconn.execute("PRAGMA table_info(runs)")]
        assert "cancel_requested" not in cols  # copy is pre-migration
    finally:
        bconn.close()


def test_upgrade_0002_with_data_to_0003(tmp_path):
    """The share-link migration: a versioned 0002 DB with data → startup →
    data intact, version 0003, old brands get share_token=None, and the
    backup preserves the pre-migration (0002) state without the column.
    0003 recreates the brands table (unique constraint), which must not
    trip over the runs → brands foreign key."""
    path = make_v2_db(tmp_path / "v2.db")
    app = create_app(db_path=path)
    with TestClient(app, base_url="http://localhost") as c:
        assert "OldBrand" in c.get("/").text
        Session = app.state.Session
        with Session() as s:
            b = s.get(Brand, 1)
            assert b.name == "OldBrand"
            assert b.share_token is None
            run = s.get(Run, 1)
            assert run.status == "done" and run.cancel_requested == 0
            assert s.get(Answer, "oldrow000001").query == "q"
            assert s.query(Hit).count() == 1
    conn = sqlite3.connect(path)
    try:
        version = conn.execute(
            "SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "0003"
        cols = [r[1] for r in conn.execute("PRAGMA table_info(brands)")]
        assert "share_token" in cols
        # the FK from runs survived the table recreation
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    backups = list((tmp_path / "backups").glob("v2-*.db"))
    assert len(backups) == 1
    bconn = sqlite3.connect(backups[0])
    try:
        assert bconn.execute(
            "SELECT version_num FROM alembic_version").fetchone()[0] == "0002"
        assert bconn.execute("SELECT name FROM brands").fetchone()[0] == "OldBrand"
        cols = [r[1] for r in bconn.execute("PRAGMA table_info(brands)")]
        assert "share_token" not in cols  # copy is pre-migration
    finally:
        bconn.close()


def test_empty_db_gets_schema_without_backup(tmp_path):
    path = tmp_path / "new.db"
    app = create_app(db_path=path)
    with TestClient(app):
        tables = set(inspect(make_engine(path)).get_table_names())
    assert {"brands", "runs", "answers", "hits", "article_hits",
            "alembic_version"} <= tables
    assert not (tmp_path / "backups").exists()  # nothing to lose → no copy


def test_repeat_startup_changes_nothing(tmp_path):
    path = tmp_path / "repeat.db"
    with TestClient(create_app(db_path=path), base_url="http://localhost"):
        pass
    before = db_snapshot(path)
    backups_before = sorted(p.name for p in (tmp_path / "backups").glob("*")) \
        if (tmp_path / "backups").exists() else []
    with TestClient(create_app(db_path=path), base_url="http://localhost"):
        pass
    assert db_snapshot(path) == before
    backups_after = sorted(p.name for p in (tmp_path / "backups").glob("*")) \
        if (tmp_path / "backups").exists() else []
    assert backups_after == backups_before  # at head → no new copy either


def test_baseline_matches_create_all_schema(tmp_path):
    old = tmp_path / "old.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{old}"))
    new = tmp_path / "new.db"
    migrate_database(make_engine(new))

    ia, ib = inspect(create_engine(f"sqlite:///{old}")), inspect(make_engine(new))
    tables_a = set(ia.get_table_names())
    tables_b = set(ib.get_table_names()) - {"alembic_version"}
    assert tables_a == tables_b
    for t in sorted(tables_a):
        cols_a = [(c["name"], str(c["type"]), c["nullable"]) for c in ia.get_columns(t)]
        cols_b = [(c["name"], str(c["type"]), c["nullable"]) for c in ib.get_columns(t)]
        assert cols_a == cols_b, t
        assert ia.get_pk_constraint(t)["constrained_columns"] == \
            ib.get_pk_constraint(t)["constrained_columns"], t
        fk_a = sorted((f["referred_table"], tuple(f["constrained_columns"]),
                       tuple(f["referred_columns"])) for f in ia.get_foreign_keys(t))
        fk_b = sorted((f["referred_table"], tuple(f["constrained_columns"]),
                       tuple(f["referred_columns"])) for f in ib.get_foreign_keys(t))
        assert fk_a == fk_b, t
        uq_a = sorted(tuple(u["column_names"]) for u in ia.get_unique_constraints(t))
        uq_b = sorted(tuple(u["column_names"]) for u in ib.get_unique_constraints(t))
        assert uq_a == uq_b, t


def test_migrating_does_not_silence_the_servers_loggers(tmp_path):
    """Alembic's env.py configures logging from alembic.ini. Done the default
    way that disables every logger that already exists — uvicorn's among
    them — so a start that runs a migration would serve requests with no
    startup line, no access log and no error tracebacks."""
    existing = [logging.getLogger(name) for name in
                ("uvicorn.error", "uvicorn.access", "geo_tracker.test_probe")]
    for lg in existing:
        lg.disabled = False
    with TestClient(create_app(db_path=tmp_path / "fresh.db"), base_url="http://localhost"):
        pass                                   # empty DB → the baseline runs
    assert [lg.name for lg in existing if lg.disabled] == []


def test_database_path_with_a_percent_sign(tmp_path):
    # the URL goes through alembic's ini-style config, where % is special
    folder = tmp_path / "100%data"
    folder.mkdir()
    with TestClient(create_app(db_path=folder / "t.db"), base_url="http://localhost") as c:
        assert c.get("/").status_code == 200
