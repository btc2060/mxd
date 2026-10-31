from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .parser import ParsedQuote, parse_html

log = logging.getLogger(__name__)
USER_AGENT = "MapleGoldWatch/1.0 (public game-currency quote monitor)"


class CollectionError(Exception):
    def __init__(self, message, blocked=False, retry_after=0):
        super().__init__(message)
        self.blocked, self.retry_after = blocked, retry_after


@dataclass
class CollectionResult:
    quotes: list[ParsedQuote]
    pages: int
    engine: str
    warnings: list[str]


def page_url(source, number):
    parsed = urlsplit(source)
    query = parse_qs(parsed.query)
    query["pageNum"] = [str(number)]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))


def allowed_navigation(url):
    parsed = urlsplit(url)
    return parsed.scheme in ("https", "http") and parsed.hostname == "search.7881.com" and not parsed.username


class Collector:
    def __init__(self, client_factory=None):
        self.robot_cache = {}
        self.client_factory = client_factory or httpx.AsyncClient

    async def robots(self, client, source):
        parsed = urlsplit(source)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self.robot_cache.get(origin)
        if cached is None or monotonic() - cached[0] > 3600:
            try:
                response = await client.get(origin + "/robots.txt", follow_redirects=False, timeout=12)
            except httpx.HTTPError as error:
                raise CollectionError(f"无法检查来源 robots.txt：{type(error).__name__}；本次未抓取报价") from error
            if response.status_code in (401, 403, 429) or response.status_code >= 500:
                raise CollectionError(f"来源 robots.txt 返回 HTTP {response.status_code}，暂停采集", blocked=True)
            if 300 <= response.status_code < 400:
                raise CollectionError("robots.txt 重定向，需核实来源访问规则后再接入", blocked=True)
            robot = RobotFileParser()
            robot.parse(response.text.splitlines() if response.status_code == 200 else [])
            self.robot_cache[origin] = monotonic(), robot
        robot = self.robot_cache[origin][1]
        if not robot.can_fetch(USER_AGENT, source):
            raise CollectionError("来源 robots.txt 不允许该路径的自动采集", blocked=True)

    async def _http_page(self, client, url):
        response = await client.get(url, follow_redirects=False)
        if response.status_code in (401, 403, 429):
            retry = response.headers.get("Retry-After", "")
            raise CollectionError(f"来源返回 HTTP {response.status_code}，未绕过访问限制", blocked=True,
                                  retry_after=min(int(retry), 86400) if retry.isdigit() else 0)
        if 300 <= response.status_code < 400:
            destination = response.headers.get("location", "")
            raise CollectionError("来源重定向，可能需要登录或来源地址已变化；未采集重定向后的内容", blocked=True)
        response.raise_for_status()
        if len(response.content) > 5_000_000:
            raise CollectionError("来源页面超过 5MB，未解析")
        return response.content

    async def collect(self, source, options):
        quotes, warnings, engine = [], [], "http"
        pages = 0
        selected = options.get("engine", "auto")
        merchant_selector = options.get("merchant_selector", "")
        player_selector = options.get("player_selector", "")
        urls = [page_url(source, number) for number in range(1, options.get("max_pages", 1) + 1)]
        async with self.client_factory(timeout=25, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}) as client:
            await self.robots(client, source)
            if selected != "browser":
                for url in urls:
                    try:
                        html = await self._http_page(client, url)
                        parsed = parse_html(html, url, merchant_selector, player_selector)
                        if parsed.blocked:
                            raise CollectionError("来源显示人机／安全验证，需要人工检查；未绕过验证", blocked=True)
                        # The same merchant sidebar repeats on every listing page.
                        had_merchants = any(q.side == "merchant" for q in quotes)
                        quotes.extend(q for q in parsed.quotes if q.side != "merchant" or not had_merchants)
                        warnings.extend(parsed.warnings)
                        pages += 1
                    except CollectionError:
                        raise
                    except httpx.HTTPError as error:
                        if not quotes and selected == "http":
                            raise CollectionError(f"来源网络请求失败：{type(error).__name__}") from error
                        warnings.append(f"页面请求失败：{type(error).__name__}")
                    if len(urls) > 1:
                        await asyncio.sleep(1)
        if selected == "browser" or selected == "auto" and {q.side for q in quotes} != {"merchant", "player"}:
            try:
                browser = await self._browser(urls, merchant_selector, player_selector)
                if browser.quotes:
                    # Replace successful sides, never count the same cards twice across engines.
                    sides = {q.side for q in browser.quotes}
                    quotes = [q for q in quotes if q.side not in sides] + browser.quotes
                    pages = max(pages, browser.pages)
                    engine = "browser"
                    warnings.extend(browser.warnings)
            except CollectionError as error:
                if error.blocked or not quotes:
                    raise
                warnings.append(str(error))
        unique = {(q.side, q.source_key): q for q in quotes}
        if not unique:
            raise CollectionError("没有解析到报价。请检查原网页、浏览器依赖或设置中的 CSS 选择器；没有写入零价格")
        if {q.side for q in unique.values()} != {"merchant", "player"}:
            warnings.append("本次只获取一个价格区域，另一区域未更新；没有用其中一个价格代替另一个")
        return CollectionResult(list(unique.values()), pages, engine, list(dict.fromkeys(warnings)))

    async def _browser(self, urls, merchant_selector, player_selector):
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                executable = os.getenv("CHROMIUM_EXECUTABLE_PATH")
                browser = await playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"],
                                                            **({"executable_path": executable} if executable else {}))
                try:
                    context = await browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
                    await context.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font") else route.continue_())
                    quotes, warnings, pages = [], [], 0
                    page = await context.new_page()
                    for url in urls:
                        if not allowed_navigation(url):
                            raise CollectionError("不允许的来源页面地址")
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                        if response and response.status in (401, 403, 429):
                            raise CollectionError(f"浏览器访问返回 HTTP {response.status}，已停止", blocked=True)
                        if not allowed_navigation(page.url):
                            raise CollectionError("页面跳转到登录或其他地址，已停止自动采集", blocked=True)
                        try:
                            await page.get_by_text(__import__("re").compile("立即购买|立即出售")).first.wait_for(state="visible", timeout=10000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)
                        for frame in page.frames[:6]:
                            host = urlsplit(frame.url).hostname or ""
                            if frame != page.main_frame and not (host == "7881.com" or host.endswith(".7881.com")):
                                continue
                            try:
                                await frame.evaluate("""() => { for(const el of document.querySelectorAll('body *')) {
                                  const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden')
                                    el.setAttribute('data-gw-hidden','true'); }}""")
                                parsed = parse_html(await frame.content(), frame.url or url, merchant_selector, player_selector)
                            except Exception:
                                continue
                            if parsed.blocked:
                                raise CollectionError("来源出现验证页面，等待人工检查；未自动操作验证", blocked=True)
                            had_merchants = any(q.side == "merchant" for q in quotes)
                            quotes.extend(q for q in parsed.quotes if q.side != "merchant" or not had_merchants)
                            if parsed.quotes:
                                warnings.extend(parsed.warnings)
                        pages += 1
                    return CollectionResult(quotes, pages, "browser", warnings)
                finally:
                    await browser.close()
        except CollectionError:
            raise
        except Exception as error:
            raise CollectionError(f"浏览器采集未完成：{type(error).__name__}；请确认已安装对应版本 Chromium 或使用 Docker 镜像") from error
