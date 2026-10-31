import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from goldwatch.analytics import bucket_start, history, latest_summary
from goldwatch.app import create_app
from goldwatch.collector import CollectionError, CollectionResult, Collector
from goldwatch.config import Environment, SOURCE_URL
from goldwatch.db import CrawlRun, Lease, Preferences, Quote, Snapshot, database, utcnow
from goldwatch.parser import extract_rate, parse_html
from goldwatch.service import Worker, store_result

HTML = Path(__file__).parent.joinpath("fixtures/quotes.html").read_text(encoding="utf-8")


@pytest.fixture
def db(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/test.db")
    yield sessions
    engine.dispose()


def save_fixture(sessions, at=None, dataset="live"):
    with sessions.begin() as session:
        run = CrawlRun(dataset=dataset)
        session.add(run)
        session.flush()
        store_result(session, run, CollectionResult(parse_html(HTML, SOURCE_URL).quotes, 1, "fixture", []), at)
        return run.id


def test_screenshot_quotes_separate_markets_and_recalculate_inverse():
    result = parse_html(HTML, SOURCE_URL)
    assert len(result.quotes) == 7
    bids = [q for q in result.quotes if q.side == "merchant"]
    asks = [q for q in result.quotes if q.side == "player"]
    assert len(bids) == 2 and len(asks) == 5
    assert bids[0].wan_per_yuan == Decimal("18.828")
    assert bids[0].yuan_per_wan == 1 / Decimal("18.828")
    assert bids[0].quantity_wan == Decimal("9946483")
    assert asks[0].wan_per_yuan == Decimal("18.66")
    assert max(q.yuan_per_wan for q in bids) < min(q.yuan_per_wan for q in asks)


def test_only_one_side_is_not_duplicated_to_other_market():
    result = parse_html('<div>1元=18.832万金<a>立即出售</a></div>', SOURCE_URL)
    assert len(result.quotes) == 1 and result.quotes[0].side == "merchant"


def test_nearly_equal_prices_cannot_collapse_two_markets_in_shared_parent():
    html = '<section><div>1元=18.828万金<a>立即出售</a></div><div>1元=18.832万金<a>立即购买</a></div></section>'
    result = parse_html(html, SOURCE_URL)
    assert [(q.side, q.wan_per_yuan) for q in result.quotes] == [("merchant", Decimal("18.828")), ("player", Decimal("18.832"))]


def test_hidden_templates_and_challenges_are_not_quotes():
    result = parse_html('<div hidden>1元=100万金<a>立即出售</a></div><h1>请完成人机验证</h1>', SOURCE_URL)
    assert not result.quotes and result.blocked
    assert extract_rate("价格未知") is None
    assert extract_rate("0元=18万金") is None
    assert extract_rate("1万金=0元") is None
    assert extract_rate("-1元=18万金") is None
    assert extract_rate("-0.05元/万金") is None
    assert extract_rate("1元=18.828万金 1万金=0.9元") is None


def test_custom_css_selector_and_title_only_fallback():
    html = '<div class="custom">186.6万金=10元</div>'
    result = parse_html(html, SOURCE_URL, player_selector=".custom")
    assert result.quotes[0].wan_per_yuan == Decimal("18.66")


def test_colored_dom_spans_do_not_break_currency_units():
    html = '<div><span>1</span>元 = <b>18.828</b>万金 <a>立即出售</a></div>'
    result = parse_html(html, SOURCE_URL)
    assert result.quotes[0].wan_per_yuan == Decimal("18.828")


def test_js_action_uses_public_title_link_and_modal_fragment_keeps_distinct_cards():
    html = '<div><h3><a href="/goods123.html">186.6万金=10元</a></h3><a href="javascript:void(0)">立即购买</a></div>'
    assert parse_html(html, SOURCE_URL).quotes[0].source_url.endswith('/goods123.html')
    html = '<section><div>1元=18.828万金<a href="#sell">立即出售</a></div><div>1元=18.832万金<a href="#sell">立即出售</a></div></section>'
    result = parse_html(html, SOURCE_URL)
    assert len(result.quotes) == 2 and len({q.source_key for q in result.quotes}) == 2


def test_best_bid_highest_price_best_ask_lowest_and_same_run_spread(db):
    save_fixture(db)
    with db() as session:
        data = latest_summary(session, Environment("", worker_enabled=False))
    assert data["markets"]["merchant"]["wan_per_yuan"] == pytest.approx(18.828)
    assert data["markets"]["player"]["wan_per_yuan"] == pytest.approx(18.66)
    assert data["markets"]["merchant"]["change_24h"] is None
    assert data["spread"]["yuan_per_wan"] > 0


def test_no_fill_empty_buckets_and_inverse_ohlc_swaps_high_low(db):
    now = datetime(2026, 10, 31, 8, 6, tzinfo=timezone.utc)
    save_fixture(db, now - timedelta(minutes=5))
    with db() as session:
        data = history(session, "live", "5m", "1d", now=now)
        inverse = history(session, "live", "5m", "1d", unit="wan_per_yuan", now=now)
    assert len([x for x in data["series"]["merchant"] if x]) == 1
    assert data["series"]["merchant"][-1] is None
    price = next(x for x in data["series"]["merchant"] if x)
    rate = next(x for x in inverse["series"]["merchant"] if x)
    assert rate["high"] == pytest.approx(1 / price["low"])
    assert rate["low"] == pytest.approx(1 / price["high"])


def test_weekly_bucket_starts_monday_beijing_and_large_ranges_are_bounded(db):
    stamp = datetime(2026, 10, 31, 0, tzinfo=timezone.utc).timestamp()
    monday = datetime.fromtimestamp(bucket_start(stamp, 604800), timezone(timedelta(hours=8)))
    assert monday.weekday() == 0 and monday.hour == 0
    with db() as session:
        data = history(session, "live", "5m", "90d")
    assert len(data["times"]) < 4002 and data["period"] != "5m"


def test_weekly_first_candle_includes_its_actual_open_even_if_outside_short_span(db):
    now = datetime(2026, 10, 31, 8, tzinfo=timezone.utc)
    monday = datetime.fromtimestamp(bucket_start(now.timestamp(), 604800), timezone.utc)
    save_fixture(db, monday + timedelta(hours=1))
    with db() as session:
        data = history(session, "live", "1w", "1d", now=now)
    assert len([point for point in data["series"]["merchant"] if point]) == 1


def test_distributed_lease_prevents_two_collectors_and_failures_keep_history(db):
    env = Environment("", worker_enabled=False)
    first, second = Worker(db, env), Worker(db, env)
    assert first.claim(force=True)
    assert second.claim(force=True) is None


def test_expired_crashed_job_is_closed_when_next_worker_claims(db):
    env = Environment("", worker_enabled=False)
    old_id, _ = Worker(db, env).claim(force=True)
    with db.begin() as session:
        session.get(Lease, 1).expires_at = utcnow() - timedelta(seconds=1)
    new_id, _ = Worker(db, env).claim(force=True)
    assert new_id != old_id
    with db() as session:
        assert session.get(CrawlRun, old_id).status == "failed"


def test_worker_success_failure_backoff_and_no_fake_zero_quotes(db):
    save_fixture(db)
    class Broken:
        async def collect(self, *_):
            raise CollectionError("HTTP 429", blocked=True, retry_after=1500)
    worker = Worker(db, Environment("", worker_enabled=False), Broken())
    assert asyncio.run(worker.once(force=True))
    with db() as session:
        runs = session.scalars(select(CrawlRun).order_by(CrawlRun.id)).all()
        assert runs[-1].status == "blocked"
        assert session.scalar(select(func.count(Quote.id))) == 7
        assert session.get(Preferences, 1).failure_streak == 1
        assert (session.get(Preferences, 1).next_due_at.replace(tzinfo=timezone.utc) - utcnow()).total_seconds() > 1400


def test_api_auth_settings_import_csv_and_demo_separation(db):
    env = Environment("", admin_token="test-password-123456", worker_enabled=False)
    app = create_app(env, db)
    auth = {"Authorization": "Bearer test-password-123456"}
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"]
        assert client.get("/api/summary").json()["markets"]["merchant"] is None
        assert client.post("/api/admin/check").status_code == 401
        assert client.post("/api/admin/check", headers=auth).status_code == 200
        settings = {"interval_seconds": 300, "enabled": True, "max_pages": 1, "engine": "auto"}
        assert client.put("/api/settings", headers=auth, json=settings).status_code == 200
        assert client.put("/api/settings", headers=auth, json={**settings, "interval_seconds": 5}).status_code == 422
        assert client.post("/api/import-html", headers=auth, json={"html": HTML}).status_code == 200
        summary = client.get("/api/summary").json()
        assert summary["markets"]["merchant"]["count"] == 2
        assert summary["last_run"]["origin"] == "manual-html"
        assert client.get("/api/quotes?side=player").json()["rows"][0]["wan_per_yuan"] == 18.66
        exported = client.get("/api/export.csv")
        assert "text/csv" in exported.headers["Content-Type"] and "商家回收" in exported.text
        assert client.get("/api/history?period=bad").status_code == 422
        assert client.post("/api/collect", headers=auth).status_code == 503
    save_fixture(db, dataset="demo")
    with TestClient(create_app(env, db)) as client:
        assert len(client.get("/api/runs").json()["rows"]) == 1


def test_invalid_selector_or_verification_page_does_not_create_run(db):
    with TestClient(create_app(Environment("", admin_token="test-password-123456", worker_enabled=False), db)) as client:
        auth = {"Authorization": "Bearer test-password-123456"}
        data = {"interval_seconds": 600, "enabled": True, "max_pages": 1, "engine": "auto", "merchant_selector": "[["}
        assert client.put("/api/settings", headers=auth, json=data).status_code == 422
        assert client.post("/api/import-html", headers=auth, json={"html": "<h1>请完成人机验证</h1>"}).status_code == 422
        assert client.get("/api/runs").json()["rows"] == []


def test_manual_collect_during_active_lease_does_not_queue_an_extra_run(db):
    env = Environment("", admin_token="test-password-123456", worker_enabled=True)
    assert Worker(db, env).claim(force=True)
    with TestClient(create_app(env, db)) as client:
        response = client.post("/api/collect", headers={"Authorization": "Bearer test-password-123456"})
        assert response.status_code == 202 and "已有" in response.json()["message"]
    with db() as session:
        assert not session.get(Preferences, 1).requested
