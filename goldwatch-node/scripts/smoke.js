// Disposable local MySQL integration test. Never reads a production .env or contacts 7881.
import { spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:net';
import http from 'node:http';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { readFileSync, mkdirSync } from 'node:fs';
import { chromium } from 'playwright-core';
import mysql from 'mysql2/promise';
import { parseHtml } from '../src/parser.js';

process.env.GOLDWATCH_IGNORE_ENV_FILE = '1';
const [{ database }, { loadConfig }, { Worker, newRun }] = await Promise.all([
  import('../src/db.js'), import('../src/config.js'), import('../src/service.js')
]);
const root = fileURLToPath(new URL('../', import.meta.url));
const name = 'goldwatch-node-test-' + randomBytes(4).toString('hex');
const password = randomBytes(24).toString('hex'), token = randomBytes(24).toString('hex');
const environment = { ...process.env, MYSQL_PASSWORD: password, MYSQL_ROOT_PASSWORD: randomBytes(24).toString('hex'),
  MYSQL_DATABASE: 'goldwatch_node_test', MYSQL_USER: 'app_test', MYSQL_HOST: '127.0.0.1', ADMIN_TOKEN: token,
  GOLDWATCH_IGNORE_ENV_FILE: '1', HOST: '127.0.0.1', COLLECTOR_ENABLED: 'false', MODE: 'live' };
for (const key of ['DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DATABASE_URL']) delete environment[key];
const docker = (...args) => {
  const result = spawnSync('docker', ['--host=unix:///var/run/docker.sock', ...args], { env: environment, encoding: 'utf8', timeout: 30000 });
  return result;
};
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function port() { const server = createServer(); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); const p = server.address().port; await new Promise(resolve => server.close(resolve)); return p; }
let child, output = '', databasePort, appPort;
async function request(url, method = 'GET', body, auth = false) {
  const data = body === undefined ? null : JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const req = http.request(`http://127.0.0.1:${appPort}${url}`, { method, headers: {
      Connection: 'close', ...(data ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } : {}),
      ...(auth ? { Authorization: 'Bearer ' + token } : {}) } }, response => {
      let text = ''; response.setEncoding('utf8'); response.on('data', chunk => text += chunk); response.on('end', () => {
        let json; try { json = JSON.parse(text); } catch {}
        resolve({ status: response.statusCode, text, json });
      });
    });
    req.setTimeout(5000, () => req.destroy(new Error('request timeout'))); req.on('error', reject); req.end(data);
  });
}
async function until(predicate, seconds = 90) { const end = Date.now() + seconds * 1000; while (Date.now() < end) { if (await predicate().catch(() => false)) return; await pause(500); } throw new Error('Local test service readiness timeout'); }
function start(mode = 'live') {
  output = '';
  child = spawn(process.execPath, ['src/server.js'], { cwd: root, env: { ...environment, MODE: mode, MYSQL_PORT: String(databasePort), PORT: String(appPort) }, stdio: ['ignore', 'pipe', 'pipe'] });
  for (const stream of [child.stdout, child.stderr]) stream.on('data', data => { output = (output + data.toString()).slice(-10000); });
}
async function stop() { if (!child || child.exitCode !== null) return; const current = child; await new Promise(resolve => { current.once('exit', resolve); current.kill('SIGTERM'); setTimeout(() => { current.kill('SIGKILL'); }, 11000).unref(); }); child = null; }
const check = (condition, message) => { if (!condition) throw new Error(message); };
try {
  databasePort = await port(); appPort = await port();
  const launched = docker('run', '-d', '--name', name, '-p', `127.0.0.1:${databasePort}:3306`, '-e', 'MYSQL_ROOT_PASSWORD', '-e', 'MYSQL_DATABASE', '-e', 'MYSQL_USER', '-e', 'MYSQL_PASSWORD', 'mysql:8.4');
  check(launched.status === 0, 'Cannot launch disposable MySQL; pull mysql:8.4 first');
  console.log('启动临时 MySQL 8.4，仅本机测试…');
  await until(async () => docker('exec', name, 'mysqladmin', 'ping', '-h', '127.0.0.1', '--silent').status === 0);
  start(); await until(async () => (await request('/api/health')).status === 200);
  check((await request('/api/admin/check', 'POST')).status === 401, 'Admin auth must be required');
  const imported = await request('/api/import-html', 'POST', { html: readFileSync(`${root}tests/quotes.html`, 'utf8') }, true);
  check(imported.status === 200 && imported.json.merchant_count === 2 && imported.json.player_count === 5, 'Both quote regions must import');
  const summary = (await request('/api/summary')).json;
  check(Math.abs(summary.markets.merchant.wan_per_yuan - 18.828) < .00001, 'Merchant best must be highest purchase price');
  check(Math.abs(summary.markets.player.wan_per_yuan - 18.66) < .00001, 'Player best must be lowest sale price');
  check(summary.spread.yuan_per_wan > 0, 'Synchronized spread');
  check((await request('/api/export.csv')).text.includes('商家回收'), 'CSV export');
  check((await request('/api/settings', 'PUT', { interval_seconds: 5, max_pages: 1, enabled: true, engine: 'auto' }, true)).status === 422, 'Reject too-short interval');
  check((await request('/api/settings', 'PUT', { interval_seconds: 300, max_pages: 1, enabled: true, engine: 'auto' }, true)).status === 200, 'Save settings');
  check((await request('/api/collect', 'POST', undefined, true)).status === 503, 'No fake queued collection without a worker');
  check((await request('/api/history?period=5m&span=1d')).json.series.merchant.filter(Boolean).length === 1, 'No fabricated missing history');
  await stop(); start(); await until(async () => (await request('/api/health')).status === 200);
  check((await request('/api/quotes?side=player')).json.rows.length === 5, 'MySQL quotes survive process restart');
  check((await request('/api/summary')).json.preferences.interval_seconds === 300, 'Settings survive restart');
  console.log('原生 Node API、认证、Decimal 报价、MySQL 8.4 和重启持久化：PASS');
  const testConfig = loadConfig({ ...environment, MYSQL_PORT: String(databasePort) });
  const pool = await database(testConfig);
  try {
    const result = { ...parseHtml(readFileSync(`${root}tests/quotes.html`, 'utf8'), testConfig.sourceUrl), pages: 1, engine: 'http' };
    let finish;
    const delayed = new Worker(pool, testConfig, { collect: () => new Promise(resolve => { finish = () => resolve(result); }) });
    const competitor = new Worker(pool, testConfig, { collect: async () => result });
    const task = delayed.once(true);
    await until(async () => Boolean(finish), 10);
    check(await competitor.once(true) === false, 'A second worker must not collect concurrently');
    finish(); await task;
    const failing = new Worker(pool, testConfig, { collect: async () => { throw new Error('test upstream failure'); } });
    await failing.once(true);
    const [[failedPrefs]] = await pool.query('SELECT * FROM preferences WHERE id=1');
    const [[failedRun]] = await pool.query('SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1');
    check(failedPrefs.failure_streak === 1 && failedRun.status === 'failed', 'Record failure and schedule backoff');
    check((await request('/api/quotes?side=player')).json.rows.length === 5, 'Upstream failure must retain last valid prices');
    await newRun(pool, 'live', 'scheduled');
    await competitor.once(true);
    const [[orphans]] = await pool.query("SELECT COUNT(*) AS n FROM crawl_runs WHERE status='running'");
    check(Number(orphans.n) === 0, 'An orphaned collection must recover after lease expiry');
    console.log('MySQL 并发锁、失败退避、旧报价保留、遗留任务恢复：PASS');
  } finally { await pool.end(); }
  const connection = await mysql.createConnection({ host: '127.0.0.1', port: databasePort, user: 'app_test', password, database: 'goldwatch_node_test' });
  await connection.query('UPDATE collector_lease SET owner=?,expires_at=DATE_ADD(UTC_TIMESTAMP(),INTERVAL 5 MINUTE) WHERE id=1', ['test']);
  await connection.end();
  await stop(); start('demo'); await until(async () => (await request('/api/health')).status === 200);
  const executable = process.env.CHROMIUM_PATH || '/usr/bin/chromium';
  const browser = await chromium.launch({ executablePath: executable, headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1150 } });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${appPort}`, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => document.getElementById('merchant-price').textContent !== '—');
    check(await page.locator('#demo-banner').isVisible(), 'Demo must be clearly labelled');
    await page.locator('[data-market="merchant"]').click(); await page.locator('#candle-button').click();
    await page.locator('#unit').selectOption('wan_per_yuan');
    await page.waitForFunction(() => document.getElementById('axis-unit').textContent.includes('万金 / 元'));
    await page.locator('[data-market="both"]').click(); await page.locator('#unit').selectOption('yuan_per_wan');
    await page.waitForFunction(() => document.getElementById('axis-unit').textContent.includes('元 / 万金'));
    if (process.env.CAPTURE_PREVIEW === '1') { mkdirSync(`${root}docs`, { recursive: true }); await page.screenshot({ path: `${root}docs/preview.png` }); }
    await page.setViewportSize({ width: 390, height: 844 }); await page.locator('#settings-top').click();
    check(await page.locator('#settings-dialog').isVisible(), 'Mobile settings available');
    await page.locator('#admin-token').fill(token); await page.locator('#interval-input').fill('600');
    await page.getByRole('button', { name: '保存设置', exact: true }).click(); await page.waitForFunction(() => !document.getElementById('settings-dialog').open);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'No mobile overflow');
    check(errors.length === 0, 'No browser JS errors');
    console.log('演示隔离、真实浏览器、K 线／单位切换、手机设置保存：PASS');
  } finally { await browser.close(); }
} catch (error) {
  console.error(error.message, output.replaceAll(password, '[redacted]').replaceAll(token, '[redacted]')); process.exitCode = 1;
} finally { await stop(); docker('rm', '-f', '-v', name); }
