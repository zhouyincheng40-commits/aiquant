#!/usr/bin/env python
"""app.py — POLY // TERMINAL (terminal edition, rich)

用法
----
    run.cmd --once            一次性输出一屏纯文本后退出（可粘贴/落库）
    run.cmd                    Live 模式：1s 重绘 + 300s 真取数，Ctrl-C 退出

    --once --color            带 ANSI 颜色输出
    --width 118               指定渲染宽度（默认 118）
    --refresh 600             覆盖取数间隔（秒）
    --edges <url>             指定可选 edge feed（否则用 feeds.py 内置口径）
    --rows 12                 每个列表面板最多显示的行数

编码（本机 chcp=936/GBK，必须处理，双保险）
-------------------------------------------
1) 本文件开头把 stdout/stderr reconfigure 成 utf-8
2) run.cmd 里 chcp 65001 + PYTHONUTF8=1 + PYTHONIOENCODING=utf-8
不做会直接 UnicodeEncodeError: 'gbk' codec can't encode character '\\xa2'（¢）

版式与口径沿用 web 版；数据全部来自 feeds.py，本文件只负责渲染。
零硬编码数字：空数据一律 -- / NO DATA / OFFLINE。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

# ── 编码双保险之一：必须在任何输出之前执行 ───────────────────────────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                     # noqa: BLE001
        pass

from rich import box                             # noqa: E402
from rich.cells import cell_len                  # noqa: E402
from rich.console import Console, Group          # noqa: E402
from rich.layout import Layout                   # noqa: E402
from rich.live import Live                       # noqa: E402
from rich.panel import Panel                     # noqa: E402
from rich.table import Table                     # noqa: E402
from rich.text import Text                       # noqa: E402

from feeds import CONFIG, get_feeds              # noqa: E402

# ── 配色语义（全页统一：绿=正/涨/买，红=负/跌/卖，琥珀=EDGE 非可行/计时）──
GREEN, RED, AMBER = "#00e07a", "#ff3b3b", "#ffb020"
DIM, DIM2, DIM3, WHITE = "#8a8a8a", "#565656", "#3a3a3a", "#ffffff"
TAG_RED_BG, TAG_GREEN_BG = "#e02020", "#00c46a"

BAR = "\u2502"          # │
CROSS = "\u271a"        # ✚
RULE = "\u2500"         # ─
ELLIPSIS = "\u2026"     # …

# 注意：中文是**双宽**字符，所有宽度计算必须走 cell_len（wcwidth），
# 用 len() 会让中文列错位、把边框撑破。


def tag_red(s: str) -> str:
    return f"[bold #000000 on {TAG_RED_BG}] {s} [/]"


def tag_green(s: str) -> str:
    return f"[bold #000000 on {TAG_GREEN_BG}] {s} [/]"


def tag_dim(s: str) -> str:
    """暗灰底标签：用于「扫到了但没有机会成立」这种**没有可执行结论**的状态。
    关键：不成立的机会绝不能显示成红色高亮，否则用户会以为可以动手。"""
    return f"[bold {DIM2} on #232323] {s} [/]"


def tone_color(tone) -> str:
    return {"up": GREEN, "down": RED, "strong": AMBER}.get(tone, DIM)


def col(s, width: int) -> str:
    """按**显示宽度**截断（中文按 2 列算）。"""
    s = "" if s is None else str(s)
    if width <= 0:
        return ""
    if cell_len(s) <= width:
        return s
    out = ""
    for ch in s:
        if cell_len(out + ch) > width - 1:
            break
        out += ch
    return out + ELLIPSIS


def pad_to(used: int, total: int) -> str:
    return " " * max(0, total - used)


def row(*segments) -> Text:
    """拼一行**永不换行**的 Text（超出裁掉，避免把版面撑破）。"""
    t = Text(no_wrap=True, overflow="crop")
    for seg in segments:
        if seg is None:
            continue
        s, style = seg
        t.append("" if s is None else str(s), style=style or "")
    return t


def sparkline(vals, width: int) -> str:
    """会话采样点的块状折线；真实点不足 2 个时返回空串（不画假的）。"""
    if not vals or len(vals) < 2:
        return ""
    blocks = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    lo, hi = min(vals), max(vals)
    span = hi - lo
    out = []
    for v in vals[-max(1, width):]:
        idx = (len(blocks) // 2) if span == 0 else int((v - lo) / span * (len(blocks) - 1))
        out.append(blocks[max(0, min(len(blocks) - 1, idx))])
    return "".join(out)


def title_str(left: str, right: str, inner: int) -> str:
    """面板标题：左 // TITLE（暗灰），右标签，中间补空格到 inner（按显示宽度算）。"""
    gap = max(1, inner - cell_len(left) - cell_len(right))
    return f"[{DIM}]{left}[/]" + " " * gap + right


# ══════════════════════ 顶栏 / 市场带 / 横幅 ══════════════════════
def render_topbar(snap: dict, width: int) -> Text:
    h = snap.get("header") or {}
    tx = h.get("text") or {}
    t = Text(no_wrap=True, overflow="crop")
    t.append("POLY ", style=f"bold {GREEN}")
    t.append("// ", style=DIM2)
    t.append("TERMINAL", style=f"bold {GREEN}")

    def sep():
        t.append(" " + BAR + " ", style=DIM3)

    sep(); t.append("\u03a3 TRACKED ", style=DIM2); t.append(tx.get("trackedPnl", "--"), style=WHITE)
    if h.get("trackedCount"):
        t.append(" \u2191", style=GREEN)
    sep(); t.append(tx.get("cashPnl", "--"),
                    style=GREEN if (h.get("cashPnl") is not None and h["cashPnl"] >= 0) else RED)
    t.append(" CASH PNL", style=DIM2)
    sep(); t.append(tx.get("edges", "--"), style=WHITE); t.append(" EDGES", style=DIM2)
    sep(); t.append(tx.get("win24h", "--"), style=WHITE); t.append(" WIN 24H", style=DIM2)
    sep(); t.append(tx.get("wallets", "--"), style=WHITE); t.append(" WALLETS", style=DIM2)
    sep(); t.append("SYNC " + tx.get("sync", "--"), style=AMBER)

    # 双状态：区分「网挂了(代理)」与「数据源挂了」
    if snap.get("proxyDown"):
        status, style = "\u25cf OFFLINE \u2014 PROXY DOWN", f"bold {RED}"
    else:
        off = (snap.get("meta") or {}).get("polyOffline", 0)
        status, style = (f"\u25cf DEGRADED {off}/6", f"bold {AMBER}") if off else ("\u25cf LIVE", f"bold {RED}")
    pad = max(0, width - t.cell_len - cell_len(status))
    t.append(" " * pad)
    t.append(status, style=style)
    return t


def render_band(snap: dict, width: int) -> Text:
    """CN 市场带。窄终端下按显示宽度贪心装填：装不下的指数省略，
    但**始终保留**「两市」与「DIRECT」标记。"""
    band = snap.get("cnBand")
    t = Text(no_wrap=True, overflow="crop")
    head = "GLOBAL " + BAR + " "
    t.append("GLOBAL ", style=DIM2)
    t.append(" " + BAR + " ", style=DIM3)
    if not band or not band.get("ok"):
        t.append("CN BAND OFFLINE", style=f"bold {RED}")
        return t

    tail = ""
    if band.get("amountYi") is not None:
        tail += f"\u4e24\u5e02 {round(band['amountYi']):d}\u4ebf  "
    tail += "DIRECT"
    # 预留 2 列给「有指数被省略」的省略号
    budget = width - cell_len(head) - cell_len(tail) - 2
    used, shown, dropped = 0, 0, 0
    for it in band.get("items", []):
        seg = f"{it['name']} {it['priceText']}" + (f" {it['chgText']}" if it.get("chgText") else "")
        seg_w = cell_len(seg) + 2
        if used + seg_w > budget:
            dropped = len(band["items"]) - shown
            break
        used += seg_w
        shown += 1
        t.append(it["name"], style=DIM)
        t.append(" " + it["priceText"], style=WHITE)
        if it.get("chgText"):
            st = GREEN if it["dir"] == "up" else (RED if it["dir"] == "down" else DIM)
            t.append(" " + it["chgText"], style=st)
        t.append("  ", style=DIM3)
    if dropped:
        t.append(ELLIPSIS + " ", style=DIM2)
    if band.get("amountYi") is not None:
        t.append("\u4e24\u5e02 ", style=DIM2)
        t.append(f"{round(band['amountYi']):d}\u4ebf", style=WHITE)
        t.append("  ", style=DIM3)
    t.append("DIRECT", style=GREEN)
    return t


def banner_text(snap: dict) -> Text:
    msg = "PROXY 127.0.0.1:7890 UNREACHABLE \u2014 POLYMARKET FEEDS DOWN"
    if (snap.get("cnBand") or {}).get("ok"):
        msg += " \u00b7 CN BAND STILL LIVE (DIRECT)"
    return row((msg, f"bold {RED}"))


# ══════════════════════ 六块面板 ══════════════════════
# 约定：cw = 面板内容宽度；tw = 标题可用宽度（比 cw 窄，因为边框/内边距）
def panel_scanner(snap: dict, cw: int, tw: int, max_rows: int) -> Panel:
    rows = snap.get("scanner") or []
    # 两层语义：红色「N EDGES FOUND」只给**可执行**的机会（扣费后净值为正）；
    # 扫到但都不成立时用暗灰标签，绝不让 NOT VIABLE 被计成 EDGE。
    n_viable = sum(1 for r in rows if r.get("viable"))
    n_total = len(rows)
    if n_viable > 0:
        tag = tag_red(f"{n_viable} EDGES FOUND")
    elif n_total > 0:
        tag = tag_dim(f"0 VIABLE / {n_total} SCAN")
    else:
        tag = tag_dim("0 EDGES")
    head = title_str("// MISPRICING SCANNER", tag, tw)
    g = Table.grid(padding=0)
    g.add_column(overflow="fold")
    if not rows:
        g.add_row(row(("NO DEFINITIONAL ARB IN SCAN RANGE", DIM2)))
    for r in rows[:max_rows]:
        g.add_row(row((col(r["market"], cw - 3), "default"), (" " + CROSS, f"bold {RED}")))
        g.add_row(row((f"{r.get('yesText','--')}  {r.get('noText','--')}", DIM)))
        g.add_row(row((r.get("priceText", "--"), f"bold {WHITE}"),
                      ("  " + r.get("fairText", "--"), GREEN)))
        label = r.get("edgeText", "--") + ("" if r.get("viable") else " NOT VIABLE")
        g.add_row(row((f" {label} ", tone_color(r.get("badgeTone"))), ("EDGE", DIM2)))
        g.add_row(row((r.get("sideText") or r.get("side") or "", DIM2)))
        g.add_row(row(("[BUY YES] [COPY TRADE] [IGNORE]", DIM3)))
    if len(rows) > max_rows:
        g.add_row(row((f"\u2026 {len(rows) - max_rows} more", DIM2)))
    meta = snap.get("meta") or {}
    if meta.get("scanRange"):
        g.add_row(row((f"scan {meta['scanRange']} mkts \u00b7 {meta.get('scanEvents', 0)} complete \u00b7 "
                       f"excl {meta.get('scanNonExclusive', 0)} nonexcl + {meta.get('scanIncomplete', 0)} incompl", DIM2)))
        g.add_row(row((col("range: keyset p1 limit=%d vol24hr" % meta["scanRange"], cw), DIM2)))
        if meta.get("scanFeeMissing"):
            g.add_row(row((f"fee rate missing on {meta['scanFeeMissing']} legs \u2192 conservative max used", DIM2)))
    return Panel(g, title=head, title_align="left", box=box.SQUARE, border_style=DIM3, padding=(0, 1))


def panel_wallets(snap: dict, cw: int, tw: int, max_rows: int) -> Panel:
    rows = snap.get("wallets") or []
    head = title_str("// WALLET TRACKER", tag_green(f"{len(rows)} TRACKED"), tw)
    g = Table.grid(padding=0)
    g.add_column(overflow="fold")
    if not rows:
        g.add_row(row(("NO DATA", DIM2)))
    for w in rows[:max_rows]:
        badge = "bold #ffffff on #6d28d9" if w["kind"] == "WHALE" else "bold #ffffff on #1d4ed8"
        pnl_style = GREEN if w.get("pnlDir") == "up" else RED
        kind_w = cell_len(w["kind"]) + 2
        pad = max(1, cw - cell_len(w["addressShort"]) - kind_w - cell_len(w["pnlText"]) - 1)
        g.add_row(row((w["addressShort"], "default"), (" ", DIM2),
                      (f" {w['kind']} ", badge), (" " * pad, ""), (w["pnlText"], pnl_style)))
        g.add_row(row(("  " + col(w.get("subText") or "--", cw - 2), DIM2)))
    return Panel(g, title=head, title_align="left", box=box.SQUARE, border_style=DIM3, padding=(0, 1))


def panel_markets(snap: dict, cw: int, tw: int, max_rows: int) -> Panel:
    rows = snap.get("markets") or []
    head = title_str("// LIVE MARKETS", tag_green(f"WATCHING {len(rows)}"), tw)
    g = Table.grid(padding=0)
    g.add_column(overflow="fold")
    if not rows:
        g.add_row(row(("NO DATA", DIM2)))
    name_w = max(8, cw - 15)
    for m in rows[:max_rows]:
        g.add_row(row((col(m["question"], name_w), "default"),
                      (f"{m.get('yesText','--'):>6}", WHITE),
                      (f"{m.get('noText','--'):>7}", WHITE)))
        cdir = m.get("chgDir")
        chg_style = GREEN if cdir == "up" else (RED if cdir == "down" else DIM2)
        chg_txt = m.get("chgText", "--")
        meta = col(m.get("meta1", "") + " \u00b7 " + m.get("meta2", ""),
                   max(8, cw - cell_len(chg_txt) - 2))
        g.add_row(row((meta, DIM2), ("  " + chg_txt, chg_style)))
        g.add_row(row((RULE * cw, DIM3)))
    if len(rows) > max_rows:
        g.add_row(row((f"\u2026 {len(rows) - max_rows} more", DIM2)))
    return Panel(g, title=head, title_align="left", box=box.SQUARE, border_style=DIM3, padding=(0, 1))


def panel_copy(snap: dict, cw: int, tw: int, max_rows: int) -> Panel:
    rows = snap.get("copy") or []
    src = (snap.get("meta") or {}).get("copySource")
    note = {"TRACKED_WALLETS": "LIVE MODE", "GLOBAL_FEED": "GLOBAL FEED"}.get(src, "--")
    head = title_str("// COPYTRADE ENGINE", tag_green(note), tw)
    g = Table.grid(padding=0)
    g.add_column(overflow="fold")
    if not rows:
        g.add_row(row(("NO DATA", DIM2)))
    for c in rows[:max_rows]:
        # 时间放地址行，动作行只放「动作 + 金额」，窄列里更耐压
        g.add_row(row((c["addressShort"], DIM), ("  " + c["sizeText"][-8:].strip(), DIM2)))
        act_w = max(6, cw - cell_len(c["sizeText"]) - 2)
        g.add_row(row((col(c["actionText"], act_w), GREEN if c["side"] == "BUY" else RED),
                      ("  " + c["sizeText"], WHITE)))
    return Panel(g, title=head, title_align="left", box=box.SQUARE, border_style=DIM3, padding=(0, 1))


def panel_pnl(snap: dict, cw: int, tw: int, max_rows: int) -> Panel:
    p = snap.get("pnl") or {}
    samples = p.get("samples") or []
    head = title_str("// PNL", f"[{DIM2}]{len(samples)} SAMPLE{'' if len(samples) == 1 else 'S'}[/]", tw)
    g = Table.grid(padding=0)
    g.add_column(overflow="fold")
    if not p.get("ok"):
        g.add_row(row(("NO DATA", f"bold {AMBER}")))
        g.add_row(row((col((p.get("reason") or "").upper(), cw), DIM2)))
        return Panel(g, title=head, title_align="left", box=box.SQUARE, border_style=DIM3, padding=(0, 1))
    g.add_row(row((p["text"], f"bold {GREEN if p.get('dir') == 'up' else RED}")))
    sp = sparkline(samples, max(4, cw))
    g.add_row(row((sp if sp else "\u2014", GREEN)))
    dstyle = GREEN if p.get("deltaDir") == "up" else (RED if p.get("deltaDir") == "down" else DIM)
    meta_w = max(4, cw - len(p.get("deltaText", "")) - 2)
    g.add_row(row((col(p.get("meta", ""), meta_w), DIM2), ("  " + p.get("deltaText", "--"), dstyle)))
    g.add_row(row(("session samples " + str(len(samples)), DIM2)))
    return Panel(g, title=head, title_align="left", box=box.SQUARE, border_style=DIM3, padding=(0, 1))


def panel_leaderboard(snap: dict, cw: int, tw: int, max_rows: int) -> Panel:
    rows = snap.get("leaderboard") or []
    head = title_str("// ARBER LEADERBOARD", tag_green(str(len(rows))), tw)
    g = Table.grid(padding=0)
    g.add_column(overflow="fold")
    if not rows:
        g.add_row(row(("NO DATA", DIM2)))
    for r in rows[:max_rows]:
        right = r.get("rightText", "--")
        name_w = max(6, cw - 4 - cell_len(r["text"]) - cell_len(right) - 3)
        g.add_row(row((f"#{r['rank']:<3}", DIM2), (col(r["label"], name_w), "default"),
                      (" ", ""), (r["text"], GREEN if r.get("dir") == "up" else RED),
                      ("  " + right, DIM)))
    has1d = sum(1 for r in rows if r.get("has1d"))
    g.add_row(row((f"24H overlap {has1d}/{len(rows)}", DIM2)))
    return Panel(g, title=head, title_align="left", box=box.SQUARE, border_style=DIM3, padding=(0, 1))


# ══════════════════════ 整屏拼装 ══════════════════════
COL_W = (42, 36)          # 前两列固定宽，第三列吃剩余


def build_layout(snap: dict, width: int, max_rows: int = 12) -> Layout:
    w1, w2 = COL_W
    w3 = max(24, width - w1 - w2)
    # tw = 标题可用宽度：要给面板边框 + 内边距 + 标题两侧空格留出余量
    cols = [(w1, panel_scanner, w1 - 8), (w2, panel_wallets, w2 - 8), (w3, panel_markets, w3 - 8)]
    cols2 = [(w1, panel_copy, w1 - 8), (w2, panel_pnl, w2 - 8), (w3, panel_leaderboard, w3 - 8)]

    rows = [Layout(name="top", size=1)]
    if snap.get("proxyDown"):
        rows.append(Layout(name="banner", size=1))
    rows += [Layout(name="band", size=1), Layout(name="main", ratio=3), Layout(name="bottom", ratio=2)]

    root = Layout()
    root.split_column(*rows)
    root["top"].update(render_topbar(snap, width))
    if snap.get("proxyDown"):
        root["banner"].update(banner_text(snap))
    root["band"].update(render_band(snap, width))

    root["main"].split_row(*[
        Layout(fn(snap, w - 4, tw, max_rows), name=f"m{i}")
        for i, (w, fn, tw) in enumerate(cols)
    ])
    root["bottom"].split_row(*[
        Layout(fn(snap, w - 4, tw, max_rows), name=f"b{i}")
        for i, (w, fn, tw) in enumerate(cols2)
    ])
    return root


# ══════════════════════ 入口 ══════════════════════
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="POLY // TERMINAL (terminal edition)")
    ap.add_argument("--once", action="store_true", help="print one screen and exit")
    ap.add_argument("--color", action="store_true", help="force ANSI colour with --once")
    ap.add_argument("--width", type=int, default=118, help="render width (default 118)")
    ap.add_argument("--refresh", type=int, default=None, help="override refresh seconds")
    ap.add_argument("--edges", type=str, default=None, help="optional edge feed URL")
    ap.add_argument("--rows", type=int, default=12, help="max rows per list panel")
    ap.add_argument("--proxy", type=str, default=None,
                    help="override proxy (default 127.0.0.1:7890); point at a dead port to "
                         "exercise the OFFLINE - PROXY DOWN path without touching the real proxy")
    ap.add_argument("--rounds", type=int, default=0,
                    help="live mode smoke test: render N ticks then exit")
    args = ap.parse_args(argv)

    if args.refresh:
        CONFIG["refresh_s"] = args.refresh

    # 本机环境实测两个坑，都会让 --color 静默失效：
    #   1) 环境变量带 NO_COLOR=1 —— rich 遵守该约定，即使 force_terminal=True 也不上色
    #   2) rich 15 在本机探测到 legacy_windows=True —— 走 Win32 控制台 API 写色，
    #      一旦 stdout 被重定向到文件/管道，ANSI 就一个字节都到不了目标
    # 用户显式传 --color 就是明确要求 ANSI，故两个开关都显式关掉。
    console = Console(width=args.width,
                      color_system=("truecolor" if args.color else None),
                      force_terminal=bool(args.color),
                      no_color=(False if args.color else None),
                      legacy_windows=(False if args.color else None),
                      highlight=False)
    feeds = get_feeds(**({"proxy": args.proxy} if args.proxy else {}))

    if args.once:
        snap = feeds.snapshot(force=True, edges_url=args.edges)
        console.print(build_layout(snap, args.width, args.rows))
        return 0

    # ── Live：1s 重绘；取数节奏由 feeds.py 的**每源 TTL** 决定 ──
    #    positions 60s（max-age=5，唯一真实时通道）/ keyset·trades 300s / lb-api 600s / CN 带 60s
    #    这里每 2s 问一次 snapshot()，它只重取"过期"的源，不会把 API 打爆。
    state = {"snap": None}

    def poll():
        try:
            state["snap"] = feeds.snapshot(force=False, edges_url=args.edges)
        except Exception as e:                       # noqa: BLE001 — 渲染层永不因取数崩
            sys.stderr.write(f"[feeds] {type(e).__name__}: {e}\n")

    poll()
    stop_evt = threading.Event()

    def worker():
        while not stop_evt.wait(2.0):
            poll()

    threading.Thread(target=worker, daemon=True).start()

    # ── 冒烟测试：跑 N 个渲染节拍后退出（用于验证「多轮不崩 + 无 UnicodeEncodeError」）──
    if args.rounds:
        with Live(console=console, screen=False, auto_refresh=False, vertical_overflow="crop") as live:
            for i in range(args.rounds):
                snap = state["snap"]
                live.update(Text("POLY // TERMINAL \u00b7 CONNECTING\u2026", style=DIM)
                            if snap is None else build_layout(snap, args.width, args.rows))
                live.refresh()
                sys.stderr.write(f"[round {i + 1}/{args.rounds}] rendered ok\n")
                if i + 1 < args.rounds:
                    time.sleep(2)
        stop_evt.set()
        return 0

    try:
        with Live(console=console, screen=True, refresh_per_second=1, vertical_overflow="crop") as live:
            while True:
                snap = state["snap"]
                live.update(Text("POLY // TERMINAL \u00b7 CONNECTING\u2026", style=DIM)
                            if snap is None else build_layout(snap, args.width, args.rows))
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
