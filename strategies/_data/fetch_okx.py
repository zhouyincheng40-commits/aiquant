# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\_data\\fetch_okx.py
OKX 历史 K 线分页拉取 -> parquet，带：
  * 显式代理（必须，直连 DNS 解析失败）
  * User-Agent（不带 UA 直接 403）
  * 超时 + 重试 + 每请求 sleep
  * 断点续传（已有 parquet 就接着最老一根往回翻）
  * 真实深度探测（翻到空/不再前进即停，并记录"疑似截断"）
  * 字段规范化（ts 毫秒 UTC，另存北京时间）
  * 数据质量校验（缺口 / 重复 / 异常值 / 零成交量）

用法:
  python fetch_okx.py --inst BTC-USDT --bar 4H --max-pages 60
  python fetch_okx.py --inst BTC-USDT --bar 1D --max-pages 40
  python fetch_okx.py --quality-only --inst BTC-USDT --bar 4H
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROXY = os.environ.get("OKX_PROXY", "http://127.0.0.1:7890")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE = "https://www.okx.com/api/v5/market/history-candles"
FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate-history"
OUTDIR = os.path.join(os.environ.get("AIQUANT_HOME", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))), "strategies", "_data")
LOGDIR = os.path.join(os.environ.get("AIQUANT_HOME", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))), "logs")
LIMIT = 300                      # OKX 硬上限
SLEEP = 0.15                     # 每请求间隔，别把接口打爆

BAR_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
          "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000, "1D": 86_400_000}

_op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    sys.stdout.flush()


def http_json(url: str, tries: int = 4, timeout: int = 25) -> dict:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"})
            raw = _op.open(req, timeout=timeout).read().decode("utf-8")
            js = json.loads(raw)
            if js.get("code") not in (None, "0"):
                raise RuntimeError("OKX code=%s msg=%s" % (js.get("code"), js.get("msg")))
            return js
        except Exception as e:
            last = e
            time.sleep(0.7 * (i + 1))
    raise last


# ----------------------------------------------------------------------
def fetch_candles(inst: str, bar: str, max_pages: int, resume: bool = True) -> dict:
    assert bar in BAR_MS, "未知 bar %s" % bar
    os.makedirs(OUTDIR, exist_ok=True)
    pq = os.path.join(OUTDIR, "%s_%s.parquet" % (inst.replace("-", ""), bar))
    meta_path = os.path.join(OUTDIR, "%s_%s.meta.json" % (inst.replace("-", ""), bar))

    rows: list[tuple] = []
    seen: set[int] = set()
    if resume and os.path.exists(pq):
        old = pd.read_parquet(pq)
        for r in old.itertuples(index=False):
            t = int(r.ts)
            if t not in seen:
                seen.add(t)
                rows.append((t, float(r.open), float(r.high), float(r.low),
                             float(r.close), float(r.vol)))
        log("断点续传：已有 %d 根，最老 %s" % (len(rows), fmt(min(seen)) if seen else "N/A"))

    after = min(seen) if seen else None
    pages = 0
    added = 0
    stop_reason = "max_pages"
    while pages < max_pages:
        q = {"instId": inst, "bar": bar, "limit": str(LIMIT)}
        if after is not None:
            q["after"] = str(after)
        url = BASE + "?" + urllib.parse.urlencode(q)
        try:
            js = http_json(url)
        except Exception as e:
            stop_reason = "http_error:%s:%s" % (type(e).__name__, str(e)[:80])
            log("请求失败，停止：%s" % stop_reason)
            break
        data = js.get("data") or []
        if not data:
            stop_reason = "empty_return(疑似服务端上限截断)"
            log("返回空 -> 停止。stop_reason=%s" % stop_reason)
            break
        batch = []
        for r in data:
            t = int(r[0])
            if t in seen:
                continue
            seen.add(t)
            batch.append((t, float(r[1]), float(r[2]), float(r[3]),
                          float(r[4]), float(r[5])))
        if not batch:
            stop_reason = "no_progress(整页重复，疑似截断)"
            log("整页重复 -> 停止。stop_reason=%s" % stop_reason)
            break
        rows.extend(batch)
        added += len(batch)
        pages += 1
        new_oldest = min(b[0] for b in batch)
        if after is not None and new_oldest >= after:
            stop_reason = "no_progress(不再变老)"
            break
        after = new_oldest
        if pages % 10 == 0:
            log("  第 %d 页, 累计 %d 根, 最老 %s" % (pages, len(rows), fmt(after)))
            flush_parquet(rows, pq)          # 周期性落盘，防中断丢失
        time.sleep(SLEEP)

    df = flush_parquet(rows, pq)
    qa = quality_check(df, bar)
    meta = {
        "inst": inst, "bar": bar, "bars": int(len(df)),
        "oldest_ms": int(df["ts"].min()), "oldest_utc": fmt(int(df["ts"].min())),
        "newest_ms": int(df["ts"].max()), "newest_utc": fmt(int(df["ts"].max())),
        "span_days": round((int(df["ts"].max()) - int(df["ts"].min())) / 86_400_000.0, 3),
        "pages_fetched": pages, "bars_added": added,
        "stop_reason": stop_reason,
        "suspect_truncated": ("截断" in stop_reason or "error" in stop_reason),
        "fetch_time_utc": fmt(int(time.time() * 1000)),
        "proxy": PROXY, "quality": qa,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log("落盘 %s  bars=%d" % (pq, len(df)))
    log("meta  %s" % meta_path)
    return meta


def flush_parquet(rows, path):
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol"])
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    df["ts"] = df["ts"].astype("int64")
    df["dt_utc"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df["dt_bj"] = df["dt_utc"].dt.tz_convert("Asia/Shanghai")
    for c in ("open", "high", "low", "close", "vol"):
        df[c] = df[c].astype(float)
    # 规范化列顺序
    df = df[["ts", "dt_utc", "dt_bj", "open", "high", "low", "close", "vol"]]
    df.to_parquet(path, index=False)
    return df


def quality_check(df: pd.DataFrame, bar: str) -> dict:
    """数据质量校验：缺口 / 重复 / 异常值 / 零成交量。"""
    w = BAR_MS[bar]
    ts = df["ts"].to_numpy(dtype=np.int64)
    gaps = np.diff(ts) if len(ts) > 1 else np.array([], dtype=np.int64)
    n_exact = int((gaps == w).sum())
    n_gap = int((gaps > w).sum())
    n_overlap = int((gaps < w).sum())
    # 缺口明细（最多列 5 个最大的）
    gap_detail = []
    if n_gap > 0:
        idx = np.where(gaps > w)[0]
        order = idx[np.argsort(-gaps[idx])][:5]
        for i in order:
            gap_detail.append({
                "after_ts": int(ts[i]), "after_utc": fmt(int(ts[i])),
                "missing_bars": int(gaps[i] // w - 1),
            })
    dup = int(df["ts"].duplicated().sum())
    # 异常值：high<low, close 超出 [low, high], 价格<=0
    bad_hl = int((df["high"] < df["low"]).sum())
    bad_close = int(((df["close"] > df["high"]) | (df["close"] < df["low"])).sum())
    bad_px = int((df[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
    zero_vol = int((df["vol"] <= 0).sum())
    mono = bool(np.all(np.diff(ts) > 0))
    return {
        "bars": int(len(df)),
        "monotonic_ts": mono,
        "dup_ts": dup,
        "gap_exact": n_exact, "gap_missing": n_gap, "gap_overlap": n_overlap,
        "gap_missing_pct": round(100.0 * n_gap / max(len(gaps), 1), 3),
        "missing_bars_total": int(sum(g["missing_bars"] for g in gap_detail)),
        "largest_gaps": gap_detail,
        "bad_high_low": bad_hl, "bad_close_range": bad_close, "bad_price": bad_px,
        "zero_vol_bars": zero_vol,
        "zero_vol_pct": round(100.0 * zero_vol / max(len(df), 1), 3),
    }


def fetch_funding(inst_swap: str = "BTC-USDT-SWAP", max_pages: int = 12) -> pd.DataFrame:
    """资金费率历史（OKX 每 8 小时结算）。分页用 before/after。"""
    rows = []
    after = None
    for _ in range(max_pages):
        q = {"instId": inst_swap, "limit": "100"}
        if after is not None:
            q["after"] = str(after)
        url = FUNDING_URL + "?" + urllib.parse.urlencode(q)
        try:
            js = http_json(url)
        except Exception as e:
            log("funding 请求失败: %s" % e)
            break
        data = js.get("data") or []
        if not data:
            break
        for r in data:
            rows.append({"ts": int(r["fundingTime"]),
                         "fundingRate": float(r["fundingRate"])})
        oldest = min(int(r["fundingTime"]) for r in data)
        if after is not None and oldest >= after:
            break
        after = oldest
        time.sleep(SLEEP)
    if not rows:
        return pd.DataFrame(columns=["ts", "fundingRate"])
    f = pd.DataFrame(rows).drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    f["dt_utc"] = pd.to_datetime(f["ts"], unit="ms", utc=True)
    out = os.path.join(OUTDIR, "funding_%s.parquet" % inst_swap.replace("-", ""))
    f.to_parquet(out, index=False)
    log("资金费率落盘 %s  rows=%d  %s ~ %s"
        % (out, len(f), fmt(int(f["ts"].min())), fmt(int(f["ts"].max()))))
    return f


def fmt(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default="BTC-USDT")
    ap.add_argument("--bar", default="4H")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--quality-only", action="store_true")
    ap.add_argument("--funding", action="store_true")
    a = ap.parse_args()
    os.makedirs(LOGDIR, exist_ok=True)
    if a.funding:
        fetch_funding(); return
    if a.quality_only:
        pq = os.path.join(OUTDIR, "%s_%s.parquet" % (a.inst.replace("-", ""), a.bar))
        df = pd.read_parquet(pq)
        print(json.dumps(quality_check(df, a.bar), ensure_ascii=False, indent=2))
        return
    meta = fetch_candles(a.inst, a.bar, a.max_pages, resume=not a.no_resume)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
