/**
 * AI量化看板 —— 浏览器半侧（手写 bundle）
 *
 * 格式与外层的模块加载器一致：window.__ModuleLoader__.load({ id, factory })。
 * id 必须等于包名（clientModules 以 manifest 包名作为浏览器模块身份）。
 * 基座外无依赖：只用平台 seed 里的 react 与 ctx.slots。
 *
 * 界面：常驻右侧「实时走向看板」，页签：概览 / 行情 / 信号 / 持仓 / 回测 / 风控 / 数据。
 * 数据：每 5 秒 POST /aiquant/api {op:'snapshot'}（服务端行情引擎每 6 秒刷一次库）。
 */
window.__ModuleLoader__.load({
  id: 'dsh-aiquant-mode',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')
    const h = React.createElement

    /*
     * 布局接缝：AppFrame 是 display:grid（sidebar | 1fr | rightbar），
     * shell.overlay 是它内部 position:absolute;inset:0 的浮动层，带稳定属性 data-shell-overlay。
     * 看板自己钉在视口右缘（fixed），不修改 frame 的盒模型 —— 原因见下面 CSS 注释。
     * 右侧栏是否收起看 data-rightbar-collapsed（新版）/ data-details-collapsed（旧版）。
     */
    const CSS = `
:root{--aq-dock-w:520px}
/* 布局策略（2026-10-09 实测修正）：
   早期版本学红队控制台给 AppFrame 加 padding-right 让中栏让位，但 DSH 0.2.0-rc.2 的
   AppFrame 在 padding 后会横向溢出（frame 宽 > 视口），overlay 层随之变宽，
   right:0 的看板被推到视口外，只剩 ~264px 一条边可见。
   现在改为：看板自己钉在视口右缘（position:fixed, right:0），不动 frame 的任何盒模型 ——
   几何量只取决于视口宽度，任何版本都不跑偏；中栏被覆盖的部分用「收起」或拖动改宽度处理。 */
.aq-dock{position:fixed;top:0;right:0;bottom:0;z-index:19;display:flex;flex-direction:column;
  width:min(var(--aq-dock-w),100vw);
  background:var(--dsw-alias-bg-layer-1);border-left:1px solid var(--dsw-alias-border-l1);
  box-shadow:-12px 0 32px rgba(0,0,0,.14);pointer-events:auto;color:var(--dsw-alias-label-primary);
  font-size:13px;line-height:1.5;transition:transform .18s ease,opacity .18s ease}
div:has(> [data-shell-overlay] .aq-dock[data-open="1"]):not([data-details-collapsed]):not([data-rightbar-collapsed]) .aq-dock,
div:has(> [data-shell-overlay] .aq-dock[data-open="1"])[data-rightbar-fullscreen] .aq-dock{
  transform:translateX(100%);opacity:0;pointer-events:none}
.aq-dock,.aq-dock *{-webkit-app-region:no-drag}
.aq-grip{position:absolute;left:-3px;top:0;bottom:0;width:6px;cursor:col-resize;background:transparent;z-index:2}
.aq-head{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--dsw-alias-border-l1)}
.aq-title{font-weight:600;font-size:14px;display:flex;align-items:center;gap:6px;white-space:nowrap}
.aq-dot{width:8px;height:8px;border-radius:50%;background:var(--dsw-alias-brand-primary);flex:none}
.aq-dot[data-live="1"]{box-shadow:0 0 0 3px rgba(38,190,120,.18);background:#26be78}
.aq-spacer{flex:1}
.aq-pill{border:1px solid var(--dsw-alias-border-l1);border-radius:999px;padding:1px 8px;font-size:11px;
  color:var(--dsw-alias-label-secondary);white-space:nowrap}
.aq-btn{border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-2);color:inherit;
  border-radius:6px;padding:3px 9px;font-size:12px;cursor:pointer;font-family:inherit;white-space:nowrap}
.aq-btn:hover{border-color:var(--dsw-alias-border-l2)}
.aq-btn:disabled{opacity:.45;cursor:default}
.aq-btn-primary{background:var(--dsw-alias-brand-primary);border-color:var(--dsw-alias-brand-primary);color:#fff}
.aq-btn-sm{padding:1px 7px;font-size:11px}
.aq-tabs{display:flex;flex-wrap:wrap;gap:4px;padding:8px 12px 0;border-bottom:1px solid var(--dsw-alias-border-l1)}
.aq-tab{padding:6px 12px;border-radius:6px 6px 0 0;cursor:pointer;font-size:12.5px;color:var(--dsw-alias-label-secondary);
  display:inline-flex;align-items:center;gap:5px;border:1px solid transparent;border-bottom:none;background:none;font-family:inherit}
.aq-tab:hover{color:var(--dsw-alias-label-primary)}
.aq-tab[data-active="1"]{background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);
  border-color:var(--dsw-alias-border-l1);font-weight:600}
.aq-badge{background:var(--dsw-alias-brand-primary);color:#fff;border-radius:999px;font-size:10px;padding:0 5px;line-height:15px}
.aq-body{flex:1;overflow:auto;padding:12px}
.aq-foot{border-top:1px solid var(--dsw-alias-border-l1);padding:6px 12px;font-size:11px;color:var(--dsw-alias-label-secondary);
  display:flex;gap:10px;align-items:center}
.aq-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:12px}
.aq-kpi{border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:8px 10px;background:var(--dsw-alias-bg-layer-2)}
.aq-kpi-label{font-size:11px;color:var(--dsw-alias-label-secondary);margin-bottom:2px}
.aq-kpi-value{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.aq-kpi-sub{font-size:11px;color:var(--dsw-alias-label-secondary);margin-top:2px}
.aq-card{border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:10px;margin-bottom:12px;background:var(--dsw-alias-bg-layer-2)}
.aq-card-title{font-size:12px;font-weight:600;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.aq-sec{font-size:12px;font-weight:600;margin:14px 0 6px;color:var(--dsw-alias-label-primary)}
.aq-table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.aq-table th{text-align:left;font-size:11px;font-weight:600;color:var(--dsw-alias-label-secondary);
  padding:4px 6px;border-bottom:1px solid var(--dsw-alias-border-l1);white-space:nowrap}
.aq-table td{padding:4px 6px;border-bottom:1px solid var(--dsw-alias-border-l1);font-size:12px}
.aq-table tr:hover td{background:var(--dsw-alias-bg-layer-1)}
.aq-num{text-align:right}
.aq-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
.aq-up{color:#e5484d}
.aq-down{color:#26be78}
.aq-flat{color:var(--dsw-alias-label-secondary)}
.aq-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.aq-input,.aq-select{border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-base);color:inherit;
  border-radius:6px;padding:3px 7px;font-size:12px;font-family:inherit;min-width:0}
.aq-input{width:110px}
.aq-input-wide{width:150px}
.aq-list{list-style:none;margin:0;padding:0}
.aq-list li{padding:5px 0;border-bottom:1px solid var(--dsw-alias-border-l1);font-size:12px;display:flex;gap:8px;align-items:center}
.aq-list li:last-child{border-bottom:none}
.aq-empty{color:var(--dsw-alias-label-secondary);font-size:12px;padding:10px 0;text-align:center}
.aq-warn{color:#e5a13c}
.aq-err{color:#e5484d}
.aq-ok{color:#26be78}
.aq-toast{position:absolute;left:12px;bottom:44px;right:12px;border:1px solid var(--dsw-alias-border-l1);
  background:var(--dsw-alias-bg-layer-2);border-radius:8px;padding:7px 10px;font-size:12px;box-shadow:0 6px 20px rgba(0,0,0,.16)}
.aq-full{position:fixed;inset:0;width:100%;height:100%;border-radius:0}
.aq-full div:has(> [data-shell-overlay] .aq-dock[data-open="1"]){padding-right:0}
.aq-tag{font-size:10.5px;border:1px solid var(--dsw-alias-border-l1);border-radius:4px;padding:0 4px;color:var(--dsw-alias-label-secondary)}
.aq-icon-btn{border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-2);color:inherit;border-radius:6px;
  width:26px;height:24px;line-height:1;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;font-size:13px}
.aq-icon-btn:hover{border-color:var(--dsw-alias-border-l2)}
.aq-side-btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--dsw-alias-border-l1);
  background:var(--dsw-alias-bg-layer-2);color:inherit;border-radius:6px;padding:4px 9px;font-size:12px;cursor:pointer;font-family:inherit}
.aq-grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media (max-width:760px){.aq-grid2{grid-template-columns:1fr}.aq-dock{width:min(var(--aq-dock-w),100vw)}}
`

    /* ────────────────────────────────── 桥接与状态 ────────────────────────────────── */

    const isFullWindow = () => {
      try { return String(window.location.hash || '') === '#aiquant-full' } catch { return false }
    }

    const api = (req) => fetch('/aiquant/api', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(req),
    }).then((res) => res.json()).then((json) => {
      if (json && json.ok === false) throw new Error(json.error || '接口返回失败')
      return json && json.data !== undefined ? json.data : json
    })

    let ui = { open: true, tab: 'overview' }
    const subs = new Set()
    const setUI = (patch) => {
      ui = Object.assign({}, ui, patch)
      for (const f of Array.from(subs)) f()
    }
    let dockWidthTag = null
    const setDockWidth = (px) => { if (dockWidthTag) dockWidthTag.textContent = ':root{--aq-dock-w:' + px + 'px}' }
    const useUI = () => {
      const [, force] = React.useReducer((x) => x + 1, 0)
      React.useEffect(() => {
        const f = () => force()
        subs.add(f)
        return () => { subs.delete(f) }
      }, [])
      return ui
    }

    /* ────────────────────────────────── 小部件 ────────────────────────────────── */

    const fmtNum = (value, digits = 2) => (value === null || value === undefined || Number.isNaN(Number(value))
      ? '—' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: digits }))
    const fmtPct = (value, digits = 2) => (value === null || value === undefined || Number.isNaN(Number(value))
      ? '—' : (Number(value) >= 0 ? '+' : '') + Number(value).toFixed(digits) + '%')
    const pctClass = (value) => (value === null || value === undefined ? 'aq-flat' : Number(value) > 0 ? 'aq-up' : Number(value) < 0 ? 'aq-down' : 'aq-flat')
    const fmtTime = (ts) => (typeof ts === 'string' && ts.length >= 16 ? ts.slice(5, 16).replace('T', ' ') : (ts || '—'))

    function Kpi(props) {
      return h('div', { className: 'aq-kpi' },
        h('div', { className: 'aq-kpi-label' }, props.label),
        h('div', { className: 'aq-kpi-value ' + (props.tone || '') }, props.value),
        props.sub === undefined ? null : h('div', { className: 'aq-kpi-sub' }, props.sub))
    }

    /** 迷你走势（SVG polyline，自动配色）。 */
    function Sparkline(props) {
      const data = (props.data || []).filter((v) => typeof v === 'number' && Number.isFinite(v))
      const width = props.width || 96
      const height = props.height || 24
      if (data.length < 2) return h('span', { className: 'aq-flat', style: { fontSize: 11 } }, '—')
      const min = Math.min(...data)
      const max = Math.max(...data)
      const span = max - min || 1
      const step = width / (data.length - 1)
      const points = data.map((v, i) => (i * step).toFixed(2) + ',' + (height - ((v - min) / span) * (height - 3) - 1.5).toFixed(2)).join(' ')
      const color = data[data.length - 1] >= data[0] ? '#e5484d' : '#26be78'
      return h('svg', { width, height, viewBox: '0 0 ' + width + ' ' + height, style: { display: 'block' } },
        h('polyline', { points, fill: 'none', stroke: color, strokeWidth: 1.4, strokeLinejoin: 'round', strokeLinecap: 'round' }))
    }

    /** 净值/市值曲线（带面积与最大回撤标注）。 */
    function EquityChart(props) {
      const points = (props.points || []).filter((p) => typeof p.nav === 'number' && Number.isFinite(p.nav))
      const height = props.height || 140
      const width = 1000
      if (points.length < 2) {
        return h('div', { className: 'aq-empty' }, '净值点不足（' + points.length + ' 个）：有持仓后引擎每 15 分钟自动记一个点，也可用 aiquant_equity 手工写入')
      }
      const navs = points.map((p) => p.nav)
      const min = Math.min(...navs)
      const max = Math.max(...navs)
      const span = max - min || 1
      const step = width / (points.length - 1)
      const y = (v) => height - 8 - ((v - min) / span) * (height - 20)
      const line = points.map((p, i) => (i * step).toFixed(2) + ',' + y(p.nav).toFixed(2)).join(' ')
      const area = '0,' + (height - 8) + ' ' + line + ' ' + width + ',' + (height - 8)
      /* 最大回撤：峰谷标注 */
      let peak = navs[0]
      let peakIdx = 0
      let maxDd = 0
      let ddFrom = 0
      let ddTo = 0
      for (let i = 0; i < navs.length; i += 1) {
        if (navs[i] > peak) { peak = navs[i]; peakIdx = i }
        const dd = peak > 0 ? (navs[i] / peak - 1) : 0
        if (dd < maxDd) { maxDd = dd; ddFrom = peakIdx; ddTo = i }
      }
      const rising = navs[navs.length - 1] >= navs[0]
      const color = rising ? '#e5484d' : '#26be78'
      const first = points[0]
      const last = points[points.length - 1]
      return h('div', null,
        h('svg', { viewBox: '0 0 ' + width + ' ' + height, preserveAspectRatio: 'none', style: { width: '100%', height: height + 'px', display: 'block' } },
          h('polygon', { points: area, fill: color, opacity: 0.08 }),
          maxDd < 0 ? h('line', {
            x1: (ddFrom * step).toFixed(1), y1: 6, x2: (ddFrom * step).toFixed(1), y2: height - 8,
            stroke: '#e5a13c', strokeWidth: 1, strokeDasharray: '3 3',
          }) : null,
          maxDd < 0 ? h('line', {
            x1: (ddTo * step).toFixed(1), y1: 6, x2: (ddTo * step).toFixed(1), y2: height - 8,
            stroke: '#e5a13c', strokeWidth: 1, strokeDasharray: '3 3',
          }) : null,
          h('polyline', { points: line, fill: 'none', stroke: color, strokeWidth: 1.6, vectorEffect: 'non-scaling-stroke' })),
        h('div', { className: 'aq-row', style: { justifyContent: 'space-between', fontSize: 11, color: 'var(--dsw-alias-label-secondary)' } },
          h('span', null, points.length + ' 点 · ' + fmtTime(first.ts) + ' → ' + fmtTime(last.ts)),
          h('span', null, '起 ' + fmtNum(first.nav) + ' → 末 ' + fmtNum(last.nav) + '（' + fmtPct(((last.nav / first.nav) - 1) * 100) + '）'),
          maxDd < 0 ? h('span', { className: 'aq-warn' }, '最大回撤 ' + (maxDd * 100).toFixed(2) + '%') : h('span', { className: 'aq-ok' }, '无回撤')))
    }

    /** 统一的数据加载 hook（轮询 + 页面隐藏时暂停）。 */
    function useSnapshot(intervalMs) {
      const [state, setState] = React.useState({ snap: null, error: null, loading: true, at: null })
      const load = React.useCallback(async (force) => {
        try {
          const data = await api({ op: 'snapshot' })
          setState({ snap: data, error: null, loading: false, at: new Date().toLocaleTimeString('zh-CN') })
        } catch (error) {
          setState((prev) => ({ snap: prev.snap, error: error && error.message ? error.message : String(error), loading: false, at: prev.at }))
        }
        void force
      }, [])
      React.useEffect(() => {
        let alive = true
        const tick = () => { if (alive && document.visibilityState !== 'hidden') void load() }
        void load()
        const timer = setInterval(tick, Math.max(2000, Number(intervalMs) || 5000))
        const onVisible = () => { if (document.visibilityState === 'visible') void load() }
        document.addEventListener('visibilitychange', onVisible)
        return () => { alive = false; clearInterval(timer); document.removeEventListener('visibilitychange', onVisible) }
      }, [intervalMs, load])
      return [state, load]
    }

    /* ────────────────────────────────── 页签视图 ────────────────────────────────── */

    function Overview(props) {
      const snap = props.snap
      const totals = snap.totals || {}
      const metrics = snap.metrics || {}
      const signals = snap.signals || []
      const watchlist = (snap.watchlist || []).slice(0, 8)
      const openAlerts = (snap.alerts || []).filter((a) => !a.ack).length
      const riskTone = metrics.samples < 20 ? '' : (metrics.max_drawdown_pct !== null && metrics.max_drawdown_pct > 25 ? 'aq-err' : 'aq-ok')
      return h('div', null,
        h('div', { className: 'aq-kpis' },
          h(Kpi, { label: '组合市值', value: fmtNum(totals.market_value), sub: '成本 ' + fmtNum(totals.cost) }),
          h(Kpi, {
            label: '浮动盈亏', value: fmtNum(totals.pnl),
            tone: pctClass(totals.pnl), sub: totals.pnl_pct === null ? '—' : fmtPct(totals.pnl_pct),
          }),
          h(Kpi, { label: '持仓 / 自选', value: (totals.count || 0) + ' / ' + (snap.watchlist || []).length, sub: totals.count > 0 ? '最大单票 ' + fmtNum(totals.max_weight) + '%' : '未建仓' }),
          h(Kpi, { label: '信号（库内）', value: snap.stats ? snap.stats.signals : signals.length, sub: signals.length > 0 ? '最新 ' + fmtTime(signals[0].ts) : '暂无' }),
          h(Kpi, {
            label: '最大回撤', tone: riskTone,
            value: metrics.samples < 20 ? '样本不足' : fmtNum(metrics.max_drawdown_pct) + '%',
            sub: metrics.samples < 20 ? '净值点 ' + metrics.samples + '/20' : '夏普 ' + fmtNum(metrics.sharpe),
          }),
          h(Kpi, { label: '告警', value: openAlerts, tone: openAlerts > 0 ? 'aq-warn' : '', sub: snap.config && snap.config.market_session ? snap.config.market_session.label : '' })),
        h('div', { className: 'aq-card' },
          h('div', { className: 'aq-card-title' }, '持仓市值 / 净值曲线'),
          h(EquityChart, { points: snap.equity || [] })),
        h('div', { className: 'aq-sec' }, '自选迷你走势'),
        h('div', { className: 'aq-card' },
          h('table', { className: 'aq-table' },
            h('thead', null, h('tr', null,
              h('th', null, '标的'), h('th', null, '名称'), h('th', { className: 'aq-num' }, '现价'),
              h('th', { className: 'aq-num' }, '涨跌幅'), h('th', { style: { width: 100 } }, '分时'), h('th', null, '更新时间'))),
            h('tbody', null, watchlist.map((w) => h('tr', { key: w.symbol },
              h('td', { className: 'aq-mono' }, w.symbol),
              h('td', null, w.name || '—'),
              h('td', { className: 'aq-num' }, fmtNum(w.price, 3)),
              h('td', { className: 'aq-num ' + pctClass(w.pct) }, fmtPct(w.pct)),
              h('td', null, h(Sparkline, { data: w.spark, width: 92, height: 20 })),
              h('td', { className: 'aq-flat' }, fmtTime(w.ts) + (w.stale ? ' ·缓存' : ''))))))),
        h('div', { className: 'aq-sec' }, '最新信号'),
        h('div', { className: 'aq-card' },
          signals.length === 0
            ? h('div', { className: 'aq-empty' }, '暂无信号 —— 用「信号」页的「扫描自选」按日线规则跑一遍')
            : h('ul', { className: 'aq-list' }, signals.slice(0, 8).map((s) => h('li', { key: s.id },
              h('span', { className: 'aq-mono', style: { width: 92, flex: 'none' } }, fmtTime(s.ts)),
              h('span', { className: 'aq-mono', style: { width: 96, flex: 'none' } }, s.symbol),
              h('span', { className: 'aq-tag' }, s.kind_label || s.kind),
              h('span', { className: s.direction === 'buy' ? 'aq-up' : 'aq-down' }, s.direction === 'buy' ? '买' : '卖'),
              h('span', { style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, s.reason || ''))))))
    }

    function Quotes(props) {
      const snap = props.snap
      const [symbol, setSymbol] = React.useState('')
      const [name, setName] = React.useState('')
      const list = snap.watchlist || []
      const add = async () => {
        if (symbol.trim() === '') return
        try {
          await api({ op: 'watchlist.add', symbol: symbol.trim(), name: name.trim() || undefined })
          props.toast('已加入自选：' + symbol.trim())
          setSymbol(''); setName('')
          await props.reload()
        } catch (error) { props.toast('加入失败：' + error.message, true) }
      }
      const remove = async (s) => {
        try { await api({ op: 'watchlist.remove', symbol: s }); props.toast('已移除 ' + s); await props.reload() } catch (error) { props.toast('移除失败：' + error.message, true) }
      }
      const refresh = async () => {
        try { await api({ op: 'quotes', force: true }); await props.reload(); props.toast('行情已刷新') } catch (error) { props.toast('刷新失败：' + error.message, true) }
      }
      return h('div', null,
        h('div', { className: 'aq-card' },
          h('div', { className: 'aq-card-title' }, '加入自选（600519 / sh600519 / 000001.SZ / 00700 / us.AAPL / BTC-USDT）'),
          h('div', { className: 'aq-row' },
            h('input', { className: 'aq-input', placeholder: '代码', value: symbol, onChange: (e) => setSymbol(e.target.value) }),
            h('input', { className: 'aq-input', placeholder: '名称（可选）', value: name, onChange: (e) => setName(e.target.value) }),
            h('button', { className: 'aq-btn aq-btn-primary', onClick: add }, '加入'),
            h('button', { className: 'aq-btn', onClick: refresh }, '刷新行情'))),
        h('table', { className: 'aq-table' },
          h('thead', null, h('tr', null,
            h('th', null, '标的'), h('th', null, '名称'), h('th', { className: 'aq-num' }, '现价'),
            h('th', { className: 'aq-num' }, '涨跌幅'), h('th', { className: 'aq-num' }, '最高'), h('th', { className: 'aq-num' }, '最低'),
            h('th', { className: 'aq-num' }, '成交额'), h('th', null, '分时'), h('th', null, '更新'), h('th', null, ''))),
          h('tbody', null, list.length === 0
            ? h('tr', null, h('td', { colSpan: 10, className: 'aq-empty' }, '自选为空'))
            : list.map((w) => h('tr', { key: w.symbol },
              h('td', { className: 'aq-mono' }, w.symbol),
              h('td', null, w.name || '—'),
              h('td', { className: 'aq-num' }, fmtNum(w.price, 3)),
              h('td', { className: 'aq-num ' + pctClass(w.pct) }, fmtPct(w.pct)),
              h('td', { className: 'aq-num' }, fmtNum(w.high, 2)),
              h('td', { className: 'aq-num' }, fmtNum(w.low, 2)),
              h('td', { className: 'aq-num' }, w.amount === null || w.amount === undefined ? '—' : (w.amount >= 1e8 ? (w.amount / 1e8).toFixed(2) + '亿' : (w.amount / 1e4).toFixed(0) + '万')),
              h('td', null, h(Sparkline, { data: w.spark, width: 84, height: 18 })),
              h('td', { className: 'aq-flat' }, fmtTime(w.ts) + (w.stale ? ' ·缓存' : '')),
              h('td', null, h('button', { className: 'aq-btn aq-btn-sm', onClick: () => remove(w.symbol) }, '移除')))))))
    }

    function Signals(props) {
      const snap = props.snap
      const signals = snap.signals || []
      const scan = async () => {
        props.toast('正在扫描自选（拉日线 + 算指标 + 落信号）…')
        try {
          const data = await api({ op: 'signal.scan', force: true })
          props.toast('扫描完成：' + data.scanned + ' 个标的，新增 ' + (data.signals || []).length + ' 条信号' + ((data.errors || []).length ? '，' + data.errors.length + ' 个告警' : ''))
          await props.reload()
        } catch (error) { props.toast('扫描失败：' + error.message, true) }
      }
      const clear = async () => {
        if (!window.confirm('清空全部信号记录？')) return
        try { await api({ op: 'signal.clear' }); await props.reload(); props.toast('已清空信号') } catch (error) { props.toast('清空失败：' + error.message, true) }
      }
      return h('div', null,
        h('div', { className: 'aq-row', style: { marginBottom: 10 } },
          h('button', { className: 'aq-btn aq-btn-primary', onClick: scan }, '扫描自选'),
          h('span', { className: 'aq-pill' }, signals.length + ' 条（最近 ' + Math.min(signals.length, 80) + '）'),
          h('button', { className: 'aq-btn', onClick: clear }, '清空')),
        h('table', { className: 'aq-table' },
          h('thead', null, h('tr', null,
            h('th', null, '时间'), h('th', null, '标的'), h('th', null, '类型'), h('th', null, '方向'),
            h('th', { className: 'aq-num' }, '强度'), h('th', { className: 'aq-num' }, '价格'), h('th', null, '依据'))),
          h('tbody', null, signals.length === 0
            ? h('tr', null, h('td', { colSpan: 7, className: 'aq-empty' }, '暂无信号'))
            : signals.map((s) => h('tr', { key: s.id },
              h('td', { className: 'aq-mono' }, fmtTime(s.ts)),
              h('td', { className: 'aq-mono' }, s.symbol + (s.name ? ' ' + s.name : '')),
              h('td', null, h('span', { className: 'aq-tag' }, s.kind_label || s.kind)),
              h('td', { className: s.direction === 'buy' ? 'aq-up' : 'aq-down' }, s.direction === 'buy' ? '买入' : '卖出'),
              h('td', { className: 'aq-num' }, s.strength === null ? '—' : fmtNum(s.strength)),
              h('td', { className: 'aq-num' }, fmtNum(s.price, 3)),
              h('td', null, s.reason || ''))))))
    }

    function Positions(props) {
      const snap = props.snap
      const [symbol, setSymbol] = React.useState('')
      const [qty, setQty] = React.useState('')
      const [cost, setCost] = React.useState('')
      const open = (snap.positions || [])
      const add = async () => {
        try {
          await api({ op: 'position.add', symbol: symbol.trim(), qty: Number(qty), avg_cost: Number(cost) })
          props.toast('已建仓 ' + symbol.trim())
          setSymbol(''); setQty(''); setCost('')
          await props.reload()
        } catch (error) { props.toast('建仓失败：' + error.message, true) }
      }
      const close = async (p) => {
        const price = window.prompt('平仓价（' + p.symbol + '，成本 ' + p.avg_cost + '）', String(p.price))
        if (price === null) return
        try {
          const data = await api({ op: 'position.close', id: p.id, price: Number(price) })
          props.toast('已平仓，已实现盈亏 ' + fmtNum(data.realized_pnl) + (data.realized_pct === null ? '' : '（' + fmtPct(data.realized_pct) + '）'))
          await props.reload()
        } catch (error) { props.toast('平仓失败：' + error.message, true) }
      }
      return h('div', null,
        h('div', { className: 'aq-card' },
          h('div', { className: 'aq-card-title' }, '建仓（落库后才会进风控与报告）'),
          h('div', { className: 'aq-row' },
            h('input', { className: 'aq-input', placeholder: '代码', value: symbol, onChange: (e) => setSymbol(e.target.value) }),
            h('input', { className: 'aq-input', placeholder: '数量', value: qty, onChange: (e) => setQty(e.target.value) }),
            h('input', { className: 'aq-input', placeholder: '成本价', value: cost, onChange: (e) => setCost(e.target.value) }),
            h('button', { className: 'aq-btn aq-btn-primary', onClick: add }, '建仓'))),
        h('div', { className: 'aq-kpis' },
          h(Kpi, { label: '持仓数', value: snap.totals.count }),
          h(Kpi, { label: '市值', value: fmtNum(snap.totals.market_value) }),
          h(Kpi, { label: '浮盈', value: fmtNum(snap.totals.pnl), tone: pctClass(snap.totals.pnl), sub: snap.totals.pnl_pct === null ? '—' : fmtPct(snap.totals.pnl_pct) }),
          h(Kpi, { label: '集中度 HHI', value: fmtNum(snap.totals.hhi, 0), tone: snap.totals.concentrated ? 'aq-warn' : '' })),
        h('table', { className: 'aq-table' },
          h('thead', null, h('tr', null,
            h('th', null, '标的'), h('th', null, '名称'), h('th', { className: 'aq-num' }, '数量'),
            h('th', { className: 'aq-num' }, '成本'), h('th', { className: 'aq-num' }, '现价'),
            h('th', { className: 'aq-num' }, '市值'), h('th', { className: 'aq-num' }, '浮盈'),
            h('th', { className: 'aq-num' }, '盈亏%'), h('th', { className: 'aq-num' }, '权重'), h('th', null, '策略'), h('th', null, ''))),
          h('tbody', null, open.length === 0
            ? h('tr', null, h('td', { colSpan: 11, className: 'aq-empty' }, '无持仓'))
            : open.map((p) => h('tr', { key: p.id },
              h('td', { className: 'aq-mono' }, p.symbol),
              h('td', null, p.name || '—'),
              h('td', { className: 'aq-num' }, fmtNum(p.qty, 4)),
              h('td', { className: 'aq-num' }, fmtNum(p.avg_cost, 3)),
              h('td', { className: 'aq-num' }, fmtNum(p.price, 3) + (p.price_stale ? '*' : '')),
              h('td', { className: 'aq-num' }, fmtNum(p.market_value)),
              h('td', { className: 'aq-num ' + pctClass(p.pnl) }, fmtNum(p.pnl)),
              h('td', { className: 'aq-num ' + pctClass(p.pnl_pct) }, fmtPct(p.pnl_pct)),
              h('td', { className: 'aq-num' }, fmtNum(p.weight) + '%'),
              h('td', { className: 'aq-flat' }, p.strategy || '—'),
              h('td', null, h('button', { className: 'aq-btn aq-btn-sm', onClick: () => close(p) }, '平仓')))))))
    }

    function Backtests(props) {
      const list = props.snap.backtests || []
      const fmtMetric = (metrics, key, suffix) => {
        const value = metrics === null || metrics === undefined ? undefined : metrics[key]
        return value === undefined || value === null ? '—' : (typeof value === 'number' ? fmtNum(value) : String(value)) + (suffix || '')
      }
      return h('div', null,
        h('div', { className: 'aq-row', style: { marginBottom: 10 } },
          h('span', { className: 'aq-pill' }, '共 ' + list.length + ' 条'),
          h('span', { className: 'aq-flat', style: { fontSize: 11 } }, '口径要求：含手续费/滑点/成交时点 + 样本外结果')),
        h('table', { className: 'aq-table' },
          h('thead', null, h('tr', null,
            h('th', null, '时间'), h('th', null, '名称'), h('th', null, '策略'), h('th', null, '标的'),
            h('th', null, '区间'), h('th', { className: 'aq-num' }, '年化'), h('th', { className: 'aq-num' }, '回撤'),
            h('th', { className: 'aq-num' }, '夏普'), h('th', null, '样本外'), h('th', null, '成本模型'))),
          h('tbody', null, list.length === 0
            ? h('tr', null, h('td', { colSpan: 10, className: 'aq-empty' }, '暂无回测记录 —— 让智能体用 q_backtest_ma 跑完再 aiquant_backtest 落库'))
            : list.map((b) => h('tr', { key: b.id },
              h('td', { className: 'aq-mono' }, fmtTime(b.ts)),
              h('td', null, b.name),
              h('td', { className: 'aq-flat' }, b.strategy || '—'),
              h('td', { className: 'aq-mono' }, (b.symbols || []).join(',')),
              h('td', { className: 'aq-mono' }, (b.start_date || '') + '→' + (b.end_date || '')),
              h('td', { className: 'aq-num ' + pctClass(b.metrics && b.metrics.annual_return_pct) }, fmtMetric(b.metrics, 'annual_return_pct', '%')),
              h('td', { className: 'aq-num' }, fmtMetric(b.metrics, 'max_drawdown_pct', '%')),
              h('td', { className: 'aq-num' }, fmtMetric(b.metrics, 'sharpe')),
              h('td', null, b.out_of_sample ? h('span', { className: 'aq-ok' }, '是') : h('span', { className: 'aq-warn' }, '否')),
              h('td', { className: 'aq-flat', style: { fontSize: 11 } }, b.cost_model || '未标注'))))))
    }

    function Risk(props) {
      const snap = props.snap
      const m = snap.metrics || {}
      const r = props.risk
      const metricsRow = [
        ['区间收益', m.total_return_pct, '%'],
        ['最大回撤', m.max_drawdown_pct, '%'],
        ['当前回撤', m.current_drawdown_pct, '%'],
        ['年化波动', m.volatility_pct, '%'],
        ['夏普', m.sharpe, ''],
        ['卡玛', m.calmar, ''],
        ['VaR95(日)', m.var95_pct, '%'],
        ['ES95(日)', m.es95_pct, '%'],
        ['胜率', m.win_rate_pct, '%'],
      ]
      return h('div', null,
        h('div', { className: 'aq-row', style: { marginBottom: 10 } },
          h('button', { className: 'aq-btn', onClick: async () => {
            try { props.setRisk(await api({ op: 'risk.get' })); props.toast('风控已重算') } catch (error) { props.toast('重算失败：' + error.message, true) }
          } }, '重算风控'),
          r === null ? null : (r.passed
            ? h('span', { className: 'aq-ok' }, '✅ 红线全部通过')
            : h('span', { className: 'aq-err' }, '⛔ 不通过：' + r.breaches.length + ' 项越线')),
          h('span', { className: 'aq-pill' }, '净值点 ' + (m.samples || 0))),
        m.samples < 20 ? h('div', { className: 'aq-card aq-warn' }, '净值样本 ' + m.samples + '/20：回撤、波动、夏普、VaR 统计量**不可用**，先积累净值点（有持仓时引擎每 15 分钟自动记一个）') : null,
        h('div', { className: 'aq-kpis' }, metricsRow.map(([label, value, suffix]) => h(Kpi, {
          key: label, label, value: value === null || value === undefined ? '—' : fmtNum(value) + suffix,
          tone: label === '最大回撤' && value !== null && value > 25 ? 'aq-err' : '',
        }))),
        r === null ? null : h('div', { className: 'aq-card' },
          h('div', { className: 'aq-card-title' }, '红线判定（单票 ≤ ' + r.limits.max_single_weight + '% · 回撤 ≤ ' + r.limits.max_drawdown + '% · 持仓数 ≥ ' + r.limits.min_positions + ' · HHI ≤ ' + r.limits.max_hhi + ' · 相关性 ≤ ' + r.limits.max_corr + '）'),
          r.breaches.length === 0
            ? h('div', { className: 'aq-ok' }, '无越线项')
            : h('ul', { className: 'aq-list' }, r.breaches.map((b, i) => h('li', { key: i },
              h('span', { className: 'aq-err' }, '⛔'),
              h('span', { style: { flex: 1 } }, b.text),
              h('span', { className: 'aq-mono' }, '实测 ' + b.value + ' / 限 ' + b.limit))))),
        r === null || r.correlation.length === 0
          ? h('div', { className: 'aq-empty' }, '相关性矩阵：样本不足（需要 ≥30 根日线与 ≥2 只持仓）')
          : h('div', { className: 'aq-card' },
            h('div', { className: 'aq-card-title' }, '持仓相关性（日线收益率，|r| ≥ 0.8 视为高相关）'),
            h('table', { className: 'aq-table' },
              h('thead', null, h('tr', null, h('th', null, 'A'), h('th', null, 'B'), h('th', { className: 'aq-num' }, 'r'), h('th', null, '程度'))),
              h('tbody', null, r.correlation.map((c, i) => h('tr', { key: i },
                h('td', { className: 'aq-mono' }, c.a), h('td', { className: 'aq-mono' }, c.b),
                h('td', { className: 'aq-num' }, fmtNum(c.r, 3)),
                h('td', { className: c.level === 'high' ? 'aq-err' : c.level === 'mid' ? 'aq-warn' : 'aq-ok' }, c.level === 'high' ? '高' : c.level === 'mid' ? '中' : '低'))))),
            h('div', { className: 'aq-flat', style: { fontSize: 11, marginTop: 6 } }, '注意：相关性是历史统计，不等于风险预算；高相关持仓应合并看待。')))
    }

    function DataView(props) {
      const snap = props.snap
      const env = props.env
      const stats = snap.stats || {}
      const notes = snap.notes || []
      const runs = snap.runs || []
      return h('div', null,
        h('div', { className: 'aq-row', style: { marginBottom: 10 } },
          h('button', { className: 'aq-btn', onClick: async () => {
            props.toast('正在探测数据源…')
            try { props.setEnv(await api({ op: 'env', probe: true })); props.toast('探测完成') } catch (error) { props.toast('探测失败：' + error.message, true) }
          } }, '数据源探测'),
          h('button', { className: 'aq-btn', onClick: async () => {
            try { const data = await api({ op: 'report.md', save: true }); props.toast('报告已写出：' + data.path) } catch (error) { props.toast('生成失败：' + error.message, true) }
          } }, '导出日报')),
        h('div', { className: 'aq-card' },
          h('div', { className: 'aq-card-title' }, '事实库'),
          h('div', { className: 'aq-kpis' },
            h(Kpi, { label: '自选', value: stats.watchlist }),
            h(Kpi, { label: '持仓(开)', value: stats.positions_open }),
            h(Kpi, { label: '信号', value: stats.signals }),
            h(Kpi, { label: '回测', value: stats.backtests }),
            h(Kpi, { label: '净值点', value: stats.equity_points }),
            h(Kpi, { label: 'K线行', value: stats.klines }),
            h(Kpi, { label: '库大小', value: Math.round((stats.db_bytes || 0) / 1024) + ' KB' }),
            h(Kpi, { label: 'schema', value: 'v' + stats.schema_version }))),
        env === null ? null : h('div', { className: 'aq-card' },
          h('div', { className: 'aq-card-title' }, '环境自检'),
          h('ul', { className: 'aq-list' },
            h('li', null, h('span', { style: { width: 110, flex: 'none' } }, '工作区'), h('span', { className: 'aq-mono' }, env.workspace)),
            h('li', null, h('span', { style: { width: 110, flex: 'none' } }, '事实库'), h('span', { className: 'aq-mono' }, env.db_file)),
            h('li', null, h('span', { style: { width: 110, flex: 'none' } }, '行情引擎'), h('span', { className: env.modules.feed.ok ? 'aq-ok' : 'aq-err' }, env.modules.feed.ok ? '在跑（轮询 ' + env.modules.feed.interval_ms + 'ms，跟踪 ' + env.modules.feed.tracked + ' 个）' : env.modules.feed.error)),
            h('li', null, h('span', { style: { width: 110, flex: 'none' } }, 'MCP 服务器'), h('span', { className: env.modules.mcp_server.ok ? 'aq-ok' : 'aq-warn' }, env.modules.mcp_server.ok ? '已就位' : '缺失')),
            h('li', null, h('span', { style: { width: 110, flex: 'none' } }, '系统代理'), h('span', { className: 'aq-mono' }, env.proxy === null ? '未配置' : env.proxy.host + ':' + env.proxy.port + (env.proxy.reachable ? '（可连）' : '（不通）'))),
            env.sources === undefined ? null : env.sources.map((s) => h('li', { key: s.name },
              h('span', { style: { width: 220, flex: 'none' } }, s.name),
              h('span', { className: s.ok ? 'aq-ok' : 'aq-err' }, (s.ok ? '通 ' : '不通 ') + s.ms + 'ms'),
              h('span', { className: 'aq-flat', style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, s.detail))))),
        h('div', { className: 'aq-grid2' },
          h('div', { className: 'aq-card' },
            h('div', { className: 'aq-card-title' }, '研究笔记'),
            notes.length === 0 ? h('div', { className: 'aq-empty' }, '暂无笔记') : h('ul', { className: 'aq-list' },
              notes.map((n) => h('li', { key: n.id },
                h('span', { className: 'aq-tag' }, n.kind),
                h('span', { style: { flex: 1 } }, n.title),
                h('span', { className: 'aq-mono' }, fmtTime(n.ts)))))),
          h('div', { className: 'aq-card' },
            h('div', { className: 'aq-card-title' }, '运行日志'),
            runs.length === 0 ? h('div', { className: 'aq-empty' }, '暂无日志') : h('ul', { className: 'aq-list' },
              runs.slice(0, 12).map((r) => h('li', { key: r.id },
                h('span', { className: 'aq-mono', style: { width: 88, flex: 'none' } }, fmtTime(r.ts)),
                h('span', { className: r.status === 'error' ? 'aq-err' : r.status === 'warn' ? 'aq-warn' : 'aq-ok', style: { width: 54, flex: 'none' } }, r.status),
                h('span', { style: { flex: 1, fontSize: 11.5 } }, r.detail || r.kind)))))))
    }

    /* ────────────────────────────────── 面板 ────────────────────────────────── */

    function Panel() {
      const state = useUI()
      const [snapshot, reload] = useSnapshot(5000)
      const [risk, setRisk] = React.useState(null)
      const [env, setEnv] = React.useState(null)
      const [toast, setToast] = React.useState(null)
      const dockRef = React.useRef(null)
      const toastTimer = React.useRef(null)
      const showToast = React.useCallback((message, isError) => {
        setToast({ message, isError: isError === true })
        if (toastTimer.current !== null) clearTimeout(toastTimer.current)
        toastTimer.current = setTimeout(() => setToast(null), 4200)
      }, [])
      React.useEffect(() => () => { if (toastTimer.current !== null) clearTimeout(toastTimer.current) }, [])
      React.useEffect(() => {
        if (state.tab !== 'risk' || risk !== null) return undefined
        let alive = true
        api({ op: 'risk.get' }).then((data) => { if (alive) setRisk(data) }).catch(() => { /* 稍后重算 */ })
        return () => { alive = false }
      }, [state.tab, risk])

      const snap = snapshot.snap
      const session = snap && snap.config && snap.config.market_session ? snap.config.market_session : null
      const tabs = [
        ['overview', '概览'], ['quotes', '行情'], ['signals', '信号'], ['positions', '持仓'],
        ['backtests', '回测'], ['risk', '风控'], ['data', '数据'],
      ]
      const props = { snap, reload, toast: showToast, risk, setRisk, env, setEnv }

      const body = snap === null
        ? h('div', { className: 'aq-empty' }, snapshot.error === null ? '加载中…' : ('看板接口不可用：' + snapshot.error + '（检查 dsh-aiquant-mode/ui 行是否加载）'))
        : state.tab === 'quotes' ? h(Quotes, props)
          : state.tab === 'signals' ? h(Signals, props)
            : state.tab === 'positions' ? h(Positions, props)
              : state.tab === 'backtests' ? h(Backtests, props)
                : state.tab === 'risk' ? h(Risk, props)
                  : state.tab === 'data' ? h(DataView, props)
                    : h(Overview, props)

      const onGripDown = (event) => {
        event.preventDefault()
        const startX = event.clientX
        const startWidth = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--aq-dock-w'), 10) || 600
        const onMove = (moveEvent) => {
          const next = Math.max(360, Math.min(1100, startWidth - (moveEvent.clientX - startX)))
          setDockWidth(next)
        }
        const onUp = () => {
          document.removeEventListener('mousemove', onMove)
          document.removeEventListener('mouseup', onUp)
        }
        document.addEventListener('mousemove', onMove)
        document.addEventListener('mouseup', onUp)
      }

      return h('div', {
        className: 'aq-dock' + (isFullWindow() ? ' aq-full' : ''),
        ref: dockRef,
        'data-open': state.open ? '1' : '0',
        style: isFullWindow() ? { position: 'fixed', inset: 0, width: '100%', transform: 'none' } : null,
      },
        h('div', { className: 'aq-grip', onMouseDown: onGripDown, title: '拖动调整宽度' }),
        h('div', { className: 'aq-head' },
          h('div', { className: 'aq-title' },
            h('span', { className: 'aq-dot', 'data-live': snap !== null && snapshot.error === null ? '1' : '0' }),
            'AI量化 看板'),
          session === null ? null : h('span', { className: 'aq-pill' }, session.label + (session.a_share === 'open' ? '' : ' · 缓存')),
          snap !== null && snap.version ? h('span', { className: 'aq-pill' }, 'v' + snap.version) : null,
          h('span', { className: 'aq-spacer' }),
          h('button', { className: 'aq-btn', onClick: () => { void reload() }, title: '立即刷新' }, '刷新'),
          h('button', { className: 'aq-btn', onClick: () => setUI({ open: false }), title: '收起看板' }, '收起')),
        snap !== null && snap.alerts && snap.alerts.length > 0
          ? h('div', { style: { padding: '6px 12px', borderBottom: '1px solid var(--dsw-alias-border-l1)' } },
            h('div', { className: 'aq-row' },
              h('span', { className: 'aq-warn' }, '⚠ ' + snap.alerts.length + ' 条未确认告警'),
              h('span', { className: 'aq-flat', style: { fontSize: 11.5, flex: 1 } }, snap.alerts[0].title + (snap.alerts[0].body ? '：' + snap.alerts[0].body : '')),
              h('button', { className: 'aq-btn aq-btn-sm', onClick: async () => { try { await api({ op: 'alert.ack', id: 'all' }); await reload() } catch (error) { showToast(error.message, true) } } }, '全部确认')))
          : null,
        h('div', { className: 'aq-tabs' }, tabs.map(([id, label]) => h('button', {
          key: id,
          className: 'aq-tab',
          'data-active': state.tab === id ? '1' : '0',
          onClick: () => setUI({ tab: id }),
        }, label, id === 'signals' && snap !== null && snap.signals.length > 0 ? h('span', { className: 'aq-badge' }, snap.signals.length > 99 ? '99+' : snap.signals.length) : null))),
        h('div', { className: 'aq-body' }, body),
        toast === null ? null : h('div', { className: 'aq-toast ' + (toast.isError ? 'aq-err' : '') }, toast.message),
        h('div', { className: 'aq-foot' },
          h('span', null, snapshot.error === null ? '更新于 ' + (snapshot.at || '—') : h('span', { className: 'aq-err' }, '接口错误：' + snapshot.error)),
          h('span', { className: 'aq-spacer' }),
          snap !== null && snap.config ? h('span', null, '源 ' + (snap.config.quote_source || '—')) : null,
          h('span', null, '每 5 秒刷新')))
    }

    function ToggleButton(props) {
      const state = useUI()
      return h('button', {
        className: 'aq-side-btn',
        title: '打开/收起 AI量化实时走向看板',
        onClick: () => setUI({ open: !state.open }),
      }, h('span', { style: { fontSize: 13 } }, '📈'), props.wide ? h('span', null, 'AI量化') : null, state.open ? h('span', { className: 'aq-dot', 'data-live': '1', style: { width: 6, height: 6 } }) : null)
    }

    function HeaderButton() {
      const state = useUI()
      return h('button', {
        className: 'aq-icon-btn',
        title: 'AI量化看板',
        onClick: () => setUI({ open: !state.open }),
      }, '📈')
    }

    /* ────────────────────────────────── 插件入口 ────────────────────────────────── */

    const inject = ['slots']

    function apply(ctx) {
      const styleTag = document.createElement('style')
      styleTag.setAttribute('data-aiquant-ui', '1')
      styleTag.textContent = CSS
      document.head.append(styleTag)

      const widthTag = document.createElement('style')
      widthTag.setAttribute('data-aiquant-ui-width', '1')
      widthTag.textContent = ':root{--aq-dock-w:520px}'
      document.head.append(widthTag)
      dockWidthTag = widthTag

      ctx.effect(() => () => {
        styleTag.remove()
        widthTag.remove()
        dockWidthTag = null
      })

      ctx.slots.inject('shell.overlay', () => ctx.slots.register(
        { name: 'shell.overlay', id: 'aiquant-board', order: 40 },
        () => h(Panel),
      ))
      ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register(
        { name: 'sidebar.footer.action', id: 'aiquant-toggle', order: 52, label: 'AI量化' },
        (props) => h(ToggleButton, props),
      ))
      ctx.slots.inject('conversation.session.header.utilities', () => ctx.slots.register(
        { name: 'conversation.session.header.utilities', id: 'aiquant-header-toggle', order: 52, label: 'AI量化' },
        () => h(HeaderButton),
      ))
    }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
