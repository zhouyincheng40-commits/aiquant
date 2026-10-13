#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Client hard-disconnect tests (RST), two modes.

MODE repro : in-process aiohttp SSE server + clients that close with SO_LINGER 0 (=> TCP RST).
             Run twice, --no-handler and --with-handler, to show what the loop exception
             handler actually changes. Prints "REPRO SURVIVED" only if the process lives to
             the end (a missing marker + non-zero exit means the exception killed it).
MODE real  : hammer the RUNNING relay (/sse) with N hard-RST clients and report whether the
             pid is still alive and serving afterwards.

Run:
  python tools/conn_reset_test.py repro --no-handler
  python tools/conn_reset_test.py repro --with-handler
  python tools/conn_reset_test.py real --rounds 25
"""
import argparse
import asyncio
import socket
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

TRANSIENT = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError)


def rst_client(host, port, path, read_first=True, hold=0.4):
    """Connect, read a little, then abort the socket so the peer sees a TCP RST."""
    s = socket.create_connection((host, port), timeout=5)
    try:
        s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nAccept: text/event-stream\r\n"
                   "Connection: keep-alive\r\n\r\n" % (path, host)).encode())
        time.sleep(hold)
        if read_first:
            try:
                s.recv(8192)
            except Exception:
                pass
    finally:
        # SO_LINGER on + timeout 0 -> close() sends RST instead of FIN
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()


# ---------------------------------------------------------------- repro mode
async def repro(args):
    loop = asyncio.get_running_loop()
    suppressed = {"n": 0}

    if args.with_handler:
        def _handler(loop_, ctx):
            exc = ctx.get("exception")
            msg = str(ctx.get("message") or "")
            if isinstance(exc, TRANSIENT) or (exc is None and "socket" in msg):
                suppressed["n"] += 1
                print("   [handler] suppressed %s: %s" % (type(exc).__name__ if exc else "-", msg[:90]))
                return
            loop_.default_exception_handler(ctx)
        loop.set_exception_handler(_handler)
        print("   loop exception handler INSTALLED")
    else:
        print("   loop exception handler NOT installed (old behaviour)")

    async def sse(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        try:
            for i in range(200):
                await resp.write(b"data: " + (b"x" * 4096) + b"\n\n")
                await asyncio.sleep(0.05)
        except Exception:
            pass
        return resp

    app = web.Application()
    app.router.add_get("/sse", sse)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    print("   repro server on %s:%d" % (args.host, args.port))

    await asyncio.sleep(0.5)
    for i in range(args.rounds):
        await asyncio.to_thread(rst_client, args.host, args.port, "/sse")
        await asyncio.sleep(0.05)
    await asyncio.sleep(3.0)

    alive = loop.is_running() and not loop.is_closed()
    print("   loop.is_running()=%s  suppressed=%d" % (alive, suppressed["n"]))
    await runner.cleanup()
    print("REPRO SURVIVED (mode=%s)" % ("with-handler" if args.with_handler else "no-handler"))
    return 0


# ---------------------------------------------------------------- real mode
async def real(args):
    host, port = "127.0.0.1", args.relay_port
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=8)) as s:
        async with s.get("http://%s:%d/health" % (host, port)) as r:
            before = await r.json()
    print("   health BEFORE: ok=%s clients=%s msgs_total=%s uptime=%ss" %
          (before.get("ok"), before.get("clients"), before.get("msgs_total"), before.get("uptime_s")))

    t0 = time.time()
    for i in range(args.rounds):
        await asyncio.to_thread(rst_client, host, port, "/sse")
        await asyncio.sleep(0.05)
    print("   fired %d hard-RST clients in %.1fs" % (args.rounds, time.time() - t0))
    await asyncio.sleep(3.0)

    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get("http://%s:%d/health" % (host, port)) as r:
                after = await r.json()
        print("   health AFTER : ok=%s clients=%s msgs_total=%s uptime=%ss" %
              (after.get("ok"), after.get("clients"), after.get("msgs_total"), after.get("uptime_s")))
        survived = after.get("msgs_total", 0) > before.get("msgs_total", 0) and after.get("uptime_s", 0) >= before.get("uptime_s", 0)
        print("REAL RESULT: %s (same process still serving, msgs still flowing)" % ("SURVIVED" if survived else "FAILED"))
        return 0 if survived else 1
    except Exception as exc:
        print("REAL RESULT: FAILED -- relay unreachable after the test: %s: %s" % (type(exc).__name__, exc))
        return 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("repro", "real"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8955, help="repro server port")
    p.add_argument("--relay-port", type=int, default=8910)
    p.add_argument("--rounds", type=int, default=15)
    p.add_argument("--with-handler", action="store_true")
    p.add_argument("--no-handler", action="store_true")
    a = p.parse_args()
    if a.mode == "repro":
        return asyncio.run(repro(a))
    return asyncio.run(real(a))


if __name__ == "__main__":
    sys.exit(main())
