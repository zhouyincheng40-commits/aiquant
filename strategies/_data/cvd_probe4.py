# -*- coding: utf-8 -*-
"""
探测 taker-volume 各 period 的真实深度上限（这决定 CVD 策略能否做样本外）。
period: 5m / 1H / 1D 已知；再试 4H / 15m / 1m。
返回 [ts, takerBuyVol, takerSellVol]（单位按币计）。
"""
import sys, json, time, urllib.parse, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROXY = "http://127.0.0.1:7890"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ms) / 1000.0))


def q(period, ccy="BTC", instType="SPOT"):
    url = ("https://www.okx.com/api/v5/rubik/stat/taker-volume?"
           + urllib.parse.urlencode({"ccy": ccy, "instType": instType, "period": period}))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return json.loads(_op.open(req, timeout=25).read().decode())


print("=" * 92)
print("taker-volume 各 period 深度（SPOT）")
print("-" * 92)
print("%-6s %-8s %-22s %-22s %-9s %s" % ("period", "rows", "最老", "最新", "跨度天", "样例行"))
for period in ["1m", "5m", "15m", "30m", "1H", "4H", "1D"]:
    try:
        js = q(period)
        d = js.get("data") or []
        if not d:
            print("%-6s EMPTY code=%s msg=%s" % (period, js.get("code"), js.get("msg")))
            continue
        tss = [int(r[0]) for r in d]
        span = (max(tss) - min(tss)) / 86400000.0
        print("%-6s %-8d %-22s %-22s %-9.2f %s"
              % (period, len(d), fmt(min(tss)), fmt(max(tss)), span, d[-1]))
    except Exception as e:
        print("%-6s ERR %s %s" % (period, type(e).__name__, str(e)[:70]))
    time.sleep(0.25)

print()
print("=" * 92)
print("分页：能否用 after/before 往回翻更久？")
d0 = (q("4H").get("data") or [])
if d0:
    oldest = min(int(r[0]) for r in d0)
    for label, extra in [("after=最老ts", {"after": str(oldest)}),
                         ("before=最新ts", {"before": str(max(int(r[0]) for r in d0))})]:
        url = ("https://www.okx.com/api/v5/rubik/stat/taker-volume?"
               + urllib.parse.urlencode({"ccy": "BTC", "instType": "SPOT",
                                         "period": "4H", **extra}))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            js = json.loads(_op.open(req, timeout=25).read().decode())
            dd = js.get("data") or []
            if dd:
                tss = [int(r[0]) for r in dd]
                print("  %-16s n=%-5d %s ~ %s" % (label, len(dd), fmt(min(tss)), fmt(max(tss))))
            else:
                print("  %-16s 空 code=%s msg=%s" % (label, js.get("code"), js.get("msg")))
        except Exception as e:
            print("  %-16s ERR %s" % (label, str(e)[:70]))
        time.sleep(0.25)
