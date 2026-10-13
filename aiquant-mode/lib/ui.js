/**
 * AI量化看板 —— host 半侧（HTTP 桥接）
 *
 * 为什么走 webServer 而不是 typert/@Remote：本包与 store/feed 都刻意保持零外部依赖，
 * 一个 POST /aiquant/api 的 JSON 桥就够看板用；浏览器半侧（lib/client.js）是手写 bundle。
 *
 * 安全：**默认拒绝**
 *   · 带 Origin → 必须与 Host 同源；
 *   · 不带 Origin → 只允许回环地址（否则一个本地恶意页面就能读到持仓与策略）。
 * 所有响应 `cache-control: no-store`（实时数据不许被中间层缓存）。
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync, statSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { nowIso, normalizeSymbol, marketSession, dirSize, SIGNAL_KIND_LABEL } from './store-core.js'
import { resolveProxy, portOpen } from './net.js'

/** Cordis 插件名。 */
export const name = 'aiquant-ui'

/** 硬依赖：事实库 + HTTP 服务。行情引擎/自举信息可选。 */
export const inject = ['aiquant', 'webServer']

/** 请求体上限（防误用大对象打爆内存）。 */
const MAX_BODY_BYTES = 2 * 1024 * 1024

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  })
  res.end(body)
}

/** 读取请求体（带上限）。 */
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new Error('请求体过大（>' + MAX_BODY_BYTES + ' 字节）'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

/** 同源校验：带 Origin 必须一致；不带 Origin 只允许回环。 */
export function originAllowed(req) {
  const origin = req.headers.origin
  const host = req.headers.host
  if (typeof origin === 'string' && origin !== '') {
    let originHost = null
    try { originHost = new URL(origin).host } catch { originHost = null }
    return originHost !== null && originHost === host
  }
  const remote = req.socket && req.socket.remoteAddress ? String(req.socket.remoteAddress) : ''
  return remote === '127.0.0.1' || remote === '::1' || remote === '::ffff:127.0.0.1'
}

/**
 * 环境自检：路径、解释器、数据源可达性、看板状态。
 * @returns 结构化结果（供 `aiquant_env` 工具与看板「数据」页共用）。
 */
export async function environmentReport(ctx, options = {}) {
  const store = ctx.aiquant
  const feed = typeof ctx.get === 'function' ? ctx.get('aiquantFeed') : undefined
  const mode = typeof ctx.get === 'function' ? ctx.get('aiquantMode') : undefined
  const workspace = (mode && mode.workspace) || store.workspace
  const paths = (mode && mode.paths) || {}
  const condaRoot = process.env.CONDA_ROOT || 'C:/Users/33096/.conda/envs'
  const interpreters = {
    aiquant: join(condaRoot, 'aiquant', 'python.exe'),
    'aiquant-live': join(condaRoot, 'aiquant-live', 'python.exe'),
  }
  const skillsDirs = [join(workspace, 'skills'), join(process.env.DSH_HOME || 'C:/Users/33096/.dsh', 'skills')]
  const proxy = await resolveProxy()
  const report = {
    ts: nowIso(),
    node: process.version,
    platform: process.platform,
    workspace,
    data_dir: store.root,
    db_file: store.dbFile,
    db_bytes: (() => { try { return statSync(store.dbFile).size } catch { return 0 } })(),
    package: (mode && mode.package) || store.plugin || null,
    modules: {
      store: { ok: true, db: store.dbFile },
      feed: feed === undefined ? { ok: false, error: 'aiquantFeed 服务未挂载（aiquant-mode-feed 行未启用）' } : { ok: true, ...feed.status() },
      ui: { ok: true, route: '/aiquant/api' },
      mcp_server: (() => {
        const file = join(workspace, 'mcp', 'quant_mcp', 'server.py')
        return existsSync(file) ? { ok: true, path: file, bytes: statSync(file).size } : { ok: false, path: file, error: '未找到 MCP 服务器文件' }
      })(),
      skills: skillsDirs.map((dir) => ({
        dir,
        exists: existsSync(dir),
        count: existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith('.md') || existsSync(join(dir, f, 'SKILL.md'))).length : 0,
      })),
    },
    interpreters: Object.fromEntries(Object.entries(interpreters).map(([key, file]) => [key, { path: file, exists: existsSync(file) }])),
    paths: { presets: paths.presets || null, skills: paths.skills || null, setup: (mode && mode.setup && mode.setup.path) || null },
    proxy: proxy === null ? null : { ...proxy, reachable: await portOpen(proxy.host, proxy.port) },
    market: marketSession(),
    storage: {
      cache: dirSize(join(workspace, 'data', 'cache')),
      exports: dirSize(join(workspace, 'data', 'exports')),
      reports: dirSize(join(workspace, 'reports')),
      logs: dirSize(join(workspace, 'logs')),
    },
    stats: store.stats(),
  }
  if (options.probeSources === true && feed !== undefined) {
    report.sources = await feed.probeSources()
  }
  return report
}

/**
 * 业务分发：看板客户端与（可选）其它调用方共用。
 * @returns 响应对象（已含 ok 字段）。
 */
export async function dispatch(ctx, request) {
  const store = ctx.aiquant
  const feed = typeof ctx.get === 'function' ? ctx.get('aiquantFeed') : undefined
  const op = typeof request.op === 'string' ? request.op : ''
  const body = request

  switch (op) {
    case 'ping':
      return { ok: true, data: { pong: true, ts: nowIso(), version: store.getMeta('schema_version'), feed: feed === undefined ? null : feed.status() } }

    case 'snapshot':
      return { ok: true, data: store.snapshot({ signalLimit: body.signals, backtestLimit: body.backtests }) }

    case 'env':
      return { ok: true, data: await environmentReport(ctx, { probeSources: body.probe === true }) }

    case 'quotes': {
      const symbols = Array.isArray(body.symbols) ? body.symbols : undefined
      if (feed !== undefined) {
        try { await feed.refreshQuotes(symbols, { force: body.force === true }) } catch (error) {
          ctx.logger?.warn?.('aiquant-ui: 刷新行情失败：%s', error && error.message ? error.message : String(error))
        }
      }
      const list = Array.isArray(symbols) && symbols.length > 0
        ? store.quotesGet(symbols)
        : store.snapshot().watchlist.map((w) => ({
          symbol: w.symbol, name: w.name, price: w.price, pct: w.pct, change: w.change,
          high: w.high, low: w.low, amount: w.amount, ts: w.ts, stale: w.stale, market: w.market,
        }))
      return { ok: true, data: list }
    }

    case 'analysis': {
      if (feed === undefined) return { ok: false, error: '行情引擎未挂载' }
      const symbol = body.symbol
      if (typeof symbol !== 'string' || symbol === '') return { ok: false, error: 'analysis 需要 symbol' }
      const result = await feed.analyzeSymbol(symbol, { force: body.force === true })
      return {
        ok: true,
        data: {
          symbol: normalizeSymbol(symbol).symbol,
          indicators: result.indicators,
          klines: result.klines,
          name: (store.watchlistGet(symbol) || {}).name || null,
        },
      }
    }

    case 'watchlist.add': {
      const row = store.watchlistAdd(body)
      if (feed !== undefined) { try { await feed.refreshQuotes([row.symbol], { force: true }) } catch { /* 行情稍后再取 */ } }
      return { ok: true, data: row }
    }

    case 'watchlist.remove':
      return { ok: true, data: store.watchlistRemove(body.symbol) }

    case 'watchlist.list':
      return { ok: true, data: store.watchlistList() }

    case 'position.add': {
      const row = store.positionAdd(body)
      const point = feed === undefined ? null : feed.recordEquityPoint()
      return { ok: true, data: { position: row, equity_point: point } }
    }

    case 'position.update':
      return { ok: true, data: store.positionUpdate(body.id, body.patch || body) }

    case 'position.close': {
      const row = store.positionClose(body.id, body)
      return { ok: true, data: row }
    }

    case 'position.list':
      return { ok: true, data: store.positionsList({ status: body.status || 'open' }) }

    case 'signal.add': {
      const row = store.signalAdd(body)
      return { ok: true, data: row }
    }

    case 'signal.list':
      return { ok: true, data: store.signalsList({ limit: body.limit, symbol: body.symbol, since: body.since, kind: body.kind }) }

    case 'signal.scan': {
      if (feed === undefined) return { ok: false, error: '行情引擎未挂载' }
      const result = await feed.scan({ force: body.force === true, symbols: body.symbols })
      return { ok: true, data: result }
    }

    case 'signal.clear':
      return { ok: true, data: store.signalsClear(body) }

    case 'backtest.add':
      return { ok: true, data: store.backtestAdd(body) }

    case 'backtest.list':
      return { ok: true, data: store.backtestsList(body.limit) }

    case 'backtest.remove':
      return { ok: true, data: store.backtestRemove(body.id) }

    case 'equity.add':
      return { ok: true, data: store.equityAdd(body) }

    case 'equity.list':
      return { ok: true, data: store.equityList({ portfolio: body.portfolio, limit: body.limit }) }

    case 'risk.get':
      return { ok: true, data: store.risk({ portfolio: body.portfolio }) }

    case 'note.add':
      return { ok: true, data: store.noteAdd(body) }

    case 'note.list':
      return { ok: true, data: store.notesList({ limit: body.limit, kind: body.kind }) }

    case 'alert.add':
      return { ok: true, data: store.alertAdd(body) }

    case 'alert.list':
      return { ok: true, data: store.alertsList({ limit: body.limit, ack: body.ack }) }

    case 'alert.ack':
      return { ok: true, data: store.alertsAck(body.id) }

    case 'run.log':
      return { ok: true, data: store.runLog(body) }

    case 'run.list':
      return { ok: true, data: store.runsList(body.limit) }

    case 'feed.status':
      return { ok: true, data: feed === undefined ? null : feed.status() }

    case 'feed.refresh': {
      if (feed === undefined) return { ok: false, error: '行情引擎未挂载' }
      const result = await feed.cycle()
      return { ok: true, data: result }
    }

    case 'report.md': {
      const markdown = store.reportMarkdown()
      if (body.save === true) {
        const dir = join(store.workspace, 'reports')
        mkdirSync(dir, { recursive: true })
        const stamp = nowIso().slice(0, 10).replace(/-/g, '')
        const file = join(dir, stamp + '-ai量化日报.md')
        writeFileSync(file, markdown, 'utf8')
        return { ok: true, data: { markdown, path: file } }
      }
      return { ok: true, data: { markdown } }
    }

    case 'config.get':
      return {
        ok: true,
        data: {
          workspace: store.workspace,
          data_dir: store.root,
          market: marketSession(),
          feed: feed === undefined ? null : feed.status(),
          signal_kinds: Object.entries(SIGNAL_KIND_LABEL).map(([id, label]) => ({ id, label })),
        },
      }

    case 'config.set': {
      const allowed = ['quote_source', 'workspace']
      const changed = {}
      for (const key of allowed) {
        if (body[key] !== undefined) { store.setMeta(key, body[key]); changed[key] = body[key] }
      }
      return { ok: true, data: changed }
    }

    default:
      return { ok: false, error: '未知 op：' + (op === '' ? '(空)' : op) }
  }
}

/**
 * @param ctx - 插件上下文。
 */
export function apply(ctx) {
  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: '/aiquant/api',
    handler: async (req, res) => {
      if (req.method !== 'POST') {
        sendJson(res, 405, { ok: false, error: '仅支持 POST' })
        return
      }
      if (!originAllowed(req)) {
        sendJson(res, 403, { ok: false, error: '跨源请求被拒绝（带 Origin 必须同源；不带 Origin 只允许回环地址）' })
        return
      }
      try {
        const raw = await readBody(req)
        const request = raw.trim() === '' ? {} : JSON.parse(raw)
        const result = await dispatch(ctx, request)
        sendJson(res, 200, result)
      } catch (error) {
        sendJson(res, 400, { ok: false, error: error && error.message ? error.message : String(error) })
      }
    },
  }))

  ctx.logger?.info?.('aiquant-ui: 看板接口就绪（POST /aiquant/api）')
}

export { readFileSync, writeFileSync }
