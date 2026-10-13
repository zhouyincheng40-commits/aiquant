/**
 * 开发工具：在浏览器里读回 agent preset 的 `broken` 原因（不会被自动加载）
 *
 * 背景：新会话模式卡片上的红色「加载失败」只把原因放进 DOM 里一个视觉隐藏元素 +
 * badge 悬浮提示（`.KZ7f9OG_cardBrokenReason` 是 clip 隐藏、`brokenTip` 是 hover 才显示），
 * 命令行拿不到。这个 bundle 通过 `ctx.remote.agentPresets.list()` 把原因直接画到页面上。
 *
 * 用法：
 *   1) copy lib\preset-diag.client.js lib\client.js
 *   2) powershell -NoProfile -File tools\install.ps1 -NoBuild
 *   3) 等约 1 秒（client-hmr 会热替换），看窗口左上角绿色浮层；用完执行
 *      git 之外的还原：copy lib\client.board.disabled.js / lib\client.js（看板）
 *      或把下面第 4 步的干净版覆盖回来。
 *   4) 用完必须还原：tools\install.ps1 -NoBuild 会同步 lib\client.js，还原成不注册任何插槽的空模块。
 *
 * 依赖：cordis browser inject 必须同时含 'remote' 与 'remote.agentPresets'，少 'remote' 会报
 * `cannot get property "remote" without inject`。
 */
window.__ModuleLoader__.load({
  id: 'dsh-aiquant-mode',
  factory: () => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })
    const inject = ['remote', 'remote.agentPresets']
    function paint(text) {
      const el = document.createElement('pre')
      el.setAttribute('data-aiquant-diag', '1')
      el.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483647;width:74vw;max-height:94vh;overflow:auto;background:#0b2b12;color:#c8ffd4;font:12px/1.5 Consolas,monospace;padding:8px;margin:0;white-space:pre-wrap;word-break:break-all;border:1px solid #888'
      el.textContent = text
      document.body.append(el)
      return el
    }
    async function apply(ctx) {
      const el = paint('AIQUANT DIAG: loading ...')
      ctx.effect(() => () => el.remove())
      try {
        const res = await ctx.remote.agentPresets.list()
        const presets = (res && res.value && res.value.presets) || []
        const mine = presets.find((p) => p && p.id === 'aiquant')
        if (mine === undefined) {
          el.textContent = 'AIQUANT DIAG: no aiquant row. ids=' + presets.map((p) => p && p.id).join(',')
          return
        }
        const broken = mine.broken
        el.textContent = 'AIQUANT DIAG broken(' + (broken === undefined ? 'undefined' : String(broken).length) + ' chars):\n'
          + (broken === undefined ? '(none)' : broken)
          + '\n\n--- row keys: ' + Object.keys(mine).join(',')
      } catch (error) {
        el.textContent = 'AIQUANT DIAG ERROR: ' + (error && error.message ? error.message : String(error))
      }
    }
    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
