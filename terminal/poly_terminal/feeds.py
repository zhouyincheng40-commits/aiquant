"""feeds.py — POLY // TERMINAL 数据层（纯数据，无渲染副作用）

单一入口：snapshot(force=False) -> dict
    {header, scanner, wallets, markets, copy, pnl, leaderboard, cnBand} + meta/health/ts/ok/proxyDown/errors

口径（改动前必读）
------------------
1. MISPRICING SCANNER =「定义性套利」，且**只对 negRisk=true 的互斥穷尽集合**计算。
   非互斥事件（如 "ceasefire through <日期A/B/C>"）Σ 价格天然 >> 1，不是套利。
   实测：真互斥集合 Σ bestBid ≈ 0.99–1.02；非互斥事件 Σ bestBid = 3.91。
   不加这道门槛会产出 +286% 这种**假套利**。
2. gamma /markets/keyset 的**分页游标无效**（cursor/next_cursor/after/page 全返同一页、
   offset 返 0 条）→ 扫描范围恒为「volume24hr 降序第一页」，meta.scanRange 自曝。
3. outcomes / outcomePrices 是 JSON **字符串**，必须二次 parse。
4. oneDayPriceChange 仅约 23/40 市场存在 → 降级 oneHourPriceChange → None（不补假值）。
5. 编码：Polymarket 是合法 UTF-8（**不做任何转换**，`Inácio` 不要"修"）；
   腾讯是 GBK（**必须** content.decode('gbk')）。
6. 代理：polymarket 三域名必须走 127.0.0.1:7890；腾讯行情直连（绕开代理）。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

# ── 配置 ─────────────────────────────────────────────────────────────────────
PROXY = "http://127.0.0.1:7890"
GAMMA = "https://gamma-api.polymarket.com"
LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
BAND_URL = "https://qt.gtimg.cn/q=s_sh000001,s_sz399001,s_sz399006,s_bj899050,s_hkHSI"
BAND_CODES = ["s_sh000001", "s_sz399001", "s_sz399006", "s_bj899050", "s_hkHSI"]

CONFIG = {
    # 取数间隔（按端点真实新鲜度分频；positions 是唯一 max-age=5 的真实时通道）
    "ttl_markets_s": 300,    # gamma /markets/keyset  Cache-Control max-age=300
    "ttl_trades_s": 300,     # data-api /trades       max-age=300
    "ttl_positions_s": 60,   # data-api /positions    max-age=5  ← 真正实时
    "ttl_lb_s": 600,         # lb-api /profit         max-age=600（1d 窗口 300）
    "ttl_lb1d_s": 300,
    "band_refresh_s": 60,    # CN 带（腾讯直连，独立于代理）
    "refresh_s": 5,          # 渲染层调用 snapshot() 的节奏；实际取数由上面 TTL 决定
    "timeout_s": 25,
    "gamma_limit": 100,      # keyset 硬上限就是 100（limit=200 也只给 100；offset 会 422）
    "tracked_n": 7,
    "copy_wallets": 5,
    "scan_event_max": 14,
    "scan_limit": 12,
    "sample_max": 120,
    "fee_rate_default": 0.05,   # 市场缺 feeSchedule.rate 时的最保守值
    "edge_feeds": ["edges.json", "data/poly/edges.json"],   # 可选热插 feed
}


# ── 纯工具 ───────────────────────────────────────────────────────────────────
def num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None      # 过滤 NaN/Inf


def jarr(v: Any) -> list:
    """outcomes / outcomePrices 是 JSON 字符串，必须二次 parse。"""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            p = json.loads(v)
            return p if isinstance(p, list) else []
        except (ValueError, TypeError):
            return []
    return []


def compact(n: Optional[float]) -> str:
    if n is None:
        return "--"
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1e9:
        s = f"{a/1e9:.1f}B"
    elif a >= 1e6:
        s = f"{a/1e6:.1f}M"
    elif a >= 1e3:
        s = f"{a/1e3:.1f}k"
    else:
        s = f"{a:.2f}" if a < 10 else f"{a:.0f}"
    return f"{sign}${s}"


def money(n: Optional[float], dp: Optional[int] = None) -> str:
    if n is None:
        return "--"
    sign = "-" if n < 0 else ""
    a = abs(n)
    d = dp if dp is not None else (2 if (a < 100 and a % 1 != 0) else 0)
    return f"{sign}${a:,.{d}f}"


def cents(p: Optional[float]) -> str:
    n = num(p)
    return "--" if n is None else f"{round(n * 1000) / 10:g}\u00a2"


def signed(v: Optional[float], fn) -> str:
    return "--" if v is None else ("+" if v > 0 else "") + fn(abs(v))


def short_addr(a: Any) -> str:
    if not a or not isinstance(a, str):
        return "--"
    return a[:6] + "..." + a[-4:] if len(a) > 12 else a


def hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def tick_floor(v: float, tick: Optional[float]) -> float:
    if not tick or tick <= 0:
        return v
    return int(v / tick + 1e-9) * tick


# ── 数据层 ───────────────────────────────────────────────────────────────────
class PolyFeeds:
    def __init__(self, proxy: str = PROXY, config: Optional[dict] = None):
        self.cfg = dict(CONFIG)
        if config:
            self.cfg.update(config)
        self.proxy = proxy
        # polymarket 走代理
        self.s_poly = requests.Session()
        self.s_poly.proxies = {"http": proxy, "https": proxy}
        self.s_poly.headers.update({"User-Agent": "poly-terminal/1.0"})
        # 腾讯直连：trust_env=False 防止继承环境变量里的代理
        self.s_direct = requests.Session()
        self.s_direct.trust_env = False
        self.s_direct.proxies = {}
        self.s_direct.headers.update({"User-Agent": "poly-terminal/1.0"})
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None
        self._src: Dict[str, dict] = {}          # 每个数据源自己的 TTL 缓存
        self._inflight = False
        self._pnl_samples: List[float] = []

    # ── 按源 TTL 缓存：positions 60s 真刷新，keyset/lb 各自按 max-age ──
    def _cached(self, name: str, ttl: float, loader, force: bool = False):
        rec = self._src.get(name)
        if not force and rec and (time.time() - rec["ts"]) < ttl:
            return rec["value"]
        val = loader()
        self._src[name] = {"ts": time.time(), "value": val}
        return val

    # ── HTTP ──
    def _get_json(self, url: str) -> Any:
        r = self.s_poly.get(url, timeout=self.cfg["timeout_s"])
        r.raise_for_status()
        return r.json()

    def _get_gbk(self, url: str) -> str:
        r = self.s_direct.get(url, timeout=self.cfg["timeout_s"])
        r.raise_for_status()
        return r.content.decode("gbk")          # 必须 gbk，默认 utf-8 会乱码

    @staticmethod
    def _settle(fn, *a, **kw) -> dict:
        try:
            return {"ok": True, "value": fn(*a, **kw)}
        except Exception as e:                   # noqa: BLE001 — 任何失败都降级，不抛给渲染层
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ── 市场 ──
    def load_markets(self, force: bool = False) -> List[dict]:
        # 与 scanner 共用同一个 keyset 缓存条目 → 每 300s 只打一次这个端点
        ms = self._cached("keyset", self.cfg["ttl_markets_s"], self._get_keyset_page, force)
        out = []
        for m in ms:
            q = m.get("question")
            if not q:
                continue
            prices = [num(x) for x in jarr(m.get("outcomePrices"))]
            outs = jarr(m.get("outcomes"))
            yes = prices[0] if prices else None
            no = prices[1] if len(prices) > 1 else None
            chg = num(m.get("oneDayPriceChange"))          # 降级链
            basis = "24H"
            if chg is None:
                chg = num(m.get("oneHourPriceChange"))
                basis = "1H"
            pct = None if chg is None else chg * 100
            direction = "flat" if pct is None else ("up" if pct > 0 else ("down" if pct < 0 else "flat"))
            arrow = "\u25b2" if direction == "up" else ("\u25bc" if direction == "down" else "")
            out.append({
                "id": m.get("id"), "question": q, "slug": m.get("slug"),
                "conditionId": m.get("conditionId"),
                "outcomes": outs, "yesPrice": yes, "noPrice": no,
                "volume24hr": num(m.get("volume24hr")), "volume": num(m.get("volume")),
                "liquidity": num(m.get("liquidity")),
                "bestBid": num(m.get("bestBid")), "bestAsk": num(m.get("bestAsk")),
                "lastTradePrice": num(m.get("lastTradePrice")), "spread": num(m.get("spread")),
                "chgPct": pct, "chgBasis": basis, "chgDir": direction,
                "meta1": "VOL24 " + compact(num(m.get("volume24hr"))),
                "meta2": "LIQ " + compact(num(m.get("liquidity"))),
                "yesText": cents(yes), "noText": cents(no),
                "chgText": "--" if pct is None else f"{arrow}{abs(pct):.1f}% {basis}",
            })
        return out

    # ── 榜单 ──
    def load_leaderboard(self, force: bool = False) -> List[dict]:
        allw = self._cached("lb_all", self.cfg["ttl_lb_s"],
                            lambda: self._get_json(f"{LB_API}/profit?window=all&limit=20"), force)
        # window 只认 all/1d/7d/30d（1w/1m/week/month 一律 400 invalid request）
        d1r = self._settle(self._cached, "lb_1d", self.cfg["ttl_lb1d_s"],
                           (lambda: self._get_json(f"{LB_API}/profit?window=1d&limit=20")), force)
        d1 = d1r["value"] if d1r["ok"] and isinstance(d1r["value"], list) else []
        m1 = {}
        for x in d1:
            if x.get("proxyWallet"):
                m1[str(x["proxyWallet"]).lower()] = num(x.get("amount"))
        rows = []
        for i, w in enumerate(allw if isinstance(allw, list) else []):
            addr = str(w.get("proxyWallet") or "")
            amt = num(w.get("amount"))
            p1 = m1.get(addr.lower())
            rows.append({
                "rank": i + 1, "address": addr,
                "label": w.get("pseudonym") or short_addr(addr),
                "pseudonym": w.get("pseudonym"),
                "amount": amt, "amount24h": p1,
                "has1d": p1 is not None, "pos1d": bool(p1 is not None and p1 > 0),
                "text": signed(amt, compact),
                "dir": None if amt is None else ("up" if amt >= 0 else "down"),
                "rightText": "--" if p1 is None else signed(p1, lambda v: compact(v) + " 24H"),
            })
        return rows

    def load_wallets(self, lb: List[dict]) -> List[dict]:
        rows = []
        for i, w in enumerate(lb[: self.cfg["tracked_n"]]):
            # WHALE/SCOUT 是本层基于真实排名的派生规则，不是接口字段
            kind = "WHALE" if i < 3 else "SCOUT"
            rows.append({
                "address": w["address"], "addressShort": short_addr(w["address"]),
                "kind": kind, "kindClass": "k-whale" if kind == "WHALE" else "k-top",
                "amount": w["amount"], "amount24h": w["amount24h"], "rank": i + 1,
                "pseudonym": w["pseudonym"],
                "pnlText": compact(w["amount"]),
                "pnlDir": None if w["amount"] is None else ("up" if w["amount"] >= 0 else "down"),
                "subText": (w["pseudonym"] or "--") if w["amount24h"] is None
                else signed(w["amount24h"], lambda v: compact(v) + " 24H"),
            })
        return rows

    # ── 成交流 ──
    def _normalize_trades(self, lst: List[dict], limit: int, source: str) -> dict:
        lst = sorted(lst, key=lambda t: num(t.get("timestamp")) or 0, reverse=True)
        rows = []
        for t in lst[:limit]:
            side = str(t.get("side") or "").upper()
            notional = (num(t.get("size")) or 0) * (num(t.get("price")) or 0)
            title = str(t.get("title") or "")
            st = title[:25] + "\u2026" if len(title) > 26 else title
            ts = num(t.get("timestamp"))
            rows.append({
                "key": f"{t.get('transactionHash')}|{t.get('asset')}|{t.get('timestamp')}",
                "address": str(t.get("proxyWallet") or ""),
                "addressShort": short_addr(t.get("proxyWallet")),
                "side": side, "outcome": t.get("outcome"), "title": title,
                "size": num(t.get("size")), "price": num(t.get("price")),
                "notional": notional, "timestamp": ts,
                "actionText": (st + "  " if st else "") + f"{side or '--'} {str(t.get('outcome') or '').upper()}",
                "sizeText": money(notional, 0) + "  " + (hhmmss(ts) if ts else "--"),
            })
        return {"source": source, "rows": rows}

    def load_copy(self, wallets: List[dict], force: bool = False) -> dict:
        tops = [w["address"] for w in wallets[: self.cfg["copy_wallets"]]]
        key = "copy:" + (tops[0][:10] if tops else "none")

        def _do():
            settled = [self._settle(self._get_json, f"{DATA_API}/trades?user={a}&limit=10") for a in tops]
            oks = [s["value"] for s in settled if s["ok"] and isinstance(s["value"], list)]
            if not oks:
                raise RuntimeError("trades api red")
            if not any(oks):
                # 追踪钱包无成交流水 → 回落全站最新流水，并在 source 注明（不冒充 copy 信号）
                j = self._get_json(f"{DATA_API}/trades?limit=40")
                return self._normalize_trades(j if isinstance(j, list) else [], 8, "GLOBAL_FEED")
            merged: List[dict] = []
            for l in oks:
                merged.extend(l)
            return self._normalize_trades(merged, 8, "TRACKED_WALLETS")

        return self._cached(key, self.cfg["ttl_trades_s"], _do, force)

    # ── PNL ──（positions 是 max-age=5 的唯一真实时通道 → 60s 刷新）
    def load_pnl(self, wallets: List[dict], force: bool = False) -> dict:
        tops = [w["address"] for w in wallets[: self.cfg["copy_wallets"]]]
        key = "pnl:" + (tops[0][:10] if tops else "none")
        return self._cached(key, self.cfg["ttl_positions_s"], lambda: self._pnl_fetch(tops), force)

    def _pnl_fetch(self, tops: List[str]) -> dict:
        settled = [self._settle(self._get_json, f"{DATA_API}/positions?user={a}&limit=20") for a in tops]
        oks = [s["value"] for s in settled if s["ok"] and isinstance(s["value"], list)]
        if not oks:
            return {"ok": False, "offline": True, "reason": "positions api red", "samples": []}
        pos: List[dict] = []
        for l in oks:
            pos.extend(l)
        if not pos:
            return {"ok": False, "offline": False, "reason": "no open positions", "samples": []}
        vals = [num(p.get("cashPnl")) for p in pos]
        vals = [v for v in vals if v is not None]
        if not vals:
            return {"ok": False, "offline": False, "reason": "cashPnl missing", "samples": []}
        total = sum(vals)
        prev = self._pnl_samples[-1] if self._pnl_samples else None
        delta = None if prev is None else total - prev
        # 只在观测值真的变化时记点（1s 重绘若每 tick 都记，序列会变成秒级重复值）
        if not self._pnl_samples or self._pnl_samples[-1] != total:
            self._pnl_samples.append(total)
            if len(self._pnl_samples) > self.cfg["sample_max"]:
                self._pnl_samples.pop(0)
        return {
            "ok": True, "offline": False, "total": total, "delta": delta,
            "positions": len(vals), "wallets": len(tops), "samples": list(self._pnl_samples),
            "text": signed(total, lambda v: money(v, 0)),
            "dir": (("up" if total >= 0 else "down") if delta is None
                    else ("up" if delta > 0 else ("down" if delta < 0 else "flat"))),
            "deltaText": "--" if delta is None else signed(delta, lambda v: money(v, 0)),
            "deltaDir": None if delta is None else ("up" if delta > 0 else ("down" if delta < 0 else "flat")),
            "meta": f"{len(vals)} POS \u00b7 {len(tops)} WALLETS",
        }

    # ── CN 带 ──
    def load_band(self) -> dict:
        txt = self._get_gbk(BAND_URL)
        mp: Dict[str, List[str]] = {}
        for line in txt.split("\n"):
            if 'v_' in line and '="' in line:
                key = line.split("v_", 1)[1].split("=", 1)[0]
                val = line.split('="', 1)[1].rsplit('"', 1)[0]
                mp[key] = val.split("~")
        items, amt, has_amt = [], 0.0, False
        for code in BAND_CODES:
            f = mp.get(code)
            if not f or len(f) < 8:
                continue
            name, price, chg, amount_wan = f[1], num(f[3]), num(f[5]), num(f[7])
            if name is None or price is None:
                continue
            items.append({
                "code": code, "name": name, "price": price, "chgPct": chg,
                "dir": "flat" if chg is None else ("up" if chg > 0 else ("down" if chg < 0 else "flat")),
                "priceText": f"{price:.2f}",
                "chgText": "" if chg is None else ("+" if chg > 0 else "") + f"{chg:.2f}%",
            })
            if code in ("s_sh000001", "s_sz399001") and amount_wan is not None:
                amt += amount_wan
                has_amt = True
        if not items:
            return {"ok": False, "reason": "no parsable band rows", "items": []}
        return {"ok": True, "direct": True, "items": items,
                "amountYi": (amt / 10000) if has_amt else None}   # 万元 → 亿元

    # ── SCANNER：定义性套利（negRisk 门槛）──
    def _scan_from_feed(self, url: str) -> Optional[dict]:
        j = self._get_json(url)
        arr = j if isinstance(j, list) else (j.get("edges") if isinstance(j, dict) else None)
        if not arr:
            return None
        rows = []
        for idx, e in enumerate(arr):
            pct = num(e.get("edge_pct"))
            if pct is None and num(e.get("edge")) is not None:
                pct = num(e["edge"]) * 100
            px, fair = num(e.get("price")), num(e.get("fair"))
            rows.append({
                "id": e.get("id") or e.get("conditionId") or f"feed{idx}",
                "market": e.get("market") or e.get("question") or "--",
                "sumBid": num(e.get("sum_bid")), "sumAsk": num(e.get("sum_ask")),
                "sumSpread": None, "legs": num(e.get("legs")),
                "feeRate": num(e.get("fee_rate")), "tick": num(e.get("tick_size")),
                "grossPct": num(e.get("gross_pct")), "netPct": pct,
                "viable": bool(pct is not None and pct > 0),
                "side": str(e.get("side") or "").upper(),
                "yesText": cents(px), "noText": cents(None if px is None else 1 - px),
                "priceText": cents(px),
                "fairText": "--" if fair is None else "FAIR " + cents(fair),
                "vol24Text": compact(num(e.get("volume24hr"))),
                "volumeText": compact(num(e.get("volume"))),
                "edgeText": "--" if pct is None else ("+" if pct > 0 else "") + f"{pct:.4f}%",
                # 与内置算法保持同一语义：不成立的机会只给最弱权重
                "badgeTone": ("dim" if (pct is None or pct <= 0) else "up"),
            })
        return {"source": url, "rows": rows}

    def _get_keyset_page(self) -> List[dict]:
        j = self._get_json(
            f"{GAMMA}/markets/keyset?closed=false&limit={self.cfg['gamma_limit']}"
            "&order=volume24hr&ascending=false"
        )
        return (j or {}).get("markets") or []      # 响应是对象，不是数组

    def scan_computed(self, force: bool = False) -> dict:
        """定义性套利，**只对完整且互斥穷尽的集合**计算。

        与上一版的两个关键差别（都是实测踩出来的）：
         1) 集合必须从 `/events/<id>` 取（事件内嵌该集合的**全部** markets）。
            只按 `/markets/keyset` 单页分组会拿到残缺集合：实测 NBA 2027 Champion
            在第一页只出现 2 条腿（事件实际 36 条），Σask 会算成 0.005 → 假 +99.5% edge。
         2) **完整度硬校验**：事件内每条腿都必须未关闭、且同时有 bestAsk 与 bestBid；
            任一条腿不满足 → 整条剔除并计入 `scanIncomplete`。
            （实测：Dota 事件 34 条腿里 14 条已关闭、Brazil 32 条里 17 条已关闭。）
        `cumulativeMarkets` 实测是布尔值不是数量，不能用来声明腿数，故不采用。
        """
        ms = self._cached("keyset", self.cfg["ttl_markets_s"], self._get_keyset_page, force)
        by_ev: Dict[str, List[dict]] = {}
        for m in ms:
            evs = m.get("events") or []
            if evs and evs[0].get("id"):
                by_ev.setdefault(str(evs[0]["id"]), []).append(m)
        # 候选 = 第一页里出现**至少 1 条腿**的 event（不是 ≥2）。
        # 理由（实测）：NBA 2027 Champion 全集 36 条腿，但某一页里可能只出现 1~2 条；
        # 若要求 ≥2 就会整条漏掉。真正的腿数一律以 /events/<id> 为准。
        # 按该 event 在本页出现腿的成交量降序，保证优先扫最活跃的集合。
        groups = sorted(
            [g for g in by_ev.values() if len(g) >= 1],
            key=lambda g: -sum((num(m.get("volume24hr")) or 0) for m in g),
        )[: self.cfg["scan_event_max"]]

        meta = {
            "scanRangeLabel": f"/markets/keyset first page, limit={self.cfg['gamma_limit']}, order=volume24hr",
            "scanRange": len(ms),
            "scanEventsCandidates": len(groups),
            "scanNonExclusive": 0,
            "scanIncomplete": 0,
            "scanRejected": 0,
            "scanFeeMissing": 0,
        }
        rows: List[dict] = []

        for g in groups:
            ev_id = str(g[0]["events"][0]["id"])
            r = self._settle(self._cached, f"event:{ev_id}", self.cfg["ttl_markets_s"],
                             (lambda eid=ev_id: self._get_json(f"{GAMMA}/events/{eid}")), force)
            if not r["ok"]:
                continue
            ev = r["value"]
            mkts = ev.get("markets") or []
            if len(mkts) < 2:
                continue
            if not ((ev.get("negRisk") is True) or (ev.get("enableNegRisk") is True)):
                meta["scanNonExclusive"] += 1          # 非互斥集合：Σ 价格天然 >> 1，不是套利
                continue

            # ── 完整度硬校验 ──
            usable, incomplete, fee_missing = [], False, 0
            for m in mkts:
                if m.get("closed") is True:
                    incomplete = True                   # 集合里已有已结算腿 → 不是干净的完整集合
                    break
                outs = jarr(m.get("outcomes"))
                ask, bid = num(m.get("bestAsk")), num(m.get("bestBid"))
                if len(outs) != 2 or ask is None or bid is None:
                    incomplete = True                   # 缺任一腿的盘口 → 合计无意义
                    break
                fr = num((m.get("feeSchedule") or {}).get("rate"))
                if fr is None:
                    fee_missing += 1
                usable.append({"ask": ask, "bid": bid, "rate": fr,
                               "spread": num(m.get("spread")) or 0.0,
                               "tick": num(m.get("orderPriceMinTickSize")) or 0.01})
            if incomplete or len(usable) != len(mkts) or len(usable) < 2:
                meta["scanIncomplete"] += 1
                continue

            cost = sum(u["ask"] for u in usable)
            proceeds = sum(u["bid"] for u in usable)
            spread_sum = sum(u["spread"] for u in usable)
            tick = usable[-1]["tick"]
            if cost <= 0:
                meta["scanRejected"] += 1
                continue
            # 成本必须读**每个市场自己的** rate（实测 0.04~0.05 浮动，不是常数）；
            # 缺失的按最保守值处理，并统计缺失条数写进 meta。
            rates = [u["rate"] for u in usable if u["rate"] is not None]
            fee_rate = max(rates + [self.cfg["fee_rate_default"]]) if rates else self.cfg["fee_rate_default"]
            meta["scanFeeMissing"] += fee_missing

            gross_buy, gross_sell = 1 - cost, proceeds - 1
            if gross_buy <= 0 and gross_sell <= 0:
                continue                              # 毛额无空间 → 不产出
            # 选边按毛额（先指出错价在哪一侧）；可行性按扣费后净值判定。
            # Σspread 只随行展示、不折进百分比：36 腿集合 Σspread 达 606.6¢，
            # 折进去会得到 -174%/-626% 这种无信息量数字，而判定结论不变。
            use_buy = gross_buy >= gross_sell
            gross_pct = (gross_buy / cost * 100) if use_buy else gross_sell * 100
            if use_buy:
                net_pct = tick_floor(1 - cost * (1 + fee_rate), tick) / cost * 100
            else:
                net_pct = tick_floor(proceeds * (1 - fee_rate) - 1, tick) * 100
            viable = net_pct > 0
            rows.append({
                "id": ev_id, "market": ev.get("title") or "--", "legs": len(usable),
                "sumAsk": cost, "sumBid": proceeds, "sumSpread": spread_sum,
                "feeRate": fee_rate, "feeMissing": fee_missing, "tick": tick,
                "grossPct": gross_pct, "netPct": net_pct, "viable": viable,
                "side": "BUY ALL" if use_buy else "SELL ALL",
                "yesText": "ΣBID " + cents(proceeds), "noText": "ΣASK " + cents(cost),
                "priceText": cents(cost if use_buy else proceeds),
                "fairText": ("GROSS " + ("+" if gross_pct > 0 else "") + f"{gross_pct:.2f}%"),
                "vol24Text": compact(num(ev.get("volume24hr"))),
                "volumeText": compact(num(ev.get("volume24hr"))),
                "edgeText": ("+" if net_pct > 0 else "") + f"{net_pct:.2f}%",
                "sideText": (("BUY ALL" if use_buy else "SELL ALL") + f" \u00b7 {len(usable)} OUT \u00b7 ΣSPR "
                             + cents(spread_sum) + ("" if viable else " \u00b7 NOT VIABLE")),
                "badgeTone": "up" if viable else "dim",
                "_sort": abs(gross_pct),
            })
        rows.sort(key=lambda r: -r["_sort"])
        for r in rows:
            r.pop("_sort", None)
        return {"source": "computed", "rows": rows[: self.cfg["scan_limit"]], "meta": meta}

    def load_scanner(self, edges_url: Optional[str] = None, force: bool = False) -> dict:
        feeds = [edges_url] if edges_url else list(self.cfg["edge_feeds"])
        for f in feeds:
            try:
                got = self._scan_from_feed(f)
            except Exception:                          # noqa: BLE001 — feed 不存在就回落内置算法
                got = None
            if got:
                return got
        return self.scan_computed(force=force)

    # ── snapshot：唯一入口 ──
    def snapshot(self, force: bool = False, edges_url: Optional[str] = None) -> dict:
        cfg = self.cfg
        if not force and self._cache and (time.time() - self._cache["ts"]) < cfg["refresh_s"]:
            return self._cache

        errors: List[str] = []
        out: Dict[str, Any] = {
            "ts": time.time(), "ok": False, "proxyDown": False, "errors": errors,
            "header": None, "scanner": [], "wallets": [], "markets": [], "copy": [],
            "pnl": None, "leaderboard": [], "cnBand": None, "meta": {}, "health": {},
        }
        health = out["health"]

        band_r = self._settle(self._cached, "band", self.cfg["band_refresh_s"], self.load_band, force)
        mk_r = self._settle(self.load_markets, force)
        lb_r = self._settle(self.load_leaderboard, force)
        sc_r = self._settle(self.load_scanner, edges_url, force)

        if mk_r["ok"]:
            out["markets"] = mk_r["value"]
            health["markets"] = "ok" if mk_r["value"] else "empty"
        else:
            errors.append("markets: " + mk_r["error"]); health["markets"] = "offline"

        if lb_r["ok"]:
            out["leaderboard"] = lb_r["value"]
            out["wallets"] = self.load_wallets(lb_r["value"])
            health["leaderboard"] = "ok" if lb_r["value"] else "empty"
            health["wallets"] = "ok" if out["wallets"] else "empty"
        else:
            errors.append("leaderboard: " + lb_r["error"])
            health["leaderboard"] = health["wallets"] = "offline"

        if sc_r["ok"] and sc_r["value"]:
            out["scanner"] = sc_r["value"]["rows"]
            out["meta"]["scannerSource"] = sc_r["value"]["source"]
            m = sc_r["value"].get("meta")
            if m:
                out["meta"].update({
                    "scanRange": m["scanRange"],
                    "scanRangeLabel": m.get("scanRangeLabel"),
                    "scanEvents": m["scanEventsCandidates"] - m["scanNonExclusive"],
                    "scanNonExclusive": m["scanNonExclusive"],
                    "scanIncomplete": m.get("scanIncomplete", 0),
                    "scanRejected": m["scanRejected"],
                    "scanFeeMissing": m.get("scanFeeMissing", 0),
                })
            health["scanner"] = "ok" if out["scanner"] else "empty"
        else:
            errors.append("scanner: " + sc_r.get("error", "no feed"))
            health["scanner"] = "offline"

        if out["wallets"]:
            cp_r = self._settle(self.load_copy, out["wallets"], force)
            pn_r = self._settle(self.load_pnl, out["wallets"], force)
        else:
            cp_r, pn_r = {"ok": False, "error": "no wallets"}, {"ok": False, "error": "no wallets"}

        if cp_r["ok"]:
            out["copy"] = cp_r["value"]["rows"]
            out["meta"]["copySource"] = cp_r["value"]["source"]
            health["copy"] = "ok" if out["copy"] else "empty"
        else:
            errors.append("copy: " + cp_r["error"]); health["copy"] = "offline"

        if pn_r["ok"]:
            out["pnl"] = pn_r["value"]
            health["pnl"] = "ok" if pn_r["value"]["ok"] else ("offline" if pn_r["value"]["offline"] else "nodata")
        else:
            out["pnl"] = {"ok": False, "offline": True, "reason": "positions api red", "samples": []}
            health["pnl"] = "offline"
            errors.append("pnl: " + pn_r["error"])

        if band_r["ok"] and band_r["value"]:
            out["cnBand"] = band_r["value"]
            health["cnBand"] = "ok" if band_r["value"]["ok"] else "empty"
        else:
            out["cnBand"] = None
            health["cnBand"] = "offline"

        # header 汇总（全部来自真实字段）
        amts = [w["amount"] for w in out["wallets"] if w["amount"] is not None]
        with1d = [x for x in out["leaderboard"] if x["has1d"]]
        pos1d = [x for x in with1d if x["pos1d"]]
        # EDGES 只报**可执行的**（扣费后净值仍为正）。
        # NOT VIABLE 的候选**不得**被计成 EDGE：否则顶栏"N EDGES"会和面板里的
        # "NOT VIABLE" 直接打架，用户会以为有 N 个机会可以动手，实际一个都不能做。
        n_viable = sum(1 for r in out["scanner"] if r.get("viable"))
        n_scanned = len(out["scanner"])
        out["header"] = {
            "trackedPnlSum": sum(amts) if amts else None, "trackedCount": len(amts),
            "cashPnl": out["pnl"]["total"] if (out["pnl"] and out["pnl"].get("ok")) else None,
            "cashPnlDir": out["pnl"]["dir"] if (out["pnl"] and out["pnl"].get("ok")) else None,
            "edges": n_viable,              # 可执行机会数（唯一允许展示为 EDGES 的口径）
            "edgesScanned": n_scanned,      # 扫到但不成立的行数，仅供透明度/审计
            "win24hPct": (len(pos1d) / len(with1d) * 100) if with1d else None,
            "wallets": len(out["wallets"]),
            "lastSyncTs": time.time(),
            "text": {
                "trackedPnl": compact(sum(amts)) if amts else "--",
                "cashPnl": out["pnl"]["text"] if (out["pnl"] and out["pnl"].get("ok")) else "--",
                "edges": str(n_viable),
                "edgesScanned": str(n_scanned),
                "win24h": f"{len(pos1d)/len(with1d)*100:.1f}%" if with1d else "--",
                "wallets": str(len(out["wallets"])),
                "sync": hhmmss(time.time()),
            },
        }

        poly_panels = ["markets", "wallets", "leaderboard", "scanner", "copy", "pnl"]
        poly_offline = sum(1 for k in poly_panels if health.get(k) == "offline")
        out["proxyDown"] = poly_offline >= 4
        out["ok"] = poly_offline < 6 or (band_r["ok"] and bool(band_r["value"]))
        out["meta"]["polyOffline"] = poly_offline
        out["meta"]["cnBandLive"] = bool(band_r["ok"] and band_r["value"])

        self._cache = out
        return out

    def refresh_band_only(self) -> dict:
        r = self._settle(self.load_band)
        if r["ok"] and r["value"] and self._cache:
            self._cache["cnBand"] = r["value"]
            self._cache["health"]["cnBand"] = "ok" if r["value"]["ok"] else "empty"
        return r["value"] if r["ok"] else {"ok": False, "reason": r["error"], "items": []}


# ── 模块级默认实例：snapshot() 单一入口 ─────────────────────────────────────
_DEFAULT: Optional[PolyFeeds] = None
_DEFAULT_LOCK = threading.Lock()


def get_feeds(**kwargs) -> PolyFeeds:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = PolyFeeds(**kwargs)
        return _DEFAULT


def snapshot(force: bool = False, edges_url: Optional[str] = None) -> dict:
    """返回 {header, scanner, wallets, markets, copy, pnl, leaderboard, cnBand} + meta/health/..."""
    return get_feeds().snapshot(force=force, edges_url=edges_url)


if __name__ == "__main__":       # 手工自检：python feeds.py
    s = snapshot(force=True)
    print("ok=%s proxyDown=%s" % (s["ok"], s["proxyDown"]))
    for k in ("header", "scanner", "wallets", "markets", "copy", "pnl", "leaderboard", "cnBand"):
        v = s.get(k)
        n = len(v) if isinstance(v, list) else ("obj" if v else "None")
        print(f"  {k:12s} rows={n}")
    print("  health:", s["health"])
    print("  errors:", s["errors"])
