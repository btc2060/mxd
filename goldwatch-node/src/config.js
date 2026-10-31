import dotenv from 'dotenv';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { existsSync, readdirSync } from 'node:fs';

export const ROOT = fileURLToPath(new URL('../', import.meta.url));
if (process.env.GOLDWATCH_IGNORE_ENV_FILE !== '1') dotenv.config({ path: path.join(ROOT, '.env'), override: false });
export const SOURCE_URL = 'https://search.7881.com/G6344-100001-G6344P002-G6344P002005-0.html?pageNum=1';

export function loadConfig(environment = process.env) {
  const mode = environment.MODE || 'live';
  if (!['live', 'demo'].includes(mode)) throw new Error('MODE 只能是 live 或 demo');
  const port = Number(environment.PORT || 8080);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('PORT 必须为 1024～65535');
  const source = new URL(environment.SOURCE_URL || SOURCE_URL);
  if (source.protocol !== 'https:' || source.hostname !== 'search.7881.com' || source.username || source.password)
    throw new Error('SOURCE_URL 仅支持 search.7881.com 的公开 HTTPS 页面');
  let mysql = {
    host: environment.MYSQL_HOST || '127.0.0.1', port: Number(environment.MYSQL_PORT || 3306),
    database: environment.MYSQL_DATABASE || 'goldwatch', user: environment.MYSQL_USER || 'goldwatch',
    password: environment.MYSQL_PASSWORD || ''
  };
  if (environment.DATABASE_URL) {
    const parsed = new URL(environment.DATABASE_URL.replace(/^mysql\+pymysql:/, 'mysql:'));
    if (parsed.protocol !== 'mysql:') throw new Error('DATABASE_URL 必须是 MySQL 连接地址');
    mysql = { host: parsed.hostname, port: Number(parsed.port || 3306), database: decodeURIComponent(parsed.pathname.slice(1)),
      user: decodeURIComponent(parsed.username), password: decodeURIComponent(parsed.password) };
  }
  if (!mysql.password || mysql.password.includes('请填写')) throw new Error('请先在 .env 的 MYSQL_PASSWORD 填写宝塔数据库实际密码');
  if (!mysql.database || !mysql.user || !mysql.host || !Number.isInteger(mysql.port) || mysql.port < 1 || mysql.port > 65535)
    throw new Error('MySQL 地址、端口、用户名或库名不完整');
  const adminToken = environment.ADMIN_TOKEN || '';
  if (adminToken && (adminToken.length < 16 || adminToken.startsWith('run-npm-'))) throw new Error('请运行 npm run setup 生成管理员口令，或设置至少 16 位随机 ADMIN_TOKEN');
  const browserRoot = environment.PLAYWRIGHT_BROWSERS_PATH && path.isAbsolute(environment.PLAYWRIGHT_BROWSERS_PATH)
    ? environment.PLAYWRIGHT_BROWSERS_PATH : path.join(ROOT, environment.PLAYWRIGHT_BROWSERS_PATH || '.browsers');
  const downloaded = existsSync(browserRoot) ? readdirSync(browserRoot).filter(name => /^chromium-\d+$/.test(name))
    .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]))
    .flatMap(name => [path.join(browserRoot, name, 'chrome-linux64/chrome'), path.join(browserRoot, name, 'chrome-linux/chrome')]) : [];
  const chromium = environment.CHROMIUM_PATH || ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome', ...downloaded].find(existsSync) || '';
  return { host: environment.HOST || '127.0.0.1', port, mode, mysql, adminToken, sourceUrl: source.href,
    workerEnabled: environment.COLLECTOR_ENABLED !== 'false', chromium,
    marketName: environment.MARKET_NAME || '冒险岛怀旧服 · 国服/小白兔' };
}
