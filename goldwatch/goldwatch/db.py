from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def iso(value):
    return aware(value).isoformat().replace("+00:00", "Z") if value is not None else None


class Base(DeclarativeBase):
    pass


class Preferences(Base):
    __tablename__ = "preferences"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    interval_seconds: Mapped[int] = mapped_column(default=600)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_pages: Mapped[int] = mapped_column(default=1)
    engine: Mapped[str] = mapped_column(String(16), default="auto")
    merchant_selector: Mapped[str] = mapped_column(String(400), default="")
    player_selector: Mapped[str] = mapped_column(String(400), default="")
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_manual_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_streak: Mapped[int] = mapped_column(default=0)


class Lease(Base):
    __tablename__ = "collector_lease"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    owner: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(8), index=True, default="live")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    origin: Mapped[str] = mapped_column(String(20), default="scheduled")
    engine: Mapped[str] = mapped_column(String(20), default="http")
    pages: Mapped[int] = mapped_column(default=0)
    merchant_count: Mapped[int] = mapped_column(default=0)
    player_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"), index=True)
    side: Mapped[str] = mapped_column(String(12), index=True)
    source_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    source_url: Mapped[str] = mapped_column(Text)
    wan_per_yuan: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    yuan_per_wan: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    quantity_wan: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    evidence: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("run_id", "side", "source_key"),)


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"), index=True)
    dataset: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(12))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    best: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    median: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    worst: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("run_id", "side"), Index("snapshot_lookup", "dataset", "side", "at"))


def database(url):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite:"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    elif url.startswith("mysql"):
        kwargs["connect_args"] = {"connect_timeout": 10, "read_timeout": 30, "write_timeout": 30}
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        if session.get(Preferences, 1) is None:
            session.add(Preferences(id=1))
        if session.get(Lease, 1) is None:
            session.add(Lease(id=1))
    return engine, sessions
