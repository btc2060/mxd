import express from 'express';
import { timingSafeEqual } from 'node:crypto';
import path from 'node:path';
import { ROOT } from './config.js';
import { dateSQL, transaction, millis, iso } from './db.js';
import { summary, quoteRows, history, runJSON, PERIODS, SPANS } from './analytics.js';
import { parseHtml, validateSelector } from './parser.js';
import { newRun, storeResult } from './service.js';

class HttpError extends Error { constructor(status, message) { super(message); this.status = status; } }
const allowed = (value, values, fallback) => {
  const result = value ?? fallback;
  if (!values.includes(result)) throw new HttpError(422, '请求参数不合法');
  return result;
};
const integer = (value, fallback, min, max) => {
  const result = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(result) || result < min || result > max) throw new HttpError(422, '数字参数超出范围');
  return result;
};
function historyOptions(query) {
  return { period: allowed(query.period, Object.keys(PERIODS), '30m'), span: allowed(query.span, Object.keys(SPANS), '7d'),
    metric: allowed(query.metric, ['best', 'median'], 'best'), unit: allowed(query.unit, ['yuan_per_wan', 'wan_per_yuan'], 'yuan_per_wan') };
}

export function createApp(pool, config) {
  const app = express();
  app.disable('x-powered-by');
  app.use((request, response, next) => {
    response.set({ 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'strict-origin-when-cross-origin' });
    if (request.path.startsWith('/api/')) response.set('Cache-Control', 'no-store');
    next();
  });
  app.use(express.json({ limit: '3mb' }));
  function admin(request, _response, next) {
    if (!config.adminToken) return next(new HttpError(503, '服务器尚未配置 ADMIN_TOKEN'));
    const header = request.get('Authorization') || '';
    const supplied = Buffer.from(header.startsWith('Bearer ') ? header.slice(7) : '');
    const expected = Buffer.from(config.adminToken);
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) return next(new HttpError(401, '管理员口令不正确'));
    next();
  }
  app.get('/api/health', async (_request, response) => { await pool.query('SELECT 1'); response.json({ ok: true, mode: config.mode, runtime: 'node' }); });
  app.get('/api/summary', async (_request, response) => response.json(await summary(pool, config)));
  app.get('/api/quotes', async (request, response) => {
    const side = allowed(request.query.side, ['merchant', 'player']);
    response.json(await quoteRows(pool, config.mode, side, integer(request.query.limit, 30, 1, 100)));
  });
  app.get('/api/history', async (request, response) => response.json(await history(pool, config.mode, historyOptions(request.query))));
  app.get('/api/runs', async (request, response) => {
    const [rows] = await pool.query('SELECT * FROM crawl_runs WHERE dataset=? ORDER BY id DESC LIMIT ?', [config.mode, integer(request.query.limit, 20, 1, 100)]);
    response.json({ rows: rows.map(runJSON) });
  });
  app.get('/api/export.csv', async (request, response) => {
    const selected = historyOptions(request.query);
    const data = await history(pool, config.mode, { ...selected, metric: 'best', unit: 'yuan_per_wan' });
    const lines = [['模式', '时间（UTC）', '价格区域', '开盘(元/万金)', '最高', '最低', '收盘', '采样次数', '采样报价条数(非成交量)']];
    for (const [side, series] of Object.entries(data.series)) series.forEach((point, index) => {
      if (point) lines.push([config.mode, data.times[index], side === 'merchant' ? '商家回收' : '玩家挂售', point.open, point.high, point.low, point.close, point.samples, point.count]);
    });
    const csv = '\ufeff' + lines.map(row => row.map(value => '"' + String(value).replaceAll('"', '""') + '"').join(',')).join('\r\n');
    response.set({ 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': `attachment; filename="goldwatch-${selected.span}-${config.mode}.csv"` }).send(csv);
  });
  app.post('/api/admin/check', admin, (_request, response) => response.json({ ok: true }));
  app.put('/api/settings', admin, async (request, response) => {
    const data = request.body || {};
    const fields = ['interval_seconds', 'enabled', 'max_pages', 'engine', 'merchant_selector', 'player_selector'];
    if (Object.keys(data).some(key => !fields.includes(key))) throw new HttpError(422, '存在未知设置字段');
    if (typeof data.interval_seconds !== 'number' || typeof data.max_pages !== 'number' || typeof data.enabled !== 'boolean')
      throw new HttpError(422, '采集间隔、页数或开关格式不合法');
    const interval = integer(data.interval_seconds, null, 60, 86400), pages = integer(data.max_pages, null, 1, 3);
    const engine = allowed(data.engine, ['auto', 'http', 'browser']);
    const selectors = ['merchant_selector', 'player_selector'].map(key => {
      const value = data[key] ?? '';
      if (typeof value !== 'string' || value.length > 400) throw new HttpError(422, 'CSS 选择器过长或格式错误');
      try { if (value.trim()) validateSelector(value.trim()); } catch { throw new HttpError(422, 'CSS 选择器语法无效'); }
      return value.trim();
    });
    await pool.query(`UPDATE preferences SET interval_seconds=?,enabled=?,max_pages=?,engine=?,merchant_selector=?,player_selector=?,next_due_at=? WHERE id=1`,
      [interval, data.enabled ? 1 : 0, pages, engine, ...selectors, dateSQL(Date.now() + interval * 1000)]);
    response.json({ ok: true });
  });
  app.post('/api/collect', admin, async (_request, response) => {
    if (config.mode === 'demo') throw new HttpError(409, '演示模式不抓取真实页面');
    if (!config.workerEnabled) throw new HttpError(503, 'COLLECTOR_ENABLED=false，采集进程未启用');
    const message = await transaction(pool, async connection => {
      const [[prefs]] = await connection.query('SELECT * FROM preferences WHERE id=1 FOR UPDATE');
      const [[lease]] = await connection.query('SELECT * FROM collector_lease WHERE id=1');
      if (lease.expires_at && millis(lease.expires_at) > Date.now()) return '当前已有采集任务运行，完成后刷新看板即可';
      if (prefs.last_manual_at && Date.now() - millis(prefs.last_manual_at) < 60000) throw new HttpError(429, '手动采集至少间隔 60 秒');
      await connection.query('UPDATE preferences SET requested=1,last_manual_at=? WHERE id=1', [dateSQL(new Date())]);
      return '已加入采集队列，通常 2 秒内开始';
    });
    response.status(202).json({ ok: true, message });
  });
  app.post('/api/import-html', admin, async (request, response) => {
    if (config.mode !== 'live') throw new HttpError(409, '演示模式不可导入真实报价');
    if (typeof request.body?.html !== 'string' || request.body.html.length < 10 || request.body.html.length > 2000000)
      throw new HttpError(422, 'HTML 内容不合法或超过 2MB');
    const result = await transaction(pool, async connection => {
      const [[prefs]] = await connection.query('SELECT * FROM preferences WHERE id=1');
      let parsed;
      try { parsed = parseHtml(request.body.html, config.sourceUrl, prefs.merchant_selector, prefs.player_selector); }
      catch { throw new HttpError(422, '解析失败，请检查 HTML 和选择器'); }
      if (parsed.blocked || !parsed.quotes.length) throw new HttpError(422, '没有识别到公开报价，验证页面不能导入');
      const id = await newRun(connection, 'live', 'manual-html', 'html-import');
      await storeResult(connection, id, 'live', { ...parsed, pages: 1, engine: 'html-import' });
      return { ok: true, merchant_count: parsed.quotes.filter(q => q.side === 'merchant').length,
        player_count: parsed.quotes.filter(q => q.side === 'player').length };
    });
    response.json(result);
  });
  app.use('/static', express.static(path.join(ROOT, 'static'), { dotfiles: 'deny', maxAge: '1h' }));
  app.get('/', (_request, response) => response.set('Cache-Control', 'no-cache').sendFile(path.join(ROOT, 'static/index.html')));
  app.use((_request, response) => response.status(404).json({ detail: '地址不存在' }));
  app.use((error, _request, response, _next) => {
    const status = error.status || 500;
    if (status >= 500) console.error('请求处理失败：', error.code || error.name);
    response.status(status).json({ detail: status >= 500 && !(error instanceof HttpError) ? '服务暂时不可用，请检查服务端日志和数据库连接' : error.message });
  });
  return app;
}
