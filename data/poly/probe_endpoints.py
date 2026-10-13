# -*- coding: utf-8 -*-
"""
Polymarket 公开 API 端点契约实测探针
- 每个端点真调用，记录 status / 耗时ms / 字节数 / CORS 头 / 字段键 / 样例值
- 不猜测，失败记录报错原文
输出: D:\量化\data\poly\raw\probe_result.json
"""
import json, time, os, sys, traceback
import requests

OUT = r"D:\量化\data\poly\raw"
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) poly-contract-probe/1.0"}
TIMEOUT = 30

results = []


def probe(name, url, note="", sample_keys=None, capture_cors=True, method="GET", extra_headers=None):
    rec = {"name": name, "url": url, "note": note, "ok": False}
    t0 = time.time()
    try:
        h = dict(UA)
        if extra_headers:
            h.update(extra_headers)
        r = requests.request(method, url, headers=h, timeout=TIMEOUT, allow_redirects=True)
        ms = int((time.time() - t0) * 1000)
        raw = r.content
        rec.update({
            "status": r.status_code,
            "ms": ms,
            "bytes": len(raw),
            "content_type": r.headers.get("Content-Type"),
            "ok": r.status_code == 200,
        })
        if capture_cors:
            cors = {k: v for k, v in r.headers.items()
                    if k.lower().startswith("access-control") or k.lower() in ("vary", "server", "cf-cache-status")}
            rec["cors_headers"] = cors
            rec["cors_acao"] = r.headers.get("Access-Control-Allow-Origin")
        # parse
        try:
            j = r.json()
            rec["json_root_type"] = type(j).__name__
            if isinstance(j, list):
                rec["count"] = len(j)
                if j and isinstance(j[0], dict):
                    rec["item_keys"] = sorted(j[0].keys())
                    rec["sample"] = j[0]
                elif j:
                    rec["sample_first_element"] = j[:2]
            elif isinstance(j, dict):
                rec["top_keys"] = sorted(j.keys())[:50]
                rec["sample"] = {k: j[k] for k in list(j.keys())[:40]}
            rec["body_head"] = r.text[:400]
        except Exception as e:
            rec["json_parse_error"] = repr(e)[:300]
            rec["body_head"] = r.text[:400]
    except Exception as e:
        rec.update({"ms": int((time.time() - t0) * 1000), "error": repr(e)[:400],
                    "trace": traceback.format_exc()[-600:]})
    results.append(rec)
    flag = "OK " if rec.get("ok") else "FAIL"
    print(f"[{flag}] {name:42s} status={rec.get('status')} {rec.get('ms')}ms {rec.get('bytes')}B ACAO={rec.get('cors_acao')}")
    return rec


# ---------------- 1. Gamma API ----------------
probe("gamma.markets.closed_false", "https://gamma-api.polymarket.com/markets?closed=false&limit=3",
      note="基础市场列表")
probe("gamma.markets.order_volume24hr", "https://gamma-api.polymarket.com/markets?closed=false&limit=5&order=volume24hr&ascending=false",
      note="按24h成交量降序")
probe("gamma.markets.order_liquidity", "https://gamma-api.polymarket.com/markets?closed=false&limit=5&order=liquidity&ascending=false",
      note="按流动性降序")
probe("gamma.markets.order_volume", "https://gamma-api.polymarket.com/markets?closed=false&limit=5&order=volume&ascending=false",
      note="按总成交量降序")
probe("gamma.markets.pagination_offset", "https://gamma-api.polymarket.com/markets?closed=false&limit=5&offset=10",
      note="分页 offset")
probe("gamma.markets.by_condition_id", "https://gamma-api.polymarket.com/markets?condition_ids=0x0e2b6a2a13d0e6e0b44e8d6b1e0b0e1b00000000000000000000000000000000&limit=2",
      note="按 condition_ids 过滤(故意用假id,测是否报错还是空)")
probe("gamma.events.closed_false", "https://gamma-api.polymarket.com/events?closed=false&limit=2",
      note="事件列表")
probe("gamma.events.order_volume24hr", "https://gamma-api.polymarket.com/events?closed=false&limit=3&order=volume24hr&ascending=false",
      note="事件按24h量")
probe("gamma.events.by_slug", "https://gamma-api.polymarket.com/events?slug=presidential-election-winner-2024",
      note="按 slug 查事件")
probe("gamma.markets.single_by_id", "https://gamma-api.polymarket.com/markets/1",
      note="单市场 by id")
probe("gamma.tags", "https://gamma-api.polymarket.com/tags?limit=5", note="标签端点")
probe("gamma.prices_history", "https://gamma-api.polymarket.com/prices-history?market=1&interval=1d&fidelity=60",
      note="gamma 价格历史(需 clob token id)")

# ---------------- 2. CLOB API ----------------
probe("clob.markets", "https://clob.polymarket.com/markets?limit=2", note="clob 市场(响应巨大)")
probe("clob.sampling_markets", "https://clob.polymarket.com/sampling-markets", note="clob 采样市场")
probe("clob.simplified_markets", "https://clob.polymarket.com/simplified-markets?limit=5", note="clob 精简市场")
probe("clob.midpoint", "https://clob.polymarket.com/midpoint?token_id=1", note="中间价(假 token 测报错)")
probe("clob.price", "https://clob.polymarket.com/price?token_id=1&side=buy", note="价格(假 token)")
probe("clob.book", "https://clob.polymarket.com/book?token_id=1", note="订单簿(假 token)")
probe("clob.spread", "https://clob.polymarket.com/spread?token_id=1", note="价差(假 token)")
probe("clob.prices", "https://clob.polymarket.com/prices", note="POST 端点,GET 测方法")
probe("clob.prices_post", "https://clob.polymarket.com/prices",
      note="POST 批量价格, body=[{token_id,side}]", method="POST")
probe("clob.time", "https://clob.polymarket.com/time", note="服务器时间")
probe("clob.ok", "https://clob.polymarket.com/", note="根路径")

# ---------------- 3. Leaderboard ----------------
for w in ["all", "1m", "1w"]:
    probe(f"lb.leaderboard.{w}.pnl", f"https://lb-api.polymarket.com/leaderboard?window={w}&limit=5&orderBy=pnl",
          note=f"排行榜 window={w} orderBy=pnl")
probe("lb.leaderboard.all.volume", "https://lb-api.polymarket.com/leaderboard?window=all&limit=5&orderBy=volume",
      note="排行榜 orderBy=volume")
probe("lb.leaderboard.no_orderby", "https://lb-api.polymarket.com/leaderboard?window=all&limit=5",
      note="排行榜 无 orderBy")
probe("lb.profile", "https://lb-api.polymarket.com/profile?address=0x0000000000000000000000000000000000000000",
      note="钱包画像(假地址)")
probe("lb.rank", "https://lb-api.polymarket.com/rank?address=0x0000000000000000000000000000000000000000",
      note="排名查询(假地址)")

# ---------------- 4. Data API ----------------
probe("data.positions", "https://data-api.polymarket.com/positions?user=0x0000000000000000000000000000000000000000&limit=5",
      note="持仓(假地址)")
probe("data.trades", "https://data-api.polymarket.com/trades?limit=5", note="成交流")
probe("data.holders", "https://data-api.polymarket.com/holders?limit=5", note="持有人")
probe("data.activity", "https://data-api.polymarket.com/activity?limit=5", note="活动流")
probe("data.value", "https://data-api.polymarket.com/value?user=0x0000000000000000000000000000000000000000",
      note="组合市值(假地址)")
probe("data.oi", "https://data-api.polymarket.com/oi?limit=5", note="未平仓量")

# ---------------- 5. CORS preflight 实测 ----------------
results_cors = []
for u in ["https://gamma-api.polymarket.com/markets?closed=false&limit=1",
          "https://clob.polymarket.com/simplified-markets?limit=1",
          "https://lb-api.polymarket.com/leaderboard?window=all&limit=1&orderBy=pnl",
          "https://data-api.polymarket.com/trades?limit=1"]:
    rec = {"url": u, "origin": "http://127.0.0.1:19387"}
    try:
        r = requests.get(u, headers={**UA, "Origin": "http://127.0.0.1:19387",
                                     "Access-Control-Request-Method": "GET"}, timeout=TIMEOUT)
        rec["status"] = r.status_code
        rec["acao"] = r.headers.get("Access-Control-Allow-Origin")
        rec["acac"] = r.headers.get("Access-Control-Allow-Credentials")
        rec["acam"] = r.headers.get("Access-Control-Allow-Methods")
        rec["acah"] = r.headers.get("Access-Control-Allow-Headers")
        rec["all_headers"] = dict(r.headers)
    except Exception as e:
        rec["error"] = repr(e)[:300]
    results_cors.append(rec)
    print(f"[CORS] {u[:60]:60s} ACAO={rec.get('acao')}")

payload = {"endpoints": results, "cors_probe": results_cors,
           "probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "probed_at_bjt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}
with open(os.path.join(OUT, "probe_result.json"), "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
print("\nSAVED ->", os.path.join(OUT, "probe_result.json"))
ok = sum(1 for r in results if r.get("ok"))
print(f"SUMMARY: {ok}/{len(results)} endpoints OK")
