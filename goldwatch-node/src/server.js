import { loadConfig } from './config.js';
import { database } from './db.js';
import { createApp } from './app.js';
import { Worker } from './service.js';
import { seedDemo } from './demo.js';

let pool, worker, server;
try {
  const config = loadConfig();
  console.log('正在检查现有 MySQL 并初始化应用表，不会重装或修改 MySQL 用户密码。');
  pool = await database(config);
  if (config.mode === 'demo') { console.log('准备明确标记的演示数据…'); await seedDemo(pool, config); }
  const app = createApp(pool, config);
  server = app.listen(config.port, config.host, () => {
    console.log(`Node 服务已启动，监听 ${config.host}:${config.port}，模式 ${config.mode}。`);
    if (config.mode === 'live' && config.workerEnabled) {
      worker = new Worker(pool, config); worker.start();
      console.log('定时采集已启动，默认每 10 分钟一次；来源受限时显示失败，不伪造报价。');
    }
    if (!config.chromium) console.log('未找到 Chromium：公开 HTML 可正常采集，动态区域可能需要安装浏览器。');
    if (process.send) process.send('ready');
  });
  server.on('error', error => { console.error('监听失败：', error.code); shutdown(1); });
} catch (error) {
  console.error('启动失败：', error.message);
  await pool?.end(); process.exitCode = 1;
}

let stopping = false;
async function shutdown(code = 0) {
  if (stopping) return; stopping = true;
  const force = setTimeout(() => process.exit(1), 10000); force.unref();
  server?.close();
  await worker?.stop().catch(error => console.error('停止采集：', error.message));
  await pool?.end();
  clearTimeout(force); process.exit(code);
}
process.once('SIGINT', () => shutdown());
process.once('SIGTERM', () => shutdown());
