# -*- coding: utf-8 -*-
"""
第四轮: 修正 complete-set 口径 + 补齐端点
 A. /events 内嵌 markets -> 真正的完整互斥集合 (修正第3轮的假套利)
 B. 找到真正有 positions 的钱包
 C. prices-history 正确端点 (gamma 404 -> 试 clob)
 D. 分页最终确认 (真实游标 x 多参数名)
 E. complete-set 口径下的 (a) 统计
输出: raw/probe4_result.json, cache/events_sets.parquet, cache/stats4.json
"""
import json, os, time
import requests, pandas as pd

OUT = r"D:\量化\data\poly\raw"; CACHE = r"D:\量化\data\poly\cache"
PROXY = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) poly-contract-probe/4.0", "Accept": "application/json"}
T = 20
GAMMA, CLOB, LB, DATA = ("https://gamma-api.polymarket.com", "https://clob.polymarket.com",
                         "https://lb-api.polymarket.com", "https://data-api.polymarket.com")
R = []
def log(*a): print(*a, flush=True)
def get(name, url, note="", keep=None, timeout=T):
    rec = {"name": name, "url": url, "note": note}; t0 = time.time()
    try:
        r = requests.get(url, headers=UA, timeout=timeout, proxies=PROXY)
        rec.update(status=r.status_code, ms=int((time.time()-t0)*1000), bytes=len(r.content))
        try: j = r.json()
        except Exception: j = None
        if j is not None and keep: rec[keep] = j
        if isinstance(j, list): rec["n"] = len(j)
        rec["head"] = json.dumps(j, ensure_ascii=False)[:250] if j is not None else r.text[:250]
    except Exception as e:
        rec.update(ms=int((time.time()-t0)*1000), error=repr(e)[:250])
    R.append(rec)
    log(f"[{rec.get('status','ERR')}] {name:38s} {rec.get('ms')}ms {rec.get('bytes','-')}B n={rec.get('n','-')} {rec.get('error','')[:40]}")
    return rec

def arr(x):
    if isinstance(x, str):
        try: return json.loads(x)
        except Exception: return None
    return x

# ================= A. events complete sets =================
log("="*78); log("A. /events 完整互斥集合 (修正假套利)"); log("="*78)
ev = get("events.vol24hr.100", f"{GAMMA}/events?closed=false&limit=100&order=volume24hr&ascending=false",
         keep="j", timeout=40)
events = ev.get("j") or []
log(f"    事件数 = {len(events)}")
if events:
    log(f"    event 字段: {sorted(events[0].keys())}")
    em = events[0].get("markets")
    if isinstance(em, list) and em:
        log(f"    内嵌 markets 字段: {sorted(em[0].keys())}")

sets = []
for e in events:
    mk = e.get("markets") or []
    if not isinstance(mk, list) or len(mk) < 2: continue
    legs = []
    for m in mk:
        ba, bb = m.get("bestAsk"), m.get("bestBid")
        legs.append({"conditionId": m.get("conditionId"), "slug": m.get("slug"),
                     "question": m.get("question"), "groupItemTitle": m.get("groupItemTitle"),
                     "bestAsk": float(ba) if ba not in (None, "") else None,
                     "bestBid": float(bb) if bb not in (None, "") else None,
                     "spread": m.get("spread"), "liquidity": m.get("liquidity"),
                     "volume24hr": m.get("volume24hr"),
                     "closed": m.get("closed"), "active": m.get("active"),
                     "archived": m.get("archived"),
                     "outcomePrices": m.get("outcomePrices"),
                     "negRisk": m.get("negRisk")})
    ok = [l for l in legs if l["bestAsk"] is not None and l["bestBid"] is not None]
    dis = [l for l in legs if l["closed"] or l["archived"] or l["active"] is False]
    sets.append({"eventSlug": e.get("slug"), "eventTitle": (e.get("title") or "")[:80],
                 "eventNegRisk": e.get("negRisk"), "n_markets": len(mk),
                 "n_quoted": len(ok), "n_disabled": len(dis),
                 "sum_ask": round(sum(l["bestAsk"] for l in ok), 6) if ok else None,
                 "sum_bid": round(sum(l["bestBid"] for l in ok), 6) if ok else None,
                 "sum_ask_all": round(sum(l["bestAsk"] for l in legs if l["bestAsk"] is not None), 6),
                 "legs_used": len(ok),
                 "complete": (len(ok) == len(mk)) and (len(mk) >= 2),
                 "sample_q": (ok[0]["question"][:60] if ok else None)})
sdf = pd.DataFrame(sets)
sdf["edge_buy_pct"] = (1 - sdf["sum_ask"]) * 100
sdf["edge_sell_pct"] = (sdf["sum_bid"] - 1) * 100
sdf.to_parquet(os.path.join(CACHE, "events_sets.parquet"), index=False)
log(f"    -> 含>=2腿的事件 = {len(sdf)}; 其中 negRisk=true = {int((sdf['eventNegRisk']==True).sum())}")
log(f"    -> 全部腿都有双边报价(complete) = {int(sdf['complete'].sum())}")

for flag, lab in [(True, "negRisk=true"), (False, "negRisk=false")]:
    s = sdf[sdf["eventNegRisk"] == flag]
    if not len(s): continue
    log(f"    --- {lab}: n={len(s)} ---")
    log(f"        Σask min={s['sum_ask'].min():.4f} median={s['sum_ask'].median():.4f} max={s['sum_ask'].max():.4f}")
    log(f"        Σbid min={s['sum_bid'].min():.4f} median={s['sum_bid'].median():.4f} max={s['sum_bid'].max():.4f}")
    log(f"        买入侧 Σask<1 命中 = {int((s['sum_ask']<1).sum())}/{len(s)}; 卖出侧 Σbid>1 命中 = {int((s['sum_bid']>1).sum())}/{len(s)}")
    log("        Top5 最极端 Σask:")
    for _, r in s.nsmallest(5, "sum_ask").iterrows():
        log(f"          legs={r['n_markets']:3d} 报价腿={r['legs_used']:3d} complete={r['complete']} "
            f"Σask={r['sum_ask']:.4f} edge={r['edge_buy_pct']:+.2f}% | {r['eventTitle']}")
# 只有 complete 的集合
comp = sdf[sdf["complete"]]
log(f"    === 严格 complete 集合 (所有腿都有双边报价) n={len(comp)} ===")
if len(comp):
    log(f"        Σask min={comp['sum_ask'].min():.6f} median={comp['sum_ask'].median():.6f} max={comp['sum_ask'].max():.6f}")
    log(f"        Σbid min={comp['sum_bid'].min():.6f} median={comp['sum_bid'].median():.6f} max={comp['sum_bid'].max():.6f}")
    log(f"        买入侧命中 Σask<1 = {int((comp['sum_ask']<1).sum())}/{len(comp)}")
    log(f"        卖出侧命中 Σbid>1 = {int((comp['sum_bid']>1).sum())}/{len(comp)}")
    log("        全部 complete 集合明细:")
    for _, r in comp.sort_values("sum_ask").iterrows():
        log(f"          negRisk={str(r['eventNegRisk']):5s} legs={r['n_markets']:3d} Σask={r['sum_ask']:.4f} "
            f"Σbid={r['sum_bid']:.4f} buy={r['edge_buy_pct']:+.2f}% | {r['eventTitle'][:55]}")

# ================= B. 找有持仓的钱包 =================
log("="*78); log("B. 找真正有 positions 的钱包"); log("="*78)
tr = get("trades.200", f"{DATA}/trades?limit=200", keep="j")
tj = tr.get("j") or []
wallets = []
for t in tj:
    w = t.get("proxyWallet")
    if w and w not in wallets: wallets.append(w)
log(f"    从 200 笔成交提取 {len(wallets)} 个钱包")
found = None
for w in wallets[:25]:
    p = get(f"pos.{w[:10]}", f"{DATA}/positions?user={w}&limit=20", keep="j", timeout=12)
    pj = p.get("j")
    if isinstance(pj, list) and pj:
        found = (w, pj)
        log(f"    >>> 钱包 {w} 有 {len(pj)} 个持仓")
        log(f"        positions 字段: {sorted(pj[0].keys())}")
        log("        样例: " + json.dumps(pj[0], ensure_ascii=False)[:600])
        break
if not found:
    log("    !! 25 个钱包全部返回空持仓 — data-api positions 可能只覆盖部分账户")
else:
    w, pj = found
    openp = [x for x in pj if abs(float(x.get("size") or 0)) > 0]
    log(f"    size!=0 的持仓 = {len(openp)}")
    cv = sum(float(x.get("currentValue") or 0) for x in pj)
    log(f"    ΣcurrentValue = {cv:,.2f}  (cashPnl Σ = {sum(float(x.get('cashPnl') or 0) for x in pj):,.2f})")
    # 反查 gamma 补市场名
    if openp:
        cid = openp[0].get("conditionId") or openp[0].get("condition_id")
        log(f"    第一个持仓 conditionId = {cid}")
        if cid:
            g = get("gamma.by_cond", f"{GAMMA}/markets?condition_ids={cid}&limit=1", keep="j")
            gj = g.get("j")
            if isinstance(gj, list) and gj:
                log(f"    gamma 反查成功: {gj[0].get('question','')[:70]}")
            else:
                log(f"    gamma 按 condition_ids 反查返回 {g.get('n')} 条 (可能不支持该参数)")

# ================= C. prices-history =================
log("="*78); log("C. prices-history 端点定位"); log("="*78)
mk = (sdf.iloc[0] if len(sdf) else None)
tok = None
# 从 events 里取一个真实 token
for e in events:
    for m in (e.get("markets") or []):
        ti = arr(m.get("clobTokenIds"))
        if ti: tok = ti[0]; cond = m.get("conditionId"); break
    if tok: break
log(f"    真实 token = {str(tok)[:40]}...")
if tok:
    for nm, u in [("clob.ph.token", f"{CLOB}/prices-history?market={tok}&interval=1d&fidelity=60"),
                  ("clob.ph.max", f"{CLOB}/prices-history?market={tok}&interval=max&fidelity=1440"),
                  ("clob.ph.simple", f"{CLOB}/prices-history?market={tok}"),
                  ("gamma.ph.startTs", f"{GAMMA}/prices-history?market={tok}&startTs=1750000000&endTs=1790000000&fidelity=60"),
                  ("gamma.ph.interval1m", f"{GAMMA}/prices-history?market={tok}&interval=1m&fidelity=10")]:
        get(nm, u)

# ================= D. 分页最终确认 =================
log("="*78); log("D. 分页最终确认 (真实游标 x 各参数名)"); log("="*78)
p1 = get("pg1", f"{GAMMA}/markets/keyset?closed=false&limit=100&order=volume24hr&ascending=false", keep="j")
m1 = p1["j"]["markets"]; cur = p1["j"]["next_cursor"]
ids1 = [m.get("conditionId") for m in m1]
log(f"    第1页 ids 指纹 = {hash(tuple(ids1))}")
for pname in ["next_cursor", "cursor", "after", "start_cursor", "page"]:
    rr = get(f"pg.{pname}", f"{GAMMA}/markets/keyset?closed=false&limit=100&order=volume24hr&ascending=false&{pname}={requests.utils.quote(cur, safe='')}", keep="j")
    j = rr.get("j")
    if isinstance(j, dict):
        m2 = j.get("markets", []); ids2 = [x.get("conditionId") for x in m2]
        log(f"        {pname:14s} -> n={len(m2)} 完全相同={ids1==ids2} 新id数={len(set(ids2)-set(ids1))}")
    else:
        log(f"        {pname:14s} -> status={rr.get('status')} body={str(rr.get('head'))[:80]}")

stats4 = {
    "probed_at_bjt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    "A_complete_sets": {
        "events_scanned": len(events), "events_with_ge2_legs": len(sdf),
        "strict_complete_sets": int(sdf["complete"].sum()),
        "negRisk_true_sets": int((sdf["eventNegRisk"] == True).sum()),
        "negRisk_false_sets": int((sdf["eventNegRisk"] == False).sum()),
        "complete_sum_ask_min": float(comp["sum_ask"].min()) if len(comp) else None,
        "complete_sum_ask_median": float(comp["sum_ask"].median()) if len(comp) else None,
        "complete_sum_ask_max": float(comp["sum_ask"].max()) if len(comp) else None,
        "complete_sum_bid_min": float(comp["sum_bid"].min()) if len(comp) else None,
        "complete_sum_bid_median": float(comp["sum_bid"].median()) if len(comp) else None,
        "complete_sum_bid_max": float(comp["sum_bid"].max()) if len(comp) else None,
        "complete_buy_hits": int((comp["sum_ask"] < 1).sum()) if len(comp) else None,
        "complete_sell_hits": int((comp["sum_bid"] > 1).sum()) if len(comp) else None,
    },
    "B_wallet": {"wallet": found[0] if found else None, "n_positions": len(found[1]) if found else 0,
                 "wallets_probed": min(25, len(wallets))},
}
with open(os.path.join(CACHE, "stats4.json"), "w", encoding="utf-8") as f:
    json.dump(stats4, f, ensure_ascii=False, indent=2, default=str)
with open(os.path.join(OUT, "probe4_result.json"), "w", encoding="utf-8") as f:
    json.dump({"endpoints": [{k: v for k, v in r.items() if k != "j"} for r in R]}, f, ensure_ascii=False, indent=2)
log("\nSAVED -> cache/stats4.json, cache/events_sets.parquet, raw/probe4_result.json")
