---
name: quant-router
description: AI量化模式总路由。收到任何一句量化需求（看走势、挖因子、写策略、跑回测、判风控、上实盘、出报告）时，用本技能判定该走哪条链路、加载哪个子技能、调用哪些 q_* 与 aiquant_* 工具。含六个链路的触发条件、前置预检、落库铁律与禁止事项。
metadata:
  version: "1.0.0"
  date: "2026-10-09"
---

# 量化总路由

本技能是入口。判定顺序固定为：**先预检 → 再定链路 → 加载一个子技能 → 执行 → 落库**。跳过任何一步都算返工。

## 1. 一句话路由表

| 用户说的话（典型） | 链路 | 立即加载 | 主工具 |
|---|---|---|---|
| "看看茅台/600519/BTC 现在走势" | 数据 → 信号 | `quant-data` | `q_quote` `q_klines` `q_indicators` |
| "今天大盘怎么样/有什么异动" | 数据 → 概览 | `quant-data` | `q_market_overview` `q_screen` |
| "挖一个动量因子/因子还有效吗" | 因子研究 | `quant-factor` | `q_factor_ic` |
| "写个双均线/多因子/事件驱动策略" | 策略研发 | `quant-strategy` | `q_klines` `q_indicators` |
| "这套策略能不能赚钱/跑个回测" | 回测验证 | `quant-backtest` | `q_backtest_ma` `q_optimize_grid` |
| "仓位多重/回撤怎么办/能不能上线" | 风控判定 | `quant-risk` | `aiquant_risk` |
| "接 SimNow/上实盘/盯着盘面" | 实盘链路 | `quant-live` | `aiquant_position` `aiquant_signal` |
| "出一份报告/总结一下这轮研究" | 报告 | `quant-report` | `aiquant_report` `aiquant_note` |

判定规则：
1. **只给标的** → 走「数据 → 信号」，不扩展成研究。
2. **给研究方法**（"挖…""有没有效"）→ 因子链路，按 数据→因子→策略→回测→风控 顺序推进，每步核对落库结果再进下一步。
3. **问"能不能上实盘"** → 只做风控 + 实盘 checklist，结论必须"通过/不通过 + 缺什么"。
4. **一句话两个意图** → 先走数据链路出事实，再走风控链路出判定，不合并成一句结论。
5. 出现"年化多少""能赚多少" → 强制转成"在 X 假设下历史回测是 Y、样本外是 Z"。

## 2. 六个链路的触发与工具

**链路 A 数据**：触发词=代码/名称/走势/"什么价"。工具：`q_env` → `q_quote`（快照）→ `q_klines`（日/分钟序列）→ `q_indicators`（均线/MACD/RSI）。港美股与加密走 `q_crypto_klines` 或 `q_klines` 的市场参数。原始数据落 `D:\量化\data\cache`，用 `q_export_csv` 导出留痕，校验见 `quant-data`。

**链路 B 因子**：触发词=因子/alpha/IC/选股。工具：`q_factor_ic` 出 IC/IR/分层，`q_screen` 出选股名单。结论必须带 IC 区间与股票池，判定见 `quant-factor`。

**链路 C 策略**：触发词=策略/规则/信号/参数。工具：`q_indicators` 算信号、`q_optimize_grid` 扫参数。产物落 `D:\量化\strategies\<name>\`（strategy.py + params.yaml + README.md），模板见 `quant-strategy`。

**链路 D 回测**：触发词=回测/收益/回撤/夏普。工具：`q_backtest_ma` 跑基线，`q_optimize_grid` 扫参数，结果必须用 `aiquant_backtest` 落库。成本三项（手续费/滑点/次日成交）缺任一项结论无效，口径见 `quant-backtest`。

**链路 E 风控**：触发词=仓位/上限/熔断/VaR/集中度。工具：`aiquant_risk` 落判定，`aiquant_position` 读持仓。红线清单见 `quant-risk`。

**链路 F 实盘/报告**：实盘触发词=SimNow/CTP/下单/盯盘，只加载 `quant-live`，凭据只从环境变量读；报告触发词=报告/总结/纪要，加载 `quant-report`，落 `D:\量化\reports\<YYYYMMDD>-<主题>.md`。

## 3. 两个工具族的硬分工

- `mcp__aiquant__q_*` 是**算**：行情、K 线、指标、回测、寻优、因子 IC、选股、导出。无状态、可重跑。
- `aiquant_*` 是**记**：`aiquant_watchlist`（自选）、`aiquant_quote`（行情入看板）、`aiquant_board`（读看板）、`aiquant_signal`（信号）、`aiquant_position`（持仓）、`aiquant_backtest`（回测）、`aiquant_risk`（风控）、`aiquant_note`（笔记）、`aiquant_report`（报告）、`aiquant_env`（环境）。写入 SQLite 事实库，右侧常驻"AI量化看板"读的就是它。
- **没落库的成果 = 没发生**。结论、信号、回测、风控判定，算完必须调对应 `aiquant_*` 写入。

## 4. 开工预检（第一个动作）

```
aiquant_env          # 环境与数据源可达性
mcp__aiquant__q_env  # MCP 侧数据通道与交易日历状态
```

任一项失败 → 立即停下，一次性列清"缺什么、为什么、要装到哪"，不要用记忆或推测填数据。

## 5. 禁止事项（违反即返工）

1. 不编价格、收益、指标：数字必须来自工具真实返回；工具失败就说失败。
2. 不承诺收益，不做无依据的目标价与胜率外推。
3. 回测不含成本、不给样本外结论的，标为无效结论。
4. 券商/CTP 账号、API key、token 禁止写入策略文件、报告、缓存或记忆，只从环境变量读。
5. 同一研究任务同时最多 3 个执行智能体，默认顺序推进；派活描述必须自包含（标的、区间、已测过什么、期望产出、落哪个库）。
