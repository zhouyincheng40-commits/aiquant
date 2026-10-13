#!/usr/bin/env node
/**
 * 事实库自检：真实建库 → 写入 → 计算 → 快照 → 报告，逐项断言。
 * 用法：node test/store-smoke.mjs [--keep]
 * 退出码 0 = 全部通过。
 */
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  QuantStore, normalizeSymbol, riskMetrics, marketSession, nowIso, SCHEMA_VERSION,
} from '../lib/store-core.js'

const keep = process.argv.includes('--keep')
const root = mkdtempSync(join(tmpdir(), 'aiquant-smoke-'))
const results = []
const check = (name, condition, detail) => {
  results.push({ name, ok: Boolean(condition), detail })
  console.log((condition ? '[PASS] ' : '[FAIL] ') + name + (detail === undefined ? '' : ' — ' + detail))
}

let store
try {
  store = new QuantStore(root, { workspace: 'D:/量化' })

  /* 1. 建库与自选种子 */
  check('schema 版本写入', Number(store.getMeta('schema_version')) === SCHEMA_VERSION, 'v' + store.getMeta('schema_version'))
  const watch = store.watchlistList()
  check('自选默认种子（8 条）', watch.length >= 8, watch.length + ' 条，例：' + watch[0].symbol)

  /* 2. symbol 规范化 */
  const normCase = [
    ['600519', 'sh600519'], ['000001', 'sz000001'], ['300750', 'sz300750'],
    ['510300', 'sh510300'], ['000300', 'sh000300'], ['159915', 'sz159915'],
    ['sh600519', 'sh600519'], ['000001.SZ', 'sz000001'], ['00700', 'hk00700'],
    ['us.AAPL', 'usAAPL'], ['BTC-USDT', 'binance:BTC/USDT'], ['BTCUSDT', 'binance:BTC/USDT'],
  ]
  let normOk = true
  for (const [input, want] of normCase) {
    const got = normalizeSymbol(input).symbol
    if (got !== want) { normOk = false; console.log('   symbol 断言失败：' + input + ' → ' + got + '（期望 ' + want + '）') }
  }
  check('symbol 规范化（12 例）', normOk)

  /* 3. 行情写入 + 历史 */
  const quoteRows = watch.map((w, i) => ({
    symbol: w.symbol,
    ts: nowIso(),
    name: w.name,
    market: w.market,
    price: 100 + i * 10,
    prev_close: 99 + i * 10,
    change: 1,
    pct: 1.01,
    open: 99, high: 101 + i, low: 98, volume: 1000, amount: 1e6,
    source: 'smoke', session: 'test', stale: false,
  }))
  const written = store.quotesUpsert(quoteRows)
  check('行情批量写入', written === quoteRows.length, written + ' 行')
  for (const w of watch) store.quoteHistoryPush(w.symbol, 100 + Math.random() * 10)
  check('迷你走势历史', store.quoteHistory(watch[0].symbol, 10).length > 0)

  /* 4. K线 + 风险指标 */
  const klines = []
  let price = 100
  const start = Date.UTC(2026, 0, 1)
  for (let i = 0; i < 120; i += 1) {
    price = price * (1 + (Math.sin(i / 6) * 0.01))
    const day = new Date(start + i * 86400000)
    klines.push({
      date: day.toISOString().slice(0, 10),
      open: price * 0.99, high: price * 1.01, low: price * 0.98, close: price, volume: 1e5,
    })
  }
  store.klinesUpsert('sh600519', 'daily', klines)
  check('K线往返（120 根）', store.klinesList('sh600519', 'daily', 200).length === 120)
  check('K线抓取时间戳', typeof store.klinesFetchedAt('sh600519') === 'string')

  /* 5. 持仓 + 组合 */
  const p1 = store.positionAdd({ symbol: '600519', qty: 100, avg_cost: 100, strategy: 'smoke', name: '贵州茅台' })
  const p2 = store.positionAdd({ symbol: '300750', qty: 200, avg_cost: 50 })
  store.positionAdd({ symbol: 'sh601318', qty: 300, avg_cost: 40 })
  check('建仓（3 笔）', store.positionsList().length === 3, 'id ' + p1.id)
  const pf = store.portfolio()
  check('组合市值计算', pf.totals.count === 3 && pf.totals.market_value > 0,
    '成本 ' + pf.totals.cost + ' 市值 ' + pf.totals.market_value + ' 权重和 ' + pf.positions.reduce((s, r) => s + r.weight, 0).toFixed(1) + '%')
  const closed = store.positionClose(p2.id, { price: 60 })
  check('平仓（已实现盈亏）', closed.realized_pnl === 2000, 'realized_pnl=' + closed.realized_pnl)

  /* 6. 净值 + 风控 */
  let nav = 1
  const navStart = Date.UTC(2026, 0, 1)
  for (let i = 0; i < 60; i += 1) {
    nav = nav * (1 + (Math.cos(i / 5) * 0.004))
    store.equityAdd({ ts: new Date(navStart + i * 86400000).toISOString().slice(0, 10) + 'T15:00:00', portfolio: 'default', nav })
  }
  const eq = store.equityList({ limit: 400 })
  check('净值序列', eq.length === 60, eq.length + ' 点，最新 ' + eq[eq.length - 1].nav.toFixed(4))
  const risk = store.risk()
  check('风控指标可算', risk.metrics.max_drawdown_pct !== null && risk.metrics.var95_pct !== null,
    '回撤 ' + risk.metrics.max_drawdown_pct + '% 波动 ' + risk.metrics.volatility_pct + '% VaR95 ' + risk.metrics.var95_pct + '% 夏普 ' + risk.metrics.sharpe)
  check('风控红线判定有结构', Array.isArray(risk.breaches) && typeof risk.passed === 'boolean', risk.passed ? '通过' : risk.breaches.length + ' 条越线')

  /* 7. 信号去重 */
  const s1 = store.signalAdd({ symbol: '600519', kind: 'golden_cross', strength: 0.8, price: 1500, reason: 'MA5 上穿 MA20' })
  const s2 = store.signalAdd({ symbol: '600519', kind: 'golden_cross', strength: 0.8, price: 1500, reason: 'MA5 上穿 MA20' })
  check('信号落库', s1.deduped === false && s2.deduped === true, '首条 ' + s1.id + '，重复被去重')

  /* 8. 回测 + 笔记 + 告警 + 日志 */
  const bt = store.backtestAdd({
    name: 'smoke 双均线', symbols: ['600519'], strategy: 'ma', start: '2025-01-01', end: '2026-01-01',
    metrics: { annual_return_pct: 12.3, max_drawdown_pct: 18.2, sharpe: 0.9 }, cost_model: 'fee 3bp + slippage 5bp', out_of_sample: true,
  })
  check('回测落库（含成本与样本外标注）', store.backtestsList()[0].id === bt.id && bt.out_of_sample === 1, bt.name)
  store.noteAdd({ kind: 'research', title: 'smoke 笔记', body: '仅测试', tags: ['smoke'], symbol: '600519' })
  check('研究笔记', store.notesList().length === 1)
  const alert = store.alertAdd({ level: 'warn', symbol: '600519', title: '回撤预警', body: '测试' })
  check('告警 + 确认', store.alertsAck(alert.id).acked === 1)

  /* 9. 快照与报告 */
  const snap = store.snapshot()
  check('看板快照完整性',
    snap.watchlist.length >= 8 && snap.positions.length === 2 && snap.signals.length === 1
    && snap.backtests.length === 1 && snap.equity.length === 60 && snap.stats.positions_all === 3,
    '自选 ' + snap.watchlist.length + ' 持仓 ' + snap.positions.length + ' 信号 ' + snap.signals.length + ' 净值点 ' + snap.equity.length)
  const md = store.reportMarkdown()
  check('报告渲染', md.includes('# AI量化日报') && md.includes('六、运行日志'), md.length + ' 字符')
  check('市场时段判定', typeof marketSession().label === 'string', marketSession().label + ' / A股 ' + marketSession().a_share)

  /* 10. 边界：空 qty / 未知 id 必须抛错 */
  let threw = 0
  try { store.positionAdd({ symbol: '600519', qty: 0, avg_cost: 1 }) } catch { threw += 1 }
  try { store.positionClose('pos_not_exist', { price: 1 }) } catch { threw += 1 }
  check('非法输入被拒绝（2 例）', threw === 2, threw + '/2')

  /* 11. 持久化：关库重开数据还在 */
  const dbFile = store.dbFile
  store.close()
  const reopened = new QuantStore(root, { workspace: 'D:/量化' })
  check('重开库数据持久', reopened.signalsList().length === 1 && reopened.positionsList().length === 2, dbFile)
  reopened.close()
} catch (error) {
  check('异常退出', false, error && error.stack ? error.stack.split('\n').slice(0, 3).join(' | ') : String(error))
} finally {
  if (store !== undefined && !keep) { /* 已在上面关闭 */ }
  if (!keep) { try { rmSync(root, { recursive: true, force: true }) } catch { /* 忽略 */ } }
  else console.log('保留测试目录：' + root)
}

const failed = results.filter((r) => !r.ok)
console.log('')
console.log('汇总：' + (results.length - failed.length) + '/' + results.length + ' 通过' + (failed.length > 0 ? '，失败 ' + failed.length + ' 项' : ''))
process.exit(failed.length === 0 ? 0 : 1)
