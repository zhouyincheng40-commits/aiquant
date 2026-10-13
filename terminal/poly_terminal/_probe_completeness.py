"""临时探针：验证 /events 是否返回**完整**互斥集合，以及完整性校验该用什么字段。

问题清单（对应 Lead 的紧急口径修正）：
 1. len(ev['markets']) 是否等于事件声明的腿数？有哪些候选声明字段？
 2. /markets/keyset?limit=200 实际返回几条？offset 是否 422？
 3. 被 closed 过滤掉的腿有多少（会不会因过滤而"人为制造"不完整集合）？
 4. NBA 2027 Champion (478277) 这个集合到底几条腿？Σask / Σbid 是多少？
 5. feeSchedule.rate 的实际分布。
"""
import json
import sys

import requests

PROXY = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
S = requests.Session()
S.proxies = PROXY
S.headers.update({"User-Agent": "poly-probe/1.0"})
G = "https://gamma-api.polymarket.com"


def get(url):
    r = S.get(url, timeout=30)
    return r.status_code, r


def num(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def jarr(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            p = json.loads(v)
            return p if isinstance(p, list) else []
        except Exception:
            return []
    return []


print("=" * 78)
print("A) keyset limit / offset 行为")
for q in ("limit=60", "limit=100", "limit=200"):
    st, r = get(f"{G}/markets/keyset?closed=false&{q}&order=volume24hr&ascending=false")
    j = r.json()
    print(f"  {q:10s} status={st} markets={len(j.get('markets', []))} cursor={str(j.get('next_cursor'))[:24]}")
st, r = get(f"{G}/markets/keyset?closed=false&limit=100&offset=100&order=volume24hr&ascending=false")
print(f"  offset=100 status={st} body={r.text[:120]}")

print("=" * 78)
print("B) 第一页(limit=100)里出现的互斥集合候选 + 完整性字段")
st, r = get(f"{G}/markets/keyset?closed=false&limit=100&order=volume24hr&ascending=false")
page = r.json()["markets"]
by_ev = {}
for m in page:
    evs = m.get("events") or []
    if evs and evs[0].get("id"):
        by_ev.setdefault(str(evs[0]["id"]), []).append(m)
print(f"  第一页 {len(page)} 条，分属 {len(by_ev)} 个 event；其中多腿 event：")
cands = [(k, v) for k, v in by_ev.items() if len(v) >= 2]
for k, v in cands:
    print(f"    ev={k:9s} page_legs={len(v):3d}  {v[0].get('question','')[:52]}")

print("=" * 78)
print("C) /events/<id> 完整性：声明的腿数 vs 实际 markets 数")
ev_keys_of_interest = ["cumulativeMarkets", "negRiskMarketID", "negRisk", "enableNegRisk", "closed"]
for k, v in cands[:10]:
    st, r = get(f"{G}/events/{k}")
    ev = r.json()
    ms = ev.get("markets") or []
    closed = [m for m in ms if m.get("closed") is True]
    live = [m for m in ms if m.get("closed") is not True]
    print(f"  ev={k:9s} ok={st} markets={len(ms):3d} closed={len(closed):3d} live={len(live):3d} "
          f"cumulativeMarkets={ev.get('cumulativeMarkets')!r} showAllOutcomes={ev.get('showAllOutcomes')!r} "
          f"negRisk={ev.get('negRisk')!r}  {str(ev.get('title'))[:34]}")

print("=" * 78)
print("D) NBA 2027 Champion 复核（Lead 点名）")
st, r = get(f"{G}/events/478277")
ev = r.json()
ms = ev.get("markets") or []
print(f"  title={ev.get('title')!r} markets={len(ms)} cumulativeMarkets={ev.get('cumulativeMarkets')!r}")
live = [m for m in ms if m.get("closed") is not True]
sa = sb = 0.0
miss = 0
rates = {}
for m in live:
    a, b = num(m.get("bestAsk")), num(m.get("bestBid"))
    if a is None or b is None:
        miss += 1
        continue
    sa += a
    sb += b
    fr = (m.get("feeSchedule") or {}).get("rate")
    rates[fr] = rates.get(fr, 0) + 1
print(f"  live_legs={len(live)} 缺腿={miss}  Σask={sa:.4f}  Σbid={sb:.4f}  官方价和(ΣoutcomePrices[0])="
      f"{sum(x for x in (num(jarr(m.get('outcomePrices'))[0]) or 0 for m in live)):.4f}")
print(f"  feeSchedule.rate 分布: {rates}")
print(f"  tick 分布: {sorted({num(m.get('orderPriceMinTickSize')) for m in live})}")

print("=" * 78)
print("E) 买入侧假套利复现（只在单页分组时出现）")
for k, v in cands:
    if len(v) == 2:
        sa = sum(num(m.get("bestAsk")) or 0 for m in v)
        sb = sum(num(m.get("bestBid")) or 0 for m in v)
        print(f"  ev={k:9s} page_legs=2 Σask={sa:.4f} → 假 edge_buy={(1/max(sa,1e-9)-1)*100:8.2f}%  "
              f"Σbid={sb:.4f}   {v[0].get('question','')[:44]}")

print("=" * 78)
print("F) lb-api window 参数实测")
for w in ("all", "1d", "7d", "30d", "1w", "1m"):
    st, r = get(f"https://lb-api.polymarket.com/profit?window={w}&limit=2")
    print(f"  window={w:4s} status={st} body={r.text[:70]}")
