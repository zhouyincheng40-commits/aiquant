/**
 * AI量化行情引擎（host 平面）
 *
 * 职责：把真实行情拉进事实库，并据此产出**可复现的技术信号**。
 *   · 实时快照：A股/ETF/指数/基金（腾讯 qt.gtimg.cn）、港股/美股（同源）、加密（Binance→OKX 兜底）
 *   · 日线：腾讯 ifzq（前复权），Sina 兜底；命中缓存不重复请求
 *   · 指标与信号：MA / EMA / MACD / RSI / BOLL / ATR / KDJ + 金叉死叉 / 超买超卖 / 突破 / 放量 / 回撤预警
 *
 * 发布服务 `ctx.aiquantFeed`；看板（ui.js）与智能体工具（tools.js）共用同一份实现。
 *
 * 工程约束：
 *   · 网络失败**绝不抛出**到轮询循环外：记 run 日志 + 标记 stale，界面显示"缓存"而不是空白。
 *   · 中文站点返回 GBK：用 TextDecoder('gbk')，不可用时退回 latin1（数字仍正确，名称可能乱码）。
 *   · 交易时段外自动降频（A股/港股），加密 7×24。
 *   · 只依赖 node: 内置模块 + 全局 fetch。
 */
import { normalizeSymbol, SIGNAL_KIND_LABEL, nowIso, marketSession } from './store-core.js'
import { fetchText as netFetchText, fetchJson as netFetchJson } from './net.js'

/** Cordis 插件名。 */
export const name = 'aiquant-feed'

/** 硬依赖事实库。 */
export const inject = ['aiquant']

/** 单次 HTTP 超时。 */
const HTTP_TIMEOUT_MS = 12000

/** 腾讯行情一次最多查多少个标的（URL 长度限制）。 */
const QUOTE_BATCH = 40

/* ────────────────────────────── 工具函数 ────────────────────────────── */

/** 数值化（非法→null）。 */
function n(value) {
  const x = Number(value)
  return Number.isFinite(x) ? x : null
}

/** 带超时与代理兜底的文本抓取。 */
async function fetchText(url, options = {}) {
  const result = await netFetchText(url, {
    charset: options.charset,
    headers: options.headers,
    timeoutMs: options.timeoutMs || HTTP_TIMEOUT_MS,
  })
  return result.text
}

/** 带超时与代理兜底的 JSON 抓取。 */
async function fetchJson(url, options = {}) {
  const result = await netFetchJson(url, {
    charset: options.charset,
    headers: options.headers,
    timeoutMs: options.timeoutMs || HTTP_TIMEOUT_MS,
  })
  return result.data
}

/** 腾讯实时行情查询代码（内码 → 接口代码）。 */
function tencentCode(norm) {
  if (norm.market === 'a') return norm.symbol
  if (norm.market === 'hk') return 'hk' + norm.code
  if (norm.market === 'us') return 'us' + norm.code
  return null
}

/** 涨跌色/方向使用的展示格式化在客户端做，这里只回原始数。 */

/* ────────────────────────────── 指标引擎 ────────────────────────────── */

/** 简单移动平均（末尾对齐，前面补 null）。 */
export function sma(values, period) {
  const out = new Array(values.length).fill(null)
  let sum = 0
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    if (i >= period - 1) out[i] = sum / period
  }
  return out
}

/** 指数移动平均。 */
export function ema(values, period) {
  const out = new Array(values.length).fill(null)
  const k = 2 / (period + 1)
  let prev = null
  for (let i = 0; i < values.length; i += 1) {
    prev = prev === null ? values[i] : values[i] * k + prev * (1 - k)
    out[i] = prev
  }
  return out
}

/** RSI（Wilder 平滑）。 */
export function rsi(values, period = 14) {
  const out = new Array(values.length).fill(null)
  if (values.length <= period) return out
  let gain = 0
  let loss = 0
  for (let i = 1; i <= period; i += 1) {
    const diff = values[i] - values[i - 1]
    if (diff >= 0) gain += diff; else loss -= diff
  }
  let avgGain = gain / period
  let avgLoss = loss / period
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss)
  for (let i = period + 1; i < values.length; i += 1) {
    const diff = values[i] - values[i - 1]
    const g = diff > 0 ? diff : 0
    const l = diff < 0 ? -diff : 0
    avgGain = (avgGain * (period - 1) + g) / period
    avgLoss = (avgLoss * (period - 1) + l) / period
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss)
  }
  return out
}

/** MACD（12/26/9）。 */
export function macd(values, fast = 12, slow = 26, signal = 9) {
  const emaFast = ema(values, fast)
  const emaSlow = ema(values, slow)
  const dif = values.map((_, i) => (emaFast[i] === null || emaSlow[i] === null ? null : emaFast[i] - emaSlow[i]))
  const difClean = dif.map((v) => (v === null ? 0 : v))
  const dea = ema(difClean, signal)
  const hist = dif.map((v, i) => (v === null || dea[i] === null ? null : (v - dea[i]) * 2))
  return { dif, dea, hist }
}

/** 布林带。 */
export function bollinger(values, period = 20, mult = 2) {
  const mid = sma(values, period)
  const upper = new Array(values.length).fill(null)
  const lower = new Array(values.length).fill(null)
  for (let i = period - 1; i < values.length; i += 1) {
    const win = values.slice(i - period + 1, i + 1)
    const mean = mid[i]
    const variance = win.reduce((s, v) => s + Math.pow(v - mean, 2), 0) / period
    const sd = Math.sqrt(variance)
    upper[i] = mean + mult * sd
    lower[i] = mean - mult * sd
  }
  return { mid, upper, lower }
}

/** ATR（真实波幅均值）。 */
export function atr(klines, period = 14) {
  const trs = klines.map((k, i) => {
    if (i === 0) return k.high - k.low
    const prevClose = klines[i - 1].close
    return Math.max(k.high - k.low, Math.abs(k.high - prevClose), Math.abs(k.low - prevClose))
  })
  return sma(trs, period)
}

/** KDJ（9/3/3）。 */
export function kdj(klines, period = 9) {
  const k = new Array(klines.length).fill(null)
  const d = new Array(klines.length).fill(null)
  const j = new Array(klines.length).fill(null)
  let prevK = 50
  let prevD = 50
  for (let i = 0; i < klines.length; i += 1) {
    if (i < period - 1) continue
    const win = klines.slice(i - period + 1, i + 1)
    const high = Math.max(...win.map((x) => x.high))
    const low = Math.min(...win.map((x) => x.low))
    const rsv = high === low ? 50 : ((klines[i].close - low) / (high - low)) * 100
    prevK = (2 / 3) * prevK + (1 / 3) * rsv
    prevD = (2 / 3) * prevD + (1 / 3) * prevK
    k[i] = prevK
    d[i] = prevD
    j[i] = 3 * prevK - 2 * prevD
  }
  return { k, d, j }
}

const last = (arr, back = 0) => {
  const idx = arr.length - 1 - back
  return idx >= 0 ? arr[idx] : null
}

/**
 * 由日线算指标快照 + 趋势判定（看板与报告共用的唯一口径）。
 * @param klines - `[{date, open, high, low, close, volume}]` 升序。
 */
export function computeIndicators(klines) {
  if (!Array.isArray(klines) || klines.length === 0) return null
  const closes = klines.map((k) => k.close)
  const volumes = klines.map((k) => k.volume)
  const ma5 = sma(closes, 5)
  const ma10 = sma(closes, 10)
  const ma20 = sma(closes, 20)
  const ma60 = sma(closes, 60)
  const rsi14 = rsi(closes, 14)
  const macdOut = macd(closes)
  const boll = bollinger(closes, 20, 2)
  const atr14 = atr(klines, 14)
  const kdjOut = kdj(klines, 9)
  const volMa20 = sma(volumes, 20)
  const close = last(closes)
  const high60 = Math.max(...klines.slice(-60).map((k) => k.high))
  const low60 = Math.min(...klines.slice(-60).map((k) => k.low))
  const high20 = Math.max(...klines.slice(-20).map((k) => k.high))
  const low20 = Math.min(...klines.slice(-20).map((k) => k.low))
  const m5 = last(ma5)
  const m10 = last(ma10)
  const m20 = last(ma20)
  const prevM5 = last(ma5, 1)
  const prevM20 = last(ma20, 1)
  const trend = m5 === null || m20 === null ? 'unknown'
    : (m5 > m20 && (m10 === null || m5 > m10) ? 'up' : (m5 < m20 && (m10 === null || m5 < m10) ? 'down' : 'flat'))
  const drawdown = high60 > 0 ? ((high60 - close) / high60) * 100 : null
  return {
    date: klines[klines.length - 1].date,
    bars: klines.length,
    close,
    ma: { ma5: m5, ma10: m10, ma20: m20, ma60: last(ma60), prev_ma5: prevM5, prev_ma20: prevM20 },
    macd: { dif: last(macdOut.dif), dea: last(macdOut.dea), hist: last(macdOut.hist) },
    rsi14: last(rsi14),
    boll: { mid: last(boll.mid), upper: last(boll.upper), lower: last(boll.lower) },
    atr14: last(atr14),
    atr_pct: (() => { const a = last(atr14); return a !== null && close > 0 ? (a / close) * 100 : null })(),
    kdj: { k: last(kdjOut.k), d: last(kdjOut.d), j: last(kdjOut.j) },
    volume: last(volumes),
    volume_ma20: last(volMa20),
    high20,
    low20,
    high60,
    low60,
    drawdown_from_high60_pct: drawdown === null ? null : Number(drawdown.toFixed(2)),
    trend,
    series: {
      ma5: ma5.slice(-60).map((v) => (v === null ? null : Number(v.toFixed(3)))),
      ma20: ma20.slice(-60).map((v) => (v === null ? null : Number(v.toFixed(3)))),
      closes: closes.slice(-60),
    },
  }
}

/**
 * 由指标快照 + 前一根快照产出信号（纯函数，可单测）。
 * @returns 信号数组（未去重；去重由 store.signalAdd 的 dedup_key 负责）。
 */
export function detectSignals(symbol, name, ind, prev) {
  const out = []
  if (ind === null || ind === undefined) return out
  const push = (kind, direction, strength, reason) => out.push({
    symbol, name, kind, direction, strength, price: ind.close, reason,
    ts: nowIso(),
  })
  const m5 = ind.ma.ma5
  const m20 = ind.ma.ma20
  const p5 = prev === null || prev === undefined ? null : prev.ma.ma5
  const p20 = prev === null || prev === undefined ? null : prev.ma.ma20
  if (m5 !== null && m20 !== null && p5 !== null && p20 !== null) {
    if (p5 <= p20 && m5 > m20) push('golden_cross', 'buy', 0.8, 'MA5(' + m5.toFixed(2) + ') 上穿 MA20(' + m20.toFixed(2) + ')')
    if (p5 >= p20 && m5 < m20) push('death_cross', 'sell', 0.8, 'MA5(' + m5.toFixed(2) + ') 下穿 MA20(' + m20.toFixed(2) + ')')
  }
  if (ind.rsi14 !== null) {
    if (ind.rsi14 < 30) push('rsi_oversold', 'buy', 0.6, 'RSI14=' + ind.rsi14.toFixed(1) + ' 进入超卖区')
    if (ind.rsi14 > 70) push('rsi_overbought', 'sell', 0.6, 'RSI14=' + ind.rsi14.toFixed(1) + ' 进入超买区')
  }
  if (ind.close > ind.high20 && ind.bars >= 21) push('breakout_high', 'buy', 0.7, '收盘 ' + ind.close + ' 突破 20 日高点 ' + ind.high20)
  if (ind.close < ind.low20 && ind.bars >= 21) push('breakdown_low', 'sell', 0.7, '收盘 ' + ind.close + ' 跌破 20 日低点 ' + ind.low20)
  if (ind.volume !== null && ind.volume_ma20 !== null && ind.volume_ma20 > 0 && ind.volume > ind.volume_ma20 * 2.5) {
    push('volume_spike', 'buy', 0.5, '成交量 ' + Math.round(ind.volume) + ' 是 20 日均量 ' + Math.round(ind.volume_ma20) + ' 的 ' + (ind.volume / ind.volume_ma20).toFixed(1) + ' 倍')
  }
  if (ind.trend === 'up' && ind.close > ind.ma.ma20) push('ma_trend_up', 'buy', 0.4, '均线多头排列（MA5>MA10>MA20）')
  if (ind.trend === 'down' && ind.close < ind.ma.ma20) push('ma_trend_down', 'sell', 0.4, '均线空头排列（MA5<MA10<MA20）')
  if (ind.drawdown_from_high60_pct !== null && ind.drawdown_from_high60_pct >= 15) {
    push('drawdown_alert', 'sell', 0.9, '距 60 日高点回撤 ' + ind.drawdown_from_high60_pct + '%（高点 ' + ind.high60 + '）')
  }
  return out
}

/* ────────────────────────────── 取数实现 ────────────────────────────── */

/** 解析腾讯行情返回（`v_sh600519="..."` 行）。 */
export function parseTencentQuotes(text, requested) {
  const rows = []
  const byCode = new Map()
  for (const norm of requested) {
    const code = tencentCode(norm)
    if (code !== null) byCode.set(code, norm)
  }
  const re = /v_([a-zA-Z0-9]+)="([^"]*)"/g
  let match
  while ((match = re.exec(text)) !== null) {
    const key = match[1]
    const fields = match[2].split('~')
    if (fields.length < 35) continue
    const norm = byCode.get(key.toLowerCase()) || normalizeSymbol(key)
    const price = n(fields[3])
    if (price === null || price <= 0) continue
    const prevClose = n(fields[4])
    const change = n(fields[31]) !== null ? n(fields[31]) : (prevClose !== null ? price - prevClose : null)
    const pct = n(fields[32]) !== null ? n(fields[32]) : (prevClose !== null && prevClose > 0 ? ((price - prevClose) / prevClose) * 100 : null)
    const volume = n(fields[36]) !== null ? n(fields[36]) : n(fields[6])
    const amountWan = n(fields[37])
    rows.push({
      symbol: norm.symbol,
      name: fields[1] || null,
      market: norm.market,
      ts: nowIso(),
      price,
      prev_close: prevClose,
      change,
      pct,
      open: n(fields[5]),
      high: n(fields[33]),
      low: n(fields[34]),
      volume,
      amount: amountWan === null ? null : amountWan * 10000,
      source: 'tencent',
      session: null,
      stale: false,
    })
  }
  return rows
}

/** A股/港股/美股实时快照。 */
async function fetchTencent(norms) {
  const codes = norms.map(tencentCode).filter((c) => c !== null)
  if (codes.length === 0) return []
  const rows = []
  for (let i = 0; i < codes.length; i += QUOTE_BATCH) {
    const batch = codes.slice(i, i + QUOTE_BATCH)
    const url = 'https://qt.gtimg.cn/q=' + batch.join(',')
    const text = await fetchText(url, { charset: 'gbk' })
    rows.push(...parseTencentQuotes(text, norms))
  }
  return rows
}

/** 加密实时快照：新浪外汇（国内直连）→ Binance → OKX → 火币 → CoinGecko。 */
async function fetchCrypto(norms) {
  const rows = []
  const remaining = []
  /* ① 新浪外汇：`fx_sbtcusd` 直连可达（国内源），目前覆盖 BTC/USD */
  const sinaSupported = new Set(['BTC'])
  const sinaList = norms.filter((nm) => sinaSupported.has(nm.symbol.replace('binance:', '').split('/')[0]))
  if (sinaList.length > 0) {
    try {
      const codes = sinaList.map((nm) => 'fx_s' + nm.symbol.replace('binance:', '').split('/')[0].toLowerCase() + 'usd')
      const text = await fetchText('https://hq.sinajs.cn/list=' + codes.join(','), {
        charset: 'gbk', headers: { referer: 'https://finance.sina.com.cn' },
      })
      const re = /var hq_str_fx_s([a-z0-9]+)="([^"]*)"/g
      let match
      while ((match = re.exec(text)) !== null) {
        const base = match[1].replace(/usd$/, '').toUpperCase()
        const f = match[2].split(',')
        if (f.length < 10 || f[8] === '') continue
        const norm = sinaList.find((nm) => nm.symbol.replace('binance:', '').split('/')[0] === base)
        if (norm === undefined) continue
        rows.push({
          symbol: norm.symbol, name: (f[9] || base + '/USD') + '（新浪外汇）', market: 'crypto',
          ts: (f[17] || nowIso().slice(0, 10)) + 'T' + (f[0] || '00:00:00'),
          price: n(f[8]), prev_close: n(f[3]), change: n(f[11]), pct: n(f[10]),
          open: n(f[7]), high: n(f[6]), low: n(f[5]), volume: n(f[4]), amount: null,
          source: 'sina-fx', session: 'open', stale: false,
        })
      }
    } catch (error) {
      remaining.push(...sinaList.map((nm) => ({ norm: nm, error: 'sina-fx: ' + (error.message || String(error)) })))
      rows.length = 0
    }
    for (const nm of sinaList) if (!rows.some((r) => r.symbol === nm.symbol)) remaining.push({ norm: nm, error: null })
  }
  for (const nm of norms) if (!sinaSupported.has(nm.symbol.replace('binance:', '').split('/')[0])) remaining.push({ norm: nm, error: null })

  const todo = remaining.length > 0 ? remaining.map((item) => item.norm) : []
  const errorsBySymbol = new Map()
  for (const item of remaining) if (item.error !== null) errorsBySymbol.set(item.norm.symbol, [item.error])

  for (const norm of todo) {
    const [base, quote] = norm.symbol.replace('binance:', '').split('/')
    const pair = base + quote
    let row = null
    const errors = errorsBySymbol.get(norm.symbol) || []
    /* ② Binance */
    try {
      const data = await fetchJson('https://api.binance.com/api/v3/ticker/24hr?symbol=' + pair, { timeoutMs: 8000 })
      row = {
        symbol: norm.symbol, name: pair, market: 'crypto', ts: nowIso(),
        price: n(data.lastPrice), prev_close: n(data.prevClosePrice),
        change: n(data.priceChange), pct: n(data.priceChangePercent),
        open: n(data.openPrice), high: n(data.highPrice), low: n(data.lowPrice),
        volume: n(data.volume), amount: n(data.quoteVolume), source: 'binance', session: 'open', stale: false,
      }
    } catch (error) { errors.push('binance: ' + (error.message || String(error))) }
    /* ③ OKX */
    if (row === null) {
      try {
        const data = await fetchJson('https://www.okx.com/api/v5/market/ticker?instId=' + base + '-' + quote, { timeoutMs: 8000 })
        const t = Array.isArray(data.data) && data.data.length > 0 ? data.data[0] : null
        if (t !== null) {
          const price = n(t.last)
          const open24 = n(t.open24h)
          row = {
            symbol: norm.symbol, name: base + '-' + quote, market: 'crypto', ts: nowIso(),
            price, prev_close: open24,
            change: price !== null && open24 !== null ? price - open24 : null,
            pct: price !== null && open24 !== null && open24 > 0 ? ((price - open24) / open24) * 100 : null,
            open: open24, high: n(t.high24h), low: n(t.low24h),
            volume: n(t.vol24h), amount: n(t.volCcy24h), source: 'okx', session: 'open', stale: false,
          }
        }
      } catch (error) { errors.push('okx: ' + (error.message || String(error))) }
    }
    /* ④ 火币 */
    if (row === null) {
      try {
        const data = await fetchJson('https://api.huobi.pro/market/detail/merged?symbol=' + pair.toLowerCase(), { timeoutMs: 8000 })
        if (data && data.tick) {
          const price = n(data.tick.close)
          const open24 = n(data.tick.open)
          row = {
            symbol: norm.symbol, name: pair, market: 'crypto', ts: nowIso(),
            price, prev_close: open24,
            change: price !== null && open24 !== null ? price - open24 : null,
            pct: price !== null && open24 !== null && open24 > 0 ? ((price - open24) / open24) * 100 : null,
            open: open24, high: n(data.tick.high), low: n(data.tick.low),
            volume: n(data.tick.amount), amount: n(data.tick.vol), source: 'huobi', session: 'open', stale: false,
          }
        }
      } catch (error) { errors.push('huobi: ' + (error.message || String(error))) }
    }
    /* ⑤ CoinGecko（最后兜底，只有价格与 24h 涨跌） */
    if (row === null) {
      try {
        const id = { BTC: 'bitcoin', ETH: 'ethereum', SOL: 'solana', BNB: 'binancecoin', XRP: 'ripple', DOGE: 'dogecoin' }[base]
        if (id !== undefined) {
          const data = await fetchJson('https://api.coingecko.com/api/v3/simple/price?ids=' + id + '&vs_currencies=usd&include_24hr_change=true', { timeoutMs: 8000 })
          const node = data[id]
          if (node !== undefined) {
            row = {
              symbol: norm.symbol, name: pair, market: 'crypto', ts: nowIso(),
              price: n(node.usd), prev_close: null, change: null, pct: n(node.usd_24h_change),
              open: null, high: null, low: null, volume: null, amount: null,
              source: 'coingecko', session: 'open', stale: false,
            }
          }
        }
      } catch (error) { errors.push('coingecko: ' + (error.message || String(error))) }
    }
    if (row !== null && row.price !== null) rows.push(row)
    else if (errors.length > 0) throw new Error('加密行情不可达 ' + norm.symbol + '（' + errors.join('；') + '）')
  }
  return rows
}

/** 腾讯日线（前复权）。 */
async function fetchTencentKlines(norm, count) {
  const code = tencentCode(norm)
  if (code === null) throw new Error('腾讯日线不支持 ' + norm.symbol)
  const url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=' + code + ',day,,,' + count + ',qfq'
  const data = await fetchJson(url)
  const node = data && data.data ? data.data[code] : null
  if (node === null || node === undefined) throw new Error('腾讯日线返回为空：' + norm.symbol)
  const list = node.qfqday || node.day || []
  return list.map((row) => ({
    date: String(row[0]),
    open: n(row[1]), close: n(row[2]), high: n(row[3]), low: n(row[4]), volume: n(row[5]),
  })).filter((k) => k.date && k.close !== null)
}

/** Sina 日线兜底。 */
async function fetchSinaKlines(norm, count) {
  const code = norm.market === 'a' ? norm.symbol : tencentCode(norm)
  const url = 'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol='
    + code + '&scale=240&ma=no&datalen=' + count
  const text = await fetchText(url, { charset: 'utf-8' })
  const data = JSON.parse(text.replace(/(\w+):/g, '"$1":'))
  return (Array.isArray(data) ? data : []).map((row) => ({
    date: String(row.day).slice(0, 10),
    open: n(row.open), close: n(row.close), high: n(row.high), low: n(row.low), volume: n(row.volume),
  })).filter((k) => k.date && k.close !== null)
}

/** 加密日线：新浪全球期货（直连，覆盖 BTC）→ Binance → OKX。 */
async function fetchCryptoKlines(norm, count) {
  const [base, quote] = norm.symbol.replace('binance:', '').split('/')
  const errors = []
  if (base === 'BTC') {
    try {
      const text = await fetchText('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_k=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=BTC', { charset: 'utf-8' })
      const start = text.indexOf('([')
      const end = text.lastIndexOf('])')
      const rows = start >= 0 && end > start ? JSON.parse(text.slice(start + 1, end + 1)) : []
      const mapped = rows.map((row) => ({
        date: String(row.date), open: n(row.open), high: n(row.high), low: n(row.low), close: n(row.close), volume: n(row.volume),
      })).filter((k) => k.close !== null)
      if (mapped.length > 0) return mapped.slice(-count)
    } catch (error) { errors.push('sina-futures: ' + (error.message || String(error))) }
  }
  try {
    const data = await fetchJson('https://api.binance.com/api/v3/klines?symbol=' + base + quote + '&interval=1d&limit=' + count, { timeoutMs: 8000 })
    const rows = (Array.isArray(data) ? data : []).map((row) => ({
      date: new Date(Number(row[0])).toISOString().slice(0, 10),
      open: n(row[1]), high: n(row[2]), low: n(row[3]), close: n(row[4]), volume: n(row[5]),
    }))
    if (rows.length > 0) return rows
  } catch (error) { errors.push('binance: ' + (error.message || String(error))) }
  try {
    const data = await fetchJson('https://www.okx.com/api/v5/market/candles?instId=' + base + '-' + quote + '&bar=1D&limit=' + Math.min(300, count), { timeoutMs: 8000 })
    const rows = (Array.isArray(data.data) ? data.data : []).map((row) => ({
      date: new Date(Number(row[0])).toISOString().slice(0, 10),
      open: n(row[1]), high: n(row[2]), low: n(row[3]), close: n(row[4]), volume: n(row[5]),
    })).reverse()
    if (rows.length > 0) return rows
  } catch (error) { errors.push('okx: ' + (error.message || String(error))) }
  throw new Error('加密日线取数失败 ' + norm.symbol + '（' + errors.join('；') + '）')
}

/* ────────────────────────────── 引擎 ────────────────────────────── */

/**
 * 行情引擎实例（发布为 ctx.aiquantFeed）。
 */
export class QuantFeed {
  /**
   * @param ctx - 插件上下文。
   * @param store - 事实库（ctx.aiquant）。
   * @param config - `{ intervalMs, idleIntervalMs, klineRefreshMinutes, klineCount, autoScan }`。
   */
  constructor(ctx, store, config = {}) {
    this.ctx = ctx
    this.store = store
    this.intervalMs = clampInt(config.intervalMs, 3000, 600000, 6000)
    this.idleIntervalMs = clampInt(config.idleIntervalMs, 10000, 3600000, 60000)
    this.klineRefreshMinutes = clampInt(config.klineRefreshMinutes, 1, 1440, 30)
    this.klineCount = clampInt(config.klineCount, 60, 1200, 320)
    this.autoScan = config.autoScan !== false
    this.quoteTtlMs = 2000
    this.lastQuoteAt = 0
    this.lastScanAt = 0
    this.lastError = null
    this.cycles = 0
    this.timer = null
    this.running = false
    this.inFlight = false
    this.analysisCache = new Map()
  }

  /** 启动轮询（fiber 内调用；dispose 时自动停）。 */
  start() {
    if (this.timer !== null) return
    this.running = true
    const tick = async () => {
      if (!this.running) return
      const session = marketSession()
      const active = session.a_share === 'open' || session.hk === 'open' || session.us === 'open' || this.store.positionsList().length > 0
      const delay = active ? this.intervalMs : this.idleIntervalMs
      this.timer = setTimeout(async () => {
        try {
          await this.cycle()
        } catch (error) {
          this.lastError = error && error.message ? error.message : String(error)
          this.store.runLog({ kind: 'feed', status: 'error', detail: this.lastError, actor: 'feed' })
        }
        void tick()
      }, delay)
      if (this.timer.unref !== undefined) this.timer.unref()
    }
    void tick()
  }

  /** 停止轮询。 */
  stop() {
    this.running = false
    if (this.timer !== null) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  /**
   * 一轮：刷新自选+持仓行情 → 增量出信号 → 记录持仓市值点。
   */
  async cycle() {
    if (this.inFlight) return { skipped: 'in-flight' }
    this.inFlight = true
    try {
      this.cycles += 1
      const symbols = this.trackedSymbols()
      let quotes = []
      let error = null
      try {
        quotes = await this.refreshQuotes(symbols)
      } catch (err) {
        error = err
      }
      if (this.autoScan && this.cycles % 3 === 1) {
        try { await this.scan({ force: false }) } catch (err) { error = err }
      }
      this.recordEquityPoint()
      this.store.setMeta('feed_state', JSON.stringify({
        last_cycle: nowIso(),
        cycles: this.cycles,
        symbols: symbols.length,
        quotes: quotes.length,
        error: error === null ? null : (error.message || String(error)),
        market: marketSession(),
      }))
      if (error !== null) this.lastError = error.message || String(error)
      else this.lastError = null
      return { ok: error === null, quotes: quotes.length, error: this.lastError }
    } finally {
      this.inFlight = false
    }
  }

  /** 需要跟踪的标的：自选 + 持仓。 */
  trackedSymbols() {
    const set = new Set()
    for (const w of this.store.watchlistList()) set.add(w.symbol)
    for (const p of this.store.positionsList({ status: 'open' })) set.add(p.symbol)
    return Array.from(set)
  }

  /**
   * 刷新实时行情。
   * @param symbols - 标的数组（缺省=自选+持仓）；`force=true` 跳过 TTL。
   */
  async refreshQuotes(symbols, options = {}) {
    const list = (Array.isArray(symbols) && symbols.length > 0 ? symbols : this.trackedSymbols())
      .map((s) => normalizeSymbol(s))
      .filter((s) => s.symbol !== '')
    if (list.length === 0) return []
    const now = Date.now()
    if (options.force !== true && now - this.lastQuoteAt < this.quoteTtlMs) {
      return this.store.quotesGet(list.map((s) => s.symbol))
    }
    const market = list.filter((s) => s.market !== 'crypto')
    const crypto = list.filter((s) => s.market === 'crypto')
    const rows = []
    const errors = []
    if (market.length > 0) {
      try {
        rows.push(...await fetchTencent(market))
      } catch (error) {
        errors.push('腾讯行情：' + (error.message || String(error)))
      }
    }
    if (crypto.length > 0) {
      try {
        rows.push(...await fetchCrypto(crypto))
      } catch (error) {
        errors.push('加密行情：' + (error.message || String(error)))
      }
    }
    if (rows.length > 0) {
      this.store.quotesUpsert(rows)
      this.store.setMeta('quote_source', Array.from(new Set(rows.map((r) => r.source))).join(','))
      for (const row of rows) this.store.quoteHistoryPush(row.symbol, row.price)
      /* 没拿到的那部分标记 stale，界面显示"缓存"而不是假装实时 */
      const got = new Set(rows.map((r) => r.symbol))
      const missing = list.filter((s) => !got.has(s.symbol)).map((s) => s.symbol)
      if (missing.length > 0) this.markStale(missing)
    } else {
      this.markStale(list.map((s) => s.symbol))
    }
    this.lastQuoteAt = Date.now()
    if (errors.length > 0) {
      const message = errors.join('；')
      this.store.runLog({ kind: 'feed', status: 'warn', detail: message, actor: 'feed' })
      if (rows.length === 0) throw new Error(message)
    }
    return rows
  }

  markStale(symbols) {
    try {
      const stmt = this.store.db.prepare('UPDATE quotes SET stale = 1 WHERE symbol = ?')
      for (const symbol of symbols) stmt.run(normalizeSymbol(symbol).symbol)
    } catch { /* 忽略 */ }
  }

  /** 确保日线数据新鲜（命中缓存直接返回）。 */
  async ensureKlines(symbol, options = {}) {
    const norm = normalizeSymbol(symbol)
    const want = clampInt(options.count, 60, 1200, this.klineCount)
    const fetchedAt = this.store.klinesFetchedAt(norm.symbol)
    const fresh = fetchedAt !== undefined && (Date.now() - Date.parse(fetchedAt)) < this.klineRefreshMinutes * 60000
    if (fresh && options.force !== true && this.store.klinesList(norm.symbol, 'daily', want + 10).length >= Math.min(want, 60)) {
      return this.store.klinesList(norm.symbol, 'daily', want)
    }
    let rows = []
    const errors = []
    if (norm.market === 'crypto') {
      try { rows = await fetchCryptoKlines(norm, want) } catch (error) { errors.push('binance: ' + (error.message || String(error))) }
    } else {
      try { rows = await fetchTencentKlines(norm, want) } catch (error) { errors.push('tencent: ' + (error.message || String(error))) }
      if (rows.length < Math.min(want, 60)) {
        try { rows = await fetchSinaKlines(norm, want) } catch (error) { errors.push('sina: ' + (error.message || String(error))) }
      }
    }
    if (rows.length > 0) {
      this.store.klinesUpsert(norm.symbol, 'daily', rows)
      return this.store.klinesList(norm.symbol, 'daily', want)
    }
    if (errors.length > 0) throw new Error('日线取数失败 ' + norm.symbol + '（' + errors.join('；') + '）')
    return []
  }

  /** 单标的指标快照（带内存缓存）。 */
  async analyzeSymbol(symbol, options = {}) {
    const norm = normalizeSymbol(symbol)
    const ttl = 60000
    const cached = this.analysisCache.get(norm.symbol)
    if (cached !== undefined && options.force !== true && Date.now() - cached.at < ttl) return cached.value
    const klines = await this.ensureKlines(norm.symbol, options)
    const ind = computeIndicators(klines)
    const prev = klines.length >= 2 ? computeIndicators(klines.slice(0, -1)) : null
    const value = { symbol: norm.symbol, indicators: ind, prev_indicators: prev, klines: klines.slice(-120) }
    this.analysisCache.set(norm.symbol, { at: Date.now(), value })
    return value
  }

  /**
   * 扫描自选+持仓：拉日线 → 算指标 → 落信号。
   * @returns `{ scanned, signals, errors }`
   */
  async scan(options = {}) {
    const symbols = Array.isArray(options.symbols) && options.symbols.length > 0
      ? options.symbols
      : this.trackedSymbols()
    const signals = []
    const errors = []
    let scanned = 0
    for (const symbol of symbols) {
      const norm = normalizeSymbol(symbol)
      try {
        const analysis = await this.analyzeSymbol(norm.symbol, { force: options.force === true })
        if (analysis.indicators === null) {
          errors.push(norm.symbol + '：无日线数据')
          continue
        }
        scanned += 1
        const name = (this.store.watchlistGet(norm.symbol) || {}).name || null
        const detected = detectSignals(norm.symbol, name, analysis.indicators, analysis.prev_indicators)
        for (const signal of detected) {
          const result = this.store.signalAdd(signal)
          if (!result.deduped) signals.push(result.signal)
        }
      } catch (error) {
        errors.push(norm.symbol + '：' + (error.message || String(error)))
      }
    }
    this.lastScanAt = Date.now()
    this.store.setMeta('last_scan', nowIso())
    if (signals.length > 0) {
      this.store.runLog({
        kind: 'scan', status: 'ok', actor: 'feed',
        detail: '扫描 ' + scanned + ' 个标的，新增 ' + signals.length + ' 条信号：'
          + signals.slice(0, 6).map((s) => s.symbol + '/' + (SIGNAL_KIND_LABEL[s.kind] || s.kind)).join('、'),
      })
    } else if (errors.length > 0) {
      this.store.runLog({ kind: 'scan', status: 'warn', actor: 'feed', detail: '扫描告警：' + errors.slice(0, 5).join('；') })
    }
    return { scanned, signals, errors }
  }

  /** 记录一个持仓市值点（有持仓才记；15 分钟一个点，避免刷库）。 */
  recordEquityPoint() {
    const positions = this.store.positionsList({ status: 'open' })
    if (positions.length === 0) return null
    const pf = this.store.portfolio()
    if (!(pf.totals.market_value > 0)) return null
    const lastPoints = this.store.equityList({ portfolio: 'default', limit: 1 })
    const prev = lastPoints.length > 0 ? Date.parse(lastPoints[lastPoints.length - 1].ts) : 0
    if (prev > 0 && Date.now() - prev < 15 * 60000) return null
    this.store.equityAdd({
      portfolio: 'default',
      nav: pf.totals.market_value,
      pnl: pf.totals.pnl,
      note: 'auto:' + pf.totals.count + 'positions',
    })
    return true
  }

  /** 引擎状态（看板顶部与自检用）。 */
  status(extra = {}) {
    return {
      running: this.running,
      cycles: this.cycles,
      interval_ms: this.intervalMs,
      idle_interval_ms: this.idleIntervalMs,
      last_quote_at: this.lastQuoteAt === 0 ? null : new Date(this.lastQuoteAt).toISOString(),
      last_scan_at: this.lastScanAt === 0 ? null : new Date(this.lastScanAt).toISOString(),
      last_error: this.lastError,
      tracked: this.trackedSymbols().length,
      kline_refresh_minutes: this.klineRefreshMinutes,
      market: marketSession(),
      ...extra,
    }
  }

  /**
   * 逐个探测数据源可达性（`aiquant_env probe=true` 与看板「数据」页用）。
   * 每项返回 `{ name, ok, detail, ms }`，不抛错。
   */
  async probeSources() {
    const probes = [
      {
        name: '腾讯行情（A股/指数/港股）',
        run: async () => {
          const rows = await fetchTencent([normalizeSymbol('sh000001'), normalizeSymbol('sh600519')])
          if (rows.length === 0) throw new Error('返回为空')
          return rows.map((r) => (r.name || r.symbol) + ' ' + r.price).join('、')
        },
      },
      {
        name: '腾讯日线（前复权）',
        run: async () => {
          const rows = await fetchTencentKlines(normalizeSymbol('sh600519'), 20)
          if (rows.length === 0) throw new Error('返回为空')
          return rows.length + ' 根，最新 ' + rows[rows.length - 1].date + ' 收 ' + rows[rows.length - 1].close
        },
      },
      {
        name: '新浪外汇（加密现货，国内直连）',
        run: async () => {
          const text = await fetchText('https://hq.sinajs.cn/list=fx_sbtcusd', { charset: 'gbk', headers: { referer: 'https://finance.sina.com.cn' } })
          const match = /="([^"]*)"/.exec(text)
          if (match === null || match[1] === '') throw new Error('返回为空')
          const fields = match[1].split(',')
          return (fields[9] || 'BTC/USD') + ' ' + fields[8]
        },
      },
      {
        name: '新浪全球期货日线（BTC 日线兜底）',
        run: async () => {
          const text = await fetchText('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_k=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=BTC', { charset: 'utf-8' })
          const rows = JSON.parse(text.slice(text.indexOf('([') + 1, text.lastIndexOf('])') + 1))
          return rows.length + ' 根，最新 ' + rows[rows.length - 1].date + ' 收 ' + rows[rows.length - 1].close
        },
      },
      {
        name: 'Binance（直连/系统代理）',
        run: async () => {
          const data = await fetchJson('https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT', { timeoutMs: 8000 })
          return 'BTCUSDT ' + data.lastPrice
        },
      },
      {
        name: 'OKX（直连/系统代理）',
        run: async () => {
          const data = await fetchJson('https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT', { timeoutMs: 8000 })
          return 'BTC-USDT ' + (data.data && data.data[0] ? data.data[0].last : '(空)')
        },
      },
    ]
    const results = []
    for (const probe of probes) {
      const started = Date.now()
      try {
        const detail = await probe.run()
        results.push({ name: probe.name, ok: true, detail: String(detail), ms: Date.now() - started })
      } catch (error) {
        results.push({ name: probe.name, ok: false, detail: error && error.message ? error.message : String(error), ms: Date.now() - started })
      }
    }
    return results
  }
}

function clampInt(value, min, max, fallback) {
  const n = Number(value)
  if (!Number.isFinite(n)) return fallback
  return Math.max(min, Math.min(max, Math.floor(n)))
}

/**
 * @param ctx - 插件上下文。
 * @param config - 见 {@link QuantFeed}。
 */
export function apply(ctx, config = {}) {
  const store = ctx.aiquant
  const feed = new QuantFeed(ctx, store, config || {})
  ctx.effect(() => () => feed.stop())
  ctx.provide('aiquantFeed', feed)
  feed.start()
  ctx.logger?.info?.(
    'aiquant-feed: 就绪（轮询 %dms / 空闲 %dms；日线刷新 %d 分钟；跟踪 %d 个标的）',
    feed.intervalMs, feed.idleIntervalMs, feed.klineRefreshMinutes, feed.trackedSymbols().length,
  )
}

export default { name, inject, apply }
