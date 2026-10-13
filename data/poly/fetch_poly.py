# -*- coding: utf-8 -*-
"""
fetch_poly.py — Polymarket 公开 API 可复用取数脚本
=================================================
交付: D:\\量化\\data\\poly\\fetch_poly.py

特性
----
* 显式代理 (本机实测: 直连 gamma 24s ConnectTimeout; 必须走 127.0.0.1:7890)
* 超时 / 指数退避重试 / 断点续传 (TTL 内复用缓存)
* 字段缺失显式处理 (缺失记 None + 计数, 不填 0 不填均值)
* 落 parquet/json 到 D:\\量化\\data\\poly\\cache\\
* 末尾 print 「本次实跑摘要」: 市场数 / 时间跨度 / 字段缺失率 / 异常值
* 产出 edges.json (可选 feed, 供前端热插)
* 日志落 D:\\量化\\logs\\

用法
----
  python fetch_poly.py                # 正常跑, TTL 内用缓存
  python fetch_poly.py --refresh      # 忽略缓存强制重取
  python fetch_poly.py --no-proxy     # 关闭代理 (预期失败, 用于验证代理前提)
  python fetch_poly.py --quick        # 只取核心市场数据, 跳过钱包/历史

不改数据: 不做 latin1->utf8 转换 (实测字节层是合法 UTF-8), 直接 r.json()。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd

# ---------------------------------------------------------------- 配置
BASE = os.path.join(os.environ.get("AIQUANT_HOME", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))), "data", "poly")
CACHE = os.path.join(BASE, "cache")
RAW = os.path.join(BASE, "raw")
LOGDIR = os.path.join(os.environ.get("AIQUANT_HOME", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))), "logs")
EDGES_PATH = os.path.join(BASE, "edges.json")
for d in (CACHE, RAW, LOGDIR):
    os.makedirs(d, exist_ok=True)
LOG_PATH = os.path.join(LOGDIR, f"poly-fetch-{datetime.now():%Y%m%d}.log")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
LB = "https://lb-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

PROXY_URL = os.environ.get("POLY_PROXY", "http://127.0.0.1:7890")   # 可用环境变量覆盖
TIMEOUT = (10, 30)              # (connect, read) —— 显式分开, 避免一次挂满 40s
PREFLIGHT_TIMEOUT = 8           # 预检快速失败: 无代理时快速报警退出, 不挂 4 分钟
RETRIES = 2
BACKOFF = 1.6
TTL = 300                       # gamma Cache-Control: public, max-age=300
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) poly-quant-fetch/1.0",
      "Accept": "application/json"}

# 【官方文档已确认】https://docs.polymarket.com/trading/fees :
#     fee = C × feeRate × p × (1 - p)        (C=股数, p=价格)
#   本脚本 fee_per_share = rate * p * (1-p) —— 与官方公式逐字一致, 非假设。
# 官方分类费率表 https://help.polymarket.com/en/articles/13364478-trading-fees :
#   Sports 0.05/rebate 15% | Finance 0.04/25% | Politics 0.04/25% | Economics 0.05/25%
#   Culture 0.05/25% | Weather 0.05/25% | Other 0.05/25% | Tech 0.04/25% | Geopolitics 0/—
#   实测 feeType=sports_fees_v3 且 rate=0.05/rebateRate=0.15 → 与 "Sports 0.05/15%" 完全吻合
# 交叉验证: 真实钱包 42 个持仓 entryFeesUsdc 反解, 中位 2.52% of notional, 区间 0%~6.40%
FEE_MODEL = ("fee_per_share = rate * p * (1-p)  [OFFICIAL: docs.polymarket.com/trading/fees -> "
             "fee = C x feeRate x p x (1-p)]; rate from market feeSchedule (0.04~0.05 observed); "
             "cross-validated on 42 real positions, median 2.52% of notional")
FEE_RATE_FALLBACK = 0.05

_LOG = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    _LOG.append(s)


def logfile(*a):
    s = " ".join(str(x) for x in a)
    _LOG.append(s)
    print(s, flush=True)


# ---------------------------------------------------------------- HTTP
class Fetcher:
    def __init__(self, use_proxy=True, refresh=False):
        self.proxies = ({"http": PROXY_URL, "https": PROXY_URL} if use_proxy
                        else {"http": None, "https": None})
        self.use_proxy = use_proxy
        self.refresh = refresh
        self.stats = []          # 每个请求的实测记录 (耗时/字节/状态)

    def _cache_path(self, key):
        return os.path.join(RAW, f"{key}.json")

    def get(self, key, url, ttl=TTL, expect="any"):
        """带缓存/重试的 GET。返回 (json_or_None, meta)"""
        cp = self._cache_path(key)
        if not self.refresh and os.path.exists(cp):
            age = time.time() - os.path.getmtime(cp)
            if age <= ttl:
                try:
                    with open(cp, encoding="utf-8") as f:
                        j = json.load(f)
                    self.stats.append({"key": key, "url": url, "status": "CACHE",
                                       "ms": 0, "bytes": os.path.getsize(cp), "age_s": round(age, 1)})
                    return j, {"status": "CACHE", "age_s": round(age, 1)}
                except Exception:
                    pass                      # 缓存损坏 -> 重取
        last = None
        for attempt in range(1, RETRIES + 1):
            t0 = time.time()
            try:
                r = requests.get(url, headers=UA, timeout=TIMEOUT, proxies=self.proxies)
                ms = int((time.time() - t0) * 1000)
                rec = {"key": key, "url": url, "status": r.status_code, "ms": ms,
                       "bytes": len(r.content), "attempt": attempt,
                       "deprecation": r.headers.get("deprecation"),
                       "cache_control": r.headers.get("Cache-Control")}
                self.stats.append(rec)
                if r.status_code != 200:
                    logfile(f"  [{r.status_code}] {key:34s} {ms}ms body={r.text[:160]!r}")
                    last = {"status": r.status_code, "body": r.text[:400]}
                    if r.status_code in (400, 404, 422):     # 请求本身错, 重试无意义
                        return None, last
                    time.sleep(BACKOFF ** attempt)
                    continue
                try:
                    j = r.json()
                except Exception as e:
                    return None, {"status": "JSON_ERR", "error": repr(e)[:200]}
                with open(cp, "w", encoding="utf-8") as f:
                    json.dump(j, f, ensure_ascii=False)
                logfile(f"  [200] {key:34s} {ms}ms {len(r.content)}B")
                return j, rec
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                err = repr(e)[:220]
                self.stats.append({"key": key, "url": url, "status": "ERR", "ms": ms, "error": err})
                logfile(f"  [ERR] {key:34s} {ms}ms {err[:110]}")
                last = {"status": "ERR", "error": err}
                if attempt < RETRIES:
                    time.sleep(BACKOFF ** attempt)
        return None, last


# ---------------------------------------------------------------- 标准化
def jarr(x):
    """gamma 的 outcomes / outcomePrices / clobTokenIds 是 JSON 字符串, 需二次 parse。
    显式处理缺失与解析失败 (返回 None, 不编默认值)。"""
    if x in (None, "", []):
        return None
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            j = json.loads(x)
            return j if isinstance(j, list) else None
        except Exception:
            return None
    return None


def fnum(x):
    """转 float; 缺失/空串 -> None (绝不用 0 顶替 '缺失')"""
    if x in (None, "", "None"):
        return None
    try:
        return float(x)
    except Exception:
        return None


def utc_to_bjt(iso):
    if not iso:
        return None
    try:
        s = str(iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


MARKET_FIELDS = [
    "bestBid", "bestAsk", "spread", "lastTradePrice", "outcomePrices", "outcomes", "clobTokenIds",
    "volume", "volume24hr", "volume1wk", "volume1mo", "liquidity", "endDate", "endDateIso",
    "conditionId", "slug", "question", "oneDayPriceChange", "oneWeekPriceChange", "oneMonthPriceChange",
    "active", "closed", "archived", "acceptingOrders", "enableOrderBook", "negRisk", "negRiskMarketID",
    "groupItemTitle", "groupItemThreshold", "feesEnabled", "feeType", "feeSchedule",
    "createdAt", "updatedAt", "orderPriceMinTickSize", "orderMinSize", "takerBaseFee", "makerBaseFee",
]


def norm_market(m, event=None):
    """单个市场 -> 扁平 dict。所有缺失字段显式为 None。"""
    op = jarr(m.get("outcomePrices"))
    oc = jarr(m.get("outcomes"))
    ti = jarr(m.get("clobTokenIds"))
    fs = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else None
    rec = {f: m.get(f) for f in MARKET_FIELDS}
    rec["feeSchedule"] = json.dumps(fs, ensure_ascii=False) if fs else None
    rec["fee_rate"] = fnum(fs.get("rate")) if fs else None
    rec["n_outcomes"] = len(oc) if oc else None
    rec["outcome0"] = fnum(op[0]) if op and len(op) > 0 else None
    rec["outcome1"] = fnum(op[1]) if op and len(op) > 1 else None
    rec["outcomes_list"] = json.dumps(oc, ensure_ascii=False) if oc else None
    rec["token0"] = ti[0] if ti else None
    rec["endDate_bjt"] = utc_to_bjt(m.get("endDate"))
    rec["eventSlug"] = event.get("slug") if event else None
    rec["eventTitle"] = event.get("title") if event else None
    rec["eventNegRisk"] = event.get("negRisk") if event else None
    for c in ("bestBid", "bestAsk", "spread", "lastTradePrice", "volume", "volume24hr", "volume1wk",
              "volume1mo", "liquidity", "oneDayPriceChange", "oneWeekPriceChange", "oneMonthPriceChange",
              "orderPriceMinTickSize", "orderMinSize"):
        rec[c] = fnum(rec.get(c))
    return rec


def _is_missing(v):
    """缺失判定: None / NaN / 空串 / 空数组 都算缺失。
    注意必须用 pd.isna —— 建 DataFrame 时 None 会被转成 NaN，用 `v in (None,...)` 会漏判。"""
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(v, str) and v.strip() in ("", "[]", "None", "null"):
        return True
    if isinstance(v, (list, tuple)) and len(v) == 0:
        return True
    return False


def market_counters(df, all_fields):
    """字段缺失率 (逐字段) + 数据质量校验"""
    miss = {}
    for f in all_fields:
        col = df[f] if f in df.columns else pd.Series([None] * len(df))
        n = int(col.map(_is_missing).sum())
        miss[f] = {"missing": n, "present": len(df) - n,
                   "missing_pct": round(n / len(df) * 100, 2) if len(df) else None}
    dq = {
        "n_markets": int(len(df)),
        "dup_conditionId": int(df["conditionId"].duplicated().sum()) if "conditionId" in df else None,
        "null_conditionId": int(df["conditionId"].isna().sum()) if "conditionId" in df else None,
        "price_bid_out_of_range": int(((df["bestBid"] < 0) | (df["bestBid"] > 1)).sum()),
        "price_ask_out_of_range": int(((df["bestAsk"] < 0) | (df["bestAsk"] > 1)).sum()),
        "neg_volume": int((df["volume"] < 0).sum()),
        "neg_volume24hr": int((df["volume24hr"] < 0).sum()),
        "neg_liquidity": int((df["liquidity"] < 0).sum()),
        "crossed_book": int((df["bestBid"] > df["bestAsk"]).sum()),
        "closed_true": int((df["closed"] == True).sum()),
        "active_false": int((df["active"] == False).sum()),
        "archived_true": int((df["archived"] == True).sum()),
        "active_and_closed": int(((df["active"] == True) & (df["closed"] == True)).sum()),
        "no_bid": int(df["bestBid"].isna().sum()),
        "no_ask": int(df["bestAsk"].isna().sum()),
        "both_sides_quoted": int((df["bestBid"].notna() & df["bestAsk"].notna()).sum()),
    }
    return miss, dq


# ---------------------------------------------------------------- edges.json
def build_edges(events, generated_at):
    """
    (a) 单市场集合内无套利检验 — 只保留「结构上成立」的集合:
        必需条件 1: event.negRisk == True      (互斥性由平台保证)
        必需条件 2: 集合完整 — 事件内所有市场都有双边报价 (bestBid+bestAsk)
    注意: 只按 negRiskMarketID 分组 + 单页扫描会产生**系统性假套利**
          (实测: NBA 2027 冠军集合只扫到 2/36 条腿 -> Σask=0.005 -> 假 +99.5% edge)。
          因此这里用 /events 内嵌 markets 保证腿数完整。
    """
    items, rejected = [], []
    for e in events or []:
        mk = e.get("markets") or []
        if not isinstance(mk, list) or len(mk) < 2:
            continue
        rows = [norm_market(m, e) for m in mk]
        quoted = [r for r in rows if r["bestAsk"] is not None and r["bestBid"] is not None]
        # 僵尸腿 = 已 closed / archived / active=false, 却仍返回 bestBid/bestAsk 的腿。
        # 实测危害: NBA 2027 Champion 36 腿中 6 腿是僵尸, 把 Σbid 从真实水平抬到 1.0200
        #          -> 这是第四类假套利来源, 必须与"集合不完整"分开报, 前端要能分别显示。
        zombie = [r for r in rows if r["closed"] or r["archived"] or r["active"] is False]
        n_mk = len(rows)
        complete = (len(quoted) == n_mk)
        negrisk = bool(e.get("negRisk"))
        sum_ask = sum(r["bestAsk"] for r in quoted) if quoted else None
        sum_bid = sum(r["bestBid"] for r in quoted) if quoted else None
        gross_buy = (1.0 - sum_ask) * 100 if sum_ask is not None else None
        gross_sell = (sum_bid - 1.0) * 100 if sum_bid is not None else None
        # 费用: 逐腿 rate * p * (1-p) —— 官方公式 fee = C x feeRate x p x (1-p)
        fee_buy = sum((r["fee_rate"] if r["fee_rate"] is not None else FEE_RATE_FALLBACK)
                      * r["bestAsk"] * (1 - r["bestAsk"]) for r in quoted) if quoted else None
        fee_sell = sum((r["fee_rate"] if r["fee_rate"] is not None else FEE_RATE_FALLBACK)
                       * r["bestBid"] * (1 - r["bestBid"]) for r in quoted) if quoted else None
        net_buy = (gross_buy - fee_buy * 100) if (gross_buy is not None and fee_buy is not None) else None
        net_sell = (gross_sell - fee_sell * 100) if (gross_sell is not None and fee_sell is not None) else None
        item = {
            "conditionId": quoted[0]["conditionId"] if quoted else (rows[0]["conditionId"] if rows else None),
            "slug": e.get("slug"), "question": (e.get("title") or "")[:160],
            "edge_buy_pct": round(gross_buy, 4) if gross_buy is not None else None,
            "edge_sell_pct": round(gross_sell, 4) if gross_sell is not None else None,
            "cost": round(sum_ask, 6) if sum_ask is not None else None,
            "proceeds": round(sum_bid, 6) if sum_bid is not None else None,
            "outcomes_n": n_mk,
            "viable": bool((net_buy is not None and net_buy > 0) or (net_sell is not None and net_sell > 0)),
            # --- 以下为扩展字段 (超出约定 schema, 供下游判断可信度) ---
            "legs_quoted": len(quoted),
            "legs_disabled": len(zombie),      # 僵尸腿数
            "zombie_legs": len(zombie),
            "complete_set": complete, "neg_risk": negrisk,
            "fee_buy_pct": round(fee_buy * 100, 4) if fee_buy is not None else None,
            "fee_sell_pct": round(fee_sell * 100, 4) if fee_sell is not None else None,
            "net_buy_pct": round(net_buy, 4) if net_buy is not None else None,
            "net_sell_pct": round(net_sell, 4) if net_sell is not None else None,
        }
        # 名义为正 = 未过滤前 gross 公式就给出正收益 —— 前端应高亮这些, 因为它们是假套利的重灾区
        nominal_positive = bool((gross_buy is not None and gross_buy > 0)
                                or (gross_sell is not None and gross_sell > 0))
        if complete and negrisk and not zombie:
            items.append(item)
        else:
            # 多重缺陷全部标记 + 一个主因, 避免前端只看到一个原因而漏判
            flags = []
            if not negrisk:
                flags.append("NON_MUTUALLY_EXCLUSIVE")     # 腿之间非互斥
            if zombie:
                flags.append("ZOMBIE_QUOTES")              # 僵尸报价 (closed/archived 腿仍有盘口)
            if not complete:
                flags.append("INCOMPLETE_SET")             # 集合不完整
            if negrisk and zombie:
                primary = "僵尸报价 (腿已 closed/archived/active=false 却仍返回 bestBid/bestAsk)"
            elif not negrisk:
                primary = "negRisk=false (腿之间非互斥)"
            elif not complete:
                primary = "集合不完整 (部分腿无双边报价)"
            else:
                primary = "其它"
            rejected.append({**item, "reject_reason": primary, "reject_flags": flags,
                             "nominal_positive": nominal_positive})
    items.sort(key=lambda x: (x["edge_buy_pct"] if x["edge_buy_pct"] is not None else 9e9))
    return items, rejected


# ---------------------------------------------------------------- 主流程
def preflight(use_proxy):
    """快速连通性预检 —— 无代理/代理挂掉时 8 秒内报警退出, 不进入重试长循环。"""
    proxies = ({"http": PROXY_URL, "https": PROXY_URL} if use_proxy
               else {"http": None, "https": None})
    url = f"{GAMMA}/markets/keyset?closed=false&limit=1"
    t0 = time.time()
    try:
        r = requests.get(url, headers=UA, timeout=PREFLIGHT_TIMEOUT, proxies=proxies)
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            logfile(f"  预检通过: {url} -> 200 in {ms}ms")
            return True
        logfile(f"  !! 预检失败: HTTP {r.status_code} in {ms}ms  body={r.text[:200]!r}")
        return False
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        logfile(f"  !! 预检失败: {repr(e)[:220]}")
        logfile(f"     （{ms}ms 内失败，未进入重试循环）")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="忽略缓存")
    ap.add_argument("--no-proxy", action="store_true", help="关闭代理(预期失败)")
    ap.add_argument("--quick", action="store_true", help="只取市场数据")
    ap.add_argument("--markets", type=int, default=100, help="keyset 单页市场数(上限100)")
    ap.add_argument("--events", type=int, default=100, help="事件数")
    ap.add_argument("--wallets", type=int, default=5, help="拉取持仓的钱包数")
    ap.add_argument("--skip-preflight", action="store_true", help="跳过连通性预检")
    a = ap.parse_args()

    t_start = time.time()
    f = Fetcher(use_proxy=not a.no_proxy, refresh=a.refresh)
    logfile("=" * 92)
    logfile(f"Polymarket 取数  {datetime.now():%Y-%m-%d %H:%M:%S} (BJT)")
    logfile(f"proxy={PROXY_URL if f.use_proxy else 'OFF(直连)'}  timeout={TIMEOUT} retries={RETRIES} TTL={TTL}s")
    logfile("=" * 92)

    # ---------- 0. 连通性预检 (快速失败) ----------
    if not a.skip_preflight:
        logfile("\n[0] 连通性预检")
        if not preflight(f.use_proxy):
            logfile("\n" + "=" * 92)
            logfile("  [CRITICAL] 无法连通 Polymarket API —— 取数中止，未写入任何新数据。")
            logfile(f"  原因: 代理 {PROXY_URL} 不可用或网络不通" if f.use_proxy
                    else "  原因: 已显式关闭代理(--no-proxy)，实测直连不通是预期结果")
            logfile("  处置: 1) 确认本机代理在 127.0.0.1:7890 运行  "
                    "2) 或设置环境变量 POLY_PROXY 指向可用代理  3) 随后重跑本脚本")
            logfile("  说明: 既有的 cache/stats.json 与 edges.json 未被覆盖，仍是上次成功的结果。")
            logfile("=" * 92)
            with open(LOG_PATH, "w", encoding="utf-8") as fh:
                fh.write("\n".join(_LOG))
            return 2

    # ---------- 1. 市场 (主通道 keyset) ----------
    logfile("\n[1] /markets/keyset  (主通道, 单页上限 100, 分页已实测不可用)")
    ks_url = f"{GAMMA}/markets/keyset?closed=false&limit={min(a.markets,100)}&order=volume24hr&ascending=false"
    ks, ks_meta = f.get("keyset_page1", ks_url)
    markets = (ks or {}).get("markets") or []
    next_cursor = (ks or {}).get("next_cursor")
    if ks is None:
        logfile("  !! keyset 取数失败 —— 后续市场统计不可用。检查代理是否在跑 (127.0.0.1:7890)")
    else:
        logfile(f"  市场 {len(markets)} 条; next_cursor 存在={bool(next_cursor)} (长度 {len(next_cursor or '')})")

    # 弃用端点兜底 (仅当 keyset 挂掉)
    deprecation_note = None
    if not markets:
        logfile("  -> 兜底调用弃用端点 /markets")
        lg, lgm = f.get("markets_legacy", f"{GAMMA}/markets?closed=false&limit={min(a.markets,100)}&order=volume24hr&ascending=false")
        markets = lg or []
        deprecation_note = lgm.get("deprecation") if isinstance(lgm, dict) else None

    mrows = [norm_market(m) for m in markets]
    mdf = pd.DataFrame(mrows)
    if len(mdf):
        mdf.to_parquet(os.path.join(CACHE, "markets_scan.parquet"), index=False)
        logfile(f"  -> cache/markets_scan.parquet  shape={mdf.shape}")

    # ---------- 2. 事件 (完整互斥集合的唯一可靠来源) ----------
    events = []
    if not a.quick:
        logfile("\n[2] /events  (内嵌全部 markets -> 完整互斥集合)")
        ev, _ = f.get("events_vol24hr_100",
                      f"{GAMMA}/events?closed=false&limit={a.events}&order=volume24hr&ascending=false",
                      ttl=TTL)
        events = ev or []
        logfile(f"  事件 {len(events)} 条")
        erows = []
        for e in events:
            for m in (e.get("markets") or []):
                erows.append(norm_market(m, e))
        edf = pd.DataFrame(erows)
        if len(edf):
            edf.to_parquet(os.path.join(CACHE, "event_markets.parquet"), index=False)
            logfile(f"  -> cache/event_markets.parquet  shape={edf.shape} (事件内市场全集, 样本量远大于单页)")

    # ---------- 3. 排行榜 ----------
    lb_data = {}
    if not a.quick:
        logfile("\n[3] lb-api 排行榜 (实测 window 只认 all/1d/7d/30d; 1w/1m 返回 400)")
        for w in ["all", "1d", "7d", "30d"]:
            p, meta = f.get(f"lb_profit_{w}", f"{LB}/profit?window={w}&limit=20", ttl=600)
            lb_data[f"profit_{w}"] = p
            if isinstance(p, list) and p:
                logfile(f"  profit window={w}: 榜首 {p[0].get('pseudonym')} amount={p[0].get('amount'):,.2f}")
        v, _ = f.get("lb_volume_all", f"{LB}/volume?window=all&limit=20", ttl=600)
        lb_data["volume_all"] = v
        if isinstance(v, list) and v:
            logfile(f"  volume window=all: 榜首 {v[0].get('pseudonym')} amount={v[0].get('amount'):,.2f}")
        # 已知 404, 留证据
        for bad in ["leaderboard", "pnl"]:
            _, m2 = f.get(f"lb_{bad}_404", f"{LB}/{bad}?window=all&limit=5", ttl=86400)
        with open(os.path.join(CACHE, "leaderboards.json"), "w", encoding="utf-8") as fh:
            json.dump(lb_data, fh, ensure_ascii=False, indent=2)

    # ---------- 4. 成交流 ----------
    trades = []
    if not a.quick:
        logfile("\n[4] data-api 成交流 / 组合市值")
        tr, _ = f.get("data_trades", f"{DATA}/trades?limit=200", ttl=TTL)
        trades = tr or []
        logfile(f"  成交流 {len(trades)} 条")

    # ---------- 5. 钱包持仓 (COPYTRADE / WALLET TRACKER) ----------
    wallet_pos = {}
    pos_semantics = []
    if not a.quick:
        logfile("\n[5] 钱包持仓 (取 profit 榜钱包, 逐个拉 positions + 用 /value 交叉校验)")
        cand = []
        for w in ["all", "1d", "7d", "30d"]:
            for x in (lb_data.get(f"profit_{w}") or [])[:3]:
                aw = x.get("proxyWallet")
                if aw and aw not in cand:
                    cand.append(aw)
        logfile(f"  候选钱包 {len(cand)} 个; 尝试前 {a.wallets} 个")
        POS_LIMIT = 500                      # 实测 limit=100 会静默截断!
        for waddr in cand[:a.wallets]:
            pj, meta = f.get(f"positions_{waddr}", f"{DATA}/positions?user={waddr}&limit={POS_LIMIT}", ttl=TTL)
            n = len(pj) if isinstance(pj, list) else None
            # /value 是权威聚合值 —— 实测与 ΣcurrentValue 相等 (H1: 3135.14 vs 3135.15)
            val, _ = f.get(f"value_{waddr}", f"{DATA}/value?user={waddr}", ttl=TTL)
            v = fnum(val[0].get("value")) if isinstance(val, list) and val and isinstance(val[0], dict) else None
            if n:
                wallet_pos[waddr] = pj
                cv = sum(fnum(x.get("currentValue")) or 0 for x in pj)
                truncated = (n >= POS_LIMIT)
                tag = "  <<< 已截断(命中 limit)" if truncated else ""
                logfile(f"  {waddr[:12]}... 持仓 {n} 条, ΣcurrentValue={cv:,.2f}, /value={v}, "
                        f"差={None if v is None else round(v-cv,2)}{tag}")
                pos_semantics.append({"wallet": waddr, "n_positions": n, "sum_currentValue": round(cv, 2),
                                      "value_api": v, "delta": None if v is None else round(v - cv, 2),
                                      "truncated_at_limit": truncated})
            else:
                # 空数组: 用 /value 与 activity 判别"真无持仓" vs "接口不覆盖"
                act, _ = f.get(f"activity_{waddr}", f"{DATA}/activity?user={waddr}&limit=100", ttl=TTL)
                na = len(act) if isinstance(act, list) else None
                last = max(((x.get("timestamp") or 0) for x in act), default=None) if isinstance(act, list) and act else None
                last_bjt = (datetime.fromtimestamp(last, timezone.utc).astimezone(
                    timezone(timedelta(hours=8))).strftime("%Y-%m-%d") if last else None)
                verdict = ("真无持仓(positions 空 且 /value=0)" if (v == 0)
                           else "!! 需人工核实(positions 空 但 /value>0 = 覆盖不全)" if (v is not None and v > 0)
                           else "UNKNOWN(/value 取不到)")
                logfile(f"  {waddr[:12]}... 持仓 0 条, /value={v}, activity={na} 最近={last_bjt} -> {verdict}")
                pos_semantics.append({"wallet": waddr, "n_positions": 0, "value_api": v,
                                      "n_activity": na, "last_activity_bjt": last_bjt, "verdict": verdict})
        if not wallet_pos:
            logfile("  !! 这批钱包全部返回空持仓 —— 见上面 verdict 列判断原因")
        with open(os.path.join(CACHE, "wallet_positions.json"), "w", encoding="utf-8") as fh:
            json.dump(wallet_pos, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(CACHE, "positions_semantics.json"), "w", encoding="utf-8") as fh:
            json.dump(pos_semantics, fh, ensure_ascii=False, indent=2)
        trunc = [p for p in pos_semantics if p.get("truncated_at_limit")]
        if trunc:
            logfile(f"  ⚠️ {len(trunc)} 个钱包的持仓数命中 limit={POS_LIMIT} -> 列表已截断, "
                    f"ΣcurrentValue 低于 /value 的真实总值。展示时必须标 TRUNCATED, 不能当总额。")

    # ---------- 6. 价格历史 (PNL CURVE) ----------
    if not a.quick and markets:
        logfile("\n[6] CLOB 价格历史 (PNL CURVE) — gamma /prices-history 实测 404, 正确端点在 clob")
        got = 0
        hist = {}
        for m in markets[:5]:
            tok = None
            ti = jarr(m.get("clobTokenIds"))
            if ti:
                tok = ti[0]
            if not tok:
                continue
            h, meta = f.get(f"ph_{tok[:16]}", f"{CLOB}/prices-history?market={tok}&interval=1d&fidelity=60", ttl=600)
            if isinstance(h, dict) and h.get("history"):
                hist[tok] = h["history"]
                got += 1
        logfile(f"  成功取到 {got}/5 个 token 的价格历史")
        with open(os.path.join(CACHE, "price_history.json"), "w", encoding="utf-8") as fh:
            json.dump(hist, fh, ensure_ascii=False)

    # ---------- 7. 统计 + 数据质量 ----------
    logfile("\n" + "=" * 92)
    logfile("本次实跑摘要")
    logfile("=" * 92)
    summary = {"generated_at_bjt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "proxy_used": PROXY_URL if f.use_proxy else None,
               "scan_scope": {"endpoint": "/markets/keyset", "order": "volume24hr desc",
                              "limit": min(a.markets, 100), "pagination": "BROKEN (已实测, 见 CONTRACT.md)"}}
    if len(mdf):
        miss, dq = market_counters(mdf, MARKET_FIELDS)
        summary["field_missing"] = miss
        summary["data_quality"] = dq
        vol24 = mdf["volume24hr"].sum()
        ed = pd.to_datetime(mdf["endDate"], errors="coerce", utc=True)
        ed_bjt = ed.dt.tz_convert("Asia/Shanghai")
        logfile(f"  市场数            : {len(mdf)}")
        logfile(f"  Σvolume24hr       : {vol24:,.0f} USDC")
        logfile(f"  Σvolume (累计)    : {mdf['volume'].sum():,.0f} USDC")
        logfile(f"  endDate 跨度(UTC) : {ed.min()} ~ {ed.max()}")
        logfile(f"  endDate 跨度(BJT) : {ed_bjt.min()} ~ {ed_bjt.max()}")
        logfile(f"  字段缺失率 (逐字段, 只列有缺失的):")
        for k, v in sorted(miss.items(), key=lambda x: -x[1]["missing_pct"]):
            if v["missing"]:
                logfile(f"     {k:24s} 缺失 {v['missing_pct']:6.2f}%  ({v['missing']}/{len(mdf)})")
        logfile(f"  数据质量异常值:")
        for k, v in dq.items():
            if k.startswith(("dup", "null", "price_", "neg_", "crossed", "active_and")):
                logfile(f"     {k:24s} = {v}")
        logfile(f"  状态分布: closed=true {dq['closed_true']} / active=false {dq['active_false']} / "
                f"archived=true {dq['archived_true']} / 双边报价 {dq['both_sides_quoted']}")

    # ---------- 8. (c) favorite-longshot 分层 ----------
    if len(mdf):
        pr = mdf[mdf["outcome0"].notna()].copy()
        BINS = [(0, .05), (.05, .15), (.15, .50), (.50, .85), (.85, .95), (.95, 1.0001)]
        NAMES = ["0-5c", "5-15c", "15-50c", "50-85c", "85-95c", "95-100c"]
        tv24 = pr["volume24hr"].sum(); tv = pr["volume"].sum()
        buckets = []
        logfile(f"\n  (c) favorite-longshot 分层 [样本: {len(pr)} 个市场, 按 YES 价 outcome0]")
        logfile(f"      {'层':9s} {'市场数':>6s} {'占比':>7s} {'24h量占比':>10s} {'累计量占比':>11s} {'中位spread':>10s}")
        for (lo, hi), nm in zip(BINS, NAMES):
            b = pr[(pr["outcome0"] >= lo) & (pr["outcome0"] < hi)]
            v24 = b["volume24hr"].sum(); vv = b["volume"].sum()
            buckets.append({"bucket": nm, "n": len(b), "pct_n": round(len(b)/len(pr)*100, 2),
                            "sum_volume24hr": float(v24),
                            "pct_volume24hr": round(v24/tv24*100, 2) if tv24 else None,
                            "sum_volume": float(vv), "pct_volume": round(vv/tv*100, 2) if tv else None,
                            "median_spread": float(b["spread"].median()) if b["spread"].notna().any() else None,
                            "median_liquidity": float(b["liquidity"].median()) if b["liquidity"].notna().any() else None})
            logfile(f"      {nm:9s} {len(b):6d} {len(b)/len(pr)*100:6.2f}% "
                    f"{(v24/tv24*100 if tv24 else 0):9.2f}% {(vv/tv*100 if tv else 0):10.2f}% "
                    f"{str(buckets[-1]['median_spread']):>10s}")
        summary["longshot_buckets"] = buckets
        pd.DataFrame(buckets).to_parquet(os.path.join(CACHE, "longshot_buckets.parquet"), index=False)
        logfile("      注: 以上仅为「当前市场的价格分布与成交量集中度」统计事实, 不构成 edge。")

    # ---------- 9. (a) edges.json ----------
    if events:
        items, rejected = build_edges(events, summary["generated_at_bjt"])
        n_viable = sum(1 for i in items if i["viable"])
        edges = {
            "generated_at": summary["generated_at_utc"],
            "generated_at_bjt": summary["generated_at_bjt"],
            "scope": (f"/events?closed=false&limit={a.events}&order=volume24hr 内嵌 markets; "
                      f"单页 markets/keyset limit={min(a.markets,100)} 作为补充; "
                      f"分页不可用故非全市场"),
            "method": ("sum-of-legs arbitrage | negRisk-only + complete-set-only + active-only(zombie-quote "
                       "filtered) + fee-included + page1-limit100 (pagination BROKEN) | fee model: " + FEE_MODEL),
            "fee_included": True,
            "fee_model": FEE_MODEL,
            "fee_source": "OFFICIAL: https://docs.polymarket.com/trading/fees -> fee = C x feeRate x p x (1-p); "
                          "rate table: https://help.polymarket.com/en/articles/13364478-trading-fees",
            "filter_chain": ["negRisk == true", "complete set (every leg has bestBid AND bestAsk)",
                             "active-only (no closed/archived/active=false leg = no zombie quotes)",
                             "fee deducted: sum over legs of rate*p*(1-p)",
                             "scope = page 1 only (limit=100); pagination BROKEN so NOT the whole market"],
            "caveats": [
                "只保留 event.negRisk=true 且所有腿都有双边报价、且无僵尸腿(closed/archived/active=false)的集合",
                "negRisk=false 的集合腿之间可能不互斥, Σ 无意义 (实测 Σbid 最高 66.885, 327 腿)",
                "单页扫描会漏腿 -> 假套利 (实测 NBA 2027 冠军只扫到 2/36 腿 -> 假 +99.5%); 本文件已用 /events 规避",
                "僵尸报价 = 已 closed/archived 的腿仍返回 bestBid/bestAsk; 实测 NBA 2027 冠军 36 腿中 6 腿是僵尸, "
                "把 Σbid 抬到 1.0200 造成名义 +2.00% 假套利 -> 已在 rejected_sets 单列该原因",
                "bestBid 在 100 市场样本中缺失 7~8%, 缺一条腿的报价即判定集合不完整",
                "分页 next_cursor 实测不可用(回传同页 100/100), 本扫描不是全市场 —— "
                "扫描范围 = /events 第一页 100 个事件(内嵌 6249 市场) + keyset 第一页 100 市场; "
                "命中率(0/9, 0/10)不可外推为全市场比例",
                "feeSchedule 缺失时按 rate=0.05 兜底(实测 14~15% 市场无该字段)",
            ],
            "items": items,
            "rejected_sets": rejected,
            "counts": {"sets_considered": len(items) + len(rejected), "sets_kept": len(items),
                       "sets_rejected": len(rejected), "viable": n_viable,
                       "rejected_zombie_quotes": sum(1 for r in rejected if "ZOMBIE_QUOTES" in r.get("reject_flags", [])),
                       "rejected_non_mutually_exclusive": sum(1 for r in rejected if "NON_MUTUALLY_EXCLUSIVE" in r.get("reject_flags", [])),
                       "rejected_incomplete_set": sum(1 for r in rejected if "INCOMPLETE_SET" in r.get("reject_flags", [])),
                       "rejected_nominal_positive": sum(1 for r in rejected if r.get("nominal_positive"))},
        }
        with open(EDGES_PATH, "w", encoding="utf-8") as fh:
            json.dump(edges, fh, ensure_ascii=False, indent=2)
        logfile(f"\n  (a) Σ(legs) 无套利检验 [negRisk-only + complete-set-only + active-only]")
        logfile(f"      参与集合 {len(items)+len(rejected)}, 保留 {len(items)}, 剔除 {len(rejected)}, 净可行 {n_viable}")
        rej = {}
        for r in rejected:
            rej[r["reject_reason"]] = rej.get(r["reject_reason"], 0) + 1
        for k, v in sorted(rej.items(), key=lambda x: -x[1]):
            logfile(f"      剔除主因: {k} = {v}")
        logfile(f"      多标签统计(一个集合可命中多个): " + json.dumps(
            {k: v for k, v in edges["counts"].items() if k.startswith("rejected_")}, ensure_ascii=False))
        logfile(f"      --- 保留集合明细 ---")
        for i in items[:12]:
            logfile(f"      legs={i['outcomes_n']:3d} Σask={i['cost']:.4f} Σbid={i['proceeds']:.4f} "
                    f"gross_buy={i['edge_buy_pct']:+.2f}% net_buy={i['net_buy_pct']:+.2f}% "
                    f"gross_sell={i['edge_sell_pct']:+.2f}% net_sell={i['net_sell_pct']:+.2f}% "
                    f"viable={i['viable']} | {i['question'][:48]}")
        # 名义正收益 (未过滤前 gross 就为正) 但被剔除的 —— 必须显式报出, 不许静默丢弃
        nom = [r for r in rejected if r.get("nominal_positive")]
        pos_nom = [r for r in nom if r["neg_risk"]]
        logfile(f"      --- 名义正收益但被剔除的 negRisk 候选 ({len(pos_nom)}) —— 假套利重灾区 ---")
        for r in sorted(pos_nom, key=lambda x: -(x["proceeds"] or 0))[:10]:
            logfile(f"      legs={r['outcomes_n']:3d} 僵尸腿={r['zombie_legs']:3d} "
                    f"Σask={r['cost']:.4f} Σbid={r['proceeds']:.4f} "
                    f"标签={','.join(r['reject_flags'])} 主因={r['reject_reason'][:32]} | {r['question'][:40]}")
        if not pos_nom:
            logfile("      无")
        logfile(f"      -> {EDGES_PATH}")
        summary["arb"] = edges["counts"]
        summary["arb_reject_reasons"] = rej
        summary["arb_nominal_rejected_negrisk"] = [
            {k: r.get(k) for k in ("question", "outcomes_n", "zombie_legs", "cost", "proceeds",
                                   "reject_reason", "reject_flags")}
            for r in sorted(pos_nom, key=lambda x: -(x["proceeds"] or 0))[:10]]

    summary["requests"] = f.stats
    if 'pos_semantics' in dir() and pos_semantics:
        summary["positions_semantics"] = pos_semantics
    if hist:
        _lens = {t: len(v) for t, v in hist.items()}
        _ts = [p["t"] for v in hist.values() for p in v if isinstance(p, dict) and p.get("t")]
        summary["price_history_depth"] = {
            "tokens": len(hist), "points_per_token": _lens,
            "min_points": min(_lens.values()), "max_points": max(_lens.values()),
            "span_days": (round((max(_ts) - min(_ts)) / 86400, 2) if _ts else None),
            "limitation": ("会话级短周期, 不是回测级长周期。gamma/prices-history 实测全 404; "
                           "clob interval=max 实测只回 1 个点 -> 长历史当前拿不到"),
        }
    ok = sum(1 for s in f.stats if s.get("status") == 200)
    cac = sum(1 for s in f.stats if s.get("status") == "CACHE")
    err = sum(1 for s in f.stats if s.get("status") in ("ERR", "JSON_ERR"))

    # 不许用失败run覆盖成功run的 stats.json —— 否则数据质量/分层/套利结果全丢
    totally_failed = (ok == 0 and cac == 0)
    if totally_failed:
        bad_path = os.path.join(CACHE, "stats_failed.json")
        with open(bad_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)
        logfile(f"\n  !! 本次全部请求失败 —— 失败摘要写入 {bad_path}")
        logfile("     cache/stats.json 保持上次成功结果不变。")
    else:
        with open(os.path.join(CACHE, "stats.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)

    logfile(f"\n  请求统计: 200={ok} CACHE={cac} 失败={err} 合计={len(f.stats)}")
    logfile(f"  耗时 {time.time()-t_start:.1f}s")
    logfile(f"  落盘: cache/stats.json | cache/markets_scan.parquet | edges.json")
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_LOG))
    logfile(f"  日志: {LOG_PATH}")
    if totally_failed:
        logfile("\n  !! 全部请求失败。若 --no-proxy 是预期结果; 否则请确认代理 127.0.0.1:7890 在运行。")
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("interrupted", flush=True); sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
