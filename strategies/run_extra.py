# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\run_extra.py
Lead 要求的三项硬性补充：
  (a) 资金费率：每策略同时报「含可得资金费」与「资金费=0」两版（成本上界/下界）
  (b) 滑点敏感性真跑三档：0.01% / 0.05% / 0.10%（= 1 / 5 / 10 bp）
  (c) 同一策略在 4H 与 1D 上结论是否一致（不一致即过拟合信号）

口径声明：K 线用 BTC-USDT **现货**，资金费率用 BTC-USDT-**SWAP**。
         这是"用现货价做永续持仓"的代理口径；现货与永续基差通常 <0.1%，
         但严格说不是同一条价格序列，报告中已声明。
"""
from __future__ import annotations

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

from backtest import (load_parquet, load_funding, run_backtest, walk_forward,
                      BAR_WIDTH_MS)                       # noqa: E402
import s1_macd, s2_rsi_vwap, s3_cvd                        # noqa: E402

DATA = os.path.join(HERE, "_data")
FUNDING_PQ = os.path.join(DATA, "funding_BTCUSDTSWAP.parquet")
STRATS = {
    "s1": ("MACD 动能", s1_macd.signal_macd, s1_macd.PARAM_GRID_DEFAULT),
    "s2": ("RSI+VWAP 反转", s2_rsi_vwap.signal_rsi_vwap, s2_rsi_vwap.PARAM_GRID_DEFAULT),
    "s3": ("CVD 背离", s3_cvd.signal_cvd, s3_cvd.PARAM_GRID_DEFAULT),
}
SLIP_LEVELS_BP = [1.0, 5.0, 10.0]      # = 0.01% / 0.05% / 0.10%
FEE_BP = 5.0


def fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ms) / 1000.0))


def load_df(sk, bar):
    df = load_parquet(os.path.join(DATA, "BTCUSDT_%s.parquet" % bar))
    bad = (df["low"] < df["close"] / 10) | (df["open"] < df["close"] / 10)
    df = df[~bad].reset_index(drop=True)
    if sk == "s3":
        tpq = os.path.join(DATA, "takervol_BTC_%s.parquet" % bar)
        if not os.path.exists(tpq):
            return None
        df = s3_cvd.merge_takervol(df, pd.read_parquet(tpq))
    return df


def best_params_from_results(sk, bar):
    p = os.path.join(HERE, "results.json")
    if not os.path.exists(p):
        return None
    for r in json.load(open(p, encoding="utf-8")):
        if r.get("strategy") == sk and r.get("bar") == bar and r.get("ok"):
            return r.get("best_params")
    return None


def main():
    fund = load_funding(FUNDING_PQ) if os.path.exists(FUNDING_PQ) else None
    print("=" * 104)
    print("资金费率历史覆盖: %s ~ %s (%d 个结算点)"
          % (fmt(fund["ts"].min()), fmt(fund["ts"].max()), len(fund)))
    print("=" * 104)

    out = {"funding_versions": [], "slippage_grid": [], "tf_consistency": []}

    # ==============================================================
    # (a) 资金费率两版 + (b) 滑点三档   —— 在同一批 OOS 上跑
    # ==============================================================
    print("\n" + "=" * 104)
    print("(a)+(b) 样本外结果：资金费 两版 × 滑点 三档   （参数=S 样本内最优，样本外从不参与选参）")
    print("=" * 104)
    print("%-4s %-4s %-6s %-9s %-11s %-11s %-9s %-8s %-9s %s"
          % ("策略", "bar", "滑点", "资金费", "OOS总收益%", "OOS夏普", "OOS回撤%", "交易数", "手续费%", "资金费$"))

    for sk in ["s1", "s2", "s3"]:
        label, fn, grid = STRATS[sk]
        bars = ["1D"] if sk == "s3" else ["4H", "1D"]
        for bar in bars:
            df = load_df(sk, bar)
            if df is None or len(df) < 50:
                print("%-4s %-4s 数据不足，跳过" % (sk, bar))
                continue
            bp = best_params_from_results(sk, bar)
            if bp is None:
                print("%-4s %-4s 无最优参数记录，跳过（先跑 run_all.py）" % (sk, bar))
                continue
            split = int(len(df) * 0.7)
            oos = df.iloc[split:].reset_index(drop=True)
            oos_years = (int(oos["ts"].iloc[-1]) - int(oos["ts"].iloc[0])) / (365 * 86400000.0)

            for slip in SLIP_LEVELS_BP:
                for fund_mode, fund_arg in [("含可得", fund), ("=0(上界)", None)]:
                    kw = dict(bar=bar, init_cash=10_000.0, leverage=1.0,
                              fee_bps=FEE_BP, slippage_bps=slip, allow_short=False)
                    if fund_arg is not None:
                        kw["funding"] = fund_arg
                    try:
                        sg = fn(oos, bp)
                        r = run_backtest(oos, sg, **kw)
                        m = r["metrics"]
                    except Exception as e:
                        print("%-4s %-4s slip=%-4.1f %-8s ERR %s" % (sk, bar, slip, fund_mode, str(e)[:50]))
                        continue
                    row = {"strategy": sk, "bar": bar, "slippage_bps": slip,
                           "funding_mode": fund_mode,
                           "oos_total_return_pct": m["total_return_pct"],
                           "oos_sharpe": m["sharpe"],
                           "oos_max_dd_pct": m["max_drawdown_pct"],
                           "trades": m["trades"],
                           "fee_pct_of_init": m["fee_pct_of_init"],
                           "funding_total": m["funding_total"],
                           "funding_coverage_ok": m.get("funding_coverage_ok"),
                           "oos_years": round(oos_years, 3)}
                    out["funding_versions"].append(row)
                    out["slippage_grid"].append(dict(row))
                    print("%-4s %-4s %-6.2f %-9s %+11.2f %+11.3f %+9.2f %-8d %-9.3f %+.4f"
                          % (sk, bar, slip, fund_mode, m["total_return_pct"], m["sharpe"],
                             m["max_drawdown_pct"], m["trades"], m["fee_pct_of_init"],
                             m["funding_total"]))
            print("-" * 104)

    # ==============================================================
    # (c) 4H vs 1D 一致性
    # ==============================================================
    print("\n" + "=" * 104)
    print("(c) 同一策略在 4H 与 1D 上结论是否一致（不一致 = 过拟合信号）")
    print("=" * 104)
    print("%-4s %-6s %-10s %-11s %-10s %-11s %-11s %s"
          % ("策略", "bar", "IS总收益%", "OOS总收益%", "OOS夏普", "OOS交易数", "盈利参数%", "符号一致?"))
    for sk in ["s1", "s2"]:
        label, fn, grid = STRATS[sk]
        signs = {}
        for bar in ["4H", "1D"]:
            df = load_df(sk, bar)
            bp = best_params_from_results(sk, bar)
            if df is None or bp is None:
                continue
            split = int(len(df) * 0.7)
            is_df = df.iloc[:split].reset_index(drop=True)
            oos = df.iloc[split:].reset_index(drop=True)
            kw = dict(bar=bar, init_cash=10_000.0, leverage=1.0,
                      fee_bps=FEE_BP, slippage_bps=2.0, allow_short=False)
            r_is = run_backtest(is_df, fn(is_df, bp), **kw)
            r_oos = run_backtest(oos, fn(oos, bp), **kw)
            # 盈利参数占比
            prof = None
            rp = os.path.join(HERE, "results.json")
            if os.path.exists(rp):
                for rr in json.load(open(rp, encoding="utf-8")):
                    if rr.get("strategy") == sk and rr.get("bar") == bar and rr.get("ok"):
                        prof = rr.get("profitable_param_pct")
            signs[bar] = np.sign(r_oos["metrics"]["total_return_pct"])
            row = {"strategy": sk, "bar": bar,
                   "is_total_pct": r_is["metrics"]["total_return_pct"],
                   "oos_total_pct": r_oos["metrics"]["total_return_pct"],
                   "oos_sharpe": r_oos["metrics"]["sharpe"],
                   "oos_trades": r_oos["metrics"]["trades"],
                   "profitable_param_pct": prof}
            out["tf_consistency"].append(row)
            print("%-4s %-6s %+10.2f %+11.2f %+10.3f %-11d %-11s %s"
                  % (sk, bar, row["is_total_pct"], row["oos_total_pct"],
                     row["oos_sharpe"], row["oos_trades"],
                     (str(prof) + "%") if prof is not None else "n/a", ""))
        if len(signs) == 2:
            same = signs["4H"] == signs["1D"]
            print("     -> %s 在 4H 与 1D 的样本外收益**符号%s**%s"
                  % (sk, ("一致" if same else "不一致"),
                     "" if same else "  ⚠️ 这是过拟合信号：同一逻辑在改动周期后结论翻转"))
    print("-" * 104)

    with open(os.path.join(HERE, "extra_checks.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\nsaved -> %s" % os.path.join(HERE, "extra_checks.json"))

    # 抽样说明（slip=5bp 档，两版资金费并列，便于人工核对）
    print("\n抽样核对（slip=5bp 档）：")
    for r in out["funding_versions"]:
        if r["slippage_bps"] == 5.0:
            print("  %-4s %-4s 资金费=%-8s OOS %+8.2f%%  夏普 %+.3f  回归绝对差校验见 selfcheck"
                  % (r["strategy"], r["bar"], r["funding_mode"],
                     r["oos_total_return_pct"], r["oos_sharpe"]))


if __name__ == "__main__":
    main()
