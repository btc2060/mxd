from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math

from sqlalchemy import func, select

from .db import CrawlRun, Preferences, Quote, Snapshot, aware, iso, utcnow
from .service import options

PERIODS = {"5m": 300, "30m": 1800, "1h": 3600, "1d": 86400, "1w": 604800}
RANGES = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
SHANGHAI = timezone(timedelta(hours=8))


def number(value):
    return float(value) if value is not None else None


def run_json(run):
    if run is None:
        return None
    return {"id": run.id, "started_at": iso(run.started_at), "finished_at": iso(run.finished_at), "status": run.status,
            "origin": run.origin, "engine": run.engine, "pages": run.pages, "merchant_count": run.merchant_count,
            "player_count": run.player_count, "message": run.error}


def latest_summary(session, environment):
    now = utcnow()
    prefs = session.get(Preferences, 1)
    markets = {}
    for side in ("merchant", "player"):
        latest = session.scalar(select(Snapshot).where(Snapshot.dataset == environment.mode, Snapshot.side == side)
                                .order_by(Snapshot.at.desc(), Snapshot.id.desc()).limit(1))
        if latest is None:
            markets[side] = None
            continue
        at = aware(latest.at)
        baseline = session.scalar(select(Snapshot).where(Snapshot.dataset == environment.mode, Snapshot.side == side,
                                                         Snapshot.at <= at - timedelta(days=1))
                                  .order_by(Snapshot.at.desc()).limit(1))
        recent = session.scalars(select(Snapshot).where(Snapshot.dataset == environment.mode, Snapshot.side == side,
                                                        Snapshot.at >= now - timedelta(days=1))).all()
        delta = (latest.best / baseline.best - 1) * 100 if baseline else None
        markets[side] = {"run_id": latest.run_id, "at": iso(latest.at), "yuan_per_wan": number(latest.best),
                         "wan_per_yuan": number(1 / latest.best), "median": number(latest.median), "count": latest.count,
                         "change_24h": number(delta), "high_24h": number(max((s.best for s in recent), default=latest.best)),
                         "low_24h": number(min((s.best for s in recent), default=latest.best)),
                         "stale": (now - at).total_seconds() > max(prefs.interval_seconds * 2, 900)}
    bid, ask = markets["merchant"], markets["player"]
    spread = None
    if bid and ask and bid["run_id"] == ask["run_id"] and not bid["stale"] and not ask["stale"]:
        spread = {"yuan_per_wan": ask["yuan_per_wan"] - bid["yuan_per_wan"],
                  "percent": (ask["yuan_per_wan"] - bid["yuan_per_wan"]) / ask["yuan_per_wan"] * 100}
    last = session.scalar(select(CrawlRun).where(CrawlRun.dataset == environment.mode).order_by(CrawlRun.id.desc()).limit(1))
    last_day = session.scalar(select(func.count(CrawlRun.id)).where(CrawlRun.dataset == environment.mode,
                                                                  CrawlRun.started_at >= now - timedelta(days=1),
                                                                  CrawlRun.status.in_(("success", "partial"))))
    return {"mode": environment.mode, "market_name": environment.market_name, "source_url": environment.source_url,
            "server_time": iso(now), "markets": markets, "spread": spread, "last_run": run_json(last),
            "samples_24h": last_day, "preferences": {**options(prefs), "next_due_at": iso(prefs.next_due_at),
                                                       "failure_streak": prefs.failure_streak},
            "worker_enabled": environment.worker_enabled and environment.mode == "live"}


def quote_rows(session, dataset, side, limit=30):
    latest = session.scalar(select(Snapshot).where(Snapshot.dataset == dataset, Snapshot.side == side)
                            .order_by(Snapshot.at.desc(), Snapshot.id.desc()).limit(1))
    if latest is None:
        return {"at": None, "rows": []}
    ordering = Quote.yuan_per_wan.desc() if side == "merchant" else Quote.yuan_per_wan.asc()
    rows = session.scalars(select(Quote).where(Quote.run_id == latest.run_id, Quote.side == side).order_by(ordering, Quote.id).limit(limit))
    return {"at": iso(latest.at), "rows": [{"id": row.id, "title": row.title, "source_url": row.source_url,
                                             "wan_per_yuan": number(row.wan_per_yuan), "yuan_per_wan": number(row.yuan_per_wan),
                                             "quantity_wan": number(row.quantity_wan)} for row in rows]}


def bucket_start(stamp, seconds):
    if seconds == 604800:
        local = datetime.fromtimestamp(stamp, SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
        return int((local - timedelta(days=local.weekday())).timestamp())
    return math.floor((stamp + 28800) / seconds) * seconds - 28800


def history(session, dataset, period="30m", span="7d", metric="best", unit="yuan_per_wan", now=None):
    now = now or utcnow()
    days = RANGES[span]
    requested = PERIODS[period]
    seconds = next((value for value in PERIODS.values() if value >= requested and days * 86400 / value <= 4000), 604800)
    start = now - timedelta(days=days)
    first, last = bucket_start(start.timestamp(), seconds), bucket_start(now.timestamp(), seconds)
    # Include the full first visible candle, rather than silently cutting off its open/high/low.
    query_start = datetime.fromtimestamp(first, timezone.utc)
    snapshots = session.scalars(select(Snapshot).where(Snapshot.dataset == dataset, Snapshot.at >= query_start, Snapshot.at <= now)
                               .order_by(Snapshot.at, Snapshot.id)).all()
    grouped = {}
    for item in snapshots:
        stamp = bucket_start(aware(item.at).timestamp(), seconds)
        grouped.setdefault((item.side, stamp), []).append(item)
    times = list(range(first, last + 1, seconds))
    result = {"merchant": [], "player": []}
    for side in result:
        for stamp in times:
            items = grouped.get((side, stamp), [])
            if not items:
                result[side].append(None)
                continue
            values = [getattr(item, metric) for item in items]
            opening, closing, high, low = values[0], values[-1], max(values), min(values)
            if unit == "wan_per_yuan":
                opening, closing, high, low = 1 / opening, 1 / closing, 1 / low, 1 / high
            result[side].append({"open": number(opening), "close": number(closing), "high": number(high), "low": number(low),
                                 "count": sum(item.count for item in items), "samples": len(items), "last_at": iso(items[-1].at)})
    actual = next(key for key, value in PERIODS.items() if value == seconds)
    return {"times": [iso(datetime.fromtimestamp(t, timezone.utc)) for t in times], "series": result, "period": actual,
            "requested_period": period, "span": span, "metric": metric, "unit": unit,
            "note": "范围较大，已自动使用更大的聚合周期" if actual != period else "没有采样的时段留空，不补造历史价格"}
