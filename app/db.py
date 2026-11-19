# -*- coding: utf-8 -*-
"""SQLAlchemy models for the service. SQLite by default; domains are DATA
(hits rows), not columns — changing a brand's domains needs no migration.

The CSV file stays the CLI's store; the service keeps its own history here.
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (JSON, DateTime, ForeignKey, Integer, String, Text,
                        create_engine, event, inspect)
from sqlalchemy.engine import URL
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker)

BASE_DIR = Path(__file__).resolve().parents[1]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Brand(Base):
    __tablename__ = "brands"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    domains: Mapped[list] = mapped_column(JSON, default=list)
    aliases: Mapped[dict] = mapped_column(JSON, default=dict)
    niches: Mapped[dict] = mapped_column(JSON, default=dict)
    articles: Mapped[dict] = mapped_column(JSON, default=dict)
    geo: Mapped[str] = mapped_column(String(8), default="")
    engines: Mapped[list] = mapped_column(JSON, default=list)
    models: Mapped[dict] = mapped_column(JSON, default=dict)
    samples: Mapped[int] = mapped_column(Integer, default=1)
    schedule: Mapped[str] = mapped_column(String(100), default="")  # cron; "" = manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Read-only client share link: the token IS the access key to /share/{token}
    # (no login). NULL = sharing disabled; UNIQUE so one token never opens two
    # brands. Rotating replaces it, revoking sets it back to NULL.
    share_token: Mapped[str] = mapped_column(String(64), nullable=True,
                                             unique=True, default=None)
    runs: Mapped[list["Run"]] = relationship(
        back_populates="brand", cascade="all, delete-orphan",
        order_by="Run.id.desc()")


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    done_calls: Mapped[int] = mapped_column(Integer, default=0)
    # 1 while the run is pending/running, NULL once it is over. UNIQUE lets the
    # database itself refuse a second active run (NULLs never collide), so
    # "one run at a time" holds even when two triggers arrive together.
    active: Mapped[int] = mapped_column(Integer, nullable=True, unique=True)
    # The brand's configuration as it was when the run started. Results and
    # exports of a finished run are read against this snapshot, never against
    # the brand as it is edited later.
    config: Mapped[dict] = mapped_column(JSON, nullable=True)
    # Set to 1 by POST /runs/{id}/cancel. execute_run checks it between calls
    # and stops with status "cancelled", keeping the answers already collected.
    cancel_requested: Mapped[int] = mapped_column(Integer, nullable=False,
                                                  default=0, server_default="0")
    brand: Mapped[Brand] = relationship(back_populates="runs")
    answers: Mapped[list["Answer"]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
        order_by="Answer.run_ts")


class Answer(Base):
    __tablename__ = "answers"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # row_id
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    engine: Mapped[str] = mapped_column(String(40))
    niche: Mapped[str] = mapped_column(String(100))
    query: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(120), default="")
    model_requested: Mapped[str] = mapped_column(String(120), default="")
    geo: Mapped[str] = mapped_column(String(8), default="")
    geo_applied: Mapped[int] = mapped_column(Integer, default=0)
    sample: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(200), default="ok")
    answer_chars: Mapped[int] = mapped_column(Integer, default=0)
    n_sources: Mapped[int] = mapped_column(Integer, default=0)
    searches: Mapped[int] = mapped_column(Integer, nullable=True)
    usage: Mapped[dict] = mapped_column(JSON, nullable=True)
    receipt: Mapped[dict] = mapped_column(JSON, nullable=True)
    all_source_domains: Mapped[str] = mapped_column(Text, default="")
    run_ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    run: Mapped[Run] = relationship(back_populates="answers")
    hits: Mapped[list["Hit"]] = relationship(cascade="all, delete-orphan")
    article_hits: Mapped[list["ArticleHit"]] = relationship(cascade="all, delete-orphan")


class Hit(Base):
    __tablename__ = "hits"
    answer_id: Mapped[str] = mapped_column(ForeignKey("answers.id"), primary_key=True)
    domain: Mapped[str] = mapped_column(String(200), primary_key=True)
    src: Mapped[int] = mapped_column(Integer, default=0)
    txt: Mapped[int] = mapped_column(Integer, default=0)


class ArticleHit(Base):
    __tablename__ = "article_hits"
    answer_id: Mapped[str] = mapped_column(ForeignKey("answers.id"), primary_key=True)
    article_key: Mapped[str] = mapped_column(String(100), primary_key=True)


def make_engine(db_path: Path = None):
    path = Path(db_path or os.environ.get("GEO_DB_PATH") or BASE_DIR / "geo_tracker.db")
    # URL.create, not an f-string URL: a URL parsed from text is
    # percent-decoded by newer SQLAlchemy, so a path like ".../100%data/"
    # would silently point somewhere else.
    eng = create_engine(URL.create("sqlite", database=str(path)),
                        connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # Per connection, not per engine: SQLite pragmas are connection-local.
        # busy_timeout: the scheduler, the run thread and a request can meet
        # on one writer — wait instead of raising "database is locked".
        # foreign_keys: SQLite ignores FKs unless asked; cascades must hold.
        # WAL: readers (pages, exports) do not block the run's writes.
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
        finally:
            cur.close()

    return eng


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


BASELINE_REVISION = "0001"


def backup_database(path: Path, dest_dir: Path = None) -> Path:
    """Copy the DB file using SQLite's online backup API (safe with open
    connections) into dest_dir — by default backups/ next to the DB — with
    a UTC timestamp."""
    backups = Path(dest_dir) if dest_dir is not None else path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    dest = backups / f"{path.stem}-{stamp}{path.suffix}"
    # timeout: an active WAL writer must not fail the backup instantly with
    # "database is locked" — wait briefly for the writer instead.
    src = sqlite3.connect(str(path), timeout=5.0)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def migrate_database(engine) -> None:
    """Apply Alembic migrations at startup.

    - empty/new DB: the schema is created by the baseline migration;
    - DB created by pre-Alembic main (tables but no alembic_version):
      stamped with the baseline, then upgraded — data untouched;
    - already at head: nothing happens, no backup is written.
    Before any change to an existing non-empty DB, a copy is made with
    SQLite's backup API into backups/ next to the DB file.
    """
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    heads = set(ScriptDirectory.from_config(cfg).get_heads())
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        current = MigrationContext.configure(conn).get_current_revision()
    if current in heads:
        return
    path = Path(engine.url.database) if engine.url.database else None
    # `tables` as well as size: journal_mode=WAL on connect writes page 1 even
    # into a brand-new empty file, so a non-zero size alone no longer means
    # there is anything to lose. An empty DB gets no backup.
    if tables and path is not None and path.exists() and path.stat().st_size > 0:
        backup_database(path)
    # Alembic runs on this engine's own connection (see migrations/env.py)
    # rather than on a URL rebuilt from text: no second engine, and no path
    # that has to survive being written out and parsed back.
    with engine.connect() as conn:
        # Batch migrations recreate tables (create copy, drop original,
        # rename). With foreign_keys=ON the DROP of a parent table that
        # other rows reference fails, so enforcement is off for the
        # migration — the copy moves every row, nothing is orphaned. The
        # pragma is connection-local and a no-op inside a transaction, so
        # it is set BEFORE the migration transaction and restored after;
        # every other pooled connection keeps its own foreign_keys=ON.
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        try:
            with conn.begin():
                cfg.attributes["connection"] = conn
                if tables and "alembic_version" not in tables:
                    command.stamp(cfg, BASELINE_REVISION)
                command.upgrade(cfg, "head")
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()
