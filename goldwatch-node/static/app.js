const $ = id => document.getElementById(id);
const state = { summary: null, period: '30m', span: '7d', metric: 'best', unit: 'yuan_per_wan', market: 'both', view: 'line', token: '', chart: null, history: null, chartRequest: 0, offset: 0, offline: false };
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (value, digits = 5) => value == null ? '—' : Number(value).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const date = (value, long = false) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', ...(long ? { month: '2-digit', day: '2-digit' } : {}), hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value)) : '—';

async function api(path, { method = 'GET', body, admin = false } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (admin) headers.Authorization = `Bearer ${$('admin-token').value.trim() || state.token}`;
  const response = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, cache: 'no-store' });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(result.detail) ? result.detail.map(x => `${x.loc?.at(-1)}: ${x.msg}`).join('；') : result.detail;
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  return result;
}
let toastTimer;
function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5000);
}
function busy(button, value) { button.disabled = value; button.style.opacity = value ? '.55' : ''; }

function renderSummary(data) {
  state.summary = data;
  state.offline = false;
  state.offset = Date.parse(data.server_time) - Date.now();
  $('region').textContent = data.market_name.split('·').at(-1).trim();
  $('demo-banner').hidden = data.mode !== 'demo';
  document.title = `${data.mode === 'demo' ? '[演示] ' : ''}枫币行情 · ${data.market_name}`;
  for (const id of ['sidebar-source', 'footer-source']) $(id).href = data.source_url;
  const last = data.last_run;
  const failed = last && ['failed', 'blocked'].includes(last.status);
  const pill = $('status-pill');
  let status = data.mode === 'demo' ? '演示数据' : last?.status === 'running' ? '正在采集' : failed ? '采集受阻' : !data.preferences.enabled ? '定时已暂停' : !data.worker_enabled ? '采集未启用' : '定时采集中';
  pill.innerHTML = `<i></i>${esc(status)}`;
  pill.className = `status-pill ${failed ? 'bad' : last && !failed ? 'good' : ''}`;
  const times = Object.values(data.markets).filter(Boolean).map(x => x.at).sort();
  $('last-update').textContent = times.length ? `报价更新于 ${date(times.at(-1), true)}` : '尚无有效报价，等待第一次成功采集';
  const interval = data.preferences.interval_seconds;
  $('interval-label').textContent = interval % 60 === 0 ? `${interval / 60} 分钟` : `${interval} 秒`;
  const notices = [];
  if (failed) notices.push(`最近一次采集${last.status === 'blocked' ? '受到访问限制' : '失败'}：${last.message}。下方保留的是最后成功报价，请查看各区域更新时间。`);
  else if (last?.status === 'partial') notices.push(`本次仅获取部分价格区域：${last.message}`);
  if (Object.values(data.markets).some(x => x?.stale)) notices.push('部分报价已超过有效更新窗口，属于旧报价。');
  if (last?.origin === 'manual-html') notices.push('最近一笔来自管理员手动导入的公开页面，不是自动抓取结果。');
  $('notice').textContent = notices.join(' ');
  $('notice').hidden = !notices.length;
  for (const side of ['merchant', 'player']) {
    const item = data.markets[side];
    $(`${side}-price`).textContent = fmt(item?.yuan_per_wan);
    $(`${side}-rate`).textContent = fmt(item?.wan_per_yuan, 3);
    $(`${side}-count`).textContent = item ? `${item.count} 条${item.stale ? ' · 旧报价' : '样本'}` : '暂无报价';
    const change = $(`${side}-change`);
    change.className = 'change ' + (item?.change_24h == null ? 'neutral' : item.change_24h > 0 ? 'up' : item.change_24h < 0 ? 'down' : 'neutral');
    change.textContent = item?.change_24h == null ? '暂无 24h 对比' : `${item.change_24h > 0 ? '↗ +' : item.change_24h < 0 ? '↘ ' : ''}${fmt(item.change_24h, 2)}%`;
  }
  $('spread-price').textContent = fmt(data.spread?.yuan_per_wan);
  $('spread-note').textContent = data.spread ? `价差约占挂售价的 ${fmt(data.spread.percent, 2)}%` : '两侧无同步有效报价，暂不计算';
  $('sample-count').textContent = data.samples_24h.toLocaleString('zh-CN');
  $('collect-button').disabled = data.mode === 'demo' || last?.status === 'running' || !data.worker_enabled;
  tick();
}

function renderQuotes(side, data) {
  $(side === 'merchant' ? 'bid-table-count' : 'ask-table-count').textContent = `${data.rows.length} 条`;
  $(`${side}-at`).textContent = `报价时间：${date(data.at, true)} · ${side === 'merchant' ? '最高回收价优先' : '最低挂售价优先'}`;
  $(`${side}-rows`).innerHTML = data.rows.length ? data.rows.map((row, index) => `<tr>
    <td><span class="rank">${index + 1}</span><a class="quote-name" title="${esc(row.title)}" href="${esc(row.source_url)}" target="_blank" rel="noopener noreferrer">${esc(row.title)}</a></td>
    <td><strong>${fmt(row.yuan_per_wan)}</strong></td><td>${fmt(row.wan_per_yuan, 3)}</td>
    <td>${side === 'merchant' ? row.quantity_wan == null ? '—' : fmt(row.quantity_wan, 0) : `<a class="source-link" href="${esc(row.source_url)}" target="_blank" rel="noopener noreferrer">查看 ↗</a>`}</td></tr>`).join('') : `<tr><td colspan="4" class="empty-cell">尚无${side === 'merchant' ? '商家回收' : '玩家挂售'}报价</td></tr>`;
}
const statusText = { success: '采集成功', partial: '部分成功', failed: '采集失败', blocked: '访问受限', running: '采集中' };
const originText = { scheduled: '定时采集', manual: '手动触发', 'manual-html': '手动导入', demo: '演示数据' };
function renderLogs(data) {
  $('log-rows').innerHTML = data.rows.length ? data.rows.map(row => `<tr>
    <td>${esc(date(row.started_at, true))}</td><td><span class="log-status ${esc(row.status)}">${statusText[row.status] || esc(row.status)}</span></td>
    <td>${row.merchant_count} / ${row.player_count}</td><td>${originText[row.origin] || esc(row.origin)}</td>
    <td class="log-message" title="${esc(row.message)}">${esc(row.message || (row.status === 'running' ? '正在请求来源页面…' : `已保存报价，采集 ${row.pages} 页`))}</td></tr>`).join('') : '<tr><td colspan="5" class="empty-cell">首次采集后会在这里保留成功或失败记录</td></tr>';
}

function drawChart() {
  const data = state.history;
  if (!data || !window.echarts) return;
  if (!state.chart) state.chart = window.echarts.init($('price-chart'), null, { renderer: 'canvas' });
  const chosen = state.market === 'both' ? ['merchant', 'player'] : [state.market];
  const colors = { merchant: '#8b78df', player: '#d7a458' };
  const names = { merchant: '商家回收', player: '玩家挂售' };
  const digits = state.unit === 'yuan_per_wan' ? 5 : 3;
  const labels = data.times.map(time => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(time)));
  const series = [];
  for (const side of chosen) {
    const points = data.series[side];
    const candle = state.view === 'candle' && state.market !== 'both';
    series.push({ name: names[side], type: candle ? 'candlestick' : 'line', xAxisIndex: 0, yAxisIndex: 0,
      data: candle ? points.map(p => p ? [p.open, p.close, p.low, p.high] : ['-', '-', '-', '-']) : points.map(p => p?.close ?? null),
      connectNulls: false, showSymbol: points.filter(Boolean).length < 10, symbolSize: 5, smooth: false,
      itemStyle: candle ? { color: '#e8787d', color0: '#43b79c', borderColor: '#e8787d', borderColor0: '#43b79c' } : { color: colors[side] },
      lineStyle: { width: 2, color: colors[side] }, areaStyle: candle ? undefined : { color: colors[side], opacity: state.market === 'both' ? .04 : .07 },
      emphasis: { focus: 'series' }, animationDuration: 250 });
    series.push({ name: `${names[side]}采样数`, type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
      data: points.map(p => p?.count ?? null), barMaxWidth: 8, itemStyle: { color: colors[side], opacity: .35 }, silent: true });
  }
  state.chart.setOption({ backgroundColor: '#fff', animation: false,
    grid: [{ left: 66, right: 28, top: 23, height: '57%' }, { left: 66, right: 28, top: '73%', height: '12%' }],
    tooltip: { trigger: 'axis', confine: true, backgroundColor: '#fff', borderColor: '#e8e8f2', borderWidth: 1,
      textStyle: { fontSize: 11, color: '#6e758b' }, extraCssText: 'box-shadow:0 5px 25px #27274d15;border-radius:7px;padding:12px;',
      formatter(params) { const index = params[0]?.dataIndex; if (index == null) return ''; let html = `<b>${esc(labels[index])}（北京时间）</b>`;
        for (const side of chosen) { const p = data.series[side][index]; html += `<div style="margin-top:7px;color:${colors[side]}">${names[side]}：${p ? fmt(p.close, digits) : '未采样'}</div>`;
          if (p && state.view === 'candle' && state.market !== 'both') html += `<div style="font-size:10px;margin-top:4px">开 ${fmt(p.open, digits)}　高 ${fmt(p.high, digits)}<br>低 ${fmt(p.low, digits)}　收 ${fmt(p.close, digits)}</div>`;
          if (p) html += `<div style="font-size:10px;color:#a0a4b2;margin-top:3px">${p.samples} 次采样 · ${p.count} 条报价</div>`; } return html; } },
    axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: '#b5aed4', type: 'dashed' } },
    xAxis: [{ type: 'category', data: labels, boundaryGap: state.view === 'candle', axisLine: { lineStyle: { color: '#edf0f5' } }, axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: true, lineStyle: { color: '#f5f6fa' } } },
      { type: 'category', data: labels, gridIndex: 1, axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: '#aeb4c2', fontSize: 9, hideOverlap: true, formatter: label => state.span === '1d' ? label.split(' ').at(-1) : label.slice(0, 5) } }],
    yAxis: [{ type: 'value', scale: true, splitNumber: 4, axisLabel: { color: '#a8aebd', fontSize: 9, formatter: value => Number(value).toFixed(digits) }, splitLine: { lineStyle: { color: '#f0f2f7', type: 'dashed' } } },
      { type: 'value', gridIndex: 1, splitNumber: 1, axisLabel: { color: '#bcc1cd', fontSize: 8 }, splitLine: { show: false } }],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }, { type: 'slider', xAxisIndex: [0, 1], bottom: 6, height: 14, borderColor: 'transparent', backgroundColor: '#fafbfe', fillerColor: '#a99dd724', handleSize: '70%', showDetail: false, dataBackground: { lineStyle: { color: '#dcd7ed' }, areaStyle: { color: '#e9e5f4' } } }], series }, true);
  const valid = chosen.some(side => data.series[side].some(Boolean));
  $('chart-empty').hidden = valid;
  const periods = { '5m': '5 分钟', '30m': '30 分钟', '1h': '1 小时', '1d': '日线', '1w': '周线' };
  $('chart-note').textContent = data.note + (data.period !== data.requested_period ? `（实际周期：${periods[data.period]}）` : '') + ' · 北京时间';
  $('axis-unit').textContent = state.unit === 'yuan_per_wan' ? '单位：元 / 万金' : '单位：万金 / 元';
  for (const side of ['merchant', 'player']) {
    const last = data.series[side].filter(Boolean).at(-1);
    $(side === 'merchant' ? 'legend-bid' : 'legend-ask').textContent = fmt(last?.close, digits);
  }
  const counts = data.times.map((_, i) => (data.series.merchant[i]?.count || 0) + (data.series.player[i]?.count || 0)).filter(Boolean).slice(-12);
  document.querySelector('.spark-bars').hidden = !counts.length;
  const highest = Math.max(...counts, 1);
  document.querySelectorAll('.spark-bars i').forEach((bar, i) => { bar.style.height = `${(counts[i] || 0) / highest * 100}%`; });
  $('candle-button').disabled = state.market === 'both';
  $('candle-button').title = state.market === 'both' ? '选择单一价格区域后可查看 K 线' : 'K线图';
}

async function loadChart() {
  const id = ++state.chartRequest;
  const params = new URLSearchParams({ period: state.period, span: state.span, metric: state.metric, unit: state.unit });
  try {
    const result = await api(`/api/history?${params}`);
    if (id !== state.chartRequest) return;
    state.history = result;
    drawChart();
  } catch (error) { toast(error.message); }
}
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const [summary, bid, ask, logs] = await Promise.all([api('/api/summary'), api('/api/quotes?side=merchant'), api('/api/quotes?side=player'), api('/api/runs')]);
    renderSummary(summary); renderQuotes('merchant', bid); renderQuotes('player', ask); renderLogs(logs);
    await loadChart();
  } catch (error) {
    state.offline = true;
    $('status-pill').innerHTML = '<i></i>看板连接中断';
    $('status-pill').className = 'status-pill bad';
    $('notice').hidden = false;
    $('notice').textContent = `无法更新看板：${error.message}。当前保留的是上次读取的数据，请勿当作实时报价。`;
    toast(`无法更新看板：${error.message}`);
  }
  finally { refreshing = false; }
}
function tick() {
  const now = Date.now() + state.offset;
  $('clock').textContent = `北京时间 ${date(new Date(now).toISOString())}`;
  const summary = state.summary;
  if (state.offline) { $('countdown').textContent = '连接中断'; return; }
  if (!summary || summary.mode === 'demo') { $('countdown').textContent = summary?.mode === 'demo' ? '演示模式' : '—'; return; }
  if (!summary.worker_enabled || !summary.preferences.enabled) { $('countdown').textContent = '已暂停'; return; }
  const next = summary.preferences.next_due_at;
  const seconds = next ? Math.max(0, Math.ceil((Date.parse(next) - now) / 1000)) : 0;
  $('countdown').textContent = seconds ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : '即将采集';
}
function openSettings() {
  const preferences = state.summary?.preferences;
  if (preferences) {
    $('interval-input').value = preferences.interval_seconds;
    $('pages-input').value = preferences.max_pages;
    $('enabled-input').checked = preferences.enabled;
    $('engine-input').value = preferences.engine;
    $('merchant-selector').value = preferences.merchant_selector;
    $('player-selector').value = preferences.player_selector;
  }
  $('settings-error').textContent = '';
  $('settings-dialog').showModal();
}
document.querySelectorAll('[data-scroll]').forEach(button => button.addEventListener('click', () => $(button.dataset.scroll).scrollIntoView({ behavior: 'smooth' })));
$('settings-side').onclick = openSettings;
$('settings-top').onclick = openSettings;
$('help-button').onclick = () => $('help-dialog').showModal();
$('close-help').onclick = () => $('help-dialog').close();
document.querySelector('.close-dialog').onclick = () => $('settings-dialog').close();
$('refresh').onclick = refresh;
$('span').onchange = event => { state.span = event.target.value; loadChart(); };
$('metric').onchange = event => { state.metric = event.target.value; loadChart(); };
$('unit').onchange = event => { state.unit = event.target.value; loadChart(); };
document.querySelectorAll('[data-period]').forEach(button => button.onclick = () => {
  state.period = button.dataset.period;
  document.querySelectorAll('[data-period]').forEach(item => item.classList.toggle('active', item === button)); loadChart();
});
document.querySelectorAll('[data-market]').forEach(button => button.onclick = () => {
  state.market = button.dataset.market;
  document.querySelectorAll('[data-market]').forEach(item => item.classList.toggle('active', item === button));
  if (state.market === 'both') { state.view = 'line'; $('line-button').classList.add('active'); $('candle-button').classList.remove('active'); }
  drawChart();
});
$('line-button').onclick = () => { state.view = 'line'; $('line-button').classList.add('active'); $('candle-button').classList.remove('active'); drawChart(); };
$('candle-button').onclick = () => { state.view = 'candle'; $('candle-button').classList.add('active'); $('line-button').classList.remove('active'); drawChart(); };
document.querySelectorAll('[data-interval]').forEach(button => button.onclick = () => { $('interval-input').value = button.dataset.interval; });
$('check-token').onclick = async () => {
  try { await api('/api/admin/check', { method: 'POST', admin: true }); state.token = $('admin-token').value.trim(); $('settings-error').textContent = ''; toast('口令验证成功'); }
  catch (error) { $('settings-error').textContent = error.message; }
};
$('settings-form').onsubmit = async event => {
  event.preventDefault(); const button = event.submitter; busy(button, true);
  try {
    await api('/api/settings', { method: 'PUT', admin: true, body: { interval_seconds: Number($('interval-input').value), enabled: $('enabled-input').checked,
      max_pages: Number($('pages-input').value), engine: $('engine-input').value, merchant_selector: $('merchant-selector').value.trim(), player_selector: $('player-selector').value.trim() } });
    state.token = $('admin-token').value.trim() || state.token;
    $('settings-dialog').close(); toast('设置已保存，按新频率安排下一次采集'); await refresh();
  } catch (error) { $('settings-error').textContent = error.message; } finally { busy(button, false); }
};
$('collect-button').onclick = async () => {
  if (!state.token && !$('admin-token').value.trim()) { openSettings(); toast('请先填写并验证管理员口令'); return; }
  busy($('collect-button'), true);
  try { const result = await api('/api/collect', { method: 'POST', admin: true }); toast(result.message); setTimeout(refresh, 2500); }
  catch (error) { toast(error.message); }
  finally { busy($('collect-button'), false); }
};
$('import-button').onclick = async () => {
  const file = $('html-file').files[0];
  if (!file) { $('settings-error').textContent = '先选择手动保存的公开报价 HTML 文件'; return; }
  if (file.size > 2_000_000) { $('settings-error').textContent = 'HTML 文件最大为 2MB'; return; }
  busy($('import-button'), true);
  try { const result = await api('/api/import-html', { method: 'POST', admin: true, body: { html: await file.text() } });
    state.token = $('admin-token').value.trim() || state.token; toast(`导入成功：商家 ${result.merchant_count} 条，玩家 ${result.player_count} 条`); $('settings-dialog').close(); await refresh(); }
  catch (error) { $('settings-error').textContent = error.message; }
  finally { busy($('import-button'), false); }
};
$('export-button').onclick = () => { location.href = `/api/export.csv?${new URLSearchParams({ span: state.span, period: state.period })}`; };
window.addEventListener('resize', () => state.chart?.resize());
setInterval(tick, 1000);
setInterval(refresh, 30000);
refresh();
