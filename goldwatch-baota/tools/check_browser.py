"""Offline browser verification: every request is intercepted; no 7881 or MySQL access."""
import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goldwatch.collector import Collector, CollectionError
from goldwatch.config import SOURCE_URL


async def main():
    fixture = Path(__file__).resolve().parent.parent.joinpath("tests/fixtures/quotes.html").read_text()
    async def fixture_page(page):
        await page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                                             body=fixture + '<iframe src="about:blank"></iframe>'))
    result = await Collector(page_setup=fixture_page)._browser([SOURCE_URL], "", "")
    assert len(result.quotes) == 7 and sum(q.side == "merchant" for q in result.quotes) == 2
    print("真实 Chromium／本地报价页／双市场解析／空白 iframe：PASS")
    async def failing_page(page):
        await page.route("**/*", lambda route: route.abort("namenotresolved"))
    try:
        await Collector(page_setup=failing_page)._browser([SOURCE_URL], "", "")
    except CollectionError as error:
        assert "访问报价页" in str(error) and "ERR_NAME_NOT_RESOLVED" in str(error)
    else:
        raise AssertionError("Expected navigation failure")
    print("浏览器失败阶段和底层原因：PASS")


if __name__ == "__main__":
    asyncio.run(main())
