from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
import logging
from statistics import median
import uuid

from sqlalchemy import or_, select, update

from .collector import Collector, CollectionError, CollectionResult
from .db import CrawlRun, Lease, Preferences, Quote, Snapshot, aware, utcnow

log = logging.getLogger(__name__)


def options(preferences):
    return {key: getattr(preferences, key) for key in ("interval_seconds", "enabled", "max_pages", "engine",
                                                       "merchant_selector", "player_selector")}


def store_result(session, run, result, at=None):
    at = at or utcnow()
    run.finished_at, run.engine, run.pages = at, result.engine, result.pages
    run.status = "success" if {q.side for q in result.quotes} == {"merchant", "player"} else "partial"
    run.error = "；".join(result.warnings)[:3000]
    for item in result.quotes:
        session.add(Quote(run_id=run.id, **item.__dict__))
    for side in ("merchant", "player"):
        values = [q.yuan_per_wan for q in result.quotes if q.side == side]
        if not values:
            continue
        run.merchant_count = len(values) if side == "merchant" else run.merchant_count
        run.player_count = len(values) if side == "player" else run.player_count
        session.add(Snapshot(run_id=run.id, dataset=run.dataset, side=side, at=at, count=len(values),
                             best=max(values) if side == "merchant" else min(values), median=median(values),
                             worst=min(values) if side == "merchant" else max(values)))


class Worker:
    def __init__(self, sessions, environment, collector=None):
        self.sessions, self.environment = sessions, environment
        self.collector = collector or Collector()
        self.owner = uuid.uuid4().hex

    def claim(self, force=False):
        now = utcnow()
        with self.sessions.begin() as session:
            acquired = session.execute(update(Lease).where(Lease.id == 1, or_(Lease.expires_at.is_(None), Lease.expires_at < now))
                                       .values(owner=self.owner, expires_at=now + timedelta(minutes=5)))
            if acquired.rowcount != 1:
                return None
            prefs = session.get(Preferences, 1)
            due = prefs.next_due_at is None or aware(prefs.next_due_at) <= now
            if not force and not prefs.requested and not (prefs.enabled and due):
                session.get(Lease, 1).expires_at = now
                return None
            # Acquiring an expired lease proves previous running rows no longer have an active owner.
            session.execute(update(CrawlRun).where(CrawlRun.dataset == self.environment.mode, CrawlRun.status == "running")
                            .values(status="failed", finished_at=now, error="上次采集未完成，锁已过期；本次重新采集"))
            origin = "manual" if prefs.requested or force else "scheduled"
            prefs.requested = False
            run = CrawlRun(dataset=self.environment.mode, origin=origin)
            session.add(run)
            session.flush()
            return run.id, options(prefs)

    async def once(self, force=False):
        claimed = self.claim(force)
        if claimed is None:
            return False
        run_id, config = claimed
        error = None
        try:
            result = await asyncio.wait_for(self.collector.collect(self.environment.source_url, config), timeout=180)
            with self.sessions.begin() as session:
                store_result(session, session.get(CrawlRun, run_id), result)
        except asyncio.CancelledError:
            error = CollectionError("采集进程停止，本次未完成")
            raise
        except Exception as exc:
            error = exc
        finally:
            now = utcnow()
            with self.sessions.begin() as session:
                prefs, lease, run = session.get(Preferences, 1), session.get(Lease, 1), session.get(CrawlRun, run_id)
                if error is not None:
                    run.status = "blocked" if isinstance(error, CollectionError) and error.blocked else "failed"
                    run.finished_at = now
                    run.error = str(error)[:2000] or type(error).__name__
                    prefs.failure_streak += 1
                else:
                    prefs.failure_streak = 0
                backoff = min(3600, 60 * 2 ** min(prefs.failure_streak, 6)) if error else 0
                retry_after = error.retry_after if isinstance(error, CollectionError) else 0
                prefs.next_due_at = now + timedelta(seconds=max(prefs.interval_seconds, backoff, retry_after))
                if lease.owner == self.owner:
                    lease.expires_at = now
        return True

    async def loop(self):
        with self.sessions.begin() as session:
            session.execute(update(CrawlRun).where(CrawlRun.status == "running", CrawlRun.started_at < utcnow() - timedelta(minutes=10))
                            .values(status="failed", finished_at=utcnow(), error="上次采集进程中断，未生成报价"))
        while True:
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Collector cycle failed")
            await asyncio.sleep(2)
