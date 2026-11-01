// Read-only diagnostics: no MySQL connection, no settings changes, no persisted quotes.
import { access, readFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { loadConfig, ROOT } from '../src/config.js';
import { browserLaunchOptions, launchBrowser, errorDetails } from '../src/browser.js';
import { Collector } from '../src/collector.js';

let browser, collector, config, stage = '读取项目配置';
try {
  if (process.argv.slice(2).some(arg => arg !== '--source')) throw new Error('使用 npm run doctor，或 npm run doctor -- --source');
  config = loadConfig();
  const version = JSON.parse(await readFile(fileURLToPath(import.meta.resolve('playwright-core/package.json')), 'utf8')).version;
  console.log(`Node：${process.version}；Playwright：${version}`);
  console.log(`项目目录：${ROOT}`);
  console.log(`进程 UID：${process.getuid?.() ?? 'Windows'}；浏览器：${config.chromium || '未找到'}`);
  stage = '检查浏览器执行权限';
  if (!config.chromium) throw new Error('未找到浏览器。请先 cd 到项目目录，再执行 node node_modules/playwright-core/cli.js install --with-deps --no-shell chromium');
  await access(config.chromium, constants.X_OK);
  stage = '启动 Chromium';
  browser = await launchBrowser(browserLaunchOptions(config));
  stage = '渲染本地测试页';
  const page = await browser.newPage();
  await page.setContent('<!doctype html><title>GoldWatch browser check</title><h1>OK</h1>');
  if (await page.title() !== 'GoldWatch browser check') throw new Error('本地页面渲染结果异常');
  console.log(`本地浏览器测试：PASS；Chromium ${browser.version()}`);
  await browser.close(); browser = null;
  if (process.argv.includes('--source')) {
    stage = '检查源站公开报价';
    console.log('读取 robots 和第一页公开报价，默认自动模式；本次不写数据库。');
    collector = new Collector(config);
    const result = await collector.collect({ max_pages: 1, engine: 'auto', merchant_selector: '', player_selector: '' }, AbortSignal.timeout(180000));
    const count = side => result.quotes.filter(q => q.side === side).length;
    console.log(`源站结果：商家 ${count('merchant')} 条，玩家 ${count('player')} 条；方式 ${result.engine}`);
    for (const warning of result.warnings) console.log(`提示：${warning}`);
    if (!count('merchant') || !count('player')) process.exitCode = 1;
  } else console.log('未访问源站。检查实时页面可追加 --source；本地 PASS 不代表源站采集成功。');
} catch (error) {
  console.error(`诊断失败（${stage}）：${errorDetails(error, config)}`);
  process.exitCode = 1;
} finally { await browser?.close().catch(() => {}); await collector?.close().catch(() => {}); }
