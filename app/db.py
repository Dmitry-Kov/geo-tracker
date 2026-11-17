# -*- coding: utf-8 -*-
"""SQLAlchemy models for the service. SQLite by default; domains are DATA
(hits rows), not columns — changing a brand's domains needs no migration.

The CSV file stays the CLI's store; the service keeps its own history here.
"""
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (JSON, DateTime, ForeignKey, Integer, String, Text,
                        create_engine)
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
    return create_engine(f"sqlite:///{path}",
                         connect_args={"check_same_thread": False})


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)
