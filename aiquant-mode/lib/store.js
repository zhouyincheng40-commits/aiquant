/**
 * AI量化事实库 —— Cordis Host 插件壳
 *
 * 职责：把 {@link QuantStore} 发布为进程级服务 `ctx.aiquant`，供
 *   · 浏览器看板（经 dsh-aiquant-mode/ui 的 HTTP 桥接）
 *   · 行情引擎（dsh-aiquant-mode/feed）
 *   · 各角色智能体的工具行（preset 平面，注入 ctx.aiquant）
 * 共同读写同一份 SQLite 事实库。
 *
 * 平面归属：host。信号/持仓/回测要跨会话被后续角色共享，服务实例必须进程级唯一。
 *
 * 零外部依赖：只用 node: 内置模块（node:sqlite），避免 profile 下解析不到 node_modules。
 */
import { join } from 'node:path'
import { homedir } from 'node:os'
import { readFileSync } from 'node:fs'
import { QuantStore } from './store-core.js'

/** Cordis 插件名。 */
export const name = 'aiquant-store'

/** 无硬依赖：数据层不依赖其它服务即可激活。 */
export const inject = []

/**
 * 解析数据根：显式配置 > AIQUANT_DATA_HOME > $DSH_HOME/aiquant。
 * 组合层通常用 `!!js dshHomePath('aiquant')` 直接给出绝对路径。
 */
function resolveRoot(config) {
  if (config && typeof config.root === 'string' && config.root.length > 0) return config.root
  if (process.env.AIQUANT_DATA_HOME) return process.env.AIQUANT_DATA_HOME
  const home = process.env.DSH_HOME || join(homedir(), '.dsh')
  return join(home, 'aiquant')
}

/** 本插件自己的包身份（看板「版本」与自检用）。 */
function readSelfPackage() {
  for (const rel of ['../package.json', '../../package.json']) {
    try {
      const pkg = JSON.parse(readFileSync(new URL(rel, import.meta.url), 'utf8'))
      if (pkg && typeof pkg.name === 'string' && typeof pkg.version === 'string') {
        return {
          name: pkg.name,
          version: pkg.version,
          description: typeof pkg.description === 'string' ? pkg.description : '',
          homepage: typeof pkg.homepage === 'string' ? pkg.homepage : null,
        }
      }
    } catch { /* 换下一个候选 */ }
  }
  return null
}

/**
 * @param ctx - 插件上下文。
 * @param config - `{ root?: string, workspace?: string }`。
 */
export function apply(ctx, config = {}) {
  const root = resolveRoot(config)
  const workspace = (config && typeof config.workspace === 'string' && config.workspace !== '')
    ? config.workspace
    : (process.env.AIQUANT_HOME || 'D:/量化')
  const store = new QuantStore(root, { workspace })
  const self = readSelfPackage()
  if (self !== null) {
    try { store.plugin = Object.freeze(self) } catch { /* 忽略 */ }
  }
  store.runLog({ kind: 'system', status: 'ok', detail: '事实库已打开：' + store.dbFile + '（工作区 ' + workspace + '）', actor: 'system' })

  /* 服务随 fiber 卸载而关闭：SQLite 句柄在同一处释放 */
  ctx.effect(() => () => store.close())

  ctx.provide('aiquant', store)
  ctx.logger?.info?.('aiquant-store: 就绪（%s；工作区 %s）', store.dbFile, workspace)
}

export { QuantStore }
