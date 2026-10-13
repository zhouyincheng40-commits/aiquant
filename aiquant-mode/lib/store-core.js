/**
 * AI量化事实库核心（无 Cordis 依赖）
 *
 * 这里只做四件事：工作区/元数据、SQLite 事实库（schema/写入/查询）、
 * 组合与风险计算、看板快照与报告渲染。Cordis 插件壳在 ./store.js，
 * 浏览器桥接在 ./ui.js，智能体工具在 ./tools.js，行情引擎在 ./feed.js。
 *
 * 设计铁律：
 *   · **零外部依赖**：只用 node: 内置模块（node:sqlite / node:fs / node:crypto）。
 *   · **凡展示必落库**：看板上的每个数字都来自这张库，不来自内存里的临时变量。
 *   · **可复现**：每条信号/回测/笔记都带 ts、来源与参数，报告里能指回它。
 *   · 写入一律经过本模块的方法，不散落 SQL。
 */
import { DatabaseSync } from 'node:sqlite'
import { createHash, randomUUID } from 'node:crypto'
import { mkdirSync, existsSync, statSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

/** schema 版本：结构变更时 +1，并在 migrate() 里补迁移。 */
export const SCHEMA_VERSION = 2

/** 市场分类（展示与交易日历用）。 */
export const MARKETS = ['a', 'hk', 'us', 'crypto', 'other']

/** 信号类型（看板图标与筛选用）。 */
export const SIGNAL_KINDS = [
  'golden_cross', 'death_cross', 'rsi_oversold', 'rsi_overbought',
  'breakout_high', 'breakdown_low', 'volume_spike', 'drawdown_alert',
  'ma_trend_up', 'ma_trend_down', 'custom',
]

/** 信号类型中文名。 */
export const SIGNAL_KIND_LABEL = {
  golden_cross: '金叉',
  death_cross: '死叉',
  rsi_oversold: 'RSI 超卖',
  rsi_overbought: 'RSI 超买',
  breakout_high: '突破新高',
  breakdown_low: '跌破新低',
  volume_spike: '放量异动',
  drawdown_alert: '回撤预警',
  ma_trend_up: '多头排列',
  ma_trend_down: '空头排列',
  custom: '自定义',
}

/** 回测状态。 */
export const BACKTEST_STATUS = ['planned', 'running', 'done', 'failed', 'rejected']

/** 时间戳（本地 ISO，秒级）。 */
export function nowIso(date = new Date()) {
  const pad = (n, w = 2) => String(n).padStart(w, '0')
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate())
    + 'T' + pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds())
}

/** 稳定 id：带前缀 + 短随机（人可读、可排序）。 */
export function newId(prefix) {
  return prefix + '_' + nowIso().replace(/[-:T]/g, '').slice(0, 14) + '_' + randomUUID().slice(0, 6)
}

/** 内容哈希（去重用：同一逻辑事实只落一次）。 */
export function hashOf(...parts) {
  return createHash('sha1').update(parts.join('\u0001')).digest('hex').slice(0, 16)
}

/**
 * 规范化标的代码 → 内部规范形式。
 *
 * 规范形式：
 *   · A股/ETF/指数：`sh600519` / `sz000001` / `bj430047`
 *   · 港股：`hk00700`
 *   · 美股：`usAAPL`
 *   · 加密：`binance:BTCUSDT`
 * @returns `{ symbol, market, code, raw }`
 */
export function normalizeSymbol(input) {
  const raw = String(input === undefined || input === null ? '' : input).trim()
  if (raw === '') return { symbol: '', market: 'other', code: '', raw }
  const s = raw.replace(/\s+/g, '')
  const upper = s.toUpperCase()

  /* 加密：BTCUSDT / BTC-USDT / BTC/USDT / binance:BTCUSDT */
  const cryptoPrefixMatch = /^(BINANCE|OKX|CRYPTO):(.+)$/.exec(upper)
  const cryptoBody = cryptoPrefixMatch !== null ? cryptoPrefixMatch[2] : upper
  const cryptoSplit = /^([A-Z0-9]{2,10})[-/]?(USDT|USDC|BTC|ETH|BUSD|USD)$/.exec(cryptoBody)
  if (cryptoPrefixMatch !== null || (cryptoSplit !== null && cryptoBody.includes('-')) || /^[A-Z0-9]{2,10}(USDT|USDC)$/.test(cryptoBody)) {
    const base = cryptoBody.replace(/[-/]/g, '')
    const quoteMatch = /^(.*?)(USDT|USDC|BUSD|BTC|ETH|USD)$/.exec(base)
    if (quoteMatch !== null) {
      const canon = 'binance:' + quoteMatch[1] + '/' + quoteMatch[2]
      return { symbol: canon, market: 'crypto', code: quoteMatch[1] + quoteMatch[2], raw }
    }
  }

  /* 港股：00700 / 00700.HK / hk00700 */
  const hkMatch = /^(?:HK\.?|HK)?(\d{4,5})(?:\.HK)?$/i.exec(s)
  if (hkMatch !== null && (s.toUpperCase().startsWith('HK') || s.includes('.') || hkMatch[1].length === 5)) {
    const code = hkMatch[1].padStart(5, '0')
    return { symbol: 'hk' + code, market: 'hk', code, raw }
  }

  /* 美股：us.AAPL / US.AAPL / usAAPL；纯字母 1-5 位且不是加密基（加密已在上面分流） */
  const usMatch = /^(?:US\.?|US)?([A-Z]{1,6})(?:\.US)?$/.exec(upper)
  if (usMatch !== null && (upper.startsWith('US') || /^[A-Z]{1,5}$/.test(upper))) {
    const code = usMatch[1]
    const cryptoBases = new Set(['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE', 'ADA', 'TON', 'TRX'])
    if (!cryptoBases.has(code)) return { symbol: 'us' + code, market: 'us', code, raw }
  }

  /* A股/ETF/基金/指数：600519 / sh600519 / SH.600519 / 000001.SZ / 510300 / 159915 / 000300 */
  const aPrefixed = /^(SH|SZ|BJ)\.?(\d{6})$/i.exec(s)
  const aSuffixed = /^(\d{6})\.(SH|SZ|BJ)$/i.exec(s)
  const aBare = /^(\d{6})$/.exec(s)
  if (aPrefixed !== null || aSuffixed !== null || aBare !== null) {
    const code = aPrefixed !== null ? aPrefixed[2] : aSuffixed !== null ? aSuffixed[1] : aBare[1]
    let prefix = (aPrefixed !== null ? aPrefixed[1] : aSuffixed !== null ? aSuffixed[2] : '').toLowerCase()
    if (prefix === '') prefix = guessASharePrefix(code)
    return { symbol: prefix + code, market: 'a', code, raw }
  }

  /* 兜底：原样保留，标记 other */
  return { symbol: s.toLowerCase(), market: 'other', code: s, raw }
}

/** 标的展示名（无名称时用代码）。 */
function displayName(symbol, name) {
  return name !== undefined && name !== null && String(name).trim() !== '' ? String(name) : symbol
}

/**
 * 6 位 A 股代码 → 交易所前缀。
 *
 * 规则（交易所实际段位）：
 *   · 沪市：60x/68x 股票、9xx B股、5xx 基金/ETF、11x/13x 可转债、000xxx 的**沪市指数**
 *   · 深市：000/001/002/003/004 股票、30x 创业板、15x/16x/18x 基金、399xxx 指数
 *   · 北交所：43x/83x/87x/88x/920
 * `000xxx` 两义（深市个股 vs 沪市指数）：默认按**深市个股**（交易场景更常见），
 * 已知沪市指数走白名单；要看上证指数请显式写 `sh000001`。
 */
export function guessASharePrefix(code) {
  const c = String(code)
  const shIndexes = new Set(['000009', '000010', '000015', '000016', '000300', '000688', '000852', '000903', '000905', '000906'])
  if (shIndexes.has(c)) return 'sh'
  if (/^(60|68|9|5|11|13)/.test(c)) return 'sh'
  if (/^(43|83|87|88|92|4|8)/.test(c)) return 'bj'
  if (/^(000|001|002|003|004|30|15|16|18|39)/.test(c)) return 'sz'
  return 'sh'
}

const DDL = [
  `CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)`,
  `CREATE TABLE IF NOT EXISTS watchlist (
     symbol TEXT PRIMARY KEY, name TEXT, market TEXT, kind TEXT, note TEXT,
     sort INTEGER DEFAULT 0, added_at TEXT
   )`,
  `CREATE TABLE IF NOT EXISTS quotes (
     symbol TEXT PRIMARY KEY, ts TEXT, name TEXT, market TEXT,
     price REAL, prev_close REAL, "change" REAL, pct REAL,
     open REAL, high REAL, low REAL, volume REAL, amount REAL,
     source TEXT, session TEXT, stale INTEGER DEFAULT 0
   )`,
  `CREATE TABLE IF NOT EXISTS quote_history (
     symbol TEXT, ts TEXT, price REAL, PRIMARY KEY (symbol, ts)
   )`,
  `CREATE TABLE IF NOT EXISTS klines (
     symbol TEXT, period TEXT, date TEXT, open REAL, high REAL, low REAL,
     close REAL, volume REAL, PRIMARY KEY (symbol, period, date)
   )`,
  `CREATE TABLE IF NOT EXISTS positions (
     id TEXT PRIMARY KEY, symbol TEXT, name TEXT, market TEXT,
     qty REAL, avg_cost REAL, opened_at TEXT, closed_at TEXT, close_price REAL,
     strategy TEXT, note TEXT, status TEXT, source TEXT
   )`,
  `CREATE TABLE IF NOT EXISTS trades (
     id TEXT PRIMARY KEY, position_id TEXT, symbol TEXT, side TEXT, qty REAL,
     price REAL, fee REAL, traded_at TEXT, note TEXT
   )`,
  `CREATE TABLE IF NOT EXISTS signals (
     id TEXT PRIMARY KEY, ts TEXT, symbol TEXT, name TEXT, market TEXT,
     kind TEXT, direction TEXT, strength REAL, price REAL, strategy TEXT,
     reason TEXT, dedup_key TEXT UNIQUE, meta TEXT
   )`,
  `CREATE TABLE IF NOT EXISTS backtests (
     id TEXT PRIMARY KEY, ts TEXT, name TEXT, symbols TEXT, strategy TEXT,
     params TEXT, start_date TEXT, end_date TEXT, metrics TEXT, equity TEXT,
     status TEXT, cost_model TEXT, out_of_sample INTEGER, note TEXT, run_by TEXT
   )`,
  `CREATE TABLE IF NOT EXISTS equity (
     id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, portfolio TEXT, nav REAL,
     pnl REAL, cash REAL, note TEXT
   )`,
  `CREATE TABLE IF NOT EXISTS notes (
     id TEXT PRIMARY KEY, ts TEXT, kind TEXT, title TEXT, body TEXT,
     tags TEXT, symbol TEXT, author TEXT
   )`,
  `CREATE TABLE IF NOT EXISTS alerts (
     id TEXT PRIMARY KEY, ts TEXT, level TEXT, symbol TEXT, title TEXT,
     body TEXT, ack INTEGER DEFAULT 0
   )`,
  `CREATE TABLE IF NOT EXISTS runs (
     id TEXT PRIMARY KEY, ts TEXT, kind TEXT, status TEXT, detail TEXT, actor TEXT
   )`,
  `CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts DESC)`,
  `CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, ts DESC)`,
  `CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity (portfolio, ts)`,
  `CREATE INDEX IF NOT EXISTS idx_klines_symbol ON klines (symbol, period, date)`,
  `CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs (ts DESC)`,
]

/** 事实库。 */
export class QuantStore {
  /**
   * @param root - 数据根目录（$DSH_HOME/aiquant）。
   * @param options - `{ workspace?: string, dbFile?: string }`。
   */
  constructor(root, options = {}) {
    this.root = root
    this.workspace = options.workspace || process.env.AIQUANT_HOME || 'D:/量化'
    this.dbFile = options.dbFile || join(root, 'aiquant.db')
    mkdirSync(root, { recursive: true })
    this.db = new DatabaseSync(this.dbFile)
    this.db.exec('PRAGMA journal_mode = WAL')
    this.db.exec('PRAGMA synchronous = NORMAL')
    this.db.exec('PRAGMA busy_timeout = 5000')
    for (const stmt of DDL) this.db.exec(stmt)
    const current = Number(this.getMeta('schema_version') || 0)
    if (current < SCHEMA_VERSION) {
      this.migrate(current)
      this.setMeta('schema_version', String(SCHEMA_VERSION))
    }
    if (this.getMeta('created_at') === undefined) this.setMeta('created_at', nowIso())
    this.setMeta('workspace', this.workspace)
    this.ensureWatchlistDefaults()
  }

  /** 结构迁移（幂等）。 */
  migrate(from) {
    if (from < 2) {
      /* v2：quotes.session / backtests.cost_model / signals.meta 后加的列，旧库补上 */
      const cols = (table) => {
        try { return this.db.prepare('PRAGMA table_info(' + table + ')').all().map((r) => String(r.name)) } catch { return [] }
      }
      const add = (table, name, decl) => {
        if (!cols(table).includes(name)) {
          try { this.db.exec('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' ' + decl) } catch { /* 已存在 */ }
        }
      }
      add('quotes', 'session', 'TEXT')
      add('backtests', 'cost_model', 'TEXT')
      add('signals', 'meta', 'TEXT')
      add('positions', 'source', 'TEXT')
    }
  }

  /** 关闭（fiber 卸载时调用）。 */
  close() {
    try { this.db.close() } catch { /* 已关闭 */ }
  }

  /* ────────────────────────────── 元数据 ────────────────────────────── */

  getMeta(key) {
    const row = this.db.prepare('SELECT value FROM meta WHERE key = ?').get(key)
    return row === undefined ? undefined : String(row.value)
  }

  setMeta(key, value) {
    this.db.prepare('INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
      .run(key, String(value))
    return String(value)
  }

  /** 库统计（面板与自检用）。 */
  stats() {
    const count = (table) => {
      try { return Number(this.db.prepare('SELECT COUNT(*) AS n FROM ' + table).get().n) } catch { return 0 }
    }
    return {
      db: this.dbFile,
      schema_version: Number(this.getMeta('schema_version') || 0),
      workspace: this.workspace,
      watchlist: count('watchlist'),
      quotes: count('quotes'),
      klines: count('klines'),
      positions_open: Number(this.db.prepare("SELECT COUNT(*) AS n FROM positions WHERE status = 'open'").get().n),
      positions_all: count('positions'),
      signals: count('signals'),
      backtests: count('backtests'),
      notes: count('notes'),
      alerts_open: Number(this.db.prepare('SELECT COUNT(*) AS n FROM alerts WHERE ack = 0').get().n),
      equity_points: count('equity'),
      runs: count('runs'),
      db_bytes: (() => { try { return statSync(this.dbFile).size } catch { return 0 } })(),
    }
  }

  /* ────────────────────────────── 自选 ────────────────────────────── */

  /** 首次启动给一份默认自选（大盘指数 + 龙头 + 加密），只做一次。 */
  ensureWatchlistDefaults() {
    if (this.getMeta('watchlist_seeded') === '1') return
    const seeded = this.watchlistList()
    if (seeded.length === 0) {
      const defaults = [
        { symbol: 'sh000001', name: '上证指数', kind: 'index' },
        { symbol: 'sz399001', name: '深证成指', kind: 'index' },
        { symbol: 'sz399006', name: '创业板指', kind: 'index' },
        { symbol: 'sh000300', name: '沪深300', kind: 'index' },
        { symbol: 'sh600519', name: '贵州茅台', kind: 'stock' },
        { symbol: 'sz300750', name: '宁德时代', kind: 'stock' },
        { symbol: 'sh601318', name: '中国平安', kind: 'stock' },
        { symbol: 'binance:BTC/USDT', name: 'BTC/USDT', kind: 'crypto' },
      ]
      for (const item of defaults) {
        const norm = normalizeSymbol(item.symbol)
        this.watchlistAdd({ symbol: norm.symbol, name: item.name, market: norm.market, kind: item.kind })
      }
    }
    this.setMeta('watchlist_seeded', '1')
  }

  /** 加入自选（幂等：已存在则更新名称/备注）。 */
  watchlistAdd(input = {}) {
    const norm = normalizeSymbol(input.symbol)
    if (norm.symbol === '') throw new Error('watchlist.add 需要 symbol')
    const name = displayName(norm.symbol, input.name)
    const kind = typeof input.kind === 'string' && input.kind !== '' ? input.kind : (norm.market === 'crypto' ? 'crypto' : norm.market === 'a' ? 'stock' : 'other')
    const sort = Number.isFinite(Number(input.sort)) ? Number(input.sort) : 0
    this.db.prepare(
      `INSERT INTO watchlist (symbol, name, market, kind, note, sort, added_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(symbol) DO UPDATE SET
         name = COALESCE(NULLIF(excluded.name, ''), watchlist.name),
         note = COALESCE(excluded.note, watchlist.note),
         kind = excluded.kind`,
    ).run(norm.symbol, name, norm.market, kind, input.note === undefined ? null : String(input.note), sort, nowIso())
    return this.watchlistGet(norm.symbol)
  }

  watchlistGet(symbol) {
    const norm = normalizeSymbol(symbol)
    const row = this.db.prepare('SELECT * FROM watchlist WHERE symbol = ?').get(norm.symbol)
    return row === undefined ? null : row
  }

  watchlistRemove(symbol) {
    const norm = normalizeSymbol(symbol)
    const info = this.db.prepare('DELETE FROM watchlist WHERE symbol = ?').run(norm.symbol)
    return { removed: Number(info.changes) > 0, symbol: norm.symbol }
  }

  watchlistList() {
    return this.db.prepare('SELECT * FROM watchlist ORDER BY sort ASC, added_at ASC').all().map((row) => ({ ...row }))
  }

  /* ────────────────────────────── 行情 ────────────────────────────── */

  /** 批量写入行情快照（供看板读取）。 */
  quotesUpsert(rows) {
    if (!Array.isArray(rows) || rows.length === 0) return 0
    const stmt = this.db.prepare(
      `INSERT INTO quotes (symbol, ts, name, market, price, prev_close, "change", pct,
                           open, high, low, volume, amount, source, session, stale)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(symbol) DO UPDATE SET
         ts = excluded.ts, name = COALESCE(NULLIF(excluded.name, ''), quotes.name),
         market = excluded.market, price = excluded.price, prev_close = excluded.prev_close,
         "change" = excluded."change", pct = excluded.pct, open = excluded.open,
         high = excluded.high, low = excluded.low, volume = excluded.volume,
         amount = excluded.amount, source = excluded.source, session = excluded.session,
         stale = excluded.stale`,
    )
    let n = 0
    for (const row of rows) {
      const norm = normalizeSymbol(row.symbol)
      if (norm.symbol === '') continue
      stmt.run(
        norm.symbol, row.ts || nowIso(), row.name || null, row.market || norm.market,
        num(row.price), num(row.prev_close), num(row.change), num(row.pct),
        num(row.open), num(row.high), num(row.low), num(row.volume), num(row.amount),
        row.source || null, row.session || null, row.stale === true ? 1 : 0,
      )
      n += 1
    }
    return n
  }

  quotesGet(symbols) {
    if (!Array.isArray(symbols) || symbols.length === 0) return []
    const list = symbols.map((s) => normalizeSymbol(s).symbol).filter((s) => s !== '')
    if (list.length === 0) return []
    const placeholders = list.map(() => '?').join(',')
    return this.db.prepare('SELECT * FROM quotes WHERE symbol IN (' + placeholders + ')').all(...list).map((r) => ({ ...r }))
  }

  quotesAll() {
    return this.db.prepare('SELECT * FROM quotes ORDER BY symbol').all().map((r) => ({ ...r }))
  }

  /** 把一次报价追加进迷你走势历史（去重到分钟级）。 */
  quoteHistoryPush(symbol, price, ts) {
    const norm = normalizeSymbol(symbol)
    if (norm.symbol === '' || !Number.isFinite(Number(price))) return false
    const stamp = (ts || nowIso()).slice(0, 16)
    this.db.prepare('INSERT OR REPLACE INTO quote_history (symbol, ts, price) VALUES (?, ?, ?)')
      .run(norm.symbol, stamp, Number(price))
    /* 只保留最近 480 个点，防库无限膨胀 */
    this.db.prepare(
      `DELETE FROM quote_history WHERE symbol = ? AND ts NOT IN (
         SELECT ts FROM quote_history WHERE symbol = ? ORDER BY ts DESC LIMIT 480)`,
    ).run(norm.symbol, norm.symbol)
    return true
  }

  quoteHistory(symbol, limit = 120) {
    const norm = normalizeSymbol(symbol)
    const rows = this.db.prepare('SELECT ts, price FROM quote_history WHERE symbol = ? ORDER BY ts DESC LIMIT ?')
      .all(norm.symbol, Math.max(1, Number(limit) || 120))
    return rows.reverse().map((r) => ({ ts: r.ts, price: Number(r.price) }))
  }

  /* ────────────────────────────── K线 ────────────────────────────── */

  klinesUpsert(symbol, period, rows) {
    const norm = normalizeSymbol(symbol)
    const stmt = this.db.prepare(
      `INSERT INTO klines (symbol, period, date, open, high, low, close, volume)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(symbol, period, date) DO UPDATE SET
         open = excluded.open, high = excluded.high, low = excluded.low,
         close = excluded.close, volume = excluded.volume`,
    )
    let n = 0
    for (const row of rows || []) {
      if (row === undefined || row === null || row.date === undefined) continue
      stmt.run(norm.symbol, period || 'daily', String(row.date), num(row.open), num(row.high), num(row.low), num(row.close), num(row.volume))
      n += 1
    }
    if (n > 0) this.setMeta('klines:' + norm.symbol + ':' + (period || 'daily') + ':at', nowIso())
    return n
  }

  klinesList(symbol, period = 'daily', limit = 260) {
    const norm = normalizeSymbol(symbol)
    const rows = this.db.prepare(
      'SELECT date, open, high, low, close, volume FROM klines WHERE symbol = ? AND period = ? ORDER BY date DESC LIMIT ?',
    ).all(norm.symbol, period, Math.max(1, Number(limit) || 260))
    return rows.reverse().map((r) => ({
      date: r.date, open: Number(r.open), high: Number(r.high), low: Number(r.low),
      close: Number(r.close), volume: Number(r.volume),
    }))
  }

  klinesFetchedAt(symbol, period = 'daily') {
    return this.getMeta('klines:' + normalizeSymbol(symbol).symbol + ':' + period + ':at')
  }

  /* ────────────────────────────── 持仓与成交 ────────────────────────────── */

  positionAdd(input = {}) {
    const norm = normalizeSymbol(input.symbol)
    if (norm.symbol === '') throw new Error('position.add 需要 symbol')
    const qty = num(input.qty)
    const avgCost = num(input.avg_cost !== undefined ? input.avg_cost : input.price)
    if (!(qty > 0)) throw new Error('position.add 需要正数 qty')
    if (!(avgCost > 0)) throw new Error('position.add 需要正数 avg_cost')
    const id = input.id || newId('pos')
    this.db.prepare(
      `INSERT INTO positions (id, symbol, name, market, qty, avg_cost, opened_at, strategy, note, status, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)`,
    ).run(
      id, norm.symbol, displayName(norm.symbol, input.name), norm.market, qty, avgCost,
      input.opened_at || nowIso(), input.strategy || null, input.note === undefined ? null : String(input.note),
      input.source || 'agent',
    )
    this.runLog({ kind: 'position', status: 'open', detail: '建仓 ' + norm.symbol + ' x' + qty + ' @' + avgCost, actor: input.actor || 'agent' })
    return this.positionGet(id)
  }

  positionGet(id) {
    const row = this.db.prepare('SELECT * FROM positions WHERE id = ?').get(String(id))
    return row === undefined ? null : { ...row }
  }

  positionUpdate(id, patch = {}) {
    const current = this.positionGet(id)
    if (current === null) throw new Error('持仓不存在：' + id)
    const qty = patch.qty === undefined ? current.qty : num(patch.qty)
    const avgCost = patch.avg_cost === undefined ? current.avg_cost : num(patch.avg_cost)
    this.db.prepare('UPDATE positions SET qty = ?, avg_cost = ?, name = ?, strategy = ?, note = ? WHERE id = ?')
      .run(qty, avgCost,
        patch.name === undefined ? current.name : displayName(current.symbol, patch.name),
        patch.strategy === undefined ? current.strategy : patch.strategy,
        patch.note === undefined ? current.note : String(patch.note), String(id))
    return this.positionGet(id)
  }

  positionClose(id, input = {}) {
    const current = this.positionGet(id)
    if (current === null) throw new Error('持仓不存在：' + id)
    const price = num(input.price)
    this.db.prepare("UPDATE positions SET status = 'closed', closed_at = ?, close_price = ? WHERE id = ?")
      .run(input.closed_at || nowIso(), price, String(id))
    this.tradesAdd({
      position_id: id, symbol: current.symbol, side: 'sell',
      qty: input.qty === undefined ? current.qty : num(input.qty), price,
      fee: input.fee, note: input.note || '平仓',
    })
    this.runLog({ kind: 'position', status: 'closed', detail: '平仓 ' + current.symbol + ' @' + price, actor: input.actor || 'agent' })
    const closed = this.positionGet(id)
    const pnl = (price - current.avg_cost) * (current.qty || 0)
    return { ...closed, realized_pnl: round(pnl, 2), realized_pct: current.avg_cost > 0 ? round((price / current.avg_cost - 1) * 100, 2) : null }
  }

  positionsList(options = {}) {
    const status = options.status || 'open'
    const rows = status === 'all'
      ? this.db.prepare('SELECT * FROM positions ORDER BY opened_at DESC').all()
      : this.db.prepare('SELECT * FROM positions WHERE status = ? ORDER BY opened_at DESC').all(status)
    return rows.map((r) => ({ ...r }))
  }

  tradesAdd(input = {}) {
    const id = input.id || newId('trd')
    this.db.prepare(
      'INSERT INTO trades (id, position_id, symbol, side, qty, price, fee, traded_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
    ).run(id, input.position_id || null, normalizeSymbol(input.symbol).symbol, input.side || 'buy',
      num(input.qty), num(input.price), num(input.fee), input.traded_at || nowIso(),
      input.note === undefined ? null : String(input.note))
    return { id }
  }

  tradesList(limit = 100) {
    return this.db.prepare('SELECT * FROM trades ORDER BY traded_at DESC LIMIT ?').all(Math.max(1, Number(limit) || 100)).map((r) => ({ ...r }))
  }

  /* ────────────────────────────── 信号 ────────────────────────────── */

  /**
   * 落一条信号。`dedup_key` 唯一约束保证同一天同一逻辑信号只落一次。
   * @returns `{ id, deduped: boolean, signal }`
   */
  signalAdd(input = {}) {
    const norm = normalizeSymbol(input.symbol)
    if (norm.symbol === '') throw new Error('signal.add 需要 symbol')
    const kind = SIGNAL_KINDS.includes(input.kind) ? input.kind : 'custom'
    const ts = input.ts || nowIso()
    const dedup = input.dedup_key || [norm.symbol, kind, ts.slice(0, 10), input.strategy || ''].join('|')
    const existing = this.db.prepare('SELECT * FROM signals WHERE dedup_key = ?').get(dedup)
    if (existing !== undefined) return { id: String(existing.id), deduped: true, signal: { ...existing } }
    const id = input.id || newId('sig')
    this.db.prepare(
      `INSERT INTO signals (id, ts, symbol, name, market, kind, direction, strength, price, strategy, reason, dedup_key, meta)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      id, ts, norm.symbol, displayName(norm.symbol, input.name), norm.market, kind,
      input.direction || (kind === 'death_cross' || kind === 'breakdown_low' || kind === 'rsi_overbought' || kind === 'ma_trend_down' || kind === 'drawdown_alert' ? 'sell' : 'buy'),
      num(input.strength), num(input.price), input.strategy || null,
      input.reason === undefined ? null : String(input.reason), dedup,
      input.meta === undefined ? null : JSON.stringify(input.meta),
    )
    return { id, deduped: false, signal: this.db.prepare('SELECT * FROM signals WHERE id = ?').get(id) }
  }

  signalsList(options = {}) {
    const limit = Math.max(1, Number(options.limit) || 100)
    const where = []
    const params = []
    if (options.symbol) { where.push('symbol = ?'); params.push(normalizeSymbol(options.symbol).symbol) }
    if (options.since) { where.push('ts >= ?'); params.push(String(options.since)) }
    if (options.kind) { where.push('kind = ?'); params.push(String(options.kind)) }
    const sql = 'SELECT * FROM signals' + (where.length > 0 ? ' WHERE ' + where.join(' AND ') : '') + ' ORDER BY ts DESC LIMIT ?'
    return this.db.prepare(sql).all(...params, limit).map((r) => ({ ...r, kind_label: SIGNAL_KIND_LABEL[r.kind] || r.kind, meta: parseJson(r.meta) }))
  }

  signalsClear(options = {}) {
    if (options.before) {
      const info = this.db.prepare('DELETE FROM signals WHERE ts < ?').run(String(options.before))
      return { removed: Number(info.changes) }
    }
    const info = this.db.prepare('DELETE FROM signals').run()
    return { removed: Number(info.changes) }
  }

  /* ────────────────────────────── 回测 ────────────────────────────── */

  backtestAdd(input = {}) {
    const id = input.id || newId('bt')
    const symbols = Array.isArray(input.symbols) ? input.symbols.map((s) => normalizeSymbol(s).symbol) : []
    this.db.prepare(
      `INSERT INTO backtests (id, ts, name, symbols, strategy, params, start_date, end_date, metrics, equity, status, cost_model, out_of_sample, note, run_by)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      id, input.ts || nowIso(), input.name || '未命名回测', JSON.stringify(symbols),
      input.strategy || null, jsonOrNull(input.params), input.start || null, input.end || null,
      jsonOrNull(input.metrics), jsonOrNull(input.equity), BACKTEST_STATUS.includes(input.status) ? input.status : 'done',
      input.cost_model || null, input.out_of_sample === true ? 1 : 0,
      input.note === undefined ? null : String(input.note), input.run_by || 'agent',
    )
    return this.backtestGet(id)
  }

  backtestGet(id) {
    const row = this.db.prepare('SELECT * FROM backtests WHERE id = ?').get(String(id))
    if (row === undefined) return null
    return {
      ...row,
      symbols: parseJson(row.symbols) || [],
      params: parseJson(row.params),
      metrics: parseJson(row.metrics),
      equity: parseJson(row.equity),
    }
  }

  backtestsList(limit = 50) {
    return this.db.prepare('SELECT * FROM backtests ORDER BY ts DESC LIMIT ?').all(Math.max(1, Number(limit) || 50)).map((r) => ({
      ...r,
      symbols: parseJson(r.symbols) || [],
      params: parseJson(r.params),
      metrics: parseJson(r.metrics),
      equity: parseJson(r.equity),
    }))
  }

  backtestRemove(id) {
    const info = this.db.prepare('DELETE FROM backtests WHERE id = ?').run(String(id))
    return { removed: Number(info.changes) > 0 }
  }

  /* ────────────────────────────── 净值 ────────────────────────────── */

  equityAdd(input = {}) {
    this.db.prepare('INSERT INTO equity (ts, portfolio, nav, pnl, cash, note) VALUES (?, ?, ?, ?, ?, ?)')
      .run(input.ts || nowIso(), input.portfolio || 'default', num(input.nav), num(input.pnl), num(input.cash),
        input.note === undefined ? null : String(input.note))
    return { ts: input.ts || nowIso() }
  }

  equityList(options = {}) {
    const portfolio = options.portfolio || 'default'
    const limit = Math.max(2, Number(options.limit) || 400)
    const rows = this.db.prepare('SELECT ts, nav, pnl FROM equity WHERE portfolio = ? ORDER BY ts DESC LIMIT ?')
      .all(portfolio, limit)
    return rows.reverse().map((r) => ({ ts: r.ts, nav: Number(r.nav), pnl: Number(r.pnl) }))
  }

  /* ────────────────────────────── 笔记 / 告警 / 日志 ────────────────────────────── */

  noteAdd(input = {}) {
    const id = input.id || newId('note')
    this.db.prepare('INSERT INTO notes (id, ts, kind, title, body, tags, symbol, author) VALUES (?, ?, ?, ?, ?, ?, ?, ?)')
      .run(id, input.ts || nowIso(), input.kind || 'research', input.title || '(无标题)',
        input.body === undefined ? '' : String(input.body),
        Array.isArray(input.tags) ? input.tags.join(',') : (input.tags === undefined ? null : String(input.tags)),
        input.symbol ? normalizeSymbol(input.symbol).symbol : null, input.author || 'agent')
    return this.db.prepare('SELECT * FROM notes WHERE id = ?').get(id)
  }

  notesList(options = {}) {
    const limit = Math.max(1, Number(options.limit) || 50)
    const rows = options.kind
      ? this.db.prepare('SELECT * FROM notes WHERE kind = ? ORDER BY ts DESC LIMIT ?').all(String(options.kind), limit)
      : this.db.prepare('SELECT * FROM notes ORDER BY ts DESC LIMIT ?').all(limit)
    return rows.map((r) => ({ ...r }))
  }

  alertAdd(input = {}) {
    const id = input.id || newId('alert')
    this.db.prepare('INSERT INTO alerts (id, ts, level, symbol, title, body, ack) VALUES (?, ?, ?, ?, ?, ?, 0)')
      .run(id, input.ts || nowIso(), input.level || 'warn',
        input.symbol ? normalizeSymbol(input.symbol).symbol : null,
        input.title || '告警', input.body === undefined ? null : String(input.body))
    return this.db.prepare('SELECT * FROM alerts WHERE id = ?').get(id)
  }

  alertsList(options = {}) {
    const limit = Math.max(1, Number(options.limit) || 50)
    const onlyOpen = options.ack !== 'all'
    const rows = onlyOpen
      ? this.db.prepare('SELECT * FROM alerts WHERE ack = 0 ORDER BY ts DESC LIMIT ?').all(limit)
      : this.db.prepare('SELECT * FROM alerts ORDER BY ts DESC LIMIT ?').all(limit)
    return rows.map((r) => ({ ...r }))
  }

  alertsAck(id) {
    if (id === undefined || id === null || id === 'all') {
      const info = this.db.prepare('UPDATE alerts SET ack = 1 WHERE ack = 0').run()
      return { acked: Number(info.changes) }
    }
    const info = this.db.prepare('UPDATE alerts SET ack = 1 WHERE id = ?').run(String(id))
    return { acked: Number(info.changes) }
  }

  runLog(input = {}) {
    const id = newId('run')
    this.db.prepare('INSERT INTO runs (id, ts, kind, status, detail, actor) VALUES (?, ?, ?, ?, ?, ?)')
      .run(id, input.ts || nowIso(), input.kind || 'task', input.status || 'ok',
        input.detail === undefined ? null : String(input.detail).slice(0, 2000), input.actor || 'agent')
    /* 只保留最近 500 条 */
    this.db.prepare('DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY ts DESC LIMIT 500)').run()
    return { id }
  }

  runsList(limit = 60) {
    return this.db.prepare('SELECT * FROM runs ORDER BY ts DESC LIMIT ?').all(Math.max(1, Number(limit) || 60)).map((r) => ({ ...r }))
  }

  /* ────────────────────────────── 组合与风险 ────────────────────────────── */

  /**
   * 组合视图：把持仓与最新报价合起来算浮盈、权重。
   * @param quotes - 可选的报价覆盖（symbol → 行）；缺省读库里的快照。
   */
  portfolio(quotes) {
    const positions = this.positionsList({ status: 'open' })
    const quoteMap = new Map()
    if (quotes instanceof Map) {
      for (const [k, v] of quotes) quoteMap.set(normalizeSymbol(k).symbol, v)
    } else {
      for (const row of this.quotesAll()) quoteMap.set(row.symbol, row)
    }
    const rows = []
    let cost = 0
    let value = 0
    for (const p of positions) {
      const q = quoteMap.get(p.symbol)
      const price = q !== undefined && Number(q.price) > 0 ? Number(q.price) : Number(p.avg_cost)
      const stale = q === undefined || Number(q.stale) === 1
      const marketValue = price * Number(p.qty)
      const costValue = Number(p.avg_cost) * Number(p.qty)
      const pnl = marketValue - costValue
      cost += costValue
      value += marketValue
      rows.push({
        id: p.id, symbol: p.symbol, name: p.name, market: p.market, qty: Number(p.qty),
        avg_cost: Number(p.avg_cost), price, price_stale: stale,
        market_value: round(marketValue, 2), cost_value: round(costValue, 2),
        pnl: round(pnl, 2), pnl_pct: costValue > 0 ? round((pnl / costValue) * 100, 2) : null,
        day_pct: q !== undefined ? (q.pct === null || q.pct === undefined ? null : Number(q.pct)) : null,
        strategy: p.strategy, opened_at: p.opened_at,
      })
    }
    for (const row of rows) row.weight = value > 0 ? round((row.market_value / value) * 100, 2) : 0
    const pnl = value - cost
    const sorted = [...rows].sort((a, b) => b.market_value - a.market_value)
    const hhi = value > 0 ? round(rows.reduce((acc, r) => acc + Math.pow(r.market_value / value, 2), 0) * 10000, 1) : 0
    return {
      positions: rows,
      positions_sorted: sorted,
      totals: {
        count: rows.length,
        cost: round(cost, 2),
        market_value: round(value, 2),
        pnl: round(pnl, 2),
        pnl_pct: cost > 0 ? round((pnl / cost) * 100, 2) : null,
        max_weight: rows.length > 0 ? Math.max(...rows.map((r) => r.weight)) : 0,
        top_symbol: rows.length > 0 ? sorted[0].symbol : null,
        hhi,
        concentrated: rows.length > 0 && (Math.max(...rows.map((r) => r.weight)) > 40 || hhi > 3000),
      },
      ts: nowIso(),
    }
  }

  /**
   * 风控指标：基于净值序列（回撤/波动/夏普/VaR）+ 持仓（集中度）+ 日线相关性。
   */
  risk(options = {}) {
    const portfolio = options.portfolio || 'default'
    const equity = this.equityList({ portfolio, limit: 400 })
    const navs = equity.map((e) => e.nav).filter((n) => Number.isFinite(n) && n > 0)
    const returns = []
    for (let i = 1; i < navs.length; i += 1) returns.push(navs[i] / navs[i - 1] - 1)
    const metrics = riskMetrics(navs, returns)
    const positions = this.portfolio()
    /* 相关性矩阵：用库里的日线收盘算（最多 8 个标的，样本 ≥ 30 才给结论） */
    const corrSymbols = positions.positions_sorted.slice(0, 8).map((p) => p.symbol)
    const series = new Map()
    for (const symbol of corrSymbols) {
      const klines = this.klinesList(symbol, 'daily', 120)
      if (klines.length < 30) continue
      const closes = klines.map((k) => k.close)
      const rets = []
      for (let i = 1; i < closes.length; i += 1) if (closes[i - 1] > 0) rets.push(closes[i] / closes[i - 1] - 1)
      series.set(symbol, rets)
    }
    const corr = []
    const keys = Array.from(series.keys())
    for (let i = 0; i < keys.length; i += 1) {
      for (let j = i + 1; j < keys.length; j += 1) {
        const r = pearson(series.get(keys[i]), series.get(keys[j]))
        if (r !== null) corr.push({ a: keys[i], b: keys[j], r: round(r, 3), level: Math.abs(r) >= 0.8 ? 'high' : Math.abs(r) >= 0.5 ? 'mid' : 'low' })
      }
    }
    /* 红线判定：任何一条不通过就 blocking */
    const limits = {
      max_single_weight: 40,
      max_drawdown: 25,
      min_positions: 3,
      max_hhi: 3000,
      max_corr: 0.8,
    }
    const breaches = []
    if (positions.totals.count > 0 && positions.totals.max_weight > limits.max_single_weight) {
      breaches.push({ rule: 'max_single_weight', value: positions.totals.max_weight, limit: limits.max_single_weight, text: '单票权重超上限' })
    }
    if (metrics.max_drawdown_pct !== null && metrics.max_drawdown_pct > limits.max_drawdown) {
      breaches.push({ rule: 'max_drawdown', value: metrics.max_drawdown_pct, limit: limits.max_drawdown, text: '历史最大回撤超限' })
    }
    if (positions.totals.count > 0 && positions.totals.count < limits.min_positions) {
      breaches.push({ rule: 'min_positions', value: positions.totals.count, limit: limits.min_positions, text: '持仓过于集中（数量不足）' })
    }
    if (positions.totals.hhi > limits.max_hhi) {
      breaches.push({ rule: 'max_hhi', value: positions.totals.hhi, limit: limits.max_hhi, text: '集中度 HHI 超限' })
    }
    for (const row of corr) {
      if (row.level === 'high') breaches.push({ rule: 'max_corr', value: row.r, limit: limits.max_corr, text: row.a + ' 与 ' + row.b + ' 高度相关' })
    }
    return {
      portfolio,
      nav_points: navs.length,
      metrics,
      limits,
      breaches,
      passed: breaches.length === 0,
      concentration: {
        max_weight: positions.totals.max_weight,
        hhi: positions.totals.hhi,
        count: positions.totals.count,
      },
      correlation: corr,
      var: metrics.var95,
      ts: nowIso(),
    }
  }

  /**
   * 看板快照：一次把界面要的东西全给出来（少轮询、少往返）。
   */
  snapshot(options = {}) {
    const quoteRows = this.quotesAll()
    const quoteMap = new Map(quoteRows.map((r) => [r.symbol, r]))
    const watchlist = this.watchlistList().map((w) => {
      const q = quoteMap.get(w.symbol)
      return {
        ...w,
        price: q === undefined ? null : Number(q.price),
        pct: q === undefined ? null : Number(q.pct),
        change: q === undefined ? null : Number(q.change),
        high: q === undefined ? null : Number(q.high),
        low: q === undefined ? null : Number(q.low),
        amount: q === undefined ? null : Number(q.amount),
        ts: q === undefined ? null : q.ts,
        stale: q === undefined ? true : Number(q.stale) === 1,
        spark: this.quoteHistory(w.symbol, 60).map((p) => p.price),
      }
    })
    const positions = this.portfolio(quoteMap)
    const equity = this.equityList({ limit: 400 })
    const navs = equity.map((e) => e.nav)
    const returns = []
    for (let i = 1; i < navs.length; i += 1) returns.push(navs[i] / navs[i - 1] - 1)
    const metrics = riskMetrics(navs, returns)
    return {
      ok: true,
      ts: nowIso(),
      version: this.getMeta('schema_version'),
      workspace: this.workspace,
      config: {
        workspace: this.workspace,
        market_session: marketSession(),
        quote_source: this.getMeta('quote_source') || 'unknown',
        feed: parseJson(this.getMeta('feed_state')) || null,
      },
      watchlist,
      quotes: quoteRows,
      positions: positions.positions,
      totals: positions.totals,
      equity,
      metrics,
      signals: this.signalsList({ limit: Number(options.signalLimit) || 80 }),
      backtests: this.backtestsList(Number(options.backtestLimit) || 20),
      notes: this.notesList({ limit: 20 }),
      alerts: this.alertsList({ limit: 30 }),
      runs: this.runsList(30),
      stats: this.stats(),
    }
  }

  /** 看板/报告用的 markdown 摘要（模型与用户共用一份口径）。 */
  reportMarkdown(options = {}) {
    const snap = this.snapshot({ signalLimit: options.signalLimit || 40, backtestLimit: 10 })
    const lines = []
    lines.push('# AI量化日报 · ' + nowIso().slice(0, 10))
    lines.push('')
    lines.push('- 生成时间：' + snap.ts + '（' + marketSession().label + '）')
    lines.push('- 工作区：' + snap.workspace + '；事实库：' + snap.stats.db)
    lines.push('- 自选 ' + snap.watchlist.length + ' · 持仓 ' + snap.totals.count + ' · 信号 ' + snap.signals.length + ' · 回测 ' + snap.backtests.length)
    lines.push('')
    lines.push('## 一、组合')
    lines.push('')
    if (snap.totals.count === 0) lines.push('_当前无持仓（看板可录入，或用 `aiquant_position` 工具建仓）_')
    else {
      lines.push('| 标的 | 名称 | 数量 | 成本 | 现价 | 市值 | 浮盈 | 盈亏% | 权重 |')
      lines.push('| --- | --- | --- | --- | --- | --- | --- | --- | --- |')
      for (const p of snap.positions) {
        lines.push('| ' + p.symbol + ' | ' + (p.name || '') + ' | ' + p.qty + ' | ' + p.avg_cost + ' | ' + p.price + (p.price_stale ? '(缓存)' : '') + ' | ' + p.market_value + ' | ' + p.pnl + ' | ' + (p.pnl_pct === null ? '-' : p.pnl_pct) + ' | ' + p.weight + '% |')
      }
      lines.push('')
      lines.push('合计：成本 ' + snap.totals.cost + ' · 市值 ' + snap.totals.market_value + ' · 浮盈 ' + snap.totals.pnl
        + '（' + (snap.totals.pnl_pct === null ? '-' : snap.totals.pnl_pct + '%') + '）；最大单票权重 ' + snap.totals.max_weight + '%')
    }
    lines.push('')
    lines.push('## 二、自选与行情')
    lines.push('')
    lines.push('| 标的 | 名称 | 现价 | 涨跌幅 | 时间 |')
    lines.push('| --- | --- | --- | --- | --- |')
    for (const w of snap.watchlist) {
      lines.push('| ' + w.symbol + ' | ' + (w.name || '') + ' | ' + (w.price === null ? '-' : w.price) + ' | ' + (w.pct === null ? '-' : w.pct + '%') + ' | ' + (w.ts || '-') + ' |')
    }
    lines.push('')
    lines.push('## 三、最新信号（' + snap.signals.length + ' 条）')
    lines.push('')
    if (snap.signals.length === 0) lines.push('_暂无信号_')
    else {
      lines.push('| 时间 | 标的 | 类型 | 方向 | 强度 | 价格 | 依据 |')
      lines.push('| --- | --- | --- | --- | --- | --- | --- |')
      for (const s of snap.signals.slice(0, 20)) {
        lines.push('| ' + s.ts + ' | ' + s.symbol + ' | ' + (s.kind_label || s.kind) + ' | ' + (s.direction || '-') + ' | ' + (s.strength === null ? '-' : s.strength) + ' | ' + (s.price === null ? '-' : s.price) + ' | ' + (s.reason || '') + ' |')
      }
    }
    lines.push('')
    lines.push('## 四、回测记录（最近 ' + Math.min(snap.backtests.length, 10) + ' 条）')
    lines.push('')
    if (snap.backtests.length === 0) lines.push('_暂无回测记录_')
    else {
      for (const b of snap.backtests.slice(0, 10)) {
        const m = b.metrics || {}
        lines.push('- **' + b.name + '**（' + b.ts + '，' + (b.strategy || '-') + '）'
          + '：年化 ' + fmtPct(m.annual_return_pct) + ' / 最大回撤 ' + fmtPct(m.max_drawdown_pct)
          + ' / 夏普 ' + fmtNum(m.sharpe) + ' / 样本外 ' + (b.out_of_sample ? '是' : '否')
          + '；成本模型 ' + (b.cost_model || '未标注'))
      }
    }
    lines.push('')
    lines.push('## 五、风险')
    lines.push('')
    const r = snap.metrics
    lines.push('- 净值样本 ' + r.samples + ' 点；区间收益 ' + fmtPct(r.total_return_pct) + '；最大回撤 ' + fmtPct(r.max_drawdown_pct) + '；年化波动 ' + fmtPct(r.volatility_pct))
    lines.push('- 夏普 ' + fmtNum(r.sharpe) + '；卡玛 ' + fmtNum(r.calmar) + '；VaR95(日) ' + fmtPct(r.var95_pct))
    lines.push('- 自检红线：' + (r.samples < 20 ? '净值样本不足，统计量**不可用**（先积累净值点）' : '样本可用'))
    lines.push('')
    lines.push('## 六、运行日志')
    lines.push('')
    for (const run of snap.runs.slice(0, 10)) lines.push('- [' + run.ts + '] ' + run.kind + ' · ' + run.status + ' · ' + (run.detail || ''))
    lines.push('')
    lines.push('---')
    lines.push('_口径：所有价格来自事实库最新快照（stale 表示非实时）；回测结论必须附成本假设与样本外结果；本报告不构成投资建议。_')
    return lines.join('\n')
  }
}

/* ────────────────────────────── 纯函数 ────────────────────────────── */

function num(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function round(value, digits = 4) {
  const n = Number(value)
  if (!Number.isFinite(n)) return null
  const factor = Math.pow(10, digits)
  return Math.round(n * factor) / factor
}

function jsonOrNull(value) {
  if (value === undefined || value === null) return null
  if (typeof value === 'string') return value
  try { return JSON.stringify(value) } catch { return null }
}

function parseJson(text) {
  if (text === undefined || text === null || text === '') return null
  try { return JSON.parse(String(text)) } catch { return null }
}

function fmtPct(value) {
  return value === null || value === undefined ? '-' : Number(value).toFixed(2) + '%'
}

function fmtNum(value) {
  return value === null || value === undefined ? '-' : Number(value).toFixed(2)
}

/** 皮尔逊相关系数（样本不足返回 null）。 */
export function pearson(a, b) {
  const n = Math.min(a.length, b.length)
  if (n < 20) return null
  const x = a.slice(a.length - n)
  const y = b.slice(b.length - n)
  const mx = x.reduce((s, v) => s + v, 0) / n
  const my = y.reduce((s, v) => s + v, 0) / n
  let cov = 0
  let vx = 0
  let vy = 0
  for (let i = 0; i < n; i += 1) {
    const dx = x[i] - mx
    const dy = y[i] - my
    cov += dx * dy
    vx += dx * dx
    vy += dy * dy
  }
  if (vx <= 0 || vy <= 0) return null
  return cov / Math.sqrt(vx * vy)
}

/**
 * 净值序列 → 风险指标。所有百分比都是百分数（25 表示 25%）。
 * 样本不足时返回 null 语义的字段（界面必须显示"样本不足"而不是 0）。
 */
export function riskMetrics(navs, returns) {
  const samples = Array.isArray(navs) ? navs.length : 0
  const out = {
    samples,
    total_return_pct: null,
    annual_return_pct: null,
    max_drawdown_pct: null,
    current_drawdown_pct: null,
    volatility_pct: null,
    sharpe: null,
    calmar: null,
    var95_pct: null,
    es95_pct: null,
    win_rate_pct: null,
    best_day_pct: null,
    worst_day_pct: null,
    avg_daily_pct: null,
  }
  if (samples >= 2) {
    const first = navs[0]
    const last = navs[samples - 1]
    if (first > 0) out.total_return_pct = round((last / first - 1) * 100, 2)
    let peak = navs[0]
    let maxDd = 0
    for (const nav of navs) {
      if (nav > peak) peak = nav
      const dd = peak > 0 ? (nav / peak - 1) * 100 : 0
      if (dd < maxDd) maxDd = dd
    }
    out.max_drawdown_pct = round(Math.abs(maxDd), 2)
    out.current_drawdown_pct = peak > 0 ? round(Math.abs((last / peak - 1) * 100), 2) : null
  }
  if (Array.isArray(returns) && returns.length >= 5) {
    const n = returns.length
    const mean = returns.reduce((s, v) => s + v, 0) / n
    const variance = returns.reduce((s, v) => s + Math.pow(v - mean, 2), 0) / Math.max(1, n - 1)
    const sd = Math.sqrt(variance)
    out.avg_daily_pct = round(mean * 100, 3)
    out.volatility_pct = round(sd * Math.sqrt(252) * 100, 2)
    out.sharpe = sd > 0 ? round((mean / sd) * Math.sqrt(252), 2) : null
    if (out.max_drawdown_pct !== null && out.max_drawdown_pct > 0 && out.annual_return_pct !== null) {
      out.calmar = round(out.annual_return_pct / out.max_drawdown_pct, 2)
    }
    const sorted = [...returns].sort((a, b) => a - b)
    const idx = Math.max(0, Math.min(n - 1, Math.floor(n * 0.05)))
    out.var95_pct = round(Math.abs(sorted[idx]) * 100, 2)
    const tail = sorted.slice(0, Math.max(1, idx + 1))
    out.es95_pct = round(Math.abs(tail.reduce((s, v) => s + v, 0) / tail.length) * 100, 2)
    out.win_rate_pct = round((returns.filter((v) => v > 0).length / n) * 100, 1)
    out.best_day_pct = round(Math.max(...returns) * 100, 2)
    out.worst_day_pct = round(Math.min(...returns) * 100, 2)
  }
  if (samples >= 2 && out.total_return_pct !== null && samples > 1) {
    out.annual_return_pct = round((Math.pow(1 + out.total_return_pct / 100, 252 / Math.max(1, samples - 1)) - 1) * 100, 2)
    if (out.max_drawdown_pct !== null && out.max_drawdown_pct > 0) out.calmar = round(out.annual_return_pct / out.max_drawdown_pct, 2)
  }
  return out
}

/** 当前市场时段（北京时间）。 */
export function marketSession(date = new Date()) {
  const bj = new Date(date.getTime() + (date.getTimezoneOffset() * 60000) + 8 * 3600000)
  const day = bj.getDay()
  const minutes = bj.getHours() * 60 + bj.getMinutes()
  const weekend = day === 0 || day === 6
  const aOpen = !weekend && ((minutes >= 9 * 60 + 30 && minutes <= 11 * 60 + 30) || (minutes >= 13 * 60 && minutes <= 15 * 60))
  const aPre = !weekend && minutes >= 9 * 60 + 15 && minutes < 9 * 60 + 30
  const hkOpen = !weekend && ((minutes >= 9 * 60 + 30 && minutes <= 12 * 60) || (minutes >= 13 * 60 && minutes <= 16 * 60))
  const usOpen = !(day === 0 || day === 6) && (minutes >= 21 * 60 + 30 || minutes <= 4 * 60)
  return {
    ts: nowIso(date),
    bj_time: bj.toISOString().slice(11, 19),
    a_share: aOpen ? 'open' : aPre ? 'pre' : weekend ? 'closed_weekend' : 'closed',
    hk: hkOpen ? 'open' : 'closed',
    us: usOpen ? 'open' : 'closed',
    crypto: 'open',
    label: aOpen ? 'A股交易中' : aPre ? '集合竞价' : weekend ? '周末休市' : (usOpen ? '美股交易中' : 'A股休市'),
  }
}

/** 目录体积（自检用）。 */
export function dirSize(dir) {
  let total = 0
  let files = 0
  const walk = (current, depth) => {
    if (depth > 4 || !existsSync(current)) return
    let entries = []
    try { entries = readdirSync(current, { withFileTypes: true }) } catch { return }
    for (const entry of entries) {
      const full = join(current, entry.name)
      if (entry.isDirectory()) walk(full, depth + 1)
      else {
        try { total += statSync(full).size; files += 1 } catch { /* 忽略 */ }
      }
    }
  }
  walk(dir, 0)
  return { bytes: total, files }
}
