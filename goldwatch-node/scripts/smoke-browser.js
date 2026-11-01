// All page requests are fulfilled/aborted locally. No contact with 7881 or MySQL.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

process.env.GOLDWATCH_IGNORE_ENV_FILE = '1';
const [{ Collector }, { loadConfig }, { launchBrowser }] = await Promise.all([
  import('../src/collector.js'), import('../src/config.js'), import('../src/browser.js')
]);
const config = loadConfig({ MYSQL_PASSWORD: 'local-test-only', CHROMIUM_PATH: process.env.CHROMIUM_PATH });
const fixture = readFileSync(new URL('../tests/quotes.html', import.meta.url), 'utf8') + '<iframe src="about:blank"></iframe>';
function intercepted(fail = false) {
  return async options => {
    const browser = await launchBrowser(options);
    return { close: () => browser.close(), newContext: async settings => {
      const context = await browser.newContext(settings);
      return { route: (...args) => context.route(...args), newPage: async () => {
        const page = await context.newPage();
        await page.route('**/*', route => fail ? route.abort('namenotresolved')
          : route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: fixture }));
        return page;
      } };
    } };
  };
}
const options = { max_pages: 1, engine: 'browser', merchant_selector: '', player_selector: '' };
const collector = new Collector(config, async () => new Response(''), intercepted());
const result = await collector.browser([config.sourceUrl], options, new AbortController().signal, 1);
assert.equal(result.quotes.length, 7); assert.equal(result.quotes.filter(q => q.side === 'merchant').length, 2);
console.log('真实 Chromium、本地拦截报价页、双市场解析、空白 iframe：PASS');
const failing = new Collector(config, async () => new Response(''), intercepted(true));
await assert.rejects(failing.browser([config.sourceUrl], options, new AbortController().signal, 1),
  error => error.message.includes('访问报价页') && error.message.includes('ERR_NAME_NOT_RESOLVED'));
console.log('真实浏览器访问失败的阶段与底层错误：PASS');
const test = spawnSync(process.execPath, ['scripts/doctor.js'], { cwd: new URL('../', import.meta.url),
  env: { ...process.env, MYSQL_PASSWORD: 'local-test-only', DATABASE_URL: '', ADMIN_TOKEN: '', CHROMIUM_PATH: config.chromium }, encoding: 'utf8', timeout: 45000 });
assert.equal(test.status, 0, test.stderr); assert.match(test.stdout, /本地浏览器测试：PASS/); assert.match(test.stdout, /未访问源站/);
assert.ok(!test.stdout.includes('local-test-only'));
console.log('只读浏览器诊断命令、不依赖 MySQL、不访问源站：PASS');
