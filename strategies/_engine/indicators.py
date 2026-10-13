# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\_engine\\indicators.py
共用指标（全部向量化、只用当期及历史数据，无未来函数）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, span: int) -> pd.Series:
    """标准 EMA。前 span 根用 expanding mean 预热，之后递推。"""
    s = pd.Series(s).astype(float)
    a = 2.0 / (span + 1.0)
    out = np.full(len(s), np.nan)
    if len(s) == 0:
        return pd.Series(out)
    out[0] = s.iloc[0]
    for i in range(1, len(s)):
        out[i] = a * s.iloc[i] + (1 - a) * out[i - 1]
    return pd.Series(out, index=s.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ef = ema(close, fast)
    es = ema(close, slow)
    dif = ef - es
    dea = ema(dif, signal)
    hist = dif - dea
    return dif, dea, hist


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI。"""
    c = pd.Series(close).astype(float)
    d = c.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    # Wilder 平滑 = alpha 1/period 的 EMA
    au = up.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    ad = dn.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(50.0)


def vwap_daily_anchored(df: pd.DataFrame, typical: str = "hlc3") -> pd.Series:
    """
    日锚 VWAP：每个 UTC 自然日开盘重置累加，vwap = Σ(典型价×量)/Σ量。

    锚点说明（可复现的关键）：本实现锚点 = **UTC 自然日 00:00**。
    bar 时间戳已是 UTC 毫秒；用 UTC 日期分组重置。
    日内 bar（1H/4H）用日锚；日线 bar 用日锚时等于"当根即当日"，退化无效，
    那种情况请改用 vwap_rolling()。
    """
    d = df[["ts", "high", "low", "close", "vol"]].copy()
    if typical == "hlc3":
        d["tp"] = (d["high"] + d["low"] + d["close"]) / 3.0
    elif typical == "hl2":
        d["tp"] = (d["high"] + d["low"]) / 2.0
    else:
        d["tp"] = d["close"]
    d["day"] = pd.to_datetime(d["ts"], unit="ms", utc=True).dt.floor("D")
    d["pv"] = d["tp"] * d["vol"]
    g = d.groupby("day", sort=False)
    cum_pv = g["pv"].cumsum()
    cum_v = g["vol"].cumsum()
    return (cum_pv / cum_v.replace(0.0, np.nan)).ffill()


def vwap_rolling(df: pd.DataFrame, window: int = 20, typical: str = "hlc3") -> pd.Series:
    """
    滚动锚 VWAP：最近 window 根 bar 的 Σ(典型价×量)/Σ量。锚点 = 滚动窗口。
    """
    d = df[["high", "low", "close", "vol"]].copy()
    if typical == "hlc3":
        d["tp"] = (d["high"] + d["low"] + d["close"]) / 3.0
    else:
        d["tp"] = (d["high"] + d["low"]) / 2.0
    pv = (d["tp"] * d["vol"]).rolling(window, min_periods=max(2, window // 2)).sum()
    v = d["vol"].rolling(window, min_periods=max(2, window // 2)).sum()
    return pv / v.replace(0.0, np.nan)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def cross_dn(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))
