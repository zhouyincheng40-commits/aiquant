# POLY // TERMINAL (terminal edition)

> **⚠ 两条硬前提，缺一不可**
> 1. **必须开代理** `127.0.0.1:7890`（Clash/mihomo）。本机实测：直连 `gamma-api.polymarket.com` 20s 超时（curl err=28/code=000），走代理才 200。代理关了 → Polymarket 六块面板全部 `OFFLINE`。
> 2. **必须 UTF-8**。本机 `chcp`=936(GBK)，Python stdout 会继承 GBK，遇到 `¢` 直接 `UnicodeEncodeError: 'gbk' codec can't encode character '\xa2'`。`run.cmd` 已内置 `chcp 65001` + `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`，**请一律用 `run.cmd` 启动**，不要直接 `python app.py`。
>
> 唯一不依赖代理的数据源是顶栏 **GLOBAL 市场带**（腾讯 `qt.gtimg.cn` 直连）—— 代理挂掉时它仍然刷新，这是刻意的韧性设计。

## 启动

```bat
run.cmd --once                      :: 一次性输出一屏纯文本后退出（可粘贴 / 落库 / 日志）
run.cmd                             :: Live 模式：1s 重绘，取数按每源 TTL，Ctrl-C 退出
run.cmd --once --color              :: 带 ANSI 颜色
run.cmd --rounds 4                  :: 冒烟测试：渲染 4 个节拍后退出
run.cmd --once --proxy http://127.0.0.1:7999   :: 指向死端口，验证 OFFLINE 降级路径
run.cmd --once --width 140 --rows 6  :: 换宽度 / 每个列表面板最多 6 行
run.cmd --once --edges <url>        :: 用外部 edge feed 覆盖内置套利口径
```

解释器固定为 `C:\Users\33096\.conda\envs\aiquant\python.exe`（3.11.17，已装 rich/requests）。

## 文件

| 文件 | 作用 |
|---|---|
| `feeds.py` | **数据层**。纯数据、无渲染副作用。入口 `snapshot(force=False, edges_url=None)` |
| `app.py` | rich 渲染层（顶栏 + GLOBAL 带 + 三列主网格 + 底栏三列） |
| `run.cmd` | 启动器（统一 UTF-8 环境 + 绝对解释器路径） |
| `_probe_completeness.py` | 诊断脚本：复现 keyset 分页/offset 行为与单页分组造成的残缺集合 |
| `_probe_audit.py` | 诊断脚本：逐个 negRisk 集合审计「为什么被剔除」（腿数/已关闭/缺盘口） |

---

## 数据层复用接口

```python
from feeds import snapshot, get_feeds, CONFIG

snap = snapshot(force=True)          # 强制重新取数；force=False 时按每源 TTL 复用缓存
snap = snapshot()                    # 高频调用安全：只重取"过期"的源
f = get_feeds(proxy="http://127.0.0.1:7890")   # 可注入代理/配置的实例
```

`snapshot()` 返回 dict，键与你约定的结构一致，**永不抛异常**（失败进 `health` / `errors`）：

| 键 | 类型 | 说明 |
|---|---|---|
| `header` | dict | `trackedPnlSum / trackedCount / cashPnl / cashPnlDir / edges / win24hPct / wallets / lastSyncTs` + `text{}` 预格式化串 |
| `scanner` | list | 定义性套利行（见口径），含 `sumAsk / sumBid / sumSpread / legs / feeRate / grossPct / netPct / viable / side` |
| `wallets` | list | `address / addressShort / kind(WHALE\|SCOUT) / amount / amount24h / pnlText / subText` |
| `markets` | list | `id / question / yesPrice / noPrice / volume24hr / liquidity / chgPct / chgBasis / chgDir` |
| `copy` | list | `addressShort / side / outcome / title / notional / timestamp / actionText / sizeText` |
| `pnl` | dict | `ok / total / delta / positions / wallets / samples[] / text / dir / deltaText / meta / reason` |
| `leaderboard` | list | `rank / address / label / amount / amount24h / has1d / pos1d / text / rightText` |
| `cnBand` | dict | `ok / direct / items[]{code,name,price,chgPct,dir,priceText,chgText} / amountYi` |
| `meta` | dict | 扫描范围与剔除计数等（见下） |
| `health` | dict | 每面板 `ok \| empty \| offline \| nodata` |
| `ok` / `proxyDown` / `errors` / `ts` | — | 全局状态；`proxyDown=True` 即判为代理不可达 |

## 取数节奏（按端点真实新鲜度分频，不是一刀切 300s）

| 端点 | Cache-Control | TTL | 说明 |
|---|---|---|---|
| `data-api /positions` | **`max-age=5`** | **60s** | 全系统唯一真正实时的通道 → PNL / 实时感靠它 |
| `gamma /markets/keyset` | `max-age=300` | 300s | 与 SCANNER 共用同一个缓存条目，每轮只打一次 |
| `data-api /trades` | `max-age=300` | 300s | COPYTRADE ENGINE |
| `lb-api /profit?window=all` | `max-age=600` | 600s | LEADERBOARD |
| `lb-api /profit?window=1d` | `max-age=300` | 300s | LEADERBOARD 右列 |
| `qt.gtimg.cn`（CN 带） | — | 60s | 直连，独立于代理 |

渲染层每 2s 调一次 `snapshot(force=False)`，由上面的 TTL 决定谁真的重新取数；**1s 重绘用的是本地缓存**。

## 字段映射表

### LIVE MARKETS ← `gamma /markets/keyset`
| 字段 | 落点 |
|---|---|
| `markets[]` | 响应是**对象** `{markets:[…], next_cursor}`，不是数组 |
| `question` | 市场名（截断到列宽） |
| `volume24hr` / `liquidity` | 元信息 `VOL24 …` / `LIQ …` |
| `outcomePrices` | YES/NO 分位价（**JSON 字符串，必须二次 parse**） |
| `oneDayPriceChange` → 降级 `oneHourPriceChange` → `--` | 涨跌；**实测只 23/40 有 day、21/40 有 hour** |

### MISPRICING SCANNER ← `gamma /markets/keyset` + `/events/<id>`
| 字段 | 落点 |
|---|---|
| `events[0].id` | 候选集合分组键（**只用于发现候选**，腿数一律以 `/events` 为准） |
| `ev.negRisk` / `ev.enableNegRisk` | 互斥穷尽门槛 |
| `ev.markets[]` | 该集合**全部**腿（含 `closed` 标志） |
| `bestAsk` / `bestBid` | `ΣASK` / `ΣBID` 与大字价格 |
| `spread` | `ΣSPR`（只显示，不折进百分比） |
| `feeSchedule.rate` | 逐市场读取，实测 **0.04~0.05 浮动**，缺失按最保守值 |
| `orderPriceMinTickSize` | EDGE 精度截断（0.01） |
| `closed` | 完整度校验用 |

### WALLET TRACKER / ARBER LEADERBOARD ← `lb-api /profit`
| 字段 | 落点 |
|---|---|
| `proxyWallet` | 地址（截断 `0x5668...5839`） |
| `amount` | 金额；榜单左列 `+$22.1M` |
| `pseudonym` | 显示名 |
| 1d 榜同地址 `amount` | 榜单右列 `+$x 24H`；**两榜地址不重叠时为 `--`**（实测 overlap 0/20） |
| — | `WHALE`(前3) / `SCOUT`(4-7)：**本层基于真实排名的派生规则**，不是接口字段 |

### COPYTRADE ENGINE ← `data-api /trades`
`proxyWallet`→地址行（含 `HH:MM:SS`）；`title`+`side`+`outcome`→动作行；`size`×`price`→金额。
`source=TRACKED_WALLETS`（追踪钱包有流水）或 `GLOBAL_FEED`（都为空时回落全站最新，标签如实显示）。

### PNL ← `data-api /positions`（追踪前 5 个钱包聚合）
`cashPnl` → 大字总额 + 右下角**本会话实测增量**；折线 = **本会话采样点**（标签写明 `N SAMPLES`），不是历史回填。

### GLOBAL 市场带 ← `qt.gtimg.cn`（**GBK**，`s_` 精简格式）
`[1]`名称 `[3]`现价 `[5]`涨跌幅% `[7]`成交额(万元)；`两市 = 上证[7]+深证[7]`（万元→亿元）。
窄终端下按显示宽度贪心装填，装不下的指数省略为 `…`，但**始终保留「两市」与 `DIRECT`**。

---

## MISPRICING SCANNER 口径（正确性核心，改动前必读）

**定义性套利：互斥穷尽集合的 Σ 价格偏离 1.00。**

```
cost      = Σ bestAsk_i      edge_buy  = 1 - cost        edge_buy_pct  = (1/cost - 1)*100
proceeds  = Σ bestBid_i      edge_sell = proceeds - 1    edge_sell_pct = (proceeds - 1)*100
```

四道门槛，缺一不可：

1. **互斥穷尽门槛：只算 `negRisk=true` 的事件。**
   实测：真互斥集合 Σ bestBid ≈ **0.99–1.02**；非互斥事件（`US-Iran ceasefire continues through <日期A/B/C>`、Dota/Valorant 赛事）Σ bestBid = **3.91 / 13.86 / 2.00** —— 那不是套利，是重叠结果。
2. **集合完整度硬校验（poly-data 挖出的更隐蔽的坑）：必须从 `/events/<id>` 取全集，且事件内每条腿都必须「未关闭 + 同时有 bestAsk 与 bestBid」，任一条腿不满足 → 整条剔除。**
   为什么必须：`/markets/keyset` 单页分组会拿到**残缺集合**。实测 `Will Brooklyn Nets win the 2027 NBA Finals?` 在第一页只出现 **2 条腿**（事件实际 **36 条**），Σask 会算成 0.005 → 公式给出 **+99.5% 的假套利**；足球同理（`Athletic Club win` 单页 2 腿，实际 3 路，Σask=0.70 → +30% 假套利）。
   实测被完整性校验拦下的真实案例：**Ballon d'Or 2026**（89 条腿里 12 条已关闭 + 21 条缺盘口）、**Brazil Presidential Election**（32 条里 17 关闭 + 17 缺盘口）、**Elon Musk #tweets**（10 条里 3 关闭 + 7 缺盘口）。
   > 注意：`cumulativeMarkets` 实测是**布尔值**不是腿数，不能用来声明集合大小，故不采用。
   > **候选发现**放宽为「第一页出现 ≥1 条腿的 event」（不是 ≥2），否则只出现 1 条腿的集合会被整条漏掉；真实腿数一律以 `/events` 为准。
3. **逐市场读 `feeSchedule.rate`**，不许用常数。实测 0.04~0.05 浮动（样例 `{"rate":0.05,"rebateRate":0.15}`）。缺失时取 `max(已有 rate, 0.05)` 并统计缺失条数写进脚注。
4. **选边按毛额，可行性按扣费后净值**：净值 > 0 → 绿徽章；毛额 > 0 但净值 ≤ 0 → 琥珀 + `NOT VIABLE`。

**两条必须知道的口径取舍：**
- **`Σspread` 只随行显示，不折进百分比。** 36 腿集合 Σspread 可达 606.6¢，折进去会得到 `-174%`/`-626%` 这种无信息量数字，而**判定结论不变**（仍 NOT VIABLE）。已获 Lead 批准。
- 行内徽标是 `ΣBID / ΣASK`，**不是**参考图的 `YES / NO` —— 这里是整组求和，标 YES/NO 会误导。

> ⚠ 这是**定义性套利**（Σ 价格偏离 1.00），与参考截图里那个基于私有模型的 EDGE **不是同一个东西，不可直接比较**。

### 扫描范围（自曝在脚注 `range:`）
`keyset` 第一页、`limit=100`、`order=volume24hr`。实测限制：
- `limit=200` 也只返回 **100** 条（hard cap）
- `offset=100` → **422 `{"error":"offset is not allowed on keyset endpoints"}`**
- `cursor` / `next_cursor` / `after` / `page` 四种游标参数**全部返回同一页**（60 条全重复、游标不变）

→ 扫描范围恒为「volume24hr 降序第一页 100 个市场里出现的 event，取成交量最高的前 14 个展开」。`N EDGES FOUND` **低估全市场真实数量**，这是已知口径缺陷，面板脚注固定自曝。

### 可选 feed（热插）
`edges.json` / `data/poly/edges.json` / `--edges <url>` 任一存在即覆盖内置算法。本模块**只读**它们，**绝不写** `D:\量化\data\poly\`。

---

## 面板状态语义

| 状态 | 含义 |
|---|---|
| 有数据 | 正常渲染 |
| `NO DATA` | 接口通但该面板无数据（如某钱包 0 持仓）—— **正确行为，不拿别的钱包凑数** |
| `OFFLINE` | 该面板数据源失败 |
| 顶栏 `● LIVE` | 六块面板全通 |
| 顶栏 `● DEGRADED n/6` | 部分失败 |
| 顶栏 `● OFFLINE — PROXY DOWN` + 红色横幅 | Polymarket 全线失败 → 判为**代理不可达**；此时 CN 带仍 `DIRECT` 刷新 |

---

## 已知陷阱（后来人勿踩）

1. **不要「修」Polymarket 里看起来像乱码的中文。** `question` 是合法 UTF-8（字节级验证 `In\xe1cio` = `Inácio`）；PowerShell 里看到的 `InÃ¡cio` 是 **GBK 控制台代码页的显示假象**。做 latin1→utf8 转换会把好数据改成乱码。→ **字节级验证优先于任何显示层观察。**
2. **腾讯必须 `resp.content.decode('gbk')`**，默认 UTF-8 解会把 `上证指数` 变乱码。→ 同一枚硬币两面：**该管的编码要管对，不该管的别乱转。**
3. **stdout 必须 UTF-8**（见文首）。`run.cmd --once > out.txt` 之后，若你用 `Get-Content -Encoding GBK` 去读，又会看到乱码 —— 那是读取端的问题，不是程序的问题。**读日志也要用 UTF-8**。
4. `outcomes` / `outcomePrices` 是 **JSON 字符串**，直接当数组用会拿到一堆字符。
5. `lb-api` 的 `window` **只认** `all` / `1d` / `7d` / `30d`；`1w`/`1m`/`week`/`month` 一律 **400 `invalid request`**。
6. **keyset 分页无效**（见上），别试图翻页。
7. `oneDayPriceChange` / `bestBid` **部分存在**，必须有降级链，绝不补假值。
8. 中文是**双宽**字符：所有列宽计算必须用 `rich.cells.cell_len`，用 `len()` 会把边框撑破。

## 已知缺数据 / 降级清单

| 面板 | 缺什么 | 当前显示 |
|---|---|---|
| ARBER LEADERBOARD | 1d 榜与 all 榜地址重叠实测 **0/20** | 右列全部 `--`，脚注自曝 `24H overlap 0/20` |
| ARBER LEADERBOARD | 参考图的 `847tx`（交易笔数）lb-api 无此字段 | **改用真实 24H 盈亏**，宁换真字段不造假字段 |
| LIVE MARKETS | NO 侧无独立涨跌字段 | NO 列涨跌留空（不编） |
| LIVE MARKETS | 参考图的市场「类别」keyset 无此字段 | 用 `VOL24` / `LIQ` 顶替 |
| PNL | 依赖追踪钱包持仓；某钱包实测 0 持仓 | `NO DATA`；折线需要 ≥2 个真实采样点 |
| SCANNER | 仅扫第一页 100 市场；4~5% 费率下定义性套利**基本不存在** | 常见 `NOT VIABLE` 或 `NO DEFINITIONAL ARB` |
| 参考图特征 | 截图里那 4 个 EDGE 基于私有模型 | **在我们的硬口径下大概率是假的**（Lead 已向用户说明） |

## 配色约定（全页统一）
**绿 = 正/涨/买/盈利**，**红 = 负/跌/卖/亏损**，**琥珀 = EDGE 非可行 / 同步计时**，灰 = 元信息，白 = 主数值。
A股带**也沿用同一套**（绿涨红跌），未采用 A股「红涨绿跌」习惯 —— 保证全页只有一套语义。

### 关于「NBA: 2027 Champion」那一行的复核结论（Lead 点名）
用 `/events/478277` 复核：**36 条真实腿、`closed=0`、缺盘口 `0` → 它是完整集合**，当时那行 `36 OUT / SELL ALL` 的**计算口径是对的，不是假套利**。
它后来从结果里消失，是因为它不在当前扫描窗口的候选里（`limit=100` 换了排序后该事件在第一页出现的腿数 < 2）。这也是我把候选发现从「≥2 腿」放宽到「≥1 腿」的原因。
真正被新校验拦下的是另外 3 个集合（Ballon d'Or / Brazil / Elon Musk），见上文。

## 验证记录（本机实测，可复现）

| 验证项 | 命令 | 结果 |
|---|---|---|
| 六面板真实数据量 | `python feeds.py` | `markets 100 / wallets 7 / copy 8 / leaderboard 20 / scanner 1 / pnl 60 POS·5 WALLETS / cnBand 6 项`，`health` 全 `ok`，`errors` 空 |
| `--once` 输出 | `run.cmd --once --rows 4` | 一屏完整渲染（见交付记录） |
| 代理不可达降级 | `run.cmd --once --proxy http://127.0.0.1:7999` | 不崩、exit 0；横幅 `PROXY 127.0.0.1:7890 UNREACHABLE … CN BAND STILL LIVE (DIRECT)`、顶栏 `● OFFLINE — PROXY DOWN`、六面板 `NO DATA`、**CN 带仍出实时数据** |
| Live 多轮不崩 | `run.cmd --rounds 4 --rows 3` | 4 轮全部 `rendered ok`，无 `Traceback`、无 `UnicodeEncodeError`，exit 0 |
| 编码正确性 | 用 UTF-8 读日志 | `¢ Σ │ ✚ —` 全部完好；`上证指数`、`Inácio` 均未乱码 |

_不构成投资建议。所有数值来自接口实时返回，代码内无任何硬编码价格/地址/金额。_
