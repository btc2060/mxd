import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url

from goldwatch.browser import browser_path, error_details
from goldwatch.collector import Collector, CollectionError
from goldwatch.config import Environment, SOURCE_URL
from goldwatch.db import database, Preferences, utcnow
from goldwatch.service import Worker


def test_mysql_fields_encode_password_and_no_silent_fallback(monkeypatch):
    monkeypatch.setenv("GOLDWATCH_IGNORE_ENV_FILE", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MYSQL_PASSWORD", "p@ss:#/special")
    monkeypatch.setenv("ADMIN_TOKEN", "")
    value = Environment.load()
    assert make_url(value.database_url).password == "p@ss:#/special"
    assert value.host == "127.0.0.1" and value.port == 8080
    monkeypatch.setenv("MYSQL_PASSWORD", "")
    with pytest.raises(ValueError, match="MYSQL_PASSWORD"):
        Environment.load()


def test_idle_tick_does_not_write_a_lease(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/idle.db")
    try:
        with sessions.begin() as session:
            session.get(Preferences, 1).next_due_at = utcnow() + timedelta(hours=1)
        statements = []
        event.listen(engine, "before_cursor_execute", lambda _c, _cu, statement, *_: statements.append(statement))
        assert Worker(sessions, Environment("")).claim() is None
        assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    finally:
        engine.dispose()


def test_browser_cache_and_credentials(monkeypatch, tmp_path):
    target = tmp_path / "chromium-1234" / "chrome-linux64" / "chrome"
    target.parent.mkdir(parents=True); target.write_text("fixture")
    for key in ("CHROMIUM_EXECUTABLE_PATH", "CHROMIUM_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert browser_path() == str(target)
    monkeypatch.setenv("MYSQL_PASSWORD", "db-test-secret")
    monkeypatch.setenv("ADMIN_TOKEN", "token-test-secret")
    try:
        try:
            raise OSError("ENOTFOUND host")
        except OSError as cause:
            raise RuntimeError("http://user:proxy-secret@proxy/ db-test-secret token-test-secret") from cause
    except RuntimeError as error:
        text = error_details(error)
    assert "ENOTFOUND" in text
    assert all(secret not in text for secret in ("proxy-secret", "db-test-secret", "token-test-secret"))


def test_disabled_browser_still_collects_public_html(monkeypatch):
    monkeypatch.setenv("BROWSER_ENABLED", "false")
    html = Path(__file__).with_name("fixtures").joinpath("quotes.html").read_text()
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
          text="User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else html)), **kwargs)
    assert len(asyncio.run(Collector(factory).collect(SOURCE_URL, {"engine": "auto"})).quotes) == 7
    with pytest.raises(CollectionError, match="BROWSER_ENABLED"):
        asyncio.run(Collector(factory)._browser([SOURCE_URL], "", ""))


def test_background_collection_runs_without_any_page_visit(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from sqlalchemy import select, func
    from goldwatch.app import create_app
    from goldwatch.collector import CollectionResult
    from goldwatch.parser import parse_html
    from goldwatch.db import Quote
    engine, sessions = database(f"sqlite:///{tmp_path}/background.db")
    fixture = Path(__file__).with_name("fixtures").joinpath("quotes.html").read_text()
    class FixtureCollector:
        async def collect(self, *_):
            return CollectionResult(parse_html(fixture, SOURCE_URL).quotes, 1, "fixture", [])
    try:
        # Entering lifespan starts the worker; no GET/POST request is made.
        with TestClient(create_app(Environment("", worker_enabled=True), sessions, FixtureCollector())):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with sessions() as session:
                    if session.scalar(select(func.count(Quote.id))) == 7:
                        break
                time.sleep(.02)
            else:
                raise AssertionError("Background collection did not persist quotes")
    finally:
        engine.dispose()
