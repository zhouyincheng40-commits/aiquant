# OKX 实时行情中继（backend relay）

**真·推送、零轮询**：一个进程订阅 OKX 公开 WebSocket，把数据同时推给浏览器（WebSocket + SSE），并按通道落盘攒历史。
本项目**只用公开通道，不需要任何 API key，不读写任何凭据**。

- 页面入口：**http://127.0.0.1:8910/**
- 后端目录：`D:\量化\okx-terminal\relay\`
- 前端目录：`D:\量化\okx-terminal\web\`（由前端同事维护，后端只挂载不写入）
- 数据目录：`D:\量化\okx-terminal\data\`

---

## 1. 启动

```powershell
# 推荐：启动器（内部用绝对路径 python，并设 PYTHONUTF8=1 / PYTHONIOENCODING=utf-8）
powershell -ExecutionPolicy Bypass -File D:\量化\okx-terminal\relay\run-relay.ps1

# 等价的手工启动
$env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"
& "C:\Users\33096\.conda\envs\aiquant\python.exe" D:\量化\okx-terminal\relay\relay.py
```

停止：`Ctrl+C`（脚本对 Windows ProactorEventLoop 不装 signal handler，用 KeyboardInterrupt 退出，退出前会 flush 落盘）。

### 端口
固定 **8910**。**端口被占用时进程打印 `FATAL: port 127.0.0.1:8910 is already in use` 并以退出码 2 结束，不会静默换端口。**
（8899 是 Polymarket 看板的，本项目不碰。）

### 常用参数
| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` / `--port` | 127.0.0.1 / 8910 | 监听地址与端口 |
| `--proxy` | 无（自动探测） | 强制上游代理，例如 `http://127.0.0.1:7890` |
| `--no-proxy` | off | 强制直连，绝不用代理 |
| `--book-channel` | `books` | `books`(400 档增量) 或 `books5`(5 档全量) |
| `--book-levels` | 20 | 向下游推送的档位数（内部仍维护全部 400 档） |
| `--candle-channel` | `candle1m` | business 端点 K 线通道 |
| `--ping-interval` | 20 | 每 N 秒发文本 `ping` |
| `--idle-timeout` | 45 | N 秒没收到任何帧 → 判死并重连 |
| `--flush-interval` | 5 | 落盘 flush 周期（秒） |
| `--trades-ring` / `--ticks-ring` | 500 / 600 | 环形缓冲条数 |
| `--no-prime` | off | 跳过启动时那一次 REST 快照 |

---

## 2. ⚠️ 必读坑位：aiohttp 连不上 OKX

**在这台机器上，`aiohttp` 用 websocket 直连 OKX 是连不上的，必须显式传代理。**

实测（唯一变量 = 连接方式）：

| 方式 | 结果 |
|---|---|
| `aiohttp` 直连（`trust_env=False`） | ❌ `ClientConnectorError: Cannot connect to host ws.okx.com:8443 [指定的网络名不再可用。]`（TCP 能连上，TLS ClientHello 被远端重置，WinError 64） |
| `aiohttp` 直连（`trust_env=True`） | ❌ 同上 —— **`trust_env` 对 `ws_connect` 无效，它不会从环境变量取代理** |
| `aiohttp` 显式 `proxy=` | ✅ 握手 874~1631ms，数据正常 |
| `websocket-client` 直连 | ✅ 能通（所以"直连就通"这个结论是**库相关**的，不是网络的性质） |

结论与做法：
```python
ws = await sess.ws_connect(URL, proxy="http://127.0.0.1:7890", ...)   # 显式 proxy 才是关键
```
`relay.py` 启动时执行一次路由探测（`detect_route`）：先试直连，失败再试 `OKX_PROXY` / `HTTPS_PROXY` 等环境变量，再试本机候选 `127.0.0.1:7890/7891/10809/1080`。探测结果进日志、`/health` 和每条 `stats`：
`"route":{"mode":"proxy","proxy":"http://127.0.0.1:7890","direct_ok":false}`。
**只读取、绝不修改用户的系统代理设置**；代理串里若含账号密码会被 `***` 遮蔽后再落日志。**本机代理只要本机可达即可，不需要任何 OKX 凭据。**

---

## 3. 上游订阅（两条连接，全部公开免登录）

| 组 | 端点 | 通道 × 标的 |
|---|---|---|
| `public` | `wss://ws.okx.com:8443/ws/v5/public` | `tickers` × BTC-USDT、ETH-USDT、**BTC-USDT-SWAP、ETH-USDT-SWAP**；`trades` / `books` / `bbo-tbt` × BTC-USDT、ETH-USDT；`open-interest` / `funding-rate` × BTC-USDT-SWAP、ETH-USDT-SWAP |
| `business` | `wss://ws.okx.com:8443/ws/v5/business` | `candle1m` × BTC-USDT、ETH-USDT |

共 **16 个订阅**。启动日志逐条打印 `SUBSCRIBED ... (n/16)`，齐了再打印 `ALL SUBSCRIPTIONS CONFIRMED`。
`{"event":"error"}` 会被记录（进 `sub.errors` 与日志 ERROR）**并 5 秒后自动重订阅**，绝不静默吞掉。

> **为什么给 SWAP 也订 `tickers`**：此前 SWAP 只订了 `open-interest`/`funding-rate`，于是 `snapshot` 里
> `BTC-USDT-SWAP.last` 是 `null`，前端无法区分"这个合约没有价格"和"后端坏了"。现在 SWAP 也有真实最新价
> （实测 `BTC-USDT-SWAP last=83031.8`、`ETH-USDT-SWAP last=2499.76`），并且**所有标的的 snapshot 结构完全一致**：
> `has_ticker` 布尔字段显式说明价格有没有到位，缺的数字一律是显式 `null`（= 该通道还没推送），不是缺字段。

### 通道实测吞吐（本机，2026-10-11，代理路由）
| 通道 | 端点 | 实测条/秒 | 单帧平均字节 | 备注 |
|---|---|---|---|---|
| `tickers` BTC-USDT | public | 6.22 | 390 | 每帧全量快照 |
| `books` BTC-USDT（**当前选用**） | public | **9.11** | **648**（快照帧最大 25912） | **400 档、快照+增量** |
| `books5` BTC-USDT | public | 3.11 | 464 | 只有 5 档，每帧全量 |
| `trades` BTC-USDT | public | ~1.8 | — | 逐笔，稀疏时更少 |
| `bbo-tbt` | public | ~2.8 | — | 最优买卖逐笔 |
| `open-interest` | public | ~0.4 | — | 稀疏 |
| `funding-rate` | public | 事件驱动 | — | 稀疏，命中即有 |
| `candle1m` | business | ~1.25 | 170 | 每分钟一根，秒级刷新 |
| **服务端合计 `msgs_per_s`** | — | **31 ~ 41** | — | `/health` 与 `stats` 实测（含 `bbo-tbt` 等不向下游广播的通道） |

### 下游实测（连续 130 秒，`/ws` 与 `/sse` 各一个客户端）
```
ws_total = 3286    ws_msgs_per_s = 25.21      sse_total = 3286   sse_msgs_per_s = 25.21
逐类：ticker 1633 (=12.56/s，两标的各约 6.3/s)   book 1264 (=9.72/s)   stats 129 (=0.99/s)
      trades 137    candle 84    oi 32    funding 6    snapshot 1（首帧）
客户端队列丢弃 dropped=0（无背压丢失）
```
`/ws` 与 `/sse` 两类消息**逐条一致**（同为 3286 条），证明两条通道推的是同一份数据。
下行 25.2/s 与上行 31~41/s 的差额来自 `bbo-tbt`：它只更新 snapshot 内部状态，不单独广播（避免刷屏）。

**为什么选 `books` 而不是 `books5`（实测依据）**：
`books` 档位实测最深 **400 档**、频率 9.11/s，比 `books5`（5 档、3.11/s）信息量与刷新率都高；单帧平均 648 B、**快照帧 25912 B**，带宽约 5.8 KB/s（每标的），完全可承受。
代价是 `books` 是**增量推送**：`prevSeqId == -1` 才是快照（400 档），其余帧只带变化的档位（0 档也常见），因此 `relay.py` 内做了**状态重建**（`BookState`：价格→数量的买卖盘表，数量为 0 即删除该档）。
`prevSeqId != 上次 seqId` 说明丢包 → 记 `gaps` 并 **unsubscribe+subscribe 强制重取快照**。
⚠️ 该通道 `checksum` 字段实测恒为 `0`，**无法用它做校验**，只能靠 `prevSeqId` 连续性。

### ⭐ 启动回填（REST backfill / prime）
环形缓冲最初是**进程启动后才开始攒**的，所以刚重启时页面图表上只有一两根 K 线。为此启动时会做**一次性** REST 回填
（这是本进程**唯一**的 REST 调用，之后实时数据全部走 WS，绝不轮询）：

| 回填内容 | 接口 | 默认条数 | 落到哪里 |
|---|---|---|---|
| 1m K 线 | `/api/v5/market/candles?bar=1m` | **300 根**（`--prime-candles`） | `candles` 环（内存） |
| 最近逐笔 | `/api/v5/market/trades` | **200 笔**（`--prime-trades`） | `trades` 环（内存） |
| 当前快照 | `/api/v5/market/ticker` | 4 个标的各 1 次 | `last/bid/ask/open24h/high24h/low24h/vol24h` |

**实测（2026-10-11 16:39:49，代理路由）**：
```
BACKFILL candles inst=BTC-USDT rows=300 ring=300 ms=643  ok
BACKFILL candles inst=ETH-USDT rows=300 ring=300 ms=383  ok
BACKFILL trades  inst=BTC-USDT rows=200 ring=200 ms=361  ok
BACKFILL trades  inst=ETH-USDT rows=200 ring=200 ms=353  ok
BACKFILL ticker  inst=BTC-USDT       last=83073.5  ms=350 ok
BACKFILL ticker  inst=ETH-USDT       last=2500.68  ms=364 ok
BACKFILL ticker  inst=BTC-USDT-SWAP  last=83031.8  ms=355 ok
BACKFILL ticker  inst=ETH-USDT-SWAP  last=2499.76  ms=358 ok
REST BACKFILL done status=ok ok=8 fail=0 elapsed_ms=3172
```
要点：
- **8 个调用 8 个成功，总耗时 3172 ms**（单次 350~643 ms），全部结果写进日志与 `snapshot.prime`。
- 每个请求有独立超时（`--prime-timeout`，默认 10s），**任何一次失败不影响 WS 正常接收**，只记 `fail` 并继续；
  `snapshot.prime.status` 会是 `ok` / `partial` / `failed` / `error`。
- 回填数据**只进内存环，不落盘**（jsonl 只记 WS 实时观察到的数据，避免把历史数据混进"实时记录"里），
  且**不向下游广播**（不会让页面在启动瞬间被 300 根 K 线刷屏）。
- 回填与 WS 并发进行：合并时按 `ts`/`tradeId` 去重、按时间排序，**实时数据优先于回填数据**。
- `--no-prime` 可整体关闭；关闭后冷启动时 `candles`/`ticks`/`trades` 从 0 开始增长。
- 开启动时是**真正的冷启动值**：`candles=0`；回填完成后 `candles=300`（实测）。

---

## 4. 下游接口

| 路径 | 说明 |
|---|---|
| `GET /` | 前端页面（显式路由读 `web/index.html`，带 `Cache-Control: no-store`，前端改完刷新即可见） |
| `GET /ws` | WebSocket 推送 |
| `GET /sse` | Server-Sent Events（`text/event-stream`，`Access-Control-Allow-Origin: *`） |
| `GET /health` | 健康检查（JSON） |
| `GET /debug` | 后端自带的极简实时视图（不给前端用，仅用于冒烟与排障） |
| `GET /data/` | 静态浏览落盘 jsonl 文件 |
| `GET /admin/kill-upstream[?group=public]` | 调试钩子：强断上游以验证重连（仅本机监听） |

同一份消息**同时**推给 `/ws` 和 `/sse`，前端自己选。新客户端连上**立刻收到 `snapshot`**（不会白屏）。
慢客户端不会拖垮服务端：每客户端独立队列，队满丢最旧的并计入 `dropped`。

### 消息协议（字段表）

所有消息都是**单行 JSON**（SSE 为 `data: <json>\n\n`，`retry: 3000`）。
**所有 `ts` 都是 OKX 原始毫秒 UTC，后端原样透传，不做任何本地改写**（显示由前端负责）。

#### `snapshot` — 每个客户端连接后的第一帧
```json
{"t":"snapshot","ts":<ms>,"upstream":"ok|degraded|down",
 "route":{"mode":"proxy|direct","proxy":"..."},
 "insts":{
   "BTC-USDT":{"t":"snapshot_inst","inst":"BTC-USDT","last":..,"chg24h_pct":..,"vol24h":..,"volCcy24h":..,
               "bid":..,"ask":..,"bidSz":..,"askSz":..,"open24h":..,"high24h":..,"low24h":..,"ts":<ms>,"src":"ws|rest",
               "book":{"t":"book",...},"bbo":{...},
               "ticks":[<1秒聚合K线，最多600根>],"candles":[<1m K线，最多300根>],"trades":[<最近50笔>]},
   "BTC-USDT-SWAP":{"oi":{...},"funding":{...}}},
 "stats":{<同 stats 消息>}}
```

#### `ticker` — 每次行情变动（真·跳动来源，约 6 条/秒/标的）
| 字段 | 含义 |
|---|---|
| `inst` | `BTC-USDT` |
| `last` | 最新价 |
| `chg24h_pct` | 24h 涨跌幅 %，**后端用 `(last-open24h)/open24h*100` 计算**（OKX tickers 不直接给该字段） |
| `vol24h` / `volCcy24h` | 24h 成交量（币）/（计价币） |
| `bid` / `ask` / `bidSz` / `askSz` | 买一/卖一价与量 |
| `open24h` / `high24h` / `low24h` | 24h 开/高/低 |
| `ts` | OKX 毫秒 UTC |
| `src` | `ws`（实时推送）/ `rest`（仅启动那一次快照） |

#### `trades` — 逐笔成交
`{"t":"trades","inst":"BTC-USDT","rows":[{"px":..,"sz":..,"side":"buy|sell","ts":<ms>,"tradeId":"..."}],"ts":<ms>}`
一帧可含多笔。

#### `book` — 订单簿（推送前 20 档，内部维护 400 档）
| 字段 | 含义 |
|---|---|
| `bids` / `asks` | `[[价, 量], ...]`，买盘价降序、卖盘价升序，各 `--book-levels`（默认 20）档 |
| `depth_total` | `{"bids":N,"asks":M}` 内部维护的真实总档数（`books` 通道约 400） |
| `seqId` | OKX 序列号（增量重建用） |
| `status` | `snapshot`（刚重建）/ `update`（增量合并成功） |
| `ts` | OKX 毫秒 UTC |

#### `candle` — K 线（business 端点）
`{"t":"candle","inst":"BTC-USDT","bar":"1m","o":..,"h":..,"l":..,"c":..,"vol":..,"volCcy":..,"volCcyQuote":..,"confirm":0|1,"ts":<ms>}`
- OKX 的 K 线**在线路上是数组** `[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]`，后端解成对象。
- `confirm` **原样透传**：`0` = 未收盘（同一根会反复刷新，前端应更新最后一根），`1` = 已收盘（只有这时才落盘）。
- `ts` 是**该根 K 线的起始时间**。

#### `oi` / `funding` — 合约
`{"t":"oi","inst":"BTC-USDT-SWAP","oi":..,"oiCcy":..,"oiUsd":..,"ts":<ms>}`
`{"t":"funding","inst":"BTC-USDT-SWAP","rate":..,"nextRate":..,"nextTime":<ms>,"premium":..,"ts":<ms>}`

#### `stats` — **每秒一条**，用来证明"它还活着"
| 字段 | 含义 |
|---|---|
| `upstream` | `ok` / `degraded`（部分组掉线）/ `reconnecting` / `down` |
| `upstream_detail` | `{"public":"ok","business":"ok"}` 分组状态 |
| `route` | 当前上行路由与代理（密码已遮蔽） |
| `reconnects` | 各组累计重连次数，如 `{"public":1}` |
| `clients` / `clients_ws` / `clients_sse` | 下游连接数 |
| `msgs_per_s` | 上游最近 5 秒滑动平均消息速率 |
| `msgs_total` / `msgs_by_channel` | 累计计数 / 分通道计数 |
| `chan_bytes` | 各通道 `{"n":条数,"bytes_avg":平均字节}` |
| `sub` | `{confirmed,expected,errors,last_error}` 订阅确认情况 |
| `ring` | 每标的 `ticks/trades/candles` 环形缓冲条数 |
| `book_state` | 每标的 `bid_levels/ask_levels/snapshots/updates/gaps/seq` |
| `persist` | `{rows,bytes,pending,files,dir}` 落盘进度 |
| `uptime_s` / `ts` | 运行秒数 / 当前毫秒 |

#### `error` — 上游异常通知（前端可用来显示 NO DATA 横幅）
`{"t":"error","scope":"upstream:public","msg":"disconnected, reconnecting in 1s","ts":<ms>}`
上游恢复时同样推一条 `"msg":"connected"`。

---

## 5. 心跳 / 断线重连策略

- **心跳**：每 20s 向上游发纯文本 `ping`，服务端回 `pong`。OKX 会踢掉空转连接。
- **看门狗**：45s 内没收到任何帧 → 判定死链 → 主动关闭 → 重连。
- **指数退避**：1s → 2s → 4s → 8s → 16s → 30s（封顶 30s）；**连接存活超 60s 后退避重置回 1s**。
- **重连后重新订阅全部通道**（这是最容易漏的 bug，日志里每次重连都能看到 `SUBSCRIBE SENT 14 args` 与 `ALL SUBSCRIPTIONS CONFIRMED`）。
- 上游断开**不影响下游客户端连接**：客户端只是暂时收不到数据，重连恢复后继续推。
- 两条上游连接（public / business）独立重连，互不阻塞。

---

## 6. 落盘

目录：`D:\量化\okx-terminal\data\`，文件名 `前缀_标的_YYYYMMDD.jsonl`，**每行一条 JSON，按天滚动**（跨天自动写新文件）。
写入是**内存缓冲 + 每 5 秒 flush**（`asyncio.to_thread` 落盘），**消息回调里不做任何同步 IO**，不会阻塞事件循环。

| 文件 | 内容 | 粒度 |
|---|---|---|
| `trades_BTC-USDT_YYYYMMDD.jsonl` | 逐笔成交（仅现货，每标的 1 个文件） | 原始逐笔 |
| `ticks_<inst>_YYYYMMDD.jsonl` | ticker **按 1 秒聚合**的 OHLC | 每秒一行（原始 6.2 条/秒全写会爆盘） |
| `candle_<inst>_YYYYMMDD.jsonl` | 已收盘 K 线（仅现货） | 每分钟一行（只写 `confirm=1`） |

标的共 4 个（BTC/ETH 现货 + 对应永续），因此形如 `ticks_BTC-USDT-SWAP_YYYYMMDD.jsonl` 的永续文件**也有**
（SWAP 订了 `tickers`），但永续没有 `trades`/`candle` 文件。

ticker 聚合行字段：`{"inst","sec"(秒级时间戳),"o","h","l","c","vol24h","volCcy24h","n"(该秒内 tick 数),"ts","src"}`。
**`src` 语义**：`ws` = 实时推送观察到的（**只有这种才落盘**）；`rest` = 启动回填来的（**只进内存环，不写文件**），
所以 **jsonl 里只会出现 `src:"ws"` 的行** —— 落盘文件 = 真实时记录，不掺历史回填数据。

### 占用估算（基于实测）
65 秒实测（行数增量，不是估算）：

| 文件 | 实测行数/65s | 实测 KB/65s | 每行字节 | 估算/天 |
|---|---|---|---|---|
| `ticks_BTC-USDT` | **66**（=1/秒 ✅） | 11.3 | 175 | 16.3 MB |
| `ticks_ETH-USDT` | 66 | 11.1 | 173 | 15.7 MB |
| `ticks_BTC-USDT-SWAP` | 66 | 10.9 | 169 | 15.2 MB |
| `ticks_ETH-USDT-SWAP` | 66 | 10.9 | 169 | 15.2 MB |
| `trades_BTC-USDT` | 25（行情活跃度相关） | 2.5 | 103 | ~3.3 MB |
| `trades_ETH-USDT` | 19 | 1.9 | 101 | ~2.6 MB |
| `candle_*` ×2 | 1（=1/分钟 ✅） | 0.2 | ~200 | 各 0.3 MB |
| **合计** | — | — | — | **≈ 69 MB/天，≈ 2.1 GB/月** |

> 与前一版（38 MB/天）的差别来自**给 4 个标的都订了 `tickers`**（SWAP 也要价格），ticks 文件从 2 个变 4 个，
> 且 ticks 是体积主项（4×15 MB/天）。`trades` 随行情活跃度波动（20:00 前后实测 19~25 行/65s = 0.3~0.4 行/秒）。
> 要降体积可以：(a) 只对现货记 tick（需改代码）；(b) 定期清理旧日期文件；(c) tick 聚合粒度改成 5 秒/行（改 `_aggregate`）。

**retention：当前不自动删除**（保留全部历史）。要控制体积就定期清理 `data\` 下旧日期文件，或调大 `--flush-interval` 意义不大 —— 真正影响体积的是 ticker 聚合行数（固定 1 行/秒/标的）。

日志：`relay\logs\relay.log`，`TimedRotatingFileHandler`+`encoding="utf-8"`，**按天滚动、保留 7 天**。日志**不含任何凭据**（代理串里的密码会被遮蔽）。

---

## 7. 已知限制

1. **深档订单簿需要登录**：`books50-l2-tbt` / `books-l2-tbt` 返回 `60011 Please log in` → 不做。当前 `books`（400 档、增量）是**免登录能拿到的最深档位**。
2. `books` 通道 `checksum` 恒为 0，**无法做内容校验**，只能靠 `prevSeqId` 连续性检测丢包。
3. `liquidation-orders`（强平）参数不被接受（`60018`）、`algo-advance`（高级委托）需登录 → **均未做**。
4. 下游 `book` 默认只推前 20 档（内部维护 400 档）；要更深调 `--book-levels`（注意每帧体积）。
5. **依赖本机代理**：本机 aiohttp 直连 OKX 被重置（见第 2 节）。代理进程挂了 → 上游会持续退避重连，`stats.upstream` 变 `down`，此时**没有数据是新造的，页面应显示 NO DATA**。
6. 只有 BTC/ETH 两个标的（现货 + 对应永续）。加标的需改 `relay.py` 顶部的 `SPOT`/`SWAP` 常量或扩展为 CLI 参数。
7. **环形缓冲不跨进程**：内存环（ticks/candles/trades）进程重启即清空，历史只存在于 `data\*.jsonl`；
   重启后的冷启动量由启动回填（prime）补齐（K 线 300 根 / 逐笔 200 笔，见第 3 节）。
8. **进程不会自我重启**：本中继是单进程，被外部强杀（关控制台、`taskkill`、会话结束）后需要外部拉起
   （`start-relay.vbs` 或 WMI）。它自身不会"死而复生"，但**再启动时会自动回填 K 线/逐笔**，页面不会空。

---

## 7.1 崩溃加固（2026-10-11 20:02 事件复盘）

### 现场
```
2026-10-11 20:02:03 ERROR Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)
Traceback (most recent call last):
ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。
```
来源是 `asyncio/proactor_events.py:154-167` 的 `_call_connection_lost`：它的 `finally` 里直接调
`sock.shutdown(SHUT_RDWR)`，**没有 try/except**。任何客户端被强制断开（关标签页、Wi-Fi 切换、休眠唤醒、
RST）走到这里都可能抛出 `WinError 10054/10053/10038/64`。

### 一个必须说清的结论（不要误判）
查 `asyncio/events.py:82-98`，`Handle._run()` 是用 **`except BaseException` 捕获**回调异常的，然后交给
`loop.call_exception_handler`。也就是说这行 `ERROR Exception in callback ...` 是**默认异常处理器打的报告**，
**这个异常在设计上不会终止事件循环、也不会杀掉进程**。
佐证：本机 `logs/relay.log` 里 **`relay stopped` 出现 0 次** —— 而这句话在**任何优雅退出路径**（含 Ctrl+C）都会打印，
所以历史上每次"进程消失"都是**外部强杀**（控制台关闭 / `taskkill` / 会话结束），不是异常崩溃。
20:02 那一刻两个下游客户端同时断开，更像外部原因（终端被关/网络瞬断）顺带产生了这条 RST 报告。
**结论：加固照做（纵深防御），但不要把"中继死了"直接归因于这条异常。**

### 已做的三层加固（全部有实测）
| # | 加固 | 位置 |
|---|---|---|
| 1 | **事件循环异常处理器**：连接类瞬时错误降级为计数 + warning，其它异常仍走默认处理器（真 bug 不会被掩盖） | `install_loop_exception_handler()` **relay.py:1210**，`_handler` **1218**，安装点 **1381**（`amain` 内、任何客户端可连之前） |
| 2 | **所有写 socket 的地方加保护**：`ws.send_str` / SSE `resp.write` 失败只丢弃该客户端并计数，绝不冒泡 | `_ws_sender` / `_sse_send` / `ws_handler` / `sse_handler`（`_sse_send` 返回 False → 调用方断开） |
| 3 | **fatal 取证**：顶层 `try/except BaseException` → `logs/fatal_<时间戳>.log`（traceback + `stats` 快照 + rings + persist），**先 flush 再写**，退出码 **3** | `write_fatal()` **relay.py:1237**；主循环 **1441-1449**；`main()` 兜底 **1483-1488** |

配套：
- **ALIVE 心跳**（`liveness_loop`，默认每 60s 一行）：`uptime / msgs_per_s / msgs_total / clients / sub / reconnects / suppressed_errs / write_errs / persist_rows / pending / ring`。
  **这条日志的存在意义**：20:02 那次事后无法判断"没日志"到底是"进程死了"还是"只是没客户端连"，以后不会再出现这种取证盲区。
- 瞬时错误计数出现在 `/health` 与 `stats.errors`：`{"transient_suppressed":N,"client_write_errors":M}`。
- 若仍想从根上换掉 Proactor：`--selector-loop`（改用 `WindowsSelectorEventLoopPolicy`）。**默认仍用 Proactor**（本机实测 37~50 msg/s 很稳），handler 两种 loop 都装。
- 测试钩子 `--selftest-fatal-after N`：N 秒后在主循环抛一个故意的未预期异常，用来验证 fatal 通路（生产不要用）。

### 加固实测（2026-10-11 22:07-22:09，pid 12476）
| 测试 | 结果 | 证据 |
|---|---|---|
| 20 个硬 RST 客户端（`SO_LINGER 0` 强制 RST）打真实中继 | **进程存活、pid 不变、数据继续流** | `tools/conn_reset_test.py real --rounds 20`；日志逐条 `WARNING sse write to client #N failed (ClientConnectionResetError...) -- dropping client`；`/health` 前后 `msgs_total` 1168→ 继续增长 |
| handler 确定性单测（喂**真实崩溃上下文**） | **PASS** | `tools/handler_unit_test.py`：`ConnectionResetError(10054)`/`10053`/`EPIPE`/`OSError(64)` → suppressed 并计数；`ValueError('real bug')` → 仍上报；`CancelledError` → 不视为瞬时 |
| fatal 取证通路 | **PASS**：退出码 **3**，fatal 文件 17 行含 traceback+快照，`pending=0`（崩溃路径也 flush） | `logs/selftest/fatal_20261011_220804.log` |

> 诚实说明：我**没能复现**出 `_call_connection_lost` 那条异常本身（12 轮 RST 的最小复现里
> `suppressed=0`，装/不装 handler 进程都活着），说明它的触发条件比"客户端断开"更窄。
> 因此加固 1 的价值是**兜住 + 计数 + 去噪**，而不是"已经证实能救回一次崩溃"。

---

## 7.2 落盘对账（行数，不是字节数）

用 `tools/persist_audit.py` 按**连续运行段**（而非文件首尾跨度）核算，因为文件跨度里包含**进程没运行的时间**。
工具会区分：`inside_run_missing`（运行期间真丢）／`downtime gap >60s`（进程没跑，不算丢数据）。

```
### ticks_BTC-USDT_20261011.jsonl      rows=3206 slots=3205 runs=10
    inside_run_missing=0 (0.0%)   <-- REAL loss
    downtime_gaps=4 (18478 slots)   small_gaps=5 (70 slots)
      downtime: 16:12:19 -> 16:37:47 / 16:40:35 -> 16:54:58 /
                17:08:18 -> 19:35:59 (8862s) / 20:02:01 -> 22:02:23 (7223s)
    last row before crash: 2026-10-11 20:02:00   lag=3s
```
**结论**：
1. **所有 8 个文件 `inside_run_missing = 0`** —— 进程在运行的每一段时间里，**一秒/一分钟都没丢**。
2. **崩溃瞬间没有丢缓冲**：ticks 最后一行 `20:02:00`（lag **3s**）、trades lag **2~4s**、candle lag 63s（一分钟一根属正常）。
   即崩溃时最多只有 ~3 秒停在内存里，而 flush 周期是 5 秒 —— **在正常 flush 窗口内，没有额外损失**。
3. **一个必须纠正的前提**：`17:08:18 → 19:35:59` 有 **8862 秒（2.46 小时）空洞**，这段既没有 tick 行、
   日志里也没有 RECONNECT / client 事件 → **那段时间进程根本没在运行**。所以"17:00–20:01 稳定运行 3 小时"
   不成立；`data\` 的体积是**多个运行段累加**的结果，不能除以 3 小时当作速率。
4. 崩溃路径现在也会 flush（见 7.1 加固 3），所以"最后 5 秒丢失"这个风险已从根上消除。

---

## 8. 排障

```powershell
# 健康（含上游路由、订阅数、瞬时错误计数）
curl http://127.0.0.1:8910/health

# 进程活着吗？—— 看 ALIVE 心跳（每 60s 一行）
Select-String -Path D:\量化\okx-terminal\relay\logs\relay.log -Pattern "ALIVE" | Select-Object -Last 3

# 崩溃取证（有则说明进程异常退出过）
Get-ChildItem D:\量化\okx-terminal\relay\logs\fatal_*.log

# 落盘行数对账（区分"真丢"和"进程没跑"）
& "C:\Users\33096\.conda\envs\aiquant\python.exe" D:\量化\okx-terminal\relay\tools\persist_audit.py --crash-at "2026-10-11 20:02:03"

# 客户端硬断测试（RST）/ 事件循环异常处理器单测
& "C:\Users\33096\.conda\envs\aiquant\python.exe" D:\量化\okx-terminal\relay\tools\conn_reset_test.py real --rounds 20
& "C:\Users\33096\.conda\envs\aiquant\python.exe" D:\量化\okx-terminal\relay\tools\handler_unit_test.py

# 上游是否活着（强断以验证重连）
curl "http://127.0.0.1:8910/admin/kill-upstream?group=public"

# 下游通道自检（同时连 /ws 与 /sse，统计各类消息条数）
& "C:\Users\33096\.conda\envs\aiquant\python.exe" D:\量化\okx-terminal\relay\tools\client_check.py --seconds 15
```

启动时报端口占用 → `netstat -ano | findstr :8910` 找占用进程。

---

## 9. 验收记录（2026-10-11，本机实跑）

| 验收项 | 结果 | 证据 |
|---|---|---|
| 订阅确认 | **16/16** 全部 `event=subscribe`，`errors=0` | 日志 `SUBSCRIBED ... (n/16)` → `ALL SUBSCRIPTIONS CONFIRMED` |
| 上游吞吐 | `msgs_per_s` **31~50**（多时点） | `/health`、`stats`、`ALIVE` 行 |
| 下游吞吐 | `/ws` 与 `/sse` 各 **3286 条 / 130 秒 = 25.21 条/秒**，`dropped=0` | `logs/client_check_130s.json` |
| 上游断线重连 | **成功**：强断后 1s 重连（1631ms），订阅全部重新确认 | `RECONNECT attempt=1 in 1s (last session 90s)` → `UPSTREAM CONNECTED` → `ALL SUBSCRIPTIONS CONFIRMED` |
| **客户端硬断（RST）** | **进程存活**，只记 warning 并丢弃该客户端 | 20 轮 RST，pid 12476 不变；`write_errs=20` |
| **fatal 取证** | 退出码 **3** + `fatal_*.log`（traceback+stats+rings+persist，`pending=0`） | `logs/selftest/fatal_20261011_220804.log` |
| **落盘行数对账** | 运行期内 **0 行丢失**；崩溃瞬间 lag 2~3s（< 5s flush 周期） | `tools/persist_audit.py` 输出（见 7.2） |
| 启动回填 | candles **300/300**、trades **200/200**、4 标的 ticker 全 ok，**8/8 调用成功，3172 ms** | 日志 `REST BACKFILL done status=ok ok=8 fail=0` |
| ALIVE 心跳 | 每 60s 一行，含 ring/persist/pending/错误计数 | 日志 `ALIVE uptime=63s ...` |
| 订单簿丢包 | `book gaps = 0`，`books` 400 档状态正常 | `stats.book_state` |
| 前端入口 | `GET /` = **200**，返回 `index.html` | `curl http://127.0.0.1:8910/` |

复现命令：
```powershell
$env:PYTHONUTF8="1"
P="C:\Users\33096\.conda\envs\aiquant\python.exe"
R="D:\量化\okx-terminal\relay"
& $P "$R\relay.py"                                                   # 起服务（前台）
& $P "$R\tools\client_check.py" --seconds 130                        # 下游吞吐
& $P "$R\tools\probe_channels.py" --seconds 18                       # 上游通道实测
& $P "$R\tools\handler_unit_test.py"                                 # 事件循环异常处理器单测
& $P "$R\tools\conn_reset_test.py" real --rounds 20                  # 客户端硬断
& $P "$R\tools\persist_audit.py"                                     # 落盘行数对账
& $P "$R\relay.py" --port 8911 --selftest-fatal-after 18 `
      --log-dir "$R\logs\selftest" --data-dir "$R\data\selftest"     # fatal 通路自测（退出码 3）
Invoke-WebRequest "http://127.0.0.1:8910/admin/kill-upstream?group=public"   # 强断验证重连
```

> 说明：`logs/relay.log` 是**同一天追加写**的，文件里 15:58 那几行 `RECONNECT attempt=1..6` 属于**修复代理之前**的那次失败运行（当时路由错误导致无限退避），不是当前版本的行为。
