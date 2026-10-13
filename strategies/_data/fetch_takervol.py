# -*- coding: utf-8 -*-
"""
拉取 OKX 官方 taker-volume（主动买量 / 主动卖量）—— 真·逐笔聚合的 CVD 数据源。
端点: /api/v5/rubik/stat/taker-volume?ccy=BTC&instType=SPOT&period=1D
返回: [ts, takerBuyVol, takerSellVol]  单位: 币(BTC)
深度: 1D=180 行, 1H=719 行, 5m=576 行（无 4H/15m）
落盘: _data/takervol_BTC_<period>.parquet
"""
import sys, json, time, urllib.parse, urllib.request
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROXY = "http://127.0.0.1:7890"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
OUTDIR = r"D:\量化\strategies\_data"
_op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ms) / 1000.0))


def get(period, ccy="BTC", instType="SPOT", tries=4):
    url = ("https://www.okx.com/api/v5/rubik/stat/taker-volume?"
           + urllib.parse.urlencode({"ccy": ccy, "instType": instType, "period": period}))
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            js = json.loads(_op.open(req, timeout=25).read().decode())
            if js.get("code") != "0":
                raise RuntimeError("code=%s msg=%s" % (js.get("code"), js.get("msg")))
            return js.get("data") or []
        except Exception as e:
            last = e
            time.sleep(0.7 * (i + 1))
    raise last


def main():
    summary = []
    for period in ["1D", "1H", "5m"]:
        try:
            d = get(period)
        except Exception as e:
            print("period=%s FAIL %s" % (period, e))
            summary.append({"period": period, "ok": False, "err": str(e)[:120]})
            continue
        if not d:
            print("period=%s 空" % period)
            continue
        df = pd.DataFrame(d, columns=["ts", "takerBuyVol", "takerSellVol"])
        df["ts"] = df["ts"].astype("int64")
        df["takerBuyVol"] = df["takerBuyVol"].astype(float)
        df["takerSellVol"] = df["takerSellVol"].astype(float)
        df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
        df["delta"] = df["takerBuyVol"] - df["takerSellVol"]
        df["cvd"] = df["delta"].cumsum()
        df["totalVol"] = df["takerBuyVol"] + df["takerSellVol"]
        df["dt_utc"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df["dt_bj"] = df["dt_utc"].dt.tz_convert("Asia/Shanghai")
        pq = "%s\\takervol_BTC_%s.parquet" % (OUTDIR, period)
        df.to_parquet(pq, index=False)
        w = {"1D": 86400000, "1H": 3600000, "5m": 300000}[period]
        gaps = int((df["ts"].diff().dropna() != w).sum())
        print("period=%-4s rows=%-5d %s ~ %s  非标准间隔=%d  -> %s"
              % (period, len(df), fmt(df["ts"].min()), fmt(df["ts"].max()), gaps, pq))
        summary.append({"period": period, "ok": True, "rows": len(df),
                        "oldest": fmt(df["ts"].min()), "newest": fmt(df["ts"].max()),
                        "irregular_gaps": gaps, "path": pq})
    with open("%s\\takervol_meta.json" % OUTDIR, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("meta -> %s\\takervol_meta.json" % OUTDIR)


if __name__ == "__main__":
    main()
