# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\run_all.py
统一跑三个策略：样本内/样本外 7:3 网格 + walk-forward + 成本敏感性 + 交易抽样。

用法:
  python run_all.py --strategy s1 --bar 4H
  python run_all.py --all
  python run_all.py --all --slippage-bps 10
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

from backtest import (load_parquet, load_funding, run_backtest, grid_is_oos,
                      walk_forward, BAR_WIDTH_MS)   # noqa: E402
import s1_macd, s2_rsi_vwap, s3_cvd                  # noqa: E402

DATA = os.path.join(HERE, "_data")
OUTJ = os.path.join(HERE, "results.json")
LOGDIR = os.path.join(os.environ.get("AIQUANT_HOME", os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))), "logs")

FUNDING_PQ = os.path.join(DATA, "funding_BTCUSDTSWAP.parquet")

STRATS = {
    "s1": ("MACD 动能(s1_macd)", s1_macd.signal_macd, s1_macd.PARAM_GRID_DEFAULT),
    "s2": ("RSI+VWAP 反转(s2_rsi_vwap)", s2_rsi_vwap.signal_rsi_vwap,
           s2_rsi_vwap.PARAM_GRID_DEFAULT),
    "s3": ("CVD 背离(s3_cvd)", s3_cvd.signal_cvd, s3_cvd.PARAM_GRID_DEFAULT),
}


def fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ms) / 1000.0))


def load_data(bar: str):
    pq = os.path.join(DATA, "BTCUSDT_%s.parquet" % bar)
    df = load_parquet(pq)
    bad = (df["low"] < df["close"] / 10) | (df["open"] < df["close"] / 10)
    dropped = int(bad.sum())
    if dropped:
        df = df[~bad].reset_index(drop=True)
    # 质量校验复核
    w = BAR_WIDTH_MS[bar]
    gaps = int((df["ts"].diff().dropna() != w).sum())
    return df, {"bars": int(len(df)), "dropped_bad_bars": dropped,
                "irregular_gaps": gaps,
                "start": fmt(df["ts"].iloc[0]), "end": fmt(df["ts"].iloc[-1])}


def run_one(strat_key: str, bar: str, *, fee_bps=5.0, slip_bps=2.0,
            init_cash=10_000.0, leverage=1.0, n_folds=5) -> dict:
    label, fn, grid = STRATS[strat_key]
    df, qa = load_data(bar)
    print("\n" + "=" * 96)
    print("[%s] %s  bar=%s  bars=%d  %s ~ %s  缺口=%d"
          % (strat_key, label, bar, qa["bars"], qa["start"], qa["end"], qa["irregular_gaps"]))
    print("=" * 96)

    # s3 需要 taker-volume 合并（样本量会大幅缩小）
    if strat_key == "s3":
        tpq = os.path.join(DATA, "takervol_BTC_%s.parquet" % bar)
        if not os.path.exists(tpq):
            return {"strategy": strat_key, "bar": bar, "ok": False,
                    "reason": "taker-volume 无 %s 周期数据（OKX 仅提供 1D/1H/5m）" % bar}
        tv = pd.read_parquet(tpq)
        df = s3_cvd.merge_takervol(df, tv)
        qa["takervol_bars"] = int(len(df))
        qa["start"] = fmt(df["ts"].iloc[0])
        qa["end"] = fmt(df["ts"].iloc[-1])
        qa["bars_after_merge"] = int(len(df))
        print("  合并 taker-volume 后 bars=%d  %s ~ %s"
              % (len(df), qa["start"], qa["end"]))

    bt_kw = dict(bar=bar, init_cash=init_cash, leverage=leverage,
                 fee_bps=fee_bps, slippage_bps=slip_bps)
    fund = None
    if os.path.exists(FUNDING_PQ):
        try:
            fund = load_funding(FUNDING_PQ)
            # 仅当回测区间与资金费历史有交集才传入
            if len(df) and fund["ts"].max() >= df["ts"].min():
                bt_kw["funding"] = fund
        except Exception as e:
            print("  资金费率读取失败: %s" % e)

    # --- 参数敏感性（全部参数在样本内表现）---
    g = grid_is_oos(df, fn, grid, objective="sharpe", split=0.7, **bt_kw)
    if not g.get("ok"):
        return {"strategy": strat_key, "bar": bar, "ok": False, "reason": g.get("reason"),
                "rows": g.get("rows")}

    rows = g["all_is_rows"]
    profitable = [r for r in rows if (r.get("total_return_pct") or 0) > 0]
    print("  【样本内】%d 组参数, 其中盈利 %d 组 (%.0f%%), 最优 params=%s"
          % (len(rows), len(profitable), 100.0 * len(profitable) / max(len(rows), 1),
             g["best_params"]))
    print("    IS  总收益 %+9.2f%%  年化 %+9.2f%%  回撤 %8.2f%%  夏普 %6.3f  交易 %d"
          % (g["is_metrics"]["total_return_pct"], g["is_metrics"]["annual_return_pct"],
             g["is_metrics"]["max_drawdown_pct"], g["is_metrics"]["sharpe"],
             g["is_metrics"]["trades"]))
    print("    OOS 总收益 %+9.2f%%  年化 %+9.2f%%  回撤 %8.2f%%  夏普 %6.3f  交易 %d"
          % (g["oos_metrics"]["total_return_pct"], g["oos_metrics"]["annual_return_pct"],
             g["oos_metrics"]["max_drawdown_pct"], g["oos_metrics"]["sharpe"],
             g["oos_metrics"]["trades"]))
    print("    OOS 手续费占初始 %.3f%%  爆仓=%s" %
          (g["oos_metrics"]["fee_pct_of_init"], g["oos_liquidated"]))
    print("    IS 区间 %s ~ %s | OOS 区间 %s ~ %s"
          % (fmt(g["is_start_ts"]), fmt(g["is_end_ts"]),
             fmt(g["oos_start_ts"]), fmt(g["oos_end_ts"])))

    # --- walk-forward ---
    wf = walk_forward(df, fn, grid, n_folds=n_folds, objective="sharpe", **bt_kw)
    if wf.get("ok"):
        print("  【Walk-forward】%d 折, 样本外拼接 bars=%d" % (len(wf["folds"]), wf["stitched_bars"]))
        print("    拼接 OOS 总收益 %+9.2f%%  年化 %+9.2f%%  回撤 %8.2f%%  夏普 %6.3f"
              % (wf["stitched_total_return_pct"], wf["stitched_annual_pct"],
                 wf["stitched_max_dd_pct"], wf["stitched_sharpe"]))
        print("    各折选中参数（稳定性）: %s" % wf["param_stability"])
    else:
        print("  【Walk-forward】不可用: %s" % wf.get("reason"))

    # --- 成本敏感性：只换滑点，参数固定为 IS 最优 ---
    sens = []
    for sl in [1.0, 5.0, 10.0]:
        kw = dict(bt_kw)
        kw["slippage_bps"] = sl
        # IS 最优参数在 OOS 上跑（保持样本外诚实）
        split = int(len(df) * 0.7)
        oos = df.iloc[split:].reset_index(drop=True)
        try:
            sg = fn(oos, g["best_params"])
            rr = run_backtest(oos, sg, **kw)
            sens.append({"slippage_bps": sl,
                         "oos_total_return_pct": rr["metrics"]["total_return_pct"],
                         "oos_sharpe": rr["metrics"]["sharpe"],
                         "oos_fee_pct": rr["metrics"]["fee_pct_of_init"],
                         "trades": rr["metrics"]["trades"]})
        except Exception as e:
            sens.append({"slippage_bps": sl, "error": str(e)[:80]})
    print("  【滑点敏感性】OOS 用 IS 最优参数:")
    for s in sens:
        if "error" in s:
            print("    slip=%.2fbp ERR %s" % (s["slippage_bps"], s["error"]))
        else:
            print("    slip=%5.2fbp -> OOS 总收益 %+9.2f%%  夏普 %6.3f  手续费占比 %.3f%%  交易 %d"
                  % (s["slippage_bps"], s["oos_total_return_pct"], s["oos_sharpe"],
                     s["oos_fee_pct"], s["trades"]))

    # --- 交易抽样（OOS 前 5 笔）---
    sample = []
    try:
        split = int(len(df) * 0.7)
        oos = df.iloc[split:].reset_index(drop=True)
        sg = fn(oos, g["best_params"])
        rr = run_backtest(oos, sg, **bt_kw)
        for t in rr["trades"][:5]:
            sample.append({
                "entry_utc": fmt(t["entry_ts"]), "entry_price": round(t["entry_price"], 4),
                "exit_utc": fmt(t["exit_ts"]), "exit_price": round(t["exit_price"], 4),
                "side": t["side"], "qty": round(t["qty"], 8),
                "fees": round(t["fees"], 4), "gross_pnl": round(t["gross_pnl"], 4),
                "net_pnl": round(t["net_pnl"], 4), "ret_pct": round(t["ret_pct"], 4),
                "exit_reason": t["exit_reason"], "bars_held": t["bars_held"]})
    except Exception as e:
        sample = [{"error": str(e)[:100]}]

    return {
        "strategy": strat_key, "label": label, "bar": bar, "ok": True,
        "qa": qa,
        "cost_model": {"fee_bps": fee_bps, "slippage_bps": slip_bps,
                       "fill": "signal@t close -> fill@t+1 open",
                       "leverage": leverage, "init_cash": init_cash,
                       "funding": ("已计（仅覆盖 2026-06-29 起）" if fund is not None
                                   else "未计（无资金费历史）")},
        "param_grid_size": len(grid),
        "profitable_param_pct": round(100.0 * len(profitable) / max(len(rows), 1), 2),
        "all_is_rows": rows,
        "best_params": g["best_params"],
        "is_metrics": g["is_metrics"],
        "oos_metrics": g["oos_metrics"],
        "is_range": [fmt(g["is_start_ts"]), fmt(g["is_end_ts"])],
        "oos_range": [fmt(g["oos_start_ts"]), fmt(g["oos_end_ts"])],
        "walk_forward": wf,
        "slippage_sensitivity": sens,
        "trade_sample_oos": sample,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--bar", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--fee-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--out", default=OUTJ)
    a = ap.parse_args()

    os.makedirs(LOGDIR, exist_ok=True)
    jobs = []
    if a.all:
        jobs = [("s1", "4H"), ("s1", "1D"), ("s2", "4H"), ("s2", "1D"), ("s3", "1D")]
    elif a.strategy:
        bars = [a.bar] if a.bar else (["1D"] if a.strategy == "s3" else ["4H", "1D"])
        jobs = [(a.strategy, b) for b in bars]
    else:
        ap.error("需要 --strategy 或 --all")

    out = []
    for sk, bar in jobs:
        try:
            r = run_one(sk, bar, fee_bps=a.fee_bps, slip_bps=a.slippage_bps,
                        leverage=a.leverage, n_folds=a.folds)
        except Exception as e:
            import traceback
            traceback.print_exc()
            r = {"strategy": sk, "bar": bar, "ok": False, "reason": "exception: %s" % str(e)[:200]}
        out.append(r)

    # 合并已有结果
    merged = []
    if os.path.exists(a.out):
        try:
            merged = json.load(open(a.out, encoding="utf-8"))
        except Exception:
            merged = []
    keys = {(x.get("strategy"), x.get("bar")) for x in out}
    merged = [m for m in merged if (m.get("strategy"), m.get("bar")) not in keys]
    merged.extend(out)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print("\n结果已写入 %s （共 %d 条）" % (a.out, len(merged)))


if __name__ == "__main__":
    main()
