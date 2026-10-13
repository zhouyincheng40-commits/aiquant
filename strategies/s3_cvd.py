# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\s3_cvd.py
策略 3：CVD 背离 —— 跟着"钱"走。

★★★ 数据口径声明（这一条决定结论强度，必须读） ★★★
本策略**不使用** (close-low-(high-close))/(high-low)*vol 这类代理指标，
而是使用 OKX 官方 `/api/v5/rubik/stat/taker-volume` 返回的**主动买量 / 主动卖量**
（真·逐笔聚合的 taker side 成交量），因此 delta 与 CVD 都是真实数据：

    delta(t) = takerBuyVol(t) - takerSellVol(t)
    CVD(t)   = Σ delta(1..t)

**但样本量是硬约束**：该端点只有
    1D = 180 根（2026-04-14 ~ 2026-10-10）
    1H = 720 根（30 天）
    5m = 575 根（2 天）
且**没有 4H / 15m**。逐笔 history-trades 分页虽可用，但实测每页仅 100 笔、
1.23 秒/页，30 天需约 5.2 万次请求≈17.7 小时，2 年 4H 需约 126 万次请求≈430 小时
—— 因此**长期 CVD 回测不可行**，本策略只能在日线 180 根上做，样本外仅约 54 根。
→ 结论强度：**弱**。报告中按"样本量不足"标注，不按"已证伪"标注。

信号定义（bar t 收盘计算）：
    看涨背离: 价格创 lookback 新低(N 根内最低) 而 CVD 未创同期新低  -> 做多
    看跌背离: 价格创 lookback 新高 而 CVD 未创同期新高              -> 平多/做空
    出场:     反向背离 或 持有超过 max_hold 根

参数：
    lookback(20), max_hold(30), allow_short(False), use_cvd_slope(False)
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_engine"))

DEFAULT_PARAMS = {
    "lookback": 20,
    "max_hold": 30,
    "allow_short": False,
    "use_cvd_slope": False,
}


def build_cvd(takervol: pd.DataFrame) -> pd.DataFrame:
    """从 taker-volume 原始表构造 delta / CVD。"""
    t = takervol.sort_values("ts").reset_index(drop=True).copy()
    t["delta"] = t["takerBuyVol"] - t["takerSellVol"]
    t["cvd"] = t["delta"].cumsum()
    return t


def signal_cvd(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """
    df 必须已含列 delta 与 cvd（由 merge_takervol 合进来的真实数据）。
    """
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    n = len(df)
    sig = np.zeros(n, dtype=float)
    if n < 10 or "cvd" not in df.columns:
        return pd.Series(sig, index=df.index)

    lb = int(p["lookback"])
    max_hold = int(p["max_hold"])
    allow_short = bool(p.get("allow_short", False))
    close = df["close"].to_numpy(dtype=float)
    cvd = df["cvd"].to_numpy(dtype=float)
    if p.get("use_cvd_slope"):
        cvd = pd.Series(cvd).diff(int(params.get("slope_window", 5))).fillna(0).to_numpy()

    state = 0.0
    held = 0
    lows = pd.Series(close).rolling(lb, min_periods=lb).min().to_numpy()
    highs = pd.Series(close).rolling(lb, min_periods=lb).max().to_numpy()
    cvd_lows = pd.Series(cvd).rolling(lb, min_periods=lb).min().to_numpy()
    cvd_highs = pd.Series(cvd).rolling(lb, min_periods=lb).max().to_numpy()

    for i in range(n):
        if i < lb:
            sig[i] = state
            continue
        # 价格创 lb 新低（含当根）——用 close[i] 是否为窗口最小值判断
        new_low = close[i] <= lows[i] + 1e-12
        new_high = close[i] >= highs[i] - 1e-12
        # 背离：价格新低但 CVD 未新低（资金没跟着走）
        bull_div = new_low and cvd[i] > cvd_lows[i] + 1e-12
        bear_div = new_high and cvd[i] < cvd_highs[i] - 1e-12

        if state == 0.0:
            if bull_div:
                state, held = 1.0, 0
            elif allow_short and bear_div:
                state, held = -1.0, 0
        elif state > 0:
            held += 1
            if bear_div or held >= max_hold:
                state, held = 0.0, 0
        elif state < 0:
            held += 1
            if bull_div or held >= max_hold:
                state, held = 0.0, 0
        sig[i] = state
    return pd.Series(sig, index=df.index)


def merge_takervol(ohlc: pd.DataFrame, takervol: pd.DataFrame) -> pd.DataFrame:
    """
    把 taker-volume 的 delta/cvd 按 ts 合并到 OHLC 上（inner join，只保留两者都有
    的 bar）。合并后行数会显著少于 OHLC —— 这正是样本量限制的体现。
    """
    tv = build_cvd(takervol)[["ts", "takerBuyVol", "takerSellVol", "delta", "cvd"]]
    m = ohlc.merge(tv, on="ts", how="inner").sort_values("ts").reset_index(drop=True)
    return m


PARAM_GRID_DEFAULT = [
    {"lookback": lb, "max_hold": mh, "allow_short": sh}
    for lb in [10, 20, 30]
    for mh in [15, 30, 60]
    for sh in [False, True]
]


def describe() -> str:
    return ("CVD 背离：价格创新高/新低而累计成交量 delta(CVD) 未同步时反向入场。"
            "使用 OKX 官方 taker buy/sell volume（真数据，非代理指标），"
            "但仅日线 180 根可用，样本外约 54 根，结论强度弱。")
