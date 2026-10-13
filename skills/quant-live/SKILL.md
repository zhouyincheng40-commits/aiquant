---
name: quant-live
description: 实盘与模拟盘执行清单。区分 aiquant（主环境）与 aiquant-live（实盘环境，Python 3.13 + vnpy_ctp）两个 conda 环境，规定 CTP/SimNow 参数只从环境变量读取、上线前 checklist、盘中监控项、断线与持仓资金不符的异常处置、以及日志落盘 D:\量化\logs\。
metadata:
  version: "1.0.0"
  date: "2026-10-09"
---

# 实盘与模拟盘

实盘是不可逆操作。本技能的每一步都必须有可核对的凭据；任何一步不通过就**停下并报告**，不许"先跑起来再说"。

## 1. 两个 conda 环境职责划分

| 环境 | 路径 | Python | 装什么 | 用途 |
|---|---|---|---|---|
| 主环境 | `C:\Users\33096\.conda\envs\aiquant\python.exe` | 3.11 | akshare/baostock/tushare/ccxt/pandas/duckdb/vnpy 4.4.0 | 取数、因子、回测、研究 |
| 实盘环境 | `C:\Users\33096\.conda\envs\aiquant-live\python.exe` | 3.13 | vnpy 4.4.0 + vnpy_ctp | 只做行情接入与下单 |

铁规则：**研究在 aiquant，实盘在 aiquant-live，两边不混用**。不要往实盘环境装研究依赖，也不要在主环境里连 CTP。

自检命令：

```powershell
& "C:\Users\33096\.conda\envs\aiquant-live\python.exe" -c "import vnpy, vnpy_ctp, sys; print(vnpy.__version__, sys.version.split()[0])"
```

预期输出 `4.4.0 3.13.x`。

## 2. 凭据处理（硬约束）

1. CTP 开户信息与 SimNow 模拟盘参数**只从环境变量读取**，变量名固定：

   `CTP_BROKER_ID`、`CTP_USER_ID`、`CTP_PASSWORD`、`CTP_MD_ADDRESS`、`CTP_TD_ADDRESS`、`CTP_APP_ID`、`CTP_AUTH_CODE`。

2. 变量缺失时**立即停止**并报告缺哪几个变量，不要交互式索要密码，不要写临时文件。

```powershell
foreach ($v in 'CTP_BROKER_ID','CTP_USER_ID','CTP_PASSWORD','CTP_MD_ADDRESS','CTP_TD_ADDRESS','CTP_APP_ID','CTP_AUTH_CODE') {
  if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($v))) { "MISSING: $v" } else { "OK: $v" }
}
```

3. 禁止：把账号/密码写进 `strategy.py`、`params.yaml`、README、报告、缓存或记忆；禁止在命令回显里打印密码值（只打印是否已设置）。
4. 上线前用 `grep -ri "password\|CTP_USER_ID\|auth_code" D:\量化\strategies` 复查，有命中即删除。

## 3. 上线前 checklist（逐项通过才可连接实盘）

1. 策略已通过 `quant-backtest` 三段验证（样本内/样本外/walk-forward）。
2. 风控已出 `通过` 判定（`quant-risk` 十条红线全过），且已用 `aiquant_risk` 落库。
3. `aiquant_env` 与 `mcp__aiquant__q_env` 均正常。
4. 环境变量齐全（§2 脚本无 MISSING）。
5. 交易时段与交易日核对通过（非交易时段只允许连行情，不允许下单）。
6. 止损、仓位上限、熔断线已写进 `params.yaml` 且代码实际读取（不是注释）。
7. 日志目录 `D:\量化\logs\` 可写。
8. 先跑 SimNow 模拟盘连续 ≥ 5 个交易日，且当日成交与持仓能对上账。
9. 初始资金与资金规模经用户书面确认；未确认不下单。
10. 只允许"手动确认后下单"模式；自动下单需用户逐次明确授权。

## 4. 盘中监控项（每 1 分钟核对，异常即记录）

1. 行情连接状态与最后一条 tick 时间戳（超过 10 秒无更新即告警）。
2. 交易连接状态与最后一条回报时间。
3. 持仓明细 vs 策略目标仓位，逐标的比对偏差。
4. 可用资金与持仓市值合计 vs 券商端权益。
5. 未成交委托列表与撤单情况。
6. 当日盈亏与回撤是否触及熔断线（`quant-risk` §2）。
7. 自选池行情刷新：用 `aiquant_quote` 更新看板；信号变化用 `aiquant_signal` 落库；仓位变化用 `aiquant_position` 落库。

## 5. 异常处置

| 异常 | 处置 |
|---|---|
| 行情断线 | 立即停止产生新信号，保留持仓；重连成功后先核对持仓再恢复；连续 3 次重连失败 → 手工平掉高风险仓位 |
| 交易断线 | 记录最后已知委托状态；重连后查全部委托与成交回补，未回补前不下新单 |
| 持仓不符 | 以券商端为准；差异 >1 手立即停止交易，逐笔核对成交明细，写 `aiquant_note` 留痕 |
| 资金不符 | 立即停止开仓；差异来源查清前只允许减仓 |
| 涨跌停无法成交 | 记录为"委托未成交"，不重复追单超过 3 次 |
| 策略异常退出 | 不自动重启下单；记录日志并等待人工确认 |

所有异常必须写 `D:\量化\logs\`，格式 `YYYYMMDD-live.log`，每条含：时间戳（ISO 8601 带时区）、标的、事件类型、原始回报、处置动作。日志只记事件与状态，不记凭据。

## 6. 验收

`aiquant_position` 读到的持仓必须与券商端一致；`aiquant_board` 能看到本轮信号与持仓；日志文件存在且有当日记录；环境变量脚本无 MISSING。任一不满足 → 判定未上线。
