# -*- coding: utf-8 -*-
"""探测是否存在可批量回补历史逐笔的端点（决定 CVD 能否真回测）。"""
import sys, json, time, urllib.parse, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROXY = "http://127.0.0.1:7890"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def try_url(path, params):
    url = "https://www.okx.com" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "application/json"})
        raw = _op.open(req, timeout=25).read().decode()
        js = json.loads(raw)
        code = js.get("code")
        d = js.get("data")
        n = len(d) if isinstance(d, list) else "n/a"
        return "code=%s n=%s msg=%s" % (code, n, str(js.get("msg"))[:60])
    except urllib.error.HTTPError as e:
        return "HTTP %d %s" % (e.code, e.reason)
    except Exception as e:
        return "ERR %s %s" % (type(e).__name__, str(e)[:70])


print("=" * 88)
print("候选端点探测（找可批量回补历史逐笔的接口）")
print("-" * 88)
cases = [
    ("/api/v5/market/trades-history", {"instId": "BTC-USDT", "limit": "100"}),
    ("/api/v5/market/history-trades", {"instId": "BTC-USDT", "limit": "100"}),
    ("/api/v5/market/trades", {"instId": "BTC-USDT", "limit": "500"}),
    ("/api/v5/market/history-trades", {"instId": "BTC-USDT", "limit": "100", "type": "2"}),
    ("/api/v5/market/history-trades", {"instId": "BTC-USDT", "limit": "100",
                                       "begin": str(int((time.time() - 86400) * 1000))}),
    ("/api/v5/rubik/stat/taker-volume", {"ccy": "BTC", "instType": "SPOT", "period": "1D"}),
    ("/api/v5/rubik/stat/taker-volume", {"ccy": "BTC", "instType": "SPOT", "period": "1H"}),
    ("/api/v5/rubik/stat/contracts/open-interest-volume", {"ccy": "BTC", "period": "1D"}),
    ("/api/v5/rubik/stat/taker-volume-contract", {"ccy": "BTC", "period": "1D"}),
    ("/api/v5/rubik/stat/margin/loan-ratio", {"ccy": "BTC", "period": "1D"}),
]
for path, p in cases:
    print("%-58s %s" % (path + " " + json.dumps(p, ensure_ascii=False)[:44],
                        try_url(path, p)))
    time.sleep(0.2)

print()
print("=" * 88)
print("重点：taker-volume（主动买/卖量） —— 若可用即为官方 CVD 代理，直接可回测")
print("-" * 88)
for period in ["1D", "1H", "5m"]:
    for inst in ["SPOT", "SWAP"]:
        p = {"ccy": "BTC", "instType": inst, "period": period}
        url = "https://www.okx.com/api/v5/rubik/stat/taker-volume?" + urllib.parse.urlencode(p)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            js = json.loads(_op.open(req, timeout=25).read().decode())
            d = js.get("data") or []
            if d:
                print("  %-5s %-5s OK n=%-5d 最新=%s  最老=%s"
                      % (period, inst, len(d), d[0], d[-1]))
            else:
                print("  %-5s %-5s code=%s 空 msg=%s" % (period, inst, js.get("code"), js.get("msg")))
        except Exception as e:
            print("  %-5s %-5s ERR %s" % (period, inst, str(e)[:70]))
        time.sleep(0.2)
