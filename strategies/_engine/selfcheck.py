# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\_engine\\selfcheck.py
引擎自检：buy & hold 基准 vs 直接用收盘价算的涨跌幅。
两者必须量级一致 —— 不一致说明记账写错了。

对照设计（逐项对齐，只留记账差异）：
  A. naive_costfree      : close_last/close_first - 1            <- 无成本、bar0 入场
  B. naive_withcost      : (A) 减去开+平两笔 fee+slip 的成本       <- 有成本、bar0 入场
  C. engine_buyhold      : 引擎跑 buy&hold（bar1 开盘成交、逐 bar 盯市、爆仓检测）
  A 与 C 的差异应能被「成本 + 1 根 bar 的入场滞后」解释。
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backtest import (load_parquet, buy_and_hold, naive_return, run_backtest,
                      is_oos_split, BAR_WIDTH_MS, load_funding)

DATA = os.path.join(os.environ.get("AIQUANT_HOME", os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))), "_data")
OUT = os.path.join(os.path.dirname(__file__), "selfcheck_result.json")
LOGDIR = os.path.join(os.environ.get("AIQUANT_HOME", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))), "logs")
FUNDING_PQ = os.path.join(DATA, "funding_BTCUSDTSWAP.parquet")


def fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ms) / 1000.0))


def check_one(inst, bar, lev=1.0, fee_bps=5.0, slip_bps=2.0):
    pq = os.path.join(DATA, "%s_%s.parquet" % (inst.replace("-", ""), bar))
    if not os.path.exists(pq):
        return {"inst": inst, "bar": bar, "error": "文件不存在 %s" % pq}
    df = load_parquet(pq)
    # 剔除坏 bar：low/open <= close/10 在数学上不可能（OKX 2017 首根 open=low=1.0）。
    # 这是数据缺陷，不是策略结论，必须排除后对照，否则会污染回撤口径。
    bad_mask = (df["low"] < df["close"] / 10) | (df["open"] < df["close"] / 10)
    n_bad = int(bad_mask.sum())
    if n_bad:
        df = df[~bad_mask].reset_index(drop=True)
    nv = naive_return(df)
    c0, c1 = nv["close_first"], nv["close_last"]
    cost = 2.0 * (fee_bps + slip_bps) / 10_000.0
    naive_costfree = (c1 / c0 - 1.0) * 100.0
    # 有成本口径：买价 c0*(1+slip)，卖价 c1*(1-slip)，两端各扣 fee
    buy = c0 * (1 + slip_bps / 10_000.0)
    sell = c1 * (1 - slip_bps / 10_000.0)
    naive_cost = (sell / buy - 1.0) * 100.0 - 2.0 * fee_bps / 100.0 * 1.0
    # 更精确：手续费按名义额扣两次
    naive_withcost = ((sell * (1 - fee_bps / 10_000.0)) /
                      (buy * (1 + fee_bps / 10_000.0)) - 1.0) * 100.0

    eng = buy_and_hold(df, init_cash=10_000.0, fee_bps=fee_bps,
                       slippage_bps=slip_bps, bar=bar, leverage=lev)
    m = eng["metrics"]
    if os.environ.get("SC_DEBUG"):
        print("    [SC_DEBUG] %s %s lev=%s" % (inst, bar, lev))
        print("      df.index[0..2]=%s  df['open'].iloc[1]=%.4f  df['close'].iloc[1]=%.4f"
              % (list(df.index[:3]), df["open"].iloc[1], df["close"].iloc[1]))
        print("      engine eq[0..3]=%s" % [round(float(x), 4) for x in eng["equity_curve"].iloc[:4]])
        print("      engine position[0..3]=%s" % [round(float(x), 4) for x in eng["position"].iloc[:4]])

    # --- 强对照（回归测试）：引擎的逐 bar 盯市权益，必须等于解析值 ---
    # 引擎采用现金流记账，恒等式：
    #     equity(t) = init - 手续费 - 资金费 + qty*(price(t) - avg_cost)
    # 建仓后：qty = notional/entry_px, 手续费 = notional*fee，故
    #     equity(t) = (init - notional*fee) + qty*(close(t) - entry_px)
    # 注意：保证金**不**在恒等式里单独出现——它就是 qty*avg_cost 所代表的
    # 那笔被锁住的现金。上一版曾写成 cash+margin+qty*px，把同一笔钱算了两遍，
    # 导致权益被高估一个名义额（本项目真实踩过的坑，特此写明）。
    entry_px = df["open"].iloc[1] * (1 + slip_bps / 10_000.0)
    notional = 10_000.0 * lev
    q = notional / entry_px
    fee_amt = notional * fee_bps / 10_000.0
    analytic = (10_000.0 - fee_amt) + q * (df["close"] - entry_px)
    analytic.iloc[0] = 10_000.0
    if os.environ.get("SC_DEBUG"):
        print("      entry_px=%.8f q=%.10f fee=%.6f" % (entry_px, q, fee_amt))
        print("      engine   eq[0..3]=%s" % [round(float(x), 4) for x in eng["equity_curve"].iloc[:4]])
        print("      analytic eq[0..3]=%s" % [round(float(x), 4) for x in analytic.iloc[:4]])
    _comp = {"entry_px": round(entry_px, 8), "notional": notional, "q": round(q, 10),
             "fee_amt": round(fee_amt, 6),
             "close_iloc1": float(df["close"].iloc[1]),
             "analytic_iloc1": round(float(analytic.iloc[1]), 4),
             "engine_iloc1": round(float(eng["equity_curve"].iloc[1]), 4),
             "diff_iloc1": round(float(eng["equity_curve"].iloc[1] - analytic.iloc[1]), 6)}
    # 引擎在最后一根按收盘价平仓（eod_close），会再扣一次费率+滑点
    eng_eq = eng["equity_curve"].copy()
    analytic.iloc[-1] = eng_eq.iloc[-1]
    reg_max_abs = float((eng_eq - analytic).abs().max())
    reg_max_rel = reg_max_abs / 10_000.0
    _j = int((eng_eq - analytic).abs().idxmax())

    diff = m["total_return_pct"] - naive_withcost
    return {
        "inst": inst, "bar": bar, "bars": int(len(df)), "dropped_bad_bars": n_bad,
        "range": "%s ~ %s" % (fmt(df["ts"].iloc[0]), fmt(df["ts"].iloc[-1])),
        "leverage": lev,
        "naive_costfree_pct": round(naive_costfree, 4),
        "naive_withcost_pct": round(naive_withcost, 4),
        "engine_total_return_pct": m["total_return_pct"],
        "engine_max_dd_pct": m["max_drawdown_pct"],
        "engine_final_equity": m["final_equity"],
        "engine_trades": m["trades"],
        "engine_fee_pct_of_init": m["fee_pct_of_init"],
        "engine_liquidated": m["liquidated"],
        "abs_diff_pct_points": round(abs(diff), 4),
        "rel_diff_vs_naive_pct": round(100.0 * abs(diff) / max(abs(naive_withcost), 1e-9), 3),
        "regression_max_abs_diff": round(reg_max_abs, 6),
        "regression_max_rel_diff": round(reg_max_rel, 8),
        "regression_worst_idx": _j,
        "regression_worst_engine": round(float(eng_eq.iloc[_j]), 4),
        "regression_worst_analytic": round(float(analytic.iloc[_j]), 4),
        "regression_worst_bar": {
            "ts": int(df["ts"].iloc[_j]),
            "open": float(df["open"].iloc[_j]), "high": float(df["high"].iloc[_j]),
            "low": float(df["low"].iloc[_j]), "close": float(df["close"].iloc[_j])},
        "regression_first_divergence_idx": (
            int((eng_eq - analytic).abs().gt(1e-6).idxmax())
            if bool(((eng_eq - analytic).abs() > 1e-6).any()) else None),
        "analytic_bar1": round(float(analytic.iloc[1]), 4),
        "engine_bar1": round(float(eng_eq.iloc[1]), 4),
        "regression_components": _comp,
        # 独立核算：直接用 close 算的最大回撤，与引擎权益曲线对照
        "naive_max_dd_pct": round(
            ((pd.Series(df["close"]) / pd.Series(df["close"]).cummax() - 1) * 100).min(), 4),
        "engine_max_dd_note": "引擎含成本与 t+1 开盘成交，回撤应略深于裸 close 口径",
    }


def main():
    rows = []
    print("=" * 108)
    print("引擎自检：buy & hold vs 收盘价口径（无成本 / 含成本）")
    print("=" * 108)
    for inst, bar in [("BTC-USDT", "1D"), ("BTC-USDT", "4H"), ("BTC-USDT", "1H")]:
        r = check_one(inst, bar)
        rows.append(r)
        if "error" in r:
            print("%s %s ERROR %s" % (inst, bar, r["error"]))
            continue
        print("\n[%s %s] %d 根 | %s" % (inst, bar, r["bars"], r["range"]))
        print("  A naive 无成本                : %+12.4f %%" % r["naive_costfree_pct"])
        print("  B naive 含成本(fee5+slip2 bp) : %+12.4f %%" % r["naive_withcost_pct"])
        print("  C 引擎 buy&hold (t+1 开盘)    : %+12.4f %%   <-- 引擎输出"
              % r["engine_total_return_pct"])
        print("  |C-B| = %.4f 个百分点 (相对 %.3f%%)" %
              (r["abs_diff_pct_points"], r["rel_diff_vs_naive_pct"]))
        print("  引擎最大回撤 %.4f %% | 裸 close 最大回撤 %.4f %%"
              % (r["engine_max_dd_pct"], r["naive_max_dd_pct"]))
        print("  交易数 %d | 手续费占初始 %.4f %% | 爆仓 %s"
              % (r["engine_trades"], r["engine_fee_pct_of_init"], r["engine_liquidated"]))
        print("  [回归测试] 引擎权益 vs 解析式 最大绝对差 = %.6f (本金 10000) 于 idx=%d"
              % (r["regression_max_abs_diff"], r["regression_worst_idx"]))
        print("             该处 bar1: analytic=%.4f engine=%.4f | 该 idx: analytic=%.4f engine=%.4f"
              % (r["analytic_bar1"], r["engine_bar1"],
                 r["regression_worst_analytic"], r["regression_worst_engine"]))
        print("             首次分歧 idx=%s  该 idx K线=%s" %
              (r["regression_first_divergence_idx"], r["regression_worst_bar"]))
        verdict = "PASS" if (r["rel_diff_vs_naive_pct"] < 5.0
                             and r["regression_max_rel_diff"] < 1e-6) else "FAIL"
        print("  判定: %s (阈值: 相对差<5%% 且 权益序列解析差<1e-6)" % verdict)
        r["verdict"] = verdict

    # 杠杆版自检：验证线性放大 + 爆仓归零逻辑
    print("\n" + "=" * 108)
    print("杠杆/爆仓自检（1D，验证 equity 只线性放大盈亏、且爆仓正确归零）")
    print("=" * 108)
    pq = os.path.join(DATA, "BTCUSDT_1D.parquet")
    df = load_parquet(pq)
    fund = load_funding(FUNDING_PQ)
    print("资金费率历史覆盖: %s ~ %s (%d 个结算点)"
          % (fmt(fund["ts"].min()), fmt(fund["ts"].max()), len(fund)))
    lev_rows = []
    for lev in [1, 2, 5, 10, 20, 50]:
        r = run_backtest(df, pd.Series(np.ones(len(df))), bar="1D", init_cash=10_000.0,
                         leverage=lev, allow_short=False, funding=fund)
        m = r["metrics"]
        eq = r["equity_curve"]
        lev_rows.append({"leverage": lev, "total_return_pct": m["total_return_pct"],
                         "final_equity": m["final_equity"],
                         "max_dd_pct": m["max_drawdown_pct"],
                         "min_equity": round(float(eq.min()), 2),
                         "liquidated": r["liquidated"],
                         "liq_idx": r["liquidated_idx"],
                         "funding_total": m.get("funding_total")})
        liq_at = (" 爆仓于 %s" % fmt(df["ts"].iloc[r["liquidated_idx"]])) if r["liquidated"] else ""
        print("  lev=%-3d 总收益 %+12.2f %%  终值 %12.2f  最大回撤 %9.2f %%  最低权益 %10.2f%s"
              % (lev, m["total_return_pct"], m["final_equity"], m["max_drawdown_pct"],
                 float(eq.min()), liq_at))
    print("\n  说明：若终值随杠杆暴涨到 1e6 以上，说明记账写错（复利基数被杠杆放大）。")
    print("  正确行为：收益近似线性放大，且杠杆>阈值时爆仓归零（终值=0）。")

    out = {"buy_hold_checks": rows, "leverage_checks": lev_rows}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\nsaved -> %s" % OUT)
    os.makedirs(LOGDIR, exist_ok=True)


if __name__ == "__main__":
    main()
