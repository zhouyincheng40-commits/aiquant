/* ============================================================================
   feeds.js — POLY // TERMINAL 数据层（与渲染完全无关，无任何 DOM 依赖）
   ----------------------------------------------------------------------------
   单一入口：PolyFeeds.snapshot(opts) -> Promise<Snapshot>
   同步取缓存：PolyFeeds.getCached() -> Snapshot | null
   自动轮询：PolyFeeds.start({ onUpdate }) / PolyFeeds.stop()

   本模块只做「取数 + 口径 + 归一化」，不含任何渲染。
   HTML 版 / 终端版 / Node 脚本都可直接用同一份数据层，口径只此一处。

   ── 环境要求 ──
   polymarket 三域名（gamma/lb-api/data-api）**依赖本机代理 127.0.0.1:7890**：
     · 浏览器：吃系统代理（WinINET ProxyEnable=1），可直接 fetch
     · Node：需设 HTTPS_PROXY=http://127.0.0.1:7890（Node 不读系统代理）
   CN 市场带（qt.gtimg.cn）直连可达，不依赖代理。

   ── 口径（关键，改动前先读）──
   1. MISPRICING SCANNER =「定义性套利」，且**只对 negRisk=true 的互斥穷尽集合**计算。
      非互斥事件（如 "ceasefire through <日期A/B/C>"）Σ 价格天然 >> 1，不是套利。
      实测：真互斥集合 Σ bestBid ≈ 0.99–1.02；非互斥事件 Σ bestBid = 3.91。
      不加这道门槛会产出 +286% 这种**假套利**。
   2. gamma /markets/keyset 的**分页游标无效**（cursor/next_cursor/after/page 全返同一页、
      offset 返 0 条）→ 扫描范围恒为「volume24hr 降序第一页」，meta.scanRange 自曝。
   3. outcomes / outcomePrices 是 JSON **字符串**，必须二次 parse。
   4. oneDayPriceChange 仅约 23/40 市场存在 → 降级 oneHourPriceChange → null（不补假值）。
   5. 编码：Polymarket 是合法 UTF-8（**不做任何转换**）；腾讯是 GBK（**必须** TextDecoder('gbk')）。
   ========================================================================== */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PolyFeeds = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONFIG = {
    refreshMs: 300000,        // Polymarket 取数间隔（gamma max-age=300）
    bandRefreshMs: 60000,     // CN 带取数间隔（腾讯直连）
    reqTimeoutMs: 25000,
    gammaLimit: 60,
    trackedN: 7,              // WALLET TRACKER 条数
    copyWallets: 5,           // COPYTRADE / PNL 追踪的钱包数
    scanEventMax: 14,         // 每轮最多展开的 negRisk 事件数
    scanLimit: 12,            // SCANNER 最多返回条数
    sampleMax: 120,           // PNL 会话采样点上限
    edgeFeeds: ['edges.json', 'data/poly/edges.json']   // 可选热插 feed（不存在则用内置算法）
  };

  var G = 'https://gamma-api.polymarket.com';
  var EP = {
    gamma: function () { return G + '/markets/keyset?closed=false&limit=' + CONFIG.gammaLimit + '&order=volume24hr&ascending=false'; },
    event: function (id) { return G + '/events/' + id; },
    profitAll: 'https://lb-api.polymarket.com/profit?window=all&limit=20',
    profit1d: 'https://lb-api.polymarket.com/profit?window=1d&limit=20',
    trades: 'https://data-api.polymarket.com/trades?limit=40',
    tradesUser: function (a, n) { return 'https://data-api.polymarket.com/trades?user=' + a + '&limit=' + (n || 10); },
    positions: function (a, n) { return 'https://data-api.polymarket.com/positions?user=' + a + '&limit=' + (n || 20); },
    band: 'https://qt.gtimg.cn/q=s_sh000001,s_sz399001,s_sz399006,s_bj899050,s_hkHSI'
  };
  var BAND_CODES = ['s_sh000001', 's_sz399001', 's_sz399006', 's_bj899050', 's_hkHSI'];

  var STATE = {
    snapshot: null,       // 最近一次完整快照
    pnlSamples: [],
    band: null,
    inflight: null
  };

  /* ── 基础工具（纯函数） ─────────────────────────────────────────────── */
  function num(v) {
    if (v === null || v === undefined || v === '') return null;
    var n = typeof v === 'number' ? v : parseFloat(v);
    return isFinite(n) ? n : null;
  }
  function jarr(v) {
    if (Array.isArray(v)) return v;
    if (typeof v === 'string') { try { var p = JSON.parse(v); return Array.isArray(p) ? p : []; } catch (e) { return []; } }
    return [];
  }
  function compact(n) {
    if (n === null) return '--';
    var sign = n < 0 ? '-' : '', a = Math.abs(n), s;
    if (a >= 1e9) s = (a / 1e9).toFixed(1) + 'B';
    else if (a >= 1e6) s = (a / 1e6).toFixed(1) + 'M';
    else if (a >= 1e3) s = (a / 1e3).toFixed(1) + 'k';
    else s = a.toFixed(a < 10 ? 2 : 0);
    return sign + '$' + s;
  }
  function money(n, dp) {
    if (n === null) return '--';
    var sign = n < 0 ? '-' : '', a = Math.abs(n);
    var d = dp === undefined ? (a < 100 && a % 1 !== 0 ? 2 : 0) : dp;
    return sign + '$' + a.toFixed(d).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function cents(p) { var n = num(p); return n === null ? '--' : (Math.round(n * 1000) / 10) + '\u00A2'; }
  function signed(v, fn) { return v === null ? '--' : (v > 0 ? '+' : '') + fn(Math.abs(v)); }
  function shortAddr(a) { return (!a || typeof a !== 'string') ? '--' : (a.length > 12 ? a.slice(0, 6) + '...' + a.slice(-4) : a); }
  function hhmmss(sec) {
    var n = num(sec); if (n === null) return '--';
    var d = new Date(n * 1000);
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2);
  }
  function tickFloor(v, tick) { return (!tick || tick <= 0) ? v : Math.floor(v / tick + 1e-9) * tick; }

  /* ── HTTP（不依赖 DOM） ─────────────────────────────────────────────── */
  function reqJSON(url) {
    var ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, CONFIG.reqTimeoutMs);
    var sep = url.indexOf('?') >= 0 ? '&' : '?';
    return fetch(url + sep + '_t=' + Date.now(), { cache: 'no-store', signal: ctl ? ctl.signal : undefined })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) { clearTimeout(timer); return j; },
            function (e) { clearTimeout(timer); throw e; });
  }
  /* 腾讯 GBK：必须显式 gbk 解码，默认 UTF-8 会把中文指数名变乱码 */
  function reqGBK(url) {
    var ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, CONFIG.reqTimeoutMs);
    var sep = url.indexOf('?') >= 0 ? '&' : '?';
    return fetch(url + sep + '_t=' + Date.now(), { cache: 'no-store', signal: ctl ? ctl.signal : undefined })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.arrayBuffer(); })
      .then(function (buf) {
        clearTimeout(timer);
        var dec, note = null;
        try { dec = new TextDecoder('gbk'); }
        catch (e) { dec = new TextDecoder('utf-8'); note = 'GBK DECODER UNAVAILABLE'; }
        return { text: dec.decode(buf), note: note };
      }, function (e) { clearTimeout(timer); throw e; });
  }
  function settle(p) { return p.then(function (v) { return { ok: true, value: v }; },
                                     function (e) { return { ok: false, error: String(e && e.message || e) }; }); }

  /* ══════════════════ SCANNER：定义性套利（negRisk 门槛） ══════════════════ */
  function scanFromFeed(url) {
    return reqJSON(url).then(function (j) {
      var arr = Array.isArray(j) ? j : (j && Array.isArray(j.edges) ? j.edges : null);
      if (!arr || !arr.length) return null;
      return {
        source: url, rows: arr.map(function (e, idx) {
          var pct = num(e.edge_pct);
          if (pct === null && num(e.edge) !== null) pct = num(e.edge) * 100;
          var px = num(e.price), fair = num(e.fair);
          return {
            id: e.id || e.conditionId || ('feed' + idx),
            market: e.market || e.question || '--',
            sumBid: num(e.sum_bid) !== null ? num(e.sum_bid) : null,
            sumAsk: num(e.sum_ask) !== null ? num(e.sum_ask) : null,
            sumSpread: null, legs: num(e.legs), feeRate: num(e.fee_rate), tick: num(e.tick_size),
            grossPct: num(e.gross_pct), netPct: pct, viable: pct !== null && pct > 0,
            side: (e.side || '').toUpperCase(),
            yesText: cents(px), noText: cents(px === null ? null : 1 - px),
            priceText: cents(px),
            fairText: fair === null ? '--' : 'FAIR ' + cents(fair),
            vol24Text: compact(num(e.volume24hr)),
            volumeText: compact(num(e.volume)),
            edgeText: pct === null ? '--' : (pct > 0 ? '+' : '') + pct.toFixed(4) + '%',
            badgeTone: pct === null ? 'flat' : (Math.abs(pct) >= 8 ? 'strong' : (pct > 0 ? 'up' : 'down'))
          };
        })
      };
    });
  }

  function scanComputed() {
    return reqJSON(EP.gamma()).then(function (j) {
      var ms = (j && j.markets) || [];
      var byEv = {};
      ms.forEach(function (m) {
        var ev = (Array.isArray(m.events) && m.events[0] && m.events[0].id) ? String(m.events[0].id) : null;
        if (!ev) return;
        if (!byEv[ev]) byEv[ev] = { id: ev, markets: [] };
        byEv[ev].markets.push(m);
      });
      var groups = Object.keys(byEv).map(function (k) { return byEv[k]; })
        .filter(function (g) { return g.markets.length >= 2; })
        .sort(function (a, b) { return b.markets.length - a.markets.length; })
        .slice(0, CONFIG.scanEventMax);

      var meta = { scanRange: ms.length, scanEventsCandidates: groups.length, scanNonExclusive: 0, scanRejected: 0 };

      return Promise.all(groups.map(function (g) {
        /* 用 /events/<id> 取该 event 的**全量**候选结果（第一页往往只含一部分） */
        return settle(reqJSON(EP.event(g.id)).then(function (ev) {
          return {
            id: g.id,
            title: ev.title || (g.markets[0].events[0] && g.markets[0].events[0].title) || '--',
            vol24: num(ev.volume24hr),
            negRisk: (ev.negRisk === true || ev.enableNegRisk === true),
            markets: (ev.markets || []).filter(function (x) { return x.closed !== true; })
          };
        }));
      })).then(function (settled) {
        var rows = [];
        settled.forEach(function (s) {
          if (!s.ok || !s.value) return;
          var g = s.value;
          if (!g.markets || g.markets.length < 2) return;
          if (!g.negRisk) { meta.scanNonExclusive++; return; }   // ← 核心门槛

          var usable = [], bad = false, feeRate = 0;
          g.markets.forEach(function (m) {
            var outs = jarr(m.outcomes);
            var ask = num(m.bestAsk), bid = num(m.bestBid);
            if (outs.length !== 2 || ask === null || bid === null) { bad = true; return; }  // 缺任一腿 → 整条剔除
            var fr = (m.feeSchedule && num(m.feeSchedule.rate)) || 0;
            if (fr > feeRate) feeRate = fr;
            usable.push({ ask: ask, bid: bid, spread: num(m.spread) || 0, tick: num(m.orderPriceMinTickSize) || 0.01 });
          });
          if (bad || usable.length < 2) { meta.scanRejected++; return; }

          var cost = 0, proceeds = 0, spreadSum = 0, tick = 0.01;
          usable.forEach(function (u) { cost += u.ask; proceeds += u.bid; spreadSum += u.spread; tick = u.tick; });
          if (cost <= 0) { meta.scanRejected++; return; }

          var grossBuy = 1 - cost, grossSell = proceeds - 1;
          if (grossBuy <= 0 && grossSell <= 0) return;           // 毛额无空间 → 不产出
          /* 选边按毛额（先指出错价在哪一侧）；可行性按扣费后净值判定。
             Σspread 只随行展示、不折进百分比：36 腿集合 Σspread 达 606.6¢，
             折进去会得到 -174%/-626% 这种无信息量数字，而判定结论不变。 */
          var useBuy = grossBuy >= grossSell;
          var grossPct = useBuy ? (grossBuy / cost) * 100 : grossSell * 100;
          var netPct = useBuy
            ? (tickFloor(1 - cost * (1 + feeRate), tick) / cost) * 100
            : tickFloor(proceeds * (1 - feeRate) - 1, tick) * 100;
          var viable = netPct > 0;

          rows.push({
            id: g.id, market: g.title, legs: usable.length,
            sumAsk: +cost.toFixed(6), sumBid: +proceeds.toFixed(6), sumSpread: +spreadSum.toFixed(6),
            feeRate: feeRate, tick: tick,
            grossPct: +grossPct.toFixed(4), netPct: +netPct.toFixed(4), viable: viable,
            side: useBuy ? 'BUY ALL' : 'SELL ALL',
            groupVolume24hr: g.vol24,
            yesText: 'ΣBID ' + cents(proceeds), noText: 'ΣASK ' + cents(cost),
            priceNum: useBuy ? cost : proceeds, priceText: cents(useBuy ? cost : proceeds),
            fairText: 'GROSS ' + (grossPct > 0 ? '+' : '') + grossPct.toFixed(2) + '%',
            vol24Text: compact(g.vol24), volumeText: compact(g.vol24),
            edgeText: (netPct > 0 ? '+' : '') + netPct.toFixed(2) + '%',
            sideText: (useBuy ? 'BUY ALL' : 'SELL ALL') + ' \u00B7 ' + usable.length + ' OUT \u00B7 \u03A3SPR '
              + cents(spreadSum) + (viable ? '' : ' \u00B7 NOT VIABLE'),
            badgeTone: viable ? 'up' : 'strong',
            _sort: Math.abs(grossPct)
          });
        });
        rows.sort(function (a, b) { return b._sort - a._sort; });
        rows.forEach(function (r) { delete r._sort; });
        return { source: 'computed', rows: rows.slice(0, CONFIG.scanLimit), meta: meta };
      });
    });
  }

  function loadScanner(opts) {
    var feeds = (opts && opts.edgesUrl) ? [opts.edgesUrl] : CONFIG.edgeFeeds.slice();
    function tryFeed(i) {
      if (i >= feeds.length) return scanComputed();
      return scanFromFeed(feeds[i]).then(function (r) { return r || tryFeed(i + 1); });
    }
    return tryFeed(0);
  }

  /* ══════════════════ 市场 / 钱包 / 榜单 / 成交流 / 持仓 ══════════════════ */
  function loadMarkets() {
    return reqJSON(EP.gamma()).then(function (j) {
      var ms = (j && j.markets) || [];
      return ms.filter(function (m) { return !!m.question; }).map(function (m) {
        var prices = jarr(m.outcomePrices).map(num), outs = jarr(m.outcomes);
        var yes = prices.length ? prices[0] : null, no = prices.length > 1 ? prices[1] : null;
        var chg = num(m.oneDayPriceChange), basis = '24H';          // 降级链：day → hour → null
        if (chg === null) { chg = num(m.oneHourPriceChange); basis = '1H'; }
        var pct = chg === null ? null : chg * 100;
        var dir = pct === null ? 'flat' : (pct > 0 ? 'up' : (pct < 0 ? 'down' : 'flat'));
        return {
          id: m.id, question: m.question, slug: m.slug, conditionId: m.conditionId,
          outcomes: outs, yesPrice: yes, noPrice: no,
          volume24hr: num(m.volume24hr), volume: num(m.volume), liquidity: num(m.liquidity),
          chgPct: pct, chgBasis: basis, chgDir: dir, spread: num(m.spread),
          bestBid: num(m.bestBid), bestAsk: num(m.bestAsk), lastTradePrice: num(m.lastTradePrice),
          meta1: 'VOL24 ' + compact(num(m.volume24hr)),
          meta2: 'LIQ ' + compact(num(m.liquidity)),
          yesText: cents(yes), noText: cents(no),
          chgText: pct === null ? '--' : ((dir === 'up' ? '\u25B2' : dir === 'down' ? '\u25BC' : '') + Math.abs(pct).toFixed(1) + '% ' + basis)
        };
      });
    });
  }

  function loadLeaderboardData() {
    return Promise.all([settle(reqJSON(EP.profitAll)), settle(reqJSON(EP.profit1d))]).then(function (r) {
      if (!r[0].ok) throw new Error(r[0].error);
      var all = Array.isArray(r[0].value) ? r[0].value : [];
      var d1 = (r[1].ok && Array.isArray(r[1].value)) ? r[1].value : [];
      var m1 = {};
      d1.forEach(function (x) { if (x && x.proxyWallet) m1[String(x.proxyWallet).toLowerCase()] = num(x.amount); });
      return all.map(function (w, i) {
        var addr = String(w.proxyWallet || ''), amt = num(w.amount);
        var p1 = (addr.toLowerCase() in m1) ? m1[addr.toLowerCase()] : null;
        return {
          rank: i + 1, address: addr, label: w.pseudonym || shortAddr(addr),
          pseudonym: w.pseudonym || null, amount: amt, amount24h: p1,
          has1d: p1 !== null, pos1d: (p1 !== null && p1 > 0),
          text: signed(amt, compact), dir: amt === null ? null : (amt >= 0 ? 'up' : 'down'),
          rightText: p1 === null ? '--' : signed(p1, function (v) { return compact(v) + ' 24H'; })
        };
      });
    });
  }

  function loadWallets(lb) {
    return lb.slice(0, CONFIG.trackedN).map(function (w, i) {
      /* WHALE/SCOUT 是**本模块基于真实排名的派生规则**，不是接口字段 */
      var kind = i < 3 ? 'WHALE' : 'SCOUT';
      return {
        address: w.address, addressShort: shortAddr(w.address), kind: kind,
        kindClass: kind === 'WHALE' ? 'k-whale' : 'k-top',
        amount: w.amount, amount24h: w.amount24h, rank: i + 1, pseudonym: w.pseudonym,
        pnlText: compact(w.amount), pnlDir: w.amount === null ? null : (w.amount >= 0 ? 'up' : 'down'),
        subText: w.amount24h === null ? (w.pseudonym || '--')
          : signed(w.amount24h, function (v) { return compact(v) + ' 24H'; })
      };
    });
  }

  function loadCopy(wallets) {
    var tops = wallets.slice(0, CONFIG.copyWallets).map(function (w) { return w.address; });
    return Promise.all(tops.map(function (a) {
      return settle(reqJSON(EP.tradesUser(a, 10)));
    })).then(function (settled) {
      var okLists = settled.filter(function (s) { return s.ok && Array.isArray(s.value); });
      if (okLists.length === 0) throw new Error('trades api red');
      var any = okLists.some(function (s) { return s.value.length > 0; });
      if (!any) {
        /* 追踪钱包无成交流水 → 回落全站最新流水，并在 source 里注明（不冒充 copy 信号）*/
        return reqJSON(EP.trades).then(function (j) {
          return normalizeTrades(Array.isArray(j) ? j : [], 8, 'GLOBAL_FEED');
        });
      }
      var merged = [];
      okLists.forEach(function (s) { merged = merged.concat(s.value); });
      return normalizeTrades(merged, 8, 'TRACKED_WALLETS');
    });
  }

  function normalizeTrades(list, limit, source) {
    var sorted = list.slice().sort(function (a, b) { return (num(b.timestamp) || 0) - (num(a.timestamp) || 0); });
    return {
      source: source,
      rows: sorted.slice(0, limit).map(function (t) {
        var side = String(t.side || '').toUpperCase();
        var notional = (num(t.size) || 0) * (num(t.price) || 0);
        var title = String(t.title || '');
        var shortTitle = title.length > 26 ? title.slice(0, 25) + '\u2026' : title;
        return {
          key: (t.transactionHash || '') + '|' + (t.asset || '') + '|' + (t.timestamp || ''),
          address: String(t.proxyWallet || ''), addressShort: shortAddr(t.proxyWallet),
          side: side, outcome: t.outcome || null, title: title, slug: t.slug || null,
          size: num(t.size), price: num(t.price), notional: notional, timestamp: num(t.timestamp),
          actionText: (shortTitle ? shortTitle + '  ' : '') + (side || '--') + ' ' + String(t.outcome || '').toUpperCase(),
          sizeText: money(notional, 0) + '  ' + hhmmss(t.timestamp)
        };
      })
    };
  }

  function loadPnl(wallets) {
    var tops = wallets.slice(0, CONFIG.copyWallets).map(function (w) { return w.address; });
    return Promise.all(tops.map(function (a) { return settle(reqJSON(EP.positions(a, 20))); }))
      .then(function (settled) {
        var okLists = settled.filter(function (s) { return s.ok && Array.isArray(s.value); });
        if (okLists.length === 0) return { ok: false, offline: true, reason: 'positions api red' };
        var pos = [];
        okLists.forEach(function (s) { pos = pos.concat(s.value); });
        if (!pos.length) return { ok: false, offline: false, reason: 'no open positions' };
        var total = 0, n = 0;
        pos.forEach(function (p) { var v = num(p.cashPnl); if (v !== null) { total += v; n++; } });
        if (!n) return { ok: false, offline: false, reason: 'cashPnl missing' };
        var prev = STATE.pnlSamples.length ? STATE.pnlSamples[STATE.pnlSamples.length - 1] : null;
        var delta = prev === null ? null : total - prev;
        /* 只在观测值真的变化时记点（渲染循环 1s，每 tick 都记会让序列变成秒级重复值）*/
        if (!STATE.pnlSamples.length || STATE.pnlSamples[STATE.pnlSamples.length - 1] !== total) {
          STATE.pnlSamples.push(total);
          if (STATE.pnlSamples.length > CONFIG.sampleMax) STATE.pnlSamples.shift();
        }
        return {
          ok: true, offline: false, total: total, delta: delta,
          positions: n, wallets: tops.length, samples: STATE.pnlSamples.slice(),
          text: signed(total, function (v) { return money(v, 0); }),
          dir: delta === null ? (total >= 0 ? 'up' : 'down') : (delta > 0 ? 'up' : (delta < 0 ? 'down' : 'flat')),
          deltaText: delta === null ? '--' : signed(delta, function (v) { return money(v, 0); }),
          deltaDir: delta === null ? null : (delta > 0 ? 'up' : (delta < 0 ? 'down' : 'flat')),
          meta: n + ' POS \u00B7 ' + tops.length + ' WALLETS'
        };
      });
  }

  /* ══════════════════ CN 市场带（直连，独立于代理） ══════════════════ */
  function loadBand() {
    return reqGBK(EP.band).then(function (r) {
      var map = {};
      String(r.text).split('\n').forEach(function (line) {
        var mm = line.match(/v_(\w+)="([^"]*)"/);
        if (mm) map[mm[1]] = mm[2].split('~');
      });
      var items = [], amt = 0, hasAmt = false;
      BAND_CODES.forEach(function (code) {
        var f = map[code];
        if (!f || !f.length) return;
        var name = f[1], price = num(f[3]), chg = num(f[5]), amountWan = num(f[7]);
        if (name === undefined || price === null) return;      /* 字段位置：见模块头注释 */
        items.push({
          code: code, name: name, price: price, chgPct: chg,
          dir: chg === null ? 'flat' : (chg > 0 ? 'up' : (chg < 0 ? 'down' : 'flat')),
          priceText: price.toFixed(2),
          chgText: chg === null ? '' : (chg > 0 ? '+' : '') + chg.toFixed(2) + '%'
        });
        if (code === 's_sh000001' || code === 's_sz399001') { if (amountWan !== null) { amt += amountWan; hasAmt = true; } }
      });
      if (!items.length) return { ok: false, reason: 'no parsable band rows', items: [] };
      return { ok: true, direct: true, decoderNote: r.note, items: items,
               amountYi: hasAmt ? amt / 10000 : null };      /* 万元 → 亿元 */
    });
  }

  /* ══════════════════ snapshot：唯一入口 ══════════════════ */
  function refresh(opts) {
    opts = opts || {};
    var errors = [];
    var out = {
      ts: Date.now(), ok: false, proxyDown: false, errors: errors,
      header: null, scanner: [], wallets: [], markets: [], copy: [], pnl: null,
      leaderboard: [], cnBand: null, meta: {}, health: {}
    };

    function mark(panel, state) { out.health[panel] = state; }

    /* ── Polymarket 三条并行主线 ── */
    var jMarkets = settle(loadMarkets());
    var jLb = settle(loadLeaderboardData());
    var jScan = settle(loadScanner(opts));

    var bandP = settle(loadBand());

    return Promise.all([jMarkets, jLb, jScan]).then(function (r) {
      var mk = r[0], lb = r[1], sc = r[2];

      if (mk.ok) { out.markets = mk.value; mark('markets', mk.value.length ? 'ok' : 'empty'); }
      else { errors.push('markets: ' + mk.error); mark('markets', 'offline'); }

      if (lb.ok) {
        out.leaderboard = lb.value;
        out.wallets = loadWallets(lb.value);
        mark('leaderboard', lb.value.length ? 'ok' : 'empty');
        mark('wallets', out.wallets.length ? 'ok' : 'empty');
      } else {
        errors.push('leaderboard: ' + lb.error);
        mark('leaderboard', 'offline'); mark('wallets', 'offline');
      }

      if (sc.ok && sc.value) {
        out.scanner = sc.value.rows;
        out.meta.scannerSource = sc.value.source;
        if (sc.value.meta) {
          out.meta.scanRange = sc.value.meta.scanRange;
          out.meta.scanEvents = sc.value.meta.scanEventsCandidates - sc.value.meta.scanNonExclusive;
          out.meta.scanNonExclusive = sc.value.meta.scanNonExclusive;
          out.meta.scanRejected = sc.value.meta.scanRejected;
        }
        mark('scanner', out.scanner.length ? 'ok' : 'empty');
      } else {
        errors.push('scanner: ' + (sc.error || 'no feed'));
        mark('scanner', 'offline');
      }

      /* ── 依赖 wallets 的第二层 ── */
      var jCopy = out.wallets.length ? settle(loadCopy(out.wallets)) : Promise.resolve({ ok: false, error: 'no wallets' });
      var jPnl = out.wallets.length ? settle(loadPnl(out.wallets)) : Promise.resolve({ ok: false, error: 'no wallets' });

      return Promise.all([jCopy, jPnl]).then(function (r2) {
        var cp = r2[0], pn = r2[1];
        if (cp.ok) { out.copy = cp.value.rows; out.meta.copySource = cp.value.source; mark('copy', out.copy.length ? 'ok' : 'empty'); }
        else { errors.push('copy: ' + cp.error); mark('copy', 'offline'); }

        if (pn.ok) { out.pnl = pn.value; mark('pnl', pn.value.ok ? 'ok' : (pn.value.offline ? 'offline' : 'nodata')); }
        else {
          out.pnl = { ok: false, offline: true, reason: 'positions api red', samples: [] };
          mark('pnl', 'offline'); errors.push('pnl: ' + pn.error);
        }

        return bandP;
      });
    }).then(function (bandRes) {
      if (bandRes && bandRes.ok) { out.cnBand = bandRes.value; STATE.band = bandRes.value; mark('cnBand', bandRes.value.ok ? 'ok' : 'empty'); }
      else if (STATE.band) { out.cnBand = STATE.band; mark('cnBand', 'stale'); }
      else { out.cnBand = null; mark('cnBand', 'offline'); }

      /* ── header 汇总（全部来自真实字段）── */
      var sum = 0, aw = 0;
      out.wallets.forEach(function (x) { if (x.amount !== null) { sum += x.amount; aw++; } });
      var with1d = out.leaderboard.filter(function (x) { return x.has1d; }).length;
      var pos1d = out.leaderboard.filter(function (x) { return x.pos1d; }).length;
      out.header = {
        trackedPnlSum: aw ? sum : null, trackedCount: aw,
        cashPnl: (out.pnl && out.pnl.ok) ? out.pnl.total : null,
        cashPnlDir: (out.pnl && out.pnl.ok) ? out.pnl.dir : null,
        edges: out.scanner.length,
        win24hPct: with1d ? (pos1d / with1d * 100) : null,
        wallets: out.wallets.length,
        lastSyncTs: Date.now(),
        text: {
          trackedPnl: aw ? compact(sum) : '--',
          cashPnl: (out.pnl && out.pnl.ok) ? out.pnl.text : '--',
          edges: String(out.scanner.length),
          win24h: with1d ? (pos1d / with1d * 100).toFixed(1) + '%' : '--',
          wallets: String(out.wallets.length),
          sync: hhmmss(Date.now() / 1000)
        }
      };

      /* ── 代理不可达判定：polymarket 面板大量 offline 而 CN 带仍活 ── */
      var polyPanels = ['markets', 'wallets', 'leaderboard', 'scanner', 'copy', 'pnl'];
      var polyOffline = polyPanels.filter(function (k) { return out.health[k] === 'offline'; }).length;
      out.proxyDown = polyOffline >= 4;
      out.ok = polyOffline < 6 || (bandRes && bandRes.ok);
      out.meta.polyOffline = polyOffline;
      out.meta.cnBandLive = !!(bandRes && bandRes.ok);
      return out;
    });
  }

  /* opts.force=true 跳过 TTL；否则 refreshMs 内直接返回缓存 */
  var _snapPromise = null;
  function snapshot(opts) {
    opts = opts || {};
    var cached = STATE.snapshot;
    if (!opts.force && cached && (Date.now() - cached.ts) < (opts.refreshMs || CONFIG.refreshMs)) {
      return Promise.resolve(cached);
    }
    if (_snapPromise) return _snapPromise;             // 去重并发调用
    _snapPromise = refresh(opts).then(function (s) {
      STATE.snapshot = s; _snapPromise = null; return s;
    }, function (e) {
      _snapPromise = null;
      var s = STATE.snapshot || {
        ts: Date.now(), ok: false, proxyDown: true, errors: [String(e)], header: null,
        scanner: [], wallets: [], markets: [], copy: [], pnl: null, leaderboard: [], cnBand: null,
        meta: {}, health: {}
      };
      s.errors = (s.errors || []).concat([String(e)]);
      return s;
    });
    return _snapPromise;
  }

  function getCached() { return STATE.snapshot; }

  /* 独立刷新 CN 带（它不依赖代理，可用更高频） */
  function refreshBandOnly() {
    return loadBand().then(function (b) {
      if (b && b.ok) {
        STATE.band = b;
        if (STATE.snapshot) {
          STATE.snapshot.cnBand = b;
          STATE.snapshot.health = STATE.snapshot.health || {};
          STATE.snapshot.health.cnBand = 'ok';
        }
      }
      return b;
    });
  }

  /* 自动轮询：onUpdate(snapshot) 每次取数后被调用（失败也会被调用，snapshot.ok=false） */
  function start(opts) {
    opts = opts || {};
    var refreshMs = opts.refreshMs || CONFIG.refreshMs;
    var bandMs = opts.bandRefreshMs || CONFIG.bandRefreshMs;
    var handle = { timers: [], running: true };
    function tick() {
      snapshot({ force: true, edgesUrl: opts.edgesUrl }).then(function (s) {
        if (typeof opts.onUpdate === 'function') opts.onUpdate(s);
      });
    }
    function bandTick() {
      refreshBandOnly().then(function (b) {
        if (typeof opts.onBand === 'function') opts.onBand(b);
        else if (typeof opts.onUpdate === 'function') opts.onUpdate(getCached() || { cnBand: b });
      });
    }
    tick();
    handle.timers.push(setInterval(tick, refreshMs));
    handle.timers.push(setInterval(bandTick, bandMs));
    handle.stop = function () { handle.timers.forEach(clearInterval); handle.running = false; };
    return handle;
  }
  function stop(handle) { if (handle && handle.stop) handle.stop(); }

  return {
    CONFIG: CONFIG, EP: EP, BAND_CODES: BAND_CODES,
    snapshot: snapshot, getCached: getCached, refreshBandOnly: refreshBandOnly,
    start: start, stop: stop,
    /* 供测试与复用 */
    _util: { num: num, jarr: jarr, compact: compact, money: money, cents: cents, signed: signed,
             shortAddr: shortAddr, hhmmss: hhmmss, tickFloor: tickFloor, reqJSON: reqJSON, reqGBK: reqGBK }
  };
});
