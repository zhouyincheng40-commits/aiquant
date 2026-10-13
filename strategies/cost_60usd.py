# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\cost_60usd.py
60 美元本金的真实可交易性核算。

回答三个问题：
 1) 60 美元能开多大仓？（对照各标的最小下单额）
 2) 每笔交易的成本占本金多少？多久被成本吃光？
 3) 用真实最小下单额跑一次回测，看"小资金版本"的真实结果。

最小下单额来源：用户提供并已实测（本机 okx-terminal 实测值），本脚本沿用并在
报告中标注为"实测值，非官方文档引用"。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "_engine"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backtest import load_parquet, run_backtest, BAR_WIDTH_MS   # noqa: E402
import s1_macd, s2_rsi_vwap                                     # noqa: E402

DATA = os.path.join(HERE, "_data")

# 本机实测的最小下单额（USD 名义额）
MIN_NOTIONAL = {
    "BTC 现货": 0.83,
    "ETH 现货": 0.25,
    "BTC 永续": 8.29,      # = 本金的 13.8%
    "ETH 永续": 2.50,
}
INIT = 60.0

FEE_BPS = 5.0      # taker 0.05%
SLIP_BPS = 2.0     # 保守


def fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ms) / 1000.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default="1D")
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--strategy", default="s1")
    a = ap.parse_args()

    print("=" * 100)
    print("60 美元本金 · 真实可交易性核算")
    print("=" * 100)
    print("本金 = $%.2f | 手续费 %.2f bp/笔 | 滑点 %.2f bp/笔 | 往返一次成本 = %.3f%% 名义额"
          % (INIT, FEE_BPS, SLIP_BPS, 2 * (FEE_BPS + SLIP_BPS) / 100.0))
    print()

    # ---- 1) 最小下单额 vs 本金 ----
    print("-" * 100)
    print("1) 最小下单额约束（名义额）")
    print("-" * 100)
    print("%-12s %-12s %-14s %-16s %s" % ("标的", "最小名义额", "占本金", "往返成本($)", "往返成本占本金"))
    rows = []
    for name, mn in MIN_NOTIONAL.items():
        rt_cost = mn * 2 * (FEE_BPS + SLIP_BPS) / 10_000.0
        rows.append({"inst": name, "min_notional_usd": mn,
                     "pct_of_capital": round(100.0 * mn / INIT, 2),
                     "roundtrip_cost_usd": round(rt_cost, 6),
                     "roundtrip_cost_pct_of_capital": round(100.0 * rt_cost / INIT, 4)})
        print("%-12s $%-11.2f %-13.2f%% $%-15.6f %.4f%%"
              % (name, mn, 100.0 * mn / INIT, rt_cost, 100.0 * rt_cost / INIT))
    print()
    max_lev_btc_swap = INIT / MIN_NOTIONAL["BTC 永续"]
    print("→ BTC 永续最小 8.29 美元 = 本金的 13.8%%。60 美元最多只能开 **%.2f 倍杠杆**"
          % max_lev_btc_swap)
    print("  （即 60/8.29 = %.2f）。杠杆再高也开不出仓，因为最小下单额是硬下限。" % max_lev_btc_swap)
    print()

    # ---- 2) 成本吃掉本金的速率 ----
    print("-" * 100)
    print("2) 成本消耗速率：按各策略实测的年均交易笔数，算手续费+滑点一年吃掉本金多少")
    print("-" * 100)
    res_path = os.path.join(HERE, "results.json")
    if not os.path.exists(res_path):
        print("  results.json 不存在，先跑 run_all.py")
        return
    results = json.load(open(res_path, encoding="utf-8"))
    print("%-6s %-5s %-8s %-12s %-14s %-16s %s"
          % ("策略", "bar", "OOS笔数", "OOS年数", "年均笔数", "名义额=$60时年成本", "占本金"))
    cost_rows = []
    for r in results:
        if not r.get("ok"):
            continue
        oos = r.get("oos_metrics") or {}
        n_tr = oos.get("trades") or 0
        yrs = oos.get("years") or 0
        if not yrs:
            continue
        per_year = n_tr / yrs
        for lev, label in [(1.0, "1x"), (max_lev_btc_swap, "%.1fx" % max_lev_btc_swap)]:
            notional = INIT * lev
            yearly_cost = notional * per_year * 2 * (FEE_BPS + SLIP_BPS) / 10_000.0
            cost_rows.append({"strategy": r["strategy"], "bar": r["bar"], "leverage": lev,
                              "trades": n_tr, "years": yrs, "trades_per_year": round(per_year, 2),
                              "notional_usd": round(notional, 2),
                              "yearly_cost_usd": round(yearly_cost, 4),
                              "yearly_cost_pct_of_capital": round(100.0 * yearly_cost / INIT, 3)})
            print("%-6s %-5s %-8d %-12.1f %-14.1f $%-15.4f %.3f%% (%s)"
                  % (r["strategy"], r["bar"], n_tr, yrs, per_year, yearly_cost,
                     100.0 * yearly_cost / INIT, label))
    print()
    print("→ 读法：成本占本金 X%% 意味着策略必须先赚到 X%% 才保本。对 60 美元的小资金，")
    print("  手续费+滑点是**按名义额**收的，杠杆越高、换手越快，成本越吃本金。")
    print()

    # ---- 3) 用 60 美元真实跑一次（1x 与最大可行杠杆）----
    print("-" * 100)
    print("3) 用本金 $60 真实跑回测（同一策略、同一参数，只换本金与杠杆）")
    print("-" * 100)
    df = load_parquet(os.path.join(DATA, "BTCUSDT_%s.parquet" % a.bar))
    bad = (df["low"] < df["close"] / 10) | (df["open"] < df["close"] / 10)
    df = df[~bad].reset_index(drop=True)
    fn = s1_macd.signal_macd if a.strategy == "s1" else s2_rsi_vwap.signal_rsi_vwap
    grid = s1_macd.PARAM_GRID_DEFAULT if a.strategy == "s1" else s2_rsi_vwap.PARAM_GRID_DEFAULT
    # 用样本内最优参数（从 results.json 读）
    best = None
    for r in results:
        if r.get("strategy") == a.strategy and r.get("bar") == a.bar and r.get("ok"):
            best = r.get("best_params")
    if best is None:
        print("  未找到 %s/%s 的最优参数" % (a.strategy, a.bar))
        return
    print("  策略=%s bar=%s 参数=%s" % (a.strategy, a.bar, best))
    print()
    print("%-10s %-12s %-14s %-12s %-14s %s" % ("本金", "杠杆", "总收益%", "终值$", "最大回撤%", "手续费$"))
    for init, lev, label in [(60.0, 1.0, "60/1x"),
                             (60.0, round(max_lev_btc_swap, 2), "60/最大可行"),
                             (10_000.0, 1.0, "10000/1x(对照)")]:
        sig = fn(df, best)
        r = run_backtest(df, sig, bar=a.bar, init_cash=init, leverage=lev,
                         fee_bps=FEE_BPS, slippage_bps=SLIP_BPS)
        m = r["metrics"]
        print("%-10.2f %-12.2f %-14.4f %-12.4f %-14.4f %.4f"
              % (init, lev, m["total_return_pct"], m["final_equity"],
                 m["max_drawdown_pct"], m["fee_total"]))
    print()
    print("→ 关键：同一策略同一参数，**本金越小，手续费占初始资金的比例越高**，")
    print("  因为手续费按名义额收、与本金无关。$60 的手续费占比是 $10000 的 %.1f 倍。"
          % (10_000.0 / 60.0))

    out = {"min_notional": rows, "cost_rates": cost_rows}
    with open(os.path.join(HERE, "cost_60usd.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\nsaved -> %s" % os.path.join(HERE, "cost_60usd.json"))


if __name__ == "__main__":
    main()
