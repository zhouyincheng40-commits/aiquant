/* ============================================================================
   POLY // TERMINAL — render engine
   ----------------------------------------------------------------------------
   纯静态前端直连公共 API（无本地后端）。
   · Polymarket 三域名（gamma / lb-api / data-api）依赖本机代理 127.0.0.1:7890
   · A股市场带（qt.gtimg.cn）为一级独立数据源，代理挂掉仍可刷新
   取数 300s（gamma/lb-api 的 Cache-Control 即 max-age=300/600）；
   渲染 1s，用本地缓存重绘，因此数字变化的红/绿闪烁始终保留。

   硬约束：
     · 页面内不出现任何硬编码的价格 / 金额 / 地址 / 指数数值。
     · 取不到 → `--` / `NO DATA` / `OFFLINE`，绝不补假数字。
     · 编码：Polymarket 是 UTF-8（不做任何转换）；腾讯是 GBK（必须 TextDecoder('gbk')）。
   ========================================================================== */
'use strict';

var TICK_MS = 1000;                  // 渲染重绘（吃本地缓存，保证数字变色高亮）
var POLL_MS = 10000;                 // 调度器节奏：每 10s 检查一次"谁过期了"
var REQ_TIMEOUT_MS = 25000;

/* 按端点真实新鲜度**分级**取数（不是一刀切 300s）。实测 Cache-Control：
     data-api /positions  max-age=5    → 60s   ← 全系统唯一真正实时的通道
     gamma   /markets/keyset max-age=300 → 300s
     data-api /trades      max-age=300 → 300s
     lb-api  /profit       max-age=600 → 600s (1d 窗口 300s)
     qt.gtimg.cn (CN 带)   直连无代理   → 60s
   这样"实时"是真的：钱包/跟单/PNL 面板 60 秒一次新数据，而不是 300s 空转。 */
var TTL = { markets: 300000, trades: 300000, positions: 60000, lb: 600000, lb1d: 300000, band: 60000 };

var TRACKED_N = 7;
var COPY_WALLETS = 5;
var GAMMA_LIMIT = 100;            // keyset 硬上限就是 100（limit=200 也只给 100；offset 会 422）
var SCAN_LIMIT = 12;              // 扫描面板最多显示条数
var SCAN_EVENT_MAX = 14;          // 每轮最多展开的候选事件数（= 每轮 /events 请求数，线性增长）
var POSITIONS_LIMIT = 500;        // positions 会在 limit 处**静默截断** → 尽量取大，并显式检测截断
/* 可选热插 feed：poly-data 产出。fetch 是相对**页面 URL** 解析的，所以要给全：
   · 从 web/poly-terminal/ 起服务 → edges.json
   · 从 D:\量化 根起服务（页面在 /web/poly-terminal/）→ ../../data/poly/edges.json 或 /data/poly/edges.json */
var EDGE_FEEDS = ['edges.json', 'data/poly/edges.json', '../../data/poly/edges.json', '/data/poly/edges.json'];

/* 官方费率表（docs.polymarket.com/trading/fees）：fee = C × feeRate × p × (1 - p)
     Crypto 0.07 | Sports 0.05 | Finance 0.04 | Politics 0.04 | Economics 0.05
     Culture 0.05 | Weather 0.05 | Other/General 0.05 | Mentions 0.04 | Tech 0.04 | Geopolitics 0
   Makers are never charged fees. Only takers pay fees.
   用途**仅限** feeSchedule.rate 缺失时的分类兜底 —— 注意 Geopolitics 是 0，
   所以不能再用 max(已有, 0.05) 那种兜底（会把 0 抬到 0.05）。 */
var FEE_BY_CATEGORY = {
  crypto: 0.07, sports: 0.05, finance: 0.04, politics: 0.04, economics: 0.05,
  culture: 0.05, weather: 0.05, other: 0.05, general: 0.05, mentions: 0.04,
  tech: 0.04, geopolitics: 0
};
var FEE_RATE_FALLBACK = 0.05;     // 分类也识别不出时的 Other 值

/* ── 活性过滤（修「数字一直不变」）─────────────────────────────────────────
   用户报"屏幕上数字不动"。实测根因：`lb-api/profit?window=all` 的历史总盈利榜
   前 12 名里**只有 2 个还持仓**，其余是"退休账户"：/value=0、positions=0、
   最后一笔交易在两年前。盯着榜首那个 $22M 大户，面板当然永远是 0。
   → 三个实时面板改用 `window=1d` 榜 + 逐钱包查最后一笔成交时间做活性过滤。 */
var ACTIVE_WALLETS_N = 8;         // 面板显示几个活跃钱包
var ACTIVE_CANDIDATES = 16;       // 从 1d 榜取多少候选去过筛
var ACTIVE_MAX_AGE_H = 24;        // 超过 24h 没成交 → 不算活跃，剔除
var LB_STALE_AGE_D = 7;           // 榜单行的活性：>7 天视觉降权

var G = 'https://gamma-api.polymarket.com';
var EP = {
  /* ⚠ 实测：keyset 的分页游标无效（cursor/next_cursor/after/page 均返回同一页，
     offset 返回 0 条）→ 扫描范围固定为「按 volume24hr 降序的第一页」。见 README。 */
  gamma: G + '/markets/keyset?closed=false&limit=' + GAMMA_LIMIT + '&order=volume24hr&ascending=false',
  event: function (id) { return G + '/events/' + id; },
  /* 历史总榜：只给 ARBER LEADERBOARD 用（它的语义就是历史排行，不换成 1d） */
  profitAll: 'https://lb-api.polymarket.com/profit?window=all&limit=20',
  /* 24h 榜：**全部实时面板的活性候选源**（实测 8 个全部当日有成交） */
  profit1d: 'https://lb-api.polymarket.com/profit?window=1d&limit=20',
  trades: 'https://data-api.polymarket.com/trades?limit=40',
  tradesUser: function (a, n) { return 'https://data-api.polymarket.com/trades?user=' + a + '&limit=' + (n || 10); },
  /* 只取 1 条 → 用 timestamp 算 last_trade_age（活性判定） */
  lastTrade: function (a) { return 'https://data-api.polymarket.com/trades?user=' + a + '&limit=1'; },
  positions: function (a, n) { return 'https://data-api.polymarket.com/positions?user=' + a + '&limit=' + (n || POSITIONS_LIMIT); },
  /* 权威总额：实测 positions 被截断时 ΣcurrentValue 会少算 26%，
     而 /value 与未截断时的 ΣcurrentValue 差 ≤0.02% → 以 /value 为准 */
  value: function (a) { return 'https://data-api.polymarket.com/value?user=' + a; },
  /* A股/港股带：腾讯直连，s_ 精简格式，GBK（s_ 只有 12 字段，**不含时间戳**） */
  band: 'https://qt.gtimg.cn/q=s_sh000001,s_sz399001,s_sz399006,s_bj899050,s_hkHSI',
  /* 完整格式（88 字段）才有 [30] = 最近报价时间戳 20261009161400 →
     用它判断是否在交易时段，休市必须标 CLOSED（休市还标 LIVE 等于骗用户） */
  bandClock: 'https://qt.gtimg.cn/q=sh000001',
  bandCodes: ['s_sh000001', 's_sz399001', 's_sz399006', 's_bj899050', 's_hkHSI']
};

var CACHE = { scanner: null, wallets: null, markets: null, copy: null, pnl: null, leaderboard: null, band: null, bandNote: null };
var HEALTH = {};
var PNL_SAMPLES = [];
var LAST_OK_TS = null;
var PROXY_DOWN = false;
var SCAN_META = { events: 0, rejected: 0, incomplete: 0, range: GAMMA_LIMIT };

/* ── 按源 TTL 缓存：每个数据源各自计龄，互不牵连 ─────────────────────────── */
var SRC = {};                     // name -> { ts, value }
function cached(name, ttlMs, loader) {
  var rec = SRC[name];
  if (rec && (Date.now() - rec.ts) < ttlMs) return Promise.resolve(rec.value);
  return loader().then(function (v) { SRC[name] = { ts: Date.now(), value: v }; return v; });
}
function srcAge(name) { return SRC[name] ? (Date.now() - SRC[name].ts) : null; }

/* ── 工具 ───────────────────────────────────────────────────────────────── */
function q(root, sel) { return root ? root.querySelector(sel) : null; }
function qa(root, sel) { return root ? Array.prototype.slice.call(root.querySelectorAll(sel)) : []; }
function num(v) { var n = typeof v === 'number' ? v : parseFloat(v); return (v === null || v === '' || v === undefined) ? null : (isFinite(n) ? n : null); }
function jarr(v) { if (Array.isArray(v)) return v; if (typeof v === 'string') { try { var p = JSON.parse(v); return Array.isArray(p) ? p : []; } catch (e) { return []; } } return []; }

function fmtCompact(n, noDollar) {
  if (n === null) return '--';
  var sign = n < 0 ? '-' : '';
  var a = Math.abs(n), s;
  if (a >= 1e9) s = (a / 1e9).toFixed(1) + 'B';
  else if (a >= 1e6) s = (a / 1e6).toFixed(1) + 'M';
  else if (a >= 1e3) s = (a / 1e3).toFixed(1) + 'k';
  else s = a.toFixed(a < 10 ? 2 : 0);
  return sign + (noDollar ? '' : '$') + s;
}
function fmtMoney(n, dp) {
  if (n === null) return '--';
  var sign = n < 0 ? '-' : '', a = Math.abs(n);
  var d = dp === undefined ? (a < 100 && a % 1 !== 0 ? 2 : 0) : dp;
  return sign + '$' + a.toFixed(d).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}
function fmtCents(p) { var n = num(p); return n === null ? '--' : (Math.round(n * 1000) / 10) + '\u00A2'; }
function fmtSign(v, fn) { return v === null ? '--' : (v > 0 ? '+' : '') + fn(Math.abs(v)); }
function truncAddr(a) { return (!a || typeof a !== 'string') ? '--' : (a.length > 12 ? a.slice(0, 6) + '...' + a.slice(-4) : a); }
function hhmmss(s) { var n = num(s); if (n === null) return '--'; var d = new Date(n * 1000); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2); }
function hhmmssNow(ts) { var d = new Date(ts); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2); }
/* 距今时长：用户要的是"一眼判断该不该看它"，所以给人话而不是时间戳 */
function ageText(tsSec) {
  var n = num(tsSec);
  if (n === null) return 'no trades';
  var s = Date.now() / 1000 - n;
  if (s < 0) s = 0;
  if (s < 90) return 'last trade ' + Math.round(s) + 's ago';
  if (s < 5400) return 'last trade ' + Math.round(s / 60) + 'm ago';
  if (s < 172800) return 'last trade ' + (s / 3600).toFixed(1) + 'h ago';
  return 'last trade ' + Math.floor(s / 86400) + 'd ago';
}
function ageHours(tsSec) {
  var n = num(tsSec);
  return n === null ? null : (Date.now() / 1000 - n) / 3600;
}
function lastTradeTs(addr) {
  return cached('lasttrade:' + String(addr).slice(0, 10), TTL.trades, function () {
    return fetchJSON(EP.lastTrade(addr))
      .then(function (j) { return (Array.isArray(j) && j[0]) ? num(j[0].timestamp) : null; })
      .catch(function () { return null; });
  });
}
function clockHHMM() { var d = new Date(); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2); }
function tickFloor(v, tick) { if (!tick || tick <= 0) return v; return Math.floor(v / tick + 1e-9) * tick; }

/* ── 闪烁高亮（终端灵魂）───────────────────────────────────────────────── */
function setText(node, txt, tone) {
  if (!node) return;
  var prev = node.getAttribute('data-v');
  var s = (txt === null || txt === undefined || txt === '') ? (txt === '' ? '' : '--') : String(txt);
  if (prev === s) return;
  node.textContent = s;
  node.setAttribute('data-v', s);
  if (prev === null || prev === undefined) return;
  var dir = (tone === 'up' || tone === 'down') ? tone : null;
  if (!dir) {
    var a = parseFloat(String(prev).replace(/[^0-9.\-]/g, ''));
    var b = parseFloat(s.replace(/[^0-9.\-]/g, ''));
    if (isFinite(a) && isFinite(b) && a !== b) dir = b > a ? 'up' : 'down';
  }
  if (!dir) return;
  node.classList.remove('flash-up', 'flash-down');
  void node.offsetWidth;
  node.classList.add(dir === 'up' ? 'flash-up' : 'flash-down');
}

function keyedList(container, items, keyFn, tplId, update) {
  var host = container.parentNode;
  var empty = q(host, '.empty');
  if (!items.length) {
    container.textContent = '';
    if (!empty) host.insertBefore(document.getElementById('tpl-empty').content.cloneNode(true), container);
    return;
  }
  if (empty) empty.remove();
  var existing = {};
  qa(container, '[data-row]').forEach(function (el) { existing[el.getAttribute('data-key')] = el; });
  var seen = {};
  items.forEach(function (it, i) {
    var k = String(keyFn(it, i));
    seen[k] = true;
    var el = existing[k];
    if (!el) {
      var frag = document.getElementById(tplId).content.cloneNode(true);
      el = q(frag, '[data-row]');
      el.setAttribute('data-key', k);
      container.appendChild(frag);
    }
    update(el, it, i);
    container.appendChild(el);
  });
  Object.keys(existing).forEach(function (k) { if (!seen[k]) existing[k].remove(); });
}

function setPanelState(name, state, note) {
  HEALTH[name] = state;
  var p = document.querySelector('[data-panel="' + name + '"]');
  if (!p) return;
  var map = { ok: 'OK', empty: 'NO DATA', nodata: 'NO DATA', offline: 'RED / OFFLINE', loading: 'LOADING' };
  var s = q(p, '[data-p="state"]');
  if (s) { s.textContent = (note ? note + ' \u00B7 ' : '') + (map[state] || state); s.className = 'p-state st-' + state; }
  p.setAttribute('data-state', state);
}

function setEmptyText(panel, txt) {
  var p = document.querySelector('[data-panel="' + panel + '"]');
  if (!p) return;
  var e = q(p, '.empty');
  if (!e) return;
  var t = q(e, '.empty-txt');
  if (t) t.textContent = txt;
}

/* 面板离线且无缓存数据时，给一个显式占位，不留空白黑框 */
function ensurePlaceholder(panel, txt) {
  var p = document.querySelector('[data-panel="' + panel + '"]');
  if (!p) return;
  var body = q(p, '.p-body');
  if (!body) return;
  if (qa(body, '[data-row]').length) return;          // 有真实数据就不覆盖
  if (!q(p, '.empty')) {
    body.textContent = '';
    p.insertBefore(document.getElementById('tpl-empty').content.cloneNode(true), q(p, '.p-foot'));
  }
  setEmptyText(panel, txt || 'OFFLINE');
}

/* ── HTTP ───────────────────────────────────────────────────────────────── */
function withTimeout(url) {
  var ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  var t = setTimeout(function () { if (ctl) ctl.abort(); }, REQ_TIMEOUT_MS);
  var sep = url.indexOf('?') >= 0 ? '&' : '?';
  return fetch(url + sep + '_t=' + Date.now(), { cache: 'no-store', signal: ctl ? ctl.signal : undefined })
    .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r; })
    .then(function (r) { clearTimeout(t); return r; }, function (e) { clearTimeout(t); throw e; });
}
function fetchJSON(url) { return withTimeout(url).then(function (r) { return r.json(); }); }

/* 腾讯 GBK：必须显式用 gbk 解，默认 UTF-8 会把中文指数名变乱码 */
function fetchGBK(url) {
  return withTimeout(url).then(function (r) { return r.arrayBuffer(); }).then(function (buf) {
    var dec;
    try { dec = new TextDecoder('gbk'); }
    catch (e) { dec = new TextDecoder('utf-8'); CACHE.bandNote = 'GBK DECODER UNAVAILABLE'; }
    return dec.decode(buf);
  });
}

/* ══════════════════════ 面板 1：MISPRICING SCANNER ══════════════════════
   口径（Lead 指定，逐字实现）：定义性套利，Σ 价格偏离 1.00。
   多结果市场（同一 event 下的候选结果，每个候选=一个二元 Yes/No 市场）：
     cost      = Σ bestAsk_i        edge_buy  = 1 - cost        edge_buy_pct  = (1/cost - 1)*100
     proceeds  = Σ bestBid_i        edge_sell = proceeds - 1    edge_sell_pct = (proceeds - 1)*100
   硬约束：任一结果缺 bestAsk/bestBid → 整条剔除；扣 feeSchedule.rate 与 Σ spread 后报净值；
           EDGE 按 orderPriceMinTickSize 截断；扣费后<=0 标 NOT VIABLE。
   注：这是定义性套利，与截图里基于私有模型的 EDGE 不是同一个东西，不可直接比较。
   ──────────────────────────────────────────────────────────────────────── */
function loadScanner() {
  var ov = new URLSearchParams(location.search).get('edges');
  /* feed 探测结果也缓存（TTL.markets）：否则每 10s 轮询都去 probe 一次不存在的
     edges.json，既刷控制台 404 又白费请求。热插场景最多晚 300s 生效。 */
  return cached(ov ? 'edgefeed:' + ov : 'edgefeed', TTL.markets, function () {
    var feeds = ov ? [ov] : EDGE_FEEDS.slice();
    function tryFeed(i) {
      if (i >= feeds.length) return Promise.resolve(null);
      return fetchJSON(feeds[i]).then(function (j) {
        /* 支持两种 shape：
           A) poly-data 的文档式 {items:[...], counts:{...}, filter_chain:[...], fee_source}
           B) 简单数组 / {edges:[...]} */
        var arr = Array.isArray(j) ? j : (j && Array.isArray(j.items) ? j.items
          : (j && Array.isArray(j.edges) ? j.edges : null));
        if (!arr || !arr.length) return tryFeed(i + 1);
        var doc = (j && !Array.isArray(j)) ? j : {};
        return { source: feeds[i], rows: arr.map(mapFeedRow), doc: doc };
      }).catch(function () { return tryFeed(i + 1); });
    }
    return tryFeed(0);
  }).then(function (r) {
    if (!r) {
      CACHE.feedMeta = null;
      return loadScannerComputed();
    }
    /* feed 的剔除原因分布 —— 比只显示一个总数更有说服力 */
    CACHE.feedMeta = summarizeFeed(r.doc, r.source);
    return r.rows;
  });
}

/* 把 feed 里的一行映射成渲染行。兼容 poly-data 的字段名与更简单的自定义 feed。 */
function mapFeedRow(e, idx) {
  var buyNet = num(e.net_buy_pct), sellNet = num(e.net_sell_pct);
  var buyGross = num(e.edge_buy_pct), sellGross = num(e.edge_sell_pct);
  var cost = num(e.cost), proceeds = num(e.proceeds);
  var pct = null, grossPct = null, side = '--';
  if (buyNet !== null || sellNet !== null) {
    var useBuyF = (buyNet !== null && (sellNet === null || buyNet >= sellNet));
    pct = useBuyF ? buyNet : sellNet;
    grossPct = useBuyF ? buyGross : sellGross;
    side = useBuyF ? 'BUY ALL' : 'SELL ALL';
  } else {
    pct = num(e.edge_pct);
    if (pct === null && num(e.edge) !== null) pct = num(e.edge) * 100;
    grossPct = num(e.gross_pct);
    side = String(e.side || '').toUpperCase();
  }
  var viable = (e.viable === true) || (e.viable === undefined && pct !== null && pct > 0);
  var legs = num(e.outcomes_n) !== null ? num(e.outcomes_n) : (num(e.legs_quoted) !== null ? num(e.legs_quoted) : num(e.legs));
  var zombies = num(e.zombie_legs);
  var px = num(e.price);
  /* 大字价格：优先用 feed 给的 cost/proceeds（Σ 口径），否则退回单价 */
  var big = (side === 'BUY ALL') ? cost : proceeds;
  if (big === null) big = px;
  var parts = [side];
  if (legs !== null) parts.push(legs + ' OUT');
  if (zombies !== null) parts.push('\u2620' + zombies);
  if (e.complete_set === false) parts.push('INCOMPLETE');
  if (e.neg_risk === false) parts.push('NON-EXCL');
  if (!viable) parts.push('NOT VIABLE');
  return {
    id: e.conditionId || e.id || ('f' + idx),
    market: e.question || e.market || '--',
    yes_text: proceeds === null ? '--' : '\u03A3BID ' + fmtCents(proceeds),
    no_text: cost === null ? '--' : '\u03A3ASK ' + fmtCents(cost),
    /* 主数值 = **离可行最近的那一侧的净边际**（这张表唯一的用途是回答"现在有没有得做"，
       "还差 3.29 个点"比"总和 100¢"有用得多）。Σ 总和降为辅助小字。 */
    price_text: pct === null ? '--' : (pct > 0 ? '+' : '') + pct.toFixed(2) + '%',
    net: pct, netDir: (pct === null ? 'flat' : (pct > 0 ? 'up' : 'down')),
    fair_text: '\u03A3BID ' + (proceeds === null ? '--' : fmtCents(proceeds))
      + '  \u03A3ASK ' + (cost === null ? '--' : fmtCents(cost)),
    gross_text: grossPct === null ? '--' : 'GROSS ' + (grossPct > 0 ? '+' : '') + grossPct.toFixed(2) + '%',
    vol24_text: fmtCompact(num(e.volume24hr)),
    volume_text: fmtCompact(num(e.volume)),
    edge_text: pct === null ? '--' : (pct > 0 ? '+' : '') + pct.toFixed(2) + '%',
    edge_tone: viable ? 'up' : 'dim',
    viable: viable,
    side: parts.join(' \u00B7 ')
  };
}

/* 从 feed 文档里抽出「剔除原因分布」与出处，供面板脚注显示 */
function summarizeFeed(doc, source) {
  if (!doc || typeof doc !== 'object') return { source: source, flags: '', counts: null };
  var c = doc.counts || null;
  var bits = [];
  if (c) {
    if (c.sets_considered !== undefined) bits.push('sets ' + c.sets_considered);
    if (c.sets_kept !== undefined) bits.push('kept ' + c.sets_kept);
    if (c.rejected_zombie_quotes) bits.push('zombie ' + c.rejected_zombie_quotes);
    if (c.rejected_incomplete_set) bits.push('incompl ' + c.rejected_incomplete_set);
    if (c.rejected_non_mutually_exclusive) bits.push('nonexcl ' + c.rejected_non_mutually_exclusive);
    if (c.rejected_nominal_positive) bits.push('nominal+ ' + c.rejected_nominal_positive);
  }
  var zl = 0;
  if (Array.isArray(doc.rejected_sets)) {
    doc.rejected_sets.forEach(function (r) {
      var f = r.reject_flags || [];
      if (f.indexOf && f.indexOf('ZOMBIE_QUOTES') >= 0) zl += (num(r.zombie_legs) || 0);
    });
    if (zl) bits.push('zombie legs ' + zl);
  }
  return { source: source, counts: c, flags: bits.join(' \u00B7 '),
           feeSource: doc.fee_source || null, scope: doc.scope || null,
           generatedAt: doc.generated_at_bjt || null };
}

/* keyset 第一页：MARKETS 与 SCANNER 共用同一个缓存条目 → 每 300s 只打一次这个端点 */
function keysetPage() {
  return cached('keyset', TTL.markets, function () {
    return fetchJSON(EP.gamma).then(function (j) {
      return (j && (j.markets || (Array.isArray(j) ? j : null))) || [];
    });
  });
}

function loadScannerComputed() {
  return keysetPage().then(function (ms) {
    SCAN_META.range = ms.length;
    var byEv = {};
    ms.forEach(function (m) {
      var ev = (Array.isArray(m.events) && m.events[0] && m.events[0].id) ? String(m.events[0].id) : null;
      if (!ev) return;
      if (!byEv[ev]) byEv[ev] = { id: ev, title: (m.events[0].title || m.question), markets: [] };
      byEv[ev].markets.push(m);
    });
    /* 候选 = 第一页出现**至少 1 条腿**的 event（不是 ≥2）。
       实测：NBA 2027 Champion 全集 36 条腿，某页里可能只出现 1~2 条；
       要求 ≥2 会把整个集合漏掉。真实腿数一律以 /events 为准。
       按该 event 在本页的成交量降序，优先扫最活跃的集合。 */
    var groups = Object.keys(byEv).map(function (k) { return byEv[k]; })
      .sort(function (a, b) { return volSum(b) - volSum(a); })
      .slice(0, SCAN_EVENT_MAX);
    SCAN_META.events = groups.length;
    return Promise.all(groups.map(function (g) {
      /* 用 /events/<id> 取**全量**候选结果（第一页往往只含其中一部分） */
      return cached('event:' + g.id, TTL.markets, function () { return fetchJSON(EP.event(g.id)); })
        .then(function (ev) {
          return {
            id: g.id, title: ev.title || g.title, vol24: num(ev.volume24hr),
            /* negRisk=true 才是「互斥且穷尽」的结果集合 —— 只有这种集合 Σ 价格偏离 1.00 才是套利。
               非互斥事件（如 "ceasefire through <日期A/B/C>"）Σ 价格天然远大于 1，必须剔除。 */
            negRisk: (ev.negRisk === true || ev.enableNegRisk === true),
            /* ⚠ 这里**不能**先 filter 掉 closed 的腿：那会悄悄把残缺集合当完整集合算。
               实测 Dota 事件 34 条腿里 14 条已关闭、Brazil 32 条里 17 条已关闭。 */
            markets: (ev.markets || [])
          };
        }).catch(function () { return null; });
    }));
  }).then(function (groups) {
    var rows = [], rejected = 0, nonExcl = 0, incomplete = 0, metaFeeByCategory = 0;
    (groups || []).forEach(function (g) {
      if (!g || !g.markets || g.markets.length < 2) return;
      if (!g.negRisk) { nonExcl++; return; }        // 非互斥集合 → Σ 价格不构成套利，整条排除
      /* ── 集合完整度 + 僵尸腿硬校验 ──
         每条腿都必须「未关闭 && 未停用 && 仍在接单 && 同时有 bestAsk 与 bestBid」，
         且可用腿数必须等于事件声明腿数。任一条不满足 → 整条剔除并计数。
         注意思路：**不是"把坏腿摘掉就干净"，而是"有僵尸腿 → 整个集合不可信 → 整条剔除"**。
         去掉某条腿会改变 Σ 的含义，等于把残缺集合当完整集合算。
         （实测：单页分组会算出 NBA 2/36 腿那种 +99.5% 假套利；poly-data 复核 96 个集合里
           僵尸报价 47 个、集合不完整 73 个、腿非互斥 56 个 —— 近一半集合会被污染。） */
      var usable = [], broken = false, feeByCategory = 0, feeRate = FEE_RATE_FALLBACK;
      g.markets.forEach(function (m) {
        if (broken) return;
        if (m.closed === true) { broken = true; return; }              // 已结算
        if (m.active === false) { broken = true; return; }             // 已停用
        if (m.acceptingOrders === false) { broken = true; return; }    // 停用但仍在发报价 = 僵尸腿
        var outs = jarr(m.outcomes);
        var ask = num(m.bestAsk), bid = num(m.bestBid);
        if (outs.length !== 2 || ask === null || bid === null) { broken = true; return; }
        /* 费率：**逐市场读 feeSchedule.rate 为准**；缺失时才按官方分类表兜底 */
        var fr = (m.feeSchedule && num(m.feeSchedule.rate));
        if (fr === null || fr === undefined) { fr = feeForMarket(m, g); feeByCategory++; }
        usable.push({ m: m, ask: ask, bid: bid, rate: fr, spread: num(m.spread) || 0,
                      tick: num(m.orderPriceMinTickSize) || 0.01 });
      });
      if (broken || usable.length !== g.markets.length || usable.length < 2) { incomplete++; return; }
      var cost = 0, proceeds = 0, spreadSum = 0, tick = 0.01, rates = [];
      usable.forEach(function (u) {
        cost += u.ask; proceeds += u.bid; spreadSum += u.spread; tick = u.tick;
        if (u.rate !== null && u.rate !== undefined) rates.push(u.rate);
      });
      if (cost <= 0) { rejected++; return; }
      /* 一个集合里各腿费率可能不同（实测 0.04~0.05 浮动）→ 取最大者，成本按最保守估 */
      feeRate = rates.length ? Math.max.apply(null, rates) : FEE_RATE_FALLBACK;
      metaFeeByCategory += feeByCategory;
      var grossBuy = 1 - cost, grossSell = proceeds - 1;
      if (grossBuy <= 0 && grossSell <= 0) return;                     // 毛额就没空间 → 不显示
      /* 选边按「毛额」：先指出错价在哪一侧；再用扣费后的净值判可行性。
         （若按净值选边，Σspread 会在多腿集合上压过一切，导致判边失真。）*/
      var useBuy = grossBuy >= grossSell;
      var grossPct = useBuy ? (grossBuy / cost) * 100 : grossSell * 100;
      /* 净值只扣 taker 手续费 feeSchedule.rate；Σspread 单独显示，不折进百分比
         （36 腿集合的原始 Σspread 量级会给出 -600% 这种无信息量的数字，见 README）*/
      /* 官方费率模型：fee = C × feeRate × p × (1-p)，只对 taker 收。
         按「每条腿各自 p、各自 rate」逐腿累加，而不是把费率乘在总名义额上。 */
      var feeSum = 0;
      usable.forEach(function (u) {
        var p = useBuy ? u.ask : u.bid;
        var r = (u.rate === null || u.rate === undefined) ? FEE_RATE_FALLBACK : u.rate;
        feeSum += r * p * (1 - p);
      });
      var netPct = useBuy
        ? (tickFloor(1 - cost - feeSum, tick) / (cost + feeSum)) * 100
        : tickFloor(proceeds - 1 - feeSum, tick) * 100;
      var viable = netPct > 0;
      rows.push({
        id: g.id, market: g.title,
        yes_text: 'ΣBID ' + fmtCents(proceeds), no_text: 'ΣASK ' + fmtCents(cost),
        vol24_text: fmtCompact(g.vol24),
        /* 主数值 = 离可行最近那侧的净边际；Σ 总和降为辅助小字（同上） */
        price_text: (netPct > 0 ? '+' : '') + netPct.toFixed(2) + '%',
        net: netPct, netDir: netPct > 0 ? 'up' : 'down',
        fair_text: '\u03A3BID ' + fmtCents(proceeds) + '  \u03A3ASK ' + fmtCents(cost),
        gross_text: 'GROSS ' + (grossPct > 0 ? '+' : '') + grossPct.toFixed(2) + '%',
        volume_text: fmtCompact(g.vol24),
        edge_pct: netPct,
        edge_text: (netPct > 0 ? '+' : '') + netPct.toFixed(2) + '%',
        /* 关键：不成立的机会必须是**最弱**视觉权重（dim），
           否则"没得做"会比"有得做"更显眼，语义就反了。 */
        edge_tone: viable ? 'up' : 'dim',
        viable: viable,
        side: (useBuy ? 'BUY ALL' : 'SELL ALL') + ' \u00B7 ' + usable.length + ' OUT \u00B7 \u03A3SPR ' + fmtCents(spreadSum)
          + (viable ? '' : ' \u00B7 NOT VIABLE'),
        sort: Math.abs(grossPct)
      });
    });
    rows.sort(function (a, b) { return b.sort - a.sort; });
    SCAN_META.rejected = rejected;
    SCAN_META.incomplete = incomplete;
    SCAN_META.feeByCategory = metaFeeByCategory;
    CACHE.scanMeta = { rejected: rejected, nonExcl: nonExcl, incomplete: incomplete,
                       feeByCategory: metaFeeByCategory,
                       events: SCAN_META.events - nonExcl - incomplete, range: SCAN_META.range };
    return rows.slice(0, SCAN_LIMIT);
  });
}

/* feeSchedule.rate 缺失时按官方分类表兜底（分类从事件/市场的可见字段尽力推断，
   推断不出按 Other=0.05）。**注意 Geopolitics 官方费率是 0，不能被抬成 0.05。** */
function feeForMarket(m, g) {
  var cands = [
    m.category, m.groupItemTitle, (m.events && m.events[0] && m.events[0].category),
    (g.markets[0].events && g.markets[0].events[0] && g.markets[0].events[0].category)
  ];
  var tags = [];
  if (Array.isArray(m.tags)) m.tags.forEach(function (t) { tags.push(typeof t === 'string' ? t : (t && t.label)); });
  if (g.markets[0].events && Array.isArray(g.markets[0].events[0].tags)) {
    g.markets[0].events[0].tags.forEach(function (t) { tags.push(typeof t === 'string' ? t : (t && t.label)); });
  }
  cands = cands.concat(tags);
  for (var i = 0; i < cands.length; i++) {
    var c = cands[i];
    if (!c || typeof c !== 'string') continue;
    var k = c.toLowerCase().trim();
    if (Object.prototype.hasOwnProperty.call(FEE_BY_CATEGORY, k)) return FEE_BY_CATEGORY[k];
  }
  return FEE_RATE_FALLBACK;
}

function volSum(g) {
  var s = 0;
  g.markets.forEach(function (m) { s += (num(m.volume24hr) || 0); });
  return s;
}

/* ═══ 唯一数据源访问器 ═══
   面板标签与顶栏计数**必须**读同一个 list，否则会出现"标签=4、顶栏=10"这种
   同数不同源的 bug（feed 路径 10 行 vs 内置路径 4 行时最容易踩）。 */
function scannerRows() { return CACHE.scanner || []; }

function renderScanner(rowsIgnored) {
  var c = q(document, '[data-list="scanner"]');
  var tag = q(document, '[data-p="scanner_tag"]');
  var rows = scannerRows();          /* ← 忽略入参，强制单一来源 */
  /* 两层语义：红色「N EDGES FOUND」只给**可执行**的机会（扣费后净值为正）；
     扫到但都不成立时用暗灰标签。绝不让 NOT VIABLE 被计成 EDGE ——
     否则顶栏"N EDGES"会和面板里的 NOT VIABLE 打架，用户以为有 N 个机会可以动手。 */
  var nViable = rows.filter(function (r) { return r.viable; }).length;
  var nTotal = rows.length;
  if (tag) {
    if (nViable > 0) {
      tag.textContent = nViable + ' EDGES FOUND';
      tag.className = 'tag tag-red';
    } else if (nTotal > 0) {
      tag.textContent = '0 VIABLE / ' + nTotal + ' SCAN';
      tag.className = 'tag tag-dim';
    } else {
      tag.textContent = '0 EDGES';
      tag.className = 'tag tag-dim';
    }
  }
  keyedList(c, rows, function (it, i) { return it.id || ('s' + i); }, 'tpl-scanner-row', function (el, it) {
    setText(q(el, '[data-f="market"]'), it.market);
    setText(q(el, '[data-f="yes"]'), it.yes_text);
    setText(q(el, '[data-f="no"]'), it.no_text);
    setText(q(el, '[data-f="vol24"]'), it.vol24_text);
    var pe = q(el, '[data-f="price"]');
    setText(pe, it.price_text, it.netDir === 'up' ? 'up' : (it.netDir === 'down' ? 'down' : null));
    if (pe) pe.setAttribute('data-dir', it.netDir || 'flat');
    setText(q(el, '[data-f="fair"]'), it.fair_text);
    setText(q(el, '[data-f="volume"]'), it.volume_text);
    setText(q(el, '[data-f="edge_pct"]'), it.gross_text || it.edge_text);
    setText(q(el, '[data-f="side"]'), it.side);
    var b = q(el, '[data-f="edge_badge"]');
    if (b) { b.classList.remove('up', 'down', 'strong', 'flat', 'dim'); b.classList.add(it.edge_tone || 'flat'); }
  });
  var m = CACHE.scanMeta;
  var fm = CACHE.feedMeta;
  var foot = fm && fm.flags ? ('feed \u00B7 ' + fm.flags)
    : (m ? ('scan ' + m.range + ' mkts \u00B7 ' + m.events + ' complete \u00B7 excl '
            + m.nonExcl + ' nonexcl + ' + m.incomplete + ' incompl/zombie'
            + (m.feeByCategory ? ' \u00B7 fee by category ' + m.feeByCategory : '')) : '');
  setPanelState('scanner', nTotal ? 'ok' : 'empty', foot);
  if (!nTotal) setEmptyText('scanner', 'NO DEFINITIONAL ARB IN SCAN RANGE');
}

/* ══════════════════════ 面板 2：WALLET TRACKER ══════════════════════
   ⚠ 数据源从 `profit?window=all`（历史总榜）改为 **`window=1d` + 活性过滤**。
   原因（实测）：历史总榜前 12 名里只有 2 个还持仓，其余 /value=0、最后一笔交易
   在两年前 —— 那是"退休账户"，面板显示 0 且永远 0，正是用户报的"数字一直不变"。 */
function walletValue(addr) {
  return cached('wval:' + String(addr).slice(0, 10), TTL.positions, function () {
    return fetchJSON(EP.value(addr)).then(function (j) {
      if (Array.isArray(j) && j.length) return num(j[0].value);
      return num(j && j.value);
    }).catch(function () { return null; });
  });
}
function loadActiveWallets() {
  return cached('lb_1d', TTL.lb1d, function () { return fetchJSON(EP.profit1d); })
    .then(function (list) {
      var cands = (Array.isArray(list) ? list : []).slice(0, ACTIVE_CANDIDATES);
      return Promise.all(cands.map(function (w) {
        var addr = String(w.proxyWallet || '');
        return Promise.all([lastTradeTs(addr), walletValue(addr)]).then(function (pair) {
          return { w: w, addr: addr, ts: pair[0], ageH: ageHours(pair[0]), value: pair[1] };
        });
      }));
    })
    .then(function (rows) {
      /* ① 活性门槛：最后一笔成交必须在 24h 内（排除退休账户）
         ② 资金门槛：/value > 0 优先 —— /value=0 的钱包进面板只会一直显示 0 */
      var live = rows.filter(function (r) { return r.ageH !== null && r.ageH <= ACTIVE_MAX_AGE_H; });
      live.sort(function (a, b) {
        var af = (a.value !== null && a.value > 0) ? 0 : 1;   // 有资金的排前面
        var bf = (b.value !== null && b.value > 0) ? 0 : 1;
        if (af !== bf) return af - bf;
        return a.ageH - b.ageH;                               // 再看谁更新
      });
      return live.slice(0, ACTIVE_WALLETS_N).map(function (r) {
        var amt = num(r.w.amount);
        var funded = (r.value !== null && r.value > 0);
        return {
          address: r.addr, addressShort: truncAddr(r.addr),
          /* 活性标签取代原来的 WHALE/SCOUT 排名标签 —— 用户要一眼看出"它现在还动" */
          kind: 'ACTIVE', kindClass: 'k-active',
          amount: amt, amount24h: amt, pseudonym: r.w.pseudonym || null,
          value: r.value, funded: funded,
          lastTradeTs: r.ts, lastTradeAgeH: r.ageH, ageText: ageText(r.ts),
          valueText: r.value === null ? 'VAL --'
            : (funded ? 'VAL ' + fmtMoney(r.value, 0) : 'VAL 0 \u00B7 FLAT'),
          pnlText: fmtCompact(amt),
          pnlDir: amt === null ? null : (amt >= 0 ? 'up' : 'down'),
          subText: (r.w.pseudonym ? String(r.w.pseudonym).slice(0, 18) : '--')
        };
      });
    });
}
function loadWallets() { return loadActiveWallets(); }
function renderWallets(rows) {
  var tag = q(document, '[data-p="wallets_tag"]');
  if (tag) tag.textContent = rows.length + ' TRACKED';
  keyedList(q(document, '[data-list="wallets"]'), rows, function (it) { return it.address; }, 'tpl-wallet-row', function (el, it) {
    setText(q(el, '[data-f="address"]'), truncAddr(it.address));
    var k = q(el, '[data-f="kind"]');
    setText(k, it.kind);
    if (k) { k.classList.remove('k-whale', 'k-top', 'k-active'); k.classList.add(it.kindClass || 'k-active'); }
    setText(q(el, '[data-f="pnl"]'), it.pnlText, it.pnlDir);
    setText(q(el, '[data-f="win"]'), it.subText);
    /* 活性字段：让用户不用问"为什么不动" */
    var ae = q(el, '[data-f="age"]');
    setText(ae, it.ageText);
    if (ae) ae.setAttribute('data-age', (it.lastTradeAgeH !== null && it.lastTradeAgeH <= ACTIVE_MAX_AGE_H) ? 'fresh' : 'stale');
    /* /value 直接可见：0 就是 0，不让用户猜 */
    var ve = q(el, '[data-f="value"]');
    setText(ve, it.valueText);
    if (ve) ve.setAttribute('data-funded', it.funded ? 'yes' : 'no');
  });
  setPanelState('wallets', rows.length ? 'ok' : 'empty');
}

/* ══════════════════════ 面板 3：LIVE MARKETS ══════════════════════ */
function loadMarkets() {
  return keysetPage().then(function (ms) {
    var rows = [];
    ms.forEach(function (m) {
      if (!m.question) return;
      var prices = jarr(m.outcomePrices).map(num), outs = jarr(m.outcomes);
      var yes = prices.length ? prices[0] : null, no = prices.length > 1 ? prices[1] : null;
      var chg = num(m.oneDayPriceChange), basis = '24H';
      if (chg === null) { chg = num(m.oneHourPriceChange); basis = '1H'; }
      var pct = chg === null ? null : chg * 100;
      var dir = pct === null ? 'flat' : (pct > 0 ? 'up' : (pct < 0 ? 'down' : 'flat'));
      rows.push({
        id: m.id, market: m.question,
        meta1: 'VOL24 ' + fmtCompact(num(m.volume24hr)),
        meta2: 'LIQ ' + fmtCompact(num(m.liquidity)),
        meta3: basis,
        yes_text: fmtCents(yes), no_text: fmtCents(no),
        yes_chg_text: pct === null ? '--' : ((dir === 'up' ? '\u25B2' : dir === 'down' ? '\u25BC' : '') + Math.abs(pct).toFixed(1) + '% ' + basis),
        no_chg_text: '\u00A0',           /* NO 侧无独立涨跌字段 → 不编 */
        yes_chg_dir: dir, no_chg_dir: 'flat'
      });
    });
    return rows;
  });
}
function renderMarkets(rows) {
  var tag = q(document, '[data-p="markets_tag"]');
  if (tag) tag.textContent = 'WATCHING ' + rows.length;
  keyedList(q(document, '[data-list="markets"]'), rows, function (it, i) { return it.id || ('m' + i); }, 'tpl-market-row', function (el, it) {
    setText(q(el, '[data-f="market"]'), it.market);
    setText(q(el, '[data-f="category"]'), it.meta1);
    setText(q(el, '[data-f="volume"]'), it.meta2);
    setText(q(el, '[data-f="yes_c"]'), it.yes_text);
    setText(q(el, '[data-f="no_c"]'), it.no_text);
    var yc = q(el, '[data-f="yes_chg"]'), nc = q(el, '[data-f="no_chg"]');
    setText(yc, it.yes_chg_text, it.yes_chg_dir);
    setText(nc, it.no_chg_text);
    if (yc) yc.setAttribute('data-dir', it.yes_chg_dir || 'flat');
  });
  setPanelState('markets', rows.length ? 'ok' : 'empty');
}

/* ══════════════════════ 面板 4：COPYTRADE ENGINE ══════════════════════ */
function buildTrades(list, limit) {
  list.sort(function (a, b) { return (num(b.timestamp) || 0) - (num(a.timestamp) || 0); });
  return list.slice(0, limit).map(function (t) {
    var side = String(t.side || '').toUpperCase();
    var notional = (num(t.size) || 0) * (num(t.price) || 0);
    var title = String(t.title || '');
    var shortTitle = title.length > 26 ? title.slice(0, 25) + '\u2026' : title;
    return {
      key: (t.transactionHash || '') + '|' + (t.asset || '') + '|' + (t.timestamp || ''),
      address: String(t.proxyWallet || ''), side: side,
      action: (shortTitle ? shortTitle + '  ' : '') + (side || '--') + ' ' + String(t.outcome || '').toUpperCase(),
      size_text: fmtMoney(notional, 0) + '  ' + hhmmss(t.timestamp)
    };
  });
}
function loadCopy(wallets) {
  var tops = wallets.slice(0, COPY_WALLETS).map(function (w) { return w.address; });
  var key = 'copy:' + (tops[0] || 'none').slice(0, 10);
  return cached(key, TTL.trades, function () {
    return Promise.all(tops.map(function (a) {
      return fetchJSON(EP.tradesUser(a, 10)).then(function (j) { return Array.isArray(j) ? j : []; }).catch(function () { return null; });
    })).then(function (lists) {
      if (!lists.some(function (l) { return l && l.length; })) {
        return fetchJSON(EP.trades).then(function (j) { return { rows: buildTrades(Array.isArray(j) ? j : [], 8), note: 'GLOBAL FEED' }; });
      }
      var merged = [];
      lists.forEach(function (l) { if (l) merged = merged.concat(l); });
      return { rows: buildTrades(merged, 8), note: 'LIVE MODE' };
    });
  });
}
function renderCopy(rows, note) {
  var tag = q(document, '[data-p="copy_tag"]');
  if (tag) tag.textContent = note || 'LIVE MODE';
  keyedList(q(document, '[data-list="copy"]'), rows, function (it) { return it.key; }, 'tpl-copy-row', function (el, it) {
    setText(q(el, '[data-f="address"]'), truncAddr(it.address));
    var a = q(el, '[data-f="action"]');
    setText(a, it.action);
    if (a) a.setAttribute('data-side', it.side);
    setText(q(el, '[data-f="size"]'), it.size_text, it.side === 'BUY' ? 'up' : 'down');
  });
  setPanelState('copy', rows.length ? 'ok' : 'empty');
}

/* ══════════════════════ 面板 5：PNL ══════════════════════ */
/* positions 会在 limit 处**静默截断**（实测 limit=500 返回恰好 500 条，
   ΣcurrentValue 比权威总额少算 26%）→ 因此：
     · /value 作**权威总额**
     · 返回条数 == limit → 标 TRUNCATED，且**禁止**把 ΣcurrentValue 当总额
     · 空数组 + /value==0 → 真的没持仓；空数组 + /value>0 → UNKNOWN（保守处理） */
function loadPnl(wallets) {
  var tops = wallets.slice(0, COPY_WALLETS).map(function (w) { return w.address; });
  var key = 'pnl:' + (tops[0] || 'none').slice(0, 10);
  return cached(key, TTL.positions, function () { return pnlFetch(tops); });
}
function pnlFetch(tops) {
  return Promise.all(tops.map(function (a) {
    return Promise.all([
      fetchJSON(EP.positions(a, POSITIONS_LIMIT))
        .then(function (j) { return Array.isArray(j) ? j : []; })
        .catch(function () { return null; }),
      fetchJSON(EP.value(a))
        .then(function (j) {
          if (Array.isArray(j) && j.length) return num(j[0].value);
          return num(j && j.value);
        })
        .catch(function () { return null; })
    ]).then(function (pair) { return { positions: pair[0], value: pair[1] }; });
  })).then(function (per) {
    var okPos = per.filter(function (x) { return x.positions !== null; });
    if (!okPos.length) return { total: null, reason: 'positions api red', offline: true };

    /* ⚠ /value == 0 的钱包**不得进入 PNL 曲线** —— 它们是退休账户（0 持仓 0 价值），
       放进来会让曲线永远是一条 0 直线，正是用户看到的"数字不动"。 */
    var contributors = per.filter(function (x) {
      if (x.value === 0) return false;
      if (x.value === null) return !!(x.positions && x.positions.length);   // 价值未知但有持仓
      return true;
    });
    var zeroValueExcluded = per.length - contributors.length;

    var pos = [], truncatedWallets = 0, unknownWallets = 0, valueSum = 0, valueKnown = 0;
    contributors.forEach(function (x) {
      if (x.positions) {
        pos = pos.concat(x.positions);
        if (x.positions.length >= POSITIONS_LIMIT) truncatedWallets++;
        if (!x.positions.length) unknownWallets++;      // 有价值但 0 持仓 → 从未观测到，保守判 UNKNOWN
      }
      if (x.value !== null) { valueSum += x.value; valueKnown++; }
    });

    var vals = [];
    pos.forEach(function (p) { var v = num(p.cashPnl); if (v !== null) vals.push(v); });
    if (!vals.length && !valueKnown) {
      return { total: null, offline: false,
               reason: zeroValueExcluded ? 'all tracked wallets /value=0 (dormant)' : 'no open positions',
               zeroValueExcluded: zeroValueExcluded };
    }
    var total = vals.reduce(function (a, b) { return a + b; }, 0);
    var truncated = truncatedWallets > 0;
    var prev = PNL_SAMPLES.length ? PNL_SAMPLES[PNL_SAMPLES.length - 1].v : null;
    var delta = prev === null ? null : total - prev;
    /* 采样点**每次真实取数记一个**（60s 一次），这样开页两轮后就有 ≥2 点可画；
       渲染循环是 1s，若按渲染记会把序列灌成秒级重复值。 */
    PNL_SAMPLES.push({ t: Date.now(), v: total });
    if (PNL_SAMPLES.length > 120) PNL_SAMPLES.shift();
    return {
      total: total,
      /* 截断时总额不完整 → 用 ≥ 明示，绝不把残缺 Σ 当总额 */
      total_text: (truncated ? '\u2265' : '') + fmtSign(total, function (v) { return fmtMoney(v, 0); }),
      truncated: truncated, truncatedWallets: truncatedWallets,
      unknownWallets: unknownWallets,
      zeroValueExcluded: zeroValueExcluded,
      /* 权威总额来自 /value */
      value: valueKnown ? valueSum : null,
      value_wallets: valueKnown,
      delta: delta,
      delta_text: delta === null ? '--' : fmtSign(delta, function (v) { return fmtMoney(v, 0); }),
      delta_dir: delta === null ? null : (delta > 0 ? 'up' : (delta < 0 ? 'down' : 'flat')),
      meta: vals.length + ' POS \u00B7 ' + contributors.length + ' ACTIVE W'
        + (zeroValueExcluded ? ' \u00B7 excl ' + zeroValueExcluded + ' dormant' : '')
        + (truncated ? ' \u00B7 TRUNCATED \u2265' + truncatedWallets : ''),
      note: PNL_SAMPLES.length + ' session samples'
    };
  });
}
function sparkline(points) {
  var svg = document.getElementById('pnlSpark');
  if (!svg) return;
  svg.textContent = '';
  if (!points || points.length < 2) return;
  var w = 200, h = 60, pad = 2;
  var min = Math.min.apply(null, points), max = Math.max.apply(null, points), span = max - min;
  var flat = (span === 0);
  var step = (w - pad * 2) / (points.length - 1);
  var d = points.map(function (v, i) {
    var x = pad + i * step;
    var y = flat ? h / 2 : (h - pad - ((v - min) / span) * (h - pad * 2));
    return (i ? 'L' : 'M') + x.toFixed(2) + ' ' + y.toFixed(2);
  }).join(' ');
  var ns = 'http://www.w3.org/2000/svg';
  var a = document.createElementNS(ns, 'path');
  a.setAttribute('d', d + ' L' + (w - pad) + ' ' + h + ' L' + pad + ' ' + h + ' Z');
  a.setAttribute('class', 'spark-area');
  var line = document.createElementNS(ns, 'path');
  line.setAttribute('d', d); line.setAttribute('class', 'spark-line');
  if (!flat) svg.appendChild(a);            // 无变化时不画填充，避免整块绿色看着像故障
  svg.appendChild(line);
}
function renderPnl(d) {
  var eT = q(document, '[data-p="pnl_total"]'), eD = q(document, '[data-p="pnl_delta"]'), eM = q(document, '[data-p="pnl_meta"]');
  var eV = q(document, '[data-p="pnl_value"]');
  if (!d || d.total === null) {
    setText(eT, 'NO DATA'); if (eT) eT.setAttribute('data-dir', 'flat');
    setText(eM, d && d.reason ? d.reason : '');
    setText(eV, '');
    setText(eD, '--'); sparkline(null);
    setPanelState('pnl', (d && d.offline) ? 'offline' : 'nodata', (d && d.reason) ? d.reason : ''); return;
  }
  setText(eT, d.total_text, d.total_dir);
  if (eT) eT.setAttribute('data-dir', d.total >= 0 ? 'up' : 'down');
  /* 权威总额（来自 /value），不是被 limit 截断的 ΣcurrentValue */
  if (eV) {
    eV.textContent = d.value === null ? 'VALUE --'
      : 'VALUE ' + fmtMoney(d.value, 0) + (d.value_wallets ? ' \u00B7 ' + d.value_wallets + ' W' : '');
  }
  setText(eD, d.delta_text, d.delta_dir === 'flat' ? null : d.delta_dir);
  if (eD) eD.setAttribute('data-dir', d.delta_dir || 'flat');
  setText(eM, d.meta);
  /* 采样点已在 pnlFetch 里按"每次真实取数"记好，这里只负责画 */
  sparkline(PNL_SAMPLES.map(function (s) { return s.v; }));
  setPanelState('pnl', 'ok', 'session samples ' + PNL_SAMPLES.length
    + (d.zeroValueExcluded ? ' \u00B7 excl ' + d.zeroValueExcluded + ' dormant' : '')
    + (d.truncated ? ' \u00B7 TRUNCATED \u2265' + d.truncatedWallets : ''));
}

/* ══════════════════════ 面板 6：ARBER LEADERBOARD ══════════════════════
   语义就是**历史总排行**，数据源保持 `window=all` 不换。
   但必须加活性标注：否则用户会把"历史战绩第一"误读成"现在最值得跟"。 */
function loadLeaderboard() {
  return Promise.all([
    cached('lb_all', TTL.lb, function () { return fetchJSON(EP.profitAll); }),
    cached('lb_1d', TTL.lb1d, function () { return fetchJSON(EP.profit1d); }).catch(function () { return []; })
  ])
    .then(function (r) {
      var all = Array.isArray(r[0]) ? r[0] : [], d1 = Array.isArray(r[1]) ? r[1] : [];
      var m1 = {};
      d1.forEach(function (x) { if (x && x.proxyWallet) m1[String(x.proxyWallet).toLowerCase()] = num(x.amount); });
      return Promise.all(all.map(function (w, i) {
        var addr = String(w.proxyWallet || ''), amt = num(w.amount), p1 = m1[addr.toLowerCase()];
        return lastTradeTs(addr).then(function (ts) {
          var ageH = ageHours(ts);
          return {
            address: addr, rank: i + 1, label: w.pseudonym || truncAddr(addr),
            amount: amt, amount24h: (p1 === undefined ? null : p1),
            lastTradeTs: ts, lastTradeAgeH: ageH, ageText: ageText(ts),
            stale: (ageH === null || ageH > LB_STALE_AGE_D * 24),   /* >7d → 视觉降权 */
            has1d: (p1 !== undefined && p1 !== null), pos1d: (p1 !== undefined && p1 !== null && p1 > 0),
            pnlText: fmtSign(amt, function (v) { return fmtCompact(v); }),
            pnlDir: amt === null ? null : (amt >= 0 ? 'up' : 'down'),
            rightText: (p1 === undefined || p1 === null) ? '--'
              : fmtSign(p1, function (v) { return fmtCompact(v) + ' 24H'; })
          };
        });
      }));
    });
}
function renderLeaderboard(rows) {
  var tag = q(document, '[data-p="leaderboard_tag"]');
  if (tag) tag.textContent = String(rows.length);
  keyedList(q(document, '[data-list="leaderboard"]'), rows, function (it) { return it.address + '#' + it.rank; }, 'tpl-leader-row', function (el, it) {
    setText(q(el, '[data-f="rank"]'), '#' + it.rank);
    setText(q(el, '[data-f="address"]'), it.label);
    setText(q(el, '[data-f="pnl"]'), it.pnlText, it.pnlDir);
    setText(q(el, '[data-f="tx"]'), it.rightText);
    /* 活性标注：历史排行 ≠ 现在还能跟。>7 天无成交 → 整行降权（dim） */
    var ae = q(el, '[data-f="age"]');
    setText(ae, it.ageText);
    if (ae) ae.setAttribute('data-age', it.stale ? 'stale' : 'fresh');
    el.setAttribute('data-stale', it.stale ? 'yes' : 'no');
  });
  /* 右列是「该地址在 1d 榜上的盈亏」；两个榜单地址不重叠时全部为 --（不编数字）*/
  var ov = rows.filter(function (x) { return x.has1d; }).length;
  var stale = rows.filter(function (x) { return x.stale; }).length;
  setPanelState('leaderboard', rows.length ? 'ok' : 'empty',
    'historical · 24H overlap ' + ov + '/' + rows.length + ' \u00B7 ' + stale + ' stale >7d');
}

/* ══════════════════════ A股/港股市场带（一级独立数据源）══════════════════════
   腾讯 GBK，s_ 精简格式字段位置（本机实测）：
     [1]名称 [3]现价 [4]涨跌额 [5]涨跌幅% [6]成交量(手) [7]成交额(万元)
   两市成交额 = 上证[7] + 深证[7]（万元 → 亿元）。
   ──────────────────────────────────────────────────────────────────────── */
function loadBand() {
  return cached('band', TTL.band, function () {
    return Promise.all([
      fetchGBK(EP.band),
      /* 完整格式才有报价时间戳 → 判断是否在交易时段。
         休市（周末/收盘后）腾讯返回的是**上次收盘快照**，价格永远不变；
         这时还标 LIVE 等于骗用户，必须标 CLOSED · last close。 */
      fetchGBK(EP.bandClock).catch(function () { return ''; })
    ]).then(function (pair) {
    var txt = pair[0], clockTxt = pair[1] || '';
    var map = {};
    String(txt).split('\n').forEach(function (line) {
      var mm = line.match(/v_(\w+)="([^"]*)"/);
      if (mm) map[mm[1]] = mm[2].split('~');
    });
    var items = [], amt = 0, hasAmt = false;
    EP.bandCodes.forEach(function (code) {
      var f = map[code];
      if (!f || !f.length) return;
      var name = f[1], price = num(f[3]), chg = num(f[5]), amount = num(f[7]);
      if (name === undefined || price === null) return;
      items.push({ code: code, name: name, price: price, chg: chg });
      if (code === 's_sh000001' || code === 's_sz399001') { if (amount !== null) { amt += amount; hasAmt = true; } }
    });
    /* 完整格式 [30] = 20261009161400（最近报价时间） */
    var quoteTs = null, quoteRaw = null;
    var cm = clockTxt.match(/v_sh000001="([^"]*)"/);
    if (cm) {
      var cf = cm[1].split('~');
      var raw = cf[30];
      if (raw && /^\d{14}$/.test(raw)) {
        quoteRaw = raw;
        quoteTs = new Date(+raw.slice(0, 4), +raw.slice(4, 6) - 1, +raw.slice(6, 8),
                           +raw.slice(8, 10), +raw.slice(10, 12), +raw.slice(12, 14)).getTime();
      }
    }
    /* 交易时段判定：报价在 5 分钟内更新过 → LIVE；否则 CLOSED。
       好处：不需要节假日日历，法定休市也会自然落到 CLOSED。 */
    var ageMs = (quoteTs === null) ? null : (Date.now() - quoteTs);
    var live = (ageMs !== null && ageMs >= 0 && ageMs <= 300000);
    return { items: items, amount: hasAmt ? amt / 10000 : null,
             live: live, quoteTs: quoteTs, quoteRaw: quoteRaw,
             quoteAgeMin: (ageMs === null) ? null : Math.round(ageMs / 60000) };
    });
  });
}
function renderBand(d) {
  var host = document.getElementById('bandItems');
  if (!host) return;
  if (!d || !d.items || !d.items.length) {
    host.textContent = '';
    var s = document.createElement('span');
    s.className = 'band-item band-dead';
    s.textContent = 'CN BAND OFFLINE';
    host.appendChild(s);
    var tagD = document.getElementById('bandState');
    if (tagD) { tagD.textContent = 'OFFLINE'; tagD.className = 'band-state band-state-bad'; }
    return;
  }
  var items = d.items.slice();
  if (d.amount !== null) items.push({ code: '__amt', name: '两市', price: null, chg: null, amount: d.amount });
  keyedList(host, items, function (it) { return it.code; }, 'tpl-band-item', function (el, it) {
    setText(q(el, '[data-b="name"]'), it.name);
    setText(q(el, '[data-b="price"]'), it.amount !== undefined && it.amount !== null ? (Math.round(it.amount) + '亿') : (it.price === null ? '--' : it.price.toFixed(2)),
      it.chg === null || it.chg === undefined ? null : (it.chg > 0 ? 'up' : (it.chg < 0 ? 'down' : null)));
    var c = q(el, '[data-b="chg"]');
    if (it.chg === null || it.chg === undefined) { setText(c, ''); el.setAttribute('data-dir', 'flat'); }
    else {
      setText(c, (it.chg > 0 ? '+' : '') + it.chg.toFixed(2) + '%', it.chg > 0 ? 'up' : (it.chg < 0 ? 'down' : null));
      el.setAttribute('data-dir', it.chg > 0 ? 'up' : (it.chg < 0 ? 'down' : 'flat'));
    }
  });
  var tag = document.getElementById('bandState');
  if (tag) {
    /* 休市必须标注：这条带子虽然"连得上"，但休市时价格是上次收盘快照、永远不变 */
    if (d.live) {
      tag.textContent = 'LIVE';
      tag.className = 'band-state band-state-ok';
    } else {
      tag.textContent = 'CLOSED \u00B7 LAST CLOSE';
      tag.className = 'band-state band-state-closed';
    }
    tag.setAttribute('data-live', d.live ? 'yes' : 'no');
    if (d.quoteAgeMin !== null) tag.setAttribute('title', 'last quote ' + d.quoteAgeMin + ' min ago');
  }
}

/* ══════════════════════ 顶栏 + 状态 ══════════════════════ */
function renderHeader() {
  var t = document.getElementById('topbar');
  var w = CACHE.wallets || [], lb = CACHE.leaderboard || [];
  var sum = 0, aw = 0;
  w.forEach(function (x) { if (x.amount !== null && x.amount !== undefined) { sum += x.amount; aw++; } });
  /* 胜率 = 有真实 24H 数据的榜单钱包中，24H 盈利为正的比例（不是「全为正」的假 100%）*/
  var with1d = lb.filter(function (x) { return x.has1d; }).length;
  var pos1d = lb.filter(function (x) { return x.pos1d; }).length;
  setText(q(t, '[data-h="bankroll"]'), aw ? fmtCompact(sum) : null, 'up');
  var ar = q(t, '[data-h="bankroll_arrow"]');
  if (ar) ar.textContent = aw ? '\u2191' : '';
  setText(q(t, '[data-h="pnl"]'), (CACHE.pnl && CACHE.pnl.total !== null) ? CACHE.pnl.total_text : null,
    CACHE.pnl ? CACHE.pnl.total_dir : null);
  var pt = q(t, '.tok-pnl');
  if (pt) pt.setAttribute('data-tone', (CACHE.pnl && CACHE.pnl.total < 0) ? 'neg' : 'pos');
  /* 顶栏 EDGES = 只报**可执行**的机会数（扣费后净值为正）。
     绝不用候选行数：那会让顶栏与面板里的 NOT VIABLE 自相矛盾。 */
  var nViable = scannerRows().filter(function (r) { return r.viable; }).length;
  setText(q(t, '[data-h="edges"]'), CACHE.scanner ? nViable : null);
  setText(q(t, '[data-h="win_rate_pct"]'), with1d ? ((pos1d / with1d * 100).toFixed(1) + '% (' + with1d + ')') : null);
  setText(q(t, '[data-h="wallets"]'), w.length || null);
  var chip = document.getElementById('sampleChip');
  if (chip && LAST_OK_TS) { chip.hidden = false; setText(q(chip, '.age'), 'SYNC ' + hhmmssNow(LAST_OK_TS)); }
}

function renderGlobalStatus() {
  var vals = ['scanner', 'wallets', 'markets', 'copy', 'pnl', 'leaderboard'].map(function (k) { return HEALTH[k]; });
  var offline = vals.filter(function (v) { return v === 'offline'; }).length;
  var polyAllDown = offline >= 4;
  var chip = document.getElementById('liveChip'), txt = q(chip, '[data-h="live"]');
  if (polyAllDown) {
    chip.setAttribute('data-state', 'offline');
    if (txt) txt.textContent = 'OFFLINE \u2014 PROXY DOWN';
  } else if (offline > 0) {
    chip.setAttribute('data-state', 'degraded');
    if (txt) txt.textContent = 'DEGRADED ' + offline + '/6';
  } else {
    chip.setAttribute('data-state', 'live');
    if (txt) txt.textContent = 'LIVE';
  }
  var banner = document.getElementById('banner');
  if (banner) {
    if (polyAllDown) {
      banner.hidden = false;
      banner.textContent = 'PROXY 127.0.0.1:7890 UNREACHABLE \u2014 POLYMARKET FEEDS DOWN'
        + ((CACHE.band && CACHE.band.items && CACHE.band.items.length) ? ' \u00B7 CN BAND STILL LIVE (DIRECT)' : '');
    } else banner.hidden = true;
  }
}

/* ══════════════════════ 主循环（按源分级取数）══════════════════════
   pollAll() 每 POLL_MS 跑一次，但每个源自带 TTL：
     positions 60s（max-age=5，唯一真实时）· keyset/trades 300s · lb 600s · CN 带 60s。
   所以"实时"是真的：钱包/跟单/PNL 60 秒一次新数据，而不是 300s 空转。 */
function pollAll() {
  var jobs = [
    loadMarkets().then(function (r) { CACHE.markets = r; setPanelState('markets', r.length ? 'ok' : 'empty'); })
      .catch(function () { setPanelState('markets', 'offline'); ensurePlaceholder('markets'); }),
    loadWallets().then(function (r) { CACHE.wallets = r; setPanelState('wallets', r.length ? 'ok' : 'empty'); })
      .catch(function () { setPanelState('wallets', 'offline'); ensurePlaceholder('wallets'); CACHE.wallets = CACHE.wallets || []; }),
    loadLeaderboard().then(function (r) { CACHE.leaderboard = r; setPanelState('leaderboard', r.length ? 'ok' : 'empty'); })
      .catch(function () { setPanelState('leaderboard', 'offline'); ensurePlaceholder('leaderboard'); }),
    loadScanner().then(function (r) { CACHE.scanner = r; renderScanner(r); })
      .catch(function () { CACHE.scanner = null; setPanelState('scanner', 'offline'); ensurePlaceholder('scanner'); }),
    /* CN 带：腾讯直连、不依赖代理 —— 代理挂掉时它必须继续刷新 */
    loadBand().then(function (d) { CACHE.band = d; renderBand(d); })
      .catch(function () { CACHE.band = null; renderBand(null); })
  ];
  Promise.all(jobs).then(function () {
    var w = CACHE.wallets || [];
    return Promise.all([
      loadCopy(w).then(function (r) { CACHE.copy = r.rows; CACHE.copyNote = r.note; renderCopy(r.rows, r.note); })
        .catch(function () { setPanelState('copy', 'offline'); ensurePlaceholder('copy'); }),
      loadPnl(w).then(function (r) { CACHE.pnl = r; renderPnl(r); })
        .catch(function () { setPanelState('pnl', 'offline'); })
    ]);
  }).then(function () {
    LAST_OK_TS = Date.now();
    paint();
  }).catch(function (e) { console.error('[pollAll]', e); paint(); });
}

function refreshBand() {
  /* 保留为手动刷新入口；常规节奏已并入 pollAll() 的按源 TTL（60s） */
  loadBand().then(function (d) { CACHE.band = d; renderBand(d); paint(); })
    .catch(function () { CACHE.band = null; renderBand(null); paint(); });
}

/* 面板内容被容器高度裁掉时，必须让"还有多少没看到"可见 ——
   否则会出现"标签说 10、眼睛看到 4"这种最容易被当成 bug 的形态。
   做法：保留面板内滚动（10 行都能翻到），并在脚注显式给出总行数与滚动提示。 */
function updateOverflowHints() {
  ['scanner', 'markets', 'wallets', 'copy', 'leaderboard'].forEach(function (n) {
    var p = document.querySelector('[data-panel="' + n + '"]');
    if (!p) return;
    var body = q(p, '.p-body'), foot = q(p, '.p-foot');
    if (!body || !foot) return;
    var hint = q(foot, '.p-overflow');
    if (!hint) {
      hint = document.createElement('span');
      hint.className = 'p-overflow';
      foot.insertBefore(hint, foot.firstChild);
    }
    var rows = qa(body, '[data-row]').length;
    if (!rows) { hint.textContent = ''; return; }
    var overflowing = body.scrollHeight > body.clientHeight + 4;
    hint.textContent = overflowing
      ? ('\u2195 ' + rows + ' ROWS \u00B7 SCROLL FOR ALL \u00B7 ')   // 明确告知总数 + 可滚动
      : (rows + ' ROWS \u00B7 ');
    hint.setAttribute('data-overflow', overflowing ? 'yes' : 'no');
  });
}

function paint() {
  renderHeader();
  if (CACHE.scanner) renderScanner(CACHE.scanner);
  if (CACHE.wallets && HEALTH.wallets !== 'offline') renderWallets(CACHE.wallets);
  if (CACHE.markets && HEALTH.markets !== 'offline') renderMarkets(CACHE.markets);
  if (CACHE.copy && HEALTH.copy !== 'offline') renderCopy(CACHE.copy, CACHE.copyNote);
  if (CACHE.pnl && HEALTH.pnl !== 'offline') renderPnl(CACHE.pnl);
  if (CACHE.leaderboard && HEALTH.leaderboard !== 'offline') renderLeaderboard(CACHE.leaderboard);
  var clk = q(document, '[data-p="clock"]');
  if (clk) clk.textContent = clockHHMM();
  updateOverflowHints();
  renderGlobalStatus();
  document.body.classList.remove('booting');
}

function boot() {
  var p = new URLSearchParams(location.search);
  /* ?refresh=<ms> 把所有源的 TTL 统一压到这个值 —— 仅用于本地快速验证（如跑多轮看闪烁）*/
  var rf = parseInt(p.get('refresh'), 10);
  if (isFinite(rf) && rf >= 2000) {
    ['markets', 'trades', 'positions', 'lb', 'lb1d', 'band'].forEach(function (k) { TTL[k] = rf; });
    POLL_MS = Math.max(1000, Math.min(10000, Math.floor(rf / 2)));
  }
  ['scanner', 'wallets', 'markets', 'copy', 'pnl', 'leaderboard'].forEach(function (k) { setPanelState(k, 'loading'); });
  paint();
  pollAll();
  setInterval(paint, TICK_MS);        // 1s 重绘（吃本地缓存，数字变色高亮）
  setInterval(pollAll, POLL_MS);      // 调度器：由各源 TTL 决定谁真取数
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
