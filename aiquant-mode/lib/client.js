/**
 * AI量化看板 —— 浏览器半侧（2026-10-10 已删除）
 *
 * 用户要求删除右侧常驻「AI量化 看板」面板，因此本文件不再向任何插槽注册 UI：
 *   · shell.overlay（看板本体，原 id=aiquant-board）
 *   · sidebar.footer.action（侧栏「AI量化」开关，原 id=aiquant-toggle）
 *   · conversation.session.header.utilities（会话头 📈 按钮，原 id=aiquant-header-toggle）
 *
 * 之所以留一个合法的空客户端模块、而不是删掉 package.json 的 dsh.client 声明：
 * 这样浏览器半侧走 client-hmr 的热替换路径（旧实例的 fiber 与插槽注册会被 tearDown，
 * 见 @deepseek-ai/dsh-client-modules 的 reload/replace），无需重启 DSH 即可让面板消失。
 *
 * host 半侧完全不受影响：事实库 ctx.aiquant / 行情引擎 ctx.aiquantFeed /
 * POST /aiquant/api / 12 个 aiquant_* 工具照常工作，数据用 aiquant_board 回读。
 *
 * 想恢复看板：把 lib/client.board.disabled.js 覆盖回 lib/client.js，再跑
 * tools/install.ps1（客户端侧由 HMR 热重载，无需重启）。
 *
 * 格式与外层的模块加载器一致：window.__ModuleLoader__.load({ id, factory })。
 * id 必须等于包名（clientModules 以 manifest 包名作为浏览器模块身份）。
 */
window.__ModuleLoader__.load({
  id: 'dsh-aiquant-mode',
  factory: () => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    /* 不注册任何插槽：本包在浏览器侧不渲染任何界面。 */
    const inject = []

    function apply() { /* 无 UI */ }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
