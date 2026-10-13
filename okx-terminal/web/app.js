/* ============================================================================
   OKX // TERMINAL  —  real-time renderer
   ----------------------------------------------------------------------------
   · 通道优先级：WebSocket  →  (连续失败3次) SSE  →  (仅 ?sim=1) 内置模拟源
   · 所有"动"的画面都由**真实推送消息**触发；唯一的定时器是：
       (a) WS/SSE 重连退避   (b) 时钟 + msgs/s 计数刷新   (c) rAF 绘制节流
     —— 没有任何 setInterval(fetch...) 轮询，没有假动画。
   · 零硬编码行情数字：拿不到就显示 "--"。
   ==========================================================================*/
'use strict';

/* ----------------------------- 配置 ----------------------------- */
const CFG = {
  wsUrl:   (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws',
  sseUrl:  '/sse',
  backoff: [1000, 2000, 4000, 8000, 15000],  // 指数退避，封顶 15s
  wsFailsBeforeFallback: 3,                  // 连续失败 3 次 → 降级 SSE
  staleMs: 10000,                            // >10s 无消息 → STALE
  maxTradeRows: 500,
  maxPricePoints: 3600,
  maxCandles: 500,
  bookRows: 14,
};

/* 模拟源开关：必须显式 ?sim=1 才允许（在 README 与页面上都明确标注） */
const SIM_ENABLED = new URLSearchParams(location.search).get('sim') === '1';

/* ----------------------------- 工具 ----------------------------- */
const $ = (id) => document.getElementById(id);
const nowMs = () => Date.now();

function fmtPx(v, ref) {
  if (v == null || !isFinite(v)) return '--';
  const a = Math.abs(ref == null ? v : ref);
  const d = a >= 1000 ? 1 : a >= 10 ? 2 : a >= 1 ? 4 : 6;
  return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtCompact(v) {
  if (v == null || !isFinite(v)) return '--';
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (a >= 1e3) return (v / 1e3).toFixed(2) + 'K';
  return v.toFixed(2);
}
function fmtPct(v, d) {
  if (v == null || !isFinite(v)) return '--';
  const s = Number(v).toFixed(d == null ? 2 : d);
  return (v >= 0 ? '+' : '') + s + '%';
}
/* OKX ts = 毫秒 UTC → 北京时间 HH:MM:SS */
function bjTime(ms) {
  if (!ms || !isFinite(ms)) return '--:--:--';
  const d = new Date(Number(ms) + 8 * 3600 * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ':' + p(d.getUTCSeconds());
}
function hmsDown(ms) {
  if (ms == null || !isFinite(ms)) return '--';
  let s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}
/* 从 instId 取展示名：BTC-USDT → BTC ; BTC-USDT-SWAP → BTC-SWAP
   （必须区分现货与永续，否则顶栏会出现两个 BTC） */
function shortName(inst) {
  if (!inst) return '--';
  const s = String(inst);
  const base = s.split('-')[0];
  return /-SWAP$/i.test(s) ? base + '-SWAP' : base;
}

/* ----------------------------- 全局状态 ----------------------------- */
const S = {
  channel: 'DOWN',          // WS | SSE | SIM | DOWN
  lastMsgAt: 0,             // 最近一次收到任何推送的时间
  msgTotal: 0,
  msgThisSec: 0,
  msgsPerSec: null,
  reconnects: 0,
  fallbacks: 0,
  wsFailStreak: 0,
  upstream: null,
  clients: null,
  clientsWs: null,
  clientsSse: null,
  upMsgsTotal: null,
  upReconnects: null,
  sub: null,
  persist: null,
  upTimeSec: null,
  ring: null,
  primary: null,            // 主标的 instId
  mainTs: null,
  insts: new Map(),         // instId -> {last, prev, chg, vol24h, ts, points[]}
  trades: new Map(),        // instId -> {rows[], rate, stamps[]}
  book: new Map(),          // instId -> {bids, asks, ts}
  oi: new Map(),            // instId -> {oiUsd, prev, ts}
  funding: new Map(),       // instId -> {rate, nextTime, ts}
  candles: new Map(),       // instId -> {bars[], src, maxSeq, appendedTotal}
  hb: [],                   // 最近 60 秒 msgs/s
  logLines: [],
  dirty: { depth: false, price: false, heart: false },
};

function log(msg) {
  const t = bjTime(nowMs());
  S.logLines.push(t + ' ' + msg);
  if (S.logLines.length > 200) S.logLines.shift();
  const el = $('log');
  if (el) {
    el.textContent = S.logLines.slice(-40).join('\n');
    el.scrollTop = el.scrollHeight;
  }
}

/* ============================================================================
   1. 连接层：WS → SSE → (SIM)
   ==========================================================================*/
let ws = null, es = null, wsTimer = null, sseTimer = null, simTimer = null;

function setChannel(ch, note) {
  S.channel = ch;
  const b = $('chanBadge');
  if (b) {
    b.textContent = ch;
    b.className = 'chan chan-' + (ch === 'WS' ? 'ws' : ch === 'SSE' ? 'sse' : ch === 'SIM' ? 'sim' : 'down');
  }
  if (note) log(note);
  renderStats();
}

/* ---------------- WebSocket ---------------- */
function connectWS() {
  clearTimeout(wsTimer);
  if (es) { try { es.close(); } catch (e) {} es = null; }
  let sock;
  try {
    sock = new WebSocket(CFG.wsUrl);
  } catch (e) {
    return wsFailed('new WebSocket 抛错: ' + e.message);
  }
  ws = sock;

  let opened = false;
  sock.onopen = () => {
    opened = true;
    S.wsFailStreak = 0;
    setChannel('WS', 'WS 已连接 ' + CFG.wsUrl);
  };
  sock.onmessage = (ev) => { S.lastMsgAt = nowMs(); ingestText(typeof ev.data === 'string' ? ev.data : ''); };
  sock.onerror = () => { /* onclose 会接手 */ };
  sock.onclose = () => {
    if (ws === sock) ws = null;
    if (sock._handled) return;
    sock._handled = true;
    if (es) return;                       /* 已经切到 SSE，不再重复记失败 */
    if (S.channel === 'WS' || S.channel === 'INIT' || S.channel === 'DOWN' || opened) {
      wsFailed(opened ? 'WS 断开' : 'WS 连接失败');
    }
  };
}

function wsFailed(reason) {
  S.wsFailStreak++;
  S.reconnects++;
  setChannel('DOWN', reason + '（连续失败 ' + S.wsFailStreak + ' 次）');
  if (S.wsFailStreak >= CFG.wsFailsBeforeFallback) {
    S.fallbacks++;
    log('→ 连续失败达 ' + CFG.wsFailsBeforeFallback + ' 次，降级到 SSE');
    connectSSE();
    return;
  }
  const d = CFG.backoff[Math.min(S.wsFailStreak - 1, CFG.backoff.length - 1)];
  log('WS 重连退避 ' + (d / 1000) + 's');
  wsTimer = setTimeout(connectWS, d);
}

/* ---------------- SSE ---------------- */
function connectSSE() {
  clearTimeout(wsTimer);
  if (es) { try { es.close(); } catch (e) {} es = null; }
  if (typeof EventSource === 'undefined') { log('EventSource 不可用'); return simFallback(); }

  const cur = new EventSource(CFG.sseUrl);
  es = cur;
  let opened = false;

  cur.onopen = () => {
    opened = true;
    setChannel('SSE', 'SSE 已连接 ' + CFG.sseUrl);
  };
  cur.onmessage = (ev) => { S.lastMsgAt = nowMs(); ingestText(ev.data); };
  cur.onerror = () => {
    if (es !== cur) return;                       // 已被新连接取代
    if (!opened) {                                // 连都没连上（后端不可达）
      if (cur._failed) return;
      cur._failed = true;
      try { cur.close(); } catch (e) {}
      es = null;
      return sseFailed('SSE 无法连接');
    }
    /* 曾经连上过 → EventSource 会自愈重连，只更新状态 */
    S.reconnects++;
    setChannel('DOWN', 'SSE 断开（浏览器自动重连中）');
  };
}
function sseFailed(reason) {
  S.fallbacks++;
  setChannel('DOWN', reason);
  if (SIM_ENABLED) return simFallback();
  const streak = Math.max(1, Math.min(S.fallbacks, CFG.backoff.length));
  const d = CFG.backoff[streak - 1];
  log('SSE 失败 → ' + (d / 1000) + 's 后回到 WS 重试');
  sseTimer = setTimeout(() => { S.wsFailStreak = 0; connectWS(); }, d);
}

/* ---------------- 模拟源（仅 ?sim=1） ---------------- */
function simFallback() {
  if (!SIM_ENABLED) return;
  if (simTimer) return;
  setChannel('SIM', '⚠️ 已切到内置模拟源（?sim=1）—— 页面显示的不是 OKX 真实行情');
  const b = $('simBanner'); if (b) b.hidden = false;
  startSimFeed(ingestObject);
}
function stopSim() { if (simTimer) { clearInterval(simTimer); simTimer = null; } }

/* ---------------- 统一入口 ---------------- */
function ingestText(raw) { ingestObject(safeParse(raw)); }
function safeParse(raw) {
  try { return JSON.parse(raw); } catch (e) { return null; }
}
function ingestObject(m) {
  if (!m || typeof m !== 'object') return;
  S.msgTotal++;
  S.msgThisSec++;
  S.lastMsgAt = nowMs();
  if (m.ts && (!S.mainTs || m.ts > S.mainTs)) S.mainTs = m.ts;
  dispatch(m);
}
function dispatch(m) {
  switch (m.t) {
    case 'snapshot': onSnapshot(m); break;
    case 'ticker':   onTicker(m);   break;
    case 'trades':   onTrades(m);   break;
    case 'book':     onBook(m);     break;
    case 'candle':   onCandle(m);   break;
    case 'candles':  onCandles(m);  break;
    case 'oi':       onOI(m);       break;
    case 'funding':  onFunding(m);  break;
    case 'stats':    onStats(m);    break;
    case 'error':    onErrorMsg(m); break;
    default: /* 未知类型忽略，不报错 */ break;
  }
}

/* ============================================================================
   2. 消息处理
   ---------------------------------------------------------------------------
   兼容两套 payload 形状：
   (A) okx-relay 实测形状（权威）：
       snapshot.insts["BTC-USDT"] = { ...ticker字段, t:"snapshot_inst",
                                      book:{bids,asks,ts,seqId}, bbo:{...},
                                      candles:[{o,h,l,c,vol,confirm,ts,src}]  ← REST BACKFILL 真历史 1m
                                      ticks:[{sec,o,h,l,c,vol24h,n,ts}]        ← 1 秒聚合 bar
                                      trades:[{px,sz,side,ts,tradeId}] }
       snapshot.insts["BTC-USDT-SWAP"] = { ...ticker字段, oi:{...}, funding:{...} }
       snapshot.stats = <stats 对象>；stats.ring[inst]={ticks,trades}；stats.sub.{confirmed,expected}
   (B) 任务书里给的扁平形状（tickers[]/books[]/candles[]…），保留兼容分支。
   ==========================================================================*/
function onSnapshot(m) {
  /* --- (B) 扁平形状 --- */
  if (Array.isArray(m.instruments)) m.instruments.forEach((it) => upsertTickerDom(it.inst || it.instId));
  if (m.ticker) onTicker(m.ticker);
  if (Array.isArray(m.tickers)) m.tickers.forEach(onTicker);
  if (Array.isArray(m.trades)) m.trades.forEach(onTrades);
  if (Array.isArray(m.books)) m.books.forEach(onBook);
  if (Array.isArray(m.oi)) m.oi.forEach(onOI);
  if (Array.isArray(m.fundings)) m.fundings.forEach(onFunding);
  if (Array.isArray(m.candles)) m.candles.forEach(onCandles);
  else if (Array.isArray(m.candle1m)) m.candle1m.forEach(onCandles);

  if (m.upstream != null) S.upstream = m.upstream;

  /* --- (A) okx-relay 实测形状 ---
     顺序很重要：**先处理 e.candles（真 1m K线）再处理 e.ticks（1s bar）**。 */
  let nInst = 0, nBars = 0, nTrades = 0, nCandles = 0;
  if (m.insts && typeof m.insts === 'object') {
    for (const inst of Object.keys(m.insts)) {
      const e = m.insts[inst]; if (!e || typeof e !== 'object') continue;
      nInst++;
      /* 现货 + 合约：整条 ticker 字段平铺在 entry 上（t:'snapshot_inst'）。
         ⚠️ 只订了 oi/funding 的合约可能没有 last（null）→ 走 num() 判空显示 "--"，不显示 0。 */
      if (num(e.last) != null) {
        onTicker({
          inst: inst, last: e.last, chg24h_pct: e.chg24h_pct, vol24h: e.vol24h,
          bid: e.bid, ask: e.ask, ts: e.ts,
        });
        upsertTickerDom(inst);
      }
      if (e.book && (e.book.bids || e.book.asks)) onBook({ inst: inst, bids: e.book.bids, asks: e.book.asks, ts: e.book.ts });
      else if (e.bbo && (e.bbo.bids || e.bbo.asks)) onBook({ inst: inst, bids: e.bbo.bids, asks: e.bbo.asks, ts: e.bbo.ts });
      if (Array.isArray(e.trades) && e.trades.length) {
        nTrades += e.trades.length;
        onTrades({ inst: inst, rows: e.trades.slice().reverse() });   // 中继存的旧→新，转成新→旧
      }
      /* ① 真历史 1m K 线（中继 REST BACKFILL 预填）—— 必须优先 */
      if (Array.isArray(e.candles) && e.candles.length) {
        nCandles += e.candles.length;
        onCandles({ inst: inst, bars: e.candles, granularity: '1m' });
      }
      /* ② 1 秒聚合 bar → 补齐 candles 没覆盖到的分钟 */
      if (Array.isArray(e.ticks) && e.ticks.length) {
        nBars += e.ticks.length;
        onCandles({ inst: inst, bars: e.ticks, granularity: '1s' });
      }
      /* 合约：oi / funding */
      if (e.oi && typeof e.oi === 'object') onOI(e.oi);
      if (e.funding && typeof e.funding === 'object') onFunding(e.funding);
    }
  }
  if (m.stats && typeof m.stats === 'object') onStats(m.stats);

  log('snapshot 首帧: ' + nInst + ' 标的 / ' + nCandles + ' 根1m K线 / ' + nBars + ' 根1s bar / ' + nTrades + ' 笔成交 / upstream=' + (S.upstream || '--'));
}

function onErrorMsg(m) {
  const s = m.msg || m.message || JSON.stringify(m);
  log('UPSTREAM ' + (m.scope || '') + ': ' + s);
  if (m.scope === 'upstream' && m.level !== 'info') S.upstream = 'degraded';
  renderStats();
}

function slot(inst) {
  if (!S.insts.has(inst)) {
    S.insts.set(inst, { inst: inst, last: null, prev: null, chg: null, vol24h: null, ts: null, points: [] });
  }
  return S.insts.get(inst);
}

function onTicker(m) {
  const inst = m.inst || m.instId; if (!inst) return;
  const st = slot(inst);
  const prev = st.last;
  const last = num(m.last);
  if (last == null) return;
  pickPrimary(inst);
  st.prev = prev; st.last = last;
  if (num(m.chg24h_pct) != null) st.chg = num(m.chg24h_pct);
  if (num(m.vol24h) != null) st.vol24h = num(m.vol24h);
  st.ts = num(m.ts) || nowMs();

  st.points.push({ t: st.ts, v: last });
  if (st.points.length > CFG.maxPricePoints) st.points.splice(0, st.points.length - CFG.maxPricePoints);

  /* 实时生长当前这根 1m K 线：src='tick' 优先级最低，
     若该分钟已有 candle/agg1s 数据会被 applyBar 拒绝，绝不覆盖真实数据。 */
  const cs = candleStore(inst);
  applyBar(cs, segOf(Math.floor(st.ts / 1000)), last, last, last, last, st.vol24h, 0, 'tick', nextBarSeq());

  if (prev != null && last !== prev) flashPrice(inst, last > prev);
  updateTickerDom(inst);
  S.dirty.price = true;
}

function onTrades(m) {
  const inst = m.inst || m.instId; if (!inst || !Array.isArray(m.rows)) return;
  let t = S.trades.get(inst);
  if (!t) { t = { rows: [], stamps: [] }; S.trades.set(inst, t); }
  const parsed = [];
  for (const r of m.rows) {
    if (!r) continue;
    const px = num(Array.isArray(r) ? r[0] : r.px);
    const sz = num(Array.isArray(r) ? r[1] : r.sz);
    const side = Array.isArray(r) ? String(r[2] || '') : String(r.side || '');
    const ts = Array.isArray(r) ? num(r[3]) : num(r.ts);
    const tid = Array.isArray(r) ? r[4] : r.tradeId;
    if (px == null) continue;
    parsed.push({ px: px, sz: sz, side: side, ts: ts, id: tid });
  }
  if (!parsed.length) return;
  parsed.sort((a, b) => (b.ts || 0) - (a.ts || 0));   // 新→旧
  parsed.forEach((p) => { t.rows.unshift(p); t.stamps.push(nowMs()); });
  if (t.rows.length > CFG.maxTradeRows) t.rows.length = CFG.maxTradeRows;
  const cut = nowMs() - 5000;
  while (t.stamps.length && t.stamps[0] < cut) t.stamps.shift();
  t.rate = t.stamps.length / 5;

  if (inst === S.primary || S.primary == null) { setPrimary(inst); renderTrades(inst, parsed.length); }
  renderTradesHead(inst);
}

function onBook(m) {
  const inst = m.inst || m.instId; if (!inst) return;
  /* 中继已把 book 做成「每次都是可用的 bids/asks 全量视图」，直接重绘即可，不拼增量。 */
  const bids = normLevels(m.bids), asks = normLevels(m.asks);
  S.book.set(inst, { bids: bids, asks: asks, ts: num(m.ts) || nowMs() });
  pickPrimary(inst);
  if (inst === S.primary) S.dirty.depth = true;
}
function normLevels(arr) {
  const out = [];
  if (!Array.isArray(arr)) return out;
  for (const l of arr) {
    if (!l) continue;
    const p = num(l[0]);
    const s = num(l[1]);
    if (p != null && s != null) out.push([p, s]);
    if (out.length >= 60) break;
  }
  return out;
}

/* ---------------- K 线：中继 1m candle（主）+ 1s bar（补）+ ticker（兜底） ----------------
   数据优先级：RELAY candle(真 1m K线，REST BACKFILL 预填) > agg1s(1 秒 bar 合成) > tick(兜底)
   confirm 原样使用：0=未收盘（更新最后一根）/ 1=已收盘（追加新根）。 */
const BAR_MS = 60000;
const BAR_SRC = { RELAY_1S: 'RELAY 1s→1m', CANDLE: 'RELAY candle' };

function candleStore(inst) {
  if (!S.candles.has(inst)) S.candles.set(inst, { bars: [], src: BAR_SRC.RELAY_1S, maxSeq: null, appendedTotal: 0 });
  return S.candles.get(inst);
}
function segOf(sec) { return Math.floor((sec * 1000) / BAR_MS) * BAR_MS; }

/* selfSeq = 本页**细粒度**单调计数（每次落一根就 +1）。
   用途：① 丢弃过期/乱序的旧回填 ② 让真 1m candle 压过同一分钟的聚合/兜底数据。
   注意不能用 S.msgTotal——同一条 snapshot 里的多个调用会拿到同一个值，失去先后关系。 */
let BAR_SEQ = 0;
function nextBarSeq() { return ++BAR_SEQ; }

/* src: 'candle'(中继原样推送的 1m K线) | 'agg1s'(1 秒 bar 合成) | 'tick'(ticker 兜底) */
function applyBar(cs, segTs, o, h, l, c, vol, confirm, src, selfSeq) {
  if (selfSeq != null) {
    if (cs.maxSeq != null && selfSeq < cs.maxSeq) return false;      // 过期回填 → 丢弃
    cs.maxSeq = selfSeq;
  }
  const bars = cs.bars;
  let hit = -1;
  for (let i = bars.length - 1; i >= 0; i--) { if (bars[i].ts === segTs) { hit = i; break; } }
  if (hit >= 0) {
    const b = bars[hit];
    /* 优先级：candle > agg1s > tick。低优先级不许覆盖高优先级。 */
    const rank = { candle: 3, agg1s: 2, tick: 1 };
    if ((rank[src] || 0) < (rank[b.src] || 0)) return false;
    if (o != null && b.o == null) b.o = o;
    if (h != null) b.h = b.h == null ? h : Math.max(b.h, h);
    if (l != null) b.l = b.l == null ? l : Math.min(b.l, l);
    if (c != null) b.c = c;
    if (vol != null) b.vol = vol;
    if (confirm === 1) b.confirm = 1;
    if ((rank[src] || 0) > (rank[b.src] || 0)) b.src = src;          // 升级来源标记
    return false;
  }
  const bar = { ts: segTs, o: o, h: h, l: l, c: c, vol: vol, confirm: confirm, src: src || 'agg1s' };
  /* 按 ts 插入到正确位置（乱序回填也能拼出完整序列） */
  let pos = bars.length;
  while (pos > 0 && bars[pos - 1].ts > segTs) pos--;
  bars.splice(pos, 0, bar);
  if (bars.length > CFG.maxCandles) bars.splice(0, bars.length - CFG.maxCandles);
  cs.appendedTotal++;
  return true;
}

/* 批量回填：bars 可以是 1m candle（granularity='1m'）或 1 秒聚合 bar（'1s'） */
function onCandles(m) {
  const inst = m.inst || m.instId; if (!inst) return;
  const bars = Array.isArray(m.bars) ? m.bars : (Array.isArray(m.rows) ? m.rows : null);
  if (!bars || !bars.length) return;
  const cs = candleStore(inst);
  const is1m = m.granularity === '1m' || m.bar === '1m';
  const srcKind = is1m ? 'candle' : 'agg1s';
  if (is1m) cs.src = BAR_SRC.CANDLE;
  const sorted = bars.slice().sort((a, b) => (a.ts || 0) - (b.ts || 0));
  let o1 = null, h1 = null, l1 = null, c1 = null, vFirst = null, vLast = null, seg = null;
  let nPushes = 0;

  for (const b of sorted) {
    if (!b) continue;
    const bt = num(b.ts); if (bt == null) continue;
    const s = segOf(Math.floor(bt / 1000));     // 两种口径的 ts 都是「该根起点」
    if (seg === null) seg = s;
    if (s !== seg) {                            // 换分钟 → 收掉上一段
      if (applyBar(cs, seg, o1, h1, l1, c1, volDelta(vFirst, vLast), 0, srcKind, nextBarSeq())) nPushes++;
      o1 = h1 = l1 = c1 = null; vFirst = vLast = null; seg = s;
    }
    const bo = num(b.o), bh = num(b.h), bl = num(b.l), bc = num(b.c);
    const bv = num(b.vol24h != null ? b.vol24h : b.vol);
    if (o1 == null) { o1 = bo != null ? bo : bc; if (vFirst == null) vFirst = bv; }
    if (bh != null) h1 = h1 == null ? bh : Math.max(h1, bh);
    if (bl != null) l1 = l1 == null ? bl : Math.min(l1, bl);
    if (bc != null) c1 = bc;
    if (bv != null) vLast = bv;
  }
  if (seg !== null) { if (applyBar(cs, seg, o1, h1, l1, c1, volDelta(vFirst, vLast), 0, srcKind, nextBarSeq())) nPushes++; }

  pickPrimary(inst);
  if (inst === S.primary) S.dirty.price = true;
  log('K线回填 ' + inst + ': ' + sorted.length + ' 根' + (is1m ? '1m candle' : '1s bar') + ' → ' + cs.bars.length + ' 根1m');
}
/* ---- 现货 ↔ 合约 配对（OI / Funding 只在 SWAP 上有） ---- */
function swapOf(spotInst) { return spotInst ? spotInst + '-SWAP' : null; }
function spotOf(swapInst) { return swapInst ? String(swapInst).replace(/-SWAP$/, '') : null; }
function volDelta(a, b) { return (a == null || b == null || b < a) ? null : +(b - a).toFixed(3); }

/* 单根 candle 增量消息：confirm=0 更新最后一根 / confirm=1 追加新根（原样使用 confirm） */
function onCandle(m) {
  const inst = m.inst || m.instId; if (!inst) return;
  const o = num(m.o), h = num(m.h), l = num(m.l), c = num(m.c), ts = num(m.ts);
  if (c == null || ts == null) return;
  const cs = candleStore(inst);
  cs.src = BAR_SRC.CANDLE;
  const confirm = num(m.confirm) === 1 ? 1 : 0;
  pickPrimary(inst);
  /* 用中继给的 bar 起点 ts 对齐分钟（OKX candle ts = 该根起点） */
  applyBar(cs, segOf(Math.floor(ts / 1000)), o, h, l, c, num(m.vol), confirm, 'candle', nextBarSeq());
  if (inst === S.primary) S.dirty.price = true;
}

function onOI(m) {
  const inst = m.inst || m.instId; if (!inst) return;
  const v = num(m.oiUsd); if (v == null) return;
  const cur = S.oi.get(inst) || { oiUsd: null, prev: null };
  cur.prev = cur.oiUsd; cur.oiUsd = v; cur.ts = num(m.ts) || nowMs();
  S.oi.set(inst, cur);
  /* OI 只在 SWAP 上有；主标的是现货，所以按「现货↔SWAP」配对触发重绘 */
  if (spotOf(inst) === S.primary || S.primary == null) { pickPrimary(spotOf(inst) || inst); renderOI(S.primary); }
}

function onFunding(m) {
  const inst = m.inst || m.instId; if (!inst) return;
  const rate = num(m.rate);
  S.funding.set(inst, { rate: rate, nextTime: num(m.nextTime), ts: num(m.ts) || nowMs() });
  const base = spotOf(inst);
  if (base === S.primary || S.funding.size === 1) renderFunding(inst);
}

function onStats(m) {
  if (m.upstream != null) S.upstream = m.upstream;
  if (num(m.clients) != null) S.clients = num(m.clients);
  if (num(m.clients_ws) != null) S.clientsWs = num(m.clients_ws);
  if (num(m.clients_sse) != null) S.clientsSse = num(m.clients_sse);
  if (num(m.msgs_per_s) != null) S.msgsPerSec = num(m.msgs_per_s);
  if (num(m.msgs_total) != null) S.upMsgsTotal = num(m.msgs_total);
  if (m.reconnects && typeof m.reconnects === 'object') S.upReconnects = Object.values(m.reconnects).reduce((a, b) => a + (num(b) || 0), 0);
  else if (num(m.reconnects) != null) S.upReconnects = num(m.reconnects);
  if (m.sub && typeof m.sub === 'object') S.sub = m.sub;
  if (m.persist && typeof m.persist === 'object') S.persist = m.persist;
  if (m.ring && typeof m.ring === 'object') S.ring = m.ring;
  if (num(m.uptime_s) != null) S.upTimeSec = num(m.uptime_s);
  renderStats();
}

function setPrimary(inst) {
  if (!inst || inst === S.primary) return;
  S.primary = inst;
  renderFunding(swapOf(inst));
  renderOI(inst);
  renderTrades(inst, 0);
  S.dirty.depth = true; S.dirty.price = true;
}
/* 主标的选法：优先 BTC-USDT（有它就不让别的标的抢焦点），否则先到先得 */
function pickPrimary(candidate) {
  if (!candidate) return;
  if (S.primary == null) { setPrimary(candidate); return; }
  if (S.primary === candidate) return;
  if (candidate === 'BTC-USDT') setPrimary(candidate);
}
function num(v) {
  if (v == null || v === '') return null;
  const n = Number(v);
  return isFinite(n) ? n : null;
}

/* ============================================================================
   3. 渲染层
   ==========================================================================*/
/* ---------- 顶栏价格 ---------- */
function upsertTickerDom(inst) {
  if (!inst) return null;
  let el = document.querySelector('[data-inst="' + cssEsc(inst) + '"]');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'ticker';
  el.setAttribute('data-inst', inst);
  el.innerHTML = '<span class="t-sym"></span><span class="t-px">--</span><span class="t-chg">--</span>';
  el.querySelector('.t-sym').textContent = shortName(inst);
  const box = $('tbPrices'); if (box) box.appendChild(el);
  return el;
}
function updateTickerDom(inst) {
  const st = S.insts.get(inst); if (!st) return;
  const el = upsertTickerDom(inst); if (!el) return;
  const px = el.querySelector('.t-px'), chg = el.querySelector('.t-chg');
  px.textContent = fmtPx(st.last, st.last);
  const up = (st.chg || 0) >= 0;
  chg.textContent = (up ? '▲' : '▼') + fmtPct(Math.abs(st.chg || 0));
  chg.className = 't-chg ' + (up ? 'up' : 'down');
}
function flashPrice(inst, isUp) {
  const el = document.querySelector('[data-inst="' + cssEsc(inst) + '"]');
  if (!el) return;
  const px = el.querySelector('.t-px');
  const cls = isUp ? 'flash-up' : 'flash-down';
  px.classList.remove('flash-up', 'flash-down');
  void px.offsetWidth;                     // 强制回流，让 transition 重启
  px.classList.add(cls);
  px.classList.add(isUp ? 'tick-up' : 'tick-down');
  px.classList.remove(isUp ? 'tick-down' : 'tick-up');
  clearTimeout(px._flashT);
  px._flashT = setTimeout(() => px.classList.remove('flash-up', 'flash-down'), 90);
}
function cssEsc(s) { return String(s).replace(/["\\]/g, '\\$&'); }

/* ---------- 成交流 ---------- */
function renderTrades(inst, addedCount) {
  const box = $('tradeRows'); if (!box || !inst) return;
  const t = S.trades.get(inst);
  if (!t || !t.rows.length) return;
  if (box.querySelector('.empty')) box.innerHTML = '';

  if (addedCount > 0 && box.childElementCount > 0) {
    const frag = document.createDocumentFragment();
    for (let i = 0; i < addedCount && i < t.rows.length; i++) frag.appendChild(tradeRowEl(t.rows[i], true));
    box.insertBefore(frag, box.firstChild);              // 顶部插入，不整表重排
    while (box.childElementCount > CFG.maxTradeRows) box.removeChild(box.lastChild);
  } else {
    box.innerHTML = '';
    const frag = document.createDocumentFragment();
    const n = Math.min(t.rows.length, 120);
    for (let i = 0; i < n; i++) frag.appendChild(tradeRowEl(t.rows[i], false));
    box.appendChild(frag);
  }
}
function tradeRowEl(r, isNew) {
  const buy = r.side === 'buy';
  const d = document.createElement('div');
  d.className = 'trow ' + (buy ? 'buy' : 'sell') + (isNew ? ' new' : '');
  const px = document.createElement('span'); px.className = 'px'; px.textContent = fmtPx(r.px, r.px);
  const sz = document.createElement('span'); sz.className = 'sz'; sz.textContent = r.sz == null ? '--' : fmtCompact(r.sz);
  const sd = document.createElement('span'); sd.className = 'side'; sd.textContent = buy ? 'BUY' : 'SELL';
  const tm = document.createElement('span'); tm.className = 'tm'; tm.textContent = bjTime(r.ts);
  d.append(px, sz, sd, tm);
  return d;
}
function renderTradesHead(inst) {
  const el = $('tradesHead'); if (!el) return;
  if (!inst) { el.textContent = '-- · --/s'; return; }
  const t = S.trades.get(inst);
  el.textContent = inst + ' · ' + (t ? t.rate.toFixed(0) : '0') + '/s';
}

/* ---------- OI / FUNDING / STATS ---------- */
function renderOI(inst) {
  const swap = swapOf(spotOf(inst) || inst);
  const cur = S.oi.get(swap) || S.oi.get(inst);
  const el = $('oiUsd'), dEl = $('oiDelta'), hEl = $('oiHead');
  if (!el) return;
  hEl.textContent = swap || '--';
  if (!cur || cur.oiUsd == null) { el.textContent = '--'; dEl.textContent = '--'; dEl.className = 'delta'; return; }
  el.textContent = '$' + fmtCompact(cur.oiUsd);
  if (cur.prev != null) {
    const dv = cur.oiUsd - cur.prev;
    const dp = cur.prev ? (dv / cur.prev) * 100 : 0;
    dEl.textContent = (dv >= 0 ? '▲ +' : '▼ ') + fmtCompact(dv) + '  (' + fmtPct(dp) + ') vs prev';
    dEl.className = 'delta ' + (dv >= 0 ? 'up' : 'down');
    flashBig(el, dv >= 0);
  }
}
function renderFunding(inst) {
  const f = S.funding.get(inst);
  const hEl = $('fundingHead'), rEl = $('fundingRate'), nEl = $('fundingNext');
  if (!hEl) return;
  hEl.textContent = inst || '--';
  if (!f || f.rate == null) { rEl.textContent = '--'; nEl.textContent = '--'; return; }
  const pct = f.rate * 100;
  rEl.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(4) + '%';
  rEl.className = 'big ' + (pct >= 0 ? 'tick-up' : 'tick-down');
  nEl.textContent = f.nextTime ? bjTime(f.nextTime) + ' 北京时间' : '--';
}
function flashBig(el, isUp) {
  el.classList.remove('flash-up', 'flash-down');
  void el.offsetWidth;
  el.classList.add(isUp ? 'flash-up' : 'flash-down');
  setTimeout(() => el.classList.remove('flash-up', 'flash-down'), 90);
}
function renderStats() {
  const set = (id, v) => { const e = $(id); if (e) e.textContent = v; };
  const upTxt = S.upstream == null ? '--' : String(S.upstream);
  set('statClients', S.clients == null ? '--' : S.clients);
  set('statUp', upTxt);
  set('sChan', S.channel);
  set('sUp', upTxt + (S.upReconnects != null ? '  (reconnects ' + S.upReconnects + ')' : ''));
  set('sClients', S.clients == null ? '--'
    : S.clients + (S.clientsWs != null && S.clientsSse != null ? '  (ws ' + S.clientsWs + ' / sse ' + S.clientsSse + ')' : ''));
  set('sMsgTotal', (S.upMsgsTotal != null ? S.upMsgsTotal : S.msgTotal) + (S.upMsgsTotal != null ? ' (上游) / ' + S.msgTotal + ' (本端)' : ''));
  set('sMsgs', S.msgsPerSec == null ? '--' : S.msgsPerSec.toFixed(1));
  set('sTicks', S.msgTotal);
  set('sReconn', S.reconnects + ' 本页 / ' + (S.upReconnects != null ? S.upReconnects + ' 上游' : '--'));
  set('sFallback', S.fallbacks);
  set('sSub', S.sub ? (S.sub.confirmed + '/' + S.sub.expected + (S.sub.errors ? '  err ' + S.sub.errors : '')) : '--');
  set('sUpTime', S.upTimeSec == null ? '--' : S.upTimeSec.toFixed(0) + 's');
  set('statsHead', S.channel + ' · ' + upTxt);
  /* ring 只展示主标的，避免挤爆 */
  const ring = S.ring;
  if (ring && S.primary && ring[S.primary]) {
    const r = ring[S.primary];
    set('sRing', S.primary + '  ticks ' + (r.ticks != null ? r.ticks : '--') + '  trades ' + (r.trades != null ? r.trades : '--'));
  } else {
    set('sRing', ring ? Object.keys(ring).length + ' insts' : '--');
  }
  const p = S.persist;
  set('sPersist', p ? (p.rows != null ? p.rows : '--') + ' rows / ' + fmtCompact(p.bytes) + 'B  pending ' + (p.pending != null ? p.pending : '--') : '--');
}

/* ---------- 每秒节拍：msgs/s / tick 计数 / STALE 判定 / 心跳带 ---------- */
setInterval(function tick1s() {
  const now = nowMs();

  /* msgs/s：优先用中继 stats 推送值，否则用本端实收计数兜底 */
  const local = S.msgThisSec;
  S.msgThisSec = 0;
  const shown = (S.msgsPerSec != null && S.msgsPerSec > 0) ? S.msgsPerSec : local;
  const el = $('statMsgs');
  if (el) {
    el.textContent = Number(shown).toFixed(1);
    el.classList.remove('pulse'); void el.offsetWidth; el.classList.add('pulse');
  }
  const tEl = $('statTicks');
  if (tEl) { tEl.textContent = S.msgTotal; tEl.classList.remove('pulse'); void tEl.offsetWidth; tEl.classList.add('pulse'); }

  /* 心跳带：最近 60 秒 */
  S.hb.push(local);
  if (S.hb.length > 60) S.hb.shift();
  const hbEl = $('hbNow'); if (hbEl) hbEl.textContent = local;
  S.dirty.heart = true;

  /* STALE 判定 —— 不许假装还在实时 */
  const since = S.lastMsgAt ? now - S.lastMsgAt : Infinity;
  updateLive(since);

  /* 时钟 + 倒计时 */
  const ck = $('clock'); if (ck) ck.textContent = bjTime(now);
  const lm = $('sLastMsg');
  if (lm) lm.textContent = S.lastMsgAt ? (isFinite(since) ? (since / 1000).toFixed(0) + 's ago' : '--') : 'never';
  const fk = swapOf(S.primary) || [...S.funding.keys()][0];
  if (fk) {
    const f = S.funding.get(fk);
    const cd = $('fundingCountdown');
    if (f && f.nextTime && cd) cd.textContent = hmsDown(f.nextTime - now);
  }
  renderTradesHead(S.primary);
  renderStats();

  /* WS 看门狗：浏览器偶发不触发 onclose 时兜底，避免停在"假连接" */
  if (ws && (ws.readyState === WebSocket.CLOSING || ws.readyState === WebSocket.CLOSED)) {
    const w = ws; ws = null;
    if (!w._handled) { w._handled = true; wsFailed('WS 看门狗发现连接已关闭'); }
  }
}, 1000);

function updateLive(sinceMs) {
  const dot = $('liveDot'), txt = $('liveText');
  if (!dot || !txt) return;
  let state, label;
  if (S.channel === 'DOWN' && !S.lastMsgAt) { state = 'down'; label = 'DOWN'; }
  else if (S.channel === 'DOWN') { state = 'stale'; label = 'RECONNECTING'; }
  else if (sinceMs > CFG.staleMs) { state = 'stale'; label = 'STALE ' + Math.floor(sinceMs / 1000) + 's'; }
  else { state = 'up'; label = 'LIVE'; }
  /* 中继自报上游异常时，即使本地消息还在流，也不谎报 LIVE。
     ok / mock / sim 都算"健康"（mock、sim 仅用于自测）。 */
  const up = S.upstream == null ? null : String(S.upstream).toLowerCase();
  if (state === 'up' && up && !/^(ok|mock|sim|connected|live)$/.test(up)) {
    state = 'stale'; label = 'UPSTREAM ' + String(S.upstream).toUpperCase();
  }
  dot.className = 'live live-' + state;
  txt.textContent = label;
}

/* ============================================================================
   4. Canvas：深度图 / K 线 / 心跳带   —— rAF 节流，脏标记驱动
   ==========================================================================*/
function fitCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 300, h = cv.clientHeight || 150;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx: ctx, w: w, h: h };
}
const FONT = 'ui-monospace, "Cascadia Mono", Consolas, monospace';

/* ---------------- 订单簿深度图 ---------------- */
function drawDepth() {
  const cv = $('cvDepth'); if (!cv) return;
  const inst = S.primary; const bk = inst ? S.book.get(inst) : null;
  const empty = $('depthEmpty');
  if (!bk || (!bk.bids.length && !bk.asks.length)) { if (empty) empty.classList.remove('hide'); return; }
  if (empty) empty.classList.add('hide');

  const { ctx, w, h } = fitCanvas(cv);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#020202'; ctx.fillRect(0, 0, w, h);

  const bids = bk.bids.slice().sort((a, b) => b[0] - a[0]);  // 价格降序
  const asks = bk.asks.slice().sort((a, b) => a[0] - b[0]);  // 价格升序
  if (!bids.length || !asks.length) return;
  const bestBid = bids[0][0], bestAsk = asks[0][0];
  const mid = (bestBid + bestAsk) / 2;
  const spread = bestAsk - bestBid;
  const spreadBp = mid ? (spread / mid) * 10000 : 0;

  const pad = { l: 8, r: 8, t: 16, b: 18 };
  const iw = Math.max(10, w - pad.l - pad.r), ih = Math.max(10, h - pad.t - pad.b);

  /* 累计量 */
  const cumB = [], cumA = [];
  let s = 0; for (const l of bids) { s += l[1]; cumB.push([l[0], s]); }
  let s2 = 0; for (const l of asks) { s2 += l[1]; cumA.push([l[0], s2]); }
  const maxQ = Math.max(s || 0, s2 || 0) || 1;

  /* 横轴范围：以 mid 为中心左右各取同等价格跨度（对称） */
  const half = Math.max(mid - cumB[cumB.length - 1][0], cumA[cumA.length - 1][0] - mid) || (mid * 0.001);
  const xLo = mid - half, xHi = mid + half;
  const X = (p) => pad.l + ((p - xLo) / (xHi - xLo)) * iw;
  const Y = (q) => pad.t + ih - (q / maxQ) * ih;

  /* 网格 */
  ctx.strokeStyle = '#161616'; ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = pad.t + (ih * i) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + iw, y); ctx.stroke();
  }
  /* mid 分割线 */
  const xMid = Math.round(X(mid)) + 0.5;
  ctx.strokeStyle = '#2e2e2e';
  ctx.beginPath(); ctx.moveTo(xMid, pad.t); ctx.lineTo(xMid, pad.t + ih); ctx.stroke();

  /* 阶梯面积：bids 在左(绿)，asks 在右(红)，均从 mid 向两侧展开 */
  function stair(cum, color, mirror) {
    ctx.beginPath();
    ctx.moveTo(X(mid), Y(0));
    for (let i = 0; i < cum.length; i++) {
      const x = X(cum[i][0]);
      ctx.lineTo(x, Y(i === 0 ? 0 : cum[i - 1][1]));
      ctx.lineTo(x, Y(cum[i][1]));
    }
    ctx.lineTo(X(mirror ? xHi : xLo), Y(cum.length ? cum[cum.length - 1][1] : 0));
    ctx.lineTo(X(mirror ? xHi : xLo), Y(0));
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  }
  stair(cumB, 'rgba(0,224,122,0.30)', false);
  stair(cumA, 'rgba(255,59,70,0.30)', true);
  /* 轮廓线 */
  ctx.lineWidth = 1.4;
  ctx.strokeStyle = 'rgba(0,224,122,0.95)';
  ctx.beginPath();
  for (let i = 0; i < cumB.length; i++) { const x = X(cumB[i][0]), y = Y(cumB[i][1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
  ctx.stroke();
  ctx.strokeStyle = 'rgba(255,59,70,0.95)';
  ctx.beginPath();
  for (let i = 0; i < cumA.length; i++) { const x = X(cumA[i][0]), y = Y(cumA[i][1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
  ctx.stroke();

  /* 最优买卖价 / 价差 */
  ctx.font = '10px ' + FONT;
  ctx.textBaseline = 'top';
  ctx.fillStyle = 'rgba(0,224,122,1)';
  ctx.textAlign = 'left';
  ctx.fillText('BID ' + fmtPx(bestBid, mid), pad.l, 2);
  ctx.fillStyle = 'rgba(255,59,70,1)';
  ctx.textAlign = 'right';
  ctx.fillText('ASK ' + fmtPx(bestAsk, mid), pad.l + iw, 2);
  ctx.fillStyle = '#8a8a8a';
  ctx.textAlign = 'center';
  ctx.fillText('MID ' + fmtPx(mid, mid) + '   SPREAD ' + fmtPx(spread, mid) + ' (' + spreadBp.toFixed(2) + 'bp)', pad.l + iw / 2, 2);
  /* 底部价格刻度 */
  ctx.fillStyle = '#4a4a4a'; ctx.textBaseline = 'bottom';
  ctx.textAlign = 'left';  ctx.fillText(fmtPx(xLo, mid), pad.l, h - 3);
  ctx.textAlign = 'right'; ctx.fillText(fmtPx(xHi, mid), pad.l + iw, h - 3);
  ctx.textAlign = 'center'; ctx.fillText('CUM QTY MAX ' + fmtCompact(maxQ), pad.l + iw / 2, h - 3);

  const bh = $('bookHead');
  if (bh) bh.textContent = inst + ' · ' + bk.bids.length + 'x' + bk.asks.length + ' · ' + bjTime(bk.ts);
}

/* ---------------- K 线图（中继 1m candle + 增量） ----------------
   蜡烛：实体 + 影线，涨绿跌红；最后一根若 confirm=0 用半透明实体 + 亮边框，
   表示「这根还在长」——由真实推送触发重绘，非假动画。 */
function drawCandles() {
  const cv = $('cvPrice'); if (!cv) return;
  const inst = S.primary;
  const cs = inst ? S.candles.get(inst) : null;
  const bars = cs ? cs.bars : [];
  const empty = $('priceEmpty');
  if (!bars.length) { if (empty) empty.classList.remove('hide'); return; }
  if (empty) empty.classList.add('hide');

  const { ctx, w, h } = fitCanvas(cv);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#020202'; ctx.fillRect(0, 0, w, h);

  const pad = { l: 8, r: 8, t: 15, b: 16 };
  const iw = Math.max(10, w - pad.l - pad.r), ih = Math.max(10, h - pad.t - pad.b);

  let lo = Infinity, hi = -Infinity;
  for (const b of bars) { if (b.l < lo) lo = b.l; if (b.h > hi) hi = b.h; }
  if (!isFinite(lo) || !isFinite(hi)) return;
  if (lo === hi) { lo -= 1; hi += 1; }
  const padV = (hi - lo) * 0.06; lo -= padV; hi += padV;

  const n = bars.length;
  const slot = iw / n;
  const bw = Math.max(1, Math.min(slot * 0.68, 14));
  /* 300 根压缩到 ~780px 时，单根实体不足 1px：此时只画影线（高低范围），
     避免 300 次 fillRect 在每帧里白白开销。 */
  const thinMode = bw < 1.2;
  const Xc = (i) => pad.l + slot * (i + 0.5);
  const Y = (v) => pad.t + ih - ((v - lo) / (hi - lo)) * ih;

  /* 网格 */
  ctx.font = '10px ' + FONT;
  ctx.strokeStyle = '#141414'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (ih * i) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + iw, y); ctx.stroke();
  }

  /* 蜡烛 */
  const lastIdx = n - 1;
  for (let i = 0; i < n; i++) {
    const bar = bars[i];
    const up = bar.c >= bar.o;
    const base = up ? '0,224,122' : '255,59,70';
    const live = (i === lastIdx && bar.confirm === 0);
    const x = Xc(i);
    /* 影线 */
    ctx.strokeStyle = 'rgba(' + base + ',' + (live ? 1 : 0.85) + ')';
    ctx.lineWidth = Math.max(1, Math.min(2, bw * 0.18));
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, Y(bar.h));
    ctx.lineTo(Math.round(x) + 0.5, Y(bar.l));
    ctx.stroke();
    if (thinMode) continue;
    /* 实体 */
    const yO = Y(bar.o), yC = Y(bar.c);
    const top = Math.min(yO, yC);
    const hgt = Math.max(1, Math.abs(yC - yO));
    if (live) {
      ctx.fillStyle = 'rgba(' + base + ',0.42)';
      ctx.fillRect(x - bw / 2, top, bw, hgt);
      ctx.strokeStyle = 'rgba(' + base + ',1)';
      ctx.lineWidth = 1;
      ctx.strokeRect(Math.round(x - bw / 2) - 0.5, Math.round(top) - 0.5, bw + 1, hgt + 1);
    } else {
      ctx.fillStyle = 'rgba(' + base + ',0.92)';
      ctx.fillRect(x - bw / 2, top, bw, hgt);
    }
  }

  /* 当前价水平虚线 */
  const lastBar = bars[lastIdx];
  const yLast = Y(lastBar.c);
  const upAll = lastBar.c >= lastBar.o;
  ctx.save();
  ctx.setLineDash([3, 3]);
  ctx.strokeStyle = upAll ? 'rgba(0,224,122,0.55)' : 'rgba(255,59,70,0.55)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, yLast); ctx.lineTo(pad.l + iw, yLast); ctx.stroke();
  ctx.restore();

  /* 文字 */
  ctx.textBaseline = 'top'; ctx.textAlign = 'left';
  ctx.fillStyle = upAll ? 'rgba(0,224,122,1)' : 'rgba(255,59,70,1)';
  ctx.fillText((inst || '--') + '  ' + fmtPx(lastBar.c, lastBar.c) + '  1m', pad.l, 2);

  ctx.textAlign = 'center';
  if (lastBar.confirm === 0) {
    ctx.fillStyle = '#ffb000';
    ctx.fillText('● 未收盘 实时更新中 (confirm=0)', pad.l + iw / 2, 2);
  } else {
    ctx.fillStyle = '#6a6a6a';
    ctx.fillText('已收盘 (confirm=1)', pad.l + iw / 2, 2);
  }

  ctx.textAlign = 'right'; ctx.fillStyle = '#4a4a4a';
  ctx.fillText(fmtPx(hi, hi), pad.l + iw, pad.t + 2);
  ctx.fillText(fmtPx(lo, lo), pad.l + iw, pad.t + ih - 12);

  ctx.textBaseline = 'bottom'; ctx.textAlign = 'left';
  ctx.fillStyle = '#4a4a4a';
  ctx.fillText(n + ' bars   ' + bjTime(bars[0].ts) + ' → ' + bjTime(lastBar.ts), pad.l, h - 3);

  /* 来源统计：candle / agg1s / tick 各多少根 */
  let nC = 0, nA = 0, nT = 0;
  for (const b of bars) { if (b.src === 'candle') nC++; else if (b.src === 'agg1s') nA++; else nT++; }
  const ph = $('pxHead');
  if (ph) {
    ph.textContent = inst + ' · ' + n + ' bars · ' + (cs ? cs.src : '--') + (lastBar.confirm === 0 ? ' · LIVE' : ' · closed');
  }
  const pn = $('pxNote');
  if (pn) {
    pn.textContent = 'K线来源: ' + (cs ? cs.src : '--') + ' · 实际 ' + n + ' 根（1m candle ' + nC + ' / 1s聚合 ' + nA + ' / ticker兜底 ' + nT + '）'
      + ' · 未伪造历史 · 最后一根' + (lastBar.confirm === 0 ? '未收盘(实时生长)' : '已收盘')
      + ' · 北京时间 ' + bjTime(lastBar.ts);
  }
}

/* ---------------- 心跳带 ---------------- */
function drawHeart() {
  const cv = $('cvHeart'); if (!cv) return;
  const { ctx, w, h } = fitCanvas(cv);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#050505'; ctx.fillRect(0, 0, w, h);
  const data = S.hb;
  const max = Math.max(1, ...data);
  const n = 60, bw = w / n;
  for (let i = 0; i < n; i++) {
    const idx = data.length - n + i;
    if (idx < 0) continue;
    const v = data[idx];
    const bh = (v / max) * (h - 3);
    const x = i * bw;
    const a = 0.25 + 0.75 * (i / n);
    ctx.fillStyle = v > 0 ? 'rgba(0,224,122,' + a.toFixed(2) + ')' : 'rgba(60,60,60,0.5)';
    ctx.fillRect(x + 0.5, h - bh, Math.max(1, bw - 1.5), bh);
  }
  ctx.strokeStyle = '#1c1c1c';
  ctx.beginPath(); ctx.moveTo(0, h - 0.5); ctx.lineTo(w, h - 0.5); ctx.stroke();
}

/* ---------------- rAF 节流主循环 ---------------- */
function raf() {
  if (S.dirty.depth) { S.dirty.depth = false; try { drawDepth(); } catch (e) {} }
  if (S.dirty.price) { S.dirty.price = false; try { drawCandles(); } catch (e) {} }
  if (S.dirty.heart) { S.dirty.heart = false; try { drawHeart(); } catch (e) {} }
  requestAnimationFrame(raf);
}
requestAnimationFrame(raf);

window.addEventListener('resize', () => { S.dirty.depth = S.dirty.price = S.dirty.heart = true; }, { passive: true });

/* ============================================================================
   5. 内置模拟源（仅 ?sim=1；用于后端未就绪时打通渲染链路）
   —— 输出严格遵循与 okx-relay 约定的 JSON 协议
   ==========================================================================*/
function startSimFeed(emit) {
  const specs = [
    { inst: 'BTC-USDT', px: 83000, sz: 0.5 },
    { inst: 'ETH-USDT', px: 2500, sz: 6 },
    { inst: 'SOL-USDT', px: 178, sz: 40 },
  ];
  const books = {};
  specs.forEach((sp) => {
    const b = { inst: sp.inst, bids: [], asks: [] };
    for (let i = 0; i < CFG.bookRows; i++) {
      b.bids.push([sp.px - (i + 1) * sp.px * 0.00008, sp.sz * (1 + Math.random() * 8)]);
      b.asks.push([sp.px + (i + 1) * sp.px * 0.00008, sp.sz * (1 + Math.random() * 8)]);
    }
    books[sp.inst] = b;
  });

  const snap = { t: 'snapshot', ts: nowMs(), instruments: specs.map((s) => ({ inst: s.inst })) };
  snap.tickers = specs.map((s) => ({ inst: s.inst, last: s.px, chg24h_pct: (Math.random() * 4 - 2), vol24h: s.px * 1e4, ts: nowMs() }));
  snap.books = Object.values(books).map((b) => ({ inst: b.inst, bids: b.bids, asks: b.asks, ts: nowMs() }));
  snap.oi = specs.map((s) => ({ inst: s.inst + '-SWAP', oiUsd: s.px * 25000, ts: nowMs() }));
  snap.fundings = specs.map((s) => ({ inst: s.inst + '-SWAP', rate: 0.0001, nextTime: nowMs() + 3600e3, ts: nowMs() }));
  emit(snap);

  let seq = 0;
  simTimer = setInterval(() => {
    const now = nowMs();
    seq++;
    specs.forEach((sp) => {
      sp.px = Math.max(0.0001, sp.px + (Math.random() - 0.5) * sp.px * 0.00035);
      emit({ t: 'ticker', inst: sp.inst, last: sp.px, chg24h_pct: (Math.random() * 4 - 2), vol24h: sp.px * 1e4, ts: now });
    });
    Object.values(books).forEach((b) => {
      const sp = specs.find((x) => x.inst === b.inst);
      for (let k = 0; k < 1 + Math.floor(Math.random() * 3); k++) {
        const i = Math.floor(Math.random() * CFG.bookRows);
        const bid = Math.random() < 0.5;
        const side = bid ? b.bids : b.asks;
        const dir = bid ? -1 : 1;
        side[i] = [sp.px + dir * (i + 1) * sp.px * 0.00008, sp.sz * (1 + Math.random() * 8)];
      }
      b.ts = now;
      emit({ t: 'book', inst: b.inst, bids: b.bids, asks: b.asks, ts: now });
    });
    specs.forEach((sp) => {
      const rows = [];
      const n = 1 + Math.floor(Math.random() * 4);
      for (let i = 0; i < n; i++) {
        rows.push({
          px: sp.px + (Math.random() - 0.5) * sp.px * 0.0002,
          sz: +(Math.random() * sp.sz * 2).toFixed(4),
          side: Math.random() < 0.5 ? 'buy' : 'sell',
          ts: now - i * 137, tradeId: 'sim-' + seq + '-' + i,
        });
      }
      emit({ t: 'trades', inst: sp.inst, rows: rows });
    });
    if (seq % 20 === 0) specs.forEach((sp) => {
      emit({ t: 'oi', inst: sp.inst + '-SWAP', oiUsd: sp.px * 25000 * (1 + (Math.random() - 0.5) * 0.002), ts: now });
      emit({ t: 'funding', inst: sp.inst + '-SWAP', rate: (Math.random() - 0.5) * 0.0004, nextTime: now + 1800e3, ts: now });
    });
    if (seq % 4 === 0) {
      emit({
        t: 'stats', upstream: 'sim', clients: 1,
        msgs_per_s: +(specs.length * 8 + Math.random() * 6).toFixed(1),
        ring: { ticker: specs.length * 2, trades: 120, book: 60 }, ts: now,
      });
    }
  }, 250);
}

/* ============================================================================
   6. 启动
   ==========================================================================*/
function boot() {
  renderStats();
  updateLive(Infinity);
  log('启动：优先 WS ' + CFG.wsUrl + (SIM_ENABLED ? '（sim 模式：允许降级到内置模拟源）' : ''));
  connectWS();
  S.dirty.depth = S.dirty.price = S.dirty.heart = true;
}
window.addEventListener('error', (e) => { try { log('JS ERROR: ' + e.message); } catch (_) {} });
window.addEventListener('beforeunload', () => { stopSim(); });
boot();
