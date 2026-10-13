#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Downstream client check: proves /ws and /sse both deliver snapshot + increments.

Run:  python tools/client_check.py --seconds 30
"""
import argparse
import asyncio
import json
import sys
import time
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import aiohttp  # noqa: E402

WS = "ws://127.0.0.1:8910/ws"
SSE = "http://127.0.0.1:8910/sse"


async def ws_client(seconds: float, out: dict):
    counts = Counter()
    samples = {}
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(trust_env=False) as s:
            async with s.ws_connect(WS, heartbeat=None, max_msg_size=2 ** 23) as ws:
                out["ws_connect_ms"] = int((time.time() - t0) * 1000)
                while time.time() - t0 < seconds:
                    m = await asyncio.wait_for(ws.receive(), timeout=seconds + 10)
                    if m.type != aiohttp.WSMsgType.TEXT:
                        break
                    o = json.loads(m.data)
                    counts[o.get("t")] += 1
                    if o.get("t") not in samples:
                        samples[o["t"]] = m.data[:230]
    except Exception as exc:
        out["ws_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
    span = max(0.001, time.time() - t0)
    out["ws_counts"] = dict(counts)
    out["ws_total"] = sum(counts.values())
    out["ws_msgs_per_s"] = round(sum(counts.values()) / span, 2)
    out["ws_samples"] = samples


async def sse_client(seconds: float, out: dict):
    counts = Counter()
    samples = {}
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(trust_env=False) as s:
            async with s.get(SSE) as r:
                out["sse_status"] = r.status
                out["sse_ctype"] = r.headers.get("Content-Type")
                out["sse_cors"] = r.headers.get("Access-Control-Allow-Origin")
                out["sse_connect_ms"] = int((time.time() - t0) * 1000)
                while time.time() - t0 < seconds:
                    try:
                        line = await asyncio.wait_for(r.content.readline(), timeout=seconds + 10)
                    except asyncio.TimeoutError:
                        break
                    if not line:
                        break
                    if not line.startswith(b"data: "):
                        continue
                    o = json.loads(line[6:].decode("utf-8"))
                    counts[o.get("t")] += 1
                    if o.get("t") not in samples:
                        samples[o["t"]] = line[6:].decode("utf-8")[:230]
    except Exception as exc:
        out["sse_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
    span = max(0.001, time.time() - t0)
    out["sse_counts"] = dict(counts)
    out["sse_total"] = sum(counts.values())
    out["sse_msgs_per_s"] = round(sum(counts.values()) / span, 2)
    out["sse_samples"] = samples


async def main(args):
    out = {}
    await asyncio.gather(ws_client(args.seconds, out), sse_client(args.seconds, out))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    ok = (
        out.get("ws_total", 0) > 0 and out.get("sse_total", 0) > 0
        and "snapshot" in out.get("ws_counts", {}) and "snapshot" in out.get("sse_counts", {})
        and "ticker" in out.get("ws_counts", {}) and "ticker" in out.get("sse_counts", {})
    )
    print("RESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=15.0)
    asyncio.run(main(p.parse_args()))
