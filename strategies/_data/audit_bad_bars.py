# -*- coding: utf-8 -*-
"""核查坏 bar：open/low=1.00 这类不可能值分布在哪、有多少。"""
import sys
import pandas as pd, numpy as np
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

for name, pq in [("1D", r"D:\量化\strategies\_data\BTCUSDT_1D.parquet"),
                 ("4H", r"D:\量化\strategies\_data\BTCUSDT_4H.parquet"),
                 ("1H", r"D:\量化\strategies\_data\BTCUSDT_1H.parquet")]:
    df = pd.read_parquet(pq)
    print("=" * 100)
    print("[%s] %d 根  %s ~ %s" % (name, len(df),
          pd.to_datetime(df.ts.iloc[0], unit="ms", utc=True),
          pd.to_datetime(df.ts.iloc[-1], unit="ms", utc=True)))
    print("-" * 100)
    print("前 3 行：")
    print(df.head(3)[["ts", "open", "high", "low", "close", "vol"]].to_string(index=False))
    print("末 2 行：")
    print(df.tail(2)[["ts", "open", "high", "low", "close", "vol"]].to_string(index=False))
    # 坏 bar 判定：low < close/10 （日内最低不足收盘的 10%，几乎必为脏数据）
    bad = df[(df["low"] < df["close"] / 10) | (df["open"] < df["close"] / 10) |
             (df["high"] < df["close"] / 10)]
    print("\n坏 bar（low/open/high < close/10）: %d 根" % len(bad))
    if len(bad):
        print(bad[["ts", "open", "high", "low", "close", "vol"]].to_string(index=False))
    # 全零量
    z = df[df["vol"] <= 0]
    print("零成交量 bar: %d 根 %s" % (len(z), list(pd.to_datetime(z.ts, unit="ms", utc=True))[:5]))
    print()
