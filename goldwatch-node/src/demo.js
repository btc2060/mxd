import Decimal from 'decimal.js';
import { createHash } from 'node:crypto';
import { transaction } from './db.js';
import { newRun, storeResult } from './service.js';

export async function seedDemo(pool, config) {
  const [[row]] = await pool.query("SELECT COUNT(*) AS n FROM crawl_runs WHERE dataset='demo'");
  if (Number(row.n)) return;
  await transaction(pool, async connection => {
    const now = Math.floor(Date.now() / 60000) * 60000;
    for (let index = 0; index <= 1008; index++) {
      const at = new Date(now - (1008 - index) * 600000);
      const base = new Decimal(.0529 + Math.sin(index / 47) * .00028 + Math.sin(index / 13) * .00008 + index * .00000023);
      const quotes = [];
      for (const [side, size] of [['merchant', 3], ['player', 14]]) for (let n = 0; n < size; n++) {
        const price = side === 'merchant' ? base.minus(new Decimal(n).times('.000025')) : base.plus('.00035').plus(new Decimal(n).times('.000018'));
        quotes.push({ side, source_key: createHash('sha256').update(`demo-${side}-${n}`).digest('hex'),
          title: side === 'merchant' ? '演示商家收货报价' : `【演示挂单】金币报价 ${n + 1}`, source_url: config.sourceUrl,
          wan_per_yuan: new Decimal(1).div(price).toFixed(12), yuan_per_wan: price.toFixed(12),
          quantity_wan: side === 'merchant' ? String(900000 + n * 13000) : null, evidence: '模拟数据，不代表真实行情' });
      }
      const id = await newRun(connection, 'demo', 'demo', 'demo', at);
      await storeResult(connection, id, 'demo', { quotes, engine: 'demo', pages: 1, warnings: [] }, at);
    }
  });
}
