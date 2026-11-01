import { chromium } from 'playwright-core';

export function browserLaunchOptions(config, environment = process.env) {
  const address = environment.HTTPS_PROXY || environment.HTTP_PROXY || environment.https_proxy || environment.http_proxy;
  const proxy = address ? new URL(address) : null;
  return { headless: true, executablePath: config.chromium, timeout: 30000, args: ['--disable-dev-shm-usage'],
    ...(proxy ? { proxy: { server: `${proxy.protocol}//${proxy.host}`, ...(proxy.username ? {
      username: decodeURIComponent(proxy.username), password: decodeURIComponent(proxy.password) } : {}) } } : {}) };
}

export const launchBrowser = options => chromium.launch(options);

// Preserve Chromium stderr and nested DNS/TLS errors, but never publish credentials.
export function errorDetails(error, config = {}, environment = process.env) {
  const messages = [];
  for (let item = error, depth = 0; item && depth < 4; item = item.cause, depth++) {
    messages.push([item.name, item.code, item.message || String(item)].filter(Boolean).join(': '));
  }
  let text = messages.join('\n').replace(/\u001b\[[0-9;]*m/g, '')
    .split('\n').map(line => line.trim()).filter(line => line && !line.includes('<launching>') && !/^[-═╔╗╚╝║]+$/.test(line)).join('\n');
  text = text.replace(/([a-z][a-z\d+.-]*:\/\/)[^\s/@]+@/gi, '$1[redacted]@');
  const secrets = [config.adminToken, config.mysql?.password, environment.ADMIN_TOKEN, environment.MYSQL_PASSWORD];
  for (const address of [environment.HTTPS_PROXY, environment.HTTP_PROXY, environment.https_proxy, environment.http_proxy]) {
    try { const url = new URL(address); secrets.push(decodeURIComponent(url.username), decodeURIComponent(url.password)); } catch {}
  }
  for (const value of secrets.filter(value => typeof value === 'string' && value.length).sort((a, b) => b.length - a.length))
    text = text.replaceAll(value, '[redacted]');
  return text.slice(0, 1800);
}
