"""复核：严格完整度校验后，每个 negRisk 候选集合到底因为什么被剔除。

回答 Lead 的点名问题：
  - NBA: 2027 Champion (478277) 真实腿数多少？是否也有已关闭腿 → 现在是否被剔除？
  - 现在唯一那行结果用的是哪个集合、是否真的是完整集合？
"""
import json
import sys

sys.path.insert(0, r"D:\量化\terminal\poly_terminal")
from feeds import GAMMA, get_feeds, jarr, num  # noqa: E402

f = get_feeds()
page = f._cached("keyset", 0, f._get_keyset_page, True)
print(f"keyset first page: {len(page)} markets")

by_ev = {}
for m in page:
    evs = m.get("events") or []
    if evs and evs[0].get("id"):
        by_ev.setdefault(str(evs[0]["id"]), []).append(m)
cands = sorted([g for g in by_ev.values() if len(g) >= 2], key=lambda g: -len(g))[: f.cfg["scan_event_max"]]
print(f"multi-leg candidate events: {len(cands)}\n")

hdr = f"{'event':>9} {'page':>4} {'mkt':>4} {'closed':>6} {'missbook':>8} {'usable':>6} {'Σask':>8} {'Σbid':>7} {'verdict':>12}  title"
print(hdr)
print("-" * len(hdr))

for g in cands:
    ev_id = str(g[0]["events"][0]["id"])
    ev = f._get_json(f"{GAMMA}/events/{ev_id}")
    mkts = ev.get("markets") or []
    closed = sum(1 for m in mkts if m.get("closed") is True)
    missbook = 0
    sa = sb = 0.0
    for m in mkts:
        a, b = num(m.get("bestAsk")), num(m.get("bestBid"))
        if a is None or b is None:
            missbook += 1
            continue
        sa += a
        sb += b
    neg = (ev.get("negRisk") is True) or (ev.get("enableNegRisk") is True)
    if not neg:
        verdict = "non-excl"
    elif closed or missbook:
        verdict = "INCOMPLETE"
    else:
        verdict = "complete"
    print(f"{ev_id:>9} {len(g):>4} {len(mkts):>4} {closed:>6} {missbook:>8} "
          f"{len(mkts)-closed-missbook:>6} {sa:>8.4f} {sb:>7.4f} {verdict:>12}  {str(ev.get('title'))[:38]}")

print("\n--- NBA 2027 Champion (478277) 专项 ---")
ev = f._get_json(f"{GAMMA}/events/478277")
ms = ev.get("markets") or []
print(f"  真实 markets 数 = {len(ms)}   negRisk={ev.get('negRisk')}  cumulativeMarkets={ev.get('cumulativeMarkets')!r}")
cl = [m for m in ms if m.get("closed") is True]
nb = [m for m in ms if num(m.get("bestAsk")) is None or num(m.get("bestBid")) is None]
print(f"  closed={len(cl)}  缺盘口={len(nb)}")
if cl:
    print(f"  已关闭样例: {[ (m.get('groupItemTitle') or m.get('question','')[:28]) for m in cl[:5] ]}")
if nb:
    print(f"  缺盘口样例: {[ (m.get('groupItemTitle') or m.get('question','')[:28]) for m in nb[:5] ]}")
print(f"  → 结论: {'剔除（集合含已关闭腿，不完整）' if (cl or nb) else '保留'}")

print("\n--- 最终 snapshot 的 SCANNER 结果 ---")
snap = f.snapshot(force=True)
meta = snap["meta"]
for k in ("scanRangeLabel", "scanRange", "scanEvents", "scanNonExclusive", "scanIncomplete",
          "scanRejected", "scanFeeMissing", "scannerSource"):
    print(f"  {k:18s} = {meta.get(k)}")
print(f"  rows = {len(snap['scanner'])}")
for r in snap["scanner"]:
    print(f"    • {r['market'][:44]}  legs={r['legs']}  Σask={r['sumAsk']:.4f} Σbid={r['sumBid']:.4f} "
          f"fee={r['feeRate']}  feeMissing={r['feeMissing']}  gross={r['grossPct']:.2f}% net={r['netPct']:.2f}% "
          f"viable={r['viable']} side={r['side']}")
print("\n  health:", snap["health"])
print("  errors:", snap["errors"])
