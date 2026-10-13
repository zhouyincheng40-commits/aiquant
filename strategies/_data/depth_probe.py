# -*- coding: utf-8 -*-
"""
OKX 历史 K 线真实可回溯深度探测（只读，不落 parquet）。
翻页直到：返回空 / 不再前进 / 达到 max_pages。
任何"返回空"都记录为"疑似截断"，不当作"已到最早"。
"""
import sys, json, time, urllib.request, urllib.parse
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROXY = "http://127.0.0.1:7890"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE = "https://www.okx.com/api/v5/market/history-candles"

_op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

BAR_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
          "1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}
LIMIT = 300          # OKX 硬上限
SLEEP = 0.15


def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            return json.loads(_op.open(req, timeout=25).read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def probe(inst="BTC-USDT", bar="1H", max_pages=200):
    """往回翻页，返回 (最老ts, 页数, 总根数, 停止原因)。"""
    after = None
    oldest = None
    pages = 0
    total = 0
    reason = "max_pages"
    seen_ts = set()
    while pages < max_pages:
        q = {"instId": inst, "bar": bar, "limit": str(LIMIT)}
        if after is not None:
            q["after"] = str(after)
        url = BASE + "?" + urllib.parse.urlencode(q)
        try:
            js = get(url)
        except Exception as e:
            reason = "error:%s" % type(e).__name__
            break
        data = js.get("data") or []
        if not data:
            reason = "empty_return(疑似截断)"
            break
        ts_list = [int(r[0]) for r in data]
        new_ts = [t for t in ts_list if t not in seen_ts]
        if not new_ts:
            reason = "no_progress(重复页，疑似截断)"
            break
        seen_ts.update(ts_list)
        total += len(new_ts)
        pages += 1
        cur_oldest = min(new_ts)
        if oldest is not None and cur_oldest >= oldest:
            reason = "no_progress(不再变老)"
            break
        oldest = cur_oldest
        after = cur_oldest
        time.sleep(SLEEP)
    return oldest, pages, total, reason


def fmt(ms):
    if ms is None:
        return "N/A"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000.0))


def main():
    now = int(time.time() * 1000)
    print("探测时间(UTC): %s" % fmt(now))
    print("=" * 100)
    print("%-5s %-8s %-22s %-8s %-8s %s" % ("bar", "pages", "最老一根(UTC)", "根数", "跨度天", "停止原因"))
    print("-" * 100)
    rows = []
    # 按 bar 大小给不同 max_pages：大的少翻，小的多翻
    mp = {"1D": 60, "4H": 120, "1H": 200, "15m": 200, "5m": 120, "1m": 60}
    for bar in ["1D", "4H", "1H", "15m", "5m", "1m"]:
        try:
            oldest, pages, total, reason = probe(bar=bar, max_pages=mp[bar])
        except Exception as e:
            print("%-5s ERROR %s" % (bar, e))
            rows.append((bar, None, 0, 0, "error"))
            continue
        span = (now - oldest) / 86400000.0 if oldest else 0
        print("%-5s %-8d %-22s %-8d %-8.2f %s"
              % (bar, pages, fmt(oldest), total, span, reason))
        rows.append((bar, oldest, total, span, reason))
        sys.stdout.flush()

    print("=" * 100)
    # 精简保存
    out = [{"bar": r[0], "oldest_ms": r[1], "oldest_utc": fmt(r[1]),
            "bars": r[2], "span_days": round(r[3], 3), "stop_reason": r[4]}
           for r in rows]
    with open(r"D:\量化\strategies\_data\depth_probe.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved -> D:\\量化\\strategies\\_data\\depth_probe.json")


if __name__ == "__main__":
    main()
