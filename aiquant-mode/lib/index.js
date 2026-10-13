/**
 * dsh-aiquant-mode —— 插件自举（host 半侧）
 *
 * 这个包是**自包含**的：事实库、行情引擎、工具、看板、预设、技能全在包内，
 * 装一个包就齐活。本文件负责四件事：
 *
 *   0. **新会话里的「AI量化模式」入口不在这里**：DSH 0.1.7-rc.2 起 agent preset
 *      只认 `@deepseek-ai/dsh-agent-preset` 声明行，由随包补丁 `cordis.patch.yml`
 *      的生成区（tools/build.mjs）注册；注册表既不扫目录、也不接受预设路径。
 *      下面这套"落地到用户预设目录"的机制是给老版本 DSH 兼容 + 给用户一份可读副本。
 *   1. 首次启动把包内 `presets/aiquant/` 装到 `$DSH_HOME/.agent-presets/aiquant/`，
 *      已存在则**不动用户内容**（用户自己的改动优先）。
 *   2. 把包内 `scripts/aiquant-setup.ps1` 落到 `$DSH_HOME/aiquant/setup.ps1`，
 *      并在工作区缺失时初始化目录骨架（data/strategies/reports/logs/skills）。
 *   3. 把包内路径挂到 `ctx.aiquantMode` 上（paths / workspace / preset 安装结果），
 *      供排障与其它行复用。
 */
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Cordis 插件名。 */
export const name = 'aiquant-mode'

/** 无硬依赖：即使别的服务缺失也不该阻断挂载。 */
export const inject = []

/** 默认工作区根（可用环境变量 AIQUANT_HOME 覆盖）。 */
export const DEFAULT_WORKSPACE = 'D:/量化'

/** 本包根目录（从模块位置解析，装到哪个 profile / 哪个路径都对）。 */
export function packagePaths() {
  const root = fileURLToPath(new URL('..', import.meta.url))
  return {
    root,
    presets: join(root, 'presets'),
    presetDir: join(root, 'presets', 'aiquant'),
    skills: join(root, 'skills'),
    scripts: join(root, 'scripts'),
    setupScript: join(root, 'scripts', 'aiquant-setup.ps1'),
  }
}

/** $DSH_HOME 目录。 */
export function dshHome() {
  return process.env.DSH_HOME || join(homedir(), '.dsh')
}

/** 事实库数据目录（与 store/feed 用的同一份约定）。 */
export function aiquantDataDir() {
  const home = dshHome()
  return join(home, 'aiquant')
}

/** 工作区根：显式参数 > AIQUANT_HOME > 默认 D:/量化。 */
export function resolveWorkspace(explicit) {
  if (typeof explicit === 'string' && explicit.trim() !== '') return explicit.trim()
  if (process.env.AIQUANT_HOME && process.env.AIQUANT_HOME.trim() !== '') return process.env.AIQUANT_HOME.trim()
  return DEFAULT_WORKSPACE
}

/** 用户 preset 根（老版本 DSH 的目录扫描路径；新版不读它，仅作副本/兼容）。 */
export function userPresetRoot() {
  return join(dshHome(), '.agent-presets')
}

/**
 * 工作区目录骨架：不存在就建，已存在不碰。
 * @returns 建了哪些目录。
 */
export function ensureWorkspaceLayout(workspace) {
  const dirs = ['data', 'data/cache', 'data/exports', 'strategies', 'reports', 'logs', 'notebooks', 'skills']
  const created = []
  for (const rel of dirs) {
    const target = join(workspace, rel)
    if (!existsSync(target)) {
      mkdirSync(target, { recursive: true })
      created.push(rel)
    }
  }
  return created
}

/**
 * 把包内预设落地到用户 preset 根（老版本 DSH 兼容 + 可读副本）。
 * 目标已存在 → 不覆盖（用户自己的改动优先），只报告。
 */
export function installPreset(options = {}) {
  const paths = packagePaths()
  const target = join(userPresetRoot(), 'aiquant')
  const log = options.log ?? (() => {})
  const presetFile = join(target, 'agent.cordis.yml')
  const skillCount = existsSync(paths.skills) ? readdirSync(paths.skills).filter((f) => f.endsWith('.md')).length : 0

  if (existsSync(presetFile) && options.force !== true) {
    log(`已存在用户自己的预设 ${target}，保留不动（要覆盖：AIQUANT_PRESET_REFRESH=1 启动一次）`)
    return { action: 'kept', dir: target, skillsDir: paths.skills, skills: skillCount }
  }
  mkdirSync(target, { recursive: true })
  for (const file of readdirSync(paths.presetDir)) {
    writeFileSync(join(target, file), readFileSync(join(paths.presetDir, file), 'utf8'), 'utf8')
  }
  log(`预设已安装到 ${target}（打包技能 ${skillCount} 个：${paths.skills}）`)
  return { action: 'installed', dir: target, skillsDir: paths.skills, skills: skillCount }
}

/**
 * 把随包的环境安装脚本落到 `$DSH_HOME/aiquant/setup.ps1`。
 * 语义：不存在 → 装；内容不同 → 覆盖；相同 → 不动。
 */
export function installSetupScript() {
  const paths = packagePaths()
  const target = join(aiquantDataDir(), 'setup.ps1')
  if (!existsSync(paths.setupScript)) return { action: 'missing', path: target }
  let wanted
  try {
    wanted = readFileSync(paths.setupScript, 'utf8')
  } catch {
    return { action: 'missing', path: target }
  }
  try {
    mkdirSync(aiquantDataDir(), { recursive: true })
    if (existsSync(target)) {
      let current = ''
      try { current = readFileSync(target, 'utf8') } catch { current = '' }
      if (current === wanted) return { action: 'kept', path: target }
      writeFileSync(target, wanted, 'utf8')
      return { action: 'updated', path: target }
    }
    writeFileSync(target, wanted, 'utf8')
    return { action: 'installed', path: target }
  } catch (error) {
    return { action: 'missing', path: target, error: error && error.message ? error.message : String(error) }
  }
}

/** 本包 package.json（版本显示用；读不到返回 null）。 */
export function readSelfPackage() {
  for (const rel of ['../package.json']) {
    try {
      const pkg = JSON.parse(readFileSync(new URL(rel, import.meta.url), 'utf8'))
      if (pkg && typeof pkg.name === 'string' && typeof pkg.version === 'string') {
        return {
          name: pkg.name,
          version: pkg.version,
          description: typeof pkg.description === 'string' ? pkg.description : '',
        }
      }
    } catch { /* 忽略 */ }
  }
  return null
}

/**
 * @param ctx - 插件上下文。
 * @param config - `{ workspace?: string }`。
 */
export function apply(ctx, config = {}) {
  const paths = packagePaths()
  const workspace = resolveWorkspace(config && config.workspace)
  let preset
  try {
    preset = installPreset({
      force: process.env.AIQUANT_PRESET_REFRESH === '1',
      log: (msg) => ctx.logger?.info?.('aiquant-mode: ' + msg),
    })
  } catch (error) {
    ctx.logger?.error?.('aiquant-mode: 预设安装失败：%s', error && error.message ? error.message : String(error))
    preset = { action: 'failed', dir: join(userPresetRoot(), 'aiquant'), skillsDir: paths.skills, skills: 0 }
  }
  let setup
  try {
    setup = installSetupScript()
  } catch (error) {
    setup = { action: 'missing', error: error && error.message ? error.message : String(error) }
  }
  let created = []
  try {
    created = ensureWorkspaceLayout(workspace)
  } catch (error) {
    ctx.logger?.warn?.('aiquant-mode: 工作区骨架创建失败（%s）：%s', workspace, error && error.message ? error.message : String(error))
  }
  if (created.length > 0) ctx.logger?.info?.('aiquant-mode: 工作区目录已初始化 %s', created.join(', '))

  ctx.provide('aiquantMode', Object.freeze({
    paths,
    workspace,
    dataDir: aiquantDataDir(),
    preset,
    setup,
    package: readSelfPackage(),
  }))
  ctx.logger?.info?.(
    'aiquant-mode: 就绪（工作区 %s；数据目录 %s；预设 %s；技能目录 %s）',
    workspace, aiquantDataDir(), preset.dir, preset.skillsDir,
  )
}

export default { name, inject, apply }
