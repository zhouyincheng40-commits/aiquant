# -*- coding: utf-8 -*-
"""
复核 v2：after=tradeId 连续分页 20 页，实测：
  - 能否一直往回翻（有没有滚动窗口墙）
  - 每页覆盖多少秒 / 多少笔
  - 反推覆盖目标区间需要多少请求 + 多少墙钟时间
只读，不改任何生产文件。
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
            return json.loads(_op.open(req, timeout=25).read().decode())
        except Exception as e:
            last = e
            time.sleep(0.7 * (i + 1))
    raise last


def fmt(ms):
    return time.strftime("%m-%d %H:%M:%S", time.gmtime(int(ms) / 1000.0))


print("=" * 92)
print("连续分页实测：after = 上一页最老 tradeId（side 抽样保留原始 side 字段）")
print("-" * 92)
print("%-5s %-6s %-20s %-20s %-9s %-9s" % ("page", "count", "newest", "oldest", "覆盖秒", "累计秒"))
all_ids = set()
anchor = None
t_start = time.time()
cum_span = 0.0
pages_done = 0
for page in range(1, 21):
    p = {"instId": "BTC-USDT", "limit": "100"}
    if anchor:
        p["after"] = anchor
    try:
        js = get("/api/v5/market/history-trades", p)
    except Exception as e:
        print("page %d FAIL %s" % (page, str(e)[:80]))
        break
    d = js.get("data") or []
    if not d:
        print("page %d 返回空 -> 撞墙" % page)
        break
    ids = [x["tradeId"] for x in d]
    new = [i for i in ids if i not in all_ids]
    ts = [int(x["ts"]) for x in d]
    span = (max(ts) - min(ts)) / 1000.0
    pages_done = page
    if page == 1:
        cum_span = span
    else:
        cum_span += (prev_oldest - min(ts)) / 1000.0
    prev_oldest = min(ts)
    print("%-5d %-6d %-20s %-20s %-9.1f %-9.1f  new=%d"
          % (page, len(d), fmt(max(ts)), fmt(min(ts)), span, cum_span, len(new)))
    if not new:
        print("  !! 整页重复，分页失效")
        break
    all_ids.update(ids)
    anchor = min(ids, key=lambda x: 0) if False else ids[-1]   # 数据按时间倒序，最后一条最老
    anchor_ts = min(ts)
    anchor = [x for x in d if int(x["ts"]) == anchor_ts][0]["tradeId"]
    time.sleep(SLEEP)

wall = time.time() - t_start
print("-" * 92)
print("完成 %d 页, 累计唯一成交 %d 笔, 累计覆盖 %.1f 秒 (%.2f 小时)"
      % (pages_done, len(all_ids), cum_span, cum_span / 3600.0))
print("墙钟耗时 %.1f 秒 -> 每页 %.2f 秒" % (wall, wall / max(pages_done, 1)))
if cum_span > 0 and pages_done > 2:
    tps = len(all_ids) / cum_span          # 成交笔/秒
    print("实测成交密度: %.3f 笔/秒 = %.0f 笔/小时 = %.0f 笔/天" % (tps, tps * 3600, tps * 86400))
    for target_days, label in [(30, "30 天"), (730, "2 年(4H 回测)")]:
        need_trades = tps * target_days * 86400
        need_req = need_trades / 100.0     # 每请求 100 笔（未按 side 过滤）
        print("  %-14s 需 %10.0f 笔 -> %8.0f 次请求 -> 按实测 %.2fs/页 约 %6.1f 小时"
              % (label, need_trades, need_req, wall / max(pages_done, 1),
                 need_req * (wall / max(pages_done, 1)) / 3600.0))
