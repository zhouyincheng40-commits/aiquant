# dsh-aiquant-mode —— DSH「AI量化模式」

一句话：**在 DSH 里新建一个会话、选「AI量化模式」，给一个标的或一句研究需求，六个量化角色（数据 / 因子 / 策略 / 回测 / 风控 / 实盘）开始干活，所有事实落 SQLite 事实库。**

- 模式入口：DSH 新建会话时的模式列表（`preset-aiquant`，展示名 **AI量化模式**）
- 看板：**已于 2026-10-10 删除**（原右侧常驻「AI量化 看板」+ 侧栏「AI量化」开关 + 会话头 📈 按钮全部移除；浏览器半侧不再注册任何插槽，原实现留档 `lib/client.board.disabled.js`）
- 事实库：`$DSH_HOME/aiquant/aiquant.db`（SQLite，WAL）
- 工作区：`D:\量化`（可用 `AIQUANT_HOME` 覆盖）

```
┌─ 事实库 (node:sqlite) ───────────────┐   ┌─ 行情引擎 (feed) ─────────────┐
│ 自选 / 持仓 / 成交 / 信号 / 回测      │◀──│ 腾讯行情 + 腾讯日线 + 新浪外汇 │
│ 净值 / 笔记 / 告警 / 运行日志 / K线   │   │ 指标(MA/MACD/RSI/BOLL/ATR/KDJ) │
└──────────────┬──────────────────────┘   │ 信号规则(金叉/超买/突破/回撤…) │
               │ ctx.aiquant              └──────────────┬────────────────┘
       ┌───────┴────────┐                                 │ ctx.aiquantFeed
       │                │                                 │
┌──────▼──────┐  ┌──────▼───────────────┐  ┌──────────────▼──────────────┐
│ 智能体工具   │  │ POST /aiquant/api    │  │ 量化 MCP 服务器 (aiquant)    │
│ aiquant_*   │  │ (host 桥接 / 看板用)  │  │ q_* 12 个工具 (Python 3.11)  │
└─────────────┘  └──────┬───────────────┘  └─────────────────────────────┘
                        │ 每 5 秒轮询
                 ┌──────▼─────────────────────────────┐
                 │ 浏览器看板 lib/client.js（手写 bundle）│
                 │ 概览/行情/信号/持仓/回测/风控/数据     │
                 └────────────────────────────────────┘
```

## 一、安装与生效

```powershell
# 1) 装进 profile（一次性）
#    在 DSH 里对 dsh-aiquant-mode 执行安装，或命令行：
dsh plugin --profile desktop add file:D:/量化/aiquant-mode

# 2) 改完源码后同步（生成 cordis.patch.yml + 覆盖 profile 里的副本）
pwsh -File D:\量化\aiquant-mode\tools\install.ps1

# 3) 生效：完整重启 DSH（profile bundles / 新增客户端 bundle 需要重启；仅刷新页面不够）
```

自检：

```powershell
pwsh -File C:\Users\33096\.dsh\aiquant\setup.ps1 -Check   # 环境/数据源/MCP/事实库
node D:\量化\aiquant-mode\test\store-smoke.mjs            # 事实库 22 项断言
node D:\量化\aiquant-mode\test\feed-smoke.mjs             # 指标+信号+真实联网 22 项
node D:\量化\aiquant-mode\tools\build.mjs --check         # 补丁与预设是否一致
```

开发注意（踩过的坑，2026-10-10 实测）：

- **工具参数 schema 硬规则**：`type: 'object'` 的节点必须显式声明 `additionalProperties: true|false`（dsh-tools 的 author-schema 校验器，见 `dsh-tools/lib/types/schema.js`）。少写会让该工具行在 preset 挂载时激活失败 —— 新会话模式卡片显示红色「加载失败」，报错形如
  `aiquant-tools (dsh-aiquant-mode/tools): unsupported JSON schema: parameters.params.additionalProperties must be explicitly true or false`。
  同一校验器允许的键只有：`description / title / default / examples / required(属性位) / type / enum / const / items / properties / additionalProperties / oneOf`。
- **「加载失败」的原因藏在 hover 提示里**：卡片里那份原因文本被 `.cardBrokenReason` 用 `clip` 隐藏，只有悬浮 badge 才显示，命令行取不到。取原因的办法：把 `lib\preset-diag.client.js` 覆盖到 `lib\client.js`，跑 `tools\install.ps1 -NoBuild`，约 1 秒后（client-hmr 热替换）窗口左上角会直接画出该 preset 的 `broken` 全文；用完把干净版覆盖回去。
- **preset 只挂载一次**：挂载结果缓存在已激活的 generation 里，改完 host 侧 JS（含 `tools.js`）**必须完整重启 DSH** 才会重新挂载；只改文件不重启，卡片仍显示「加载失败」。

## 二、看板（已于 2026-10-10 删除）

> 右侧常驻「AI量化 看板」及其两个开关按钮已删除：`lib/client.js` 现在只保留一个不注册任何插槽的空客户端模块，原看板实现留档在 `lib/client.board.disabled.js`。
> host 半侧不变 —— 事实库 `ctx.aiquant`、行情引擎 `ctx.aiquantFeed`、`POST /aiquant/api`、12 个 `aiquant_*` 工具照常工作；看板数据改用 `aiquant_board` 工具回读。

下表是删除前的原看板功能（仅作留档）：

| 页签 | 内容 |
|---|---|
| 概览 | 组合市值/浮盈/持仓数/信号数/最大回撤/告警 KPI、**持仓市值-净值曲线**（含最大回撤标注）、自选迷你走势、最新信号 |
| 行情 | 自选表（现价/涨跌幅/高低/成交额/分时 sparkline/更新时间）、加入与移除自选、强制刷新 |
| 信号 | 引擎扫描出的信号（金叉/死叉/RSI 超买超卖/20 日突破/放量/回撤预警/均线多空排列），一键「扫描自选」 |
| 持仓 | 建仓/平仓/权重/浮盈/集中度，落库后才进风控与报告 |
| 回测 | 回测记录（年化/回撤/夏普/样本外/成本模型），缺成本或样本外的会被标黄 |
| 风控 | 回撤/波动/夏普/卡玛/VaR95/ES95/胜率、红线越线清单、持仓相关性矩阵（|r|≥0.8 视为高相关） |
| 数据 | 事实库统计、环境自检、数据源延迟探测、研究笔记、运行日志、一键导出日报 |

看板每 5 秒拉一次 `/aiquant/api`；页面隐藏时自动暂停轮询。**所有数字都来自事实库**，代理不编数。

## 三、智能体工具（preset 平面，`dsh-aiquant-mode/tools`）

| 工具 | 用途 |
|---|---|
| `aiquant_env` | 开工前自检（事实库/引擎/MCP/解释器/技能/代理/数据源探测） |
| `aiquant_watchlist` | 自选增删查 |
| `aiquant_quote` | 实时行情 + 可选日线技术指标 |
| `aiquant_board` | 读看板事实快照（组合/自选/信号/回测/风险/告警/日志） |
| `aiquant_signal` | 信号写入 / 查询 / 扫描 / 清空（同日同逻辑自动去重） |
| `aiquant_position` | 建仓 / 改仓 / 平仓（算已实现盈亏）/ 列表 |
| `aiquant_backtest` | 回测落库（强制口径检查：`cost_model` 与 `out_of_sample`） |
| `aiquant_risk` | 风控判定（指标 + 集中度 + 相关性 + 红线通过/不通过） |
| `aiquant_note` | 研究笔记（含失败尝试） |
| `aiquant_report` | 生成并落盘 AI量化日报到 `reports/` |
| `aiquant_alert` | 告警写入 / 列表 / 确认 |
| `aiquant_equity` | 净值点读写（风险指标的唯一数据源） |
| `aiquant_paths` | 所有关键路径与占用 |

## 四、量化 MCP 服务器（`q_*`，serverName=aiquant）

`mcp/quant_mcp/server.py`（零第三方 MCP SDK 依赖，stdio JSON-RPC 2.0，解释器 = 主环境 `aiquant`）：

`q_env` `q_quote` `q_klines` `q_indicators` `q_backtest_ma` `q_optimize_grid` `q_factor_ic`
`q_market_overview` `q_screen` `q_crypto_klines` `q_export_csv` `q_cache_clear`

分工：**`q_*` 负责算（回测/寻优/因子/选股），`aiquant_*` 负责记（落库进看板）**。

## 五、技能库（`D:\量化\skills`，随模式加载）

`quant-router`（总路由）· `quant-data` · `quant-factor` · `quant-strategy` · `quant-backtest` · `quant-risk` · `quant-live` · `quant-report`

## 六、数据源与降级顺序（实测）

| 数据 | 首选（直连） | 兜底 |
|---|---|---|
| A股/ETF/指数/港股/美股实时 | 腾讯 `qt.gtimg.cn`（GBK 解码） | 库内缓存（标 `stale`） |
| A股日线（前复权） | 腾讯 `web.ifzq.gtimg.cn` | 新浪日线 |
| 加密实时 | 新浪外汇 `hq.sinajs.cn/list=fx_sbtcusd` | Binance → OKX → 火币 → CoinGecko（自动走系统代理） |
| 加密日线 | 新浪全球期货（BTC） | Binance → OKX |

代理：`AIQUANT_PROXY` > `HTTPS_PROXY/HTTP_PROXY` > Windows 注册表系统代理（本机 `127.0.0.1:7890`）。直连失败才走代理，A股不受影响。

## 七、目录

```
D:\量化\
├─ aiquant-mode\           插件包（本模式本体）
│  ├─ lib\                 index / store / store-core / feed / net / ui / tools / client(+ board.disabled 留档)
│  ├─ presets\aiquant\     预设源（preset.yml + agent.cordis.yml）
│  ├─ tools\               build.mjs（生成补丁）/ install.ps1（部署同步）
│  ├─ test\                store-smoke / feed-smoke / net-debug
│  ├─ scripts\             aiquant-setup.ps1
│  └─ cordis.patch.yml     生成物（勿手改生成区）
├─ skills\quant-*\         8 个量化技能
├─ mcp\quant_mcp\server.py 量化 MCP 服务器
├─ data\cache|exports\     行情缓存 / 导出
├─ strategies\  reports\  logs\
└─ $DSH_HOME\aiquant\      事实库 aiquant.db + setup.ps1
```

## 八、口径与红线（模式内置，写进了 persona）

1. 不编数据、不承诺收益；结论必须给标的+区间+口径+可复现命令。
2. 回测必须含手续费/滑点/次日成交与样本外结果，否则结论无效。
3. **落库才算发生**：看板看不到的成果不算完成。
4. 风控红线（单票 ≤40%、回撤 ≤25%、持仓 ≥3、HHI ≤3000、相关性 ≤0.8）不通过不许上实盘。
5. 券商/CTP 凭据只从环境变量读，禁止写入文件或记忆。

## 九、已知限制

- **首次装完必须完整重启 DSH**：profile bundles、客户端 bundle、`mcp-quant` 行冲突修复都要在启动时结算。看板 host 侧接口在重启前即可用，但 `q_*` 工具要重启后才出现。
- 加密非 BTC 标的的日线依赖 Binance/OKX（需代理）；BTC 走新浪直连不受影响。
- 净值曲线靠「有持仓时每 15 分钟自动记点」与 `aiquant_equity` 手工补点；样本 <20 时风险统计量按**不可用**显示，不显示假 0。
- 实盘链路（vnpy_ctp）只做了技能与 checklist，账户/CTP 环境变量未配置前不会自动下单。

## 十、许可

本包为本地自用插件（MIT）。第三方行情接口仅用于个人研究，请遵守各站点使用条款；本模式输出不构成投资建议。
