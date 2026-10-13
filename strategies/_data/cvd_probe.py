# -*- coding: utf-8 -*-
"""
独立复核 OKX 逐笔成交 (history-trades) 的分页能力与可回溯深度。
Lead 报称：翻 4 页拿回同一批、100 笔只覆盖 72 秒。
我要拿自己的确切数字，并试多种分页参数。
"""
import sys, json, time, urllib.parse, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROXY = "http://127.0.0.1:7890"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
SLEEP = 0.15


def get(path, params):
    url = "https://www.okx.com" + path + "?" + urllib.parse.urlencode(params)
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            return json.loads(_op.open(req, timeout=25).read().decode()), url
        except Exception as e:
            last = e
            time.sleep(0.7 * (i + 1))
    raise last


def fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(ms) / 1000.0))


print("=" * 96)
print("A) history-trades limit 上限测试")
for lim in [100, 300, 500]:
    js, url = get("/api/v5/market/history-trades", {"instId": "BTC-USDT", "limit": lim})
    d = js.get("data") or []
    print("  limit=%-4s 返回 %3d 笔" % (lim, len(d)))
    time.sleep(SLEEP)

print()
print("=" * 96)
print("B) 100 笔覆盖多长时间 + tradeId 是否可用作分页锚点")
js, _ = get("/api/v5/market/history-trades", {"instId": "BTC-USDT", "limit": "100"})
d = js.get("data") or []
if d:
    ts = [int(x["ts"]) for x in d]
    print("  笔数=%d  newest=%s  oldest=%s" % (len(d), fmt(max(ts)), fmt(min(ts))))
    print("  覆盖 %.1f 秒" % ((max(ts) - min(ts)) / 1000.0))
    print("  tradeId 字段存在: %s  样例: %s" % ("tradeId" in d[0], d[0].get("tradeId")))
    print("  样例记录: %s" % json.dumps(d[0], ensure_ascii=False))
    anchor_trade_id = d[-1].get("tradeId")
    anchor_ts = min(ts)
else:
    anchor_trade_id, anchor_ts = None, None

print()
print("=" * 96)
print("C) 分页参数穷举：能不能真的往回翻？")
first_ids = [x.get("tradeId") for x in d]
last_ids = [x.get("tradeId") for x in d]
cases = []
if anchor_trade_id:
    cases.append(("after=tradeId(最老)", {"instId": "BTC-USDT", "limit": "100", "after": anchor_trade_id}))
    cases.append(("before=tradeId(最新)", {"instId": "BTC-USDT", "limit": "100", "before": first_ids[0]}))
cases.append(("after=ts(最老)", {"instId": "BTC-USDT", "limit": "100", "after": str(anchor_ts)}))
cases.append(("type=1 (仅买)", {"instId": "BTC-USDT", "limit": "300", "type": "1"}))

for label, p in cases:
    try:
        js, _ = get("/api/v5/market/history-trades", p)
        dd = js.get("data") or []
        ids = [x.get("tradeId") for x in dd]
        newn = len(set(ids) - set(first_ids))
        tss = [int(x["ts"]) for x in dd] or [0]
        print("  %-22s 返回 %3d 笔 | 与首批不同的笔数=%3d | 时间 %s ~ %s"
              % (label, len(dd), newn, fmt(min(tss)), fmt(max(tss))))
    except Exception as e:
        print("  %-22s FAIL %s %s" % (label, type(e).__name__, str(e)[:90]))
    time.sleep(SLEEP)

print()
print("=" * 96)
print("D) 用 trades-history (仅近3个月) 能否做日级 CVD 回测？")
try:
    js, _ = get("/api/v5/market/trades-history",
                {"instId": "BTC-USDT", "limit": "100"})
    dd = js.get("data") or []
    print("  trades-history 返回 %d 笔" % len(dd))
    if dd:
        tss = [int(x["ts"]) for x in dd]
        print("  时间 %s ~ %s 覆盖 %.1f 秒" % (fmt(min(tss)), fmt(max(tss)),
                                              (max(tss) - min(tss)) / 1000.0))
    # 试翻页
    if dd:
        p2 = {"instId": "BTC-USDT", "limit": "100", "after": dd[-1]["tradeId"]}
        js2, _ = get("/api/v5/market/trades-history", p2)
        d2 = js2.get("data") or []
        ids1 = set(x.get("tradeId") for x in dd)
        ids2 = set(x.get("tradeId") for x in d2)
        print("  第2页返回 %d 笔, 新增 %d 笔" % (len(d2), len(ids2 - ids1)))
except Exception as e:
    print("  trades-history FAIL %s %s" % (type(e).__name__, str(e)[:150]))

print()
print("=" * 96)
print("E) 要达到 30 天全职逐笔需要多少请求（用实测密度外推）")
if d:
    span_s = (max(ts) - min(ts)) / 1000.0
    per_req_s = span_s
    print("  每请求(100笔)覆盖 %.1f 秒" % per_req_s)
    if per_req_s > 0:
        print("  30 天 = %d 秒 -> 约需 %.0f 次请求" % (30 * 86400, 30 * 86400 / per_req_s))
        print("  按 sleep %.2fs 计，纯等待约 %.1f 小时" % (SLEEP, 30 * 86400 / per_req_s * SLEEP / 3600))
