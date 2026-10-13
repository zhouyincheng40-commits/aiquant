# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\s1_macd.py
策略 1：MACD 动能 — 金叉做多 / 死叉平仓（或反手做空）。

信号定义（bar t 收盘计算，引擎在 t+1 开盘成交）：
    dif  = EMA(close, fast) - EMA(close, slow)
    dea  = EMA(dif, signal)
    金叉: dif 上穿 dea  ->  目标仓位 +1（做多）
    死叉: dif 下穿 dea  ->  目标仓位 -1（反手做空，若 allow_short）
                            否则 0（平仓空仓）
参数（params dict）：
    fast, slow, signal : MACD 三参数（fast<slow 才有意义）
    allow_short        : 死叉是反手做空(True)还是仅平仓(False)
    confirm            : 需要连续 confirm 根保持金叉/死叉才认，默认 1
用法：
    from s1_macd import signal_macd
    sig = signal_macd(df, {"fast":12,"slow":26,"signal":9,"allow_short":False})
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_engine"))
from indicators import macd, cross_up, cross_dn  # noqa: E402

DEFAULT_PARAMS = {
    "fast": 12,
    "slow": 26,
    "signal": 9,
    "allow_short": False,
    "confirm": 1,
}


def signal_macd(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    fast, slow, sg = int(p["fast"]), int(p["slow"]), int(p["signal"])
    if fast >= slow:
        return pd.Series(np.zeros(len(df)), index=df.index)
    dif, dea, _ = macd(df["close"], fast, slow, sg)
    up = cross_up(dif, dea)
    dn = cross_dn(dif, dea)
    conf = max(1, int(p.get("confirm", 1)))
    if conf > 1:
        up = up.rolling(conf, min_periods=conf).sum().fillna(0) >= conf
        dn = dn.rolling(conf, min_periods=conf).sum().fillna(0) >= conf

    # 事件驱动状态机：金叉->目标 +1；死叉->目标 short_target
    short_target = -1.0 if p.get("allow_short") else 0.0
    sig = np.zeros(len(df), dtype=float)
    state = 0.0
    upv = up.to_numpy()
    dnv = dn.to_numpy()
    # 前 slow+signal 根不进仓位（指标未预热）
    warm = slow + sg
    for i in range(len(df)):
        if i >= warm:
            if upv[i]:
                state = 1.0
            elif dnv[i]:
                state = short_target
        sig[i] = state
    return pd.Series(sig, index=df.index)


PARAM_GRID_DEFAULT = [
    {"fast": f, "slow": s, "signal": sg, "allow_short": sh}
    for f, s in [(5, 20), (8, 21), (10, 30), (12, 26), (16, 48), (20, 60)]
    for sg in [9]
    for sh in [False, True]
]


def describe() -> str:
    return ("MACD 动能：EMA(fast)-EMA(slow) 与 EMA(signal) 的金叉/死叉驱动"
            "多空状态机。金叉做多，死叉平仓或反手。")
