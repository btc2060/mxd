import Decimal from 'decimal.js';
import { dateSQL, iso, millis } from './db.js';
import { preferenceOptions } from './service.js';

export const PERIODS = { '5m': 300, '30m': 1800, '1h': 3600, '1d': 86400, '1w': 604800 };
export const SPANS = { '1d': 1, '7d': 7, '30d': 30, '90d': 90 };
const num = value => value == null ? null : Number(value);
export const runJSON = row => row ? { id: row.id, started_at: iso(row.started_at), finished_at: iso(row.finished_at),
  status: row.status, origin: row.origin, engine: row.engine, pages: row.pages, merchant_count: row.merchant_count,
  player_count: row.player_count, message: row.error } : null;

export async function summary(pool, config) {
  const now = Date.now();
  const [[prefs]] = await pool.query('SELECT * FROM preferences WHERE id=1');
  const markets = {};
  for (const side of ['merchant', 'player']) {
    const [[latest]] = await pool.query('SELECT * FROM snapshots WHERE dataset=? AND side=? ORDER BY at DESC,id DESC LIMIT 1', [config.mode, side]);
    if (!latest) { markets[side] = null; continue; }
    const [[baseline]] = await pool.query('SELECT best FROM snapshots WHERE dataset=? AND side=? AND at<=? ORDER BY at DESC,id DESC LIMIT 1',
      [config.mode, side, dateSQL(millis(latest.at) - 86400000)]);
    const [[range]] = await pool.query('SELECT MIN(best) AS low,MAX(best) AS high FROM snapshots WHERE dataset=? AND side=? AND at>=?',
      [config.mode, side, dateSQL(now - 86400000)]);
    markets[side] = { run_id: latest.run_id, at: iso(latest.at), yuan_per_wan: num(latest.best),
      wan_per_yuan: new Decimal(1).div(latest.best).toNumber(), median: num(latest.median), count: latest.count,
      change_24h: baseline ? new Decimal(latest.best).div(baseline.best).minus(1).times(100).toNumber() : null,
      high_24h: num(range.high ?? latest.best), low_24h: num(range.low ?? latest.best),
      stale: now - millis(latest.at) > Math.max(prefs.interval_seconds * 2000, 900000) };
  }
  const bid = markets.merchant, ask = markets.player;
  const spread = bid && ask && bid.run_id === ask.run_id && !bid.stale && !ask.stale ? {
    yuan_per_wan: ask.yuan_per_wan - bid.yuan_per_wan, percent: (ask.yuan_per_wan - bid.yuan_per_wan) / ask.yuan_per_wan * 100
  } : null;
  const [[last]] = await pool.query('SELECT * FROM crawl_runs WHERE dataset=? ORDER BY id DESC LIMIT 1', [config.mode]);
  const [[samples]] = await pool.query("SELECT COUNT(*) AS n FROM crawl_runs WHERE dataset=? AND started_at>=? AND status IN ('success','partial')",
    [config.mode, dateSQL(now - 86400000)]);
  return { mode: config.mode, market_name: config.marketName, source_url: config.sourceUrl, server_time: new Date().toISOString(),
    markets, spread, last_run: runJSON(last), samples_24h: Number(samples.n), worker_enabled: config.workerEnabled && config.mode === 'live',
    preferences: { ...preferenceOptions(prefs), next_due_at: iso(prefs.next_due_at), failure_streak: prefs.failure_streak } };
}

export async function quoteRows(pool, dataset, side, limit = 30) {
  const [[latest]] = await pool.query('SELECT * FROM snapshots WHERE dataset=? AND side=? ORDER BY at DESC,id DESC LIMIT 1', [dataset, side]);
  if (!latest) return { at: null, rows: [] };
  const order = side === 'merchant' ? 'DESC' : 'ASC';
  const [rows] = await pool.query(`SELECT * FROM quotes WHERE run_id=? AND side=? ORDER BY yuan_per_wan ${order},id LIMIT ?`, [latest.run_id, side, limit]);
  return { at: iso(latest.at), rows: rows.map(row => ({ id: row.id, title: row.title, source_url: row.source_url,
    wan_per_yuan: num(row.wan_per_yuan), yuan_per_wan: num(row.yuan_per_wan), quantity_wan: num(row.quantity_wan) })) };
}

export function bucketStart(stamp, seconds) {
  if (seconds === 604800) {
    const local = new Date(stamp * 1000 + 28800000);
    const day = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate());
    return (day - ((local.getUTCDay() + 6) % 7) * 86400000 - 28800000) / 1000;
  }
  return Math.floor((stamp + 28800) / seconds) * seconds - 28800;
}

export function aggregate(snapshots, { period = '30m', span = '7d', metric = 'best', unit = 'yuan_per_wan', now = Date.now() } = {}) {
  const requested = PERIODS[period], days = SPANS[span];
  const seconds = Object.values(PERIODS).find(value => value >= requested && days * 86400 / value <= 4000) || 604800;
  const first = bucketStart((now - days * 86400000) / 1000, seconds), last = bucketStart(now / 1000, seconds);
  const grouped = new Map();
  for (const row of snapshots) {
    const stamp = bucketStart(millis(row.at) / 1000, seconds);
    if (stamp < first || stamp > last || millis(row.at) > now) continue;
    const key = `${row.side}:${stamp}`;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(row);
  }
  const times = []; for (let stamp = first; stamp <= last; stamp += seconds) times.push(stamp);
  const series = { merchant: [], player: [] };
  for (const side of Object.keys(series)) for (const stamp of times) {
    const rows = grouped.get(`${side}:${stamp}`);
    if (!rows?.length) { series[side].push(null); continue; }
    rows.sort((a, b) => millis(a.at) - millis(b.at) || (a.id || 0) - (b.id || 0));
    const values = rows.map(row => Number(row[metric]));
    let [open, close, high, low] = [values[0], values.at(-1), Math.max(...values), Math.min(...values)];
    if (unit === 'wan_per_yuan') [open, close, high, low] = [1 / open, 1 / close, 1 / low, 1 / high];
    series[side].push({ open, close, high, low, count: rows.reduce((sum, row) => sum + row.count, 0), samples: rows.length, last_at: iso(rows.at(-1).at) });
  }
  const actual = Object.keys(PERIODS).find(key => PERIODS[key] === seconds);
  return { times: times.map(stamp => new Date(stamp * 1000).toISOString()), series, period: actual, requested_period: period,
    span, metric, unit, note: actual !== period ? '范围较大，已自动使用更大的聚合周期' : '没有采样的时段留空，不补造历史价格' };
}

export async function history(pool, dataset, options) {
  const now = Date.now(), days = SPANS[options.span], requested = PERIODS[options.period];
  const seconds = Object.values(PERIODS).find(value => value >= requested && days * 86400 / value <= 4000) || 604800;
  const from = bucketStart((now - days * 86400000) / 1000, seconds);
  const [rows] = await pool.query('SELECT * FROM snapshots WHERE dataset=? AND at>=? AND at<=? ORDER BY at,id',
    [dataset, dateSQL(from * 1000), dateSQL(now)]);
  return aggregate(rows, { ...options, now });
}
