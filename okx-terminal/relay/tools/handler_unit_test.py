#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deterministic unit test of relay.install_loop_exception_handler.

Feeds the handler the EXACT context asyncio produces for the 20:02:03 crash:
    message   "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)"
    exception ConnectionResetError(10054, ...)

Expected: transient socket errors are swallowed + counted, everything else still reaches
the default handler (so real bugs stay visible).

Run: python tools/handler_unit_test.py
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relay  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="      [log] %(levelname)s %(message)s")


class FakeHub:
    def __init__(self):
        self.transient_errors = 0


def run():
    loop = asyncio.new_event_loop()
    hub = FakeHub()
    relay.install_loop_exception_handler(loop, hub)

    leaked = []
    loop.default_exception_handler = lambda ctx: leaked.append(ctx)

    cases = []

    # 1) the real crash context
    ctx1 = {"message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
            "exception": ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")}
    loop.call_exception_handler(ctx1)
    cases.append(("ConnectionResetError(10054)  [the 20:02 crash context]",
                  "suppressed" if not leaked else "LEAKED", hub.transient_errors))

    # 2) other transient variants
    for exc in (ConnectionAbortedError(10053, "x"), BrokenPipeError(32, "x"), OSError(64, "x")):
        before = len(leaked)
        loop.call_exception_handler({"message": "cb", "exception": exc})
        cases.append((type(exc).__name__ + "(%s)" % exc.errno,
                      "suppressed" if len(leaked) == before else "LEAKED", hub.transient_errors))

    # 3) message-only socket noise (no exception object)
    before = len(leaked)
    loop.call_exception_handler({"message": "socket.send() raised exception.", "exception": None})
    cases.append(("socket.send() raised exception (no exc object)",
                  "suppressed" if len(leaked) == before else "LEAKED", hub.transient_errors))

    # 4) a REAL bug must NOT be swallowed
    before = len(leaked)
    loop.call_exception_handler({"message": "Exception in callback boom", "exception": ValueError("real bug")})
    cases.append(("ValueError('real bug')  [must stay visible]",
                  "forwarded" if len(leaked) == before + 1 else "WRONGLY SUPPRESSED", hub.transient_errors))

    # 5) CancelledError must never be treated as transient
    cases.append(("is_transient_socket_error(CancelledError())",
                  str(relay.is_transient_socket_error(asyncio.CancelledError())), hub.transient_errors))

    print()
    print("%-52s %-22s %s" % ("case", "result", "suppressed_total"))
    print("-" * 92)
    ok = True
    for name, result, total in cases:
        good = result in ("suppressed", "forwarded", "False")
        ok = ok and good
        print("%-52s %-22s %d %s" % (name, result, total, "" if good else "  <-- BAD"))
    loop.close()
    print()
    print("HANDLER UNIT TEST: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
