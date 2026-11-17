# -*- coding: utf-8 -*-
"""Startup migrations: pre-Alembic DBs adopted without data loss, empty DBs
get the schema, repeat startups change nothing, and the baseline matches
the create_all schema of pre-Alembic main (semantically, via inspector)."""
import logging
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.db import (Answer, Base, Brand, Hit, Run, make_engine,
                    migrate_database)
from app.main import create_app


@pytest.fixture(autouse=True)
def env(monkeypatch):
    # same contract as test_app: never read the real .env
    monkeypatch.setattr("geo_tracker.load_env", lambda: None)
    for var in ("APP_PASSWORD", "APP_ALLOWED_HOSTS", "APP_SECRET", "APP_HTTPS_ONLY"):
        monkeypatch.delenv(var, raising=False)


def make_old_main_db(path):
    """Exactly what pre-Alembic main did: create_all, plus some data."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as s:
        b = Brand(name="OldBrand", domains=["old.io"], aliases={},
                  niches={"n": {"title": "N", "primary_domain": "old.io",
                                "queries": ["q"]}},
                  articles={}, geo="", engines=["gemini"], models={},
                  samples=1, schedule="")
        s.add(b)
        s.commit()
        run = Run(brand_id=b.id, status="done", trigger="manual",
                  total_calls=1, done_calls=1, config={"domains": ["old.io"]})
        s.add(run)
        s.commit()
        a = Answer(id="oldrow000001", run_id=run.id, engine="gemini", niche="n",
                   query="q", status="ok", model="m",
                   run_ts=datetime.now(timezone.utc))
        s.add(a)
        s.add(Hit(answer_id=a.id, domain="old.io", src=1, txt=0))
        s.commit()
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
    path = make_old_main_db(tmp_path / "old.db")
    app = create_app(db_path=path)
    with TestClient(app, base_url="http://localhost") as c:
        assert "OldBrand" in c.get("/").text  # service works on the adopted DB
        Session = app.state.Session
        with Session() as s:
            assert s.get(Brand, 1).name == "OldBrand"
            assert s.get(Run, 1).status == "done"
            assert s.get(Answer, "oldrow000001").query == "q"
            assert s.query(Hit).count() == 1
        conn = sqlite3.connect(path)
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        conn.close()
        assert version == "0001"

    backups = list((tmp_path / "backups").glob("old-*.db"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])  # valid SQLite copy with the data
    try:
        assert conn.execute("SELECT name FROM brands").fetchone()[0] == "OldBrand"
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        # the copy was taken BEFORE any migration touched the file
        assert "alembic_version" not in tables
    finally:
        conn.close()


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
