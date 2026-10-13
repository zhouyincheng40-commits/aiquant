/**
 * AI量化智能体工具集（preset 平面）
 *
 * 这些行只向 `tools` 注册模型可见工具，自身不发布服务，因此不需要 isolate realm；
 * 它们解析的是 host 平面的 `ctx.aiquant`（事实库）与 `ctx.aiquantFeed`（行情引擎）。
 *
 * 设计约定：
 *   · **凡展示必落库**：看板上的每个数字都来自事实库，工具不返回"只在对话里存在"的结论。
 *   · 写操作一律带来源与参数，报告里能指回具体调用。
 *   · 读操作给模型**紧凑、可判定**的文本（不贴全量 K 线，只给关键字段）。
 *   · 出错不吞：返回 JSON `{ok:false, error}`，并给出可行的下一步。
 */
import { defineTool } from '@deepseek-ai/dsh-tools'
import { existsSync, mkdirSync, writeFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { nowIso, marketSession, normalizeSymbol, SIGNAL_KIND_LABEL } from './store-core.js'

/** Cordis 插件名。 */
export const name = 'aiquant-tools'

/** 硬依赖：事实库 + 工具注册表；行情引擎/自举信息走 ctx.get 可选获取。 */
export const inject = ['aiquant', 'tools']

/** 工具统一返回紧凑字符串。 */
const text = (value) => (typeof value === 'string' ? value : JSON.stringify(value, null, 2))

/** 把执行结果包成 JSON（错误也包，不抛给模型除非是参数错误）。 */
function ok(data) {
  return text({ ok: true, ...data })
}

function fail(error, hint) {
  return text({ ok: false, error: error && error.message ? error.message : String(error), hint: hint || null })
}

/** 会话/智能体可读标识（用于落库的 run_by / author）。 */
function actorOf(exec) {
  try {
    const session = exec && exec.agent && exec.agent.session
    if (session !== undefined && session !== null && session.id !== undefined) return String(session.id).slice(0, 24)
  } catch { /* 忽略 */ }
  return 'agent'
}

/** 把报价行压成一行文本。 */
function quoteLine(row) {
  const pct = row.pct === null || row.pct === undefined ? '—' : (Number(row.pct) >= 0 ? '+' : '') + Number(row.pct).toFixed(2) + '%'
  const stale = Number(row.stale) === 1 ? '（缓存）' : ''
  return (row.name || row.symbol) + ' ' + row.symbol + '：' + (row.price === null ? '—' : row.price)
    + ' ' + pct + stale + (row.source ? ' · ' + row.source : '')
}

/**
 * @param ctx - 插件上下文。
 */
export function apply(ctx) {
  const store = ctx.aiquant
  const feed = typeof ctx.get === 'function' ? ctx.get('aiquantFeed') : undefined
  const mode = typeof ctx.get === 'function' ? ctx.get('aiquantMode') : undefined

  /* ── 环境自检 ─────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_env',
    description: '【AI量化模式开工前必须跑一次】环境自检：事实库位置与统计、行情引擎状态、工作区目录、conda 解释器（aiquant / aiquant-live）、量化 MCP 服务器文件、技能目录、系统代理与数据源可达性。返回每项的"能用/不能用 + 原因"，缺什么直接照 hint 补，不要靠猜。',
    parameters: {
      probe: { type: 'boolean', description: '是否实际探测数据源（腾讯行情 / 新浪外汇 / Binance 直连与代理），默认 true' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args) {
      try {
        const { environmentReport } = await import('./ui.js')
        const report = await environmentReport(ctx, { probeSources: args.probe !== false })
        const lines = []
        lines.push('【AI量化环境自检】' + report.ts + '（' + report.market.label + '）')
        lines.push('· Node ' + report.node + ' / ' + report.platform + '；事实库 ' + report.db_file + '（' + Math.round(report.db_bytes / 1024) + ' KB）')
        lines.push('· 工作区 ' + report.workspace + '（缓存 ' + Math.round(report.storage.cache.bytes / 1024) + ' KB / 报告 ' + report.storage.reports.files + ' 个）')
        lines.push('· 行情引擎：' + (report.modules.feed.ok ? '在跑，轮询 ' + report.modules.feed.interval_ms + 'ms，跟踪 ' + report.modules.feed.tracked + ' 个标的' + (report.modules.feed.last_error ? '（最近错误：' + report.modules.feed.last_error + '）' : '') : '未挂载 —— ' + report.modules.feed.error))
        lines.push('· MCP 量化服务器：' + (report.modules.mcp_server.ok ? report.modules.mcp_server.path + '（' + Math.round(report.modules.mcp_server.bytes / 1024) + ' KB）' : '缺失：' + report.modules.mcp_server.path))
        for (const skill of report.modules.skills) lines.push('· 技能目录 ' + skill.dir + '：' + (skill.exists ? skill.count + ' 个技能' : '不存在'))
        for (const [key, item] of Object.entries(report.interpreters)) lines.push('· 解释器 ' + key + '：' + (item.exists ? item.path : '未找到 ' + item.path))
        lines.push('· 系统代理：' + (report.proxy === null ? '未配置（境外数据源需直连可达或自配 AIQUANT_PROXY）' : report.proxy.host + ':' + report.proxy.port + (report.proxy.reachable ? '（可连）' : '（端口不通）')))
        lines.push('· 事实库统计：自选 ' + report.stats.watchlist + ' / 持仓 ' + report.stats.positions_open + ' / 信号 ' + report.stats.signals + ' / 回测 ' + report.stats.backtests + ' / 净值点 ' + report.stats.equity_points)
        if (report.sources !== undefined) {
          lines.push('· 数据源探测：')
          for (const item of report.sources) lines.push('   - ' + item.name + '：' + (item.ok ? '通（' + item.detail + '）' : '不通 —— ' + item.detail))
        }
        return lines.join('\n')
      } catch (error) {
        return fail(error, '看 ui.js / feed.js 是否加载成功；可先用 aiquant_board 看事实库是否可用')
      }
    },
  }))

  /* ── 自选 ─────────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_watchlist',
    description: '管理看板自选列表（会立刻出现在右侧「AI量化看板」的行情页）。action=add 加入（symbol 支持 600519 / sh600519 / 000001.SZ / 00700 / us.AAPL / BTC-USDT），action=remove 移除，action=list 列出。加入后行情引擎会自动开始跟踪。',
    parameters: {
      action: { type: 'string', required: true, description: 'add | remove | list', enum: ['add', 'remove', 'list'] },
      symbol: { type: 'string', description: '标的代码（add/remove 必填）' },
      name: { type: 'string', description: '展示名称（可选，建议给中文名）' },
      kind: { type: 'string', description: 'stock | index | etf | fund | crypto | other（可选）' },
      note: { type: 'string', description: '备注（可选，例如"观察 60 日线支撑"）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args) {
      try {
        if (args.action === 'list') {
          const rows = store.watchlistList()
          return ok({ count: rows.length, watchlist: rows.map((r) => r.symbol + ' ' + (r.name || '')) })
        }
        if (typeof args.symbol !== 'string' || args.symbol.trim() === '') return fail(new Error('需要 symbol'), '例如 aiquant_watchlist action=add symbol=600519 name=贵州茅台')
        if (args.action === 'remove') return ok({ removed: store.watchlistRemove(args.symbol), watchlist_now: store.watchlistList().length })
        const row = store.watchlistAdd({ symbol: args.symbol, name: args.name, kind: args.kind, note: args.note })
        let quote = null
        if (feed !== undefined) {
          try {
            const rows = await feed.refreshQuotes([row.symbol], { force: true })
            quote = rows[0] || null
          } catch (error) { quote = { error: error.message } }
        }
        return ok({ added: row, quote: quote === null ? null : quoteLine(quote), note: '行情引擎已开始跟踪；看板「行情」页可见' })
      } catch (error) {
        return fail(error, '检查 symbol 写法：A股 600519 / sh600519 / 000001.SZ，港股 00700，美股 us.AAPL，加密 BTC-USDT')
      }
    },
  }))

  /* ── 行情 ─────────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_quote',
    description: '取实时行情（A股/ETF/指数/港股/美股/加密），并可选算技术指标（MA/MACD/RSI/BOLL/ATR/趋势/回撤）。数据落事实库、看板同步可见。symbols 省略则取自选+持仓。',
    parameters: {
      symbols: { type: 'array', description: '标的数组，如 ["600519","BTC-USDT"]；省略=自选+持仓', items: { type: 'string' } },
      indicators: { type: 'boolean', description: '是否同时返回日线技术指标（默认 false，会多花几秒拉日线）' },
      force: { type: 'boolean', description: '跳过 2 秒 TTL 强制重新取数（默认 false）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args) {
      try {
        if (feed === undefined) return fail(new Error('行情引擎未挂载'), '检查 profile 里 aiquant-mode-feed 行是否启用')
        const symbols = Array.isArray(args.symbols) && args.symbols.length > 0 ? args.symbols : undefined
        let rows = []
        let error = null
        try {
          rows = await feed.refreshQuotes(symbols, { force: args.force === true })
        } catch (err) {
          error = err.message
          rows = store.quotesGet(symbols || feed.trackedSymbols())
        }
        const lines = []
        lines.push('【实时行情】' + nowIso() + '（' + marketSession().label + '）')
        for (const row of rows) lines.push('· ' + quoteLine(row))
        if (error !== null) lines.push('⚠ 实时取数失败，以下为库内缓存：' + error)
        if (args.indicators === true) {
          lines.push('')
          lines.push('【技术指标（日线）】')
          for (const row of rows) {
            try {
              const analysis = await feed.analyzeSymbol(row.symbol)
              const ind = analysis.indicators
              if (ind === null) { lines.push('· ' + row.symbol + '：无日线数据'); continue }
              lines.push('· ' + row.symbol + '（' + ind.bars + ' 根，至 ' + ind.date + '）：收盘 ' + ind.close
                + '｜MA5 ' + fmt(ind.ma.ma5) + ' MA20 ' + fmt(ind.ma.ma20) + ' MA60 ' + fmt(ind.ma.ma60)
                + '｜RSI14 ' + fmt(ind.rsi14) + '｜MACD ' + fmt(ind.macd.dif, 3) + '/' + fmt(ind.macd.dea, 3)
                + '｜趋势 ' + ind.trend + '｜距 60 日高点 -' + fmt(ind.drawdown_from_high60_pct) + '%'
                + '｜20 日高/低 ' + ind.high20 + '/' + ind.low20)
            } catch (err) {
              lines.push('· ' + row.symbol + '：指标失败 —— ' + (err.message || String(err)))
            }
          }
        }
        return ok({ text: lines.join('\n'), count: rows.length })
      } catch (error) {
        return fail(error, '数据源可能不可达：跑 aiquant_env probe=true 看哪个源断了')
      }
    },
  }))

  /* ── 看板快照 ─────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_board',
    description: '读右侧「AI量化看板」的事实快照：组合（持仓/浮盈/权重）、自选行情、最新信号、回测记录、净值与风险指标、告警、运行日志。用户在问"现在什么情况/看板什么样"时用它回答真实数字。',
    parameters: {
      sections: { type: 'array', description: '要哪些片段，可选：totals/watchlist/signals/backtests/risk/alerts/runs（默认全部）', items: { type: 'string' } },
      signals: { type: 'number', description: '返回多少条信号（默认 20）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args) {
      try {
        const snap = store.snapshot({ signalLimit: Number(args.signals) || 20 })
        const want = Array.isArray(args.sections) && args.sections.length > 0 ? new Set(args.sections) : null
        const has = (key) => want === null || want.has(key)
        const lines = []
        lines.push('【AI量化看板】' + snap.ts + '（' + snap.config.market_session.label + '）')
        if (has('totals')) {
          lines.push('· 组合：' + snap.totals.count + ' 只持仓，市值 ' + snap.totals.market_value + '，浮盈 ' + snap.totals.pnl
            + (snap.totals.pnl_pct === null ? '' : '（' + snap.totals.pnl_pct + '%）')
            + (snap.totals.count > 0 ? '，最大单票 ' + snap.totals.max_weight + '%' + (snap.totals.concentrated ? ' ⚠ 集中' : '') : ''))
          for (const p of snap.positions) {
            lines.push('   - ' + p.symbol + ' ' + (p.name || '') + ' ' + p.qty + '股 成本 ' + p.avg_cost + ' 现价 ' + p.price
              + (p.price_stale ? '(缓存)' : '') + ' 浮盈 ' + p.pnl + (p.pnl_pct === null ? '' : '(' + p.pnl_pct + '%)') + ' 权重 ' + p.weight + '%')
          }
        }
        if (has('watchlist')) {
          lines.push('· 自选：' + snap.watchlist.length + ' 个')
          for (const w of snap.watchlist.slice(0, 20)) {
            lines.push('   - ' + (w.name || w.symbol) + ' ' + w.symbol + ' ' + (w.price === null ? '—' : w.price)
              + ' ' + (w.pct === null ? '—' : (w.pct >= 0 ? '+' : '') + w.pct + '%') + (w.stale ? '（缓存）' : ''))
          }
        }
        if (has('signals')) {
          lines.push('· 信号：库内 ' + snap.stats.signals + ' 条，最近 ' + snap.signals.length + ' 条')
          for (const s of snap.signals.slice(0, Number(args.signals) || 20)) {
            lines.push('   - [' + s.ts + '] ' + s.symbol + ' ' + (s.kind_label || s.kind) + ' ' + (s.direction || '')
              + ' 强度 ' + (s.strength === null ? '—' : s.strength) + ' @' + (s.price === null ? '—' : s.price) + ' ' + (s.reason || ''))
          }
        }
        if (has('backtests')) {
          lines.push('· 回测：' + snap.backtests.length + ' 条')
          for (const b of snap.backtests.slice(0, 10)) {
            const m = b.metrics || {}
            lines.push('   - ' + b.name + '（' + b.strategy + '，' + b.start_date + '→' + b.end_date + '）年化 '
              + (m.annual_return_pct === undefined ? '—' : m.annual_return_pct + '%') + ' 回撤 '
              + (m.max_drawdown_pct === undefined ? '—' : m.max_drawdown_pct + '%') + ' 夏普 '
              + (m.sharpe === undefined ? '—' : m.sharpe) + (b.out_of_sample ? '（含样本外）' : '（⚠ 无样本外）'))
          }
        }
        if (has('risk')) {
          const m = snap.metrics
          lines.push('· 风险：净值点 ' + m.samples + '，区间收益 ' + fmt(m.total_return_pct) + '%，最大回撤 ' + fmt(m.max_drawdown_pct)
            + '%，年化波动 ' + fmt(m.volatility_pct) + '%，夏普 ' + fmt(m.sharpe) + '，VaR95(日) ' + fmt(m.var95_pct) + '%'
            + (m.samples < 20 ? ' ⚠ 样本不足，统计量不可用' : ''))
        }
        if (has('alerts')) {
          lines.push('· 未确认告警 ' + snap.alerts.length + ' 条：' + snap.alerts.slice(0, 8).map((a) => a.level + '/' + a.title).join('；'))
        }
        if (has('runs')) {
          lines.push('· 最近运行：')
          for (const r of snap.runs.slice(0, 8)) lines.push('   - [' + r.ts + '] ' + r.kind + ' ' + r.status + ' ' + (r.detail || ''))
        }
        return ok({ text: lines.join('\n'), stats: snap.stats })
      } catch (error) {
        return fail(error)
      }
    },
  }))

  /* ── 信号 ─────────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_signal',
    description: '策略信号落库与查询。action=add 手工写一条（kind 见枚举）、action=list 查询、action=scan 让引擎按日线规则扫一遍自选（金叉/死叉/超买超卖/突破/放量/回撤预警，同一天同一逻辑自动去重）、action=clear 清空。',
    parameters: {
      action: { type: 'string', required: true, description: 'add | list | scan | clear', enum: ['add', 'list', 'scan', 'clear'] },
      symbol: { type: 'string', description: '标的（add 必填；list 可选筛选）' },
      kind: { type: 'string', description: '信号类型（add 用）：golden_cross/death_cross/rsi_oversold/rsi_overbought/breakout_high/breakdown_low/volume_spike/drawdown_alert/ma_trend_up/ma_trend_down/custom' },
      direction: { type: 'string', description: 'buy | sell（默认按 kind 推断）' },
      strength: { type: 'number', description: '强度 0-1（可选）' },
      price: { type: 'number', description: '触发价格（可选）' },
      strategy: { type: 'string', description: '策略名（可选，便于回溯）' },
      reason: { type: 'string', description: '触发依据（强烈建议写具体数字，例如 "MA5(12.3) 上穿 MA20(12.1)"）' },
      limit: { type: 'number', description: 'list 返回条数（默认 30）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args) {
      try {
        if (args.action === 'list') {
          const rows = store.signalsList({ limit: Number(args.limit) || 30, symbol: args.symbol, kind: args.kind })
          return ok({
            count: rows.length,
            signals: rows.map((s) => '[' + s.ts + '] ' + s.symbol + ' ' + (s.kind_label || s.kind) + ' ' + (s.direction || '') + ' ' + (s.reason || '')),
          })
        }
        if (args.action === 'scan') {
          if (feed === undefined) return fail(new Error('行情引擎未挂载'))
          const result = await feed.scan({ force: args.symbol !== undefined, symbols: args.symbol ? [args.symbol] : undefined })
          return ok({
            scanned: result.scanned,
            new_signals: result.signals.map((s) => s.symbol + ' ' + (SIGNAL_KIND_LABEL[s.kind] || s.kind) + '：' + s.reason),
            errors: result.errors,
          })
        }
        if (args.action === 'clear') return ok({ cleared: store.signalsClear({}) })
        if (typeof args.symbol !== 'string') return fail(new Error('add 需要 symbol'))
        const result = store.signalAdd({
          symbol: args.symbol, kind: args.kind || 'custom', direction: args.direction,
          strength: args.strength, price: args.price, strategy: args.strategy, reason: args.reason,
        })
        return ok({ id: result.id, deduped: result.deduped, note: result.deduped ? '同一天同一逻辑已存在，未重复落库（去重生效）' : '已落库，看板可见' })
      } catch (error) {
        return fail(error)
      }
    },
  }))

  /* ── 持仓 ─────────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_position',
    description: '持仓事实库：action=add 建仓（symbol/qty/avg_cost）、action=update 改数量或成本、action=close 平仓（给 price 算已实现盈亏）、action=list 列表。**只有这里落库的持仓才会进看板与风控计算**；模拟盘与实盘都要走它。',
    parameters: {
      action: { type: 'string', required: true, description: 'add | update | close | list', enum: ['add', 'update', 'close', 'list'] },
      id: { type: 'string', description: '持仓 id（update/close 必填）' },
      symbol: { type: 'string', description: '标的（add 必填）' },
      qty: { type: 'number', description: '数量（add 必填；update 可改）' },
      avg_cost: { type: 'number', description: '成本价（add 必填；update 可改）' },
      price: { type: 'number', description: '平仓价（close 必填）' },
      strategy: { type: 'string', description: '所属策略（可选，便于归因）' },
      note: { type: 'string', description: '备注（可选）' },
      status: { type: 'string', description: 'list 用：open | closed | all（默认 open）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args, exec) {
      try {
        if (args.action === 'list') {
          const rows = store.positionsList({ status: args.status || 'open' })
          return ok({
            count: rows.length,
            positions: rows.map((p) => p.id + ' | ' + p.symbol + ' ' + p.qty + ' @' + p.avg_cost + ' | ' + p.status + ' | ' + (p.strategy || '-')),
          })
        }
        if (args.action === 'add') {
          const row = store.positionAdd({
            symbol: args.symbol, qty: args.qty, avg_cost: args.avg_cost, strategy: args.strategy,
            note: args.note, source: 'tool', actor: actorOf(exec),
          })
          if (feed !== undefined) { try { await feed.refreshQuotes([row.symbol], { force: true }) } catch { /* 稍后再取 */ } }
          return ok({ position: row, board: '已进看板「持仓」页；风控指标随之更新（净值点会在 15 分钟节流后写入）' })
        }
        if (args.action === 'update') {
          if (typeof args.id !== 'string') return fail(new Error('update 需要 id'))
          return ok({ position: store.positionUpdate(args.id, { qty: args.qty, avg_cost: args.avg_cost, note: args.note, strategy: args.strategy }) })
        }
        if (args.action === 'close') {
          if (typeof args.id !== 'string') return fail(new Error('close 需要 id'))
          const row = store.positionClose(args.id, { price: args.price, note: args.note, actor: actorOf(exec) })
          return ok({ closed: row, note: '已实现盈亏 = (平仓价 - 成本价) × 数量；看板「持仓」页已移出未平仓列表' })
        }
        return fail(new Error('未知 action：' + args.action))
      } catch (error) {
        return fail(error, '建仓示例：aiquant_position action=add symbol=600519 qty=100 avg_cost=1260')
      }
    },
  }))

  /* ── 回测 ─────────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_backtest',
    description: '回测结果落库（看板「回测」页 + 报告共用）。**必须**给 cost_model（手续费/滑点/成交时点）与 out_of_sample=true 才算有效结论；缺了会被报告标注为"口径不全"。MCP 的 q_backtest_ma 算完后用这个工具落库。action=add/list/remove。',
    parameters: {
      action: { type: 'string', required: true, description: 'add | list | remove', enum: ['add', 'list', 'remove'] },
      id: { type: 'string', description: '回测 id（remove 必填）' },
      name: { type: 'string', description: '回测名称（add 必填，例如 "茅台双均线 5/20"）' },
      symbols: { type: 'array', description: '标的数组', items: { type: 'string' } },
      strategy: { type: 'string', description: '策略标识（ma/momentum/breakout/rsi_reversion/多因子名）' },
      params: { type: 'object', additionalProperties: true, description: '参数对象（如 {fast:5, slow:20}）' },
      start: { type: 'string', description: '回测起（YYYY-MM-DD）' },
      end: { type: 'string', description: '回测止（YYYY-MM-DD）' },
      metrics: { type: 'object', additionalProperties: true, description: '指标对象：annual_return_pct / max_drawdown_pct / sharpe / calmar / win_rate_pct / profit_factor / turnover_pct / trades' },
      cost_model: { type: 'string', description: '成本假设（例："fee 2.5bp + slippage 5bp + 次日开盘成交"）' },
      out_of_sample: { type: 'boolean', description: '是否含样本外结果（默认 false，报告会据此给警告）' },
      note: { type: 'string', description: '备注（过拟合红旗、数据缺口等）' },
      limit: { type: 'number', description: 'list 返回条数（默认 20）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args, exec) {
      try {
        if (args.action === 'list') {
          const rows = store.backtestsList(Number(args.limit) || 20)
          return ok({
            count: rows.length,
            backtests: rows.map((b) => b.id + ' | ' + b.name + ' | ' + b.strategy + ' | 年化 ' + (b.metrics && b.metrics.annual_return_pct) + '% | 回撤 ' + (b.metrics && b.metrics.max_drawdown_pct) + '% | 样本外 ' + (b.out_of_sample ? '是' : '否')),
          })
        }
        if (args.action === 'remove') return ok({ removed: store.backtestRemove(args.id) })
        if (typeof args.name !== 'string' || args.name === '') return fail(new Error('add 需要 name'))
        const row = store.backtestAdd({
          name: args.name, symbols: args.symbols, strategy: args.strategy, params: args.params,
          start: args.start, end: args.end, metrics: args.metrics, cost_model: args.cost_model,
          out_of_sample: args.out_of_sample === true, note: args.note, run_by: actorOf(exec),
        })
        const warnings = []
        if (!args.cost_model) warnings.push('缺少 cost_model（手续费/滑点/成交时点）—— 报告会标为口径不全')
        if (args.out_of_sample !== true) warnings.push('未标注样本外结果 —— 过拟合风险未排除')
        return ok({ backtest: { id: row.id, name: row.name }, warnings, note: '已进看板「回测」页' })
      } catch (error) {
        return fail(error)
      }
    },
  }))

  /* ── 风控 ─────────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_risk',
    description: '风控判定：按净值序列算回撤/波动/夏普/卡玛/VaR95/ES95/胜率，按持仓算集中度（最大单票、HHI），按日线算持仓间相关性，并对照红线给出"通过/不通过 + 具体越线项"。**上线前必须跑，不通过不许上实盘。**',
    parameters: {
      portfolio: { type: 'string', description: '组合名（默认 default）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args) {
      try {
        const risk = store.risk({ portfolio: args.portfolio })
        const m = risk.metrics
        const lines = []
        lines.push('【风控判定】' + (risk.passed ? '✅ 通过' : '⛔ 不通过（' + risk.breaches.length + ' 项越线）') + ' —— ' + risk.ts)
        lines.push('· 组合：' + risk.concentration.count + ' 只，最大单票 ' + risk.concentration.max_weight + '%，HHI ' + risk.concentration.hhi)
        lines.push('· 净值：' + m.samples + ' 点，区间收益 ' + fmt(m.total_return_pct) + '%，最大回撤 ' + fmt(m.max_drawdown_pct)
          + '%，当前回撤 ' + fmt(m.current_drawdown_pct) + '%，年化波动 ' + fmt(m.volatility_pct) + '%')
        lines.push('· 风险调整：夏普 ' + fmt(m.sharpe) + '，卡玛 ' + fmt(m.calmar) + '，胜率 ' + fmt(m.win_rate_pct)
          + '%，VaR95(日) ' + fmt(m.var95_pct) + '%，ES95 ' + fmt(m.es95_pct) + '%')
        lines.push('· 红线：单票 ≤ ' + risk.limits.max_single_weight + '%，回撤 ≤ ' + risk.limits.max_drawdown
          + '%，持仓数 ≥ ' + risk.limits.min_positions + '，HHI ≤ ' + risk.limits.max_hhi + '，相关性 ≤ ' + risk.limits.max_corr)
        if (risk.breaches.length > 0) {
          lines.push('· 越线项：')
          for (const b of risk.breaches) lines.push('   - ' + b.text + '（实测 ' + b.value + '，限值 ' + b.limit + '）')
        }
        if (m.samples < 20) lines.push('⚠ 净值样本 < 20：回撤/波动/夏普/VaR **不可用**，先积累净值点再判')
        if (risk.correlation.length > 0) {
          lines.push('· 相关性：')
          for (const c of risk.correlation) lines.push('   - ' + c.a + ' ~ ' + c.b + '：' + c.r + '（' + c.level + '）')
        } else {
          lines.push('· 相关性：样本不足（需要 ≥30 根日线且至少 2 只持仓）')
        }
        return ok({ text: lines.join('\n'), passed: risk.passed, breaches: risk.breaches })
      } catch (error) {
        return fail(error)
      }
    },
  }))

  /* ── 笔记 ─────────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_note',
    description: '研究笔记落库（看板「数据」页可见）：记录假设、数据口径、失败尝试、结论与反例。action=add/list。**失败尝试也要记**——避免下一轮重复劳动。',
    parameters: {
      action: { type: 'string', required: true, description: 'add | list', enum: ['add', 'list'] },
      title: { type: 'string', description: '标题（add 必填）' },
      body: { type: 'string', description: '正文（支持 markdown）' },
      kind: { type: 'string', description: 'research | idea | failure | decision | data（默认 research）' },
      symbol: { type: 'string', description: '关联标的（可选）' },
      tags: { type: 'array', description: '标签数组', items: { type: 'string' } },
      limit: { type: 'number', description: 'list 返回条数（默认 20）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args, exec) {
      try {
        if (args.action === 'list') {
          const rows = store.notesList({ limit: Number(args.limit) || 20 })
          return ok({ count: rows.length, notes: rows.map((n) => '[' + n.ts + '] ' + n.kind + ' ' + n.title) })
        }
        if (typeof args.title !== 'string' || args.title === '') return fail(new Error('add 需要 title'))
        const row = store.noteAdd({
          title: args.title, body: args.body, kind: args.kind, symbol: args.symbol,
          tags: args.tags, author: actorOf(exec),
        })
        return ok({ note: { id: row.id, title: row.title, kind: row.kind } })
      } catch (error) {
        return fail(error)
      }
    },
  }))

  /* ── 报告 ─────────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_report',
    description: '生成 AI量化日报（markdown：组合/自选/信号/回测/风险/日志），可落盘到工作区 reports/。结论口径与看板一致，不构成投资建议。save=true 时返回文件绝对路径。',
    parameters: {
      save: { type: 'boolean', description: '是否写入 <工作区>/reports/（默认 true）' },
      inline: { type: 'boolean', description: '是否在返回值里带完整 markdown（默认 false，只返回摘要与前 40 行）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args) {
      try {
        const markdown = store.reportMarkdown()
        let file = null
        if (args.save !== false) {
          const dir = join(store.workspace, 'reports')
          mkdirSync(dir, { recursive: true })
          const stamp = nowIso().slice(0, 10).replace(/-/g, '')
          file = join(dir, stamp + '-ai量化日报.md')
          writeFileSync(file, markdown, 'utf8')
        }
        const lines = markdown.split('\n')
        const head = args.inline === true ? markdown : lines.slice(0, 40).join('\n')
        return ok({ path: file, bytes: Buffer.byteLength(markdown, 'utf8'), lines: lines.length, preview: head + (args.inline === true ? '' : '\n…（完整内容见文件）') })
      } catch (error) {
        return fail(error)
      }
    },
  }))

  /* ── 告警 ─────────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_alert',
    description: '看板告警：action=add 写一条（level=info/warn/critical）、action=list 列出、action=ack 确认（id 省略=全部确认）。风控越线、数据缺失、持仓不符这类事必须写告警，不要只在对话里说。',
    parameters: {
      action: { type: 'string', required: true, description: 'add | list | ack', enum: ['add', 'list', 'ack'] },
      id: { type: 'string', description: '告警 id（ack 用；省略=全部确认）' },
      level: { type: 'string', description: 'info | warn | critical（默认 warn）' },
      title: { type: 'string', description: '标题（add 必填）' },
      body: { type: 'string', description: '详情' },
      symbol: { type: 'string', description: '关联标的（可选）' },
      limit: { type: 'number', description: 'list 返回条数（默认 30）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args) {
      try {
        if (args.action === 'list') {
          const rows = store.alertsList({ limit: Number(args.limit) || 30 })
          return ok({ count: rows.length, alerts: rows.map((a) => '[' + a.ts + '] ' + a.level + ' ' + a.title + (a.ack ? '（已确认）' : '')) })
        }
        if (args.action === 'ack') return ok({ acked: store.alertsAck(args.id) })
        if (typeof args.title !== 'string' || args.title === '') return fail(new Error('add 需要 title'))
        const row = store.alertAdd({ level: args.level, title: args.title, body: args.body, symbol: args.symbol })
        return ok({ alert: { id: row.id, level: row.level, title: row.title } })
      } catch (error) {
        return fail(error)
      }
    },
  }))

  /* ── 净值点 ───────────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_equity',
    description: '组合净值/市值点的读写：风险指标（回撤/波动/夏普/VaR）全部基于这条序列。action=add 手工写一个点（nav + 可选 pnl/cash/note），action=list 读序列。持仓存在时引擎会每 15 分钟自动记一个点。',
    parameters: {
      action: { type: 'string', required: true, description: 'add | list', enum: ['add', 'list'] },
      nav: { type: 'number', description: '净值或组合市值（add 必填，>0）' },
      pnl: { type: 'number', description: '当日/累计盈亏（可选）' },
      cash: { type: 'number', description: '现金（可选）' },
      portfolio: { type: 'string', description: '组合名（默认 default）' },
      note: { type: 'string', description: '备注（可选，例如 "dca 补仓后"）' },
      limit: { type: 'number', description: 'list 返回点数（默认 60）' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute(args) {
      try {
        if (args.action === 'list') {
          const rows = store.equityList({ portfolio: args.portfolio, limit: Number(args.limit) || 60 })
          const first = rows.length > 0 ? rows[0].nav : null
          const last = rows.length > 0 ? rows[rows.length - 1].nav : null
          return ok({
            count: rows.length,
            first, last,
            change_pct: first !== null && last !== null && first > 0 ? Number((((last / first) - 1) * 100).toFixed(2)) : null,
            tail: rows.slice(-10).map((r) => r.ts + ' ' + r.nav),
          })
        }
        if (typeof args.nav !== 'number' || !(args.nav > 0)) return fail(new Error('add 需要 >0 的 nav'))
        const row = store.equityAdd({ nav: args.nav, pnl: args.pnl, cash: args.cash, portfolio: args.portfolio, note: args.note })
        return ok({ added: row, note: '风险指标会随之更新（样本 <20 时统计量标记为不可用）' })
      } catch (error) {
        return fail(error)
      }
    },
  }))

  /* ── 数据/文件定位 ────────────────────────────────────────── */
  ctx.tools.register(defineTool({
    name: 'aiquant_paths',
    description: '返回本模式的所有关键路径与当前占用（工作区、事实库、缓存、策略、报告、日志、技能、MCP 服务器、包安装目录），用于落盘前确认写到哪里、以及排查"文件到底在哪"。',
    parameters: {},
    output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
    async execute() {
      try {
        const workspace = (mode && mode.workspace) || store.workspace
        const items = [
          ['工作区根', workspace],
          ['事实库', store.dbFile],
          ['数据缓存', join(workspace, 'data', 'cache')],
          ['导出目录', join(workspace, 'data', 'exports')],
          ['策略目录', join(workspace, 'strategies')],
          ['报告目录', join(workspace, 'reports')],
          ['日志目录', join(workspace, 'logs')],
          ['技能目录', join(workspace, 'skills')],
          ['MCP 服务器', join(workspace, 'mcp', 'quant_mcp', 'server.py')],
          ['预设副本', (mode && mode.preset && mode.preset.dir) || join(process.env.DSH_HOME || 'C:/Users/33096/.dsh', '.agent-presets', 'aiquant')],
          ['环境脚本', (mode && mode.setup && mode.setup.path) || null],
        ]
        const lines = ['【AI量化路径】']
        for (const [label, path] of items) {
          if (path === null) { lines.push('· ' + label + '：—'); continue }
          const exists = existsSync(path)
          let size = ''
          try { if (exists && statSync(path).isFile()) size = '（' + Math.round(statSync(path).size / 1024) + ' KB）' } catch { /* 忽略 */ }
          lines.push('· ' + label + '：' + path + (exists ? ' ✓' + size : ' ✗ 不存在'))
        }
        const cache = dirSize(join(workspace, 'data', 'cache'))
        lines.push('· 缓存占用：' + cache.files + ' 文件 / ' + Math.round(cache.bytes / 1024) + ' KB')
        return ok({ text: lines.join('\n') })
      } catch (error) {
        return fail(error)
      }
    },
  }))

  ctx.logger?.info?.('aiquant-tools: 已注册 12 个智能体工具（aiquant_env/quote/board/watchlist/signal/position/backtest/risk/note/report/alert/equity/paths）')
}

/** 数字格式化（null → '—'）。 */
function fmt(value, digits = 2) {
  return value === null || value === undefined ? '—' : Number(value).toFixed(digits)
}

export default { name, inject, apply }
