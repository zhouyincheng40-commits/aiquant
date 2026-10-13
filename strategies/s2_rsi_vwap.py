# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\s2_rsi_vwap.py
策略 2：RSI 超买超卖 + VWAP 过滤的反转策略。

信号定义（bar t 收盘计算，引擎在 t+1 开盘成交）：
    做多入场: RSI(t) 上穿 oversold       且 close(t) < VWAP(t)   （价在 VWAP 下方才抄底）
    多头出场: RSI(t) 下穿 overbought    或 close(t) > VWAP(t)   （回到 VWAP 上方就走）
    （可选 allow_short 对称做空：RSI 下穿 overbought 且 close > VWAP 做空）

⚠️ VWAP 锚点必须写清否则不可复现。本实现支持两种，params["vwap_mode"]：
    "day"     : 日锚 —— UTC 自然日 00:00 重置累加（适合日内 bar 1H/4H）
    "rolling" : 滚动锚 —— 最近 vwap_window 根 bar（适合日线 bar）
   default = "day"。日线数据上用 "day" 会退化（每根即当日），脚本会自动改用 rolling。

参数：
    rsi_period(14), oversold(30), overbought(70),
    vwap_mode("day"|"rolling"), vwap_window(20), allow_short(False)
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_engine"))
from indicators import rsi, vwap_daily_anchored, vwap_rolling, cross_up, cross_dn  # noqa: E402

DEFAULT_PARAMS = {
    "rsi_period": 14,
    "oversold": 30,
    "overbought": 70,
    "vwap_mode": "day",
    "vwap_window": 20,
    "allow_short": False,
    "exit_on_vwap": True,
}


def build_vwap(df: pd.DataFrame, params: dict) -> pd.Series:
    mode = params.get("vwap_mode", "day")
    # 日线 bar 用日锚无意义（每根 bar 自己就是一天），自动降级为滚动锚并记入 params
    if mode == "day":
        w = int(df["ts"].iloc[1] - df["ts"].iloc[0]) if len(df) > 1 else 86400000
        if w >= 86400000:
            mode = "rolling"
    if mode == "rolling":
        return vwap_rolling(df, int(params.get("vwap_window", 20)))
    return vwap_daily_anchored(df)


def signal_rsi_vwap(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    n = len(df)
    if n < 5:
        return pd.Series(np.zeros(n), index=df.index)
    r = rsi(df["close"], int(p["rsi_period"]))
    vw = np.asarray(build_vwap(df, p), dtype=float)
    close = df["close"].to_numpy(dtype=float)
    osold, obought = float(p["oversold"]), float(p["overbought"])

    up_cross = cross_up(r, pd.Series(osold, index=df.index)).to_numpy()
    dn_cross = cross_dn(r, pd.Series(obought, index=df.index)).to_numpy()

    sig = np.zeros(n, dtype=float)
    state = 0.0
    warm = int(p["rsi_period"]) + 1
    allow_short = bool(p.get("allow_short", False))
    exit_on_vwap = bool(p.get("exit_on_vwap", True))
    for i in range(n):
        if i >= warm and np.isfinite(vw[i]):
            if state == 0.0:
                if up_cross[i] and close[i] < vw[i]:
                    state = 1.0
                elif allow_short and dn_cross[i] and close[i] > vw[i]:
                    state = -1.0
            elif state > 0:
                if dn_cross[i] or (exit_on_vwap and close[i] > vw[i]):
                    state = 0.0
            elif state < 0:
                if up_cross[i] or (exit_on_vwap and close[i] < vw[i]):
                    state = 0.0
        sig[i] = state
    return pd.Series(sig, index=df.index)


PARAM_GRID_DEFAULT = [
    {"rsi_period": rp, "oversold": os_, "overbought": ob, "vwap_mode": vm,
     "allow_short": sh}
    for rp in [7, 14, 21]
    for os_, ob in [(30, 70), (25, 75), (35, 65)]
    for vm in ["day", "rolling"]
    for sh in [False]
]


def describe() -> str:
    return ("RSI+VWAP 反转：RSI 超卖且价在 VWAP 下方做多，RSI 超买或价回 VWAP 上方平仓。"
            "VWAP 锚点可切换 日锚(UTC 自然日) / 滚动锚(N 根)。")
