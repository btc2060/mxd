"""Explicit demo dataset only. Never presented as scraped/live observations."""
from datetime import timedelta
from decimal import Decimal
import math
import random

from sqlalchemy import func, select

from .collector import CollectionResult
from .db import CrawlRun, utcnow
from .parser import ParsedQuote
from .service import store_result


def seed(sessions, source):
    with sessions.begin() as session:
        if session.scalar(select(func.count(CrawlRun.id)).where(CrawlRun.dataset == "demo")):
            return
        now = utcnow().replace(second=0, microsecond=0)
        rng = random.Random(7881)
        for index in range(1009):
            at = now - timedelta(minutes=(1008 - index) * 10)
            wave = math.sin(index / 47) * .00028 + math.sin(index / 13) * .00008 + index * .00000023
            base = Decimal(str(.0529 + wave + rng.uniform(-.000035, .000035)))
            quotes = []
            for side, size in (("merchant", 3), ("player", 14)):
                for n in range(size):
                    price = base - Decimal(n) * Decimal(".000025") if side == "merchant" else base + Decimal(".00035") + Decimal(n) * Decimal(".000018")
                    quotes.append(ParsedQuote(side, f"demo-{side}-{n}",
                                              "演示商家收货报价" if side == "merchant" else f"【演示挂单】金币报价 {n + 1}",
                                              source, 1 / price, price, Decimal(900000 + n * 13000) if side == "merchant" else None,
                                              "模拟数据，不代表真实行情"))
            run = CrawlRun(dataset="demo", started_at=at, origin="demo", engine="demo")
            session.add(run)
            session.flush()
            store_result(session, run, CollectionResult(quotes, 1, "demo", []), at)
