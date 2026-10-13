# POLY // TERMINAL

> **⚠ 依赖本机 Clash/mihomo 代理 `127.0.0.1:7890` 运行；代理关闭则 Polymarket 三块面板全站 `OFFLINE`。**
> 本机实测：直连 `gamma-api.polymarket.com` 20s 超时（curl err=28 / code=000），走系统代理才 200。
> 唯一不依赖代理的是顶栏 **GLOBAL 市场带**（腾讯 `qt.gtimg.cn` 直连），代理挂掉时它仍然刷新 —— 这是刻意的韧性设计。

## 启动（PowerShell 可直接粘贴）

**推荐：从工作区根起服务**（这样 poly-data 的 `data/poly/edges.json` 能被自动热插）：

```powershell
cd D:\量化
python -m http.server 8899 --bind 127.0.0.1
```
然后浏览器打开 → <http://127.0.0.1:8899/web/poly-terminal/index.html>

**只跑单页面也可以**（此时读不到 poly-data 的 feed，SCANNER 走内置算法）：
```powershell
cd D:\量化\web\poly-terminal
python -m http.server 8899 --bind 127.0.0.1
```
然后打开 → <http://127.0.0.1:8899/>

> **PowerShell 两个坑，已替你避开**
> 1. **不要**敲 `run.cmd` —— PowerShell 不从当前目录加载可执行文件，必须写 `.\run.cmd` 或改用上面的 `python -m http.server`。上面的命令是完整的、可直接粘贴的。
> 2. `python` 需在 PATH 里（conda base 即可）；若提示找不到，用绝对路径
>    `C:\Users\33096\AppData\Local\Programs\Python\Python314\python.exe -m http.server 8899 --bind 127.0.0.1 --directory D:\量化`。

> **⚠ 必须开代理 `127.0.0.1:7890`（Clash/mihomo）**：三个 Polymarket 域名直连 20s 超时（curl err=28/code=000），走系统代理才 200。浏览器吃系统代理，实测 Chrome/Playwright 都能通。
> 代理关闭 → 六块面板 `OFFLINE` + 顶栏 `● OFFLINE — PROXY DOWN`；唯一不依赖代理的是 **GLOBAL 市场带**（腾讯直连），它会继续刷新。

URL 参数：
| 参数 | 作用 |
|---|---|
| `?refresh=<毫秒>` | 把所有源的 TTL 统一压到该值（最小 2000）—— 仅用于本地跑多轮验证闪烁高亮 |
| `?edges=<url>` | 覆盖 MISPRICING SCANNER 的 edge feed（见下文「口径」） |

## 节奏 —— **按端点真实新鲜度分级**（不是一刀切 300s）

| 端点 | Cache-Control | TTL | 说明 |
|---|---|---|---|
| `data-api /positions` | **`max-age=5`** | **60s** | 全系统唯一真正实时的通道 → PNL/跟单的"实时感"靠它 |
| `data-api /value` | `max-age=5` | 60s | 权威总额 + 钱包活性（按地址缓存） |
| `data-api /trades` | `max-age=300` | 300s | COPYTRADE ENGINE |
| `data-api /trades?user=<0x>&limit=1` | `max-age=300` | 300s | **活性判定**（最后一笔成交时间），按地址缓存 |
| `gamma /markets/keyset` | `max-age=300` | 300s | 与 SCANNER **共用同一缓存条目**，每 300s 只打一次 |
| `gamma /events/<id>` | — | 300s | 每个候选事件一条缓存 |
| `lb-api /profit?window=1d` | `max-age=300` | 300s | **活性候选源**（实时面板都从这里来） |
| `lb-api /profit?window=all` | `max-age=600` | 600s | 只给 ARBER LEADERBOARD（历史语义） |
| `qt.gtimg.cn`（CN 带） | — | 60s | 直连，独立于代理 |
| edge feed 探测 | — | 300s | 避免每轮都 probe 不存在的 `edges.json` |

调度器每 **10s** 跑一次 `pollAll()`，由各源 TTL 决定谁真的重取；**渲染 1s 重绘吃本地缓存**，数字变色高亮始终生效。

**请求预算**（每轮全量取数，按 TTL 摊薄）：`keyset 1 + events ≤14 + lb1d 1 + lbAll 1 + trades(5 钱包) 5 + lastTrade 16 + value 16 + positions 8 + band 2 ≈ 64` 次。
其中 **60s 频的只有 `positions`(8) + `value`(16)**，其余都在 300s/600s 频 —— 全部**按地址缓存**，不会每 10s 重打。

---

## 端点表（全部本机实测 200 + CORS `access-control-allow-origin: *`）

| 面板 | 端点 | 实测 |
|---|---|---|
| LIVE MARKETS / SCANNER 基础 | `GET https://gamma-api.polymarket.com/markets/keyset?closed=false&limit=100&order=volume24hr&ascending=false` | 200 · `max-age=300` · **limit 硬上限 100** |
| SCANNER 全量候选 | `GET https://gamma-api.polymarket.com/events/<id>` | 200 · 返回该 event 全部候选市场 |
| WALLET TRACKER / ARBER LEADERBOARD | `GET https://lb-api.polymarket.com/profit?window=all&limit=20` | 200 · 1225ms · `max-age=600` |
| LEADERBOARD 右列 | `GET https://lb-api.polymarket.com/profit?window=1d&limit=20` | 200 |
| COPYTRADE ENGINE | `GET https://data-api.polymarket.com/trades?user=<0x>&limit=10` | 200 · 1032ms |
| COPYTRADE ENGINE 兜底 | `GET https://data-api.polymarket.com/trades?limit=40` | 200 |
| PNL 明细 | `GET https://data-api.polymarket.com/positions?user=<0x>&limit=500` | 200 · `max-age=5` · **会在 limit 处静默截断** |
| **PNL 权威总额** | `GET https://data-api.polymarket.com/value?user=<0x>` | 200 · 与未截断时 ΣcurrentValue 差 ≤0.02% |
| GLOBAL 市场带 | `GET https://qt.gtimg.cn/q=s_sh000001,s_sz399001,s_sz399006,s_bj899050,s_hkHSI` | 200 · **GBK** |

❌ 不要用 `lb-api/leaderboard`、`lb-api/pnl`（404）。
⚠ `lb-api` 的 `window` **只认** `all`/`1d`/`7d`/`30d`；`1w`/`1m`/`week`/`month` 一律 **400 `invalid request`**。
⚠ `keyset` 的 `offset` → **422 `offset is not allowed on keyset endpoints`**；`limit=200` 也只回 100 条。

---

## 字段映射表

### LIVE MARKETS ← `gamma /markets/keyset`
| 接口字段 | 落点 | 备注 |
|---|---|---|
| `markets[]` | 行 | **响应是对象 `{markets:[…], next_cursor}`，不是数组** |
| `question` | 市场名（可两行，`-webkit-line-clamp:2`） | UTF-8，**不做任何编码转换** |
| `volume24hr` | 元信息 `VOL24 …` | |
| `liquidity` | 元信息 `LIQ …` | |
| `outcomePrices` | `YES`/`NO` 徽标（`67¢`/`33¢`） | **JSON 字符串**，必须二次 `JSON.parse` |
| `outcomes` | （用于臂数判断） | 同上，JSON 字符串 |
| `oneDayPriceChange` → 降级 `oneHourPriceChange` → `--` | 涨跌 `▲0.5% 24H` / `1H` | **只有 23/40 市场有 day，21/40 有 hour** |
| `id` | keyed-list 的 key | |

### MISPRICING SCANNER ← `gamma /markets/keyset` + `/events/<id>`
| 字段 | 落点 |
|---|---|
| `events[0].id` | 分组键（同一 event 下的候选结果集合） |
| `ev.negRisk` / `ev.enableNegRisk` | **套利判定前置门槛**（见口径） |
| `bestAsk` / `bestBid` | `ΣASK` / `ΣBID` 与大字价格 |
| `spread` | `ΣSPR`（只显示，不折进百分比） |
| `feeSchedule.rate` | 扣费（实测某市场 `0.04`） |
| `orderPriceMinTickSize` | EDGE 精度截断（实测 `0.01`） |
| `outcomes`（长度必须 = 2） | 腿完整性校验 |

### WALLET TRACKER ← **`lb-api/profit?window=1d`** + 活性过滤（**已换源，见下**）
| 字段 | 落点 |
|---|---|
| `proxyWallet` | 地址（截断为 `0x2005...75ea`） |
| `amount` | 右上金额（**24h 盈利**） |
| **`trades?user=<0x>&limit=1` → `timestamp`** | **`last trade 3m ago`**（活性字段，用户靠它判断该不该看） |
| **`/value?user=<0x>`** | `VAL $425,916`；为 0 时显示 `VAL 0 · FLAT` |
| `pseudonym` | 地址下方小字（截断 18 字） |
| — | 标签 `ACTIVE`：只表示"24h 内有成交"，不是接口字段 |

> **🔴 为什么换源（这是用户报"数字一直不变"的真正根因）**
> 原来用 `profit?window=all`（历史总盈利榜）。实测**前 12 名里只有 2 个还持仓**：
> `#1 0x56687bf4…` /value=**0**、0 持仓、最后一笔交易 **2024-11-13**；`#3 0x1f2dd6d4…` 同样 /value=0、最后交易 2024-10-24。
> **那是"历史战绩彪炳但已金盆洗手"的退休账户** —— 面板显示 0、永远 0，不是刷新问题。
> 对照组 `window=1d` 榜 8 个钱包**全部当日有成交**、最近一笔在几分钟前。
> 现在：**24h 榜取 16 个候选 → 逐个查最后一笔成交 → 排除 `age > 24h` → `/value > 0` 优先 → 取前 8**。

### COPYTRADE ENGINE ← `data-api/trades`
| 字段 | 落点 |
|---|---|
| `proxyWallet` | 地址行 |
| `title` + `side` + `outcome` | 动作行 `Fed cuts Q1  BUY YES` 形态 |
| `size` × `price` | 金额（USDC 名义额，实时计算） |
| `timestamp` | 行尾 `HH:MM:SS` |
| `transactionHash`+`asset` | keyed-list 的 key |

### PNL ← `data-api/positions?limit=500` + `data-api/value`（对追踪的前 5 个钱包聚合）
| 字段 | 落点 |
|---|---|
| `cashPnl` | 大字总额（Σ 全部持仓）与右下角 **本会话增量** |
| `value`（来自 **`/value`**） | `VALUE $…` 行的**权威总额** |
| 折线 | **本会话真实采样点序列**（`session samples N`），不是历史回填 |

**`positions` 会在 `limit` 处静默截断**（实测 `limit=500` 返回**恰好 500** 条，ΣcurrentValue 比权威总额少算 **26%**）。因此：
- **`/value` 作权威总额**（与未截断时 ΣcurrentValue 差 ≤0.02%）；
- 返回条数 == limit → 大字前加 **`≥`**，并显示 `TRUNCATED ≥N`，**禁止**把 ΣcurrentValue 当总额；
- 空数组语义：**空且 `/value==0` → 真无持仓**（休眠账户）；**空且 `/value>0` → `UNKNOWN`**（从未观测到，保守处理）。
- **`/value == 0` 的钱包不得进入 PNL 曲线** —— 放进来的唯一效果是让曲线永远是一条 0 直线（正是用户看到的"数字不动"）。被排除的个数显示为 `excl N dormant`。
- 采样点**每次真实取数记一个**（60s 一次），不是每次渲染记 —— 渲染是 1s，按渲染记会把序列灌成秒级重复值。

**硬声明（面板固定显示）**：`session-level short window · history source limited to ~1 day`
→ 每个 token 只有约 **22–25 个点 / 跨度约 1 天**（`gamma/prices-history` 全 404、`clob interval=max` 只回 1 个点）。
→ **本看板不得展示任何需要长历史的年化 / 长期回撤类指标**；折线只反映本会话观测，不代表历史业绩。

### ARBER LEADERBOARD ← `lb-api/profit?window=all`（**保持历史总榜，不换源**）
| 字段 | 落点 |
|---|---|
| 序号 | `#1` 排名列 |
| `pseudonym` / `proxyWallet` | 地址列 |
| `amount` | 绿色 `+$22.1M` |
| **`trades?user=<0x>&limit=1` → `timestamp`** | **活性标注** `last trade 696d ago` |
| 1d 榜同地址 `amount` | 右列 `+$x 24H`；**两榜地址不重叠时为 `--`** |

> 这个面板的语义**就是历史排行**，所以数据源保持 `window=all` 不换。
> 但必须加活性标注：否则用户会把"历史战绩第一"误读成"现在最值得跟"。
> 实测 20 行里 **19 行 >7 天无成交**（`696d / 45d / 717d / 183d / 117d / 704d…`，只有 1 行是 `8m`）→ 这些行**整行降权（opacity 0.45）**，脚注写明 `historical · 24H overlap 1/20 · 19 stale >7d`。

### GLOBAL 市场带 ← `qt.gtimg.cn`（GBK，`s_` 精简格式 + 完整格式取时间戳）
| 位置 | 含义 |
|---|---|
| `s_` 格式 `[1]` | 名称（上证指数 / 恒生指数 …） |
| `s_` 格式 `[3]` | 现价 |
| `s_` 格式 `[5]` | 涨跌幅 % |
| `s_` 格式 `[7]` | 成交额（**万元**） |
| **完整格式 `[30]`** | **最近报价时间戳 `20261009161400`** → 判断是否在交易时段 |
| — | `两市 = 上证[7] + 深证[7]`（万元→亿元，实时计算） |

> **⚠ 休市必须标注（修正我先前"这条是实时数据"的不准确口径）**
> `s_` 精简格式**只有 12 个字段、不含时间戳**，所以额外取一次完整格式（88 字段）拿 `[30]`。
> 判定：**报价在 5 分钟内更新过 → `LIVE`；否则 → `CLOSED · LAST CLOSE`（琥珀色）**。
> 好处：**不需要节假日日历**，法定休市也会自然落到 `CLOSED`。
> 周末/收盘后腾讯返回的是**上次收盘快照**（实测 `[30]=20261009161400`，`上证 3813.79` 是周五收盘价），
> 价格永远不变 —— **这时还标 LIVE 等于骗用户**。
> 注：午休（11:30–13:00）报价同样停止更新，会显示 `CLOSED`；如需要可再加 `BREAK` 状态。

---

## MISPRICING SCANNER 口径（Lead 指定，逐字实现）

**定义性套利：多结果集合的 Σ 价格偏离 1.00。**

```
cost      = Σ bestAsk_i          edge_buy  = 1 - cost         edge_buy_pct  = (1/cost - 1) * 100
proceeds  = Σ bestBid_i          edge_sell = proceeds - 1     edge_sell_pct = (proceeds - 1) * 100
```

配套硬约束：
1. **只有 `negRisk=true`（互斥且穷尽）的 event 才参与计算。** 非互斥集合（如 `US-Iran ceasefire continues through <日期A/B/C>`）Σ 价格天然远大于 1，不是套利。
2. **集合必须从 `/events/<id>` 取全集**（单页 `keyset` 分组会拿到残缺集合 —— 实测 NBA 冠军在某页只出现 2/36 条腿，Σask=0.005 → 假 **+99.5%**）。
3. **完整度 + 僵尸腿硬校验**：集合内每条腿都必须满足
   `closed !== true && active !== false && acceptingOrders !== false` **且**同时有 `bestAsk` 与 `bestBid`；
   任一条不满足 → **整条剔除**（脚注计数），不做"摘掉坏腿继续算"。
4. **成本 = 官方费率模型**：`fee = Σ_i rate_i × p_i × (1 − p_i)`，逐腿累加（不是把费率乘在总名义额上）。`rate` **逐市场读 `feeSchedule.rate`**；缺失时按官方分类表兜底（见下），不再用常数。
5. **选边按毛额**（先指出错价在哪一侧），**可行性按扣费后净值**判定：净值 > 0 → 绿徽章；毛额 > 0 但净值 ≤ 0 → **暗灰徽章 + `NOT VIABLE`**（不成立的机会必须是最弱视觉权重，不能比成立的机会更显眼）。
6. EDGE 按 `orderPriceMinTickSize` 截断，不显示假精度。

### EDGES 计数的两层语义（**正确性红线**）
**任何地方都不许把 `NOT VIABLE` 的候选计成 EDGE。** 否则顶栏"N EDGES"会和面板里的 `NOT VIABLE` 打架，用户会以为有 N 个机会可以动手，实际一个都不能做。

| 条件 | 面板标题 | 底色 |
|---|---|---|
| `n_viable > 0` | `{n_viable} EDGES FOUND` | 红底（有真机会） |
| `n_viable == 0 && n_total > 0` | `0 VIABLE / {n_total} SCAN` | **暗灰底** |
| `n_viable == 0 && n_total == 0` | `0 EDGES` | **暗灰底** |

顶栏 `EDGES` 计数**同源**，只数 `viable`。**宁可是 `0 EDGES`，也不要一个误导性的 `1`。**

### 官方费率表（缺失 `feeSchedule.rate` 时的分类兜底）
来源：<https://docs.polymarket.com/trading/fees> · `fee = C × feeRate × p × (1 − p)`（C=股数, p=价格，**只有 taker 付费，maker 免费**）

| 分类 | rate | rebate | 分类 | rate | rebate |
|---|---|---|---|---|---|
| **Crypto** | **0.07**（全场最高） | 20% | Culture | 0.05 | 25% |
| Sports | 0.05 | 15% | Weather | 0.05 | 25% |
| Economics | 0.05 | 25% | Other / General | 0.05 | 25% |
| Finance | 0.04 | 25% | Mentions | 0.04 | 25% |
| Politics | 0.04 | 25% | Tech | 0.04 | 25% |
| **Geopolitics** | **0** | — | | | |

> ✅ **`Crypto 0.07` 已在代码与上表中**（`FEE_BY_CATEGORY.crypto = 0.07`）。它**高于** Sports，是全场最高费率 —— 若漏掉它，加密类市场的成本会被低估近一半。
> rebate 列来自 Lead 独立走代理抓取的官方页面（`docs.polymarket.com/trading/fees`，465KB 原文）。
> ⚠ **Geopolitics 官方费率是 0**，所以**不能**再用 `max(已有, 0.05)` 那种兜底（会把 0 抬到 0.05）。实测 `feeSchedule.rate` 在 **0.04~0.05** 之间浮动，且约 14~15% 的市场没有该字段。

**两条必须知道的口径取舍：**
- **`Σspread` 只显示、不折进百分比。** 36 腿集合的原始 Σspread 可达 606.6¢，折进去会得到 `-174%`/`-626%` 这种无信息量数字，而**判定结论不变**。已获 Lead 批准。
- 行内 `ΣBID / ΣASK`（**不是**参考图的 `YES / NO`）—— 这里是整组求和，标 YES/NO 会误导。

### 行内信息的视觉主次（唯一用途是回答"现在有没有得做"）
| 位置 | 内容 | 理由 |
|---|---|---|
| **主数值（大号、按符号上色）** | **`NET NEAREST`** = 离可行最近那一侧的**扣费后净边际** | "还差 3.29 个点"比"Σ 总和 100¢"有用得多 |
| 辅助小字（暗灰） | `ΣBID / ΣASK` 总和 | 支撑数据，不该抢焦点 |
| 徽章（暗灰底） | `GROSS -0.80%` = **扣费前**毛边际 | 与主数值成对读，一眼看出"费用吃掉了多少" |
| 侧栏文字 | `SELL ALL · N OUT · ☠僵尸数 · NOT VIABLE` | 口径与可行性结论 |

> ⚠ **这是「定义性套利」（Σ 价格偏离 1.00），与参考截图里那个基于私有模型的 EDGE 不是同一个东西，不可直接比较。**

### 内容被容器裁掉时必须可见
面板保留**面板内滚动**（`overflow-y:auto` + 加宽滚动条），并在脚注显式给出总行数与可滚动提示，例如 `↕ 10 ROWS · SCROLL FOR ALL ·`。
理由：截图里"标签说 10 行、眼睛只看到 4 行"是最容易被误判成 bug 的形态 —— **"还有多少没看到"必须写在界面上**。

### 计数一致性（硬约束）
面板标签、顶栏 `EDGES`、DOM 行数**读同一个访问器** `scannerRows()`（返回 `CACHE.scanner`），
`renderScanner()` 故意**忽略传入参数**，从结构上杜绝"同一个数两处不同源"。
DOM 硬断言（非目测）：`顶栏 EDGES == viable 行数`、`DOM 行数 == CACHE.scanner.length`、`标签末位数字 == 行数`，三项同时为真才算通过。

### 扫描范围（重要限制，实测结论）
`markets/keyset` 第一页、`limit=100`、`order=volume24hr`（**limit 硬上限 100**）。分页游标实测**全部无效**：
`cursor` / `next_cursor` / `after` / `page` 四种参数均返回**同一页**（全重复、游标不变），`offset` → **422 `offset is not allowed on keyset endpoints`**。
→ **扫描范围 ≠ 全市场**，`N EDGES FOUND` 会低估。面板脚注固定自曝 `scan N mkts · …`。

### 可选 feed（已热插 poly-data 产出）
按顺序探测：`edges.json` → `data/poly/edges.json` → `../../data/poly/edges.json` → `/data/poly/edges.json`（相对页面 URL 解析，所以**从 `D:\量化` 根起服务**才能读到 poly-data 的文件）；也可用 `?edges=<url>` 指定。
- 支持 poly-data 的**文档式** `{items, counts, rejected_sets, filter_chain, fee_source}`，也兼容简单数组 / `{edges:[…]}`。
- feed 存在时**优先用 feed**，面板脚注显示其**剔除原因分布**（比只显示一个总数更有说服力），例如实测：
  `feed · sets 96 · kept 10 · zombie 47 · incompl 73 · nonexcl 56 · nominal+ 58 · zombie legs 827`
- feed 不存在时自动回落到内置算法。
> 本前端**只读**该 feed，**绝不写** `D:\量化\data\poly\` 下任何文件。

---

## 面板状态语义

| 状态 | 含义 |
|---|---|
| `OK` | 有真实数据 |
| `NO DATA` | 接口通了但该面板没数据（如某钱包 0 持仓）——**正确行为，不许补别的钱包凑数** |
| `RED / OFFLINE` | 该面板数据源失败；body 显示 `OFFLINE` 占位，不留空白 |
| 顶栏 `● LIVE` | 六块面板全通 |
| 顶栏 `● DEGRADED n/6` | 部分面板失败 |
| 顶栏 `● OFFLINE — PROXY DOWN` + 红色横幅 | Polymarket 全线失败 → **判为代理不可达**，横幅明示 `PROXY 127.0.0.1:7890 UNREACHABLE`；此时 A股带仍 `DIRECT` 刷新 |

---

## 已知陷阱（后来人勿踩）

1. **不要「修」Polymarket 的看起来像乱码的中文**。`question` 字段是**合法 UTF-8**（字节级验证 `In\xe1cio` = `Inácio`）；在 PowerShell 控制台看到的 `InÃ¡cio` 是 **GBK 代码页的显示假象**。做 latin1→utf8 转换会**把好数据改成乱码**。→ **字节级验证优先于任何显示层观察。**
2. **腾讯必须显式 `TextDecoder('gbk')`**。用默认 UTF-8 解会把 `上证指数` 变成乱码。→ 同一枚硬币的两面：**该管的编码要管对，不该管的别乱转。**
3. **`outcomes` / `outcomePrices` 是 JSON 字符串**，直接当数组用会拿到一堆字符。
4. **gamma 响应是对象**，取 `.markets`。
5. **keyset 分页无效**（见上）。
6. **`oneDayPriceChange` / `bestBid` 是部分存在**，必须有降级链，绝不补假值。
7. `positions` 对某些钱包返回 **空数组**（如全周期榜第 1 名 `0x56687bf4…` 当前 0 持仓）→ 显示 `NO DATA`。

---

## 已知缺数据 / 降级面板清单

| 面板 | 缺什么 | 当前显示 |
|---|---|---|
| LIVE MARKETS | NO 侧无独立涨跌字段 | NO 列涨跌留空（不编） |
| ARBER LEADERBOARD | 右列需要 1d 榜地址重叠，实测 **overlap 0/20** | 全部 `--`，脚注自曝 `24H overlap 0/20` |
| ARBER LEADERBOARD | 参考图的 `847tx`（交易笔数）lb-api 无此字段 | **改用真实 24H 盈亏**，宁换真字段不造假字段 |
| MISPRICING SCANNER | 仅扫第一页 60 市场；多数时段无可行套利 | 常见 `NOT VIABLE` / `NO DEFINITIONAL ARB IN SCAN RANGE` |
| PNL | 依赖追踪钱包持仓；全为 0 时无数据 | `NO DATA`；折线仅本会话采样点 |
| PNL | 参考图右下角 `+$7` | 显示**本会话实测增量**（首轮为 `--`） |
| 全页 | `★` 参考图的市场类别字段 gamma keyset 无 | 用 `VOL24` / `LIQ` 替代 |

## 配色约定（全页统一）
**绿 = 正/涨/买/盈利**，**红 = 负/跌/卖/亏损**，**琥珀 = EDGE 非可行 / 同步计时**，灰 = 元信息，白 = 主数值。
A股带**也沿用同一套**（绿涨红跌），未采用 A股「红涨绿跌」习惯 —— 保证全页只有一套语义。

## 验证记录（本机实测，可复现）

服务从 `D:\量化` 根起，页面 `http://127.0.0.1:8899/web/poly-terminal/index.html`（纯静态）。

| 验证项 | 方法 | 实测结果 |
|---|---|---|
| 页面能打开并渲染 | Playwright 真浏览器 + DOM 断言 | 六面板真实行数：`scanner:ok/10  wallets:ok/7  markets:ok/100  copy:ok/8  pnl:ok/0  leaderboard:ok/20`；截图 `_evidence/render-live.png` |
| 是真数据不是样例 | 断言全文**不含** `Starship`/`312.0k`/`5.4M`/`847tx`/`Fed cuts`/`0x3f4a` | `forbidden_strings_present: []` ✅ |
| **EDGES 两层语义** | DOM 断言 | 顶栏 `0`；面板标签 `0 VIABLE / 10 SCAN`，class = `tag tag-dim`（暗灰，非红）；10 行徽章全部 `badge dim` ✅ **无 NOT VIABLE 被计成 EDGE** |
| **计数一致性（硬断言）** | DOM 断言三项同时为真 | `CONSISTENT: true`（顶栏==viable 行数、DOM 行数==cache 长度、标签末位数字==行数）✅ |
| **活性过滤（本轮修复）** | DOM 断言 + 两次快照 | 8 行钱包**全部** `last trade` 在 24h 内（`58s/2m/4m/5m/6m/8m/27m/7.9h`），`/value` **8/8 非 0**，`funded_count: 8` ✅ |
| **数字真的会动** | 间隔 95s 抓两次快照 | `pnl_total ≥$10,289,086 → ≥$10,285,673`；`VALUE $1,658,416 → $1,668,304`；`samples 1 → 2`；`spark_paths 0 → 2`；钱包 `last trade` 时长同步刷新 ✅ **不再是"一直不变"** |
| **活性字段在 DOM** | 断言 `[data-f="age"]` | `wallet_ages_present: true`（形如 `last trade 58s ago`）✅ |
| **休市标注** | DOM 断言 | `band_state = "CLOSED · LAST CLOSE"`、`data-live="no"`（今天为周末，报价时间戳停在周五）✅ |
| **榜单活性 + 降权** | DOM 断言 | 20 行里 **19 行** `data-stale="yes"`（`696d/45d/717d…`，仅 1 行 `8m`），脚注 `historical · 24H overlap 1/20 · 19 stale >7d` ✅ |
| 分级 TTL | 开页观察 72s + fetch 计数包装 | `keyset` 80s / `lb-*` 81s / `trades` 79s **未重取**；`positions` 10s、`cn-band` 11s **已重取** ✅ |
| **/value 权威总额** | DOM 断言 | 大字 `≥$10,577,574`（截断标记）+ `VALUE $1,543,372 · 5 W` + `2500 POS · 5 ACTIVE W · TRUNCATED ≥5` ✅ |
| **PNL 历史声明** | DOM 断言 `.pnl-caveat` | `session-level short window · history source limited to ~1 day` ✅ |
| **feed 热插 + 剔除分布** | DOM 断言脚注 | `feed · sets 96 · kept 10 · zombie 47 · incompl 73 · nonexcl 56 · nominal+ 58 · zombie legs 827`（源 `../../data/poly/edges.json`）✅ |
| 闪烁高亮 | `MutationObserver` 监听 class | 早前实测捕获 **24 次** `flash-up`/`flash-down` ✅ |
| 降级不崩 | `page.route(...).abort()` 掐断三个 Polymarket 域名 | 红横幅 + `OFFLINE — PROXY DOWN` + 六面板 `OFFLINE` 占位；**CN 带仍 `DIRECT`**；截图 `_evidence/render-offline.png` |

> 控制台仅有 edge feed 的 404 探测日志（首次加载各路径一条，之后按 300s TTL 缓存）。**无 JS 报错。**

## 与 poly-data 的集成状态
`D:\量化\data\poly\edges.json`（79.5KB）**已就绪并被本页热插命中**：面板脚注显示其 `counts` 与逐标签剔除分布。
- 该文件自述口径：`negRisk-only + complete-set-only + active-only(zombie-quote filter) + fee deducted + scope = page 1 only`
- `counts`：`sets_considered 96 / kept 10 / viable 0`，剔除原因 `zombie 47 / incompl 73 / nonexcl 56 / nominal+ 58`
- **`viable 0`** —— 与内置算法结论一致：4~5% 费率下定义性套利不成立。
- 两条路径都读同一份真相：feed 存在时用 feed；不存在时回落到内置算法（**同一套四道门槛**）。

## 一处需要更正的历史结论（自查）
我在早前回报中说「NBA 2027 Champion 36 腿、0 关闭、0 缺盘口 → 完整集合，所以那行 +2% 不是假套利」。
**这个结论是错的。** poly-data 复核发现该事件 36 腿中有 **6 条僵尸腿**（`closed`/`archived`/`active=false` 却仍在返回双边报价），把 Σbid 抬到 1.0200 造成名义 +2.00% 的**假套利**。
根因：我当时**只校验了 `closed`，没校验 `active` / `acceptingOrders`**。现已补上（见口径第 3 条），并在 feed 侧由 `ZOMBIE_QUOTES` 标签独立印证。**宁可 `0 EDGES`，也不要一个僵尸腿撑起来的假信号。**

_不构成投资建议。所有数值来自接口实时返回，页面内无任何硬编码价格/地址/金额。_
