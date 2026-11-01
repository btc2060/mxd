from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .parser import ParsedQuote, parse_html
from .browser import launch_options, error_details

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
    def __init__(self, client_factory=None, page_setup=None, artifact_dir=None):
        self.robot_cache = {}
        self.page_setup = page_setup
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.visit_delay = 1
        self.client_factory = client_factory or httpx.AsyncClient

    async def robots(self, client, source):
        parsed = urlsplit(source)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self.robot_cache.get(origin)
        if cached is None or monotonic() - cached[0] > 3600:
            try:
                response = await client.get(origin + "/robots.txt", follow_redirects=False, timeout=12)
            except httpx.HTTPError as error:
                raise CollectionError(f"无法检查来源 robots.txt：{error_details(error)}；本次未抓取报价") from error
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
        delay = max(1, robot.crawl_delay(USER_AGENT) or 0)
        if delay > 60:
            raise CollectionError("来源要求较长抓取间隔，暂停本次采集", blocked=True, retry_after=delay)
        return delay

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
            delay = await self.robots(client, source)
            self.visit_delay = delay
            if selected != "browser":
                for url in urls:
                    try:
                        html = await self._http_page(client, url)
                        self.save_html(f"http-page-{pages + 1}.html", html)
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
                            raise CollectionError(f"来源网络请求失败：{error_details(error)}") from error
                        warnings.append(f"页面请求失败：{error_details(error)}")
                    if len(urls) > 1:
                        await asyncio.sleep(delay)
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

    def save_html(self, name, html):
        if self.artifact_dir:
            self.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            content = html if isinstance(html, bytes) else html.encode("utf-8")
            (self.artifact_dir / name).write_bytes(content)

    async def _browser(self, urls, merchant_selector, player_selector):
        stage = "启动 Chromium"
        if os.getenv("BROWSER_ENABLED", "true").lower() != "true":
            raise CollectionError("公开 HTML 未获取两类报价；BROWSER_ENABLED=false，未启动浏览器")
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(**launch_options())
                try:
                    stage = "创建浏览器页面"
                    context = await browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
                    await context.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font") else route.continue_())
                    quotes, warnings, pages = [], [], 0
                    page = await context.new_page()
                    if self.page_setup:
                        await self.page_setup(page)
                    for url in urls:
                        if pages:
                            await asyncio.sleep(self.visit_delay)
                        if not allowed_navigation(url):
                            raise CollectionError("不允许的来源页面地址")
                        stage = "访问报价页"
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                        if response and response.status in (401, 403, 429):
                            raise CollectionError(f"浏览器访问返回 HTTP {response.status}，已停止", blocked=True)
                        if not allowed_navigation(page.url):
                            raise CollectionError("页面跳转到登录或其他地址，已停止自动采集", blocked=True)
                        stage = "等待报价并解析"
                        try:
                            await page.get_by_text(__import__("re").compile("立即购买|立即出售")).first.wait_for(state="visible", timeout=10000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)
                        self.save_html(f"browser-page-{pages + 1}.html", await page.content())
                        if self.artifact_dir:
                            await page.screenshot(path=str(self.artifact_dir / f"browser-page-{pages + 1}.png"))
                        for frame in page.frames[:6]:
                            if urlsplit(frame.url).scheme not in ("http", "https"):
                                continue
                            host = urlsplit(frame.url).hostname or ""
                            if frame != page.main_frame and not (host == "7881.com" or host.endswith(".7881.com")):
                                continue
                            try:
                                await frame.evaluate("""() => { for(const el of document.querySelectorAll('body *')) {
                                  const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden')
                                    el.setAttribute('data-gw-hidden','true'); }}""")
                                content = await frame.content()
                                self.save_html(f"browser-page-{pages + 1}-frame-{page.frames.index(frame)}.html", content)
                                parsed = parse_html(content, frame.url or url, merchant_selector, player_selector)
                            except Exception as error:
                                warnings.append(f"子页面解析失败：{error_details(error)[:500]}")
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
            raise CollectionError(f"浏览器采集未完成（{stage}）：{error_details(error)}") from error
