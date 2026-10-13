#!/usr/bin/env node
/**
 * 行情引擎自检：① 指标纯函数（已知答案）② 腾讯报文解析 ③ 信号规则 ④ 真实联网取数。
 * 用法：node test/feed-smoke.mjs [--offline]
 * 退出码 0 = 全部通过（联网项失败会明确列出，不掩盖）。
 */
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { QuantStore } from '../lib/store-core.js'
import {
  QuantFeed, sma, ema, rsi, macd, bollinger, atr, kdj, computeIndicators, detectSignals, parseTencentQuotes,
} from '../lib/feed.js'

const offline = process.argv.includes('--offline')
const root = mkdtempSync(join(tmpdir(), 'aiquant-feed-'))
const results = []
const check = (name, ok, detail) => {
  results.push({ name, ok: Boolean(ok), detail })
  console.log((ok ? '[PASS] ' : '[FAIL] ') + name + (detail === undefined ? '' : ' — ' + detail))
}
const near = (a, b, tol = 1e-6) => a !== null && b !== null && Math.abs(a - b) <= tol

const store = new QuantStore(root, { workspace: 'D:/量化' })
const ctx = { logger: { info: () => {}, warn: () => {}, error: () => {} } }
const feed = new QuantFeed(ctx, store, { intervalMs: 60000, idleIntervalMs: 60000 })

/* ───────── ① 指标纯函数 ───────── */
const closes = [10, 11, 12, 13, 14, 15, 14, 13, 12, 11, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
const ma3 = sma(closes, 3)
check('SMA 正确（末值）', near(ma3[ma3.length - 1], (18 + 19 + 20) / 3), 'ma3=' + ma3[ma3.length - 1])
check('SMA 前缀补 null', ma3[0] === null && ma3[1] === null && ma3[2] !== null)
const e5 = ema(closes, 5)
check('EMA 收敛且在值域内', e5[e5.length - 1] > 15 && e5[e5.length - 1] < 20, 'ema5=' + e5[e5.length - 1].toFixed(3))
const rising = Array.from({ length: 30 }, (_, i) => 100 + i)
check('单调上涨 RSI=100', near(rsi(rising, 14)[29], 100, 1e-9), 'rsi=' + rsi(rising, 14)[29])
const falling = Array.from({ length: 30 }, (_, i) => 100 - i)
check('单调下跌 RSI=0', near(rsi(falling, 14)[29], 0, 1e-9), 'rsi=' + rsi(falling, 14)[29])
const m = macd(rising)
check('MACD 上涨段 DIF>DEA', m.dif[29] > m.dea[29] && m.hist[29] > 0,
  'dif=' + m.dif[29].toFixed(3) + ' dea=' + m.dea[29].toFixed(3))
const b = bollinger(closes, 20, 2)
check('BOLL 上轨>中轨>下轨', b.upper[20] > b.mid[20] && b.mid[20] > b.lower[20],
  'mid=' + b.mid[20].toFixed(2) + ' up=' + b.upper[20].toFixed(2) + ' low=' + b.lower[20].toFixed(2))
const kl = closes.map((c, i) => ({ date: '2026-01-' + String(1 + (i % 28)).padStart(2, '0'), open: c - 0.5, high: c + 1, low: c - 1, close: c, volume: 1000 + i * 10 }))
const a14 = atr(kl, 14)
check('ATR 非负', a14[a14.length - 1] > 0, 'atr=' + a14[a14.length - 1].toFixed(3))
const k = kdj(kl, 9)
check('KDJ 输出在合理范围', k.k[20] !== null && k.d[20] !== null && k.k[20] >= 0 && k.k[20] <= 100,
  'K=' + k.k[20].toFixed(1) + ' D=' + k.d[20].toFixed(1) + ' J=' + k.j[20].toFixed(1))

/* ───────── ② 腾讯报文解析 ───────── */
/* 按真实字段位构造报文：1=名称 2=代码 3=现价 4=昨收 5=今开 6=成交量(手)
   30=时间 31=涨跌 32=涨跌% 33=最高 34=最低 36=成交量 37=成交额(万) */
const f = new Array(45).fill('0')
f[0] = '1'
f[1] = '贵州茅台'
f[2] = '600519'
f[3] = '1408.00'
f[4] = '1400.00'
f[5] = '1405.00'
f[6] = '30000'
f[30] = '20261009150000'
f[31] = '8.00'
f[32] = '0.57'
f[33] = '1410.00'
f[34] = '1395.00'
f[35] = '1408.00/30000/4224000000'
f[36] = '30000'
f[37] = '422400.00'
const canned = 'v_sh600519="' + f.join('~') + '";'
const parsed = parseTencentQuotes(canned, [])
check('腾讯报文解析（名称/价格/涨跌）',
  parsed.length === 1 && parsed[0].symbol === 'sh600519' && parsed[0].name === '贵州茅台'
  && near(parsed[0].price, 1408) && near(parsed[0].pct, 0.57),
  JSON.stringify(parsed[0] === undefined ? null : { s: parsed[0].symbol, n: parsed[0].name, p: parsed[0].price, pct: parsed[0].pct }))

/* ───────── ③ 信号规则 ───────── */
function klinesFromCloses(list, base = 1000) {
  return list.map((c, i) => ({ date: '2026-02-' + String(1 + (i % 28)).padStart(2, '0'), open: c, high: c * 1.001, low: c * 0.999, close: c, volume: base }))
}
/* 先跌后急涨 → 应出现金叉 */
const downThenUp = []
for (let i = 0; i < 40; i += 1) downThenUp.push(100 - i * 0.8)
for (let i = 0; i < 12; i += 1) downThenUp.push(downThenUp[downThenUp.length - 1] + i * 1.2)
const kUp = klinesFromCloses(downThenUp)
const indUp = computeIndicators(kUp)
const prevUp = computeIndicators(kUp.slice(0, -1))
const sigUp = detectSignals('sh600519', '测试', indUp, prevUp)
check('指标快照结构完整', indUp !== null && indUp.ma.ma5 !== null && indUp.rsi14 !== null && indUp.trend !== 'unknown',
  'trend=' + indUp.trend + ' rsi=' + (indUp.rsi14 === null ? '-' : indUp.rsi14.toFixed(1)) + ' 回撤=' + indUp.drawdown_from_high60_pct + '%')
check('急涨段产出买入类信号', sigUp.some((s) => s.direction === 'buy'), sigUp.map((s) => s.kind).join(',') || '无')
/* 高位急跌 → 应出现死叉或回撤预警 */
const upThenDown = []
for (let i = 0; i < 40; i += 1) upThenDown.push(100 + i * 0.8)
for (let i = 0; i < 20; i += 1) upThenDown.push(upThenDown[upThenDown.length - 1] - i * 1.5)
const kDown = klinesFromCloses(upThenDown)
const sigDown = detectSignals('sh600519', '测试', computeIndicators(kDown), computeIndicators(kDown.slice(0, -1)))
check('急跌段产出卖出类信号', sigDown.some((s) => s.direction === 'sell'), sigDown.map((s) => s.kind + '/' + s.direction).join(',') || '无')
check('信号字段齐全（strength/price/reason）',
  [...sigUp, ...sigDown].every((s) => typeof s.strength === 'number' && typeof s.price === 'number' && typeof s.reason === 'string'))

/* ───────── ④ 真实联网 ───────── */
if (!offline) {
  try {
    const quotes = await feed.refreshQuotes(['600519', '000001', 'BTC-USDT'], { force: true })
    const a = quotes.find((q) => q.symbol === 'sh600519')
    const crypto = quotes.find((q) => q.symbol === 'binance:BTC/USDT')
    check('真实行情：A股', a !== undefined && a.price > 0, a === undefined ? '未取到' : a.name + ' ' + a.price + ' (' + (a.pct === null ? '-' : a.pct + '%') + ') 源=' + a.source)
    check('真实行情：加密', crypto !== undefined && crypto.price > 0, crypto === undefined ? '未取到' : crypto.name + ' ' + crypto.price + ' 源=' + crypto.source)
    const stored = store.quotesGet(['sh600519'])
    check('行情已落库', stored.length === 1 && stored[0].price > 0, stored.length > 0 ? 'price=' + stored[0].price + ' ts=' + stored[0].ts : '空')
    const hist = store.quoteHistory('sh600519', 10)
    check('迷你走势已落库', hist.length >= 1, hist.length + ' 点')
    const bars = await feed.ensureKlines('600519', { force: true, count: 120 })
    const ind = computeIndicators(bars)
    check('真实日线 + 指标', bars.length >= 60 && ind !== null && ind.ma.ma20 !== null,
      bars.length + ' 根，最新 ' + (bars.length > 0 ? bars[bars.length - 1].date + ' 收 ' + bars[bars.length - 1].close : '-')
      + '，MA20=' + (ind && ind.ma.ma20 ? ind.ma.ma20.toFixed(2) : '-') + ' RSI14=' + (ind && ind.rsi14 ? ind.rsi14.toFixed(1) : '-'))
    const scan = await feed.scan({ force: true, symbols: ['sh600519', 'sz000001'] })
    check('扫描并落信号（去重后）', scan.scanned === 2, '扫 ' + scan.scanned + ' 个，新增信号 ' + scan.signals.length + ' 条，告警 ' + scan.errors.length + ' 条')
    check('第二次扫描不重复落信号', (await feed.scan({ force: true, symbols: ['sh600519'] })).signals.length === 0)
    const cryptoBars = await feed.ensureKlines('binance:BTC/USDT', { force: true, count: 60 })
    check('真实加密日线', cryptoBars.length >= 30, cryptoBars.length + ' 根，最新收 ' + (cryptoBars.length > 0 ? cryptoBars[cryptoBars.length - 1].close : '-'))
  } catch (error) {
    check('真实联网取数', false, error && error.message ? error.message : String(error))
  }
} else {
  console.log('[SKIP] 联网项（--offline）')
}

feed.stop()
store.close()
try { rmSync(root, { recursive: true, force: true }) } catch { /* 忽略 */ }

const failed = results.filter((r) => !r.ok)
console.log('')
console.log('汇总：' + (results.length - failed.length) + '/' + results.length + ' 通过' + (failed.length > 0 ? '，失败 ' + failed.length + ' 项' : ''))
process.exit(failed.length === 0 ? 0 : 1)
