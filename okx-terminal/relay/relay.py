#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OKX realtime relay (backend fan-out) -- PUSH ONLY, never polls the upstream.

Upstream : two OKX public WebSocket connections (NO credentials, public channels only)
             public   wss://ws.okx.com:8443/ws/v5/public     tickers/trades/books/bbo-tbt
                                                                open-interest/funding-rate
             business wss://ws.okx.com:8443/ws/v5/business   candle1m
Downstream: GET /ws (WebSocket) and GET /sse (Server-Sent Events) -- identical payloads.
Side car : jsonl persistence (trades raw, tickers 1s-aggregated, closed candles), log rotation.

*** NETWORK PITFALL (verified on this box) ***
aiohttp's ws_connect CANNOT reach OKX directly here: TCP connects, then the TLS ClientHello
is reset by the remote (WinError 64 / "指定的网络名不再可用"). websocket-client does not
hit this, which makes the failure look library-independent when it is not.
aiohttp also IGNORES trust_env for ws_connect, so the proxy must be passed EXPLICITLY:
    sess.ws_connect(url, proxy="http://127.0.0.1:7890")
This process therefore probes the direct route once at startup and falls back to the local
proxy if direct is dead (see --proxy / --no-proxy / detect_route()). The user's system proxy
settings are never modified, and no credentials are read or stored.

Design contract (see README.md for the full field table):
  {"t":"snapshot", ...}    first frame per client
  {"t":"ticker","inst","last","chg24h_pct","vol24h",...}
  {"t":"trades","inst","rows":[...]}
  {"t":"book","inst","bids","asks","ts"}
  {"t":"candle","inst","bar","o","h","l","c","vol","confirm","ts"}
  {"t":"oi","inst","oiUsd","ts"}
  {"t":"funding","inst","rate","nextTime","ts"}
  {"t":"stats","upstream","route","clients","msgs_per_s","ring",...}   1 per second
  {"t":"error","scope","msg"}                                           upstream trouble

All OKX `ts` values are milliseconds UTC and are passed through untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import errno
import json
import logging
import os
import socket
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit, urlunsplit

# Windows console is GBK (chcp 936); force utf-8 so CJK / math glyphs never crash logging.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import aiohttp
from aiohttp import WSMsgType, web

try:
    import orjson

    def jdumps(obj: Any) -> str:
        return orjson.dumps(obj).decode("utf-8")
except Exception:  # pragma: no cover
    def jdumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


CN = timezone(timedelta(hours=8))
log = logging.getLogger("relay")

OKX_PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"
OKX_BUSINESS_WS = "wss://ws.okx.com:8443/ws/v5/business"
OKX_REST = "https://www.okx.com"

SPOT = ("BTC-USDT", "ETH-USDT")
SWAP = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
ALL_INSTS = SPOT + SWAP

# Local proxy candidates probed ONLY if the environment does not advertise one.
PROXY_CANDIDATES = ("http://127.0.0.1:7890", "http://127.0.0.1:7891", "http://127.0.0.1:10809", "http://127.0.0.1:1080")


def now_ms() -> int:
    return int(time.time() * 1000)


# --- transient socket errors ---------------------------------------------------------
# A client hard-closing (tab close, Wi-Fi drop, sleep/resume, RST) makes Windows raise
# OSError 10054/10053/10038/64. asyncio's ProactorEventLoop hits this inside
#   proactor_events.py:154 _call_connection_lost -> self._sock.shutdown(SHUT_RDWR)
# whose `finally` block has NO try/except, so the error escapes into the loop's callback
# machinery. These are NORMAL conditions, never a reason to stop pushing data.
TRANSIENT_WINERR = {10053, 10054, 10038, 64, 995, 1236, 10058}
TRANSIENT_ERRNO = {errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.ENOTCONN, errno.ESHUTDOWN}


def is_transient_socket_error(exc: BaseException) -> bool:
    if exc is None:
        return False
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        # On Windows errno and winerror may carry the same WSA code, or only one of them is
        # set (a bare OSError(64) has winerror=None) -- accept either, and cover the POSIX
        # errnos (ECONNRESET/EPIPE/...) as well for portability.
        codes = {getattr(exc, "winerror", None), exc.errno}
        codes.discard(None)
        if codes & (TRANSIENT_WINERR | TRANSIENT_ERRNO):
            return True
    return False


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    x = _f(v)
    return int(x) if x is not None else None


def mask_url(url: Optional[str]) -> Optional[str]:
    """Never log proxy credentials."""
    if not url:
        return url
    try:
        p = urlsplit(url)
        if p.username or p.password:
            host = p.hostname or ""
            if p.port:
                host += ":%d" % p.port
            return urlunsplit((p.scheme, "***@" + host, p.path, p.query, p.fragment))
        return url
    except Exception:
        return "***"


def parse_candle(inst: str, item: Any, bar: str, src: str = "ws") -> Optional[dict]:
    """OKX candle payloads are ARRAYS (both WS and REST):
       [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    A dict form is tolerated defensively, but arrays are what OKX actually sends."""
    if isinstance(item, (list, tuple)):
        g = lambda i: item[i] if len(item) > i else None  # noqa: E731
        ts = _i(g(0))
        o, h, l, c = _f(g(1)), _f(g(2)), _f(g(3)), _f(g(4))
        vol, vol_ccy, vol_ccy_q = _f(g(5)), _f(g(6)), _f(g(7))
        confirm = int(_f(g(8)) or 0)
    elif isinstance(item, dict):
        ts = _i(item.get("ts"))
        o, h, l, c = _f(item.get("o")), _f(item.get("h")), _f(item.get("l")), _f(item.get("c"))
        vol, vol_ccy = _f(item.get("vol")), _f(item.get("volCcy"))
        vol_ccy_q = _f(item.get("volCcyQuote"))
        confirm = int(_f(item.get("confirm")) or 0)
    else:
        return None
    if ts is None:
        return None
    return {
        "t": "candle", "inst": inst, "bar": bar,
        "o": o, "h": h, "l": l, "c": c, "vol": vol, "volCcy": vol_ccy,
        "volCcyQuote": vol_ccy_q, "confirm": confirm, "ts": ts, "src": src,
    }


def parse_trade(item: dict) -> dict:
    return {
        "px": _f(item.get("px")),
        "sz": _f(item.get("sz")),
        "side": item.get("side"),
        "ts": _i(item.get("ts")) or now_ms(),
        "tradeId": item.get("tradeId"),
    }


def merge_ring(ring: collections.deque, rows: List[dict], key: str) -> int:
    """Merge REST-backfilled rows into a live ring: dedup by `key`, keep chronological order.
    Live WS rows win over backfilled rows with the same key (dict assignment order)."""
    merged: Dict[Any, dict] = {}
    for r in rows:                       # backfilled first ...
        if r.get(key) is not None:
            merged[r[key]] = r
    for r in ring:                       # ... live rows overwrite them
        if r.get(key) is not None:
            merged[r[key]] = r
    ordered = sorted(merged.values(), key=lambda r: r[key])
    cap = ring.maxlen or len(ordered)
    ring.clear()
    ring.extend(ordered[-cap:])
    return len(ring)


# --------------------------------------------------------------------------------------
# persistence: append-only jsonl, buffered in memory, flushed by a timer task (never IO
# inside the websocket receive path).
# --------------------------------------------------------------------------------------
class Persist:
    def __init__(self, data_dir: Path, flush_interval: float = 5.0) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.flush_interval = flush_interval
        self.buf: Dict[tuple, list] = collections.defaultdict(list)
        self.rows_written = 0
        self.bytes_written = 0
        self.files: set = set()
        self.flushes = 0

    def pending(self) -> int:
        return sum(len(v) for v in self.buf.values())

    @staticmethod
    def _day() -> str:
        return datetime.now(CN).strftime("%Y%m%d")

    def enqueue(self, prefix: str, inst: str, obj: dict) -> None:
        self.buf[(prefix, inst, self._day())].append(obj)

    def _write_sync(self, batches: Dict[tuple, list]) -> None:
        for (prefix, inst, day), rows in batches.items():
            path = self.data_dir / ("%s_%s_%s.jsonl" % (prefix, inst, day))
            written = 0
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                for row in rows:
                    line = jdumps(row) + "\n"
                    fh.write(line)
                    written += len(line.encode("utf-8"))
            self.rows_written += len(rows)
            self.bytes_written += written
            self.files.add(path.name)

    async def flush(self) -> None:
        if not self.buf:
            return
        batches = self.buf
        self.buf = collections.defaultdict(list)
        await asyncio.to_thread(self._write_sync, batches)
        self.flushes += 1

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            try:
                await self.flush()
            except Exception:
                log.exception("persist flush failed")


# --------------------------------------------------------------------------------------
# order book state
# --------------------------------------------------------------------------------------
class BookState:
    """OKX `books` (400 levels) is a SNAPSHOT + INCREMENTAL DELTA channel, NOT full frames.

    Measured: prevSeqId == -1  -> snapshot (400 levels both sides)
              prevSeqId == last seqId -> delta carrying only the changed levels
              checksum is always 0 on this channel, so it CANNOT be used for validation.
    A prevSeqId gap means data was lost: the caller must force a re-snapshot.
    `books5` sends a full 5-level snapshot every frame and goes through the same path.
    """

    def __init__(self, inst: str) -> None:
        self.inst = inst
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.seq: Optional[int] = None
        self.ts = 0
        self.snapshots = 0
        self.updates = 0
        self.gaps = 0

    @staticmethod
    def _apply_levels(rows: Iterable, target: Dict[float, float]) -> None:
        for r in rows or []:
            if len(r) < 2:
                continue
            px = _f(r[0])
            sz = _f(r[1])
            if px is None:
                continue
            if not sz:
                target.pop(px, None)   # size 0 == delete this level
            else:
                target[px] = sz

    def apply(self, item: dict) -> str:
        seq = _i(item.get("seqId"))
        prev = _i(item.get("prevSeqId"))
        self.ts = _i(item.get("ts")) or now_ms()
        if prev is None or prev == -1 or self.seq is None:
            self.bids.clear()
            self.asks.clear()
            self._apply_levels(item.get("bids"), self.bids)
            self._apply_levels(item.get("asks"), self.asks)
            self.seq = seq
            self.snapshots += 1
            return "snapshot"
        if prev != self.seq:
            self.gaps += 1
            self.seq = seq
            return "gap"
        self._apply_levels(item.get("bids"), self.bids)
        self._apply_levels(item.get("asks"), self.asks)
        self.seq = seq
        self.updates += 1
        return "update"

    def top(self, depth: int) -> tuple:
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:depth]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:depth]
        return [[p, s] for p, s in bids], [[p, s] for p, s in asks]


# --------------------------------------------------------------------------------------
# downstream client
# --------------------------------------------------------------------------------------
class Client:
    __slots__ = ("cid", "kind", "queue", "dropped", "sent", "t0")

    def __init__(self, cid: int, kind: str) -> None:
        self.cid = cid
        self.kind = kind
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.dropped = 0
        self.sent = 0
        self.t0 = time.time()


# --------------------------------------------------------------------------------------
# hub: state + fan-out
# --------------------------------------------------------------------------------------
class Hub:
    def __init__(self, args: argparse.Namespace, persist: Persist) -> None:
        self.args = args
        self.persist = persist
        self.t0 = time.time()

        self.ticks: Dict[str, collections.deque] = {i: collections.deque(maxlen=args.ticks_ring) for i in ALL_INSTS}
        self.trades: Dict[str, collections.deque] = {i: collections.deque(maxlen=args.trades_ring) for i in ALL_INSTS}
        self.candles: Dict[str, collections.deque] = {i: collections.deque(maxlen=args.candles_ring) for i in ALL_INSTS}
        self.last_tick: Dict[str, dict] = {}
        self.book: Dict[str, dict] = {}
        self.book_states: Dict[str, BookState] = {}
        self.bbo: Dict[str, dict] = {}
        self.oi: Dict[str, dict] = {}
        self.funding: Dict[str, dict] = {}
        self.tick_bucket: Dict[str, dict] = {}

        self.clients: List[Client] = []
        self._cid = 0
        self.stopping = False
        self.prime_info: Dict[str, Any] = {"status": "skipped"}

        self.group_status: Dict[str, str] = {}
        self.upstream_sockets: Dict[str, Any] = {}
        self.reconnects: Dict[str, int] = collections.Counter()
        self.sub_ok: Dict[str, set] = collections.defaultdict(set)
        self.sub_expected: Dict[str, int] = {}
        self.sub_err: List[dict] = []
        self.msg_total = 0
        self.transient_errors = 0
        self.write_errors = 0
        self.msg_by_channel: Dict[str, int] = collections.Counter()
        self.chan_bytes: Dict[str, list] = collections.defaultdict(lambda: [0, 0])  # [sum, count]
        self._mps_window: collections.deque = collections.deque()
        self.route = {"mode": "unknown", "proxy": None, "direct_ok": None}

    # ---------------- clients ----------------
    def add_client(self, kind: str) -> Client:
        self._cid += 1
        c = Client(self._cid, kind)
        self.clients.append(c)
        log.info("client+ %s#%d now=%d", kind, c.cid, len(self.clients))
        return c

    def remove_client(self, c: Client) -> None:
        if c in self.clients:
            self.clients.remove(c)
        log.info("client- %s#%d now=%d sent=%d dropped=%d", c.kind, c.cid, len(self.clients), c.sent, c.dropped)

    def broadcast(self, obj: dict) -> None:
        data = jdumps(obj)
        for c in self.clients:
            try:
                c.queue.put_nowait(data)
            except asyncio.QueueFull:
                try:
                    c.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                c.dropped += 1
                try:
                    c.queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

    # ---------------- upstream bookkeeping ----------------
    def note_msg(self) -> None:
        self.msg_total += 1
        t = time.time()
        self._mps_window.append(t)
        while self._mps_window and t - self._mps_window[0] > 5.0:
            self._mps_window.popleft()

    def note_frame(self, channel: str, nbytes: int) -> None:
        slot = self.chan_bytes[channel]
        slot[0] += nbytes
        slot[1] += 1

    def mps(self) -> float:
        if not self._mps_window:
            return 0.0
        span = max(1.0, time.time() - self._mps_window[0])
        return len(self._mps_window) / span

    def overall_status(self) -> str:
        vals = list(self.group_status.values())
        if not vals:
            return "connecting"
        if all(v == "ok" for v in vals):
            return "ok"
        if any(v == "ok" for v in vals):
            return "degraded"
        if any(v == "reconnecting" for v in vals):
            return "reconnecting"
        return "down"

    # ---------------- channel handlers ----------------
    def on_ticker(self, inst: str, item: dict, src: str = "ws") -> None:
        ts = _i(item.get("ts")) or now_ms()
        last = _f(item.get("last"))
        open24h = _f(item.get("open24h"))
        chg = ((last - open24h) / open24h * 100.0) if (last is not None and open24h) else None
        msg = {
            "t": "ticker",
            "inst": inst,
            "last": last,
            "chg24h_pct": round(chg, 4) if chg is not None else None,
            "vol24h": _f(item.get("vol24h")),
            "volCcy24h": _f(item.get("volCcy24h")),
            "bid": _f(item.get("bidPx")),
            "ask": _f(item.get("askPx")),
            "bidSz": _f(item.get("bidSz")),
            "askSz": _f(item.get("askSz")),
            "open24h": open24h,
            "high24h": _f(item.get("high24h")),
            "low24h": _f(item.get("low24h")),
            "ts": ts,
            "src": src,
        }
        self.last_tick[inst] = msg
        self._aggregate(inst, msg)
        self.broadcast(msg)

    def _aggregate(self, inst: str, msg: dict) -> None:
        sec = msg["ts"] // 1000
        b = self.tick_bucket.get(inst)
        if b is None or b["sec"] != sec:
            if b is not None:
                self._close_bar(inst, b)
            self.tick_bucket[inst] = {
                "sec": sec, "o": msg["last"], "h": msg["last"], "l": msg["last"], "c": msg["last"],
                "vol": msg["vol24h"], "volCcy": msg["volCcy24h"], "src": msg["src"], "n": 1, "ts": msg["ts"],
            }
            return
        if msg["last"] is not None:
            b["h"] = msg["last"] if b["h"] is None else max(b["h"], msg["last"])
            b["l"] = msg["last"] if b["l"] is None else min(b["l"], msg["last"])
            b["c"] = msg["last"]
        b["vol"] = msg["vol24h"]
        b["volCcy"] = msg["volCcy24h"]
        b["ts"] = msg["ts"]
        b["n"] += 1

    def _close_bar(self, inst: str, b: dict) -> None:
        bar = {
            "inst": inst, "sec": b["sec"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
            "vol24h": b["vol"], "volCcy24h": b["volCcy"], "n": b["n"], "ts": b["ts"], "src": b["src"],
        }
        self.ticks[inst].append(bar)
        if b["src"] == "ws":
            # only live-observed bars are persisted; REST-backfilled ones stay in memory
            self.persist.enqueue("ticks", inst, bar)

    def close_all_bars(self) -> None:
        for inst, b in list(self.tick_bucket.items()):
            self._close_bar(inst, b)
        self.tick_bucket.clear()

    def on_trades(self, inst: str, items: Iterable[dict], prime: bool = False) -> None:
        rows = []
        for it in items:
            row = parse_trade(it)
            rows.append(row)
            self.trades[inst].append(row)
            if not prime:
                self.persist.enqueue("trades", inst, {"inst": inst, **row})
        if rows and not prime:
            self.broadcast({"t": "trades", "inst": inst, "rows": rows, "ts": rows[-1]["ts"]})

    def backfill_trades(self, inst: str, items: Iterable[dict]) -> int:
        """REST prime: fill the ring for display only (never persisted, never broadcast)."""
        rows = [parse_trade(it) for it in items]
        return merge_ring(self.trades[inst], rows, "tradeId")

    def backfill_candles(self, inst: str, items: Iterable[Any]) -> int:
        rows = [b for b in (parse_candle(inst, it, self.args.candle_bar, src="rest") for it in items) if b]
        return merge_ring(self.candles[inst], rows, "ts")

    def on_book(self, channel: str, inst: str, item: dict) -> Optional[str]:
        """Returns 'gap' when the incremental feed lost data and needs a re-snapshot."""
        if channel == "bbo-tbt":
            self.bbo[inst] = {
                "bids": [[_f(l[0]), _f(l[1])] for l in (item.get("bids") or []) if len(l) >= 2],
                "asks": [[_f(l[0]), _f(l[1])] for l in (item.get("asks") or []) if len(l) >= 2],
                "ts": _i(item.get("ts")) or now_ms(),
            }
            return None

        state = self.book_states.get(inst)
        if state is None:
            state = self.book_states[inst] = BookState(inst)
        status = state.apply(item)
        if status == "gap":
            log.warning("book gap inst=%s (seq lost) -> requesting re-snapshot", inst)
            return "gap"
        bids, asks = state.top(self.args.book_levels)
        msg = {
            "t": "book",
            "inst": inst,
            "bids": bids,
            "asks": asks,
            "depth_total": {"bids": len(state.bids), "asks": len(state.asks)},
            "seqId": state.seq,
            "status": status,
            "ts": state.ts,
        }
        self.book[inst] = msg
        self.broadcast(msg)
        return None

    def on_candle(self, inst: str, item: Any, src: str = "ws") -> None:
        """src='rest' bars come from the startup backfill and are never persisted."""
        bar = parse_candle(inst, item, self.args.candle_bar, src=src)
        if bar is None:
            log.warning("unparseable candle inst=%s item=%r", inst, str(item)[:120])
            return
        ts = bar["ts"]
        ring = self.candles.setdefault(inst, collections.deque(maxlen=self.args.candles_ring))
        if ring and ring[-1].get("ts") == ts:
            ring[-1] = bar
        elif ring and ring[-1].get("ts", 0) > ts:
            merge_ring(ring, [bar], "ts")   # out-of-order (backfill raced live data)
        else:
            ring.append(bar)
        if bar["confirm"] == 1 and src == "ws":
            self.persist.enqueue("candle", inst, bar)
        self.broadcast(bar)

    def on_oi(self, inst: str, item: dict) -> None:
        msg = {
            "t": "oi", "inst": inst,
            "oi": _f(item.get("oi")), "oiCcy": _f(item.get("oiCcy")), "oiUsd": _f(item.get("oiUsd")),
            "ts": _i(item.get("ts")) or now_ms(),
        }
        self.oi[inst] = msg
        self.broadcast(msg)

    def on_funding(self, inst: str, item: dict) -> None:
        msg = {
            "t": "funding", "inst": inst,
            "rate": _f(item.get("fundingRate")), "nextRate": _f(item.get("nextFundingRate")),
            "nextTime": _i(item.get("nextFundingTime")) or 0, "premium": _f(item.get("premium")),
            "ts": _i(item.get("ts")) or now_ms(),
        }
        self.funding[inst] = msg
        self.broadcast(msg)

    # ---------------- payloads ----------------
    def stats_obj(self) -> dict:
        ring = {}
        for inst in ALL_INSTS:
            ring[inst] = {
                "ticks": len(self.ticks[inst]),
                "trades": len(self.trades[inst]),
                "candles": len(self.candles.get(inst, ())),
            }
        chan = {}
        for k, (total, cnt) in self.chan_bytes.items():
            chan[k] = {"n": cnt, "bytes_avg": round(total / cnt, 1) if cnt else 0}
        books = {}
        for inst, st in self.book_states.items():
            books[inst] = {"bid_levels": len(st.bids), "ask_levels": len(st.asks),
                           "snapshots": st.snapshots, "updates": st.updates, "gaps": st.gaps,
                           "seq": st.seq}
        return {
            "t": "stats",
            "upstream": self.overall_status(),
            "upstream_detail": dict(self.group_status),
            "route": {"mode": self.route["mode"], "proxy": mask_url(self.route["proxy"]), "direct_ok": self.route["direct_ok"]},
            "reconnects": dict(self.reconnects),
            "clients": len(self.clients),
            "clients_ws": sum(1 for c in self.clients if c.kind == "ws"),
            "clients_sse": sum(1 for c in self.clients if c.kind == "sse"),
            "errors": {"transient_suppressed": self.transient_errors, "client_write_errors": self.write_errors},
            "msgs_per_s": round(self.mps(), 2),
            "msgs_total": self.msg_total,
            "msgs_by_channel": dict(self.msg_by_channel),
            "chan_bytes": chan,
            "book_state": books,
            "sub": {
                "confirmed": sum(len(v) for v in self.sub_ok.values()),
                "expected": sum(self.sub_expected.values()) or None,
                "errors": len(self.sub_err),
                "last_error": self.sub_err[-1] if self.sub_err else None,
            },
            "ring": ring,
            "persist": {
                "rows": self.persist.rows_written,
                "bytes": self.persist.bytes_written,
                "pending": self.persist.pending(),
                "files": sorted(self.persist.files),
                "dir": str(self.persist.data_dir),
            },
            "uptime_s": round(time.time() - self.t0, 1),
            "ts": now_ms(),
        }

    def snapshot_obj(self) -> dict:
        """Uniform entry for EVERY instrument (spot and swap) so the frontend never has to
        guess: `has_ticker` says whether a price exists, and missing numbers are explicit
        nulls -- a null means "this channel has not delivered yet", never "broken"."""
        insts: Dict[str, dict] = {}
        for inst in ALL_INSTS:
            tk = self.last_tick.get(inst)
            insts[inst] = {
                "t": "snapshot_inst",
                "inst": inst,
                "kind": "spot" if inst in SPOT else "swap",
                "has_ticker": tk is not None,
                "last": tk["last"] if tk else None,
                "chg24h_pct": tk["chg24h_pct"] if tk else None,
                "vol24h": tk["vol24h"] if tk else None,
                "volCcy24h": tk["volCcy24h"] if tk else None,
                "bid": tk["bid"] if tk else None,
                "ask": tk["ask"] if tk else None,
                "bidSz": tk["bidSz"] if tk else None,
                "askSz": tk["askSz"] if tk else None,
                "open24h": tk["open24h"] if tk else None,
                "high24h": tk["high24h"] if tk else None,
                "low24h": tk["low24h"] if tk else None,
                "ts": tk["ts"] if tk else None,
                "src": tk["src"] if tk else None,
                "book": self.book.get(inst),
                "bbo": self.bbo.get(inst),
                "ticks": list(self.ticks[inst])[-self.args.snapshot_ticks:],
                "candles": list(self.candles[inst])[-self.args.snapshot_candles:],
                "trades": list(self.trades[inst])[-self.args.snapshot_trades:],
                "oi": self.oi.get(inst),
                "funding": self.funding.get(inst),
                "ring_len": {"ticks": len(self.ticks[inst]), "candles": len(self.candles[inst]),
                             "trades": len(self.trades[inst])},
            }
        return {
            "t": "snapshot", "ts": now_ms(), "upstream": self.overall_status(),
            "route": {"mode": self.route["mode"], "proxy": mask_url(self.route["proxy"])},
            "prime": dict(self.prime_info),
            "insts": insts, "stats": self.stats_obj(),
        }

    def snapshot_json(self) -> str:
        return jdumps(self.snapshot_obj())

    async def stats_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self.broadcast(self.stats_obj())
            except Exception:
                log.exception("stats broadcast failed")


# --------------------------------------------------------------------------------------
# upstream: one OKX websocket per endpoint group; heartbeat, watchdog, backoff reconnect
# --------------------------------------------------------------------------------------
class Upstream:
    def __init__(self, hub: Hub, args: argparse.Namespace, name: str, url: str, chans: List[tuple]) -> None:
        self.hub = hub
        self.args = args
        self.name = name
        self.url = url
        self.sub_args = [{"channel": ch, "instId": inst} for ch, insts in chans for inst in insts]
        self.ws = None
        self.session_start = 0.0
        hub.sub_expected[name] = len(self.sub_args)

    async def run(self) -> None:
        backoff = 1.0
        self.hub.group_status[self.name] = "connecting"
        while not self.hub.stopping:
            healthy = 0.0
            try:
                await self._session()
                healthy = time.time() - self.session_start
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                healthy = time.time() - self.session_start if self.session_start else 0.0
                log.warning("[%s] upstream session ended err=%s: %s", self.name, type(exc).__name__, str(exc)[:200])
            if self.hub.stopping:
                break
            if healthy > 60:
                backoff = 1.0
            self.hub.reconnects[self.name] += 1
            self.hub.group_status[self.name] = "reconnecting"
            self.hub.broadcast({"t": "error", "scope": "upstream:" + self.name,
                                "msg": "disconnected, reconnecting in %.0fs" % backoff, "ts": now_ms()})
            log.warning("[%s] RECONNECT attempt=%d in %.0fs (last session %.0fs)",
                        self.name, self.hub.reconnects[self.name], backoff, healthy)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=12)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as sess:
            t_conn = time.time()
            # explicit proxy is mandatory for aiohttp.ws_connect on this network
            ws = await sess.ws_connect(self.url, proxy=self.hub.route["proxy"], heartbeat=None,
                                       max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=15))
            self.ws = ws
            self.hub.upstream_sockets[self.name] = ws
            self.session_start = time.time()
            self.hub.group_status[self.name] = "ok"
            log.info("[%s] UPSTREAM CONNECTED url=%s connect_ms=%d route=%s",
                     self.name, self.url, int((time.time() - t_conn) * 1000), self.hub.route["mode"])
            self.hub.broadcast({"t": "error", "scope": "upstream:" + self.name, "level": "info",
                                "msg": "connected", "ts": now_ms()})

            # (re)subscribe EVERY channel after EVERY connect -- forgetting this is the classic bug
            self.hub.sub_ok[self.name].clear()
            for arg in self.sub_args:
                await ws.send_str(json.dumps({"op": "subscribe", "args": [arg]}))
            log.info("[%s] SUBSCRIBE SENT %d args: %s", self.name, len(self.sub_args),
                     ",".join("%s/%s" % (a["channel"], a["instId"]) for a in self.sub_args))

            hb = asyncio.create_task(self._heartbeat(ws))
            last_recv = time.time()
            try:
                async for msg in ws:
                    last_recv = time.time()
                    self.last_recv = last_recv
                    if msg.type == WSMsgType.TEXT:
                        self._on_text(ws, msg.data)
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.CLOSE, WSMsgType.ERROR):
                        log.warning("[%s] upstream ws type=%s", self.name, msg.type)
                        break
            finally:
                hb.cancel()
                self.ws = None
                self.hub.upstream_sockets.pop(self.name, None)
                if self.hub.group_status.get(self.name) == "ok":
                    self.hub.group_status[self.name] = "down"
                log.info("[%s] upstream session closed", self.name)

    async def _heartbeat(self, ws) -> None:
        """Text 'ping' every N s + idle watchdog (OKX drops idle sockets)."""
        try:
            while True:
                await asyncio.sleep(self.args.ping_interval)
                if ws.closed:
                    return
                await ws.send_str("ping")
                idle = time.time() - getattr(self, "last_recv", time.time())
                if idle > self.args.idle_timeout:
                    log.warning("[%s] WATCHDOG idle=%.1fs > %.1fs -> forcing reconnect",
                                self.name, idle, self.args.idle_timeout)
                    await ws.close()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[%s] heartbeat error: %s", self.name, str(exc)[:150])

    def _on_text(self, ws, text: str) -> None:
        if text == "pong":
            return
        try:
            obj = json.loads(text)
        except Exception:
            log.warning("[%s] non-json upstream frame: %r", self.name, text[:200])
            return

        arg = obj.get("arg") or {}
        channel = arg.get("channel")
        inst = arg.get("instId")

        event = obj.get("event")
        if event:
            key = "%s:%s" % (channel, inst)
            if event == "subscribe":
                self.hub.sub_ok[self.name].add(key)
                log.info("[%s] SUBSCRIBED %s (%d/%d)", self.name, key,
                         len(self.hub.sub_ok[self.name]), len(self.sub_args))
                if len(self.hub.sub_ok[self.name]) == len(self.sub_args):
                    log.info("[%s] ALL SUBSCRIPTIONS CONFIRMED (%d)", self.name, len(self.sub_args))
            elif event == "error":
                rec = {"group": self.name, "code": obj.get("code"), "msg": obj.get("msg"),
                       "arg": arg, "ts": now_ms()}
                self.hub.sub_err.append(rec)
                log.error("[%s] SUBSCRIBE ERROR code=%s msg=%s arg=%s",
                          self.name, obj.get("code"), obj.get("msg"), arg)
                asyncio.create_task(self._retry_sub(ws, arg))
            else:
                log.info("[%s] upstream event=%s %s", self.name, event, obj)
            return

        data = obj.get("data") or []
        self.hub.note_msg()
        self.hub.msg_by_channel[channel] += 1
        self.hub.note_frame(channel, len(text.encode("utf-8")))

        if channel == "tickers":
            for it in data:
                self.hub.on_ticker(inst, it)
        elif channel == "trades":
            self.hub.on_trades(inst, data)
        elif channel in ("books", "books5", "bbo-tbt"):
            for it in data:
                if self.hub.on_book(channel, inst, it) == "gap":
                    asyncio.create_task(self._resnapshot(ws, {"channel": channel, "instId": inst}))
        elif channel and channel.startswith("candle"):
            for it in data:
                self.hub.on_candle(inst, it)
        elif channel == "open-interest":
            for it in data:
                self.hub.on_oi(inst, it)
        elif channel == "funding-rate":
            for it in data:
                self.hub.on_funding(inst, it)
        else:
            log.info("[%s] unhandled channel=%s inst=%s n=%d", self.name, channel, inst, len(data))

    async def _retry_sub(self, ws, arg: dict) -> None:
        """An {"event":"error"} subscription is retried (never silently swallowed)."""
        await asyncio.sleep(5.0)
        try:
            if ws is not None and not ws.closed:
                await ws.send_str(json.dumps({"op": "subscribe", "args": [arg]}))
                log.warning("[%s] RESUBSCRIBE retry arg=%s", self.name, arg)
        except Exception as exc:
            log.warning("[%s] resubscribe retry failed arg=%s err=%s", self.name, arg, str(exc)[:120])

    async def _resnapshot(self, ws, arg: dict) -> None:
        """After a sequence gap, unsubscribe+subscribe to force a fresh full snapshot."""
        try:
            if ws is None or ws.closed:
                return
            await ws.send_str(json.dumps({"op": "unsubscribe", "args": [arg]}))
            await asyncio.sleep(0.3)
            if not ws.closed:
                await ws.send_str(json.dumps({"op": "subscribe", "args": [arg]}))
                log.warning("[%s] RESNAPSHOT requested arg=%s", self.name, arg)
        except Exception as exc:
            log.warning("[%s] resnapshot failed arg=%s err=%s", self.name, arg, str(exc)[:120])


# --------------------------------------------------------------------------------------
# route detection: direct first, explicit local proxy as the verified fallback
# --------------------------------------------------------------------------------------
async def _try_route(proxy: Optional[str], timeout: float = 6.0) -> tuple:
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=timeout + 6)) as sess:
            t0 = time.time()
            ws = await sess.ws_connect(OKX_PUBLIC_WS, proxy=proxy, heartbeat=None, max_msg_size=0,
                                       timeout=aiohttp.ClientWSTimeout(ws_close=timeout))
            ms = int((time.time() - t0) * 1000)
            await ws.close()
            return True, "connect_ms=%d" % ms
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def env_proxy() -> Optional[str]:
    for k in ("OKX_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    return None


async def detect_route(hub: Hub, args: argparse.Namespace) -> None:
    if args.no_proxy:
        hub.route.update({"mode": "direct(forced)", "proxy": None, "direct_ok": None})
        log.info("ROUTE forced direct (--no-proxy)")
        return
    if args.proxy is not None:
        hub.route.update({"mode": "proxy(forced)", "proxy": args.proxy, "direct_ok": None})
        log.info("ROUTE forced proxy=%s (--proxy)", mask_url(args.proxy))
        return

    ok, detail = await _try_route(None)
    hub.route["direct_ok"] = ok
    log.info("ROUTE probe direct: ok=%s (%s)", ok, detail)
    if ok:
        hub.route.update({"mode": "direct", "proxy": None})
        return

    cands: List[str] = []
    e = env_proxy()
    if e:
        cands.append(e)
    for c in PROXY_CANDIDATES:
        if c not in cands:
            cands.append(c)
    for cand in cands:
        ok, detail = await _try_route(cand)
        log.info("ROUTE probe proxy=%s: ok=%s (%s)", mask_url(cand), ok, detail)
        if ok:
            hub.route.update({"mode": "proxy", "proxy": cand})
            log.warning("ROUTE: direct connect to OKX is BLOCKED on this network "
                        "(TLS reset); using explicit proxy %s. aiohttp ignores trust_env for ws_connect.",
                        mask_url(cand))
            return
    hub.route.update({"mode": "none", "proxy": None})
    log.critical("ROUTE: neither direct nor any candidate proxy reached OKX. Upstream will retry.")


# --------------------------------------------------------------------------------------
# startup REST backfill (the ONLY rest calls, once, at startup) -- so a freshly restarted
# relay does not serve a page whose chart has 2 candles on it.
# --------------------------------------------------------------------------------------
async def _rest_get(sess, path: str, params: dict, proxy: Optional[str]) -> tuple:
    """Returns (json_or_None, elapsed_ms, error_or_None)."""
    t0 = time.time()
    try:
        async with sess.get(OKX_REST + path, params=params, proxy=proxy) as r:
            js = await r.json(content_type=None)
        ms = int((time.time() - t0) * 1000)
        if js.get("code") not in ("0", 0, None):
            return None, ms, "code=%s msg=%s" % (js.get("code"), js.get("msg"))
        return js, ms, None
    except Exception as exc:
        return None, int((time.time() - t0) * 1000), "%s: %s" % (type(exc).__name__, str(exc)[:110])


async def prime_once(hub: Hub, args: argparse.Namespace) -> None:
    proxy = hub.route["proxy"]
    t_all = time.time()
    info = {"status": "running", "started_ts": now_ms(), "calls": [], "ok": 0, "fail": 0,
            "elapsed_ms": None, "candles": {}, "trades": {}, "tickers": {}}
    hub.prime_info = info
    log.info("REST BACKFILL start (one-shot) via %s | candles<=%d trades<=%d insts=%s",
             hub.route["mode"], args.prime_candles, args.prime_trades, ",".join(ALL_INSTS))
    try:
        async with aiohttp.ClientSession(trust_env=False,
                                         timeout=aiohttp.ClientTimeout(total=args.prime_timeout)) as sess:
            # ---- 1) candles (spot only: the business candle channel is subscribed for spot) ----
            for inst in SPOT:
                js, ms, err = await _rest_get(sess, "/api/v5/market/candles",
                                              {"instId": inst, "bar": args.candle_bar,
                                               "limit": args.prime_candles}, proxy)
                rows = (js or {}).get("data") or []
                n = hub.backfill_candles(inst, rows) if rows else 0
                info["candles"][inst] = n
                info["calls"].append({"ep": "candles", "inst": inst, "rows": len(rows), "ring": n, "ms": ms, "err": err})
                info["ok" if not err else "fail"] += 1
                log.info("BACKFILL candles inst=%s rows=%d ring=%d ms=%d %s",
                         inst, len(rows), n, ms, ("ERR " + err) if err else "ok")

            # ---- 2) recent trades (spot only) ----
            for inst in SPOT:
                js, ms, err = await _rest_get(sess, "/api/v5/market/trades",
                                              {"instId": inst, "limit": args.prime_trades}, proxy)
                rows = (js or {}).get("data") or []
                n = hub.backfill_trades(inst, rows) if rows else 0
                info["trades"][inst] = n
                info["calls"].append({"ep": "trades", "inst": inst, "rows": len(rows), "ring": n, "ms": ms, "err": err})
                info["ok" if not err else "fail"] += 1
                log.info("BACKFILL trades  inst=%s rows=%d ring=%d ms=%d %s",
                         inst, len(rows), n, ms, ("ERR " + err) if err else "ok")

            # ---- 3) current ticker snapshot for ALL instruments (spot + swap) ----
            for inst in ALL_INSTS:
                js, ms, err = await _rest_get(sess, "/api/v5/market/ticker", {"instId": inst}, proxy)
                rows = (js or {}).get("data") or []
                if rows and not err:
                    hub.on_ticker(inst, rows[0], src="rest")
                    info["tickers"][inst] = rows[0].get("last")
                info["calls"].append({"ep": "ticker", "inst": inst, "rows": len(rows), "ms": ms, "err": err})
                info["ok" if not err else "fail"] += 1
                log.info("BACKFILL ticker  inst=%s last=%s ms=%d %s", inst,
                         (rows[0].get("last") if rows else None), ms, ("ERR " + err) if err else "ok")
    except Exception as exc:
        info["status"] = "error"
        log.warning("REST backfill aborted: %s", str(exc)[:160])
    info["elapsed_ms"] = int((time.time() - t_all) * 1000)
    if info["status"] == "running":
        info["status"] = "ok" if info["fail"] == 0 else ("partial" if info["ok"] else "failed")
    info["finished_ts"] = now_ms()
    log.info("REST BACKFILL done status=%s ok=%d fail=%d elapsed_ms=%d | candles=%s trades=%s | "
             "ring: %s",
             info["status"], info["ok"], info["fail"], info["elapsed_ms"],
             info["candles"], info["trades"],
             {k: {"candles": len(hub.candles[k]), "ticks": len(hub.ticks[k]), "trades": len(hub.trades[k])} for k in ALL_INSTS})
    log.info("no further REST calls will be made (realtime data is WS-push only)")


# --------------------------------------------------------------------------------------
# downstream http
# --------------------------------------------------------------------------------------
HUB: Optional[Hub] = None


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    hub = HUB
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=2 ** 22)
    await ws.prepare(request)
    client = hub.add_client("ws")
    sender = asyncio.create_task(_ws_sender(ws, client))
    try:
        await ws.send_str(hub.snapshot_json())
        async for msg in ws:
            if msg.type == WSMsgType.TEXT and msg.data == "ping":
                await ws.send_str("pong")
            elif msg.type == WSMsgType.ERROR:
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:                     # dead client: never propagate
        hub.write_errors += 1
        log.warning("ws client #%d gone: %s: %s", client.cid, type(exc).__name__, exc)
    finally:
        sender.cancel()
        hub.remove_client(client)
    return ws


async def _ws_sender(ws: web.WebSocketResponse, client: Client) -> None:
    try:
        while True:
            data = await client.queue.get()
            if ws.closed:
                return
            try:
                await ws.send_str(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:             # dead client: stop this sender only
                if HUB is not None:
                    HUB.write_errors += 1
                log.warning("ws send to client #%d failed (%s: %s) -- dropping client",
                            client.cid, type(exc).__name__, exc)
                return
            client.sent += 1
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("ws sender for client #%d stopped: %s: %s", client.cid, type(exc).__name__, exc)
        return


async def sse_handler(request: web.Request) -> web.StreamResponse:
    hub = HUB
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    client = hub.add_client("sse")
    try:
        await resp.write(b"retry: 3000\n\n")
        if not await _sse_send(resp, hub.snapshot_json(), client):
            return resp
        while True:
            data = await client.queue.get()
            if not await _sse_send(resp, data, client):
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:                     # dead client: never propagate
        hub.write_errors += 1
        log.warning("sse client #%d gone: %s: %s", client.cid, type(exc).__name__, exc)
    finally:
        hub.remove_client(client)
    return resp


async def _sse_send(resp: web.StreamResponse, data: str, client: Client) -> bool:
    """Returns False when the client is gone -- the caller must then drop it."""
    try:
        await resp.write(b"data: " + data.encode("utf-8") + b"\n\n")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if HUB is not None:
            HUB.write_errors += 1
        log.warning("sse write to client #%d failed (%s: %s) -- dropping client",
                    client.cid, type(exc).__name__, exc)
        return False
    client.sent += 1
    return True


async def health_handler(request: web.Request) -> web.Response:
    hub = HUB
    body = {
        "ok": hub.overall_status() == "ok",
        "upstream": hub.overall_status(),
        "upstream_detail": dict(hub.group_status),
        "route": {"mode": hub.route["mode"], "proxy": mask_url(hub.route["proxy"]), "direct_ok": hub.route["direct_ok"]},
        "clients": len(hub.clients),
        "msgs_per_s": round(hub.mps(), 2),
        "msgs_total": hub.msg_total,
        "reconnects": dict(hub.reconnects),
        "sub": {"confirmed": sum(len(v) for v in hub.sub_ok.values()),
                "expected": sum(hub.sub_expected.values()) or None,
                "errors": len(hub.sub_err)},
        "uptime_s": round(time.time() - hub.t0, 1),
        "ts": now_ms(),
    }
    return web.json_response(body, headers={"Access-Control-Allow-Origin": "*"})


async def kill_upstream_handler(request: web.Request) -> web.Response:
    """Debug hook: force-drop the upstream socket(s) to exercise the reconnect path."""
    hub = HUB
    group = request.query.get("group")
    targets = {k: v for k, v in hub.upstream_sockets.items() if not group or k == group}
    if not targets:
        return web.json_response({"ok": False, "msg": "no live upstream socket", "groups": list(hub.group_status)})
    log.warning("admin: kill-upstream requested for groups=%s -- closing", list(targets))
    for ws in targets.values():
        asyncio.create_task(ws.close())
    return web.json_response({"ok": True, "closed": list(targets)})


DEBUG_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>OKX relay debug</title><style>
body{background:#0b0f14;color:#d7e2ee;font:13px/1.5 Consolas,monospace;margin:0;padding:12px}
h2{font-size:14px;color:#57d3a6;margin:4px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}
.card{background:#121820;border:1px solid #1e2a36;border-radius:6px;padding:8px}
.px{font-size:24px;color:#fff}.up{color:#57d3a6}.dn{color:#ff6b6b}
table{border-collapse:collapse;width:100%}td{padding:1px 4px}
#log{height:120px;overflow:auto;background:#0e141b;border:1px solid #1e2a36;padding:4px;font-size:11px;white-space:pre}
</style></head><body>
<h2>OKX relay debug (SSE) &mdash; backend smoke view; real UI lives at <a href="/" style="color:#57d3a6">/</a></h2>
<div id="top" class="card">connecting...</div>
<div class="grid" id="cards"></div>
<h2>recent trades</h2><div class="card"><table id="tr"></table></div>
<h2>raw event log</h2><div id="log"></div>
<script>
const es=new EventSource('/sse');const S={};let n=0;
function card(i){if(!S[i]){S[i]={};document.getElementById('cards').insertAdjacentHTML('beforeend',
 '<div class="card" id="c'+i+'"><b>'+i+'</b><div class="px" id="p'+i+'">-</div><div id="x'+i+'"></div></div>');}return i;}
es.onmessage=e=>{const m=JSON.parse(e.data);n++;
 document.getElementById('log').textContent=(m.t+' '+JSON.stringify(m).slice(0,110)+'\\n'+document.getElementById('log').textContent).slice(0,4000);
 if(m.t==='snapshot'){for(const k in m.insts){const v=m.insts[k];if(v.last!==undefined&&v.last!==null){card(k);document.getElementById('p'+k).textContent=v.last;
  document.getElementById('x'+k).textContent='24h '+(v.chg24h_pct??'-')+'%  vol '+Math.round(v.vol24h||0);}}}
 if(m.t==='ticker'){card(m.inst);document.getElementById('p'+m.inst).textContent=m.last;
  document.getElementById('x'+m.inst).textContent='24h '+m.chg24h_pct+'%  bid '+m.bid+' / ask '+m.ask+'  vol '+Math.round(m.vol24h||0);}
 if(m.t==='candle'){const el=document.getElementById('x'+m.inst);if(el)el.textContent='1m '+m.o+'/'+m.h+'/'+m.l+'/'+m.c+' confirm='+m.confirm;}
 if(m.t==='trades'){const t=document.getElementById('tr');for(const r of m.rows){t.insertAdjacentHTML('afterbegin',
  '<tr><td>'+(r.side==='buy'?'<span class=up>BUY</span>':'<span class=dn>SELL</span>')+'</td><td>'+r.px+'</td><td>'+r.sz+'</td><td>'+r.ts+'</td></tr>');}
  while(t.rows.length>25)t.deleteRow(-1);}
 if(m.t==='stats'){document.getElementById('top').innerHTML='upstream <b class="'+(m.upstream==='ok'?'up':'dn')+'">'+m.upstream+
  '</b> '+JSON.stringify(m.upstream_detail)+' | route '+m.route.mode+' | clients '+m.clients+' | msgs_per_s <b>'+m.msgs_per_s+
  '</b> | msgs_total '+m.msgs_total+' | reconnects '+JSON.stringify(m.reconnects)+' | sub '+m.sub.confirmed+'/'+m.sub.expected+
  ' | rows '+m.persist.rows+' | frames '+n+'/s';}
 if(m.t==='oi'){card(m.inst);const el=document.getElementById('x'+m.inst);if(el)el.textContent='OI $'+Math.round(m.oiUsd||0);}
 if(m.t==='funding'){card(m.inst);const el=document.getElementById('x'+m.inst);if(el)el.textContent='funding '+m.rate;}
};
</script></body></html>"""


async def debug_handler(request: web.Request) -> web.Response:
    return web.Response(text=DEBUG_PAGE, content_type="text/html", charset="utf-8")


async def index_handler(request: web.Request) -> web.StreamResponse:
    """Explicit index route: aiohttp's static handler answers 403 for '/' when show_index=False."""
    idx = Path(request.app["web_dir"]) / "index.html"
    if idx.exists():
        return web.FileResponse(idx, headers={"Cache-Control": "no-store"})
    return web.Response(
        text="<html><body style='font:14px monospace;background:#0b0f14;color:#d7e2ee'>"
             "index.html not found in web dir yet.<br>backend smoke view: <a style='color:#57d3a6' href='/debug'>/debug</a>"
             "</body></html>",
        content_type="text/html", charset="utf-8",
    )


# --------------------------------------------------------------------------------------
# bootstrap
# --------------------------------------------------------------------------------------
def install_loop_exception_handler(loop: asyncio.AbstractEventLoop, hub: Optional["Hub"] = None) -> None:
    """Never let a transient socket error from a dead client take the process down.

    Without this, an unhandled ConnectionResetError raised deep inside asyncio's transport
    machinery is reported by the DEFAULT handler (and, depending on where it surfaces, can
    end the process). Here such errors are downgraded to a counted warning.
    """

    def _handler(loop_: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        msg = str(context.get("message") or "")
        if is_transient_socket_error(exc):
            if hub is not None:
                hub.transient_errors += 1
            log.warning("event loop: transient socket error suppressed (%s): %s | %s",
                        type(exc).__name__, exc, msg)
            return
        if exc is None and ("socket.send()" in msg or "socket.accept()" in msg or "Unable to send" in msg):
            if hub is not None:
                hub.transient_errors += 1
            log.warning("event loop: transient socket condition suppressed: %s", msg)
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def write_fatal(args: argparse.Namespace, exc: BaseException, hub: Optional["Hub"] = None,
                persist: Optional["Persist"] = None, note: str = "") -> str:
    """A crash must leave evidence: full traceback + the state snapshot at death."""
    ts = datetime.now(CN).strftime("%Y%m%d_%H%M%S")
    path = Path(args.log_dir) / ("fatal_%s.log" % ts)
    lines = [
        "=" * 90,
        "FATAL at %s (CN)  pid=%d" % (datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S"), os.getpid()),
        "argv: %s" % (" ".join(sys.argv)),
        "note: %s" % note if note else "",
        "=" * 90,
        "--- traceback ---",
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    ]
    if hub is not None:
        try:
            lines.append("--- state snapshot ---")
            lines.append(jdumps(hub.stats_obj()))
            lines.append("--- rings ---")
            lines.append(jdumps({i: {"ticks": len(hub.ticks[i]), "trades": len(hub.trades[i]),
                                     "candles": len(hub.candles[i])} for i in ALL_INSTS}))
        except Exception as inner:
            lines.append("state snapshot failed: %r" % (inner,))
    if persist is not None:
        try:
            lines.append("--- persist ---")
            lines.append(jdumps({"rows_written": persist.rows_written, "bytes_written": persist.bytes_written,
                                 "pending_in_buffer": persist.pending(), "files": sorted(persist.files),
                                 "data_dir": str(persist.data_dir)}))
        except Exception as inner:
            lines.append("persist snapshot failed: %r" % (inner,))
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(l for l in lines if l is not None) + "\n")
        log.critical("FATAL evidence written to %s", path)
    except Exception as inner:
        log.critical("could not write fatal log %s: %r", path, inner)
    return str(path)


def setup_logging(log_dir: Path, verbose: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = TimedRotatingFileHandler(log_dir / "relay.log", when="midnight", backupCount=7, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    base = Path(__file__).resolve().parent
    root = base.parent
    p = argparse.ArgumentParser(description="OKX realtime relay (push only)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8910)
    p.add_argument("--data-dir", default=str(root / "data"))
    p.add_argument("--web-dir", default=str(root / "web"))
    p.add_argument("--log-dir", default=str(base / "logs"))
    p.add_argument("--upstream-url", default=OKX_PUBLIC_WS, help="public endpoint (tickers/trades/books/... )")
    p.add_argument("--business-url", default=OKX_BUSINESS_WS, help="business endpoint (candle1m)")
    p.add_argument("--proxy", default=None, help="force this proxy for upstream (else: probe direct, then env/local candidates)")
    p.add_argument("--no-proxy", action="store_true", help="force direct connect, never use a proxy")
    p.add_argument("--book-channel", default="books", choices=("books", "books5"))
    p.add_argument("--book-levels", type=int, default=20, help="levels pushed downstream (raw depth is reported too)")
    p.add_argument("--candle-channel", default="candle1m", help="business candle channel (candle1m/candle1H/...)")
    p.add_argument("--candle-bar", default="1m")
    p.add_argument("--candles-ring", type=int, default=300)
    p.add_argument("--prime-candles", type=int, default=300, help="startup REST backfill: candles to pull (max 300)")
    p.add_argument("--prime-trades", type=int, default=200, help="startup REST backfill: recent trades to pull")
    p.add_argument("--prime-timeout", type=float, default=10.0, help="per-request timeout for the startup backfill")
    p.add_argument("--snapshot-candles", type=int, default=300, help="candles sent in the snapshot frame")
    p.add_argument("--snapshot-ticks", type=int, default=600, help="1s tick bars sent in the snapshot frame")
    p.add_argument("--snapshot-trades", type=int, default=200, help="trades sent in the snapshot frame")
    p.add_argument("--ping-interval", type=float, default=20.0, help="send text 'ping' every N s")
    p.add_argument("--idle-timeout", type=float, default=45.0, help="no frame for N s -> force reconnect")
    p.add_argument("--flush-interval", type=float, default=5.0)
    p.add_argument("--trades-ring", type=int, default=500)
    p.add_argument("--ticks-ring", type=int, default=600, help="1s aggregated ticker bars kept (600 = 10 min)")
    p.add_argument("--no-prime", action="store_true", help="skip the startup one-shot REST prime")
    p.add_argument("--liveness-interval", type=float, default=60.0,
                   help="log an ALIVE line every N s (forensics: proves the process was up)")
    p.add_argument("--selector-loop", action="store_true",
                   help="use WindowsSelectorEventLoopPolicy instead of Proactor (both work; the "
                        "loop exception handler is installed either way)")
    p.add_argument("--selftest-fatal-after", type=float, default=0.0,
                   help="TEST HOOK: raise a deliberate unexpected exception after N seconds")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def liveness_loop(hub: "Hub", interval: float) -> None:
    """A periodic heartbeat line: without it, a silent log cannot be told apart from a dead
    process (the 20:02 crash could not be diagnosed from the log for exactly this reason)."""
    while True:
        await asyncio.sleep(interval)
        try:
            log.info("ALIVE uptime=%.0fs msgs_per_s=%.2f msgs_total=%d clients=%d sub=%d/%d "
                     "reconnects=%s suppressed_errs=%d write_errs=%d persist_rows=%d pending=%d ring=%s",
                     time.time() - hub.t0, hub.mps(), hub.msg_total, len(hub.clients),
                     sum(len(v) for v in hub.sub_ok.values()), sum(hub.sub_expected.values()) or 0,
                     dict(hub.reconnects), hub.transient_errors, hub.write_errors,
                     hub.persist.rows_written, hub.persist.pending(),
                     {i: (len(hub.ticks[i]), len(hub.trades[i])) for i in ALL_INSTS})
        except Exception:
            log.exception("liveness log failed")


async def amain(args: argparse.Namespace) -> int:
    global HUB
    if not port_available(args.host, args.port):
        msg = ("FATAL: port %s:%d is already in use -- refusing to start (no silent fallback). "
               "Find the owner with: netstat -ano | findstr :%d" % (args.host, args.port, args.port))
        log.critical(msg)
        print(msg, file=sys.stderr, flush=True)
        return 2

    persist = Persist(Path(args.data_dir), args.flush_interval)
    hub = Hub(args, persist)
    HUB = hub

    # CRITICAL: install BEFORE any client can connect. A dead client's RST must never be
    # able to end the process (see is_transient_socket_error / proactor_events.py:165).
    install_loop_exception_handler(asyncio.get_running_loop(), hub)

    await detect_route(hub, args)

    groups = [
        ("public", args.upstream_url, [
            ("tickers", ALL_INSTS),          # spot AND swap: swap gets a real last price too
            ("trades", SPOT),
            (args.book_channel, SPOT),
            ("bbo-tbt", SPOT),
            ("open-interest", SWAP),
            ("funding-rate", SWAP),
        ]),
        ("business", args.business_url, [(args.candle_channel, SPOT)]),
    ]

    app = web.Application()
    web_dir = Path(args.web_dir)
    web_dir.mkdir(parents=True, exist_ok=True)
    app["web_dir"] = web_dir
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/sse", sse_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/admin/kill-upstream", kill_upstream_handler)
    app.router.add_get("/debug", debug_handler)
    app.router.add_get("/", index_handler)
    app.router.add_static("/", web_dir, show_index=False, follow_symlinks=False)
    app.router.add_static("/data", persist.data_dir, show_index=True, follow_symlinks=False)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    try:
        await site.start()
    except OSError as exc:
        msg = "FATAL: cannot bind %s:%d -> %s" % (args.host, args.port, exc)
        log.critical(msg)
        print(msg, file=sys.stderr, flush=True)
        await runner.cleanup()
        return 2

    log.info("=" * 78)
    log.info("OKX relay listening on http://%s:%d/  (web=%s)", args.host, args.port, web_dir)
    log.info("  WS ws://%s:%d/ws   SSE http://%s:%d/sse   debug http://%s:%d/debug   health http://%s:%d/health",
             args.host, args.port, args.host, args.port, args.host, args.port, args.host, args.port)
    log.info("  data %s | logs %s", persist.data_dir, args.log_dir)
    log.info("  route mode=%s proxy=%s | book=%s(%d levels pushed) candle=%s",
             hub.route["mode"], mask_url(hub.route["proxy"]), args.book_channel, args.book_levels, args.candle_channel)
    log.info("=" * 78)

    tasks = [asyncio.create_task(hub.stats_loop(), name="stats"),
             asyncio.create_task(persist.flush_loop(), name="persist"),
             asyncio.create_task(liveness_loop(hub, args.liveness_interval), name="liveness")]
    for name, url, chans in groups:
        up = Upstream(hub, args, name, url, chans)
        tasks.append(asyncio.create_task(up.run(), name="upstream-" + name))
    if not args.no_prime:
        tasks.append(asyncio.create_task(prime_once(hub, args), name="prime"))

    fatal_exc: Optional[BaseException] = None
    try:
        while not hub.stopping:
            await asyncio.sleep(1.0)
            if args.selftest_fatal_after and (time.time() - hub.t0) > args.selftest_fatal_after:
                raise RuntimeError("SELFTEST: deliberate unexpected exception in the main loop "
                                   "(--selftest-fatal-after)")
    except asyncio.CancelledError:
        pass
    except BaseException as exc:                # noqa: BLE001 -- crash must leave evidence
        fatal_exc = exc
        log.critical("UNHANDLED EXCEPTION in main loop: %s: %s", type(exc).__name__, exc)
        log.critical("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        try:
            await persist.flush()               # do not lose the last 5 s of buffer
        except Exception:
            log.exception("flush during fatal handling failed")
        write_fatal(args, exc, hub, persist, note="unhandled exception in the main loop")
    finally:
        hub.stopping = True
        hub.close_all_bars()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await persist.flush()
        except Exception:
            log.exception("final flush failed")
        try:
            await runner.cleanup()
        except Exception:
            log.exception("runner cleanup failed")
        log.info("relay stopped. msgs_total=%d rows_written=%d bytes=%d pending=%d suppressed_errs=%d",
                 hub.msg_total, persist.rows_written, persist.bytes_written, persist.pending(),
                 hub.transient_errors)
    return 3 if fatal_exc is not None else 0


def main() -> int:
    args = parse_args()
    setup_logging(Path(args.log_dir), args.verbose)
    if args.selector_loop:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        log.info("event loop policy: WindowsSelectorEventLoopPolicy (--selector-loop)")
    else:
        log.info("event loop policy: default (ProactorEventLoop on Windows)")
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt -- shutting down")
        return 0
    except BaseException as exc:                # noqa: BLE001 -- last-resort crash evidence
        # Reaching here means the exception escaped asyncio.run() entirely.
        log.critical("FATAL: unhandled exception escaped asyncio.run(): %s: %s",
                     type(exc).__name__, exc)
        log.critical("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        write_fatal(args, exc, HUB, None, note="escaped asyncio.run() (process-level)")
        return 3


if __name__ == "__main__":
    sys.exit(main())
