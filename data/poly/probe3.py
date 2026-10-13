# -*- coding: utf-8 -*-
"""
第三轮: 决定性测试
 A. 用真实 next_cursor 值翻页 (决定性 — poly-ui 用假游标测的, 结论需复核)
 B. lb-api window 枚举 + 真实钱包 positions
 C. gamma prices-history 正确参数
 D. 100 市场字段级缺失率
 E. (a) Σ≠1 无套利检验 — negRisk 分组
 F. (c) favorite-longshot 分层统计
 G. feeSchedule 实测
输出: raw/probe3_result.json, cache/markets_scan.parquet, stats.json
"""
import json, os, time, hashlib
import requests
import pandas as pd

OUT = r"D:\量化\data\poly\raw"
CACHE = r"D:\量化\data\poly\cache"
os.makedirs(OUT, exist_ok=True); os.makedirs(CACHE, exist_ok=True)
PROXY = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) poly-contract-probe/3.0", "Accept": "application/json"}
T = 15
GAMMA, CLOB, LB, DATA = ("https://gamma-api.polymarket.com", "https://clob.polymarket.com",
                         "https://lb-api.polymarket.com", "https://data-api.polymarket.com")
R = []
def log(*a): print(*a, flush=True)

def get(name, url, note="", keep=None, timeout=T):
    rec = {"name": name, "url": url, "note": note}
    t0 = time.time()
    try:
        r = requests.get(url, headers=UA, timeout=timeout, proxies=PROXY)
        rec.update(status=r.status_code, ms=int((time.time()-t0)*1000), bytes=len(r.content))
        try: j = r.json()
        except Exception: j = None
        if j is not None and keep: rec[keep] = j
        if j is not None:
            rec["root"] = type(j).__name__
            if isinstance(j, list): rec["n"] = len(j)
            if isinstance(j, dict): rec["next_cursor"] = j.get("next_cursor","<absent>") if "markets" in j else None
            rec["head"] = json.dumps(j, ensure_ascii=False)[:200]
        else: rec["head"] = r.text[:200]
    except Exception as e:
        rec.update(ms=int((time.time()-t0)*1000), error=repr(e)[:250])
    R.append(rec)
    log(f"[{rec.get('status','ERR')}] {name:40s} {rec.get('ms')}ms {rec.get('bytes','-')}B n={rec.get('n','-')} {rec.get('error','')[:50]}")
    return rec

log("="*78); log("A. 分页 — 用响应里的真实 next_cursor 值翻页"); log("="*78)
p = CACHE
page1 = get("p1.keyset100", f"{GAMMA}/markets/keyset?closed=false&limit=100&order=volume24hr&ascending=false",
            note="第1页", keep="j")
mk1 = page1.get("j", {}).get("markets", [])
cur1 = page1.get("j", {}).get("next_cursor")
ids1 = [m.get("conditionId") for m in mk1]
log(f"    -> 第1页 {len(mk1)} 市场, next_cursor 长度={len(cur1) if cur1 else 0}")

pages = {1: mk1}
if cur1:
    # 真实游标以 next_cursor 参数提交
    for pno, param in [(2, "next_cursor")]:
        r2 = get(f"p2.{param}", f"{GAMMA}/markets/keyset?closed=false&limit=100&order=volume24hr&ascending=false&{param}={requests.utils.quote(cur1, safe='')}",
                 keep="j", note=f"第2页 via {param}=<真实游标>")
        mk2 = r2.get("j", {}).get("markets", []) if isinstance(r2.get("j"), dict) else []
        ids2 = [m.get("conditionId") for m in mk2]
        ov = len(set(ids1) & set(ids2))
        log(f"    -> 第2页 {len(mk2)} 市场, 与第1页重复 {ov} 个, 是否同页={ids1==ids2}")
        pages[2] = mk2
        if ids2 and ids1 != ids2:
            cur2 = r2["j"].get("next_cursor")
            r3 = get("p3.next_cursor", f"{GAMMA}/markets/keyset?closed=false&limit=100&order=volume24hr&ascending=false&next_cursor={requests.utils.quote(cur2, safe='')}",
                     keep="j", note="第3页")
            mk3 = r3.get("j", {}).get("markets", []) if isinstance(r3.get("j"), dict) else []
            log(f"    -> 第3页 {len(mk3)} 市场, 与1+2重复 {len(set(ids for m in mk3 for ids in [m.get('conditionId')]) & (set(ids1)|set(ids2)))}")
            pages[3] = mk3
# 也测老端点是否支持 next_cursor
if cur1:
    get("p_legacy.next_cursor", f"{GAMMA}/markets?closed=false&limit=100&order=volume24hr&ascending=false&next_cursor={requests.utils.quote(cur1, safe='')}",
        note="弃用端点是否吃 keyset 游标")

log("="*78); log("B. lb-api window 枚举 + 真实钱包"); log("="*78)
for w in ["all", "1d", "1w", "1m", "7d", "30d", "week", "month", "daily", "weekly", "monthly"]:
    get(f"lb.window.{w}", f"{LB}/profit?window={w}&limit=3")
lbp = get("lb.profit.all20", f"{LB}/profit?window=all&limit=20", keep="j")
lbv = get("lb.volume.all20", f"{LB}/volume?window=all&limit=20", keep="j")
lbd = get("lb.profit.1d.20", f"{LB}/profit?window=1d&limit=20", keep="j")

wallet = None
for src in (lbp, lbd, lbv):
    j = src.get("j")
    if isinstance(j, list) and j and isinstance(j[0], dict):
        log(f"    lb 字段: {sorted(j[0].keys())}")
        for cand in ("proxyWallet", "wallet", "address"):
            if j[0].get(cand): wallet = j[0][cand]; break
        if wallet: break
log(f"    -> 真实钱包 = {wallet}")

if wallet:
    pos = get("data.positions.real", f"{DATA}/positions?user={wallet}&limit=50", keep="j")
    pj = pos.get("j")
    if isinstance(pj, list) and pj:
        log(f"    positions 字段: {sorted(pj[0].keys())}")
        log("    样例: " + json.dumps(pj[0], ensure_ascii=False)[:500])
    tr = get("data.trades.realuser", f"{DATA}/trades?user={wallet}&limit=10", keep="j")
    tj = tr.get("j")
    if isinstance(tj, list) and tj: log(f"    trades 字段: {sorted(tj[0].keys())}")
    get("data.value.real", f"{DATA}/value?user={wallet}")
    get("data.activity.real", f"{DATA}/activity?user={wallet}&limit=5")

log("="*78); log("C. gamma prices-history 正确参数"); log("="*78)
tok = None; cid = None
if mk1:
    m0 = mk1[0]
    ti = m0.get("clobTokenIds")
    if isinstance(ti, str): ti = json.loads(ti)
    if ti: tok = ti[0]
    cid = m0.get("conditionId")
log(f"    token={tok} cond={cid}")
if tok:
    for nm, u in [("ph.market_token", f"{GAMMA}/prices-history?market={tok}&interval=1d&fidelity=60"),
                  ("ph.market_all", f"{GAMMA}/prices-history?market={tok}&interval=max&fidelity=1440"),
                  ("ph.market_noparam", f"{GAMMA}/prices-history?market={tok}")]:
        get(nm, u)
if cid:
    get("ph.cond", f"{GAMMA}/prices-history?market={cid}&interval=1d&fidelity=60")

log("="*78); log("D. 字段级缺失率 (100 市场) + E.(a) + F.(c)"); log("="*78)
allm = []
seen = set()
for pno in sorted(pages):
    for m in pages[pno]:
        c = m.get("conditionId")
        if c and c not in seen:
            seen.add(c); allm.append(m)
log(f"    去重后市场总数 = {len(allm)}")

FIELDS = ["bestBid","bestAsk","spread","lastTradePrice","outcomePrices","outcomes","clobTokenIds",
          "volume","volume24hr","volume1wk","volume1mo","liquidity","endDate","endDateIso","conditionId",
          "slug","question","oneDayPriceChange","oneWeekPriceChange","oneMonthPriceChange",
          "active","closed","archived","acceptingOrders","enableOrderBook","negRisk","negRiskMarketID",
          "groupItemTitle","groupItemThreshold","feesEnabled","feeType","feeSchedule","createdAt","updatedAt",
          "bestBidYes","bestAskYes","umaResolutionStatus","umaEndDate","category","spread_pct"]
log("    --- 字段缺失率 ---")
missing = {}
for f in FIELDS:
    miss = sum(1 for m in allm if m.get(f) in (None, "", [], "[]"))
    missing[f] = round(miss/len(allm)*100, 2)
for f, v in sorted(missing.items(), key=lambda x: -x[1]):
    log(f"       {f:24s} 缺失 {v:6.2f}%   (有 {len(allm)-int(v*len(allm)/100)}/{len(allm)})")

# 标准化 DataFrame
rows = []
for m in allm:
    def arr(x):
        if isinstance(x, str):
            try: return json.loads(x)
            except Exception: return None
        return x
    op = arr(m.get("outcomePrices")); oc = arr(m.get("outcomes")); ti = arr(m.get("clobTokenIds"))
    rows.append({"conditionId": m.get("conditionId"), "slug": m.get("slug"), "question": m.get("question"),
                 "bestBid": m.get("bestBid"), "bestAsk": m.get("bestAsk"), "spread": m.get("spread"),
                 "lastTradePrice": m.get("lastTradePrice"), "volume": m.get("volume"),
                 "volume24hr": m.get("volume24hr"), "liquidity": m.get("liquidity"),
                 "endDate": m.get("endDate"), "active": m.get("active"), "closed": m.get("closed"),
                 "archived": m.get("archived"), "acceptingOrders": m.get("acceptingOrders"),
                 "negRisk": m.get("negRisk"), "negRiskMarketID": m.get("negRiskMarketID"),
                 "feesEnabled": m.get("feesEnabled"), "feeSchedule": json.dumps(m.get("feeSchedule"), ensure_ascii=False) if m.get("feeSchedule") else None,
                 "n_outcomes": len(oc) if oc else None, "outcome0": op[0] if op else None,
                 "outcome1": op[1] if op and len(op)>1 else None,
                 "outcomes_raw": json.dumps(oc, ensure_ascii=False) if oc else None,
                 "oneDayPriceChange": m.get("oneDayPriceChange"),
                 "tokens": json.dumps(ti) if ti else None})
df = pd.DataFrame(rows)
for c in ["bestBid","bestAsk","spread","lastTradePrice","volume","volume24hr","liquidity","outcome0","outcome1","oneDayPriceChange"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df.to_parquet(os.path.join(CACHE, "markets_scan.parquet"), index=False)
log(f"    -> parquet 落盘 {os.path.join(CACHE,'markets_scan.parquet')}  shape={df.shape}")

# 数据质量校验
dq = {}
dq["n_markets"] = len(df)
dq["n_duplicate_conditionId"] = int(df["conditionId"].duplicated().sum())
dq["null_conditionId"] = int(df["conditionId"].isna().sum())
dq["price_out_of_range_bid"] = int(((df["bestBid"] < 0) | (df["bestBid"] > 1)).sum())
dq["price_out_of_range_ask"] = int(((df["bestAsk"] < 0) | (df["bestAsk"] > 1)).sum())
dq["negative_volume"] = int((df["volume"] < 0).sum())
dq["negative_volume24hr"] = int((df["volume24hr"] < 0).sum())
dq["negative_liquidity"] = int((df["liquidity"] < 0).sum())
dq["crossed_book_bid_gt_ask"] = int((df["bestBid"] > df["bestAsk"]).sum())
dq["closed_true_in_scan"] = int((df["closed"] == True).sum())
dq["active_false"] = int((df["active"] == False).sum())
dq["archived_true"] = int((df["archived"] == True).sum())
dq["inconsistent_active_and_closed"] = int(((df["active"] == True) & (df["closed"] == True)).sum())
dq["no_bid"] = int(df["bestBid"].isna().sum())
dq["no_ask"] = int(df["bestAsk"].isna().sum())
dq["both_sides_quoted"] = int((df["bestBid"].notna() & df["bestAsk"].notna()).sum())
log("    --- 数据质量校验 ---")
for k, v in dq.items(): log(f"       {k:34s} = {v}")

# ---- (a) Σ≠1 无套利检验: negRisk 分组 ----
log("    --- (a) 单市场内部/negRisk 集合内 无套利检验 ---")
sub = df[df["bestBid"].notna() & df["bestAsk"].notna()].copy()
# a1: 单个二元市场 买两边 (Yes+No) 成本
single = sub[sub["n_outcomes"] == 2].copy()
single["cost_both"] = single["bestAsk"]  # 单市场只有 YES 一侧报价, 另一侧=No
log(f"       二元市场(有双边报价) = {len(single)}")
# a2: 真正互斥集合 = 同一 negRiskMarketID 下的多腿
neg = df[df["negRisk"] == True].copy()
groups = neg.groupby("negRiskMarketID")
multi = {g: s for g, s in groups if len(s) >= 2}
log(f"       negRisk=true 市场数 = {len(neg)}; 含 >=2 腿的互斥集合数 = {len(multi)}")
log(f"       negRisk=false 市场数 = {int((df['negRisk']==False).sum())}; negRisk 缺失 = {int(df['negRisk'].isna().sum())}")
arb = []
for gid, g in multi.items():
    gg = g[g["bestAsk"].notna() & g["bestBid"].notna()]
    if len(gg) < 2: continue
    arb.append({"negRiskMarketID": gid, "legs": len(gg), "legs_total": len(g),
                "sum_bestAsk": float(gg["bestAsk"].sum()), "sum_bestBid": float(gg["bestBid"].sum()),
                "edge_buy_pct": float((1 - gg["bestAsk"].sum()) * 100),
                "edge_sell_pct": float((gg["bestBid"].sum() - 1) * 100),
                "sample": gg.iloc[0]["question"][:70] if gg.iloc[0]["question"] else None})
adf = pd.DataFrame(arb)
if len(adf):
    adf = adf.sort_values("sum_bestAsk")
    adf.to_parquet(os.path.join(CACHE, "arb_sets.parquet"), index=False)
    log(f"       互斥集合 Σ(bestAsk) 统计: n={len(adf)} min={adf['sum_bestAsk'].min():.6f} "
        f"median={adf['sum_bestAsk'].median():.6f} max={adf['sum_bestAsk'].max():.6f}")
    log(f"       Σ(bestBid) 统计: min={adf['sum_bestBid'].min():.6f} median={adf['sum_bestBid'].median():.6f} max={adf['sum_bestBid'].max():.6f}")
    log(f"       >> 买入侧套利 (Σask<1) 命中数 = {int((adf['sum_bestAsk']<1).sum())} / {len(adf)}")
    log(f"       >> 卖出侧套利 (Σbid>1) 命中数 = {int((adf['sum_bestBid']>1).sum())} / {len(adf)}")
    log("       Top5 最极端:")
    for _, r in adf.head(5).iterrows():
        log(f"          legs={r['legs']:2d} Σask={r['sum_bestAsk']:.4f} Σbid={r['sum_bestBid']:.4f} buy_edge={r['edge_buy_pct']:+.2f}% | {r['sample']}")
else:
    log("       !! 没有找到任何 >=2 腿的 negRisk 互斥集合 -> (a) 在本扫描范围内无样本")

# ---- (c) favorite-longshot 分层 ----
log("    --- (c) favorite-longshot 分层统计 (按 YES 价 = outcome0) ---")
pr = df[df["outcome0"].notna()].copy()
BINS = [(0,0.05),(0.05,0.15),(0.15,0.50),(0.50,0.85),(0.85,0.95),(0.95,1.0001)]
names = ["0-5c","5-15c","15-50c","50-85c","85-95c","95-100c"]
tot_v24 = pr["volume24hr"].sum(); tot_v = pr["volume"].sum()
log(f"       参与统计市场 = {len(pr)}; Σvolume24hr={tot_v24:,.0f}; Σvolume={tot_v:,.0f}")
buckets = []
for (lo, hi), nm in zip(BINS, names):
    b = pr[(pr["outcome0"] >= lo) & (pr["outcome0"] < hi)]
    v24 = b["volume24hr"].sum(); vv = b["volume"].sum()
    buckets.append({"bucket": nm, "lo": lo, "hi": hi, "n_markets": len(b),
                    "pct_markets": round(len(b)/len(pr)*100, 2),
                    "sum_volume24hr": float(v24), "pct_volume24hr": round(v24/tot_v24*100, 2) if tot_v24 else None,
                    "sum_volume": float(vv), "pct_volume": round(vv/tot_v*100, 2) if tot_v else None,
                    "median_liquidity": float(b["liquidity"].median()) if b["liquidity"].notna().any() else None,
                    "median_spread": float(b["spread"].median()) if b["spread"].notna().any() else None})
for b in buckets:
    log(f"       {b['bucket']:9s} n={b['n_markets']:3d} ({b['pct_markets']:5.2f}%) "
        f"vol24h占比={str(b['pct_volume24hr']):>6}% vol占比={str(b['pct_volume']):>6}% "
        f"中位spread={b['median_spread']}")
pd.DataFrame(buckets).to_parquet(os.path.join(CACHE, "ls_buckets.parquet"), index=False)

log("    --- G. feeSchedule 实测 ---")
fs = df[df["feeSchedule"].notna()]
log(f"       有 feeSchedule 的市场 = {len(fs)}/{len(df)}; feesEnabled=true = {int((df['feesEnabled']==True).sum())}")
if len(fs):
    log("       样例: " + str(fs.iloc[0]["feeSchedule"])[:300])
    for v in fs["feeSchedule"].head(5): log("         " + str(v)[:200])

summary = {"probed_at_bjt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
           "probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "scan_scope": {"endpoint": "/markets/keyset", "order": "volume24hr desc",
                          "pages_fetched": len(pages), "markets_after_dedup": len(allm),
                          "limit_per_page": 100},
           "data_quality": dq, "field_missing_pct": missing,
           "arb_a": ({"sets": len(adf), "buy_hits": int((adf['sum_bestAsk']<1).sum()),
                      "sell_hits": int((adf['sum_bestBid']>1).sum()),
                      "sum_ask_min": float(adf['sum_bestAsk'].min()), "sum_ask_median": float(adf['sum_bestAsk'].median()),
                      "sum_ask_max": float(adf['sum_bestAsk'].max()),
                      "sum_bid_min": float(adf['sum_bestBid'].min()), "sum_bid_median": float(adf['sum_bestBid'].median()),
                      "sum_bid_max": float(adf['sum_bestBid'].max())} if len(adf) else None),
           "longshot_c": buckets}
with open(os.path.join(CACHE, "stats.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
with open(os.path.join(OUT, "probe3_result.json"), "w", encoding="utf-8") as f:
    json.dump({"endpoints": [{k: v for k, v in r.items() if k != "j"} for r in R]}, f, ensure_ascii=False, indent=2)
log("\nSAVED -> cache/stats.json, raw/probe3_result.json, cache/markets_scan.parquet")
