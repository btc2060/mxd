import robotsParser from 'robots-parser';
import { chromium } from 'playwright-core';
import { parseHtml } from './parser.js';
import { EnvHttpProxyAgent, fetch as proxyFetch } from 'undici';

export const USER_AGENT = 'MapleGoldWatch/1.0 (public game-currency quote monitor)';
export class CollectionError extends Error {
  constructor(message, { blocked = false, retryAfter = 0 } = {}) { super(message); this.blocked = blocked; this.retryAfter = retryAfter; }
}
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

export class Collector {
  constructor(config, request = null) {
    this.config = config; this.robots = null;
    this.agent = !request && (process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.https_proxy || process.env.http_proxy)
      ? new EnvHttpProxyAgent() : null;
    this.request = request || ((url, options) => proxyFetch(url, { ...options, ...(this.agent ? { dispatcher: this.agent } : {}) }));
  }
  async close() { await this.agent?.close(); }
  async get(url, signal) {
    let response;
    try { response = await this.request(url, { signal: AbortSignal.any([signal, AbortSignal.timeout(25000)]), redirect: 'manual',
      headers: { 'User-Agent': USER_AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9' } }); }
    catch (error) { throw new CollectionError(`来源网络请求未完成：${error.name}`); }
    return response;
  }
  async robotCheck(signal) {
    if (!this.robots || Date.now() - this.robots.at > 3600000) {
      const url = new URL('/robots.txt', this.config.sourceUrl).href;
      const response = await this.get(url, signal);
      if ([401, 403, 429].includes(response.status) || response.status >= 500 || response.status >= 300 && response.status < 400)
        throw new CollectionError(`robots.txt 返回 HTTP ${response.status}，暂停本次采集`, { blocked: true });
      this.robots = { at: Date.now(), parser: robotsParser(url, response.status === 200 ? await response.text() : '') };
    }
    if (this.robots.parser.isAllowed(this.config.sourceUrl, USER_AGENT) === false)
      throw new CollectionError('来源 robots.txt 不允许此路径的自动采集', { blocked: true });
    return Math.max(1, this.robots.parser.getCrawlDelay(USER_AGENT) || 0);
  }
  async collect(options, signal = new AbortController().signal) {
    const delay = await this.robotCheck(signal);
    if (delay > 60) throw new CollectionError('来源要求较长抓取间隔，请人工核实采集规则', { blocked: true, retryAfter: delay });
    const urls = Array.from({ length: options.max_pages }, (_, index) => {
      const url = new URL(this.config.sourceUrl); url.searchParams.set('pageNum', String(index + 1)); return url.href;
    });
    let quotes = [], pages = 0, engine = 'http';
    const warnings = [];
    if (options.engine !== 'browser') {
      for (const url of urls) {
        if (pages) await pause(delay * 1000);
        signal.throwIfAborted();
        const response = await this.get(url, signal);
        if ([401, 403, 429].includes(response.status)) throw new CollectionError(`来源返回 HTTP ${response.status}，未绕过访问限制`, {
          blocked: true, retryAfter: Math.min(Number(response.headers.get('retry-after')) || 0, 86400) });
        if (response.status >= 300 && response.status < 400) throw new CollectionError('来源发生重定向，可能需要登录，已停止', { blocked: true });
        if (!response.ok) throw new CollectionError(`来源 HTTP ${response.status}`);
        const bytes = Buffer.from(await response.arrayBuffer());
        if (bytes.length > 5000000) throw new CollectionError('来源页面超过 5MB，未解析');
        const parsed = parseHtml(bytes, url, options.merchant_selector, options.player_selector);
        if (parsed.blocked) throw new CollectionError('来源显示验证页面，需要人工检查，未自动操作验证', { blocked: true });
        const hadBid = quotes.some(q => q.side === 'merchant');
        quotes.push(...parsed.quotes.filter(q => q.side !== 'merchant' || !hadBid));
        warnings.push(...parsed.warnings); pages++;
      }
    }
    if (options.engine === 'browser' || options.engine === 'auto' && new Set(quotes.map(q => q.side)).size !== 2) {
      if (!this.config.chromium) {
        if (!quotes.length) throw new CollectionError('公开 HTML 未解析到报价，且未配置 Chromium；请安装浏览器或调整 CSS 选择器');
        warnings.push('未配置 Chromium，动态报价区域未更新');
      } else {
        try {
          const result = await this.browser(urls, options, signal, delay);
          if (result.quotes.length) {
            const sides = new Set(result.quotes.map(q => q.side));
            quotes = quotes.filter(q => !sides.has(q.side)).concat(result.quotes);
            pages = Math.max(pages, result.pages); engine = 'browser'; warnings.push(...result.warnings);
          }
        } catch (error) {
          if (error.blocked || !quotes.length) throw error;
          warnings.push(error.message);
        }
      }
    }
    quotes = [...new Map(quotes.map(q => [`${q.side}:${q.source_key}`, q])).values()];
    if (!quotes.length) throw new CollectionError('未解析到公开报价，没有写入零价格；请检查源站和选择器');
    if (new Set(quotes.map(q => q.side)).size !== 2) warnings.push('本次只更新一个报价区域，另一区域保留原时间，不互相替代');
    return { quotes, pages, engine, warnings: [...new Set(warnings)] };
  }
  async browser(urls, options, signal, delay) {
    let browser;
    const stop = () => { browser?.close().catch(() => {}); };
    try {
      signal.throwIfAborted();
      const proxyUrl = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.https_proxy || process.env.http_proxy;
      const proxy = proxyUrl ? new URL(proxyUrl) : null;
      browser = await chromium.launch({ headless: true, executablePath: this.config.chromium, args: ['--disable-dev-shm-usage'],
        ...(proxy ? { proxy: { server: `${proxy.protocol}//${proxy.host}`, ...(proxy.username ? {
          username: decodeURIComponent(proxy.username), password: decodeURIComponent(proxy.password) } : {}) } } : {}) });
      signal.addEventListener('abort', stop, { once: true });
      signal.throwIfAborted();
      const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1440, height: 1000 } });
      await context.route('**/*', route => ['image', 'media', 'font'].includes(route.request().resourceType()) ? route.abort() : route.continue());
      const page = await context.newPage(); const quotes = [], warnings = []; let pages = 0;
      for (const url of urls) {
        if (pages) await pause(delay * 1000);
        signal.throwIfAborted();
        const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 35000 });
        if ([401, 403, 429].includes(response?.status())) throw new CollectionError(`浏览器访问返回 HTTP ${response.status()}`, { blocked: true });
        if (new URL(page.url()).hostname !== 'search.7881.com') throw new CollectionError('页面跳转到其他站点或登录页，已停止', { blocked: true });
        await page.getByText(/立即购买|立即出售/).first().waitFor({ state: 'visible', timeout: 10000 }).catch(() => {});
        await page.waitForTimeout(1500);
        for (const frame of page.frames().slice(0, 6)) {
          const host = new URL(frame.url()).hostname;
          if (frame !== page.mainFrame() && host !== '7881.com' && !host.endsWith('.7881.com')) continue;
          await frame.evaluate(() => { for (const element of document.querySelectorAll('body *')) {
            const style = getComputedStyle(element); if (style.display === 'none' || style.visibility === 'hidden') element.setAttribute('data-gw-hidden', 'true');
          } }).catch(() => {});
          const parsed = parseHtml(await frame.content(), frame.url() || url, options.merchant_selector, options.player_selector);
          if (parsed.blocked) throw new CollectionError('来源出现验证页面，未自动操作验证', { blocked: true });
          const hadBid = quotes.some(q => q.side === 'merchant');
          quotes.push(...parsed.quotes.filter(q => q.side !== 'merchant' || !hadBid)); warnings.push(...parsed.warnings);
        }
        pages++;
      }
      return { quotes, pages, warnings };
    } catch (error) {
      if (error instanceof CollectionError) throw error;
      throw new CollectionError(`Chromium 采集失败：${error.name}，请检查 CHROMIUM_PATH 和系统浏览器依赖`);
    } finally { signal.removeEventListener('abort', stop); await browser?.close().catch(() => {}); }
  }
}
