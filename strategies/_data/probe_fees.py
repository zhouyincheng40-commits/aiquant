# -*- coding: utf-8 -*-
"""试着从官方文档核实 OKX 现货/SWAP 的 taker 费率与最小下单量。"""
import sys, re, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROXY = "http://127.0.0.1:7890"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

TARGETS = [
    "https://www.okx.com/fees",
    "https://www.okx.com/help/okx-trading-fees",
    "https://www.okx.com/docs-v5/en/",
]
for u in TARGETS:
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        html = _op.open(req, timeout=25).read().decode("utf-8", "replace")
        print("=" * 90)
        print("URL:", u, "len=", len(html))
        # 找费率相关的片段
        txt = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
        txt = re.sub(r"<style.*?</style>", " ", txt, flags=re.S | re.I)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = re.sub(r"\s+", " ", txt)
        pats = [r".{140}?0\.0[0-9]+\s?%.{140}?", r".{100}?taker.{160}?"]
        hits = []
        for p in pats:
            hits += re.findall(p, txt, flags=re.I)
        seen = set()
        for h in hits[:12]:
            h2 = h.strip()
            if h2 in seen:
                continue
            seen.add(h2)
            print("  ...", h2[:300])
        if not hits:
            print("  (未匹配到费率片段)")
    except Exception as e:
        print("URL:", u, "FAIL", type(e).__name__, str(e)[:120])
