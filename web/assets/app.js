const app = document.querySelector('#app');
const esc = (v) => String(v ?? '尚未能確認').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = async (path, options) => { const response = await fetch(path, options); if (!response.ok) throw new Error('資料未能讀取'); return response.json(); };
const enums = {
  UNKNOWN:'尚未能確認', 'NOT AVAILABLE':'暫無資料', HEALTHY:'正常', DEGRADED:'需留意', ATTENTION:'需要關注',
  CONNECTED:'已連接', DISCONNECTED:'連接中斷', RECONNECTING:'正在重新連接',
  MONITORING:'正常監察', MARKET_CLOSED:'IG 市場目前休市', SCHEDULED_OFF:'已排程停止監察', STALE_DATA:'IG 市場應可交易，但即時報價已過期',
  TRADEABLE:'IG 產品可交易', OPEN:'IG 產品開市', CLOSED:'IG 產品休市', EDITS_ONLY:'只可修改掛單', OFFLINE:'IG 產品暫停交易',
  BULLISH:'看升／偏強', UP:'向上', BEARISH:'看跌／偏弱', DOWN:'向下', NEUTRAL:'中性',
  TREND_UP:'上升趨勢', TREND_DOWN:'下跌趨勢', TRANSITION:'趨勢轉換中', CONFIRMED:'趨勢已確認', MATURE:'趨勢成熟', EXHAUSTION_RISK:'趨勢或有衰竭',
  LONG:'偏向做多', SHORT:'偏向做空', DO_NOT_CHASE:'不宜追價', GOOD_SETUP_NOT_TRIGGERED:'條件良好・尚未觸發', POOR_LOCATION:'目前位置不理想', ENTRY_QUALIFIED:'入場條件符合',
  ORIGINAL_STRUCTURE_VALID:'原有結構仍有效', STRUCTURE_INVALIDATED:'原有結構已失效', NORMAL:'正常', NOT_ELIGIBLE:'暫不符合條件',
  RUNNER_ELIGIBLE:'符合延伸行情條件', RUNNER_NOT_ELIGIBLE:'暫不符合延伸行情條件',
  CLOSED:'已收市', FORMING:'形成中', 'CLOSED/CONFIRMED':'已收市確認', 'FORMING/PROVISIONAL':'形成中・暫定',
  '24_7':'IG AI 持續監察：24小時運行', CUSTOM:'自訂監察時段', MARKET_HOURS:'IG 產品時段（以供應商狀態確認）', CLOSED_NO_TRADING:'IG 產品目前沒有交易',
  INFO:'資訊', WATCH:'觀察', WARNING:'警告', CRITICAL:'嚴重', LOW:'低', MODERATE:'中等', HIGH:'高', VERY_HIGH:'非常高',
  ALL:'全部', READ:'已讀', UNREAD:'未讀', NONE:'無', LIVE:'即時', 'LIVE / CURRENT':'即時報價正常', STALE:'資料過期',
  WAITING:'等待合資格資料', 'NO CURRENT WARNING':'目前沒有警告', 'WARNING PRESENT':'有警告', INSUFFICIENT_EVIDENCE:'證據不足',
  TREND:'趨勢', TRENDING:'趨勢中', 'NO TRADE':'暫無操作', ENTRY_QUALIFIED:'入場條件符合',
};
const alertNames = {
  DIRECTION_SHIFT:'方向轉變', REVERSAL_CONFIRMED:'轉勢已確認', REVERSAL_WATCH:'留意轉勢風險', REVERSAL_RISK_INCREASE:'轉勢風險上升',
  REVERSAL_RISK_DECREASE:'轉勢風險下降', EARLY_REVERSAL_WARNING:'轉勢初步警示', TIMEFRAME_REALIGNMENT:'不同時段走勢重新一致',
  TIMEFRAME_CONFLICT:'不同時段走勢出現分歧', FALSE_BREAKOUT:'假突破', BREAKOUT_CONFIRMED:'突破已確認', RESISTANCE_BREAK:'突破阻力',
  SUPPORT_BREAK:'跌穿支持', PATTERN_NEAR_CONFIRMATION:'形態接近確認', PATTERN_CONFIRMED:'形態已確認', PATTERN_FAILED:'形態失效',
  VOLATILITY_EXPANSION:'波幅正在擴大', HOLDING_WINDOW_SHORTENED:'建議持有時間縮短', HOLDING_WINDOW_EXTENDED:'建議持有時間延長',
  TREND_STAGE_CHANGE:'趨勢階段轉變',
};
const evidenceNames = {
  DIRECTION_SHIFT:'方向出現轉變', REVERSAL_CONFIRMED:'轉勢條件已確認', REVERSAL_WATCH:'轉勢風險值得留意',
  REVERSAL_RISK_INCREASE:'轉勢風險上升', REVERSAL_RISK_DECREASE:'轉勢風險下降', EARLY_REVERSAL_WARNING:'出現初步轉勢訊號',
  TIMEFRAME_CONFLICT:'不同時間圖表方向不一致', TIMEFRAME_REALIGNMENT:'不同時間圖表方向重新一致', BREAKOUT_CONFIRMED:'價格突破關鍵位置',
  FALSE_BREAKOUT:'突破未能維持', VOLATILITY_EXPANSION:'近期波幅擴大', PATTERN_CONFIRMED:'圖表形態符合確認條件',
  PATTERN_NEAR_CONFIRMATION:'圖表形態接近確認條件', SUPPORT_BREAK:'價格跌穿支持位置', RESISTANCE_BREAK:'價格升穿阻力位置',
};
const incidentNames = {
  'provider market is active but the latest tick is stale':'IG 市場仍可交易，但即時報價未有更新',
  'runtime heartbeat is unavailable':'未能取得系統運行心跳',
  'runtime heartbeat is not advancing':'系統運行狀態沒有持續更新',
  'database read/persistence evidence is unavailable':'資料庫未能讀取或未有持續儲存',
  'eligible closed 1H evidence has no persisted analysis yet':'已有合資格的一小時K線，分析結果尚未更新',
};
const text = (value, map = enums) => {
  if (value === null || value === undefined || value === '') return '尚未能確認';
  const raw = String(value);
  return map[raw] || (/^[A-Z][A-Z0-9_ /-]*$/.test(raw) ? raw.replaceAll('_', ' ').toLocaleLowerCase('zh-Hant') : raw);
};
const date = (value) => {
  if (!value) return '尚未有紀錄';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '尚未能確認' : new Intl.DateTimeFormat('zh-Hant-HK', {dateStyle:'medium', timeStyle:'short', hour12:false}).format(parsed);
};
const fact = (name, value, cls='') => `<div class="fact"><label>${esc(name)}</label><strong class="${cls}">${esc(value)}</strong></div>`;
const translatedState = (state) => text(state);
function marketStatus(m) {
  if (m.monitoring_state === 'MARKET_CLOSED') return {label:'IG 市場目前休市', cls:'status-closed'};
  if (m.monitoring_state === 'SCHEDULED_OFF') return {label:'已排程停止監察', cls:'status-neutral'};
  if (m.monitoring_state === 'STALE_DATA') return {label:'⚠️ IG 市場仍可交易，但即時報價未有更新', cls:'status-stale'};
  if (m.monitoring_state === 'MONITORING') return {label:'即時報價正常', cls:'status-live'};
  return {label:'尚未能確認市場／報價狀態', cls:'status-unknown'};
}
function dashboard(data) {
  document.querySelector('#connection').textContent = '只讀市場資料';
  return `<div class="section-head"><div><div class="eyebrow">IG CFD 即時監察</div><h1>市場概覽</h1><p>市場狀態以 IG 該產品當刻資料為準；不以相關現貨交易所時間推斷。</p></div></div><section class="market-grid">${data.markets.map(m => { const status=marketStatus(m); return `<article class="card market-card"><div class="market-top"><div class="market-name">${esc(m.market)}</div><span class="status-pill ${status.cls}">${status.label}</span></div><div class="price">${esc(m.price === 'UNKNOWN' ? '尚未有報價' : m.price)}</div><div class="muted">IG 報價時間：${date(m.price_timestamp)}</div><div class="primary-facts">${fact('1小時方向',text(m.trend_1h))}${fact('市場趨勢',text(m.regime))}${fact('主要操作狀態',text(m.primary_action))}</div><details class="market-details"><summary>分析與風險詳情</summary><div class="facts">${fact('市場偏向',text(m.market_bias))}${fact('方向參考',text(m.evaluated_side))}${fact('入場質素',m.entry_quality === 'NOT AVAILABLE' ? '暫無資料' : m.entry_quality)}${fact('入場狀態',text(m.entry_quality_state))}${fact('結構狀態',text(m.structure_invalidation))}${fact('風險保護',text(m.profit_protection))}${fact('延伸行情',text(m.runner_state))}${fact('15分鐘警示',text(m.warning_15m))}</div><p class="muted">分析更新：${date(m.analysis_updated_at)} · 資料品質：${text(m.data_quality)}</p></details></article>`; }).join('')}</section>`;
}
function evidenceLine(value) {
  const raw = String(value ?? '');
  const [key, ...rest] = raw.split(':');
  if (evidenceNames[key]) return evidenceNames[key] + (rest.length ? `：${text(rest.join(':'))}` : '');
  return text(raw);
}
function alerts(data) {
  return `<div class="section-head"><div><div class="eyebrow">分析狀態轉變</div><h1>市場警報</h1><p>警報反映已記錄的市場分析狀態，不代表自動交易指示。</p></div></div><div class="toolbar"><select id="alert-state" aria-label="警報狀態"><option value="ALL">全部警報</option><option value="UNREAD">未讀</option><option value="READ">已讀</option></select><select id="alert-market" aria-label="市場"><option value="">所有市場</option><option>US Tech 100</option><option>Japan 225</option><option>Hong Kong HS50</option></select></div><section class="alert-list">${data.alerts.length ? data.alerts.map(a => `<article class="card alert-card ${a.is_read?'':'unread'}"><button class="action" data-alert="${esc(a.alert_id)}" data-read="${a.is_read?'false':'true'}">${a.is_read?'標記為未讀':'標記為已讀'}</button><div class="alert-meta"><span>${esc(a.market)} · ${esc(alertNames[a.alert_type] || text(a.alert_type))}</span><span>${date(a.created_at)}</span></div><h3>${esc(text(a.priority))} · ${esc(text(a.state || (a.confirmed?'CLOSED/CONFIRMED':'FORMING/PROVISIONAL')))}</h3><div class="alert-facts">${fact('1小時方向',text(a.current_direction))}${fact('趨勢階段',text(a.current_trend_stage))}${fact('轉勢風險',text(a.current_reversal_risk))}${fact('持有時間參考',text(a.current_holding_window))}</div>${a.trigger_evidence?.length ? `<p class="evidence">${a.trigger_evidence.map(evidenceLine).map(esc).join('；')}</p>` : ''}</article>`).join('') : '<div class="empty">目前沒有符合條件的警報。</div>'}</section>`;
}
function health(data) {
  const active = (data.incidents || []).filter(i => i.status === 'ACTIVE');
  const incidentText = active.map(i => `${i.market || text(i.domain)}：${incidentNames[i.reason] || text(i.reason)}`).join('；') || '目前沒有需要處理的事項';
  const db = data.database || {};
  return `<div class="section-head"><div><div class="eyebrow">自動更新的系統監察</div><h1>系統健康：${esc(text(data.overall_status))}</h1><p>系統會持續自行檢查；休市期間沒有新報價屬正常情況。</p></div></div><section class="health-grid health-summary"><article class="card"><h3>系統狀態</h3>${fact('整體狀態',text(data.overall_status))}${fact('服務',text(data.service?.status))}${fact('最後心跳',date(data.service?.heartbeat || data.service?.last_heartbeat))}</article><article class="card"><h3>IG 連接</h3>${fact('連接狀態',text(data.ig?.connection_status || data.service?.ig_connection))}${fact('連線心跳',date(data.ig?.heartbeat || data.service?.last_heartbeat))}</article><article class="card"><h3>資料庫</h3>${fact('讀取狀態',text(db.status))}${fact('最後持久化',date(db.persistence_timestamp))}</article><article class="card"><h3>警報與注意事項</h3>${fact('最近警報',date(data.alerts?.latest_alert))}<p class="incident-text">${esc(incidentText)}</p></article></section><h2 class="subhead">市場資料</h2><section class="health-grid">${(data.markets || []).length ? data.markets.map(m => { const status=marketStatus(m); return `<article class="card health-market"><h3>${esc(m.market)}</h3><div class="health-market-status ${status.cls}">${status.label}</div>${[['IG 市場狀態',text(m.provider_status)],['資料狀態',translatedState(m.monitoring_state)],['IG 合約 EPIC',m.epic || '尚未能確認'],['最後報價',date(m.latest_tick)],['報價距今',m.tick_age_seconds == null ? '尚未能確認' : `${Math.max(0,Math.round(m.tick_age_seconds))} 秒`],['最後15分鐘K線',date(m.latest_closed_15m)],['最後1小時K線',date(m.latest_closed_1h)],['最後分析',date(m.latest_analysis)],['分析狀態',text(m.analysis_health)],['最後決策',date(m.latest_decision)],['最後警報',date(m.latest_alert)],['監察排程',m.schedule_mode === '24_7' ? 'IG AI 持續監察：24小時運行' : text(m.schedule_mode)]].map(([k,v])=>`<div><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}</article>`; }).join('') : '<div class="empty">目前沒有市場監察紀錄。</div>'}</section>`;
}
function settings(data) {
  return `<div class="section-head"><div><div class="eyebrow">監察設定</div><h1>監察時間</h1><p>設定只控制 IG AI 何時執行監察，並不代表 IG 產品的實際交易時段。</p></div></div><div class="notice"><strong>24_7</strong>：IG AI 持續監察，24小時運行。這不代表所有 IG 市場全年24小時交易；實際可交易狀態以 IG 該產品當刻狀態為準。</div><div class="notice notice-secondary">沒有可靠的 IG 產品狀態時，系統會顯示「尚未能確認」，不會自行推斷休市。</div><section class="health-grid settings-grid">${data.schedules.length ? data.schedules.map(s=>`<article class="card"><h3>${esc(s.market)}</h3>${fact('監察模式',s.mode === '24_7' ? 'IG AI 持續監察：24小時運行' : text(s.mode))}${fact('指定時段',s.windows?.join('；')||'未設定')}${fact('時區',s.timezone)}<p class="muted">目前為唯讀設定檢視。</p></article>`).join(''):'<div class="empty">目前沒有監察排程。</div>'}</section>`;
}
async function render(view='dashboard') {
  try {
    const data = view==='dashboard'?await api('/api/dashboard'):view==='alerts'?await api('/api/alerts'):view==='health'?await api('/api/health'):await api('/api/schedule');
    app.innerHTML = view==='dashboard'?dashboard(data):view==='alerts'?alerts(data):view==='health'?health(data):settings(data);
    if(view==='alerts') bindAlerts();
  } catch {
    document.querySelector('#connection').textContent='連線中斷';
    app.innerHTML='<div class="card"><h2>資料暫時未能讀取</h2><p>目前無法連接服務，畫面沒有可確認的最新狀態。</p></div>';
  }
}
function bindAlerts() {
  document.querySelector('#alert-state').onchange = filterAlerts;
  document.querySelector('#alert-market').onchange = filterAlerts;
  document.querySelectorAll('[data-alert]').forEach(b=>b.onclick=async()=>{ await api(`/api/alerts/${encodeURIComponent(b.dataset.alert)}/${b.dataset.read==='true'?'read':'unread'}`,{method:'POST'}); render('alerts'); });
}
async function filterAlerts() {
  const state=document.querySelector('#alert-state').value, market=document.querySelector('#alert-market').value;
  try { app.innerHTML=alerts(await api(`/api/alerts?state=${state}&market=${encodeURIComponent(market)}`)); bindAlerts(); }
  catch { render('alerts'); }
}
document.querySelectorAll('.bottom-nav button').forEach(button=>button.onclick=()=>{ document.querySelectorAll('.bottom-nav button').forEach(item=>item.classList.toggle('active',item===button)); render(button.dataset.view); });
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/assets/sw.js').catch(()=>{});
render();
