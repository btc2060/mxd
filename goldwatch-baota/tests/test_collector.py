import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from goldwatch.collector import Collector, CollectionError, CollectionResult
from goldwatch.config import SOURCE_URL
from goldwatch.parser import parse_html

HTML = Path(__file__).parent.joinpath("fixtures/quotes.html").read_text()


def client(handler):
    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


def test_public_html_collects_both_markets_without_launching_browser():
    def handler(request):
        return httpx.Response(200, text="User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else HTML)
    collector = Collector(client(handler))
    collector._browser = AsyncMock(side_effect=AssertionError("browser unnecessary"))
    result = asyncio.run(collector.collect(SOURCE_URL, {"engine": "auto", "max_pages": 1}))
    assert len(result.quotes) == 7 and result.engine == "http"
    collector._browser.assert_not_called()


def test_robots_disallow_is_not_bypassed_using_browser():
    collector = Collector(client(lambda _: httpx.Response(200, text="User-agent: *\nDisallow: /")))
    collector._browser = AsyncMock()
    with pytest.raises(CollectionError, match="robots"):
        asyncio.run(collector.collect(SOURCE_URL, {"engine": "auto"}))
    collector._browser.assert_not_called()


def test_429_and_verification_are_reported_not_retried_in_browser():
    for response in (httpx.Response(429, headers={"Retry-After": "1200"}), httpx.Response(200, text="<h1>请完成安全验证</h1>")):
        def handler(request):
            return httpx.Response(404) if request.url.path == "/robots.txt" else response
        collector = Collector(client(handler))
        collector._browser = AsyncMock()
        with pytest.raises(CollectionError) as error:
            asyncio.run(collector.collect(SOURCE_URL, {"engine": "auto"}))
        assert error.value.blocked
        collector._browser.assert_not_called()


def test_auto_fallback_merges_sides_without_double_counting():
    player_only = '<div>1元=18.66万金<a href="https://www.7881.com/offer.html">立即购买</a></div>'
    def handler(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, text=player_only)
    collector = Collector(client(handler))
    collector._browser = AsyncMock(return_value=CollectionResult(parse_html(HTML, SOURCE_URL).quotes, 1, "browser", []))
    result = asyncio.run(collector.collect(SOURCE_URL, {"engine": "auto"}))
    assert len(result.quotes) == 7 and result.engine == "browser"


def test_repeated_listing_urls_across_pages_are_not_counted_twice():
    def handler(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, text=HTML)
    result = asyncio.run(Collector(client(handler)).collect(SOURCE_URL, {"engine": "http", "max_pages": 2}))
    assert len(result.quotes) == 7 and result.pages == 2
