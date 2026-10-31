import { load, loadBuffer } from 'cheerio';
import Decimal from 'decimal.js';
import { createHash } from 'node:crypto';

Decimal.set({ precision: 36 });
const N = '(\\d[\\d,]*(?:\\.\\d+)?)';
const WAN = '万(?:金币|游戏币|金|币)?';
const DIRECT = new RegExp('(?<![-\\d.])' + N + '\\s*元\\s*[=＝:：]\\s*' + N + '\\s*' + WAN, 'g');
const INVERSE = new RegExp('(?<![-\\d.])' + N + '\\s*' + WAN + '\\s*[=＝:：]\\s*' + N + '\\s*元', 'g');
const UNIT = new RegExp('(?<![-\\d.])' + N + '\\s*元\\s*/\\s*' + WAN, 'g');
const QUANTITY = new RegExp('收货数量\\s*[:：]?\\s*' + N + '\\s*' + WAN);
const value = text => new Decimal(text.replaceAll(',', ''));
const bounds = number => number.isFinite() && number.gte('0.000001') && number.lte('1000000000');
const BLOCKS = new Set(['div', 'p', 'section', 'article', 'aside', 'main', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'tr', 'td', 'th', 'br']);
// Separate adjacent block elements without splitting numbers styled with inline spans.
function readableText(node) {
  if (node.type === 'text') return node.data;
  const text = (node.children || []).map(readableText).join('');
  return BLOCKS.has(node.name) ? ` ${text} ` : text;
}

export function extractRate(text) {
  const direct = [], inverse = [];
  try {
    for (const match of text.matchAll(DIRECT)) if (value(match[1]).gt(0)) direct.push(value(match[2]).div(value(match[1])));
    for (const match of text.matchAll(INVERSE)) if (value(match[2]).gt(0)) inverse.push(value(match[1]).div(value(match[2])));
    let candidates = direct.length ? direct : inverse;
    if (!candidates.length) candidates = [...text.matchAll(UNIT)].filter(m => value(m[1]).gt(0)).map(m => new Decimal(1).div(value(m[1])));
    if (!candidates.length || !candidates.every(bounds)) return null;
    const rate = candidates[0];
    if (candidates.some(other => other.div(rate).minus(1).abs().gt('.005'))) return null;
    if (direct.length && inverse.some(other => other.div(rate).minus(1).abs().gt('.02'))) return null;
    return rate;
  } catch { return null; }
}

export function safeUrl(href, source) {
  if (!href || /^(#|javascript:|data:)/i.test(href.trim())) return source;
  try {
    const url = new URL(href, source);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password &&
      (url.hostname === '7881.com' || url.hostname.endsWith('.7881.com')) ? url.href : source;
  } catch { return source; }
}

export function parseHtml(html, sourceUrl, merchantSelector = '', playerSelector = '') {
  const $ = Buffer.isBuffer(html) ? loadBuffer(html, { encoding: { defaultEncoding: 'utf-8' } }) : load(html);
  $('script,style,noscript,[hidden],[aria-hidden="true"],[data-gw-hidden="true"]').remove();
  $('[style]').each((_, element) => { if (/display\s*:\s*none|visibility\s*:\s*hidden/i.test($(element).attr('style'))) $(element).remove(); });
  const quotes = [], warnings = [], seen = new Set();
  const actionText = element => ($(element).attr('value') || $(element).text()).replace(/\s+/g, '').replace(/[>›»]/g, '');
  const sideAction = side => side === 'merchant' ? /^(立即出售|马上出售)$/ : /^(立即购买|马上购买)$/;
  const allAction = /^(立即出售|马上出售|立即购买|马上购买)$/;
  const cardText = element => readableText(element).replace(/\s+/g, ' ').trim();
  function add(card, action, side, index) {
    if (seen.has(card)) return;
    const text = cardText(card);
    if (text.length > 16000) { warnings.push(`${side} 卡片范围过大，已跳过`); return; }
    const actions = $(card).find('a,button,input').toArray().filter(el => allAction.test(actionText(el)));
    if (actions.length > 1) { warnings.push(`${side} 选择器包含多个报价，请选择单张卡片`); return; }
    const rate = extractRate(text);
    if (!rate) return;
    const hrefs = $(card).find('a[title][href],h2 a[href],h3 a[href]').toArray().map(el => $(el).attr('href'));
    if (action) hrefs.push($(action).attr('href'));
    hrefs.push(...$(card).find('a[href]').toArray().map(el => $(el).attr('href')));
    const link = hrefs.map(href => safeUrl(href, sourceUrl)).find(url => url !== sourceUrl) || sourceUrl;
    let title = side === 'merchant' ? '商家收货报价' : '玩家金币挂单';
    for (const element of $(card).find('[title],h2,h3,h4').toArray()) {
      const candidate = ($(element).attr('title') || $(element).text()).trim();
      if (candidate.length > 6 && /万|交易/.test(candidate)) { title = candidate.slice(0, 280); break; }
    }
    const quantity = side === 'merchant' ? text.match(QUANTITY) : null;
    const identity = link !== sourceUrl ? `${side}|${link}` : `${side}|${sourceUrl}|${index}`;
    quotes.push({ side, source_key: createHash('sha256').update(identity).digest('hex'), title, source_url: link,
      wan_per_yuan: rate.toFixed(12), yuan_per_wan: new Decimal(1).div(rate).toFixed(12),
      quantity_wan: quantity ? value(quantity[1]).toFixed(4) : null, evidence: text.slice(0, 1200) });
    seen.add(card);
  }
  for (const [side, selector] of [['merchant', merchantSelector], ['player', playerSelector]]) {
    if (selector) {
      const selected = $(selector).toArray();
      selected.forEach((card, index) => add(card, null, side, index));
      if (!selected.length) warnings.push(`${side} 选择器未匹配报价`);
      continue;
    }
    const own = sideAction(side), opposite = sideAction(side === 'merchant' ? 'player' : 'merchant');
    const actions = $('a,button,input').toArray().filter(element => own.test(actionText(element)));
    actions.forEach((action, index) => {
      let candidate = null;
      for (const parent of $(action).parents().toArray().slice(0, 10)) {
        if (['body', 'html'].includes(parent.name)) break;
        const text = cardText(parent);
        if (text.length > 16000) break;
        const inner = $(parent).find('a,button,input').toArray();
        if (inner.some(el => opposite.test(actionText(el))) || inner.filter(el => own.test(actionText(el))).length > 1) break;
        if (extractRate(text)) candidate = parent;
      }
      if (candidate) add(candidate, action, side, index);
    });
  }
  const blocked = !quotes.length && /人机验证|滑动验证|请完成验证|访问过于频繁|安全验证|Access Denied|captcha/i.test($.root().text());
  if (!quotes.length && !blocked) warnings.push('未识别到报价卡片，可能需要浏览器渲染或调整 CSS 选择器');
  return { quotes, warnings, blocked };
}

export function validateSelector(selector) { load('<main></main>')(selector); }
