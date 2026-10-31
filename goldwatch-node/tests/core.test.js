import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parseHtml, extractRate, safeUrl } from '../src/parser.js';
import { loadConfig, SOURCE_URL } from '../src/config.js';
import { aggregate, bucketStart } from '../src/analytics.js';
import { iso } from '../src/db.js';
import { Collector } from '../src/collector.js';

const html = readFileSync(new URL('./quotes.html', import.meta.url), 'utf8');
const options = { max_pages: 1, engine: 'auto', merchant_selector: '', player_selector: '' };
const config = { sourceUrl: SOURCE_URL, chromium: '' };

test('截图报价分成两类，倒数与收货数量正确', () => {
  const { quotes } = parseHtml(html, SOURCE_URL);
  assert.equal(quotes.length, 7);
  assert.equal(quotes.filter(q => q.side === 'merchant').length, 2);
  assert.equal(quotes.filter(q => q.side === 'player').length, 5);
  assert.equal(quotes[0].wan_per_yuan, '18.828000000000');
  assert.ok(Math.abs(Number(quotes[0].yuan_per_wan) - 1 / 18.828) < 1e-11);
  assert.equal(quotes[0].quantity_wan, '9946483.0000');
});
test('相近回收价和挂售价不被共同父节点混合', () => {
  const parsed = parseHtml('<section><div>1元=18.828万金<a>立即出售</a></div><div>1元=18.832万金<a>立即购买</a></div></section>', SOURCE_URL);
  assert.deepEqual(parsed.quotes.map(q => q.wan_per_yuan), ['18.828000000000', '18.832000000000']);
});
test('隐藏模板和验证码不是报价，不保存负值和零价', () => {
  const parsed = parseHtml('<div hidden>1元=999万金<a>立即出售</a></div><h1>请完成人机验证</h1>', SOURCE_URL);
  assert.equal(parsed.blocked, true); assert.equal(parsed.quotes.length, 0);
  for (const value of ['0元=10万金', '-1元=10万金', '1万金=0元', '-0.05元/万金', '价格未知', '1元=18.828万金 1万金=0.9元']) assert.equal(extractRate(value), null);
});
test('彩色 DOM 标签拆开的数字和单位能解析', () => {
  assert.equal(parseHtml('<div><span>1</span>元=<b>18.828</b>万金<a>立即出售</a></div>', SOURCE_URL).quotes[0].wan_per_yuan, '18.828000000000');
});
test('JS 按钮使用公开标题链接，重复弹窗链接不会合并不同卡片', () => {
  const item = parseHtml('<div><h3><a href="/goods123.html">186.6万金=10元</a></h3><a href="javascript:void(0)">立即购买</a></div>', SOURCE_URL);
  assert.ok(item.quotes[0].source_url.endsWith('/goods123.html'));
  const result = parseHtml('<section><div>1元=18.828万金<a href="#sell">立即出售</a></div><div>1元=18.832万金<a href="#sell">立即出售</a></div></section>', SOURCE_URL);
  assert.equal(new Set(result.quotes.map(q => q.source_key)).size, 2);
  assert.equal(safeUrl('javascript:alert(1)', SOURCE_URL), SOURCE_URL);
  assert.equal(safeUrl('https://example.com', SOURCE_URL), SOURCE_URL);
});
test('CSS 可解析无按钮卡片，整列选择器不会合成一条报价', () => {
  assert.equal(parseHtml('<div class="card">186.6万金=10元</div>', SOURCE_URL, '', '.card').quotes[0].wan_per_yuan, '18.660000000000');
  assert.equal(parseHtml(html, SOURCE_URL, 'aside', '.nothing').quotes.length, 0);
});
test('MySQL 独立字段无需 URL 编码，也能复用已有连接 URL', () => {
  const c = loadConfig({ MYSQL_PASSWORD: 'a@b#c:pass', ADMIN_TOKEN: 'local-test-token-123456' });
  assert.equal(c.mysql.password, 'a@b#c:pass');
  const u = loadConfig({ DATABASE_URL: 'mysql+pymysql://gold:p%40ss@localhost:3307/old', ADMIN_TOKEN: 'local-test-token-123456' });
  assert.equal(u.mysql.password, 'p@ss'); assert.equal(u.mysql.port, 3307); assert.equal(u.mysql.database, 'old');
});
test('缺少数据库密码和短口令清楚报错，不自动替换真实密码', () => {
  assert.throws(() => loadConfig({}), /MYSQL_PASSWORD/);
  assert.throws(() => loadConfig({ MYSQL_PASSWORD: 'x', ADMIN_TOKEN: 'short' }), /管理员口令/);
  assert.throws(() => loadConfig({ MYSQL_PASSWORD: 'x', SOURCE_URL: 'https://example.com/' }), /SOURCE_URL/);
});
test('UTC 和 MySQL DATETIME 在所有服务器时区下一致', () => {
  assert.equal(iso('2026-10-31 08:10:00.000'), '2026-10-31T08:10:00.000Z');
  assert.equal(iso('2026-10-31T08:10:00+00:00'), '2026-10-31T08:10:00.000Z');
});
test('未采样的 5 分钟桶为空，不把十分钟采集伪造成五分钟历史', () => {
  const now = Date.parse('2026-10-31T08:06:00Z');
  const data = aggregate([{ at: '2026-10-31 08:01:00', side: 'merchant', best: '0.05', count: 2 }], { now, period: '5m', span: '1d' });
  assert.equal(data.series.merchant.filter(Boolean).length, 1); assert.equal(data.series.merchant.at(-1), null);
});
test('倒数 K 线交换高低，开收顺序不颠倒', () => {
  const now = Date.parse('2026-10-31T08:29:00Z');
  const points = [{ at: '2026-10-31 08:01:00', side: 'merchant', best: '.05', count: 2 }, { at: '2026-10-31 08:11:00', side: 'merchant', best: '.06', count: 3 }];
  const data = aggregate(points, { now, period: '30m', span: '1d', unit: 'wan_per_yuan' }).series.merchant.filter(Boolean)[0];
  assert.equal(data.open, 20); assert.equal(data.high, 20); assert.equal(data.low, 1 / .06); assert.equal(data.count, 5);
});
test('周线按北京时间周一开始，大范围不返回过多点', () => {
  const now = Date.parse('2026-10-31T08:06:00Z');
  const monday = new Date(bucketStart(now / 1000, 604800) * 1000 + 28800000);
  assert.equal(monday.getUTCDay(), 1); assert.equal(monday.getUTCHours(), 0);
  const data = aggregate([], { period: '5m', span: '90d', now });
  assert.ok(data.times.length < 4002); assert.notEqual(data.period, '5m');
});
const mock = handler => async url => handler(new URL(url));
test('公开 HTML 成功无需浏览器，robots 和 429 不通过浏览器绕过', async () => {
  const collector = new Collector(config, mock(url => new Response(url.pathname === '/robots.txt' ? 'User-agent: *\nAllow: /' : html)));
  assert.equal((await collector.collect(options)).quotes.length, 7);
  const forbidden = new Collector(config, mock(() => new Response('User-agent: *\nDisallow: /')));
  await assert.rejects(forbidden.collect(options), /robots/);
  const blocked = new Collector(config, mock(url => url.pathname === '/robots.txt' ? new Response('', { status: 404 }) : new Response('', { status: 429 })));
  await assert.rejects(blocked.collect(options), /429/);
});
test('单个区域不复制成另一区域，没有浏览器时明确标记缺失', async () => {
  const collector = new Collector(config, mock(url => url.pathname === '/robots.txt' ? new Response('', { status: 404 }) : new Response('<div>1元=18.828万金<a>立即出售</a></div>')));
  const data = await collector.collect(options);
  assert.equal(data.quotes.length, 1); assert.equal(data.quotes[0].side, 'merchant'); assert.ok(data.warnings.some(w => w.includes('Chromium')));
});
test('跨页相同明细和重复商家栏不会双计数', async () => {
  const collector = new Collector(config, mock(url => url.pathname === '/robots.txt' ? new Response('', { status: 404 }) : new Response(html)));
  const data = await collector.collect({ ...options, max_pages: 2 });
  assert.equal(data.pages, 2); assert.equal(data.quotes.length, 7);
});
