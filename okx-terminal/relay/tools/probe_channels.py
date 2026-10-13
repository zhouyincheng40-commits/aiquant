#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One-off probe: compare OKX order-book channels (depth levels + raw frame bytes).

Read-only measurement, no persistence, no credentials. Used to decide which order-book
channel relay.py subscribes to.

NOTE (hard-won): on this network aiohttp CANNOT reach OKX directly -- the TLS ClientHello
is reset by the remote (WinError 64). aiohttp's ws_connect ignores trust_env, so the proxy
must be passed EXPLICITLY. websocket-client does not have this problem, which is why the
"direct connect works" observation was library-specific.

Run:  python tools/probe_channels.py [--proxy http://127.0.0.1:7890] [--seconds 20]
"""
import argparse
import asyncio
import json
import statistics
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import aiohttp  # noqa: E402

PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"


async def connect(sess, url, proxy, tries=3):
    last = None
    for attempt in range(1, tries + 1):
        try:
            return await sess.ws_connect(
                url, proxy=proxy, heartbeat=None, max_msg_size=0,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            )
        except Exception as exc:
            last = exc
            print("  connect attempt %d failed: %s" % (attempt, str(exc)[:110]), flush=True)
            await asyncio.sleep(1.5 * attempt)
    raise last


async def probe(url, channel, inst, proxy, seconds):
    got, sizes, bids, asks = [], [], set(), set()
    t_conn = time.time()
    async with aiohttp.ClientSession(trust_env=False) as sess:
        ws = await connect(sess, url, proxy)
        async with ws:
            conn_ms = int((time.time() - t_conn) * 1000)
            await ws.send_str(json.dumps({"op": "subscribe", "args": [{"channel": channel, "instId": inst}]}))
            t0 = time.time()
            while time.time() - t0 < seconds:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=seconds)
                except asyncio.TimeoutError:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                raw = msg.data
                obj = json.loads(raw)
                if obj.get("event"):
                    if obj["event"] == "error":
                        return {"channel": channel, "inst": inst, "ok": False, "error": obj}
                    continue
                if obj.get("arg", {}).get("channel") != channel:
                    continue
                got.append(time.time())
                sizes.append(len(raw.encode("utf-8")))
                for item in obj.get("data", []):
                    bids.add(len(item.get("bids") or []))
                    asks.add(len(item.get("asks") or []))
    span = (got[-1] - got[0]) if len(got) > 1 else 0
    return {
        "channel": channel,
        "inst": inst,
        "ok": True,
        "connect_ms": conn_ms,
        "msgs": len(got),
        "span_s": round(span, 1),
        "msgs_per_s": round(len(got) / span, 2) if span else 0,
        "bytes_avg": round(statistics.mean(sizes), 1) if sizes else 0,
        "bytes_min": min(sizes) if sizes else 0,
        "bytes_max": max(sizes) if sizes else 0,
        "kbps": round(statistics.mean(sizes) * (len(got) / span) / 1024, 1) if span and sizes else 0,
        "bid_levels": sorted(bids),
        "ask_levels": sorted(asks),
    }


async def main(args):
    print("== OKX channel probe | proxy=%s | direct-would-fail on aiohttp ==" % args.proxy, flush=True)
    jobs = [
        (PUBLIC, "books5", "BTC-USDT"),
        (PUBLIC, "books", "BTC-USDT"),
        (PUBLIC, "tickers", "BTC-USDT"),
        (BUSINESS, "candle1m", "BTC-USDT"),
    ]
    for url, ch, inst in jobs:
        try:
            r = await probe(url, ch, inst, args.proxy, args.seconds)
        except Exception as exc:
            r = {"channel": ch, "ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
        print(json.dumps(r, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--proxy", default="http://127.0.0.1:7890")
    p.add_argument("--seconds", type=float, default=20.0)
    asyncio.run(main(p.parse_args()))
