# Polymarket 公开 API 接口契约（实测版）

> **文档状态**：全部结论来自本机真调用（`D:\量化\data\poly\probe2.py` / `probe3.py` / `probe4.py` / `fetch_poly.py`），
> 原始响应存 `raw\probe*_result.json`，日志存 `D:\量化\logs\poly-*.log`。
> 取样时刻：**2026-10-10 20:34–21:05 BJT**（= 2026-10-10 12:34–13:05 UTC），含三次独立实跑的稳定性复核（见 §8）。
> **任何未实测的项都标「未实测」，不写"应该可以"。**

---

## 0. 开工前置条件（三条硬约束，必须先满足）

### 0.1 代理是必需的（不是可选优化）

| 方式 | 实测结果 |
|---|---|
| **不开代理直连** `gamma-api.polymarket.com/markets/keyset` | **失败** `ConnectTimeout(MaxRetryError("HTTPSConnectionPool(host='gamma-api.polymarket.com', port=443): Max retries exceeded ... Caused by ConnectTimeoutError(... 'Connection to gamma-api.polymarket.com timed out')))`，**24,069 ms** 才超时 |
| **显式代理** `http://127.0.0.1:7890` | **200**，772–7,283 ms |

本机 `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings`：`ProxyEnable=1`、`ProxyServer=127.0.0.1:7890`。
Python `requests` 通过 `urllib.request.getproxies()` 读注册表，**因此默认就会走代理**——这是"看起来直连可达"的真实原因。

> **重要修正**：先前"直连可达、无需代理"的结论**不成立**，那是 PowerShell `Invoke-WebRequest` 继承 WinINET 系统代理造成的假象。
> `fetch_poly.py` 已**显式**写死 `proxies={'http':…,'https':…}`，代理地址可用环境变量覆盖：`POLY_PROXY`（默认 `http://127.0.0.1:7890`）。

**无代理时的失败行为（实测，含一次真实的教训）**：

| 版本 | 行为 | 实测耗时 |
|---|---|---|
| 初版 | 进入 `RETRIES × timeout` 长循环后才退出 | **249.1 s**（2 端点 × 3 次 × ~40 s） |
| **现版** | **[0] 连通性预检** 单次请求 `timeout=8s` + `(connect, read)` 分离，失败即中止 | **16.5 s**，exit code **2** |

现版失败输出（原文摘录）：
```
[0] 连通性预检
  !! 预检失败: ConnectTimeout(MaxRetryError("HTTPSConnectionPool(host='gamma-api.polymarket.com', port=443):
     Max retries exceeded ... Caused by ConnectTimeoutError(...)
  [CRITICAL] 无法连通 Polymarket API —— 取数中止，未写入任何新数据。
  处置: 1) 确认本机代理在 127.0.0.1:7890 运行  2) 或设置环境变量 POLY_PROXY 指向可用代理  3) 随后重跑本脚本
```
> 直连真实报错原文（`--no-proxy --refresh`）：
> `ConnectTimeout(MaxRetryError("HTTPSConnectionPool(host='gamma-api.polymarket.com', port=443): Max retries exceeded with url: /markets/keyset?closed=false&limit=1 (Caused by ConnectTimeoutError(<HTTPSConnection(host='gamma-api.polymarket.com', port=443) ...`，单次 **40,068 ms**。
> （注：`requests` 传单个 float 时 connect 与 read 各算一次，故实际可达 2×timeout。现已改为元组 `(10, 30)`。）

**⚠️ 另一处已修复的真实缺陷**：初版在**全部请求失败**时仍会覆盖 `cache/stats.json`，把上次成功的数据质量/分层/套利结果全部冲掉（实测已复现）。
现版判定"全部失败"时改写 `cache/stats_failed.json`，**`cache/stats.json` 与 `edges.json` 保持上次成功结果不变**。

### 0.2 编码陷阱：不要做任何 latin1→utf8 转换

实测字节层是**合法 UTF-8**。`Inácio` 的字节是 `In\xc3\xa1cio`；在 GBK 代码页的控制台里显示为 `InÃ¡cio` 是**显示假象**。
→ 一律用 `response.json()`（requests 按 UTF-8 正确解码）；**禁止** `.content.decode('latin1')`。

### 0.3 时区口径

| 位置 | 口径 |
|---|---|
| `endDate` / `createdAt` / `updatedAt` | **ISO 8601 带偏移，UTC**（实测形如 `2026-06-03T03:59:00Z`） |
| `endDateIso` | **仅日期**，UTC 日期（无时区信息，按 UTC 解释） |
| `clob /time` | **Unix epoch 秒**（UTC）。实测 `1791636726` ↔ 2026-10-10 12:52:06 UTC |
| `data-api trades/activity` 的 `timestamp` | **Unix epoch 秒**（UTC） |
| `clob /prices-history` 的 `t` | **Unix epoch 秒**（UTC） |
| **展示** | **BJT = UTC+8**。`fetch_poly.py: utc_to_bjt()` 输出 `endDate_bjt` 列，格式 `YYYY-MM-DD HH:MM:SS` |

> 实测 `endDate` 跨度（100 市场样本）：UTC `2026-06-03 03:59` ~ `2028-11-08 04:59` = **BJT `2026-06-03 11:59` ~ `2028-11-08 12:59`**。

---

## 1. CORS：**前端可直连**（明确结论，非猜测）

**结论：`gamma-api` / `clob` / `lb-api` / `data-api` 四个域全部支持 CORS，前端纯静态页可直连，不需要本地后端。**

实测方法：带 `Origin: http://127.0.0.1:8848` 发 GET，以及对同一 URL 发 OPTIONS 预检。

| 端点 | GET status | `Access-Control-Allow-Origin` | `Access-Control-Allow-Credentials` | OPTIONS |
|---|---|---|---|---|
| `gamma-api/markets/keyset` | 200 | **`*`** | 无 | 200 / `*` |
| `clob/simplified-markets` | 200 | **`*`** | 无 | 204 / `*` |
| `lb-api/profit` | 200 | **`*`** | `true` | 204 / `*` |
| `data-api/trades` | 200 | **`*`** | `true` | 204 / `*` |
| `data-api/positions` | 200 | **`*`** | `true` | 204 / `*` |

**为什么不带 `Origin` 时看不到 ACAO**：响应头有 `vary: Accept-Encoding,Origin`（gamma/data-api）或 `vary: Origin, accept-encoding`（lb-api）。
服务端**只在请求带 Origin 时才回 ACAO**，这是标准 CORS 行为，**不代表不支持 CORS**。

⚠️ `lb-api` 与 `data-api` 返回 `Access-Control-Allow-Credentials: true`，而 ACAO 是通配符 `*`。
浏览器规范下 **`*` + credentials 组合会被拒绝**——所以前端**不要**带 `credentials`（不要 `withCredentials`/`credentials:'include'`）。
这两个 API 是公开只读、无需 cookie，正常 `fetch()` 即可。

**缓存**：gamma `Cache-Control: public, max-age=300`；data-api 同为 `300`；lb-api `600`。
→ **轮询间隔 ≥ 300 s**。前端 5 秒刷新应只刷**本地内存状态**，真取数按 300 s 走。

---

## 2. 端点清单（逐项实测）

### 2.1 市场列表

| 端点 | 状态 | 耗时 / 字节 | 备注 |
|---|---|---|---|
| **`GET gamma/markets/keyset?closed=false&limit=N&order=volume24hr&ascending=false`** | **200** | 1,045 ms / 738,221 B (N=100) | **✅ 主通道** |
| `GET gamma/markets?...` | 200 | 715 ms / 17,639 B (N=3) | ⚠️ **弃用兜底**。响应头实测 `deprecation: true` |
| `GET gamma/markets/keyset?limit=3`（不带 `closed`） | 200 | 719 ms / 17,968 B | 与 `closed=false` 同结果 —— `closed` 参数实际不改变返回 |
| `GET gamma/markets/keyset?...&active=true&archived=false` | 200 | 1,346 ms / 17,968 B | 同上，未观察到过滤效果 |

**`/markets` 弃用响应头（实测原文）**：
```
deprecation: true
sunset: Fri, 01 May 2026 00:00:00 GMT
warning: 299 - "use /markets/keyset"
```
> `lb-api` 的 `/profit`、`/volume` **也有** `deprecation: true`（但本次实测未取到 `sunset` 值）。

### 2.2 ⚠️ 分页：**实测不可用**（这是本契约最重要的发现）

`/markets/keyset` 响应是**对象**：`{"$schema":…, "markets":[…], "next_cursor":"<258 字符不透明串>"}`。
`next_cursor` **确实存在**（长度 258），但**无法用它翻页**：

| 测试 | 结果 |
|---|---|
| 第 1 页 `limit=100&order=volume24hr` | 100 条，`next_cursor` 长度 258 |
| 把**响应里真实的** `next_cursor` 值原样回传，参数名 `next_cursor` | 100 条，**与第 1 页 100/100 完全相同，新 id = 0** |
| 同上，参数名 `cursor` / `after` / `start_cursor` / `page` | **全部** 100 条，**完全相同，新 id = 0** |
| `offset=60` | **422** `{"type":"validation error","error":"offset is not allowed on keyset endpoints"}` |
| `limit=200` | 200，但实际只返回 **100** 条（硬上限） |
| 弃用端点 `/markets` 吃同一游标 | 返回同页 |

> **结论**：**分页实际不生效**。独立复核了前端同事的结论，但修正一处细节——`offset` 不是"返回 0 条"，而是 **422 明确拒绝**。
> **`(a)` 口径的扫描范围只能声明为「`/markets/keyset` 第一页，limit=100，order=volume24hr」**，命中率**不可外推**到全市场。
> 缓解手段：用 `/events`（内嵌全部 markets，1 次请求拿 6,249 个市场，见 §2.3）扩大样本。

### 2.3 事件（获取**完整互斥集合**的唯一可靠来源）

| 端点 | 状态 | 耗时 / 字节 |
|---|---|---|
| **`GET gamma/events?closed=false&limit=100&order=volume24hr&ascending=false`** | **200** | 2,978 ms / **21,798,544 B**（100 事件 → 内嵌 **6,249** 个市场） |
| `GET gamma/events?closed=false&limit=2` | 200 | 687 ms / 45,105 B |
| `GET gamma/tags?limit=5` | 200 | 726 ms / 780 B |

⚠️ **前端注意**：`limit=100` 的 events 响应 **21.8 MB**，必须做后端/本地缓存，不能每次刷新直拉。
> events 内嵌 markets **字段比 keyset 更全**：含 `orderPriceMinTickSize`、`orderMinSize`、`takerBaseFee`、`makerBaseFee`、`volumeClob`、`volume24hrClob`、`makerBaseFee` 等 keyset 没有的字段。

### 2.4 排行榜 `lb-api.polymarket.com`

| 端点 | 状态 | 字节 |
|---|---|---|
| `GET /profit?window=all&limit=20` | **200** | 3,876 B |
| `GET /profit?window=1d&limit=20` | **200** | 4,422 B |
| `GET /profit?window=7d&limit=20` | **200** | 3,855 B |
| `GET /profit?window=30d&limit=20` | **200** | 4,043 B |
| `GET /volume?window=all&limit=20` | **200** | 6,498 B |
| `GET /profit?window=1w` | **400** | `{"error":"invalid request"}` |
| `GET /profit?window=1m` / `week` / `month` / `daily` / `weekly` / `monthly` | **400** | `{"error":"invalid request"}` |
| `GET /leaderboard?window=all&limit=5` | **404** | `404 page not found\n` |
| `GET /pnl?window=all&limit=5` | **404** | `404 page not found\n` |

> ⚠️ **修正 Lead 给的口径**：`window` 只认 **`all` / `1d` / `7d` / `30d`**。
> **`1w` 和 `1m` 实测 400**，不能用。`/leaderboard`、`/pnl` 确认 404，与 Lead 一致。

**字段**（`/profit`、`/volume` 同构，共 7 个）：
`proxyWallet`(0x 地址) `amount`(float，USD) `pseudonym` `name` `bio` `profileImage` `profileImageOptimized`

**实测榜首样例**：
| 榜单 | 榜首 `pseudonym` | `amount` |
|---|---|---|
| profit `all` | `Theo4` | **22,053,933.75** |
| profit `1d` | `mooseborzoii` | **282,217.66** |
| profit `7d` | `mooseborzoii` | **1,598,635.43** |
| profit `30d` | `00gringo00` | **2,658,044.18** |
| volume `all` | `swisstony` | **1,850,584,673.06** |

### 2.5 钱包数据 `data-api.polymarket.com`

| 端点 | 状态 | 字节 | 备注 |
|---|---|---|---|
| `GET /trades?limit=200` | **200** | 159,966 B | 全市场成交流，**COPYTRADE / 资金流正解** |
| `GET /trades?user=<0x>&limit=10` | **200** | 8,385 B | 指定钱包成交 |
| `GET /positions?user=<0x>&limit=100` | **200** | 最高 96,457 B | ⚠️ **账户覆盖不全**，见下 |
| `GET /positions?user=0x000…0` | 200 | **2 B**（`[]`） | 假地址返回空数组，不报错 |
| `GET /value?user=<0x>` | **200** | 65 B | `[{"user":"0x…","value":3502.9758}]` |
| `GET /activity?user=<0x>&limit=5` | **200** | 4,014 B | 缺 `user` → **400** `{"error":"required query param 'user' not provided"}` |
| `GET /holders?limit=5` | **400** | 54 B | `{"error":"required query param 'market' not provided"}`（**必填 `market`**） |

**⚠️ `positions` 空数组语义 + 截断问题（已实测判定，**修正我先前"覆盖不全"的说法**）**

**判定方法**：`/value?user=<0x>` 与 `positions` 的 `ΣcurrentValue` 做交叉校验。实测二者**是同一个数**：

| 钱包 | positions 条数 | Σ`currentValue` | `/value` | 差 | 判定 |
|---|---|---|---|---|---|
| `0x204f72f3…` | 53 | 3,140.94 | 3,141.0811 | **0.14** | ✅ 一致 |
| `0x3dfb153c…` | 373 | 142,037.20 | 142,037.1989 | **0.00** | ✅ 一致 |
| `0x1f3a646c…` | 338 | 400,414.10 | 400,477.0762 | 62.98 | ✅ 一致（40 万量级下 0.016%） |
| `0x5268527977…` | **500（命中 limit）** | 365,426.35 | **493,001.3067** | **127,574.96** | ❌ **列表被截断** |
| `0x56687bf4…`（profit all 榜首，盈利 2205 万） | **0** | 0 | **0.0** | 0 | ✅ **真无持仓** |
| `0x1f2dd6d4…` | **0** | 0 | **0.0** | 0 | ✅ **真无持仓** |

**两个空钱包的补充证据**：`/activity` 各返回 100 条，但**最近活动时间都是 `2024-11-15`**（≈ 2 年前）→ 是**长期不动的休眠账户**，不是接口拿不到数据。

### ✅ 我能背书的结论（前端按此落地）

1. **`/value` 是权威聚合值**：4 个非截断钱包上 `ΣcurrentValue` 与 `/value` 差额 ∈ [0.00, 62.98]（相对误差 ≤ 0.02%）→ **可以用 `/value` 做交叉校验**。
2. **空 `[]` 更可能是「该用户确实无持仓」，不是「接口覆盖不全」**：
   判定依据 = 空 `positions` **且** `/value == 0`（两个独立端点一致），且账户长期休眠。
   → **我没有观测到任何一个「`positions` 为空但 `/value > 0`」的样本**（我在 200 笔成交里按成交活跃度找 40 个钱包，**全部都有持仓**，一个空样本都没找到）。
   → 所以"无法区分"**不成立**；在这个样本上可以判定为**真无持仓**。
3. **⚠️ 但真正的坑不是覆盖率，是 `positions` 会在 `limit` 处静默截断**：
   `0x5268527977…` 用 `limit=500` 返回**恰好 500 条**，而 `/value` 显示真实总值 **493,001**，
   `ΣcurrentValue` 只有 **365,426** → **少算 127,575（−26%）**。
   同一钱包在 `limit=100` 时只返回 100 条，`0x1f3a646c…` 从 100 条变 338 条。
   → **用 `limit=100` 会把大钱包的资产总额少算很大一截。** 这是比"覆盖率"严重得多的数据缺陷。

**前端落地规则（可直接照抄）**：
```
若 positions 条数 == 请求的 limit  →  显示 "TRUNCATED ≥N"，禁止把 ΣcurrentValue 当总额
若 positions 为空 且 /value == 0   →  显示 "NO POSITIONS"（可背书：真无持仓）
若 positions 为空 且 /value > 0    →  显示 "UNKNOWN"（未观测到，保守处理）
若 positions 非空                  →  用 /value 作总额，ΣcurrentValue 作校验，差额大则告警
```
`fetch_poly.py` 已实现该逻辑，输出 `cache\positions_semantics.json`，并在命中 limit 时打
`⚠️ ... 已截断(命中 limit)`。

**`positions` 字段（实测 29 个）**：
`proxyWallet` `asset` `conditionId` `size` `avgPrice` `initialValue` `grossInitialValue` `entryFeesUsdc`
`currentValue` `cashPnl` `percentPnl` `realizedPnl` `percentRealizedPnl` `totalBought` `curPrice`
`redeemable` `mergeable` `negativeRisk` `oppositeAsset` `oppositeOutcome` `outcome` `outcomeIndex`
`title` `slug` `eventId` `eventSlug` `icon` `endDate`

**`trades` 字段（实测 20 个）**：
`proxyWallet` `side`(BUY/SELL) `asset` `conditionId` `size` `price` `timestamp` `title` `slug` `icon`
`eventSlug` `outcome` `outcomeIndex` `name` `pseudonym` `bio` `profileImage` `profileImageOptimized` `transactionHash`

**`activity` 字段**：`proxyWallet` `timestamp` `conditionId` `type`(实测 `TRADE`) `size` `usdcSize` `transactionHash` `price` `asset` `side` `outcomeIndex` `title` …

### 2.6 CLOB 价格端点（用**真实** token_id 实测）

| 端点 | 状态 | 响应样例 |
|---|---|---|
| `GET /book?token_id=<id>` | **200** 5099 B / **404** | 有订单簿时返回 book；无订单簿时 **404** `{"error":"No orderbook exists for the requested token id"}` |
| `GET /midpoint?token_id=<id>` | **200** 17 B | 同上 404 语义 |
| `GET /spread?token_id=<id>` | **200** 19 B | 同上 |
| `GET /price?token_id=<id>&side=buy` | **200** 18 B | 同上；`side=sell` 同样 200 |
| `POST /prices` body `[{"token_id":…,"side":"BUY"}]` | **200** 98 B | 批量价格 |
| `POST /midpoints` body `[{"token_id":…}]` | **200** 91 B | 批量中间价 |
| `GET /time` | **200** 10 B | `1791636726`（epoch 秒） |
| `GET /simplified-markets?limit=3` | **200** 495,662 B | 体积仍很大 |
| `GET /sampling-markets` | **200** 2,514,209 B | ⚠️ 2.5 MB，慎用 |
| `GET /markets?limit=2` | **200** 1,838,355 B | ⚠️ 1.8 MB |

> ⚠️ **`token_id` 必须来自有订单簿的市场**。运动类/新上线市场常无订单簿 → 上述 5 个端点全返回 404 + `No orderbook exists`。
> 前端对 404 要做降级，不能当"价格=0"。

### 2.7 价格历史（PNL CURVE）

| 端点 | 状态 | 备注 |
|---|---|---|
| **`GET clob/prices-history?market=<token_id>&interval=1d&fidelity=60`** | **200** 605 B | ✅ **正确端点** |
| `GET clob/prices-history?market=<token>&interval=max&fidelity=1440` | 200 但仅 41 B | 实测只回 1 个点，**无长历史** |
| `GET clob/prices-history?market=<token>`（无 interval） | **400** | — |
| `GET gamma/prices-history?market=<token_id>&interval=1d` | **404** | `404 page not found\n` |
| `GET gamma/prices-history?market=<conditionId>&…` | **404** | — |
| `GET gamma/prices-history?market=<token>&startTs=…&endTs=…` | **404** | — |

**响应结构**：`{"history":[{"t":1791558051,"p":0.55},{"t":1791561628,"p":0.555}, …]}`
→ `t` = Unix 秒（UTC），`p` = 价格（0–1）。
→ ⚠️ **gamma 的 `prices-history` 不可用，PNL CURVE 只能走 clob**。

**实测深度（`interval=1d&fidelity=60`，5 个真实 token 逐个统计）**：

| token（前缀） | 返回点数 |
|---|---|
| `1765474298577311…` | 24 |
| `3063099424866789…` | 24 |
| `1049489628709615…` | 24 |
| `1741597684394540…` | 25 |
| `7040216012826195…` | 22 |

→ **每 token 22–25 个点，时间跨度 = 1.0 天**（`cache\stats.json` 的 `price_history_depth`）。

> ## 🔴 显式限制（口径声明，请与前端保持一致）
>
> **本看板的 PNL 曲线是「会话级短周期」，不是「回测级长周期」。要做长周期必须先解决历史数据源。**
>
> 依据：① `gamma/prices-history` 各种参数组合**实测全部 404**；② `clob/prices-history` 的 `interval=max`
> **实测只返回 1 个点**；③ `interval=1d&fidelity=60` 只给 **22–25 个点 / 1.0 天**。
> → **因此本看板不得展示任何"历史收益曲线 / 长期回撤 / 年化"之类需要长历史的指标。**
> 前端标注 `1 SAMPLE` / `session samples N` 的处理**与数据现状一致，是正确的**。

---

## 3. 市场对象字段字典（实测含义 + 真实样例）

### 3.1 价格类

| 字段 | 含义 | 实测样例 | 类型 |
|---|---|---|---|
| `outcomes` | 结果名数组。**JSON 字符串，需二次 parse** | `"[\"Yes\", \"No\"]"` | str→list |
| `outcomePrices` | 各结果价格（0–1）。**JSON 字符串，需二次 parse** | `"[\"0.115\", \"0.885\"]"` | str→**字符串**数组 |
| `bestBid` | 最优买价（别人愿买） | `0.53` | float |
| `bestAsk` | 最优卖价（别人愿卖） | `0.54` | float |
| `spread` | 价差。实测有 `bestBid` 时 `spread ≈ bestAsk − bestBid` | `0.01` | float |
| `lastTradePrice` | 最新成交价 | `0.535` | float |

⚠️ **`outcomePrices` 的元素是字符串不是数字**，前端务必 `parseFloat`。
⚠️ **`bestBid` 可缺失，`bestAsk` 实测不缺失**（见 §4 缺失率）。

### 3.2 涨跌类（**小数，不是百分数**）

`oneDayPriceChange` / `oneWeekPriceChange` / `oneMonthPriceChange` / `oneYearPriceChange`
→ `-0.02` = **−2%**。前端显示须 ×100。

### 3.3 量类（USD / USDC）

`volume`（累计） `volume24hr` `volume1wk` `volume1mo` `volume1yr` `liquidity`

实测（100 市场）：Σ`volume24hr` = **17,913,197**；Σ`volume` = **372,775,140**。

### 3.4 时间类

`endDate`（ISO，**UTC**） `endDateIso`（仅日期） `createdAt` `updatedAt` `startDate`

### 3.5 状态类

`active` `closed` `archived` `acceptingOrders` `enableOrderBook`
实测一致性：100 市场样本中 `closed=true` **0** 个、`active=false` **0** 个、`archived=true` **0** 个、
`active=true 且 closed=true` **0** 个 → **本样本无状态自相矛盾**（但 `events` 内嵌 markets 里有大量 `closed/archived` 腿，见 §5.2）。

### 3.6 分组类（**套利口径的关键**）

| 字段 | 含义 | 实测 |
|---|---|---|
| `negRisk` | 该市场是否属于**平台保证互斥**的 negRisk 集合 | 必填，100/100 有值 |
| `negRiskMarketID` | negRisk 集合 id | **42/100 有值，缺失 58%**（恰好等于 `negRisk=false` 的 58 个）→ **只在 negRisk 市场上有** |
| `groupItemTitle` | 集合内该腿的名字 | 89/100 有值 |
| `groupItemThreshold` | 阈值型腿的门槛 | 86/100 有值 |
| `conditionId` | 市场唯一条件 id（`0x` + 64 hex） | 100/100 有值，**0 重复** |
| `slug` | URL 标识 | 100/100 |
| `clobTokenIds` | CLOB token id 数组（**JSON 字符串**） | 100/100 |
| `question` | 市场问题文本 | 100/100，**合法 UTF-8** |

### 3.7 费用类（**成本硬约束**）

| 字段 | 含义 | 实测 |
|---|---|---|
| `feesEnabled` | 是否收费 | 100/100 有值，**85 个为 `true`** |
| `feeType` | 费率类型 | 85/100，实测值 `sports_fees_v3` |
| `feeSchedule` | 费率表 | 85/100 |
| `takerBaseFee` / `makerBaseFee` | 基础费 | 实测 `1000` / `1000`（仅 events 内嵌 markets 有此字段） |

**`feeSchedule` 实测原文**：
```json
{"exponent": 1, "rate": 0.05, "takerOnly": true, "rebateRate": 0.15}
{"exponent": 1, "rate": 0.04, "takerOnly": true, "rebateRate": 0.25}
```
→ `rate` 取值为 **0.04 或 0.05**（不是单一 4%）。`takerOnly: true` = **只有吃单收费**。

**费率的实证反解 → 现已升级为「官方文档确认」（不再是假设）**

**✅ 官方来源已找到，公式逐字一致**：
[Polymarket 官方文档 · Fees](https://docs.polymarket.com/trading/fees)：
```
fee = C × feeRate × p × (1 - p)          C = 股数, p = 价格
```
→ 即 `fee_per_share = feeRate × p × (1−p)`，**与我从 42 个真实持仓反解出的形式完全一致**。
官方并说明费用以 USDC 计、**围绕 50% 概率对称**（30¢ 与 70¢ 的交易费金额相同）—— 这正是 `p(1−p)` 的性质。

**官方分类费率表**（[Polymarket Help Center · Trading Fees](https://help.polymarket.com/en/articles/13364478-trading-fees)）：

| 分类 | `feeRate` | `rebateRate` |
|---|---|---|
| **Sports** | **0.05** | **15%** |
| Finance | 0.04 | 25% |
| Politics | 0.04 | 25% |
| Economics | 0.05 | 25% |
| Culture | 0.05 | 25% |
| Weather | 0.05 | 25% |
| Other / General | 0.05 | 25% |
| Tech | 0.04 | 25% |
| Geopolitics | **0** | — |

→ **实测交叉验证成功**：我抓到的运动市场 `feeType = "sports_fees_v3"`、`rate = 0.05`、`rebateRate = 0.15`
与官方表 **"Sports 0.05 / 15%"** **完全吻合**；另一批 `rate=0.04 / rebateRate=0.25` 对应 Finance/Politics/Tech。
→ 官方文档页另有一张 `feeRate = 0.07` 的费率表（我当前样本只观测到 0.04/0.05）→ **说明还有我没覆盖到的分类**。

**实证反解（作为独立交叉验证保留）**（真实钱包 `0x6daae042…` 的 42 个持仓）：

- 恒等式实测成立：`entryFeesUsdc = grossInitialValue − initialValue`
  例：`32.28007 = 1435.759077 − 1403.479` ✅
- 边际每股费用与 **`rate · p · (1−p)`** 一致（`rate=0.05`）：
  `p=0.4499 → 0.05×0.4499×0.5501 = 0.0123749` vs 实测 `0.01237494` ✅
  `p=0.5499 → 0.05×0.5499×0.4501 = 0.0123750` vs 实测 `0.01237495` ✅
  `p=0.5399 → 0.0124220` vs 实测 `0.01242000` ✅
- 但因 `avgPrice` 是**多次成交的时间加权均价**而费用是**非线性**函数，逐仓反解存在散布：
  `implied_rate_if_linear` 中位 `0.032667`，区间 `0 ~ 0.068603`。
- **汇总实测**：Σ`entryFeesUsdc` = **42.3661** / Σ`grossInitialValue` = **3,531.17** → **1.20% of notional**；
  **逐仓中位 2.5151%**，区间 **0% ~ 6.3950%**。
- 部分仓位费用为 **0**（如 `Will Trump and Putin meet next in China…`）→ 与官方表 `Geopolitics feeRate=0` 一致，**必须逐市场读 `feeSchedule`**。

**⚠️ 一处官方文档与 API 字段的不一致（如实记录，不强行闭环）**：
官方 API 参考把 `exponent` 描述为 "Exponent applied to the price component of the fee curve"，API 实测返回 `exponent: 1`，
而 Help Center 费率表的 exponent 列写 `0`。**两者对不上，我无法判定哪个是现行口径。**
好在 `exponent=1` 时 `p·(1−p)` 与官方主公式及 42 个持仓实测都一致，故本交付按 `exponent=1` 处理。
**若某市场出现 `exponent ≠ 1`，费用模型需重算 —— 当前样本未观测到。**

> **结论等级**：公式 = **官方文档确认 + 42 个真实持仓实证交叉验证**（高置信）。
> 唯一残留不确定项 = `exponent` 语义（已显式标出），**不是"找不到来源"**。
> ⚠️ 成本口径**不可用固定"4%"或"5%"** —— 必须逐市场读 `feeSchedule.rate`（缺失按 0.05 兜底），逐腿算 `rate·p·(1−p)`。
> `edges.json` 的 `fee_model` 与 `fee_source` 字段已写明模型与官方出处。

---

## 4. 数据质量校验（100 市场样本，2026-10-10 20:53 BJT）

**逐字段缺失率**（只列有缺失的；全部 48 个字段的完整表见 `cache\stats.json`）：

| 字段 | 缺失率 | 有值数 |
|---|---|---|
| `bestBid` | **8.00%** | 92/100 |
| `negRiskMarketID` | 58.00% | 42/100 |
| `oneMonthPriceChange` | 66.00% | 34/100 |
| `oneWeekPriceChange` | 35.00% | 65/100 |
| `oneDayPriceChange` | **30.00%** | 70/100 |
| `feeSchedule` / `feeType` | **15.00%** | 85/100 |
| `groupItemThreshold` | 14.00% | 86/100 |
| `groupItemTitle` | 11.00% | 89/100 |
| `bestAsk` / `spread` / `lastTradePrice` / `volume*` / `liquidity` / `endDate` / `conditionId` / `slug` / `question` / `active` / `closed` / `archived` / `negRisk` / `clobTokenIds` / `outcomes` / `outcomePrices` | **0.00%** | 100/100 |
| `category` / `spread_pct` / `bestBidYes` / `bestAskYes` / `umaEndDate` | 100%（**该端点根本不返回这些字段**） | 0/100 |

**异常值统计（实测全为 0）**：
| 检查项 | 结果 |
|---|---|
| `conditionId` 重复 | **0** |
| `conditionId` 为空 | **0** |
| `bestBid` 越界（<0 或 >1） | **0** |
| `bestAsk` 越界 | **0** |
| `volume` / `volume24hr` / `liquidity` 为负 | **0** / **0** / **0** |
| 交叉盘（`bestBid > bestAsk`） | **0** |
| `closed=true` 混入 | **0** |
| `active=false` / `archived=true` | **0** / **0** |
| 状态自相矛盾（`active=true` 且 `closed=true`） | **0** |
| 无双边报价 | **8/100**（与 `bestBid` 缺失率一致） |

> **可用样本量结论**：MISPRICING SCANNER 若要同时用 `bestBid` 与 `bestAsk`，**100 个市场里只有 92 个可用**（−8%）。
> 若要附带 `oneDayPriceChange`，**仅 70 个可用**（−30%）。

---

## 5. edge / 错价算法口径（**不编 fair value**）

> **硬约束**：截图里第三方终端的 "EDGE" 是它自有模型算的 fair value，**我们不许编一个数字冒充**。
> 下面只实现**有据可依、能当场验算**的口径，每条写明公式与数据来源字段。

### 5.1 (a) 单市场/集合内无套利检验 —— **结论：本扫描范围内无可用信号**

**公式**（定义性套利，不含任何模型）：
- 买入侧：若 `Σ(所有互斥腿的 bestAsk) < 1` → 买齐所有腿，必有一腿结算为 1，毛收益 `= 1 − Σask`
- 卖出侧：若 `Σ(所有互斥腿的 bestBid) > 1` → 卖空所有腿，毛收益 `= Σbid − 1`

**成立前提（缺一不可，这是血泪教训）**：
1. **腿必须互斥** → 只用 `negRisk=true`
2. **集合必须完整** → 所有腿都有双边报价，且无 `closed`/`archived`/`active=false` 的腿
3. 报价必须**真实可成交**（`book` 端点复核）

#### ⚠️ 坑 1：只按 `negRiskMarketID` 分组 + 单页扫描 → **系统性假套利**
实测（`probe3.py`，100 市场单页，按 `negRiskMarketID` 分组，11 个 ≥2 腿的集合）：
```
Σ(bestAsk): min=0.0050  median=1.0040  max=1.0200
Σ(bestBid): min=0.0030  median=0.9800  max=1.0000
买入侧"命中"(Σask<1) = 5/11
```
最极端的那个是**假的**：`Will Brooklyn Nets win the 2027 NBA Finals?` **legs=2、Σask=0.0050 → 假 edge +99.50%**。
NBA 总冠军集合显然不止 2 条腿 —— **第一页只扫到了 2/36 条腿**，Σ 自然远小于 1。
同理 `Athletic Club win on 2026-10-10` legs=2 Σask=0.70 → 假 +30%（足球是胜/平/负三路，缺 1 条腿）。

#### ⚠️ 坑 2：`negRisk=false` 的"集合"腿之间**根本不互斥** → Σ 完全失去意义
实测（`probe4.py`，`/events` 全量）：
```
negRisk=false: Σ(bestBid) 中位 8.9765，最大 66.8850（49ers vs. Seahawks, 327 腿）
negRisk=true : Σ(bestBid) 中位 0.9900，最大 1.0200
```
高值样例：`What price will Bitcoin hit in 2026?` Σbid=**18.626**（47 腿，价格阈值互相重叠）；
`Israel x Iran ceasefire continues through...?` Σbid=**25.741**（27 腿，截止日期互相嵌套）；
`Cowboys vs. Eagles` Σbid=**5.902**（280 腿，"More Markets" 各种 prop 盘）。
→ 这些按原始公式会产出 **+1862% / +2474%** 的荒谬"套利"。**这就是 Lead 说的假套利坑，实测确认存在且量级更大。**

#### ✅ 修正后的真实结果（`fetch_poly.py`，2026-10-10 20:53 BJT）

**扫描范围**：`/events?closed=false&limit=100&order=volume24hr`（100 事件 / 6,249 个市场）
**过滤链（缺一不可）**：`negRisk=true` **且** 所有腿都有双边报价 **且** **无僵尸腿** **且** 扣费

| 项 | 数值 |
|---|---|
| 含 ≥2 腿的事件 | 96 |
| **保留的合格集合** | **10** |
| 被剔除 | **86** |
| **净可行（扣费后为正）** | **0** |
| **买入侧 Σask<1 命中** | **0 / 10** |
| **卖出侧 Σbid>1 命中** | **0 / 10** |

**剔除主因（单一主因计数，三者互斥）**：
| 主因 | 数量 |
|---|---|
| `negRisk=false`（腿之间非互斥） | **56** |
| **僵尸报价**（腿已 `closed`/`archived`/`active=false` 却仍返回 `bestBid`/`bestAsk`） | **23** |
| 集合不完整（部分腿无双边报价） | **7** |

**多标签统计（一个集合可同时命中多个缺陷）—— 这张表才反映真实污染面**：
| 标签 | 数量 | 占 96 个集合的比例 |
|---|---|---|
| `INCOMPLETE_SET` 集合不完整 | **73** | 76% |
| `NON_MUTUALLY_EXCLUSIVE` 腿非互斥 | **56** | 58% |
| `rejected_nominal_positive` **未过滤前 gross 就为正（假套利候选）** | **58** | 60% |
| **`ZOMBIE_QUOTES` 僵尸报价** | **47** | **49%** |

> 🔴 **僵尸报价的规模比我第一轮报的大得多**：第一轮我按"单一主因"数出 6–7 个，
> 改用多标签统计后是 **47/96（49%）的集合含僵尸腿**。
> → **`active && !closed` 这道校验不是边角防护，是主力过滤条件** —— 前端加上它是对的，
> 而且**必须加**：不加的话，**近一半的集合**会被僵尸腿的陈旧盘口污染。
>
> **僵尸腿的判定**：`r.closed == True` 或 `r.archived == True` 或 `r.active is False`，
> 但这些腿**仍然返回非空 `bestBid`/`bestAsk`**。
> `edges.json` 里每一类剔除原因**已单列**（`reject_reason` 主因 + `reject_flags` 多标签 +
> `zombie_legs` 计数 + `nominal_positive` 布尔），前端可分别显示，**不会被混进"集合不完整"**。

10 个合格集合明细（全部为负）：

| 集合 | 腿数 | Σask | Σbid | 毛买入 | 净买入(扣费) | 毛卖出 | 净卖出(扣费) |
|---|---|---|---|---|---|---|---|
| Elon Musk # tweets Oct 10–12 | 10 | 1.0810 | 0.9920 | −8.10% | −11.52% | −0.80% | −3.89% |
| Aston Villa FC vs. Brentford FC | 3 | 1.0300 | 1.0000 | −3.00% | −6.34% | +0.00% | −3.29% |
| Balance of Power: 2026 Midterms | 5 | 1.0220 | 0.9890 | −2.20% | −4.36% | −1.10% | −3.21% |
| Ipswich Town FC vs. Fulham FC | 3 | 1.0200 | 0.9900 | −2.00% | −5.32% | −1.00% | −4.27% |
| Fed Decision in October? | 5 | 1.0110 | 0.9880 | −1.10% | −2.50% | −1.20% | −2.58% |
| Arsenal FC vs. Leeds United FC | 3 | 1.0100 | 0.9800 | −1.00% | −2.35% | −2.00% | −3.30% |
| Rayo Vallecano vs. Athletic Club | 3 | 1.0100 | 0.9800 | −1.00% | −4.31% | −2.00% | −5.26% |
| Chelsea FC vs. AFC Bournemouth | 3 | 1.0100 | 0.9800 | −1.00% | −4.04% | −2.00% | −4.99% |
| GAIS vs. IK Sirius | 3 | 1.0100 | 0.9800 | −1.00% | −4.31% | −2.00% | −5.25% |
| FC Barcelona vs. Getafe CF | 3 | 1.0060 | 0.9930 | −0.60% | −1.55% | −0.70% | −1.67% |

> 注意 `Aston Villa` 毛卖出 = **+0.00%**（Σbid 恰为 1.0000）→ 扣费后 −3.29%。
> **这正是"毛收益为正 ≠ 有套利"的最好例证**：`viable` 判定必须扣费。

**名义为正但被剔除的 negRisk 候选（必须显式报出，不许静默丢弃）**：

| 集合 | 腿数 | 僵尸腿 | Σbid | 标签 | 主因 |
|---|---|---|---|---|---|
| `NBA: 2027 Champion` | 36 | **6** | **1.0200** | `ZOMBIE_QUOTES` | 僵尸报价 |
| `Elon Musk # tweets Oct 6 – Oct 13` | 26 | **12** | **1.0180** | `ZOMBIE_QUOTES`,`INCOMPLETE_SET` | 僵尸报价 |
| `Next Prime Minister of Spain?` | 58 | **42** | **1.0020** | `ZOMBIE_QUOTES`,`INCOMPLETE_SET` | 僵尸报价 |

> **`NBA: 2027 Champion` Σbid=1.0200（毛 +2.00%）被剔除的原因**：
> **36 条腿里有 6 条已停用，却仍在返回 `bestBid`/`bestAsk`（僵尸报价）**——这 6 条正是把 Σbid 抬到 1.02 的来源。
> 剔除它是对的；同时因为 **+2.00% 毛收益 < 实测 2.5% 中位 taker 费**，即使不剔除，扣费后也是负的。
> **三个名义为正的候选全部带 `ZOMBIE_QUOTES` 标签** —— 僵尸腿是"名义正收益"的主要制造者。

#### **→ (a) 口径结论：该口径无信号。**
在 2026-10-10 20:53–21:05 BJT 的扫描范围内，**买入侧命中 0/10、卖出侧命中 0/10、扣费后净可行 0**。
Polymarket 的给定互斥集合上 **`Σ(bestAsk) ≥ 1.0060`、`Σ(bestBid) ≤ 1.0000`**（合格集合极值），
即**定义性套利空间不存在**，这与市场有效性预期一致。

**🟢 与前端独立实现互相验证**：前端（独立代码路径）scanner 输出 **0 EDGES**，
唯一候选 `Arsenal vs Leeds` 扣 5% 费后 **NOT VIABLE**；
本交付（`/events` 全量 + 多标签过滤）输出 **净可行 0**，`Arsenal FC vs. Leeds United FC`
毛买入 −1.00% / 净买入 −2.35%、毛卖出 −2.00% / 净卖出 −3.30%。
→ **两条完全独立的实现得出同一结论：零套利。**
→ 这也说明**用户截图里那 4 个 EDGE 是第三方终端的私有模型产物，不是可复制的定义性套利。**

### 5.2 (b) 买卖价差成本

`mid = (bestBid + bestAsk)/2`，`spread = bestAsk − bestBid`。

实测（100 市场，按 YES 价分层的中位 spread）：

| 价格层 | 中位 spread |
|---|---|
| 0–5¢ | **0.001** |
| 5–15¢ | 0.01 |
| 15–50¢ | 0.01 |
| 50–85¢ | 0.01 |
| 85–95¢ | 0.01 |
| 95–100¢ | **0.001** |

> 长尾（0–5¢ 与 95–100¢）**中位 spread 反而最小（0.1¢）**，中间价区 1¢。
> 注意这是**中位数**；`Σask` 与 `Σbid` 在长尾腿上的巨大差（如 NBA 集合 Σask=7.086 vs Σbid=1.020）
> 说明**个别长尾腿的 spread 极宽**（bid≈0.001 / ask≈0.999），中位数掩盖了尾部。

### 5.3 (c) favorite-longshot bias —— **只报统计事实，不叫它 edge**

**口径**：按 `outcomePrices[0]`（YES 价）分 6 层，统计市场数与成交量集中度。数据源 `gamma/markets/keyset`。
**样本**：100 个市场，Σ`volume24hr` = 17,913,197，Σ`volume` = 372,775,140。

| 价格层 | 市场数 | 市场占比 | 24h 成交量占比 | 累计成交量占比 | 中位 spread |
|---|---|---|---|---|---|
| **0–5¢** | 33 | 33.00% | 24.07% | **46.18%** | 0.001 |
| 5–15¢ | 9 | 9.00% | 11.12% | 12.88% | 0.01 |
| 15–50¢ | 25 | 25.00% | 25.67% | 24.85% | 0.01 |
| 50–85¢ | 17 | 17.00% | 11.45% | 7.08% | 0.01 |
| 85–95¢ | 4 | 4.00% | 5.31% | 7.80% | 0.01 |
| **95–100¢** | 12 | 12.00% | **22.40%** | **1.21%** | 0.001 |

**可复述的统计事实**：
1. **极端层占了 45% 的市场数**（0–5¢ 33% + 95–100¢ 12%），但中间层（15–50¢）只有 25%。
2. **0–5¢ 层吸收了 46.18% 的累计成交量**，是最大的单一价格层 —— 长尾（longshot）市场是成交量的主要去处。
3. **95–100¢ 层出现明显背离**：占 22.40% 的 24h 成交量，却只占 1.21% 的累计成交量 ——
   说明这一层的**近期**活跃度远高于其历史地位，是**新近涌入**的钱。
4. 这两层的中位 spread 都是 0.001（最窄），说明**长尾层流动性并不差**。

> ⚠️ 以上**仅为当前市场的价格分布与成交量集中度统计**，**不构成 edge，也不构成收益预期**。
> 要把它变成 edge 需要额外的 fair value 模型与样本外验证 —— 本交付**不做**，也不编。

### 5.4 (d) 跨市场一致性

用 `/events` 内嵌 markets 可做**同事件跨腿一致性**，即 §5.1 已做的 Σ 检验（这是最干净的 (d)）。
**实测找到的结构性矛盾（均为 `negRisk=false`，属数据口径问题而非可套利机会）**：
- `What price will Bitcoin hit in 2026?`（47 腿，阈值互相重叠）Σbid = **18.626**
- `Israel x Iran ceasefire continues through...?`（27 腿，截止日期互相嵌套）Σbid = **25.741**
- `US-Iran ceasefire continues through...?`（12 腿）Σbid = **9.908**
- `Cowboys vs. Eagles`（280 腿，prop 盘混合）Σbid = **5.902**

> **结论**：这些是**"同一事件下非互斥腿被并列成一个 event"**造成的口径现象，**不是套利**。
> 真正的"互斥但价格矛盾"（同事件不同截止日期 / 同候选人不同市场）在**本次扫描范围内未发现**
> —— 因为平台用 `negRisk` 标志把真正互斥的集合标注清楚了，合格集合的 Σ 都紧贴 1.00（0.9800–1.0000）。

### 5.5 🔴 四类假套利（`Σ≠1` 口径的完整陷阱清单）

> 这四类**全部在本次实测中真实出现过**，任何一类没过滤，`edges.json` 与前端 scanner 都会被污染。
> **过滤链必须四道齐全**，缺任何一道，输出的"EDGE"都不可信。

| # | 假套利类型 | 实测证据 | 会产生多假的结果 | 过滤条件 |
|---|---|---|---|---|
| **1** | **腿非互斥** | `What price will Bitcoin hit in 2026?` 47 腿（价格阈值互相重叠）Σbid = **18.626**；`Israel x Iran ceasefire…` 27 腿 Σbid = **25.741**；`49ers vs. Seahawks` 327 腿 Σbid = **66.885** | **+1862% ~ +6585%** | 只取 `event.negRisk == true` |
| **2** | **集合不完整** | 只按 `negRiskMarketID` 分组 + 单页扫描：`Brooklyn Nets 2027 NBA` 只扫到 **2/36 腿** → Σask = 0.0050；`Athletic Club 2026-10-10` 缺 1 腿（足球是胜/平/负三路） | **+99.50% / +30%** | 必须从 `/events` 取全部腿，且**每条腿都有双边报价**（实测 **73/96 = 76%** 的集合不满足） |
| **3** | **僵尸报价** | 腿已 `closed`/`archived`/`active=false` **却仍返回 `bestBid`/`bestAsk`**：`NBA: 2027 Champion` 36 腿中 **6 腿是僵尸** → Σbid 被抬到 **1.0200** | **+2.00%（名义为正）** | **`active && !closed && !archived`**（实测 **47/96 = 49%** 的集合含僵尸腿） |
| **4** | **费率常数误用** | 用固定"4%"或"5%"当成本。实测 `feeRate ∈ {0.04, 0.05}` 因分类而异，Geopolitics **= 0**；且费用是 `rate·p·(1−p)`，**非线性**（中间价最贵）。实测逐仓中位 **2.5151%**、区间 **0%~6.395%** | 低估或高估成本，把 −1.55% 的负收益说成正收益 | 逐市场读 `feeSchedule.rate`（缺失按 0.05 兜底），逐腿算 `rate·p·(1−p)`，再判 `viable` |

**实测：未过滤前有 58/96（60%）的集合"名义为正"**，套上四道过滤后**只剩 10 个合格集合，且净可行 = 0**。
→ **"60% 的集合看起来有套利"是假象的全部来源。零套利才是真相。**

**互为验证**：前端的四类校验 + 本交付的四道过滤链，两条独立实现都得到 **0 EDGES / 净可行 0**。

---

## 6. 六块面板 → 数据源映射

| 面板 | 数据源 | 口径 |
|---|---|---|
| **LIVE MARKETS** | `gamma/markets/keyset?order=volume24hr&limit=100` | `question` + YES/NO 概率（`outcomePrices`，**字符串需 parseFloat**）+ `oneDayPriceChange`（**×100** 显示）。⚠️ 该字段 30% 缺失 |
| **MISPRICING SCANNER** | `gamma/events?order=volume24hr` → 见 §5.1 | **只用硬事实口径**：`negRisk=true` **+ 集合完整（全腿双边报价）+ 无僵尸腿（`active && !closed && !archived`）** 的 `ΣbestAsk`/`ΣbestBid` 相对 1.00 的偏离，扣 `feeSchedule` 费用，加 `spread`。**不许编 fair value 冒充 EDGE**。⚠️ 缺"僵尸腿"校验会让 **47/96 集合**被污染 |
| **ARBER LEADERBOARD** | `lb-api/profit?window=all\|1d\|7d\|30d` | 排名 + `pseudonym` + `proxyWallet` + `amount`。⚠️ `1w`/`1m` **400**，别用 |
| **WALLET TRACKER** | `lb-api/profit` 取地址 → `data-api/positions?user=` | 逐个钱包拉持仓；`conditionId` 反查 `gamma/markets?condition_ids=<id>`（**实测可用**，返回 1 条）。⚠️ 空 `[]` 可能是**接口不覆盖**而非真无仓 |
| **COPYTRADE ENGINE** | `data-api/trades?limit=200` | **正解**：直接给 `side`/`size`/`price`/`title`/`timestamp`，不需要自建 diff 快照。首轮建基线，之后按 `transactionHash` 去重 |
| **PNL CURVE** | `clob/prices-history?market=<token>&interval=1d&fidelity=60` | `{t, p}` 序列。⚠️ **gamma 的 prices-history 是 404**；`interval=max` 只回 1 个点，**长历史拿不到** |

---

## 7. 响应耗时与字节数（给前端做超时/分页决策）

| 端点 | 耗时 | 字节 | 建议 |
|---|---|---|---|
| `keyset?limit=3` | 772 ms | 17,968 | — |
| `keyset?limit=60` | 1,308 ms | 446,588 | — |
| `keyset?limit=100&order=volume24hr` | 1,045–2,017 ms | 738,221 | 建议超时 ≥ 10 s |
| `keyset?limit=200` | 2,004 ms | 737,869 | 实际只回 100 条 |
| `/markets?limit=3`（弃用） | 715 ms | 17,639 | — |
| **`/events?limit=100&order=volume24hr`** | **2,978–3,963 ms** | **21,798,544（21.8 MB）** | ⚠️ **必须缓存，不能每次刷新直拉** |
| `/events?limit=2` | 687 ms | 45,105 | — |
| `clob/simplified-markets?limit=3` | 1,326 ms | 495,662 | — |
| `clob/sampling-markets` | 1,466 ms | 2,514,209 | ⚠️ 慎用 |
| `clob/markets?limit=2` | 3,119 ms | 1,838,355 | ⚠️ 慎用 |
| `clob/book` | 640 ms | 5,099 | — |
| `clob/prices-history` | 572–1,515 ms | 605–689 | — |
| `lb-api/profit` | 481–1,129 ms | 3,876 | — |
| `data-api/trades?limit=200` | 596–897 ms | 159,966 | — |
| `data-api/positions?user=` | 645–1,082 ms | 2 – 96,457 | — |

**建议超时**：普通端点 10 s；`/events?limit=100` 与 `clob/markets` 类 ≥ 20 s。
**建议缓存 TTL**：gamma 300 s、data-api 300 s、lb-api 600 s（与响应头 `Cache-Control` 一致）。

---

## 8. 数据稳定性复核（同一天三次独立实跑，证明数据是活的）

| 指标 | 实跑① 20:53 | 实跑② 20:59 | 实跑③ 21:05 |
|---|---|---|---|
| 市场数 | 100 | 100 | 100 |
| Σ`volume24hr` | 17,913,197 | 19,154,500 | **19,050,435** |
| Σ`volume`（累计） | 372,775,140 | 372,168,313 | **372,059,737** |
| `bestBid` 缺失率 | 8.00% | 7.00% | **6.00%** |
| `oneDayPriceChange` 缺失率 | 30.00% | 28.00% | **27.00%** |
| 双边报价数 | 92/100 | 93/100 | **94/100** |
| 合格套利集合数 | 9 | 10 | **10** |
| **扣费后净可行** | **0** | **0** | **0** |
| 参与集合总数 | — | 97 | **96** |
| 5–15¢ 层市场数 | 9 | 9 | **8** |
| 15–50¢ 层市场数 | 25 | 25 | **19** |
| 50–85¢ 层市场数 | 17 | 16 | **22** |
| 95–100¢ 层市场数 | 12 | 13 | **14** |
| 95–100¢ 层 24h 量占比 | 22.40% | 23.86% | **24.92%** |

→ 单页样本内数值随真实成交**持续变动**（成交量、缺失率、分层市场数都在动），
说明取到的是**实时数据而非静态快照**。
→ **但 (a) 三次都得 `净可行 = 0`，买入/卖出侧命中都是 0 —— 结论高度稳定。**

> ⚠️ **(c) 分层数字随样本漂移明显**（15–50¢ 层 25→19、50–85¢ 层 17→22），
> 因为 keyset 只取**按 volume24hr 排序的第一页 100 个**，排序本身随成交在变。
> → **(c) 的百分比只能作为「当下这一刻的分布快照」，不可当作稳定统计量引用。** 引用时必须带取样时刻。

---

## 9. 落盘清单

| 路径 | 内容 |
|---|---|
| `D:\量化\data\poly\CONTRACT.md` | 本文件 |
| `D:\量化\data\poly\fetch_poly.py` | 可复用取数脚本（显式代理/超时/重试/断点/缺失显式处理） |
| `D:\量化\data\poly\edges.json` | (a) 口径可选 feed，含 `rejected_sets` 与 `caveats` |
| `D:\量化\data\poly\probe2.py` `probe3.py` `probe4.py` | 契约实测脚本 |
| `D:\量化\data\poly\raw\probe2_result.json` `probe3_result.json` `probe4_result.json` | 每次调用的状态/耗时/字节/字段 |
| `D:\量化\data\poly\raw\keyset_page1_60.json` 等 | 原始响应留档 |
| `D:\量化\data\poly\cache\markets_scan.parquet` | 100 市场标准化表（48 列） |
| `D:\量化\data\poly\cache\event_markets.parquet` | 事件内市场全集（6,249 行 × 48 列） |
| `D:\量化\data\poly\cache\arb_sets.parquet` | (a) negRiskMarketID 分组结果（含假套利证据） |
| `D:\量化\data\poly\cache\events_sets.parquet` | (a) events 完整集合结果 |
| `D:\量化\data\poly\cache\longshot_buckets.parquet` | (c) 分层统计 |
| `D:\量化\data\poly\cache\wallet_fees.parquet` | 42 个真实持仓的费用反解（费率实证） |
| `D:\量化\data\poly\cache\wallet_positions.json` | 钱包持仓快照（COPYTRADE 基线） |
| `D:\量化\data\poly\cache\positions_semantics.json` | **空数组语义 + 截断判定**（`/value` 交叉校验明细，见 §2.5） |
| `D:\量化\data\poly\cache\leaderboards.json` | profit/volume 榜 |
| `D:\量化\data\poly\cache\price_history.json` | CLOB 价格历史 |
| `D:\量化\data\poly\cache\stats.json` | 本次实跑摘要（含全部请求耗时/字节） |
| `D:\量化\data\poly\cache\stats_failed.json` | **全失败**时的摘要（保护 `stats.json` 不被冲掉） |
| `D:\量化\logs\poly-*.log` | 全部执行日志 |

---

## 10. 未实测 / 已知缺口（如实声明，不猜）

1. **全市场扫描做不到** —— 分页失效，最多 100 市场（keyset）/ 6,249 市场（events 内嵌）。**命中率不可外推。**
2. **历史价格深度不足** —— `clob/prices-history` 实测每 token **22–25 个点 / 1.0 天**；`interval=max` 只回 1 个点。**PNL CURVE 无法做长周期回测（见 §2.7 显式限制）。**
3. ~~`positions` 覆盖率未知~~ → **已判定（§2.5）**：`/value` 是权威聚合值，空 `[]` 且 `/value==0` = **真无持仓**（实测 2 例，账户均休眠至 2024-11-15）。
   **未观测到"positions 空但 /value>0"的样本。** 但新发现：**`positions` 会在 `limit` 处静默截断**
   （实测某钱包 `limit=500` 返回恰好 500 条，`ΣcurrentValue` 比 `/value` 少算 **127,575 / −26%**）。
   **遗留**：`limit` 的真实上限、以及是否存在永远拿不全的钱包，仍未确定。
4. **未接 WebSocket** —— 实时推送通道（`ws-subscriptions-clob.polymarket.com`）**未实测**。
5. **未测写操作** —— 下单 / 撤单 / API key 鉴权（`L1`/`L2` header 签名）**完全未涉及**，本契约只覆盖公开只读端点。
6. **`negRisk` 的完整腿数无法从单市场字段推断** —— 必须走 `/events`。如果某事件在 events 列表之外，就无法判断集合完整性。
   → 这意味着 §5.1 的 **73/96 集合"不完整"**里，有一部分可能是**事件本身没列全腿**而非盘口缺失，两者我目前无法区分。
7. **`(c)` 只在 100 市场单页样本上跑过** —— 扩大到 `event_markets.parquet`（5,790–6,249 市场）的版本尚未产出（脚本已具备数据，下次跑可加）。
8. ~~费用公式是实证反解而非官方文档~~ → **已解决（§3.7）**：官方文档 `fee = C × feeRate × p × (1−p)` **逐字一致**，
   且官方分类费率表与实测 `feeType=sports_fees_v3 / rate=0.05 / rebateRate=0.15` 完全吻合。
   **唯一残留不确定项**：官方把 `exponent` 描述为价格分量的指数，API 返回 `exponent: 1` 而 Help Center 表写 `0`，**两者不一致，无法判定现行口径**（当前样本未观测到 `exponent ≠ 1`）。
9. **僵尸腿的 `active`/`closed` 语义未完全确认** —— 我按 `closed==True 或 archived==True 或 active is False` 判定僵尸腿，
   这些腿仍返回盘口。**但我无法确认它们是"结算后残留"还是"暂停但可恢复"**，只知其盘口不可信。
