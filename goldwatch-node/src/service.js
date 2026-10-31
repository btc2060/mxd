import Decimal from 'decimal.js';
import { randomUUID } from 'node:crypto';
import { dateSQL, transaction, millis } from './db.js';
import { Collector } from './collector.js';

export const preferenceOptions = row => ({ interval_seconds: row.interval_seconds, enabled: Boolean(row.enabled),
  max_pages: row.max_pages, engine: row.engine, merchant_selector: row.merchant_selector, player_selector: row.player_selector });

export async function newRun(connection, dataset, origin, engine = 'http', at = new Date()) {
  const [result] = await connection.query(`INSERT INTO crawl_runs
    (dataset,started_at,finished_at,status,origin,engine,pages,merchant_count,player_count,error)
    VALUES (?,?,NULL,'running',?,?,0,0,0,'')`, [dataset, dateSQL(at), origin, engine]);
  return result.insertId;
}

export async function storeResult(connection, id, dataset, result, at = new Date()) {
  const status = new Set(result.quotes.map(q => q.side)).size === 2 ? 'success' : 'partial';
  const count = side => result.quotes.filter(q => q.side === side).length;
  await connection.query(`UPDATE crawl_runs SET finished_at=?,status=?,engine=?,pages=?,merchant_count=?,player_count=?,error=? WHERE id=?`,
    [dateSQL(at), status, result.engine, result.pages, count('merchant'), count('player'), result.warnings.join('；').slice(0, 3000), id]);
  if (result.quotes.length) await connection.query(`INSERT INTO quotes
    (run_id,side,source_key,title,source_url,wan_per_yuan,yuan_per_wan,quantity_wan,evidence) VALUES ?`,
    [result.quotes.map(q => [id, q.side, q.source_key, q.title, q.source_url, q.wan_per_yuan, q.yuan_per_wan, q.quantity_wan, q.evidence])]);
  for (const side of ['merchant', 'player']) {
    const prices = result.quotes.filter(q => q.side === side).map(q => new Decimal(q.yuan_per_wan)).sort((a, b) => a.cmp(b));
    if (!prices.length) continue;
    const mid = Math.floor(prices.length / 2);
    const median = prices.length % 2 ? prices[mid] : prices[mid - 1].plus(prices[mid]).div(2);
    const best = side === 'merchant' ? prices.at(-1) : prices[0];
    const worst = side === 'merchant' ? prices[0] : prices.at(-1);
    await connection.query('INSERT INTO snapshots (run_id,dataset,side,at,best,median,worst,count) VALUES (?,?,?,?,?,?,?,?)',
      [id, dataset, side, dateSQL(at), best.toFixed(12), median.toFixed(12), worst.toFixed(12), prices.length]);
  }
}

export class Worker {
  constructor(pool, config, collector = new Collector(config)) {
    this.pool = pool; this.config = config; this.collector = collector; this.owner = randomUUID(); this.task = null;
    this.timer = null; this.controller = null;
  }
  async claim(force = false) {
    return transaction(this.pool, async connection => {
      const now = new Date();
      const [lease] = await connection.query('UPDATE collector_lease SET owner=?,expires_at=? WHERE id=1 AND (expires_at IS NULL OR expires_at<?)',
        [this.owner, dateSQL(Date.now() + 300000), dateSQL(now)]);
      if (lease.affectedRows !== 1) return null;
      const [[prefs]] = await connection.query('SELECT * FROM preferences WHERE id=1 FOR UPDATE');
      const due = prefs.next_due_at == null || millis(prefs.next_due_at) <= now.getTime();
      if (!force && !prefs.requested && !(prefs.enabled && due)) {
        await connection.query('UPDATE collector_lease SET expires_at=? WHERE id=1', [dateSQL(now)]);
        return null;
      }
      await connection.query("UPDATE crawl_runs SET status='failed',finished_at=?,error='上次采集未完成，锁已过期' WHERE dataset=? AND status='running'",
        [dateSQL(now), this.config.mode]);
      const origin = force || prefs.requested ? 'manual' : 'scheduled';
      await connection.query('UPDATE preferences SET requested=0 WHERE id=1');
      const id = await newRun(connection, this.config.mode, origin);
      return { id, options: preferenceOptions(prefs) };
    });
  }
  async once(force = false) {
    const claimed = await this.claim(force);
    if (!claimed) return false;
    this.controller = new AbortController();
    const timer = setTimeout(() => this.controller?.abort(new Error('采集超过 180 秒')), 180000);
    let failure = null;
    try {
      const result = await this.collector.collect(claimed.options, this.controller.signal);
      await transaction(this.pool, connection => storeResult(connection, claimed.id, this.config.mode, result));
    } catch (error) { failure = error; }
    finally {
      clearTimeout(timer); this.controller = null;
      await transaction(this.pool, async connection => {
        const [[prefs]] = await connection.query('SELECT * FROM preferences WHERE id=1 FOR UPDATE');
        const streak = failure ? prefs.failure_streak + 1 : 0;
        if (failure) await connection.query('UPDATE crawl_runs SET status=?,finished_at=?,error=? WHERE id=?',
          [failure.blocked ? 'blocked' : 'failed', dateSQL(new Date()), String(failure.message || failure.name).slice(0, 2000), claimed.id]);
        const seconds = Math.max(prefs.interval_seconds, failure ? Math.min(3600, 60 * 2 ** Math.min(streak, 6)) : 0, failure?.retryAfter || 0);
        await connection.query('UPDATE preferences SET failure_streak=?,next_due_at=? WHERE id=1', [streak, dateSQL(Date.now() + seconds * 1000)]);
        await connection.query('UPDATE collector_lease SET expires_at=? WHERE id=1 AND owner=?', [dateSQL(new Date()), this.owner]);
      });
    }
    return true;
  }
  start() {
    const tick = () => {
      if (this.task) return;
      this.task = this.once().catch(error => console.error('采集调度异常：', error.message)).finally(() => { this.task = null; });
    };
    this.timer = setInterval(tick, 2000); tick();
  }
  async stop() { clearInterval(this.timer); this.controller?.abort(new Error('采集进程停止')); await this.task; await this.collector.close?.(); }
}
