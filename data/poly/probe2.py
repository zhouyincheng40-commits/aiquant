# -*- coding: utf-8 -*-
"""
Polymarket 公开 API 契约实测 — 第二轮
修正项（来自 Lead 实测情报）:
 1. 主通道改 /markets/keyset，/markets 为弃用兜底
 2. 显式代理 (poly-ui 复现: 直连 20s 超时 err=28)
 3. 不保证 ACAO -> 自己带 Origin 实测
 4. 编码: 用 r.json()，不做 latin1 转换
 5. 分页 next_cursor 独立复核
 6. 字段级缺失率
输出: D:\量化\data\poly\raw\probe2_result.json
"""
import json, os, sys, time, traceback
import requests

OUT = r"D:\量化\data\poly\raw"
os.makedirs(OUT, exist_ok=True)
PROXY = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) poly-contract-probe/2.0",
      "Accept": "application/json"}
TIMEOUT = 12
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
LB = "https://lb-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

def log(*a):
    print(*a, flush=True)

def call(name, url, method="GET", json_body=None, use_proxy=True, origin=None,
         timeout=TIMEOUT, save=None, note=""):
    rec = {"name": name, "method": method, "url": url, "note": note,
           "proxy": PROXY["https"] if use_proxy else None}
    h = dict(UA)
    if origin:
        h["Origin"] = origin
        h["Access-Control-Request-Method"] = "GET"
    t0 = time.time()
    try:
        r = requests.request(method, url, headers=h, json=json_body, timeout=timeout,
                             proxies=PROXY if use_proxy else {"http": None, "https": None})
        rec.update(status=r.status_code, ms=int((time.time()-t0)*1000), bytes=len(r.content),
                   hdr_deprecation=r.headers.get("deprecation"),
                   hdr_sunset=r.headers.get("sunset"),
                   hdr_warning=r.headers.get("warning"),
                   hdr_cache_control=r.headers.get("Cache-Control"),
                   acao=r.headers.get("Access-Control-Allow-Origin"),
                   acac=r.headers.get("Access-Control-Allow-Credentials"),
                   vary=r.headers.get("vary"),
                   content_type=r.headers.get("Content-Type"))
        try:
            j = r.json()
        except Exception as e:
            rec["json_error"] = repr(e)[:200]; rec["body_head"] = r.text[:300]; j = None
        if j is not None:
            rec["root_type"] = type(j).__name__
            if isinstance(j, dict):
                rec["top_keys"] = sorted(j.keys())
                m = j.get("markets")
                if isinstance(m, list):
                    rec["markets_len"] = len(m)
                    rec["next_cursor"] = j.get("next_cursor")
                    if m:
                        rec["item_keys"] = sorted(m[0].keys())
                        rec["sample"] = m[0]
                else:
                    rec["sample"] = {k: (j[k] if not isinstance(j[k], (list, dict)) else
                                         (j[k][:3] if isinstance(j[k], list) else "dict")) for k in list(j)[:25]}
            elif isinstance(j, list):
                rec["list_len"] = len(j)
                if j and isinstance(j[0], dict):
                    rec["item_keys"] = sorted(j[0].keys()); rec["sample"] = j[0]
                elif j:
                    rec["sample"] = j[:5]
        if save:
            with open(os.path.join(OUT, save), "w", encoding="utf-8") as f:
                f.write(r.text)
            rec["saved_raw"] = os.path.join(OUT, save)
    except Exception as e:
        rec.update(ms=int((time.time()-t0)*1000), error=repr(e)[:300])
    results.append(rec)
    st = rec.get("status") or "ERR"
    log(f"[{st}] {name:44s} {rec.get('ms')}ms {rec.get('bytes','-')}B ACAO={rec.get('acao')} dep={rec.get('hdr_deprecation')} err={rec.get('error','')[:60]}")
    return rec

results = []

# ============ A. 代理对比：直连 vs 代理 ============
log("=" * 78)
log("A. 代理可达性对比  (直连记录报错原文)")
log("=" * 78)
call("PROXY_OFF.gamma.keyset", f"{GAMMA}/markets/keyset?closed=false&limit=1", use_proxy=False,
     note="关闭代理直连 — 用于验证 poly-ui 的 20s超时/err=28 结论")
call("PROXY_ON.gamma.keyset", f"{GAMMA}/markets/keyset?closed=false&limit=1", use_proxy=True,
     note="显式代理 127.0.0.1:7890")

# ============ B. keyset 参数发现 ============
log("=" * 78)
log("B. /markets/keyset 参数发现")
log("=" * 78)
call("keyset.base", f"{GAMMA}/markets/keyset?closed=false&limit=3", save="keyset_base.json")
call("keyset.order_volume24hr", f"{GAMMA}/markets/keyset?closed=false&limit=3&order=volume24hr&ascending=false")
call("keyset.order_liquidity", f"{GAMMA}/markets/keyset?closed=false&limit=3&order=liquidity&ascending=false")
call("keyset.limit60", f"{GAMMA}/markets/keyset?closed=false&limit=60&order=volume24hr&ascending=false",
     save="keyset_page1_60.json", note="第一页 60 条 — (a)口径的扫描范围")
call("keyset.limit200", f"{GAMMA}/markets/keyset?closed=false&limit=200&order=volume24hr&ascending=false",
     note="测试 limit 上限是否被截断")
call("keyset.no_closed_param", f"{GAMMA}/markets/keyset?limit=3")
call("keyset.active_param", f"{GAMMA}/markets/keyset?active=true&archived=false&limit=3")
call("keyset.offset", f"{GAMMA}/markets/keyset?closed=false&limit=60&offset=60&order=volume24hr&ascending=false",
     note="offset 是否生效(poly-ui 实测 0 条)")
call("keyset.cursor_param", f"{GAMMA}/markets/keyset?closed=false&limit=60&cursor=MA==", note="cursor 参数")
call("keyset.next_cursor_param", f"{GAMMA}/markets/keyset?closed=false&limit=60&next_cursor=MA==", note="next_cursor 参数")
call("keyset.after_param", f"{GAMMA}/markets/keyset?closed=false&limit=60&after=MA==", note="after 参数")
call("keyset.page_param", f"{GAMMA}/markets/keyset?closed=false&limit=60&page=2", note="page 参数")
call("keyset.liquidity_min", f"{GAMMA}/markets/keyset?closed=false&limit=3&liquidity_num_min=10000")
call("keyset.volume_min", f"{GAMMA}/markets/keyset?closed=false&limit=3&volume_num_min=100000")

# ============ C. 弃用端点 ============
log("=" * 78)
log("C. 弃用 /markets 兜底端点 (响应头 deprecation/sunset/warning)")
log("=" * 78)
call("markets.deprecated", f"{GAMMA}/markets?closed=false&limit=3", note="弃用兜底")

# ============ D. CORS 实测 (Origin: 127.0.0.1:8848) ============
log("=" * 78)
log("D. CORS 实测 — 带 Origin, 以及 OPTIONS 预检")
log("=" * 78)
ORIGIN = "http://127.0.0.1:8848"
for nm, u in [("cors.gamma", f"{GAMMA}/markets/keyset?closed=false&limit=1"),
              ("cors.clob", f"{CLOB}/simplified-markets?limit=1"),
              ("cors.lb.profit", f"{LB}/profit?window=all&limit=1"),
              ("cors.data.trades", f"{DATA}/trades?limit=1"),
              ("cors.data.positions", f"{DATA}/positions?user=0x0000000000000000000000000000000000000000&limit=1")]:
    call(nm, u, origin=ORIGIN, note=f"GET with Origin: {ORIGIN}")
    call(nm + ".OPTIONS", u, method="OPTIONS", origin=ORIGIN, note="CORS 预检")

# ============ E. 排行榜 / 钱包 ============
log("=" * 78)
log("E. 排行榜 lb-api 与 data-api")
log("=" * 78)
call("lb.profit.all", f"{LB}/profit?window=all&limit=10", save="lb_profit_all.json")
call("lb.profit.1d", f"{LB}/profit?window=1d&limit=10", save="lb_profit_1d.json")
call("lb.profit.1w", f"{LB}/profit?window=1w&limit=5")
call("lb.profit.1m", f"{LB}/profit?window=1m&limit=5")
call("lb.volume.all", f"{LB}/volume?window=all&limit=10", save="lb_volume_all.json")
call("lb.leaderboard.404check", f"{LB}/leaderboard?window=all&limit=5", note="Lead 报 404 — 复核")
call("lb.pnl.404check", f"{LB}/pnl?window=all&limit=5", note="Lead 报 404 — 复核")
call("data.trades", f"{DATA}/trades?limit=10", save="data_trades.json")
call("data.positions.zero", f"{DATA}/positions?user=0x0000000000000000000000000000000000000000&limit=5")
call("data.activity", f"{DATA}/activity?limit=5")
call("data.holders", f"{DATA}/holders?limit=5")
call("data.value.zero", f"{DATA}/value?user=0x0000000000000000000000000000000000000000")

# ============ F. CLOB 价格端点 (用真实 token) ============
log("=" * 78)
log("F. CLOB 价格端点 — 用刚取到的真实 clobTokenIds")
log("=" * 78)
p1 = os.path.join(OUT, "keyset_page1_60.json")
real_token = None
if os.path.exists(p1):
    try:
        d = json.load(open(p1, encoding="utf-8"))
        for m in d.get("markets", []):
            ti = m.get("clobTokenIds")
            if isinstance(ti, str):
                ti = json.loads(ti)
            if isinstance(ti, list) and ti:
                real_token = ti[0]; condition_id = m.get("conditionId")
                log(f"    使用真实 token_id={real_token[:22]}... cond={str(condition_id)[:18]}...")
                break
    except Exception as e:
        log("    解析 token 失败:", repr(e))
if real_token:
    call("clob.midpoint.real", f"{CLOB}/midpoint?token_id={real_token}")
    call("clob.price.buy.real", f"{CLOB}/price?token_id={real_token}&side=buy")
    call("clob.price.sell.real", f"{CLOB}/price?token_id={real_token}&side=sell")
    call("clob.spread.real", f"{CLOB}/spread?token_id={real_token}")
    call("clob.book.real", f"{CLOB}/book?token_id={real_token}", save="clob_book.json")
    call("clob.prices_post", f"{CLOB}/prices", method="POST",
         json_body=[{"token_id": real_token, "side": "BUY"}], note="POST 批量价格")
    call("clob.midpoints_post", f"{CLOB}/midpoints", method="POST",
         json_body=[{"token_id": real_token}])
else:
    log("    !! 没拿到真实 token_id, CLOB 价格端点无法实测")
call("clob.time", f"{CLOB}/time")
call("clob.simplified", f"{CLOB}/simplified-markets?limit=3")
call("clob.sampling", f"{CLOB}/sampling-markets")

# ============ G. gamma 其它 ============
log("=" * 78)
log("G. gamma events / tags / 历史价")
log("=" * 78)
call("gamma.events", f"{GAMMA}/events?closed=false&limit=2")
call("gamma.events.order", f"{GAMMA}/events?closed=false&limit=3&order=volume24hr&ascending=false")
call("gamma.tags", f"{GAMMA}/tags?limit=5")
if real_token:
    call("gamma.prices_history", f"{GAMMA}/prices-history?market={real_token}&interval=1d&fidelity=60",
         note="gamma 价格历史 — PNL CURVE 可用性")

with open(os.path.join(OUT, "probe2_result.json"), "w", encoding="utf-8") as f:
    json.dump({"endpoints": results, "origin_used": "http://127.0.0.1:8848",
               "proxy": PROXY,
               "probed_at_bjt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
               "probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
              f, ensure_ascii=False, indent=2)

log("\n" + "=" * 78)
ok = [r for r in results if r.get("status") == 200]
log(f"SUMMARY: {len(ok)}/{len(results)} returned 200")
log("SAVED -> " + os.path.join(OUT, "probe2_result.json"))
