import { randomBytes } from 'node:crypto';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const target = `${root}.env`;
if (existsSync(target)) {
  console.log('.env 已存在，未覆盖；请检查 MYSQL_PASSWORD 等数据库字段。');
} else {
  const template = readFileSync(`${root}.env.example`, 'utf8')
    .replace('run-npm-run-setup-to-generate-a-random-token', randomBytes(24).toString('hex'));
  writeFileSync(target, template, { flag: 'wx', mode: 0o600 });
  console.log('已生成 .env，包含全部 MySQL 配置和随机 ADMIN_TOKEN。');
  console.log('请填写宝塔数据库实际密码；脚本不会创建或修改你的 MySQL 用户密码。');
}
