# OKX // TERMINAL — 前端渲染层（web/）

实时加密行情面板。**WebSocket 推送驱动，不是轮询**。暗色终端风、等宽字体、零 CDN、零框架、手写 DOM + Canvas。

---

## 1. 怎么跑

后端（同事 `okx-relay` 的 `relay/relay.py`）**监听 `http://127.0.0.1:8910`**，并用 aiohttp 把本目录挂在 `/`，所以：

```
页面:  http://127.0.0.1:8910/
WS  :  ws://127.0.0.1:8910/ws
SSE :  http://127.0.0.1:8910/sse
```

前端不需要单独起服务。**不要**自己在 8910 上再起一个服务（会和 relay 抢端口）。

### 自测用模拟源（可选，仅回归/离线时用）

`_selftest/mockserver.js` 是一个**零依赖 Node 模拟数据源**，实现了同一套协议 + 静态文件服务：

```powershell
node _selftest\mockserver.js 8911      # 故意用 8911，避开 relay 的 8910
# 打开 http://127.0.0.1:8911/
```

> ⚠️ 它推的是**模拟数据**，不是 OKX 真实行情，只能用来回归渲染与断线逻辑。
> 控制接口：`/__mock/hold`（停推→测 STALE）、`/__mock/resume`、`/__mock/stop`（进程退出→测重连）、
> `/__mock/wsfail` / `/__mock/wson`（禁用/恢复 WS 升级→测降级到 SSE）。

---

## 2. 通道选择与自愈逻辑

```
        ┌─────────────── WS 连接失败/断开
        ▼
   [ WS ] ──连续失败 3 次──▶ [ SSE ] ──失败──▶ 退避后回到 WS 重试
        ◀──── 退避 1s/2s/4s/8s/15s（封顶 15s）────┘
                       │
                       └── ?sim=1 时才会降级到内置模拟源（并在页面顶部挂醒目警示条）
```

| 状态 | 顶栏显示 | 触发条件 |
|---|---|---|
| 正常 | 绿色 `● LIVE` | 通道已连且距上一条消息 ≤ 10s |
| 上游异常 | 琥珀 `● UPSTREAM XXX` | 中继自报 `upstream != ok`（**本地消息还在流也不谎报 LIVE**）|
| 卡死 | 琥珀 `● STALE Ns` | > 10 秒没有任何推送 |
| 断开 | 琥珀 `● RECONNECTING` / 红色 `● DOWN` | 通道不在连 |

顶栏 `CHANNEL` 徽标实时显示当前是 `WS` / `SSE` / `SIM` / `DOWN`。
`FALLBACKS` 记录降级次数，`RECONNECTS` 记录重连次数（本页 / 上游）。

### 唯一允许的定时器（其余"动"全部由真实推送触发）

1. **重连退避** `setTimeout`（1s→2s→4s→8s→15s）
2. **1 秒节拍**：时钟、`msgs/s`、tick 计数、心跳带、STALE 判定、WS 看门狗
3. **`requestAnimationFrame` 绘制节流**（脏标记驱动，不是每条消息都重绘）

**没有 `setInterval(fetch, ...)`。没有任何 `setInterval` 驱动的假动画。**
价格闪色 / 成交行滚入 / 深度图重绘全部由收到消息的事件路径触发。

---

## 3. 消息协议 → 界面字段映射

> 以 `okx-relay` 的实测输出为准（已用 `_selftest/assert_relay.js` 对真实 8910 断言）。

| 消息 | 关键字段 | 用在哪 |
|---|---|---|
| `snapshot` | `upstream`、`route`、`insts{inst:{...}}`、`stats` | 首帧一次性填满页面（含历史 K 线）|
| `ticker` | `inst,last,chg24h_pct,vol24h,bid,ask,ts` | 顶栏价格（**闪绿/闪红**）、当前 1m K 线 |
| `trades` | `inst,rows[{px,sz,side,ts,tradeId}]` | 左列成交流（顶部插入 + 高亮淡出）|
| `book` | `inst,bids[[px,sz]],asks[[px,sz]],depth_total,seqId,status` | 深度图（**每次都是全量视图**）|
| `candle` | `inst,bar,o,h,l,c,vol,confirm,ts` | 中列 K 线（`confirm=0` 更新最后一根 / `1` 追加新根）|
| `oi` | `inst,oiUsd,oi,oiCcy,ts` | 右列 OPEN INTEREST |
| `funding` | `inst,rate,nextRate,nextTime,ts` | 右列 FUNDING + 倒计时 |
| `stats` | `upstream,reconnects,clients,clients_ws,clients_sse,msgs_per_s,msgs_total,sub,ring,persist,uptime_s` | 顶栏 `msgs/s`·`CLIENTS`·`UPSTREAM`；右列 STATS 全表 |
| `error` | `scope,msg` | 写进右列 LOG，并把上游标为降级 |

- **时间**：OKX `ts` 是毫秒 UTC，**前端统一 +8h 显示北京时间**（`bjTime()`）。
- **`chg24h_pct`** 直接用后端算好的值（OKX tickers 不给该字段）。
- **零硬编码**：任何拿不到的字段显示 `--`，绝不写死价格/成交量。

---

## 4. K 线数据来源（**已更新：不是前端累积**）

**历史 K 线来自 `snapshot.insts[inst].candles`（中继回填的真实 1m bar），增量来自 `candle` 消息。**

- 实测：`snapshot` 里每标的带 **10 根**真实 1m K 线（中继当前上限）。
- `candle` 增量：`confirm=0` → **更新最后一根**（实时生长）；`confirm=1` → **追加新根**。
  `confirm` **原样使用，绝不自己猜收盘**。
- 启动时另外会用 `snapshot.insts[inst].ticks`（1 秒聚合 bar）在**前端合成 1m bar** 补齐。
- 图注实时显示实际根数与来源标签，例如 `K线来源: RELAY candle · 本页实际收到 12 根，未伪造历史`。

> **重要发现（Lead 警告的字节级坑，已实测排除）**
> OKX `candle1m` 原始 `data` 是**数组** `[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]`。
> 断言结果：中继已解成对象，实测 **8 条 `candle` 消息中 `o/h/l/c` 非数字的 = 0**，
> 字段类型 `o:number h:number l:number c:number vol:number confirm:number ts:number`。
> 复现：`node _selftest\assert_relay.js http://127.0.0.1:8910 10`

---

## 5. 订单簿深度图

- 横轴价格（以中间价 `mid` 为中心左右对称），纵轴**累计量**，bids 绿 / asks 红阶梯面积。
- 中间显示 `BID / MID / ASK` 与 `SPREAD（绝对价 + bp）`。
- **每次收到 `book` 只置脏标记，由 `requestAnimationFrame` 统一重绘**（避免每条消息重绘导致掉帧）。
- 中继下游默认推 **20 档**（内部维护 400 档），`depth_total` 实测 `{bids:400,asks:400}`。
- 实测确认**每条 `book` 都是可直接重绘的全量视图**（连续多条均为 `bids=20 asks=20`），前端不拼增量。

---

## 6. "它在动"的可观测证据（设计要点）

| 元素 | 怎么动 | 由什么触发 |
|---|---|---|
| 顶栏价格 | 数字闪绿/闪红 + 淡出 | `ticker.last` 变化（CSS class 切换 + `transition`）|
| 成交流 | 新行从顶部 slide+fade，买绿卖红 | `trades.rows` |
| 深度图 | 阶梯面积实时重绘 | `book` |
| K 线 | 最后一根半透明+亮框持续生长 | `candle(confirm=0)` / `ticker` |
| `msgs/s` | **每秒必变** | `stats.msgs_per_s`（缺失时用本端实收计数兜底）|
| `TICK COUNTER` | 每秒自增（累计收到的消息数）| 每条消息 |
| 底部心跳带 | 最近 60 秒 `msgs/s` 迷你柱状图 | 1 秒节拍 |
| `● LIVE` | 状态灯颜色 + `STALE Ns` 计数 | 上一条消息距今时长 |

---

## 7. 渲染/健壮性细节

- 成交流超过 **500 行**裁剪头部，避免 DOM 无限增长。
- 任何面板无数据时显示 `NO DATA` / `--`，**不会白屏**。
- 单个 `try/catch` 包住每个 Canvas 绘制，一个面板出错不影响其他面板。
- `window.onerror` 捕获的异常会写进右列 LOG。
- WS 有 **1 秒看门狗**（浏览器偶发不触发 `onclose` 时兜底重连）。
- 事件源切换时用 `_handled` 标记避免重复计失败。

---

## 8. 文件

| 文件 | 说明 |
|---|---|
| `index.html` | 结构：顶栏 / 三列（成交流 · 深度图+K线 · OI+Funding+STATS+LOG）/ 底部心跳带 |
| `styles.css` | 暗色终端风：纯黑底、1px 分格线、涨绿跌红琥珀警示 |
| `app.js` | 连接层 + 消息处理 + 渲染层 + Canvas（无依赖） |
| `_selftest/mockserver.js` | ⚠️ 模拟数据源（自测用，端口 8911） |
| `_selftest/assert_relay.js` | 对真实 relay 的协议断言（candle 数字性、book 全量性） |
| `_selftest/cdp_verify.js` | 间隔 N 秒抓两次 DOM 并 diff + 截图 + 收集 JS 报错 |
| `_selftest/fallback_test.js` | 通道降级/恢复全流程验证 |
| `_selftest/scenario_test.js` | STALE → 恢复 → 后端被杀 的场景验证 |
| `_selftest/probe_state.js` | 深探页面内部状态 `S`（K 线数组、seq 守卫、book/trades）|
| `_evidence/` | 截图与 DOM 快照证据 |

### 复现命令

```powershell
cd D:\量化\okx-terminal\web

# 协议断言（candle 数字性 + book 全量性 + 消息速率）
node _selftest\assert_relay.js http://127.0.0.1:8910 10

# 两次 DOM 快照对比 + 截图 + JS 报错收集（对真实 relay）
node _selftest\cdp_verify.js "http://127.0.0.1:8910/" "D:\量化\okx-terminal\web\_evidence\render-live.png" 12

# 降级/恢复全流程（需先起 8911 模拟源）
node _selftest\mockserver.js 8911
node _selftest\fallback_test.js "http://127.0.0.1:8911/" "http://127.0.0.1:8911" "D:\量化\okx-terminal\web\_evidence"
```

---

## 9. 证据文件与数据来源标注（重要）

`_evidence/` 下的截图分为两类，**必须分清**：

| 文件 | 数据来源 | 说明 |
|---|---|---|
| `render-live.png` | ⚠️ **当前是错误页**（Chrome `ERR_CONNECTION_REFUSED`）| 见下方说明 |
| `render-mock-8911.png` | **模拟源（8911）** | ✅ 有效截图，但**不是真实行情** |
| `channel-1-ws.png` / `channel-2-sse.png` / `channel-2b-sse-later.png` / `channel-3-down.png` | **模拟源（8911）** | 通道降级/恢复证据 |
| `scenario-1-live-ws.png` / `scenario-2-stale.png` / `scenario-3-down.png` | **模拟源（8911）** | STALE / 断线场景证据 |
| `channel-states.json` / `scenario-states.json` | **模拟源（8911）** | 各场景的 DOM 状态机读数 |

### ⚠️ 关于真实数据截图

真实 relay（8910）**实测跑通过**：`_selftest/assert_relay.js` 的全部断言通过，并且抓到过
两次间隔 12 秒的 DOM 快照，字段确实在变 —— **原始命令输出已完整留档在
[`_evidence/evidence-real-8910.txt`](_evidence/evidence-real-8910.txt)**（含 16 个变化字段的逐项 diff）。
但 **relay 进程在 16:13 后持续不可用（>20 分钟 `ERR_CONNECTION_REFUSED`）**，
导致最后一次重抓截图时页面加载失败，`render-live.png` 被覆盖成了 Chrome 错误页。

**复现真实截图**（relay 恢复后执行，或直接跑等待脚本）：

```powershell
node _selftest\assert_relay.js http://127.0.0.1:8910 10
node _selftest\cdp_verify.js "http://127.0.0.1:8910/" "D:\量化\okx-terminal\web\_evidence\render-live.png" 12
# 或者：等 relay 上线后自动跑完这两步
node _selftest\wait_and_verify.js 20
```

---

## 10. 已知限制

1. **`snapshot.candles` 实测只有 10 根**（中继当前上限），所以 K 线一打开只有 10 根历史；根数随时间增长，图注会显示真实根数。若需要更长历史，需中继提高回填条数。
2. 成交流的 `SZ` 用紧凑格式（`0.00` 表示 < 0.005），BTC 小额成交看起来偏小。
3. 深度图只画中继给的前 20 档（可视化足够）；要更深档位需中继加 `--book-levels`。
4. 顶栏 `tick`（本端实收条数）与 `MSGS/S`（中继侧的 `msgs_per_s`）口径不同，两者都保留以免单点失真。
5. 本页不构成任何投资建议。
