import { loadConfig } from './config.js';
import { database } from './db.js';
import { Worker } from './service.js';

let pool, worker;
try {
  const config = loadConfig();
  const action = process.argv[2];
  if (!['check-db', 'collect'].includes(action)) throw new Error('使用 npm run db:check 或 npm run collect');
  pool = await database(config);
  if (action === 'check-db') console.log('MySQL 连接和应用表检查通过；没有修改数据库用户密码。');
  else {
    if (config.mode !== 'live') throw new Error('演示模式不会采集真实来源');
    worker = new Worker(pool, config);
    if (!await worker.once(true)) console.log('已有采集任务运行，本次未重复启动。');
    else {
      const [[run]] = await pool.query("SELECT * FROM crawl_runs WHERE dataset='live' ORDER BY id DESC LIMIT 1");
      console.log(`采集结果：${run.status}；商家 ${run.merchant_count} 条，玩家 ${run.player_count} 条；${run.error || '已入库'}`);
      if (['failed', 'blocked'].includes(run.status)) process.exitCode = 1;
    }
  }
} catch (error) { console.error(error.message); process.exitCode = 1; }
finally { await worker?.stop(); await pool?.end(); }
