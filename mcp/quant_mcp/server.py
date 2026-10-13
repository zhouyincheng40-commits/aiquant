# -*- coding: utf-8 -*-
"""
quant_mcp.server — AI量化模式 真实量化 MCP 服务器 (stdio JSON-RPC 2.0)

零第三方 MCP SDK 依赖：仅用标准库手写 MCP stdio 协议。
若安装了 `mcp` SDK 也不使用它，保证在裸环境同样可运行。

启动:  <python> server.py            (stdio JSON-RPC, stdout 只输出 JSON-RPC)
自检:  <python> server.py --selftest (逐个真实调用工具核心函数)

日志只写 stderr；stdout 必须是纯 JSON-RPC。
"""
from __future__ import annotations

# ============================================================================
# 段 0. 引导：屏蔽第三方库噪声，保证 stdout 干净
# ============================================================================
import os
import sys
import io
import json
import time
import glob as _glob
import math
import re
import socket
import ssl
import gzip
import shutil
import traceback
import datetime as _dt
import threading
import warnings
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Callable, Dict, List, Optional, Tuple

warnings.filterwarnings("ignore")

# 强制 stdout 为 UTF-8，且任何第三方 print 都被重定向到 stderr
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")
except Exception:
    pass

_STDOUT_LOCK = threading.Lock()
_REAL_STDOUT = sys.stdout


class _StderrProxy(io.TextIOBase):
    """把误写到 stdout 的内容重定向到 stderr，保护 JSON-RPC 通道。"""

    def write(self, s):  # noqa: D401
        return sys.stderr.write(s)

    def flush(self):
        try:
            sys.stderr.flush()
        except Exception:
            pass


sys.stdout = _StderrProxy()

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aiquant"
SERVER_VERSION = "1.0.0"

# 工作区路径：AIQUANT_HOME > 本文件向上两级（mcp/quant_mcp -> 仓库根）> 旧本机默认
ROOT = (
    os.environ.get("AIQUANT_HOME")
    or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    or r"D:\量化"
)
CACHE_DIR = os.path.join(ROOT, "data", "cache")
EXPORT_DIR = os.path.join(ROOT, "data", "exports")
LOG_DIR = os.path.join(ROOT, "data", "logs")

TOOL_TIMEOUT = 120.0          # 单次工具调用最长 120 秒
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
def _detect_proxy() -> str:
    """代理探测：环境变量 QUANT_MCP_PROXY > 系统代理 > 本机常见端口实测。

    注意：本机实测 127.0.0.1:7890 可用、7897 拒绝连接；因此不再硬编码 7897，
    并且只在直连失败后才回退到代理（调用方 allow_proxy_fallback）。
    """
    env = (os.environ.get("QUANT_MCP_PROXY") or "").strip()
    if env:
        return env if "://" in env else ("http://" + env)
    try:
        sysp = urllib.request.getproxies() or {}
    except Exception:  # noqa: BLE001
        sysp = {}
    for k in ("https", "http"):
        v = (sysp.get(k) or "").strip()
        if v and "127.0.0.1" not in v and "localhost" not in v:
            return v if "://" in v else ("http://" + v)
    cand = (sysp.get("https") or sysp.get("http") or "").strip()
    if cand and "://" not in cand:
        cand = "http://" + cand
    for port in (7890, 7897, 10809, 1080):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.35):
                return "http://127.0.0.1:%d" % port
        except Exception:  # noqa: BLE001
            continue
    return cand


PROXY = _detect_proxy()


def log(msg: str) -> None:
    """日志只进 stderr。"""
    try:
        sys.stderr.write("[quant_mcp] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def out(msg: str = "") -> None:
    """CLI（--selftest/--version/--tools）输出到真实 stdout。
    服务器 JSON-RPC 模式不使用此函数，保证协议通道纯净。"""
    with _STDOUT_LOCK:
        _REAL_STDOUT.write(str(msg) + "\n")
        _REAL_STDOUT.flush()


def ensure_dirs() -> None:
    for d in (CACHE_DIR, EXPORT_DIR, LOG_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            log("mkdir failed %s: %r" % (d, e))


# ============================================================================
# 段 1. 结构化错误
# ============================================================================
class QuantError(Exception):
    """带降级方案的结构化错误。"""

    def __init__(self, reason: str, fallback: str = "", detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.fallback = fallback
        self.detail = detail

    def to_payload(self, tool: str = "") -> Dict[str, Any]:
        return {
            "ok": False,
            "tool": tool,
            "error": self.reason,
            "detail": self.detail[:600],
            "fallback": self.fallback or "无可用降级方案",
        }


def err_text(tool: str, exc: BaseException) -> str:
    if isinstance(exc, QuantError):
        p = exc.to_payload(tool)
        lines = ["❌ %s 取数/计算失败" % tool, "", "**原因**: %s" % p["error"]]
        if p["detail"]:
            lines.append("**细节**: %s" % p["detail"])
        lines.append("**降级方案**: %s" % p["fallback"])
        return "\n".join(lines)
    return "❌ %s 内部错误: %s: %s\n降级方案: 请重试；若持续失败请检查网络与标的代码。" % (
        tool, type(exc).__name__, str(exc)[:400])


# ============================================================================
# 段 2. HTTP 工具层（重试 / 限速 / 多 Host 轮换）
# ============================================================================
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_RATE_LOCK = threading.Lock()
_LAST_HIT: Dict[str, float] = {}


def _throttle(host_key: str, min_gap: float) -> None:
    with _RATE_LOCK:
        last = _LAST_HIT.get(host_key, 0.0)
        now = time.time()
        wait = min_gap - (now - last)
        if wait > 0:
            time.sleep(min(wait, 5.0))
        _LAST_HIT[host_key] = time.time()


def http_get(url: str, *, referer: str = "", encoding: str = "utf-8",
             timeout: float = 11.0, retries: int = 3, min_gap: float = 0.0,
             host_key: str = "", proxy: Optional[str] = None,
             allow_proxy_fallback: bool = True) -> str:
    """HTTP GET，带重试、限速、可选代理兜底。返回解码文本。"""
    host_key = host_key or urllib.parse.urlsplit(url).netloc
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer

    last_err: Optional[BaseException] = None
    for attempt in range(max(1, retries)):
        if min_gap > 0:
            _throttle(host_key, min_gap)
        try:
            req = urllib.request.Request(url, headers=headers)
            opener = None
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            resp = (opener.open(req, timeout=timeout) if opener
                    else urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX))
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode(encoding, "replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.6 * (attempt + 1))

    # 直连全部失败 -> 尝试代理（仅一次）
    if allow_proxy_fallback and proxy:
        try:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(handler)
            resp = opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode(encoding, "replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise QuantError(
        "网络请求失败: %s" % url,
        "检查网络/代理；或稍后重试（部分站点有频率限制）。",
        "%s: %s" % (type(last_err).__name__, str(last_err)[:200]))


def http_get_json(url: str, **kw) -> Any:
    txt = http_get(url, **kw)
    try:
        return json.loads(txt)
    except Exception as e:
        raise QuantError("接口返回非 JSON", "稍后重试或更换数据源。",
                         "%s | 前 200 字符: %s" % (e, txt[:200]))


def probe(url: str, timeout: float = 8.0) -> Tuple[bool, int, str]:
    """探测可达性，返回 (是否通, 毫秒, 说明)。

    HTTP 4xx/5xx 也算“通”——说明 TCP+TLS 已建立、对端在应答；
    只有连接层异常（超时/拒绝/DNS/RST）才判定不通。
    """
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
        urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX).read(64)
        return True, int((time.time() - t0) * 1000), "通"
    except urllib.error.HTTPError as e:
        return True, int((time.time() - t0) * 1000), "通(HTTP %s)" % e.code
    except Exception as e:  # noqa: BLE001
        return False, int((time.time() - t0) * 1000), "%s" % type(e).__name__


# ============================================================================
# 段 3. 通用格式化 / 数值工具
# ============================================================================
def beijing_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _spec(nd: int, kind: str = "f", pct: bool = False):
    """构造保留 nd 位小数的格式化器。

    修复点：旧写法 ("%%.%df%%" % nd) 依赖 "%%" 在 old-style 格式串里退化，
    在 Python 3.11 实际得到 '%.2f%'（尾部单个 %），再套一层 % 即
    ValueError: incomplete format。改用 str.format，结果确定。
    """
    tpl = "{:." + str(int(nd)) + kind + "}" + ("%" if pct else "")
    return tpl.format


def fmt_num(v: Any, nd: int = 2, dash: str = "-") -> str:
    if v is None:
        return dash
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(f) or math.isinf(f):
        return dash
    return _spec(nd)(f)


def fmt_pct(v: Any, nd: int = 2, dash: str = "-") -> str:
    """数值 -> 'x.xx%'（入参已是百分数，如 1.23 表示 1.23%）。"""
    if v is None:
        return dash
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(f) or math.isinf(f):
        return dash
    return _spec(nd, pct=True)(f)


def fmt_big(v: Any) -> str:
    """大数转 亿/万 文本。"""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(f) or math.isinf(f):
        return "-"
    a = abs(f)
    if a >= 1e8:
        return "%.2f亿" % (f / 1e8)
    if a >= 1e4:
        return "%.2f万" % (f / 1e4)
    return "%.2f" % f


def md_table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_(无数据)_"
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def safe_float(v: Any) -> Optional[float]:
    if v is None or v == "" or v == "-":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def parse_date(s: Any, default: Optional[str] = None) -> Optional[str]:
    """解析多种日期写法 -> 'YYYYMMDD'。"""
    if s is None or str(s).strip() == "":
        return default
    t = str(s).strip().replace("/", "-").replace(".", "-")
    if len(t) == 8 and t.isdigit():
        return t
    for f in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(t, f).strftime("%Y%m%d")
        except ValueError:
            continue
    digits = "".join(ch for ch in t if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else default


# ============================================================================
# 段 4. 标的代码规范化
# ============================================================================
# 主要指数别名
INDEX_ALIAS = {
    "sh000001": ("sh000001", "上证指数"), "000001.sh": ("sh000001", "上证指数"),
    "上证指数": ("sh000001", "上证指数"), "上证": ("sh000001", "上证指数"),
    "sz399001": ("sz399001", "深证成指"), "399001.sz": ("sz399001", "深证成指"),
    "深证成指": ("sz399001", "深证成指"),
    "sz399006": ("sz399006", "创业板指"), "399006.sz": ("sz399006", "创业板指"),
    "创业板指": ("sz399006", "创业板指"), "创业板": ("sz399006", "创业板指"),
    "sh000688": ("sh000688", "科创50"), "000688.sh": ("sh000688", "科创50"),
    "科创50": ("sh000688", "科创50"),
    "bj899050": ("bj899050", "北证50"), "899050.bj": ("bj899050", "北证50"),
    "北证50": ("bj899050", "北证50"),
    "hkHSI": ("hkHSI", "恒生指数"), "恒生": ("hkHSI", "恒生指数"),
    "恒生指数": ("hkHSI", "恒生指数"), "HSI": ("hkHSI", "恒生指数"),
    "usIXIC": ("usIXIC", "纳斯达克"), "纳斯达克": ("usIXIC", "纳斯达克"),
    "纳指": ("usIXIC", "纳斯达克"),
    "usSPX": ("usSPX", "标普500"), "标普": ("usSPX", "标普500"), "标普500": ("usSPX", "标普500"),
    "usDJI": ("usDJI", "道琼斯"), "道琼斯": ("usDJI", "道琼斯"),
    "btc": ("BTCUSDT", "比特币"), "BTCUSDT": ("BTCUSDT", "比特币"),
    "比特币": ("BTCUSDT", "比特币"),
}


class Sym:
    """规范化后的标的。kind: a|hk|us|crypto|index"""

    __slots__ = ("raw", "kind", "code", "name", "tencent", "sina", "em_secid", "crypto",
                 "tencent_k", "sina_k")

    def __init__(self, raw: str, kind: str, code: str, name: str = "",
                 tencent: str = "", sina: str = "", em_secid: str = "",
                 crypto: str = "", tencent_k: str = "", sina_k: str = ""):
        self.raw = raw
        self.kind = kind
        self.code = code
        self.name = name
        self.tencent = tencent
        self.sina = sina
        self.em_secid = em_secid
        self.crypto = crypto
        self.tencent_k = tencent_k or tencent
        self.sina_k = sina_k or sina

    def __repr__(self) -> str:  # pragma: no cover
        return "<Sym %s %s %s>" % (self.kind, self.code, self.raw)


def _a_share(raw: str, digits: str) -> Sym:
    """数字代码 -> 交易所前缀。6 开头=sh，0/3 开头=sz，4/8/92=bj。"""
    if digits.startswith(("60", "68", "9", "5", "11", "13")):
        mk = "sh"
    elif digits.startswith(("4", "8", "92")):
        mk = "bj"
    else:
        mk = "sz"
    pre = mk + digits
    em = ("1." if mk == "sh" else ("0." if mk == "sz" else "0.")) + digits
    return Sym(raw, "a", digits, "", tencent=pre, sina=pre, em_secid=em,
               tencent_k=pre, sina_k=pre)


# 腾讯 K 线只认 sh/sz 前缀（bj 无数据），新浪 K 线认 sh/sz/bj
_TENCENT_K_MARKETS = ("sh", "sz")


def _kline_code(sym: Sym, provider: str) -> str:
    """给出某数据源可用的规范代码，如 sh600519 / sz000001 / bj430047 / hk00700。"""
    if sym.kind == "a":
        mk = sym.tencent[:2] if sym.tencent[:2] in ("sh", "sz", "bj") else "sz"
        if provider == "tencent" and mk not in _TENCENT_K_MARKETS:
            raise QuantError("腾讯日线不覆盖北交所标的: %s" % sym.raw,
                             "改用新浪源或 akshare 源。")
        return mk + sym.code
    if sym.kind == "index":
        if sym.tencent[:2] in ("sh", "sz", "bj"):
            return sym.tencent
        raise QuantError("腾讯/新浪日线不覆盖该指数: %s" % sym.raw,
                         "A 股指数用 sh000001 这类写法；海外指数请用 q_quote。")
    if sym.kind == "hk":
        return "hk" + sym.code
    if sym.kind == "us":
        return "us" + (sym.tencent[2:] if sym.tencent.startswith("us") else sym.code)
    raise QuantError("不支持的 K 线市场: %s" % sym.kind, "改用 A 股/港股/指数标的。")


def normalize_symbol(raw: str) -> Sym:
    """支持 600519 / sh600519 / sz000001 / 000001.SZ / 00700.HK / BTCUSDT / BTC-USDT 等。"""
    if raw is None:
        raise QuantError("标的代码为空", "传入如 600519 / sh600519 / 00700.HK / BTCUSDT。")
    s = str(raw).strip()
    if not s:
        raise QuantError("标的代码为空", "传入如 600519 / sh600519 / 00700.HK / BTCUSDT。")

    # 别名（指数/加密）
    key = s.replace(" ", "")
    if key in INDEX_ALIAS:
        canon, name = INDEX_ALIAS[key]
        if canon.startswith("hk"):
            return Sym(raw, "index", "HSI", name, tencent="hkHSI", sina="hkHSI",
                       em_secid="100.HSI", tencent_k="hkHSI", sina_k="hkHSI")
        if canon.startswith("us"):
            mp = {"usIXIC": ("IXIC", "100.NDX"), "usSPX": ("SPX", "100.SPX"),
                  "usDJI": ("DJI", "100.DJIA")}
            code, em = mp[canon]
            return Sym(raw, "index", code, name, tencent=code, sina="gb_$" + code.lower(),
                       em_secid=em, tencent_k="us" + code, sina_k="gb_" + code.lower())
        if canon.startswith("btc"):
            return Sym(raw, "crypto", "BTCUSDT", name, crypto="BTCUSDT")
        if canon.startswith("sh") or canon.startswith("sz") or canon.startswith("bj"):
            return Sym(raw, "index", canon[2:], name, tencent=canon, sina=canon,
                       em_secid=("1." if canon.startswith("sh") else "0.") + canon[2:],
                       tencent_k=canon, sina_k=canon)

    up = s.upper().replace("_", "-")

    # 加密: BTCUSDT / BTC-USDT / BTC/USDT
    for sep in ("-", "/"):
        if sep in up:
            a, b = up.split(sep, 1)
            if b in ("USDT", "USDC", "USD", "BUSD") and a.isalpha():
                return Sym(raw, "crypto", a + "USDT", "", crypto=a + "USDT")
    if up.endswith("USDT") and up[:-4].isalpha():
        return Sym(raw, "crypto", up, "", crypto=up)

    # 后缀式: 600519.SH / 000001.SZ / 00700.HK / 600519.SS
    if "." in s:
        left, right = s.split(".", 1)
        rs = right.strip().upper()
        if rs in ("SH", "SS", "XSHG"):
            return _a_share(raw, "".join(ch for ch in left if ch.isdigit()))
        if rs in ("SZ", "XSHE"):
            return _a_share(raw, "".join(ch for ch in left if ch.isdigit()))
        if rs in ("BJ",):
            return _a_share(raw, "".join(ch for ch in left if ch.isdigit()))
        if rs in ("HK", "XHKG"):
            d = "".join(ch for ch in left if ch.isdigit()).zfill(5)
            return Sym(raw, "hk", d, "", tencent="hk" + d, sina="rt_hk" + d,
                       em_secid="116." + d, tencent_k="hk" + d, sina_k="hk" + d)
        if rs in ("US", "O", "N"):
            return Sym(raw, "us", left.strip().upper(), "", tencent="us" + left.strip().upper(),
                       sina="gb_" + left.strip().lower(),
                       tencent_k="us" + left.strip().upper(),
                       sina_k="gb_" + left.strip().lower())

    # 前缀式: sh600519 / sz000001 / hk00700 / bj920000
    low = s.lower()
    for p in ("sh", "sz", "bj"):
        if low.startswith(p) and low[2:].isdigit():
            return _a_share(raw, low[2:])
    if low.startswith("hk") and low[2:].isdigit():
        d = low[2:].zfill(5)
        return Sym(raw, "hk", d, "", tencent="hk" + d, sina="rt_hk" + d, em_secid="116." + d,
                   tencent_k="hk" + d, sina_k="hk" + d)
    if low.startswith("us") and len(low) > 2:
        t = s[2:].upper()
        return Sym(raw, "us", t, "", tencent="us" + t, sina="gb_" + t.lower(),
                   tencent_k="us" + t, sina_k="gb_" + t.lower())

    # 纯数字
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits and len(digits) == len(s):
        if len(digits) == 5:  # 港股
            return Sym(raw, "hk", digits, "", tencent="hk" + digits,
                       sina="rt_hk" + digits, em_secid="116." + digits,
                       tencent_k="hk" + digits, sina_k="hk" + digits)
        if len(digits) == 6:
            return _a_share(raw, digits)
        raise QuantError("无法识别的代码长度: %s" % s,
                         "A 股用 6 位（600519），港股用 5 位（00700）。")

    # 纯字母 -> 美股
    if s.replace(".", "").isalpha() and len(s) <= 6:
        t = s.upper()
        return Sym(raw, "us", t, "", tencent="us" + t, sina="gb_" + t.lower(),
                   tencent_k="us" + t, sina_k="gb_" + t.lower())
    raise QuantError("无法识别的标的写法: %s" % s,
                     "支持 600519 / sh600519 / 000001.SZ / 00700.HK / BTCUSDT。")


# ============================================================================
# 段 5. 缓存层（parquet 优先，csv 兜底）
# ============================================================================
def _cache_path(key: str, ext: str = ".parquet") -> str:
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in key)
    return os.path.join(CACHE_DIR, safe + ext)


def cache_read_df(key: str, ttl: float = 86400.0):
    import pandas as pd
    p1, p2 = _cache_path(key, ".parquet"), _cache_path(key, ".csv")
    for p in (p1, p2):
        if os.path.exists(p):
            try:
                if ttl is not None:
                    age = time.time() - os.path.getmtime(p)
                    if age > ttl:
                        return None
                if p.endswith(".parquet"):
                    df = pd.read_parquet(p)
                else:
                    df = pd.read_csv(p)
                if df is not None and len(df) > 0:
                    return df
            except Exception:
                continue
    return None


def cache_write_df(key: str, df) -> str:
    import pandas as pd
    ensure_dirs()
    p1 = _cache_path(key, ".parquet")
    try:
        df.to_parquet(p1, index=False)
        return p1
    except Exception:
        p2 = _cache_path(key, ".csv")
        df.to_csv(p2, index=False, encoding="utf-8-sig")
        return p2


def dir_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except Exception:
                pass
    return total


def cache_detail() -> Dict[str, Any]:
    ensure_dirs()
    res = {"dir": CACHE_DIR, "files": 0, "bytes": 0, "by_ext": {}}
    if not os.path.isdir(CACHE_DIR):
        return res
    for f in os.listdir(CACHE_DIR):
        fp = os.path.join(CACHE_DIR, f)
        if not os.path.isfile(fp):
            continue
        try:
            sz = os.path.getsize(fp)
        except Exception:
            sz = 0
        res["files"] += 1
        res["bytes"] += sz
        ext = os.path.splitext(f)[1] or "(none)"
        res["by_ext"][ext] = res["by_ext"].get(ext, 0) + sz
    return res


# ============================================================================
# 段 6. 行情快照（腾讯 / 新浪 / EM / 加密）
# ============================================================================
def _tencent_quote(syms: List[Sym]) -> Dict[str, Dict[str, Any]]:
    """腾讯 qt.gtimg.cn，GBK 解码。一次可查多个。"""
    codes = [s.tencent for s in syms if s.tencent]
    if not codes:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        txt = http_get(url, encoding="gbk", referer="https://gu.qq.com/",
                       timeout=9.0, retries=2, host_key="qt.gtimg.cn")
        for line in txt.split(";"):
            line = line.strip()
            if "=" not in line or '"' not in line:
                continue
            head, _, body = line.partition("=")
            key = head.replace("v_", "").strip()
            f = body.strip().strip('"').split("~")
            if len(f) < 35:
                continue
            out[key] = {
                "code": f[2], "name": f[1], "price": safe_float(f[3]),
                "prev_close": safe_float(f[4]), "open": safe_float(f[5]),
                "volume": safe_float(f[6]),            # 手
                "time": f[30] if len(f) > 30 else "",
                "change": safe_float(f[31]), "change_pct": safe_float(f[32]),
                "high": safe_float(f[33]), "low": safe_float(f[34]),
                "amount": safe_float(f[37]) * 1e4 if len(f) > 37 and safe_float(f[37]) else None,
                "turnover": safe_float(f[38]) if len(f) > 38 else None,
                "pe": safe_float(f[39]) if len(f) > 39 else None,
                "amplitude": safe_float(f[43]) if len(f) > 43 else None,
                "pb": safe_float(f[46]) if len(f) > 46 else None,
                "mktcap": safe_float(f[45]) * 1e8 if len(f) > 45 and safe_float(f[45]) else None,
                "volratio": safe_float(f[49]) if len(f) > 49 else None,
                "source": "腾讯",
            }
    return out


_INDEX_IDS = {"hs300": "000300", "zz500": "000905", "sz50": "000016"}


def _cached_constituents(idx: str) -> List[str]:
    """缓存中的指数成分股代码列表；没有则返回 []。"""
    dfc = cache_read_df("cons_%s" % idx, ttl=7 * 86400)
    if dfc is None or len(dfc) == 0:
        return []
    col = None
    for c in list(dfc.columns):
        if str(c) in ("成分券代码", "品种代码", "代码", "证券代码"):
            col = c
            break
    if col is None and len(dfc.columns) > 4:
        col = dfc.columns[4]
    if col is None:
        return []
    out = []
    for x in dfc[col].tolist():
        d = "".join(ch for ch in str(x) if ch.isdigit())
        if len(d) == 6:
            out.append(d)
    return out


def _index_constituents(index_key: str) -> List[str]:
    """指数成分股代码（缓存 -> akshare 联网）。失败返回 []。"""
    idx = _INDEX_IDS.get((index_key or "hs300").lower(), index_key)
    codes = _cached_constituents(idx)
    if codes:
        return codes
    try:
        import akshare as ak
        dfc = ak.index_stock_cons_csindex(symbol=idx)
        if dfc is not None and len(dfc):
            cache_write_df("cons_%s" % idx, dfc)
            return _cached_constituents(idx)
    except Exception as e:  # noqa: BLE001
        log("constituents fetch failed (%s): %r" % (idx, e))
    return []


def _sina_quote(syms: List[Sym]) -> Dict[str, Dict[str, Any]]:
    """新浪 hq.sinajs.cn，需 Referer。"""
    codes = [s.sina for s in syms if s.sina]
    if not codes:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        url = "https://hq.sinajs.cn/list=" + ",".join(chunk)
        txt = http_get(url, encoding="gbk", referer="https://finance.sina.com.cn",
                       timeout=9.0, retries=2, host_key="hq.sinajs.cn")
        for line in txt.split(";"):
            line = line.strip()
            if "hq_str_" not in line or '"' not in line:
                continue
            head = line.split("hq_str_", 1)[1].split("=")[0].strip()
            body = line.split('"')[1] if '"' in line else ""
            f = body.split(",")
            if len(f) < 4 or not body:
                continue
            try:
                if head.startswith("rt_hk"):
                    out[head] = {"code": head[5:], "name": f[1], "open": safe_float(f[2]),
                                 "prev_close": safe_float(f[3]), "high": safe_float(f[4]),
                                 "low": safe_float(f[5]), "price": safe_float(f[6]),
                                 "change": safe_float(f[7]), "change_pct": safe_float(f[8]),
                                 "amount": safe_float(f[11]), "time": f[17] if len(f) > 17 else "",
                                 "source": "新浪"}
                elif head.startswith("s_"):
                    out[head] = {"code": head[2:], "name": f[0], "price": safe_float(f[1]),
                                 "change": safe_float(f[2]), "change_pct": safe_float(f[3]),
                                 "volume": safe_float(f[4]), "amount": safe_float(f[5]) * 1e4,
                                 "source": "新浪"}
                elif head.startswith("gb_"):
                    out[head] = {"code": head[3:].upper(), "name": f[0], "price": safe_float(f[1]),
                                 "change_pct": safe_float(f[2]), "change": safe_float(f[4]),
                                 "open": safe_float(f[5]), "high": safe_float(f[6]),
                                 "low": safe_float(f[7]), "prev_close": safe_float(f[26]) if len(f) > 26 else None,
                                 "time": f[3] if len(f) > 3 else "", "source": "新浪"}
                else:
                    out[head] = {"code": head[2:], "name": f[0], "open": safe_float(f[1]),
                                 "prev_close": safe_float(f[2]), "price": safe_float(f[3]),
                                 "high": safe_float(f[4]), "low": safe_float(f[5]),
                                 "volume": safe_float(f[8]), "amount": safe_float(f[9]),
                                 "time": (f[30] + " " + f[31]) if len(f) > 31 else "",
                                 "source": "新浪"}
            except Exception:
                continue
    return out


# 新浪外汇/加密现货代码表（直连可用，参考 hq.sinajs.cn 外汇行情）
_SINA_FX = {
    "BTCUSDT": ("fx_sbtcusd", "比特币兑美元"),
    "ETHUSDT": ("fx_sethusd", "以太坊兑美元"),
    "USDCNY": ("fx_susdcny", "在岸人民币"),
    "EURUSD": ("fx_seudusd", "欧元兑美元"),
    "XAUUSD": ("fx_sxauusd", "黄金兑美元"),
}


def _sina_fx_quotes(pairs: List[str]) -> Dict[str, Dict[str, Any]]:
    """新浪外汇/加密实时快照（GBK 解码，需 Referer）。

    字段（列表）：[0]=时间 [1]=最新价 [2]=买价 [3]=昨收 [4]=成交量
                 [5]=最低 [6]=最高 [7]=今开 [8]=最新价 [9]=名称
                 [10]=涨跌幅% [11]=涨跌额 [12]=振幅% [17]=日期
    """
    req: List[str] = []
    back: Dict[str, str] = {}
    for p in pairs:
        sin = _SINA_FX.get(p.upper())
        if sin:
            req.append(sin[0])
            back[sin[0]] = p.upper()
    if not req:
        return {}
    txt = http_get("https://hq.sinajs.cn/list=" + ",".join(req),
                   referer="https://finance.sina.com.cn", encoding="gbk",
                   timeout=10.0, retries=3, min_gap=0.25, host_key="hq.sinajs")
    out: Dict[str, Dict[str, Any]] = {}
    for line in txt.splitlines():
        if "hq_str_" not in line or '="' not in line:
            continue
        head = line.split("hq_str_", 1)[1].split("=", 1)[0].strip()
        body = line.split('="', 1)[1].rstrip('";\r\n ')
        f = body.split(",")
        if len(f) < 11 or not f[1]:
            continue
        key = back.get(head)
        if not key:
            continue
        out[key] = {
            "code": key, "name": f[9] if len(f) > 9 else key,
            "price": safe_float(f[1]), "prev_close": safe_float(f[3]),
            "open": safe_float(f[7]), "high": safe_float(f[6]), "low": safe_float(f[5]),
            "change_pct": safe_float(f[10]), "change": safe_float(f[11]),
            "volume": safe_float(f[4]),
            "time": ((f[17] + " " + f[0]) if len(f) > 17 and f[17] else
                     (f[0] if f else "")),
            "source": "新浪外汇",
        }
    return out


def _binance_quote(pairs: List[str]) -> Dict[str, Dict[str, Any]]:
    """币安 24h ticker 兜底（直连被 RST，走代理通常可用）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for p in pairs:
        sj = http_get_json("https://api.binance.com/api/v3/ticker/24hr?symbol=" + p.upper(),
                           timeout=8.0, retries=1, host_key="binance", proxy=PROXY)
        if not isinstance(sj, dict) or not sj.get("lastPrice"):
            continue
        out[p.upper()] = {
            "code": p.upper(), "name": p.upper(),
            "price": safe_float(sj.get("lastPrice")),
            "prev_close": safe_float(sj.get("prevClosePrice")),
            "open": safe_float(sj.get("openPrice")),
            "high": safe_float(sj.get("highPrice")), "low": safe_float(sj.get("lowPrice")),
            "change_pct": safe_float(sj.get("priceChangePercent")),
            "change": safe_float(sj.get("priceChange")),
            "volume": safe_float(sj.get("volume")),
            "time": beijing_now().strftime("{}-{}-{} {}:{}:{}".format(
                "%Y", "%m", "%d", "%H", "%M", "%S")),
            "source": "Binance(代理)",
        }
    return out


def _em_index_quote(syms: List[Sym]) -> Dict[str, Dict[str, Any]]:
    """东方财富 push2 指数/外盘快照。"""
    ids = [s.em_secid for s in syms if s.em_secid]
    if not ids:
        return {}
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&secids="
           + ",".join(ids)
           + "&fields=f1,f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18,f20,f21")
    js = http_get_json(url, referer="https://quote.eastmoney.com/",
                       timeout=10.0, retries=3, min_gap=0.5, host_key="push2.em")
    out: Dict[str, Dict[str, Any]] = {}
    for d in ((js or {}).get("data") or {}).get("diff") or []:
        mk = d.get("f13")
        code = str(d.get("f12") or "")
        secid = ("%s.%s" % (mk, code)) if mk is not None else ""
        out[secid] = {"code": code, "name": d.get("f14"),
                      "price": safe_float(d.get("f2")),
                      "change_pct": safe_float(d.get("f3")),
                      "change": safe_float(d.get("f4")),
                      "volume": safe_float(d.get("f5")),
                      "amount": safe_float(d.get("f6")),
                      "high": safe_float(d.get("f15")), "low": safe_float(d.get("f16")),
                      "open": safe_float(d.get("f17")), "prev_close": safe_float(d.get("f18")),
                      "source": "东方财富"}
    return out


def _tencent_spot_table(index_key: str):
    """腾讯批量快照兜底（直连可用）。

    指数成分股 -> 腾讯 qt.gtimg.cn 快照 -> 中文字段 DataFrame。
    字段与 `_fetch_spot_table` 其它源的返回保持一致：
      代码/名称/最新价/涨跌幅/成交量/成交额/换手率/市盈率-动态/量比/总市值/市净率
    """
    import pandas as pd
    codes = _index_constituents(index_key)
    if not codes:
        raise QuantError("腾讯快照兜底缺少股票池（%s 成分股为空）" % index_key,
                         "先联网获取一次成分股，或直接传 pool=600519,000001 这类列表。")
    syms = [normalize_symbol(c) for c in codes]
    got = _tencent_quote(syms)
    if not got:
        raise QuantError("腾讯快照返回空", "稍后重试。")
    rows = []
    for s in syms:
        q = got.get(s.tencent)
        if not q:
            continue
        rows.append({
            "代码": s.code, "名称": q.get("name"),
            "最新价": q.get("price"), "涨跌幅": q.get("change_pct"),
            "成交量": q.get("volume"), "成交额": q.get("amount"),
            "换手率": q.get("turnover"), "市盈率-动态": q.get("pe"),
            "量比": q.get("volratio"), "总市值": q.get("mktcap"),
            "市净率": q.get("pb"),
        })
    if not rows:
        raise QuantError("腾讯快照解析为空", "稍后重试。")
    return pd.DataFrame(rows), "腾讯批量快照(直连)"


def fetch_quotes(symbols: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """多源取快照。返回 (结果列表, 失败列表)。"""
    syms: List[Sym] = []
    failed: List[Dict[str, str]] = []
    for raw in symbols:
        try:
            syms.append(normalize_symbol(raw))
        except QuantError as e:
            failed.append({"symbol": str(raw), "reason": e.reason})

    a_syms = [s for s in syms if s.kind == "a"]
    other = [s for s in syms if s.kind != "a" and s.kind != "crypto"]
    crypto = [s for s in syms if s.kind == "crypto"]

    got: Dict[str, Dict[str, Any]] = {}
    if a_syms or other:
        try:
            got.update(_tencent_quote(a_syms + other))
        except Exception as e:  # noqa: BLE001
            log("tencent quote failed: %r" % (e,))
    if other:
        try:
            got.update(_em_index_quote(other))
        except Exception as e:  # noqa: BLE001
            log("em index quote failed: %r" % (e,))

    # 逐标的兜底新浪
    missing = [s for s in (a_syms + other) if s.tencent not in got and s.sina not in got]
    if missing:
        try:
            sina_got = _sina_quote(missing)
            for s in missing:
                for k in (s.sina, s.tencent):
                    if k in sina_got:
                        got[k] = sina_got[k]
                        break
        except Exception as e:  # noqa: BLE001
            log("sina quote failed: %r" % (e,))

    # 加密：新浪外汇直连首选 -> 币安代理兜底
    if crypto:
        pairs = [s.crypto or s.code for s in crypto]
        crypto_errs: List[str] = []
        try:
            got.update(_sina_fx_quotes(pairs))
        except Exception as e:  # noqa: BLE001
            crypto_errs.append("新浪外汇: %s: %s" % (type(e).__name__, str(e)[:120]))
        left = [p for p in pairs if p not in got]
        if left:
            try:
                got.update(_binance_quote(left))
            except Exception as e:  # noqa: BLE001
                crypto_errs.append("Binance(代理 %s): %s: %s"
                                   % (PROXY, type(e).__name__, str(e)[:120]))
        crypto_note = " ;; ".join(crypto_errs)

    results: List[Dict[str, Any]] = []
    for s in syms:
        if s.kind == "crypto":
            q = got.get(s.crypto or s.code)
            if not q:
                failed.append({"symbol": s.raw,
                               "reason": ("加密行情全部数据源失败。已试：新浪外汇 "
                                          "hq.sinajs.cn/list=fx_sbtcusd（直连）"
                                          "-> Binance 代理兜底。"
                                          + ("错误: " + crypto_note if crypto_note else
                                             "该币种不在新浪外汇代码表中，需代理访问 Binance。"))})
                continue
            q = dict(q)
            q["symbol"] = s.raw
            q["kind"] = s.kind
            results.append(q)
            continue
        q = got.get(s.tencent) or got.get(s.sina) or got.get(s.em_secid)
        if not q:
            failed.append({"symbol": s.raw, "reason": "所有数据源均未返回该标的"})
            continue
        q = dict(q)
        q["symbol"] = s.raw
        q["kind"] = s.kind
        results.append(q)
    return results, failed


# ============================================================================
# 段 7. 历史 K 线（腾讯主源 -> 新浪次源 -> akshare -> 东方财富兜底 + parquet 缓存）
# ============================================================================
KLINE_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]

_EM_KLT = {"daily": "101", "weekly": "102", "monthly": "103"}


def _to_kdf(rows):
    """[[date, open, close, high, low, volume], ...] -> DataFrame(KLINE_COLS)。"""
    import pandas as pd
    if not rows:
        raise QuantError("K 线为空", "扩大日期区间或更换标的。")
    df = pd.DataFrame(rows)
    df.columns = ["date", "open", "high", "low", "close", "volume"]
    df["amount"] = None
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df[KLINE_COLS]
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(df) == 0:
        raise QuantError("K 线全为空值", "更换标的或日期区间。")
    return df


def _tencent_kline(sym: Sym, period: str, start: str, end: str, limit: int):
    """腾讯日线主源（直连可用）。

    接口: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
    参数: param=<code>,day,,,<n>,qfq，code 形如 sh600519 / sz000001
    返回: data[code]["qfqday"]（指数为 data[code]["day"]），每行
          [日期, 开, 收, 高, 低, 量] —— 注意第 3 列是收盘，不是最高。
    """
    import pandas as pd
    code = _kline_code(sym, "tencent")
    req_n = max(int(limit), 20) + 40
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param={code},day,,,{n},qfq".format(code=code, n=req_n))
    js = http_get_json(url, timeout=12.0, retries=3, min_gap=0.25, host_key="ifzq.gtimg")
    node = (js or {}).get("data") or {}
    node = node.get(code) or {}
    series = node.get("qfqday") or node.get("day") or []
    rows = []
    for k in series:
        if not isinstance(k, (list, tuple)) or len(k) < 6:
            continue
        try:
            rows.append([k[0], float(k[1]), float(k[3]), float(k[4]),
                         float(k[2]), float(k[5])])
        except (TypeError, ValueError):
            continue
    if not rows:
        raise QuantError("腾讯日线返回空数据: %s" % code,
                         "确认代码是否正确；北交所标的请用新浪源。")
    df = _to_kdf(rows)
    if period in ("weekly", "monthly"):
        df = resample_ohlc(df, period)
    return df


def _sina_kline(sym: Sym, period: str, start: str, end: str, limit: int):
    """新浪日线次源（直连可用）。

    接口: money.finance.sina.com.cn/quotes_service/api/json_v2.php/
          CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=N
    返回标准 JSON 数组，但字段顺序是 day/open/high/low/close/volume。
    """
    import pandas as pd
    code = _kline_code(sym, "sina")
    req_n = max(int(limit), 20) + 40
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={n}"
           .format(code=code, n=req_n))
    txt = http_get(url, referer="https://finance.sina.com.cn", encoding="utf-8",
                   timeout=12.0, retries=3, min_gap=0.25, host_key="money.finance.sina")
    txt = txt.strip()
    if txt.startswith("var "):          # 兼容 JSONP 包装
        i = txt.find("(")
        txt = txt[i + 1: txt.rfind(")")]
    js = _loads_loose(txt)
    if not isinstance(js, list) or not js:
        raise QuantError("新浪日线返回空数据: %s" % code,
                         "确认代码是否正确；或改用腾讯/akshare 源。")
    rows = []
    for it in js:
        if not isinstance(it, dict):
            continue
        try:
            rows.append([it.get("day"), safe_float(it.get("open")),
                         safe_float(it.get("high")), safe_float(it.get("low")),
                         safe_float(it.get("close")), safe_float(it.get("volume"))])
        except Exception:  # noqa: BLE001
            continue
    df = _to_kdf(rows)
    if period in ("weekly", "monthly"):
        df = resample_ohlc(df, period)
    return df


def _loads_loose(txt: str):
    """宽松 JSON：修复 {'day':1} 这类 key 无引号的非标准返回。"""
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        pass
    fixed = re.sub(r"([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', txt)
    fixed = re.sub(r"'([^']*)'", r'"\1"', fixed)
    return json.loads(fixed)


def _em_kline(sym: Sym, period: str, start: str, end: str, limit: int):
    """东方财富 K 线兜底。"""
    import pandas as pd
    klt = _EM_KLT.get(period, "101")
    secid = sym.em_secid
    if not secid:
        raise QuantError("该标的无东方财富 secid", "改用 akshare 源。")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid={secid}&fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56,f57"
           "&klt={klt}&fqt=1&beg={beg}&end={end}&lmt={lmt}"
           .format(secid=secid, klt=klt, beg=start or "0", end=end or "20500101",
                   lmt=max(limit, 100)))
    js = http_get_json(url, referer="https://quote.eastmoney.com/",
                       timeout=12.0, retries=2, host_key="push2his.em")
    kl = ((js or {}).get("data") or {}).get("klines") or []
    rows = []
    for item in kl:
        p = item.split(",")
        if len(p) < 7:
            continue
        rows.append({"date": p[0], "open": safe_float(p[1]), "close": safe_float(p[2]),
                     "high": safe_float(p[3]), "low": safe_float(p[4]),
                     "volume": safe_float(p[5]), "amount": safe_float(p[6])})
    if not rows:
        raise QuantError("东方财富 K 线为空", "稍后重试或改用其他标的。")
    df = pd.DataFrame(rows)[KLINE_COLS]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df


def _ak_kline(sym: Sym, period: str, start: str, end: str):
    """akshare 主源。周/月线由日线重采样，避免接口差异。"""
    import pandas as pd
    import akshare as ak
    s = start or "19900101"
    e = end or beijing_now().strftime("%Y%m%d")
    if sym.kind == "a":
        df = ak.stock_zh_a_hist(symbol=sym.code, period="daily", start_date=s,
                                end_date=e, adjust="qfq")
        ren = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
               "最低": "low", "成交量": "volume", "成交额": "amount"}
    elif sym.kind == "hk":
        df = ak.stock_hk_hist(symbol=sym.code, period="daily", start_date=s,
                              end_date=e, adjust="qfq")
        ren = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
               "最低": "low", "成交量": "volume", "成交额": "amount"}
    elif sym.kind == "index":
        df = ak.index_zh_a_hist(symbol=sym.code, period="daily", start_date=s, end_date=e)
        ren = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
               "最低": "low", "成交量": "volume", "成交额": "amount"}
    else:
        raise QuantError("akshare 不覆盖该市场: %s" % sym.kind,
                         "美股/加密请改用 q_quote 或 q_crypto_klines。")
    if df is None or len(df) == 0:
        raise QuantError("akshare 返回空数据: %s" % sym.raw,
                         "确认代码与日期区间；非交易日区间可能为空。")
    df = df.rename(columns=ren)
    need = [c for c in KLINE_COLS if c in df.columns]
    df = df[need].copy()
    if "amount" not in df.columns:
        df["amount"] = None
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    if period in ("weekly", "monthly"):
        df = resample_ohlc(df, period)
    return df[KLINE_COLS]


def resample_ohlc(df, period: str):
    """日线 -> 周/月线。"""
    import pandas as pd
    d = df.copy()
    d["_dt"] = pd.to_datetime(d["date"])
    d = d.set_index("_dt")
    rule = "W-FRI" if period == "weekly" else "ME"
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "amount" in d.columns:
        agg["amount"] = "sum"
    out = d.resample(rule).agg(agg).dropna(subset=["close"]).reset_index()
    out["date"] = out["_dt"].dt.strftime("%Y-%m-%d")
    return out.drop(columns=["_dt"])


def get_klines(symbol: str, period: str = "daily", start: Optional[str] = None,
               end: Optional[str] = None, limit: int = 250,
               use_cache: bool = True, ttl: float = 43200.0):
    """获取历史 K 线 DataFrame（date/open/high/low/close/volume/amount）。

    数据源优先级（本机实测）：
      1. 腾讯日线 web.ifzq.gtimg.cn  —— 直连可用（首选）
      2. 新浪日线 money.finance.sina.com.cn —— 直连可用（次选）
      3. akshare（stock_zh_a_daily 走新浪等）
      4. 东方财富 push2his —— 本机连接被重置，仅作最后兜底
    """
    import pandas as pd
    sym = normalize_symbol(symbol)
    if sym.kind == "crypto":
        raise QuantError("加密 K 线请使用 q_crypto_klines",
                         "该工具不取加密货币日线。")
    period = (period or "daily").lower()
    if period not in ("daily", "weekly", "monthly"):
        raise QuantError("period 仅支持 daily/weekly/monthly", "改用 daily。")

    key = "kline_%s_%s_%s_%s" % (sym.kind, sym.code, period, (start or "0"))
    if use_cache:
        df = cache_read_df(key, ttl=ttl)
        if df is not None and len(df) > 0:
            df = _slice_dates(df, start, end)
            if len(df) > 0:
                out = df.tail(limit).reset_index(drop=True)
                out.attrs["source"] = "本地缓存(data/cache)"
                return out

    chain = [
        ("腾讯日线(ifzq.gtimg.cn)", lambda: _tencent_kline(sym, period, start, end, limit)),
        ("新浪日线(money.finance.sina)", lambda: _sina_kline(sym, period, start, end, limit)),
        ("akshare", lambda: _ak_kline(sym, period, start, end)),
        ("东方财富(push2his)", lambda: _em_kline(sym, period, start, end, limit)),
    ]
    errs: List[str] = []
    df = None
    src_used = ""
    for name, fn in chain:
        try:
            cand = fn()
        except Exception as e:  # noqa: BLE001
            errs.append("%s -> %s: %s" % (name, type(e).__name__, str(e)[:160]))
            continue
        if cand is not None and len(cand) > 0:
            df, src_used = cand, name
            break
        errs.append("%s -> 返回空数据" % name)

    if df is None or len(df) == 0:
        raise QuantError(
            "全部 K 线数据源失败: %s" % symbol,
            "已尝试 腾讯日线/新浪日线/akshare/东方财富 四个源，全部失败。"
            "排查：1) 代码是否正确（A 股 6 位、港股 5 位）；"
            "2) 网络是否可直连 web.ifzq.gtimg.cn；"
            "3) 若需代理，设置 QUANT_MCP_PROXY=http://127.0.0.1:7890（本机 7897 未运行）。",
            " ;; ".join(errs))
    try:
        cache_write_df(key, df)
    except Exception as e:  # noqa: BLE001
        log("cache write failed: %r" % (e,))
    out = _slice_dates(df, start, end)
    if len(out) == 0:
        raise QuantError(
            "日期区间内无 K 线: %s（源 %s 返回 %d 根，区间 %s~%s）"
            % (symbol, src_used, len(df), df["date"].iloc[0], df["date"].iloc[-1]),
            "放宽 start/end；注意腾讯日线约覆盖最近 2.5 年。")
    out.attrs["source"] = src_used
    return out.tail(limit).reset_index(drop=True)


def _slice_dates(df, start: Optional[str], end: Optional[str]):
    import pandas as pd
    out = df
    if start:
        s = parse_date(start)
        if s:
            sd = "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
            out = out[out["date"] >= sd]
    if end:
        e = parse_date(end)
        if e:
            ed = "%s-%s-%s" % (e[:4], e[4:6], e[6:8])
            out = out[out["date"] <= ed]
    return out.reset_index(drop=True)


# ============================================================================
# 段 8. 技术指标（本地计算，TA-Lib 优先，纯 pandas 兜底）
# ============================================================================
try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

HAS_TALIB = False
try:
    import talib  # noqa: F401
    HAS_TALIB = True
except Exception:
    HAS_TALIB = False


def ind_ma(close, n: int):
    import pandas as pd
    return pd.Series(close).rolling(n).mean()


def ind_ema(close, n: int):
    import pandas as pd
    return pd.Series(close).ewm(span=n, adjust=False).mean()


def ind_macd(close, fast=12, slow=26, signal=9):
    import pandas as pd
    c = pd.Series(close)
    if HAS_TALIB:
        import talib
        dif, dea, hist = talib.MACD(c.values, fast, slow, signal)
        return pd.Series(dif, index=c.index), pd.Series(dea, index=c.index), pd.Series(hist, index=c.index)
    ef, es = c.ewm(span=fast, adjust=False).mean(), c.ewm(span=slow, adjust=False).mean()
    dif = ef - es
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea, (dif - dea) * 2


def ind_rsi(close, n: int = 14):
    import pandas as pd
    c = pd.Series(close)
    if HAS_TALIB:
        import talib
        return pd.Series(talib.RSI(c.values, n), index=c.index)
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, 1e-12))


def ind_boll(close, n: int = 20, k: float = 2.0):
    import pandas as pd
    c = pd.Series(close)
    mid = c.rolling(n).mean()
    sd = c.rolling(n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def ind_atr(high, low, close, n: int = 14):
    import pandas as pd
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    if HAS_TALIB:
        import talib
        return pd.Series(talib.ATR(h.values, l.values, c.values, n), index=c.index)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def ind_kdj(high, low, close, n=9, m1=3, m2=3):
    import pandas as pd
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    ln = l.rolling(n).min()
    hn = h.rolling(n).max()
    rsv = (c - ln) / (hn - ln).replace(0, 1e-12) * 100
    k = rsv.ewm(alpha=1.0 / m1, adjust=False).mean()
    d = k.ewm(alpha=1.0 / m2, adjust=False).mean()
    return k, d, 3 * k - 2 * d


# ============================================================================
# 段 9. 回测引擎（次日开盘成交 + 手续费 + 滑点）
# ============================================================================
def compute_metrics(equity, trades, dates, hold_days: int, cash0: float) -> Dict[str, Any]:
    """由净值曲线与交易明细计算绩效指标。"""
    import numpy as np
    eq = np.asarray(equity, dtype=float)
    if len(eq) < 2 or cash0 <= 0:
        return {}
    total_ret = eq[-1] / cash0 - 1.0
    n = len(eq)
    years = max(n / 252.0, 1e-9)
    ann = (eq[-1] / cash0) ** (1.0 / years) - 1.0 if eq[-1] > 0 else -1.0
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    max_dd = float(dd.min())
    rets = np.diff(eq) / eq[:-1]
    sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    sharpe = float(np.mean(rets) / sd * math.sqrt(252)) if sd > 1e-12 else 0.0
    downside = rets[rets < 0]
    dsd = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
    sortino = float(np.mean(rets) / dsd * math.sqrt(252)) if dsd > 1e-12 else 0.0
    calmar = float(ann / abs(max_dd)) if abs(max_dd) > 1e-9 else 0.0

    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    win_rate = len(wins) / len(closed) if closed else 0.0
    avg_w = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
    avg_l = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
    pf = (avg_w / avg_l) if avg_l > 1e-12 else (float("inf") if avg_w > 0 else 0.0)
    hold_ratio = hold_days / float(n) if n else 0.0
    return {
        "total_return": total_ret, "annual_return": ann, "max_drawdown": max_dd,
        "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
        "win_rate": win_rate, "profit_factor": pf,
        "trade_count": len(closed), "hold_days": hold_days,
        "hold_ratio": hold_ratio, "final_equity": float(eq[-1]),
        "bars": n, "start": str(dates[0]), "end": str(dates[-1]),
        "avg_win": avg_w, "avg_loss": avg_l,
    }


def run_backtest(df, strategy: str = "ma", fast: int = 5, slow: int = 20,
                 cash: float = 100000.0, fee_bps: float = 3.0, slippage_bps: float = 2.0,
                 params: Optional[Dict[str, Any]] = None):
    """
    事件驱动回测：信号在 T 日收盘生成，T+1 开盘成交，避免未来函数。
    含手续费与滑点。支持 ma / momentum / breakout / rsi_reversion。
    """
    import numpy as np
    import pandas as pd

    p = dict(params or {})
    strategy = (strategy or "ma").lower()
    fee = fee_bps / 10000.0
    slip = slippage_bps / 10000.0

    d = df.reset_index(drop=True).copy()
    if len(d) < 10:
        raise QuantError("数据不足，无法回测（需 >=10 根 K 线）", "扩大 start/end 区间。")
    o = d["open"].astype(float).values
    h = d["high"].astype(float).values
    l = d["low"].astype(float).values
    c = d["close"].astype(float).values
    dates = d["date"].astype(str).values
    n = len(c)

    # ---- 生成仓位目标（0/1），在 T 日收盘可得 ----
    target = np.zeros(n, dtype=int)
    if strategy == "ma":
        f = int(p.get("fast", fast))
        s = int(p.get("slow", slow))
        if f >= s:
            raise QuantError("fast 必须小于 slow", "例如 fast=5, slow=20。")
        maf = ind_ma(c, f).values
        mas = ind_ma(c, s).values
        target = np.where((~np.isnan(maf)) & (~np.isnan(mas)) & (maf > mas), 1, 0)
    elif strategy == "momentum":
        lb = int(p.get("lookback", 20))
        thr = float(p.get("threshold", 0.0))
        mom = pd.Series(c).pct_change(lb).values
        target = np.where(np.nan_to_num(mom) > thr, 1, 0)
    elif strategy == "breakout":
        lb = int(p.get("lookback", 20))
        hh = pd.Series(c).rolling(lb).max().shift(1).values
        ll = pd.Series(c).rolling(lb).min().shift(1).values
        tgt = np.zeros(n, dtype=int)
        pos = 0
        for i in range(n):
            if not np.isnan(hh[i]) and c[i] > hh[i]:
                pos = 1
            elif not np.isnan(ll[i]) and c[i] < ll[i]:
                pos = 0
            tgt[i] = pos
        target = tgt
    elif strategy == "rsi_reversion":
        nn = int(p.get("rsi_n", 14))
        lo = float(p.get("oversold", 30))
        hi = float(p.get("overbought", 70))
        r = ind_rsi(c, nn).values
        tgt = np.zeros(n, dtype=int)
        pos = 0
        for i in range(n):
            if not np.isnan(r[i]):
                if r[i] < lo:
                    pos = 1
                elif r[i] > hi:
                    pos = 0
            tgt[i] = pos
        target = tgt
    else:
        raise QuantError("未知策略: %s" % strategy,
                         "可选 ma | momentum | breakout | rsi_reversion。")

    # ---- 执行：T 日信号 -> T+1 开盘成交 ----
    shares = 0
    cash_amt = float(cash)
    equity = np.zeros(n)
    trades: List[Dict[str, Any]] = []
    hold_days = 0
    entry: Optional[Dict[str, Any]] = None
    pending: Optional[int] = None    # 待执行的目标仓位
    skipped: List[Dict[str, Any]] = []  # 信号有效但资金不足一手 -> 必须显式报告

    for i in range(n):
        px_open = o[i]
        if pending is not None and not math.isnan(px_open) and px_open > 0:
            want = pending
            cur = 1 if shares > 0 else 0
            if want != cur:
                if want == 1:  # 买入
                    # A 股一手 = 100 股；不足一手的资金无法建仓，
                    # 但绝不能静默吞掉信号（否则「166 个信号 / 0 笔交易」会被误读为策略无信号）。
                    buy_px = px_open * (1.0 + slip)
                    one_lot_cost = 100 * buy_px
                    if cash_amt >= one_lot_cost * (1.0 + fee):
                        max_sh = int(cash_amt / (buy_px * (1.0 + fee)))
                        max_sh = (max_sh // 100) * 100
                        if max_sh > 0:
                            cost = max_sh * buy_px
                            fee_amt = cost * fee
                            cash_amt -= (cost + fee_amt)
                            shares = max_sh
                            entry = {"entry_date": str(dates[i]), "entry_px": buy_px,
                                     "shares": shares, "entry_fee": fee_amt,
                                     "entry_i": i}
                    else:
                        skipped.append({
                            "date": str(dates[i]), "open": round(px_open, 3),
                            "reason": "资金不足一手（需 %.2f 元，可用 %.2f 元）"
                                      % (one_lot_cost * (1.0 + fee), cash_amt),
                        })
                else:  # 卖出
                    if shares > 0 and entry is not None:
                        sell_px = px_open * (1.0 - slip)
                        gross = shares * sell_px
                        fee_amt = gross * fee
                        cash_amt += (gross - fee_amt)
                        pnl = (sell_px - entry["entry_px"]) * shares - fee_amt - entry["entry_fee"]
                        ret = sell_px / entry["entry_px"] - 1.0
                        trades.append({
                            "entry_date": entry["entry_date"], "exit_date": str(dates[i]),
                            "entry_px": round(entry["entry_px"], 4), "exit_px": round(sell_px, 4),
                            "shares": shares, "pnl": round(pnl, 2),
                            "return_pct": round(ret * 100, 3),
                            "hold_days": i - entry["entry_i"],
                            "exit_reason": "信号",
                        })
                        shares = 0
                        entry = None
            pending = None

        if shares > 0:
            hold_days += 1
        equity[i] = cash_amt + shares * c[i]
        pending = int(target[i])

    # 末日未平仓 -> 标记浮动盈亏
    open_trade = None
    if shares > 0 and entry is not None:
        last_px = c[-1]
        pnl = (last_px - entry["entry_px"]) * shares - entry["entry_fee"]
        open_trade = {"entry_date": entry["entry_date"], "exit_date": "持仓中",
                      "entry_px": round(entry["entry_px"], 4), "exit_px": round(last_px, 4),
                      "shares": shares, "pnl": round(pnl, 2),
                      "return_pct": round((last_px / entry["entry_px"] - 1) * 100, 3),
                      "hold_days": n - entry["entry_i"], "exit_reason": "未平仓"}

    metrics = compute_metrics(equity, trades, dates, hold_days, cash)
    if metrics:
        metrics["signal_count"] = int(target.sum())
        metrics["skipped_buy"] = len(skipped)
        metrics["skipped_note"] = (
            "有 %d 次买入信号因资金不足一手被跳过（最小一手成本 > 初始资金）"
            % len(skipped)) if skipped else ""
    return {"metrics": metrics, "equity": equity, "dates": dates, "trades": trades,
            "open_trade": open_trade, "target": target, "skipped": skipped}


# ============================================================================
# 段 10. 工具实现（q_ 前缀首字母大写）
# ============================================================================
def q_Env(**kw) -> str:
    """q_env 环境自检。"""
    lines = ["# 环境自检 (q_env)", ""]

    # Python
    lines.append("## 运行时")
    lines.append(md_table(["项", "值"], [
        ["Python", sys.version.split()[0]],
        ["可执行文件", sys.executable],
        ["平台", sys.platform],
        ["pip 解释器", os.environ.get("CONDA_PREFIX", "-")],
    ]))

    # 库版本
    libs = ["akshare", "pandas", "numpy", "talib", "backtrader", "vectorbt",
            "lightgbm", "xgboost", "torch", "streamlit", "requests", "pyarrow"]
    rows = []
    for name in libs:
        try:
            mod = __import__(name)
            rows.append([name, "✅ 已安装", str(getattr(mod, "__version__", "?"))])
        except Exception:
            rows.append([name, "❌ 未安装", "-"])
    lines += ["", "## 依赖库", md_table(["库", "状态", "版本"], rows)]

    # 缓存
    cd = cache_detail()
    lines += ["", "## 缓存目录", md_table(["项", "值"], [
        ["路径", cd["dir"]],
        ["文件数", cd["files"]],
        ["总大小", fmt_big(cd["bytes"]) + " (%d 字节)" % cd["bytes"]],
        ["扩展名分布", ", ".join("%s=%s" % (k, fmt_big(v)) for k, v in cd["by_ext"].items()) or "-"],
    ])]

    # 网络可达性（含本轮修复后的首选源）
    targets = [
        ("腾讯日线 ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                                   "?param=sh600519,day,,,5,qfq"),
        ("腾讯行情 qt.gtimg.cn", "https://qt.gtimg.cn/q=sh600519"),
        ("新浪日线 money.finance.sina",
         "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         "CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=5"),
        ("新浪行情 hq.sinajs.cn", "https://hq.sinajs.cn/list=sh600519"),
        ("新浪外汇 fx_sbtcusd", "https://hq.sinajs.cn/list=fx_sbtcusd"),
        ("东方财富 push2", "https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43"),
        ("Binance", "https://api.binance.com/api/v3/ping"),
        ("OKX", "https://www.okx.com/api/v5/public/time"),
        ("本地代理 %s" % PROXY, PROXY or "http://127.0.0.1:7890"),
    ]
    nrows = []
    for label, url in targets:
        ok, ms, note = probe(url, timeout=7.0)
        nrows.append([label, "✅ 通" if ok else "❌ 不通", "%d ms" % ms, note])
    lines += ["", "## 网络可达性", md_table(["目标", "状态", "延迟", "说明"], nrows)]

    # 时间
    bj, ut = beijing_now(), utc_now()
    lines += ["", "## 时间", md_table(["时区", "当前时间"], [
        ["北京时间 (UTC+8)", bj.strftime("%Y-%m-%d %H:%M:%S")],
        ["UTC", ut.strftime("%Y-%m-%d %H:%M:%S")],
    ])]

    lines += ["", "## 结论"]
    ok_cnt = sum(1 for r in nrows if r[1].startswith("✅"))
    lines.append("- 行情数据源 %d/%d 可达。" % (ok_cnt, len(nrows)))
    lines.append("- MCP 协议层：标准库手写 stdio JSON-RPC（protocolVersion=%s），无第三方 SDK 依赖。"
                 % PROTOCOL_VERSION)
    lines.append("- K 线源优先级：腾讯日线(ifzq.gtimg.cn) -> 新浪日线 -> akshare -> 东方财富；"
                 "加密：新浪外汇/全球期货（直连）-> Binance/OKX（走代理 %s）。" % PROXY)
    if not any(r[0].startswith("Binance") and r[1].startswith("✅") for r in nrows):
        lines.append("- ⚠️ Binance/OKX 直连不可达：加密分钟/小时线需本地代理；"
                     "日线与实时快照已由新浪直连覆盖。")
    return "\n".join(lines)


def q_Quote(symbols=None, **kw) -> str:
    """q_quote 实时行情快照。"""
    if not symbols:
        raise QuantError("symbols 为空", "传入数组，如 [\"600519\",\"000001.SZ\",\"00700.HK\"]。")
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.replace("，", ",").split(",") if s.strip()]
    symbols = [str(s) for s in symbols][:60]

    results, failed = fetch_quotes(symbols)
    if not results and failed:
        raise QuantError("全部标的取数失败",
                         "检查代码格式；A 股用腾讯/新浪源，加密需外网。",
                         "; ".join("%s: %s" % (f["symbol"], f["reason"]) for f in failed[:5]))

    rows = []
    for q in results:
        rows.append([
            q.get("symbol", "-"), q.get("name", "-") or "-", fmt_num(q.get("price"), 3),
            fmt_pct(q.get("change_pct")), fmt_num(q.get("open"), 3), fmt_num(q.get("high"), 3),
            fmt_num(q.get("low"), 3), fmt_num(q.get("prev_close"), 3),
            fmt_big(q.get("volume")), fmt_big(q.get("amount")),
            fmt_num(q.get("turnover"), 2), q.get("time", "-") or "-", q.get("source", "-"),
        ])
    out = ["# 实时行情快照 (%d/%d)" % (len(results), len(symbols)), ""]
    out.append(md_table(["代码", "名称", "现价", "涨跌幅", "开", "高", "低", "昨收",
                         "成交量", "成交额", "换手(%)", "时间", "数据源"], rows))

    # 估值补充
    extra = [q for q in results if q.get("pe") or q.get("pb") or q.get("mktcap")]
    if extra:
        out += ["", "## 估值", md_table(["代码", "名称", "PE", "PB", "总市值"],
                                       [[q["symbol"], q.get("name", "-"), fmt_num(q.get("pe")),
                                         fmt_num(q.get("pb")), fmt_big(q.get("mktcap"))]
                                        for q in extra])]
    if failed:
        out += ["", "## 未取到 (%d)" % len(failed)]
        out.append(md_table(["代码", "原因"],
                            [[f["symbol"], f["reason"]] for f in failed]))
    out += ["", "_数据源: 腾讯 qt.gtimg.cn (GBK) / 新浪 hq.sinajs.cn / 东方财富 push2_"]
    return "\n".join(out)


def q_Klines(symbol=None, period="daily", start=None, end=None, limit=250, **kw) -> str:
    """q_klines 历史 K 线 + 统计。"""
    if not symbol:
        raise QuantError("symbol 为空", "传入如 600519。")
    limit = max(1, min(int(limit or 250), 5000))
    df = get_klines(symbol, period, start, end, limit)
    if df is None or len(df) == 0:
        raise QuantError("K 线为空: %s" % symbol, "扩大日期区间或稍后重试。")

    import numpy as np
    c = df["close"].astype(float)
    ret = c.pct_change().dropna()
    total_ret = (c.iloc[-1] / c.iloc[0] - 1.0) if len(c) > 1 else 0.0
    vol = float(np.std(ret, ddof=1) * math.sqrt(252)) if len(ret) > 2 else 0.0
    peak = c.cummax()
    mdd = float(((c / peak) - 1.0).min())

    out = ["# %s %s K线 (最近 %d 根)" % (symbol, period, len(df)), ""]
    rows = []
    import pandas as pd  # noqa: F401
    for _, r in df.tail(30).iterrows():
        rows.append([r["date"], fmt_num(r["open"], 3), fmt_num(r["high"], 3),
                     fmt_num(r["low"], 3), fmt_num(r["close"], 3),
                     fmt_big(r.get("volume")), fmt_big(r.get("amount"))])
    out.append(md_table(["日期", "开", "高", "低", "收", "成交量", "成交额"], rows))
    if len(df) > 30:
        out += ["", "_（仅显示最近 30 根，共 %d 根）_" % len(df)]

    out += ["", "## 区间统计", md_table(["指标", "值"], [
        ["区间", "%s ~ %s" % (df["date"].iloc[0], df["date"].iloc[-1])],
        ["K线根数", len(df)],
        ["首/末收盘", "%s → %s" % (fmt_num(c.iloc[0], 3), fmt_num(c.iloc[-1], 3))],
        ["区间涨跌幅", fmt_pct(total_ret * 100)],
        ["年化波动率", fmt_pct(vol * 100)],
        ["最大回撤", fmt_pct(mdd * 100)],
        ["区间最高/最低", "%s / %s" % (fmt_num(c.max(), 3), fmt_num(c.min(), 3))],
        ["日均成交量", fmt_big(df["volume"].astype(float).mean())],
    ])]
    out += ["", "_前复权；本地 parquet 缓存已启用（命中缓存不重复请求）。_"]
    return "\n".join(out)


def q_Indicators(symbol=None, period="daily", indicators=None, limit=250, **kw) -> str:
    """q_indicators 技术指标（纯本地计算）。"""
    if not symbol:
        raise QuantError("symbol 为空", "传入如 600519。")
    if not indicators:
        indicators = ["ma", "ema", "macd", "rsi", "boll", "atr", "kdj"]
    if isinstance(indicators, str):
        indicators = [s.strip().lower() for s in indicators.replace("，", ",").split(",") if s.strip()]
    indicators = [str(i).lower() for i in indicators]

    df = get_klines(symbol, period, None, None, max(int(limit or 250), 120))
    if df is None or len(df) < 20:
        raise QuantError("K 线不足（需 >=20 根）", "扩大日期区间。")
    c, h, l = df["close"].astype(float), df["high"].astype(float), df["low"].astype(float)
    dates = df["date"].tolist()
    n = len(c)
    out = ["# %s 技术指标 (%s, %s, %d 根)" % (symbol, period, dates[-1], n), ""]

    sigs: List[str] = []
    blocks: List[str] = []

    def tail5(series_name, vals):
        seg = list(zip(dates[-5:], [fmt_num(v, 4) for v in vals[-5:]]))
        return md_table(["指标", "日期", "值"],
                        [[series_name, d, v] for d, v in seg])

    if "ma" in indicators:
        periods = kw.get("ma_periods") or [5, 10, 20, 60]
        rows, extra = [], []
        for p in periods:
            if n < p:
                continue
            m = ind_ma(c, p)
            rows.append(["MA%d" % p, fmt_num(m.iloc[-1], 4), fmt_num(m.iloc[-2], 4),
                         fmt_pct((c.iloc[-1] / m.iloc[-1] - 1) * 100)])
        for i in range(min(5, n)):
            pass
        blocks.append("## 均线 MA\n" + md_table(["指标", "当前值", "前值", "价格偏离"], rows))
        # 最近 5 根序列
        seq_rows = []
        for p in periods:
            if n < p:
                continue
            m = ind_ma(c, p)
            for d, v in zip(dates[-5:], m.values[-5:]):
                seq_rows.append(["MA%d" % p, d, fmt_num(v, 4)])
        blocks.append("**最近 5 根序列**\n" + md_table(["指标", "日期", "值"], seq_rows))
        if n >= 20:
            ma5, ma20 = ind_ma(c, 5), ind_ma(c, 20)
            if ma5.iloc[-1] > ma20.iloc[-1] and ma5.iloc[-2] <= ma20.iloc[-2]:
                sigs.append("🟢 MA5 上穿 MA20 —— **金叉**（短期看多）")
            elif ma5.iloc[-1] < ma20.iloc[-1] and ma5.iloc[-2] >= ma20.iloc[-2]:
                sigs.append("🔴 MA5 下穿 MA20 —— **死叉**（短期看空）")
            elif ma5.iloc[-1] > ma20.iloc[-1]:
                sigs.append("MA5 在 MA20 上方（多头排列）")
            else:
                sigs.append("MA5 在 MA20 下方（空头排列）")

    if "ema" in indicators:
        periods = kw.get("ema_periods") or [12, 26]
        rows = [[("EMA%d" % p), fmt_num(ind_ema(c, p).iloc[-1], 4),
                 fmt_num(ind_ema(c, p).iloc[-2], 4)] for p in periods if n >= p]
        blocks.append("## 指数均线 EMA\n" + md_table(["指标", "当前值", "前值"], rows))
        seq = []
        for p in periods:
            if n < p:
                continue
            e = ind_ema(c, p)
            for d, v in zip(dates[-5:], e.values[-5:]):
                seq.append(["EMA%d" % p, d, fmt_num(v, 4)])
        blocks.append("**最近 5 根序列**\n" + md_table(["指标", "日期", "值"], seq))

    if "macd" in indicators:
        dif, dea, hist = ind_macd(c)
        blocks.append("## MACD (12,26,9)\n" + md_table(["项", "当前", "前值"], [
            ["DIF", fmt_num(dif.iloc[-1], 4), fmt_num(dif.iloc[-2], 4)],
            ["DEA", fmt_num(dea.iloc[-1], 4), fmt_num(dea.iloc[-2], 4)],
            ["MACD柱", fmt_num(hist.iloc[-1], 4), fmt_num(hist.iloc[-2], 4)],
        ]))
        seq = []
        for d, a, b, cc in zip(dates[-5:], dif.values[-5:], dea.values[-5:], hist.values[-5:]):
            seq.append([d, fmt_num(a, 4), fmt_num(b, 4), fmt_num(cc, 4)])
        blocks.append("**最近 5 根序列**\n" + md_table(["日期", "DIF", "DEA", "MACD柱"], seq))
        if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]:
            sigs.append("🟢 MACD **金叉**（DIF 上穿 DEA）")
        elif dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2]:
            sigs.append("🔴 MACD **死叉**（DIF 下穿 DEA）")
        sigs.append("MACD柱%s" % ("翻红（动能转强）" if hist.iloc[-1] > 0 else "翻绿（动能转弱）"))

    if "rsi" in indicators:
        nn = int(kw.get("rsi_n") or 14)
        r = ind_rsi(c, nn)
        cur = r.iloc[-1]
        blocks.append("## RSI(%d)\n" % nn + md_table(["项", "当前", "前值"],
                                                    [["RSI", fmt_num(cur, 2), fmt_num(r.iloc[-2], 2)]]))
        blocks.append("**最近 5 根序列**\n" + md_table(
            ["指标", "日期", "值"], [["RSI%d" % nn, d, fmt_num(v, 2)]
                                     for d, v in zip(dates[-5:], r.values[-5:])]))
        if cur >= 70:
            sigs.append("🔴 RSI=%.1f **超买**（>70，回调风险）" % cur)
        elif cur <= 30:
            sigs.append("🟢 RSI=%.1f **超卖**（<30，反弹机会）" % cur)
        else:
            sigs.append("RSI=%.1f 中性区间" % cur)

    if "boll" in indicators:
        mid, up, lo = ind_boll(c)
        cur = c.iloc[-1]
        blocks.append("## 布林带 BOLL(20,2)\n" + md_table(["项", "值"], [
            ["上轨", fmt_num(up.iloc[-1], 4)], ["中轨", fmt_num(mid.iloc[-1], 4)],
            ["下轨", fmt_num(lo.iloc[-1], 4)], ["现价", fmt_num(cur, 4)],
            ["带宽", fmt_pct((up.iloc[-1] - lo.iloc[-1]) / mid.iloc[-1] * 100)],
        ]))
        seq = []
        for d, a, b, cc in zip(dates[-5:], up.values[-5:], mid.values[-5:], lo.values[-5:]):
            seq.append([d, fmt_num(a, 4), fmt_num(b, 4), fmt_num(cc, 4)])
        blocks.append("**最近 5 根序列**\n" + md_table(["日期", "上轨", "中轨", "下轨"], seq))
        if cur > up.iloc[-1]:
            sigs.append("🟢 价格**突破布林上轨**（强势/超买）")
        elif cur < lo.iloc[-1]:
            sigs.append("🔴 价格**跌破布林下轨**（弱势/超卖）")

    if "atr" in indicators:
        a = ind_atr(h, l, c)
        cur = a.iloc[-1]
        blocks.append("## ATR(14)\n" + md_table(["项", "值"], [
            ["ATR", fmt_num(cur, 4)],
            ["占现价", fmt_pct(cur / c.iloc[-1] * 100)],
            ["建议止损(2ATR)", fmt_num(c.iloc[-1] - 2 * cur, 4)],
        ]))
        blocks.append("**最近 5 根序列**\n" + md_table(
            ["指标", "日期", "值"], [["ATR", d, fmt_num(v, 4)]
                                     for d, v in zip(dates[-5:], a.values[-5:])]))

    if "kdj" in indicators:
        k, dd, j = ind_kdj(h, l, c)
        blocks.append("## KDJ(9,3,3)\n" + md_table(["项", "当前", "前值"], [
            ["K", fmt_num(k.iloc[-1], 2), fmt_num(k.iloc[-2], 2)],
            ["D", fmt_num(dd.iloc[-1], 2), fmt_num(dd.iloc[-2], 2)],
            ["J", fmt_num(j.iloc[-1], 2), fmt_num(j.iloc[-2], 2)],
        ]))
        seq = []
        for d, a, b, cc in zip(dates[-5:], k.values[-5:], dd.values[-5:], j.values[-5:]):
            seq.append([d, fmt_num(a, 2), fmt_num(b, 2), fmt_num(cc, 2)])
        blocks.append("**最近 5 根序列**\n" + md_table(["日期", "K", "D", "J"], seq))
        if k.iloc[-1] > dd.iloc[-1] and k.iloc[-2] <= dd.iloc[-2]:
            sigs.append("🟢 KDJ **金叉**")
        elif k.iloc[-1] < dd.iloc[-1] and k.iloc[-2] >= dd.iloc[-2]:
            sigs.append("🔴 KDJ **死叉**")
        if j.iloc[-1] > 100:
            sigs.append("🔴 KDJ J值>100 **超买**")
        elif j.iloc[-1] < 0:
            sigs.append("🟢 KDJ J值<0 **超卖**")

    out += blocks
    out += ["", "## 信号解读"]
    if sigs:
        for s in sigs:
            out.append("- " + s)
    else:
        out.append("- 无明显信号")
    out += ["", "_纯本地计算（%s），不联网。_" % ("TA-Lib 加速" if HAS_TALIB else "pandas/numpy 实现")]
    return "\n".join(out)


def q_BacktestMa(symbol=None, strategy="ma", fast=5, slow=20, start=None, end=None,
                 cash=100000.0, fee_bps=3.0, slippage_bps=2.0, limit=0, **kw) -> str:
    """q_backtest_ma 回测。"""
    if not symbol:
        raise QuantError("symbol 为空", "传入如 600519。")
    fast, slow = int(fast or 5), int(slow or 20)
    cash = float(cash or 100000.0)
    fee_bps, slippage_bps = float(fee_bps or 0.0), float(slippage_bps or 0.0)
    need = max(slow * 3, 200) if strategy == "ma" else 300
    df = get_klines(symbol, "daily", start, end, limit=limit or 3000)
    if df is None or len(df) < need:
        raise QuantError("K 线不足（%s 需 >=%d 根，实际 %d）"
                         % (strategy, need, 0 if df is None else len(df)),
                         "扩大 start/end 日期区间。")
    res = run_backtest(df, strategy, fast, slow, cash, fee_bps, slippage_bps)
    m, trades = res["metrics"], res["trades"]
    if not m:
        raise QuantError("回测无法产生有效净值", "检查参数。")

    out = ["# 回测结果 %s [%s%s]" % (
        symbol, strategy, (" fast=%d slow=%d" % (fast, slow)) if strategy == "ma" else ""), ""]
    out.append("## 绩效总览")
    out.append(md_table(["指标", "值"], [
        ["区间", "%s ~ %s" % (m["start"], m["end"])],
        ["交易日数", m["bars"]],
        ["初始资金", fmt_num(cash, 2)],
        ["期末权益", fmt_num(m["final_equity"], 2)],
        ["总收益率", fmt_pct(m["total_return"] * 100)],
        ["年化收益率", fmt_pct(m["annual_return"] * 100)],
        ["最大回撤", fmt_pct(m["max_drawdown"] * 100)],
        ["夏普比率", fmt_num(m["sharpe"], 3)],
        ["索提诺比率", fmt_num(m["sortino"], 3)],
        ["卡玛比率", fmt_num(m["calmar"], 3)],
        ["胜率", fmt_pct(m["win_rate"] * 100)],
        ["盈亏比", ("∞" if math.isinf(m["profit_factor"]) else fmt_num(m["profit_factor"], 3))],
        ["交易次数", m["trade_count"]],
        ["持仓天数", m["hold_days"]],
        ["持仓天数占比", fmt_pct(m["hold_ratio"] * 100)],
        ["平均盈利/亏损", "%s / %s" % (fmt_num(m["avg_win"], 2), fmt_num(m["avg_loss"], 2))],
        ["多头信号数", m.get("signal_count", "-")],
        ["因资金不足跳过", m.get("skipped_buy", 0)],
    ]))
    out.append("")
    out.append("_成交模型：T 日收盘出信号 → **次日开盘成交**；手续费 %.2f bps，滑点 %.2f bps。_"
               % (fee_bps, slippage_bps))
    if res.get("skipped"):
        sk = res["skipped"]
        out += ["", "## ⚠️ 因资金不足被跳过的买入信号 (%d)" % len(sk),
                "> 初始资金 %.2f 元不足以买入一手（100 股）。" % cash,
                "> 前 5 条：%s" % "; ".join(
                    "%s 开盘 %s（%s）" % (x["date"], x["open"], x["reason"]) for x in sk[:5]),
                "> 提额可解决：把 cash 设为 ≥ %.2f 元。"
                % (next((x["open"] * 100 * 1.001 for x in sk), 0.0))]
        if not trades:
            out.append("> **本区间 0 笔成交，原因是资金约束，不是没有信号。**")

    # 逐笔交易
    out += ["", "## 逐笔交易 (最多 50 笔，共 %d 笔)" % len(trades)]
    if trades:
        rows = []
        for t in trades[:50]:
            rows.append([t["entry_date"], t["entry_px"], t["exit_date"], t["exit_px"],
                         t["shares"], fmt_num(t["pnl"], 2), fmt_pct(t["return_pct"]),
                         t["hold_days"], t["exit_reason"]])
        out.append(md_table(["买入日", "买入价", "卖出日", "卖出价", "股数",
                             "盈亏", "收益率", "持有天", "原因"], rows))
        if len(trades) > 50:
            out.append("_（仅显示前 50 笔）_")
    else:
        out.append("_(无已平仓交易)_")
    if res.get("open_trade"):
        ot = res["open_trade"]
        out += ["", "**未平仓持仓**: 买入日 %s @ %s，股数 %s，浮动盈亏 %s (%s)"
                % (ot["entry_date"], ot["entry_px"], ot["shares"],
                   fmt_num(ot["pnl"], 2), fmt_pct(ot["return_pct"]))]

    # 净值曲线（最近 250 日）
    eq = list(res["equity"])
    dts = list(res["dates"])
    seg = min(250, len(eq))
    nav = [round(v / cash, 4) for v in eq[-seg:]]
    out += ["", "## 最近 %d 日净值曲线" % seg]
    out.append("```")
    out.append("dates=%s" % json.dumps([str(d) for d in dts[-seg:][::max(1, seg // 25)]], ensure_ascii=False))
    out.append("nav=%s" % json.dumps(nav[::max(1, seg // 25)]))
    out.append("```")
    out.append("_完整 netvalue 数组长度 %d，抽样显示约 25 点。_" % seg)
    return "\n".join(out)


def q_OptimizeGrid(symbol=None, grid=None, start=None, end=None,
                   metric="sharpe", split=0.7, strategy="ma", **kw) -> str:
    """q_optimize_grid 参数网格寻优（含样本内/外分割）。"""
    if not symbol:
        raise QuantError("symbol 为空", "传入如 600519。")
    metric = (metric or "sharpe").lower()
    if metric not in ("sharpe", "calmar", "annual_return", "total_return"):
        raise QuantError("metric 不支持: %s" % metric, "可选 sharpe|calmar|annual_return。")
    if not grid:
        grid = {"fast": [3, 5, 10, 15], "slow": [20, 30, 60]}
    if isinstance(grid, str):
        try:
            grid = json.loads(grid)
        except Exception:
            raise QuantError("grid 解析失败", '形如 {"fast":[5,10],"slow":[20,60]}。')
    if not isinstance(grid, dict) or not grid:
        raise QuantError("grid 必须是非空对象", '形如 {"fast":[5,10],"slow":[20,60]}。')

    df = get_klines(symbol, "daily", start, end, limit=3000)
    if df is None or len(df) < 120:
        raise QuantError("K 线不足（需 >=120 根）", "扩大日期区间。")
    n = len(df)
    n_is = max(60, int(n * float(split or 0.7)))
    if n_is >= n - 20:
        n_is = n - 20
    is_df = df.iloc[:n_is].reset_index(drop=True)
    oos_df = df.iloc[n_is:].reset_index(drop=True)

    keys = list(grid.keys())
    combos = [{}]
    for k in keys:
        vals = grid[k]
        if not isinstance(vals, (list, tuple)):
            vals = [vals]
        combos = [dict(c, **{k: v}) for c in combos for v in vals]
    if len(combos) > 400:
        raise QuantError("网格过大 (%d 组合 > 400)" % len(combos), "缩小网格范围。")

    rows = []
    for combo in combos:
        try:
            ris = run_backtest(is_df, strategy, combo.get("fast", 5), combo.get("slow", 20),
                               params=combo)
            mis = ris["metrics"]
            if not mis:
                continue
            try:
                ros = run_backtest(oos_df, strategy, combo.get("fast", 5),
                                   combo.get("slow", 20), params=combo)
                mos = ros["metrics"] or {}
            except Exception:
                mos = {}
            rows.append({"params": combo, "is": mis, "oos": mos})
        except Exception:
            continue
    if not rows:
        raise QuantError("所有参数组合均回测失败", "检查 grid 参数（如 fast<slow）。")

    def mval(r, which):
        v = r[which].get(metric)
        return -1e18 if v is None else float(v)

    rows.sort(key=lambda r: mval(r, "is"), reverse=True)
    top = rows[:10]

    out = ["# 参数网格寻优 %s" % symbol, ""]
    out.append(md_table(["项", "值"], [
        ["策略", strategy],
        ["寻优指标", metric],
        ["组合总数", len(rows)],
        ["样本内 (IS)", "%s ~ %s (%d 根, %.0f%%)"
         % (df["date"].iloc[0], df["date"].iloc[n_is - 1], n_is, 100.0 * n_is / n)],
        ["**样本外 (OOS)**", "**%s ~ %s (%d 根, %.0f%%)**"
         % (df["date"].iloc[n_is], df["date"].iloc[-1], n - n_is, 100.0 * (n - n_is) / n)],
        ["分割比例", "%.0f:%.0f" % (100 * float(split or 0.7), 100 - 100 * float(split or 0.7))],
    ]))
    out += ["", "## Top 10 参数组合（按**样本内** %s 排序）" % metric]
    hdr = ["排名"] + keys + ["IS " + metric, "IS 年化", "IS 回撤", "IS 交易数",
                             "**OOS " + metric + "**", "**OOS 年化**", "**OOS 回撤**"]
    body = []
    for i, r in enumerate(top, 1):
        p, mis, mos = r["params"], r["is"], r["oos"]
        body.append([i] + [p.get(k) for k in keys] + [
            fmt_num(mis.get(metric), 3), fmt_pct((mis.get("annual_return") or 0) * 100),
            fmt_pct((mis.get("max_drawdown") or 0) * 100), mis.get("trade_count"),
            fmt_num(mos.get(metric), 3), fmt_pct((mos.get("annual_return") or 0) * 100),
            fmt_pct((mos.get("max_drawdown") or 0) * 100),
        ])
    out.append(md_table(hdr, body))
    out += ["", "> **样本外说明**：`OOS *` 三列（加粗）完全使用**样本外**数据回测，"
                "参数由样本内选出后**未再优化**，是判断过拟合的关键依据。"
                "若 IS 表现优秀而 OOS 显著衰减，说明参数过拟合。"]
    best = top[0]
    out += ["", "## 最佳组合", md_table(["项", "值"], [
        ["参数", json.dumps(best["params"], ensure_ascii=False)],
        ["IS %s" % metric, fmt_num(best["is"].get(metric), 3)],
        ["OOS %s" % metric, fmt_num(best["oos"].get(metric), 3)],
        ["IS 总收益", fmt_pct((best["is"].get("total_return") or 0) * 100)],
        ["OOS 总收益", fmt_pct((best["oos"].get("total_return") or 0) * 100)],
    ])]
    return "\n".join(out)


def q_FactorIc(universe=None, factor="动量", period=20, start=None, end=None,
               max_stocks=40, **kw) -> str:
    """q_factor_ic 因子 IC/IR 分析。"""
    import numpy as np
    import pandas as pd

    factor = (factor or "动量").strip()
    alias = {"momentum": "动量", "reversal": "反转", "volatility": "波动率",
             "turnover": "换手率", "volratio": "量比", "mktcap": "市值"}
    factor = alias.get(factor.lower(), factor)
    if factor not in ("动量", "反转", "波动率", "换手率", "量比", "市值"):
        raise QuantError("factor 不支持: %s" % factor,
                         "可选 动量|反转|波动率|换手率|量比|市值。")
    period = max(2, min(int(period or 20), 250))
    max_stocks = max(5, min(int(max_stocks or 40), 300))

    # 股票池
    codes: List[str] = []
    pool_note = ""
    if universe and not (isinstance(universe, str) and universe.lower() in ("hs300", "zz500", "sz50")):
        if isinstance(universe, str):
            codes = [c.strip() for c in universe.replace("，", ",").split(",") if c.strip()]
        else:
            codes = [str(c).strip() for c in universe]
        pool_note = "自定义列表 (%d 只)" % len(codes)
    else:
        key = (universe or "hs300").lower()
        idx = _INDEX_IDS.get(key, "000300")
        codes = _index_constituents(key)
        if not codes:
            raise QuantError("成分股获取失败 (%s)" % idx,
                             "稍后重试或直接传入 universe 列表（如 \"600519,000001\"）。")
        pool_note = "%s 成分股 (%d 只)" % (key.upper(), len(codes))

    step = max(1, len(codes) // max_stocks)
    codes = codes[::step][:max_stocks]

    # 逐股拉取日线（记录每只失败原因，样本不足时结构化回报）
    frames: Dict[str, Any] = {}
    fails: List[Dict[str, str]] = []
    short: List[Dict[str, str]] = []
    hit_timeout = False
    t0 = time.time()
    for code in codes:
        if time.time() - t0 > 75:
            hit_timeout = True
            break
        try:
            d = get_klines(code, "daily", start, end, limit=400)
            if d is not None and len(d) > period + 5:
                frames[code] = d.set_index("date")
            else:
                short.append({"code": code,
                              "reason": "K 线根数 %d <= period+5(%d)"
                                        % (0 if d is None else len(d), period + 5)})
        except Exception as e:  # noqa: BLE001
            fails.append({"code": code, "reason": "%s: %s" % (type(e).__name__, str(e)[:140])})
    failed = len(fails) + len(short)
    if len(frames) < 5:
        detail_lines = []
        detail_lines.append("股票池: %s（取样 %d 只）" % (pool_note, len(codes)))
        detail_lines.append("取数成功: %d 只；失败: %d 只（异常 %d / 根数不足 %d）%s"
                            % (len(frames), failed, len(fails), len(short),
                               "；已达 75s 时间预算提前中断" if hit_timeout else ""))
        if fails:
            detail_lines.append("失败明细(前 20): " + " ;; ".join(
                "%s -> %s" % (f["code"], f["reason"]) for f in fails[:20]))
        if short:
            detail_lines.append("根数不足(前 20): " + " ;; ".join(
                "%s -> %s" % (f["code"], f["reason"]) for f in short[:20]))
        if frames:
            detail_lines.append("成功代码: " + ", ".join(sorted(frames)[:30]))
        raise QuantError(
            "有效样本不足 (%d 只，失败 %d)" % (len(frames), failed),
            "1) 查看下方明细里失败的那几只及其原因；"
            "2) 缩小 max_stocks 或换 universe；"
            "3) 若失败原因是『全部 K 线数据源失败』多为该代码停牌/退市或新股。",
            " || ".join(detail_lines))

    # 构造面板
    close_p = pd.DataFrame({k: v["close"] for k, v in frames.items()}).sort_index()
    vol_p = pd.DataFrame({k: v["volume"] for k, v in frames.items()}).sort_index()
    amt_p = pd.DataFrame({k: v.get("amount") for k, v in frames.items()}).sort_index()

    # 因子值（截面在 t 日可得）
    if factor == "动量":
        f = close_p.pct_change(period)
    elif factor == "反转":
        f = -close_p.pct_change(period)
    elif factor == "波动率":
        f = -close_p.pct_change().rolling(period).std()
    elif factor == "换手率":
        f = (vol_p / vol_p.rolling(period * 3).mean())
    elif factor == "量比":
        f = vol_p / vol_p.rolling(period).mean()
    else:  # 市值
        f = amt_p.rolling(period).mean()

    # 未来 period 日收益（作为标签）
    fwd = close_p.shift(-period) / close_p - 1.0

    # Rank IC 序列（逐日截面）
    ic_series = []
    dates = []
    for dt in close_p.index[period:-period]:
        try:
            x = f.loc[dt].dropna()
            y = fwd.loc[dt].dropna()
            common = x.index.intersection(y.index)
            if len(common) < 5:
                continue
            xa, ya = x[common], y[common]
            if xa.nunique() < 2 or ya.nunique() < 2:
                continue
            ic = float(pd.Series(xa).corr(pd.Series(ya), method="spearman"))
            if not math.isnan(ic):
                ic_series.append(ic)
                dates.append(str(dt))
        except Exception:
            continue
    if len(ic_series) < 5:
        raise QuantError("有效 IC 样本不足 (%d)" % len(ic_series), "扩大日期区间或股票池。")

    ics = np.array(ic_series)
    ic_mean, ic_std = float(ics.mean()), float(ics.std(ddof=1))
    icir = ic_mean / ic_std if ic_std > 1e-12 else 0.0
    win = float((ics > 0).mean())

    # 分层（5 层）收益
    lyr = {i: [] for i in range(1, 6)}
    for dt in close_p.index[period:-period]:
        try:
            x = f.loc[dt].dropna()
            y = fwd.loc[dt].dropna()
            common = x.index.intersection(y.index)
            if len(common) < 10:
                continue
            xa, ya = x[common], y[common]
            try:
                q = pd.qcut(xa.rank(method="first"), 5, labels=False)
            except Exception:
                continue
            for i in range(5):
                sel = ya[q == i]
                if len(sel):
                    lyr[i + 1].append(float(sel.mean()))
        except Exception:
            continue

    out = ["# 因子 IC/IR 分析 — 因子: %s" % factor, ""]
    out.append(md_table(["项", "值"], [
        ["股票池", pool_note],
        ["实际样本", "%d 只（失败 %d）" % (len(frames), failed)],
        ["因子", factor],
        ["调仓周期", "%d 个交易日" % period],
        ["IC 样本数", len(ic_series)],
        ["区间", "%s ~ %s" % (dates[0], dates[-1])],
    ]))
    out += ["", "## IC 统计", md_table(["指标", "值", "解读"], [
        ["Rank IC 均值", fmt_num(ic_mean, 4),
         "|IC|>0.03 有效，>0.05 较强" ],
        ["IC 标准差", fmt_num(ic_std, 4), "越小越稳定"],
        ["ICIR", fmt_num(icir, 4), ">0.5 较优（IC均值/IC标准差）"],
        ["IC>0 胜率", fmt_pct(win * 100), "大于 55% 较优"],
        ["IC t 统计量", fmt_num(ic_mean / (ic_std / math.sqrt(len(ics))), 3) if ic_std > 1e-12 else "-",
         "|t|>2 显著"],
        ["最近 5 期 IC", ", ".join(fmt_num(v, 3) for v in ics[-5:]), ""],
    ])]

    out += ["", "## 分层收益（5 层，Layer1=因子值最小，Layer5=最大）"]
    lrows = []
    prev = None
    for i in range(1, 6):
        arr = np.array(lyr[i]) if lyr[i] else np.array([0.0])
        avg = float(arr.mean())
        lrows.append(["Layer%d" % i, len(lyr[i]), fmt_pct(avg * 100),
                      fmt_pct(float(np.std(arr, ddof=1) * math.sqrt(252) / math.sqrt(period) * 100))
                      if len(arr) > 2 else "-"])
    out.append(md_table(["层", "样本数", "平均区间收益", "年化波动"], lrows))
    try:
        l1 = float(np.mean(lyr[1])) if lyr[1] else 0.0
        l5 = float(np.mean(lyr[5])) if lyr[5] else 0.0
        out.append("")
        out.append("**多空价差 (Layer5 - Layer1)**: %s（单调性: %s）"
                   % (fmt_pct((l5 - l1) * 100),
                      "良好" if (l5 - l1) * (ic_mean) > 0 else "偏弱/反向"))
    except Exception:
        pass
    out += ["", "_IC 用 Spearman 秩相关逐日截面计算；标签为未来 %d 日收益。_"
            "样本受数据源限频影响，`max_stocks` 默认 40。" % period]
    return "\n".join(out)


def _fetch_spot_table():
    """全市场 A 股快照（多源）。返回 DataFrame 或抛错。"""
    import pandas as pd
    # 源1: akshare 东财（字段最全）
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is not None and len(df) > 100:
            return df, "东方财富(akshare)"
    except Exception as e:  # noqa: BLE001
        log("spot_em failed: %r" % (e,))
    # 源2: 东财 push2 直连
    try:
        rows = []
        base = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
                "&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
                "&fields=f2,f3,f5,f6,f8,f9,f10,f12,f14,f20,f23")
        for pn in range(1, 61):
            js = http_get_json(base.format(pn=pn), referer="https://quote.eastmoney.com/",
                               timeout=10.0, retries=2, min_gap=0.35, host_key="push2.em")
            diff = ((js or {}).get("data") or {}).get("diff") or []
            if not diff:
                break
            rows.extend(diff)
            if len(diff) < 100:
                break
        if len(rows) > 100:
            df = pd.DataFrame(rows).rename(columns={
                "f12": "代码", "f14": "名称", "f2": "最新价", "f3": "涨跌幅",
                "f5": "成交量", "f6": "成交额", "f8": "换手率", "f9": "市盈率-动态",
                "f10": "量比", "f20": "总市值", "f23": "市净率"})
            return df, "东方财富(直连)"
    except Exception as e:  # noqa: BLE001
        log("em clist failed: %r" % (e,))
    # 源3: 新浪批量（无估值）
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot()
        if df is not None and len(df) > 100:
            df = df.rename(columns={"代码": "代码", "名称": "名称", "最新价": "最新价",
                                    "涨跌幅": "涨跌幅", "成交量": "成交量", "成交额": "成交额"})
            df["代码"] = df["代码"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)
            return df, "新浪(akshare，无估值字段)"
    except Exception as e:  # noqa: BLE001
        log("sina spot failed: %r" % (e,))
    # 源4: 腾讯批量快照（指数成分股池，直连实测可用）
    for key in ("hs300", "zz500", "sz50"):
        try:
            return _tencent_spot_table(key)
        except Exception as e:  # noqa: BLE001
            log("tencent spot table failed (%s): %r" % (key, e))
    raise QuantError("全市场快照全部数据源失败",
                     "已试：akshare 东财 / 东财 push2 直连 / akshare 新浪 / 腾讯成分股快照。"
                     "补救：1) 稍后重试（东财限频）；2) 用 q_quote 查询单只标的；"
                     "3) 设 QUANT_MCP_PROXY 走代理。")


def q_MarketOverview(**kw) -> str:
    """q_market_overview 市场全景。"""
    out = ["# 市场全景", ""]
    notes: List[str] = []

    # 指数
    idx_specs = [
        ("上证指数", "sh000001"), ("深证成指", "sz399001"), ("创业板指", "sz399006"),
        ("科创50", "sh000688"), ("北证50", "bj899050"),
        ("恒生指数", "hkHSI"), ("纳斯达克", "usIXIC"), ("标普500", "usSPX"),
        ("比特币", "BTCUSDT"),
    ]
    results, failed = fetch_quotes([s for _, s in idx_specs])
    got = {}
    for q in results:
        got[q.get("symbol", "")] = q
    rows = []
    for label, code in idx_specs:
        q = got.get(code) or got.get(label)
        if q:
            rows.append([label, fmt_num(q.get("price"), 2), fmt_pct(q.get("change_pct")),
                         q.get("source", "-")])
        else:
            f = next((x for x in failed if x["symbol"] == code), None)
            rows.append([label, "-", "-", "❌ " + (f["reason"][:40] if f else "不可达")])
    out += ["## 主要指数", md_table(["指数", "点位", "涨跌幅", "数据源"], rows)]

    # 两市成交额 / 涨跌家数 / 板块
    try:
        df, src = _fetch_spot_table()
        out += ["", "## A 股市场（数据源: %s）" % src]
        ncol = "最新价" if "最新价" in df.columns else None
        chg = "涨跌幅" if "涨跌幅" in df.columns else None
        amt = "成交额" if "成交额" in df.columns else None
        tot_amt = float(df[amt].astype(float).sum()) if amt else 0.0
        ch = df[chg].astype(float) if chg else None
        up = int((ch > 0).sum()) if ch is not None else 0
        dn = int((ch < 0).sum()) if ch is not None else 0
        flat = int((ch == 0).sum()) if ch is not None else 0
        limit_up = int((ch >= 9.8).sum()) if ch is not None else 0
        limit_dn = int((ch <= -9.8).sum()) if ch is not None else 0
        out.append(md_table(["指标", "值"], [
            ["两市成交额", fmt_big(tot_amt) + " 元"],
            ["统计股票数", len(df)],
            ["上涨家数", up], ["下跌家数", dn], ["平盘家数", flat],
            ["涨停(>=9.8%)", limit_up], ["跌停(<=-9.8%)", limit_dn],
            ["涨跌比", fmt_num(up / dn, 2) if dn else "∞"],
        ]))
    except QuantError as e:
        notes.append("A 股快照降级: %s" % e.reason)
        out += ["", "## A 股市场", "> ⚠️ 降级：%s" % e.reason]

    # 行业板块
    try:
        js = http_get_json(
            "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2"
            "&fid=f3&fs=m:90+t:2+f:!50&fields=f2,f3,f12,f14,f104,f105",
            referer="https://quote.eastmoney.com/", timeout=10.0, retries=3,
            min_gap=0.5, host_key="push2.em")
        diff = ((js or {}).get("data") or {}).get("diff") or []
        if diff:
            rows = [[i, d.get("f14"), fmt_pct(d.get("f3")), d.get("f104"), d.get("f105")]
                    for i, d in enumerate(diff[:10], 1)]
            out += ["", "## 行业板块涨幅前 10", md_table(
                ["#", "板块", "涨跌幅", "上涨家数", "下跌家数"], rows)]
            rows2 = [[i, d.get("f14"), fmt_pct(d.get("f3"))]
                     for i, d in enumerate(diff[-10:][::-1], 1)]
            out += ["", "## 行业板块跌幅前 10", md_table(["#", "板块", "涨跌幅"], rows2)]
        else:
            notes.append("行业板块为空")
    except QuantError as e:
        notes.append("行业板块降级: %s" % e.reason)

    # 北向资金
    try:
        import akshare as ak
        nb = ak.stock_hsgt_fund_flow_summary_em()
        if nb is not None and len(nb):
            rows = []
            for _, r in nb.iterrows():
                rows.append([r.get("交易日"), r.get("类型"), r.get("板块"),
                             fmt_num(r.get("成交净买额"), 2), fmt_num(r.get("资金净流入"), 2)])
            out += ["", "## 北向/沪深港通资金", md_table(
                ["交易日", "类型", "板块", "成交净买额(亿)", "资金净流入(亿)"], rows)]
        else:
            notes.append("北向资金返回空")
    except Exception as e:  # noqa: BLE001
        notes.append("北向资金取不到（%s），已降级" % type(e).__name__)
        out += ["", "## 北向资金", "> ⚠️ 降级：接口不可用（%s）。替代：使用 `q_quote` 查指数，"
                "或访问东方财富沪深港通页面。" % type(e).__name__]

    if notes:
        out += ["", "## 降级说明"]
        for n in notes:
            out.append("- " + n)
    return "\n".join(out)


def q_Screen(pool="hs300", conditions=None, limit=50, **kw) -> str:
    """q_screen 选股器。"""
    import pandas as pd

    limit = max(1, min(int(limit or 50), 200))
    conds: List[str] = []
    if conditions:
        if isinstance(conditions, str):
            conds = [c.strip() for c in conditions.replace("，", ",").split(",") if c.strip()]
        else:
            conds = [str(c).strip() for c in conditions]
    if not conds:
        conds = ["pe<30", "pb<3"]

    df, src = _fetch_spot_table()
    df = df.copy()
    df.columns = [str(c) for c in df.columns]

    # 股票池过滤
    pool_note = ""
    codes: Optional[List[str]] = None
    if pool and not (isinstance(pool, str) and pool.lower() in ("hs300", "zz500", "sz50", "all")):
        if isinstance(pool, str):
            codes = [c.strip() for c in pool.replace("，", ",").split(",") if c.strip()]
        else:
            codes = [str(c).strip() for c in pool]
        pool_note = "自定义 (%d 只)" % len(codes)
    elif isinstance(pool, str) and pool.lower() in ("hs300", "zz500", "sz50"):
        codes = _index_constituents(pool.lower())
        if not codes:
            raise QuantError("成分股获取失败 (%s)" % pool,
                             "改用 pool=all 或直接传代码列表。")
        pool_note = "%s (%d 只)" % (pool.upper(), len(codes))
    else:
        pool_note = "全部 A 股"

    if codes is not None:
        code_col = "代码" if "代码" in df.columns else df.columns[0]
        df[code_col] = df[code_col].astype(str).str.zfill(6)
        df = df[df[code_col].isin(set(codes))]
    if len(df) == 0:
        raise QuantError("股票池为空", "检查成分股接口或自定义列表。")

    # 字段映射
    COL = {
        "pe": ["市盈率-动态", "市盈率", "PE"],
        "pb": ["市净率", "PB"],
        "roe": ["ROE", "净资产收益率"],
        "涨幅": ["涨跌幅"], "涨跌幅": ["涨跌幅"],
        "量比": ["量比"],
        "市值": ["总市值"], "总市值": ["总市值"],
        "换手": ["换手率"], "换手率": ["换手率"],
        "价格": ["最新价"], "现价": ["最新价"], "最新价": ["最新价"],
        "成交额": ["成交额"],
    }

    def resolve(field: str) -> Optional[str]:
        for c in COL.get(field, []):
            if c in df.columns:
                return c
        return None

    applied, skipped = [], []
    mask = pd.Series(True, index=df.index)
    # 条件解析: field op value  （支持 中文条件名与 亿/万 单位）
    import re
    for c in conds:
        mm = re.match(r"^\s*([A-Za-z\u4e00-\u9fa5]+)\s*(<=|>=|<|>|=|==)\s*([-\d.]+)\s*(亿|万)?\s*$", c)
        if not mm:
            skipped.append("%s（无法解析）" % c)
            continue
        field, op, val, unit = mm.group(1), mm.group(2), float(mm.group(3)), mm.group(4)
        if unit == "亿":
            val *= 1e8
        elif unit == "万":
            val *= 1e4
        col = resolve(field)
        if col is None:
            skipped.append("%s（当前数据源无字段 '%s'）" % (c, field))
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        if op in ("<",):
            m = s < val
        elif op in (">",):
            m = s > val
        elif op == "<=":
            m = s <= val
        elif op == ">=":
            m = s >= val
        else:
            m = (s - val).abs() < 1e-9
        mask &= m.fillna(False)
        applied.append("%s %s %s%s" % (field, op, mm.group(3), unit or ""))

    res = df[mask].copy()
    # 按涨跌幅排序
    if "涨跌幅" in res.columns:
        res = res.sort_values("涨跌幅", ascending=False)

    out = ["# 选股结果", ""]
    out.append(md_table(["项", "值"], [
        ["股票池", pool_note],
        ["数据源", src],
        ["输入条件", "; ".join(conds)],
        ["生效条件", "; ".join(applied) or "-"],
        ["命中数量", len(res)],
    ]))
    if skipped:
        out += ["", "## ⚠️ 未生效条件"]
        for s in skipped:
            out.append("- " + s)

    out += ["", "## 命中标的 (前 %d)" % min(limit, len(res))]
    if len(res) == 0:
        out.append("_(无标的满足全部条件，建议放宽阈值)_")
    else:
        rows = []
        for _, r in res.head(limit).iterrows():
            rows.append([
                str(r.get("代码", "-")), str(r.get("名称", "-")),
                fmt_num(r.get("最新价"), 2), fmt_pct(r.get("涨跌幅")),
                fmt_num(r.get("量比"), 2), fmt_num(r.get("换手率"), 2),
                fmt_num(r.get("市盈率-动态"), 2), fmt_num(r.get("市净率"), 2),
                fmt_big(r.get("总市值")),
            ])
        out.append(md_table(["代码", "名称", "现价", "涨跌幅", "量比", "换手(%)",
                             "PE", "PB", "总市值"], rows))
        lo = ", ".join(str(x) for x in res["代码"].head(limit).tolist())
        out += ["", "**代码列表**: `%s`" % lo]
    return "\n".join(out)


def _sina_crypto_daily(symbol="BTC", limit: int = 100):
    """新浪全球期货日线（直连可用）。返回 list[dict(date,open,high,low,close,volume)]。

    接口返回 JSONP: var _k=([{"date":..,"open":..,"high":..,"low":..,"close":..,"volume":..},...]);
    """
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_k=/"
           "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={s}".format(s=symbol))
    txt = http_get(url, referer="https://finance.sina.com.cn", encoding="utf-8",
                   timeout=12.0, retries=3, min_gap=0.25, host_key="stock2.finance.sina")
    m = re.search(r"\((\[.*\])\)", txt, re.S)
    if not m:
        raise QuantError("新浪全球期货日线格式异常", "改用代理访问 Binance。", txt[:200])
    arr = json.loads(m.group(1))
    if not arr:
        raise QuantError("新浪全球期货日线为空: %s" % symbol, "改用代理访问 Binance。")
    rows = []
    for it in arr[-max(10, int(limit)):]:
        try:
            rows.append({"date": it["date"], "open": float(it["open"]),
                         "high": float(it["high"]), "low": float(it["low"]),
                         "close": float(it["close"]), "volume": safe_float(it.get("volume"))})
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        raise QuantError("新浪全球期货日线解析为空: %s" % symbol, "改用代理访问 Binance。")
    return rows


def q_CryptoKlines(symbol="BTCUSDT", interval="1h", limit=100, **kw) -> str:
    """q_crypto_klines 加密 K 线。

    源优先级（本机实测）：新浪全球期货日线（直连可用，仅 1d）
    -> Binance（直连被 RST，自动走代理 QUANT_MCP_PROXY）
    -> OKX（DNS 直连失败，走代理）。
    """
    import pandas as pd
    sym = normalize_symbol(symbol) if symbol else normalize_symbol("BTCUSDT")
    pair = sym.crypto or (str(symbol).upper().replace("-", ""))
    if not pair.endswith("USDT"):
        pair = pair + "USDT"
    limit = max(10, min(int(limit or 100), 1000))
    iv = str(interval or "1h").lower()
    if iv not in ("1m", "5m", "15m", "1h", "4h", "1d"):
        raise QuantError("interval 仅支持 1m|5m|15m|1h|4h|1d", "改用 1h。")

    errs: List[str] = []
    df = None
    src = ""
    base = pair.replace("USDT", "")

    # 源1: 新浪全球期货日线（直连，仅日线）
    if iv == "1d":
        try:
            rows = _sina_crypto_daily(base, limit)
            df = pd.DataFrame(rows)
            src = "新浪全球期货日线(直连)"
        except Exception as e:  # noqa: BLE001
            errs.append("新浪全球期货日线: %s: %s" % (type(e).__name__, str(e)[:120]))

    # 源2: Binance（直连失败自动回退代理）
    if df is None or len(df) == 0:
        try:
            url = ("https://api.binance.com/api/v3/klines?symbol={p}&interval={i}&limit={n}"
                   .format(p=pair, i=iv, n=limit))
            js = http_get_json(url, timeout=10.0, retries=1, host_key="binance")
            rows = [{"date": _dt.datetime.fromtimestamp(r[0] / 1000, _dt.timezone.utc)
                     .strftime("{}-{}-{} {}:{}".format("%Y", "%m", "%d", "%H", "%M")),
                     "open": float(r[1]), "high": float(r[2]),
                     "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                     "amount": float(r[7])} for r in js]
            df = pd.DataFrame(rows)
            src = "Binance(直连或代理)"
        except Exception as e:  # noqa: BLE001
            errs.append("Binance(直连/代理 %s): %s: %s"
                        % (PROXY, type(e).__name__, str(e)[:120]))
            df = None
    # 源3: OKX 兜底
    if df is None or len(df) == 0:
        try:
            inst = pair.replace("USDT", "-USDT")
            bar = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}[iv]
            url = ("https://www.okx.com/api/v5/market/candles?instId={i}&bar={b}&limit={n}"
                   .format(i=inst, b=bar, n=min(limit, 300)))
            js = http_get_json(url, timeout=10.0, retries=1, host_key="okx")
            data = (js or {}).get("data") or []
            rows = [{"date": _dt.datetime.fromtimestamp(int(r[0]) / 1000, _dt.timezone.utc)
                     .strftime("{}-{}-{} {}:{}".format("%Y", "%m", "%d", "%H", "%M")),
                     "open": float(r[1]), "high": float(r[2]),
                     "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                     "amount": None} for r in data]
            df = pd.DataFrame(rows).iloc[::-1].reset_index(drop=True)
            src = "OKX(直连或代理)"
        except Exception as e:  # noqa: BLE001
            errs.append("OKX(直连/代理 %s): %s: %s"
                        % (PROXY, type(e).__name__, str(e)[:120]))
            df = None
    if df is None or len(df) == 0:
        raise QuantError(
            "加密行情全部数据源失败（%s）" % "; ".join(errs),
            "补救：1) 新浪全球期货日线只覆盖日线，请用 interval=1d；"
            "2) 分钟/小时线需访问 Binance/OKX，先启动本机代理"
            "（实测 127.0.0.1:7890 可用、7897 未运行），"
            "再设置 QUANT_MCP_PROXY=http://127.0.0.1:7890 后重试。")

    c = df["close"].astype(float)
    import numpy as np
    ret = c.pct_change().dropna()
    vol = float(np.std(ret, ddof=1)) if len(ret) > 2 else 0.0
    peak = c.cummax()
    mdd = float(((c / peak) - 1).min())
    # 部分数据源（如新浪全球期货）不提供成交量，返回字段恒为 "0"；
    # 此时标注"数据源未提供"而非展示具误导性的 0。
    try:
        vol_ok = bool(pd.to_numeric(df["volume"], errors="coerce").fillna(0).abs().sum() > 0)
    except Exception:
        vol_ok = False

    out = ["# %s 加密 K 线 (%s, %d 根, 源: %s)" % (pair, iv, len(df), src), ""]
    rows = [[r["date"], fmt_num(r["open"], 2), fmt_num(r["high"], 2), fmt_num(r["low"], 2),
             fmt_num(r["close"], 2),
             (fmt_num(r["volume"], 2) if vol_ok else "数据源未提供")]
            for _, r in df.tail(25).iterrows()]
    out.append(md_table(["时间(UTC)", "开", "高", "低", "收", "量"], rows))
    if not vol_ok:
        out += ["", "> ℹ️ 成交量列：当前数据源（%s）不提供成交量（接口恒返回 0），"
                    "故标注为“数据源未提供”而非 0。需成交量请改用 Binance/OKX（需本地代理）。" % src]
    out += ["", "## 统计", md_table(["指标", "值"], [
        ["区间", "%s ~ %s" % (df["date"].iloc[0], df["date"].iloc[-1])],
        ["最新价", fmt_num(c.iloc[-1], 2)],
        ["区间涨跌幅", fmt_pct((c.iloc[-1] / c.iloc[0] - 1) * 100)],
        ["单根波动率", fmt_pct(vol * 100)],
        ["最大回撤", fmt_pct(mdd * 100)],
        ["最高/最低", "%s / %s" % (fmt_num(c.max(), 2), fmt_num(c.min(), 2))],
    ])]
    return "\n".join(out)


def q_ExportCsv(kind="klines", symbol=None, filename=None, period="daily",
                start=None, end=None, limit=5000, **kw) -> str:
    """q_export_csv 导出 CSV。"""
    import pandas as pd
    ensure_dirs()
    kind = (kind or "klines").lower()
    ts = beijing_now().strftime("%Y%m%d_%H%M%S")

    if kind in ("klines", "kline", "k"):
        if not symbol:
            raise QuantError("导出 K 线需要 symbol", "传入如 600519。")
        df = get_klines(symbol, period, start, end, int(limit or 5000))
        name = filename or ("klines_%s_%s_%s.csv" % (symbol, period, ts))
    elif kind in ("backtest", "bt"):
        if not symbol:
            raise QuantError("导出回测需要 symbol", "传入如 600519。")
        df_in = get_klines(symbol, "daily", start, end, int(limit or 3000))
        res = run_backtest(df_in, kw.get("strategy", "ma"), int(kw.get("fast", 5)),
                           int(kw.get("slow", 20)), float(kw.get("cash", 100000.0)),
                           float(kw.get("fee_bps", 3.0)), float(kw.get("slippage_bps", 2.0)))
        df = pd.DataFrame({"date": res["dates"], "equity": res["equity"]})
        name = filename or ("backtest_%s_%s.csv" % (symbol, ts))
    elif kind in ("factor", "ic"):
        raise QuantError("因子导出请先用 q_factor_ic 查看结果",
                         "当前支持 kind=klines|backtest。")
    else:
        raise QuantError("kind 不支持: %s" % kind, "可选 klines|backtest。")

    path = os.path.join(EXPORT_DIR, name)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    size = os.path.getsize(path)
    out = ["# 导出完成", ""]
    out.append(md_table(["项", "值"], [
        ["类型", kind], ["标的", symbol or "-"],
        ["绝对路径", path], ["行数", len(df)], ["列数", len(df.columns)],
        ["大小", fmt_big(size) + " (%d 字节)" % size],
        ["列名", ", ".join(map(str, df.columns))],
    ]))
    return "\n".join(out)


def q_CacheClear(scope="all", **kw) -> str:
    """q_cache_clear 清理缓存。"""
    scope = (scope or "all").lower()
    ensure_dirs()
    before = dir_size(CACHE_DIR)
    removed, freed = 0, 0
    if os.path.isdir(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, f)
            if not os.path.isfile(fp):
                continue
            low = f.lower()
            hit = (scope == "all"
                   or (scope == "klines" and low.startswith("kline_"))
                   or (scope == "quotes" and low.startswith(("quote", "spot"))))
            if not hit:
                continue
            try:
                sz = os.path.getsize(fp)
                os.remove(fp)
                removed += 1
                freed += sz
            except Exception:
                continue
    after = dir_size(CACHE_DIR)
    out = ["# 缓存清理", ""]
    out.append(md_table(["项", "值"], [
        ["scope", scope], ["删除文件数", removed],
        ["释放字节", "%d (%s)" % (freed, fmt_big(freed))],
        ["清理前", "%d (%s)" % (before, fmt_big(before))],
        ["剩余大小", "%d (%s)" % (after, fmt_big(after))],
        ["缓存目录", CACHE_DIR],
    ]))
    return "\n".join(out)


# ============================================================================
# 段 11. MCP 工具清单（schema）
# ============================================================================
def _schema(props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": True}


TOOLS: List[Dict[str, Any]] = [
    {"name": "q_env", "fn": q_Env,
     "description": "环境自检：Python/依赖库版本、缓存目录与大小、网络可达性（腾讯/东财/新浪/币安/OKX）、当前北京时间与 UTC。",
     "inputSchema": _schema({})},
    {"name": "q_quote", "fn": q_Quote,
     "description": "实时行情快照。支持 600519 / sh600519 / 000001.SZ / 00700.HK / BTCUSDT 等写法，自动规范化。返回代码/名称/现价/涨跌幅/开高低/量额/时间/数据源。",
     "inputSchema": _schema({
         "symbols": {"type": "array", "items": {"type": "string"},
                     "description": "标的数组，如 [\"600519\",\"000001.SZ\",\"00700.HK\"]"}},
         ["symbols"])},
    {"name": "q_klines", "fn": q_Klines,
     "description": "历史日/周/月 K 线（前复权，akshare 主源 + 东财兜底，parquet 缓存）。返回最近 N 根简化表 + 区间涨跌幅/波动率/最大回撤。",
     "inputSchema": _schema({
         "symbol": {"type": "string"}, "period": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
         "start": {"type": "string"}, "end": {"type": "string"},
         "limit": {"type": "integer", "default": 250}}, ["symbol"])},
    {"name": "q_indicators", "fn": q_Indicators,
     "description": "技术指标计算（纯本地，不联网）：ma/ema/macd/rsi/boll/atr/kdj，返回当前值、最近 5 根序列与信号解读。",
     "inputSchema": _schema({
         "symbol": {"type": "string"}, "period": {"type": "string"},
         "indicators": {"type": "array", "items": {"type": "string"}},
         "limit": {"type": "integer", "default": 250}}, ["symbol"])},
    {"name": "q_backtest_ma", "fn": q_BacktestMa,
     "description": "回测（ma|momentum|breakout|rsi_reversion）。含手续费与滑点、次日开盘成交（无未来函数）。返回总收益/年化/最大回撤/夏普/胜率/盈亏比/交易次数/持仓占比/逐笔交易/净值曲线。",
     "inputSchema": _schema({
         "symbol": {"type": "string"}, "strategy": {"type": "string"},
         "fast": {"type": "integer", "default": 5}, "slow": {"type": "integer", "default": 20},
         "start": {"type": "string"}, "end": {"type": "string"},
         "cash": {"type": "number", "default": 100000},
         "fee_bps": {"type": "number", "default": 3}, "slippage_bps": {"type": "number", "default": 2}},
         ["symbol"])},
    {"name": "q_optimize_grid", "fn": q_OptimizeGrid,
     "description": "参数网格寻优（fast×slow 或自定义 grid），输出 Top10 组合，含样本内/样本外 7:3 分割，明确标注样本外列以防过拟合。",
     "inputSchema": _schema({
         "symbol": {"type": "string"}, "grid": {"type": "object"},
         "start": {"type": "string"}, "end": {"type": "string"},
         "metric": {"type": "string", "enum": ["sharpe", "calmar", "annual_return", "total_return"]},
         "split": {"type": "number", "default": 0.7},
         "strategy": {"type": "string"}}, ["symbol"])},
    {"name": "q_factor_ic", "fn": q_FactorIc,
     "description": "因子 IC/IR 分析（动量|反转|波动率|换手率|量比|市值）。Rank IC 序列、IC 均值/ICIR/胜率、5 层分层收益。",
     "inputSchema": _schema({
         "universe": {"description": "股票池，'hs300'/'zz500' 或代码数组"},
         "factor": {"type": "string"}, "period": {"type": "integer", "default": 20},
         "start": {"type": "string"}, "end": {"type": "string"},
         "max_stocks": {"type": "integer", "default": 40}})},
    {"name": "q_market_overview", "fn": q_MarketOverview,
     "description": "市场全景：主要指数（上证/深证/创业板/科创50/北证50/恒生/纳指/标普/比特币）、两市成交额、涨跌家数、北向资金、行业板块涨跌幅前 10。",
     "inputSchema": _schema({})},
    {"name": "q_screen", "fn": q_Screen,
     "description": "选股器。pool=hs300|zz500|all 或代码列表；conditions 如 'pe<30,pb<3,roe>10,涨幅>3,量比>2,市值<200亿'。返回前 50 只与命中原因。",
     "inputSchema": _schema({
         "pool": {"type": "string", "default": "hs300"},
         "conditions": {"type": "string"}, "limit": {"type": "integer", "default": 50}})},
    {"name": "q_crypto_klines", "fn": q_CryptoKlines,
     "description": "加密 K 线（Binance → OKX 兜底），interval=1h|4h|1d，返回 OHLCV 表与关键统计。",
     "inputSchema": _schema({
         "symbol": {"type": "string", "default": "BTCUSDT"},
         "interval": {"type": "string", "enum": ["1m", "5m", "15m", "1h", "4h", "1d"], "default": "1h"},
         "limit": {"type": "integer", "default": 100}})},
    {"name": "q_export_csv", "fn": q_ExportCsv,
     "description": "把 K 线/回测结果导出到 D:\\量化\\data\\exports\\*.csv，返回绝对路径与行数。",
     "inputSchema": _schema({
         "kind": {"type": "string", "enum": ["klines", "backtest"], "default": "klines"},
         "symbol": {"type": "string"}, "filename": {"type": "string"},
         "period": {"type": "string", "default": "daily"},
         "start": {"type": "string"}, "end": {"type": "string"},
         "limit": {"type": "integer", "default": 5000}})},
    {"name": "q_cache_clear", "fn": q_CacheClear,
     "description": "清理缓存（scope=all|klines|quotes），返回释放字节数与剩余大小。",
     "inputSchema": _schema({"scope": {"type": "string", "enum": ["all", "klines", "quotes"],
                                       "default": "all"}})},
]

TOOL_MAP: Dict[str, Dict[str, Any]] = {t["name"]: t for t in TOOLS}


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """执行工具并包装为 MCP 结果。异常绝不外溢。"""
    tool = TOOL_MAP.get(name)
    if tool is None:
        return {"content": [{"type": "text", "text": "❌ 未知工具: %s。可用: %s"
                             % (name, ", ".join(TOOL_MAP))}], "isError": True}
    args = args or {}
    if not isinstance(args, dict):
        return {"content": [{"type": "text", "text": "❌ 参数必须是对象"}], "isError": True}

    box: Dict[str, Any] = {}

    def _work():
        try:
            box["text"] = tool["fn"](**args)
        except BaseException as e:  # noqa: BLE001
            box["text"] = err_text(name, e)
            box["is_error"] = True
            log("tool %s error: %s" % (name, traceback.format_exc()[-800:]))

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(TOOL_TIMEOUT)
    if th.is_alive():
        return {"content": [{"type": "text", "text":
                             "❌ %s 超时（>%.0f 秒）已中断。\n**降级方案**: 缩小股票池 / 缩短日期区间 / "
                             "减小 limit 或 max_stocks，然后重试。" % (name, TOOL_TIMEOUT)}],
                "isError": True}
    return {"content": [{"type": "text", "text": box.get("text", "") or "(空结果)"}],
            "isError": bool(box.get("is_error"))}


# ============================================================================
# 段 12. MCP stdio JSON-RPC 2.0 协议层（标准库手写）
# ============================================================================
def _send(obj: Dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with _STDOUT_LOCK:
        _REAL_STDOUT.write(line + "\n")
        _REAL_STDOUT.flush()


def _ok(rid, result) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _fail(rid, code: int, message: str, data: Any = None) -> None:
    e: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    _send({"jsonrpc": "2.0", "id": rid, "error": e})


def handle_message(msg: Dict[str, Any]) -> bool:
    """处理一条 JSON-RPC 消息。返回 False 表示应退出。"""
    if not isinstance(msg, dict):
        _fail(None, -32600, "Invalid Request")
        return True
    method = msg.get("method")
    rid = msg.get("id", None)
    is_notification = "id" not in msg or rid is None

    try:
        if method == "initialize":
            _ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif method == "notifications/initialized":
            pass  # 通知，无响应
        elif method == "ping":
            _ok(rid, {})
        elif method == "tools/list":
            _ok(rid, {"tools": [
                {"name": t["name"], "description": t["description"],
                 "inputSchema": t["inputSchema"]} for t in TOOLS]})
        elif method == "tools/call":
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                _fail(rid, -32602, "Invalid params: params must be object")
                return True
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            log("tools/call %s" % name)
            res = call_tool(str(name), args)
            _ok(rid, res)
        elif method in ("resources/list", "prompts/list"):
            key = "resources" if method.startswith("resources") else "prompts"
            _ok(rid, {key: []})
        elif method == "shutdown":
            _ok(rid, {})
            return False
        elif is_notification:
            pass  # 未知通知忽略
        else:
            _fail(rid, -32601, "Method not found: %s" % method)
    except Exception as e:  # noqa: BLE001
        log("handler error: %s" % traceback.format_exc()[-600:])
        if not is_notification:
            _fail(rid, -32603, "Internal error", str(e)[:300])
    return True


def serve_stdio() -> int:
    """主循环：逐行读取 JSON-RPC。"""
    ensure_dirs()
    log("quant_mcp starting (pid=%d, protocol=%s)" % (os.getpid(), PROTOCOL_VERSION))
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        # 兼容 Content-Length 帧（防御性）
        if line.lower().startswith("content-length:"):
            continue
        try:
            msg = json.loads(line)
        except Exception as e:  # noqa: BLE001
            log("bad json: %r" % (e,))
            _fail(None, -32700, "Parse error")
            continue
        if isinstance(msg, list):
            for m in msg:
                if not handle_message(m):
                    return 0
        else:
            if not handle_message(msg):
                return 0
    log("stdin closed, exiting")
    return 0


# ============================================================================
# 段 13. 自检 --selftest
# ============================================================================
def selftest(verbose: bool = False) -> int:
    ensure_dirs()
    results: List[Tuple[str, str, str]] = []  # (status, name, detail)
    full: List[Tuple[str, str]] = []          # (name, 完整输出)

    def run(name: str, fn: Callable[[], str], network: bool = True):
        t0 = time.time()
        try:
            txt = fn()
            dt = time.time() - t0
            head = (txt or "").strip().splitlines()
            summary = " | ".join(h.strip() for h in head[:3] if h.strip())[:190]
            results.append(("PASS", name, "%.1fs  %s" % (dt, summary)))
            full.append((name, txt or ""))
        except QuantError as e:
            dt = time.time() - t0
            if network and any(k in e.reason for k in
                               ("不可达", "全部数据源", "全部 K 线", "网络")):
                results.append(("SKIP", name, "%.1fs  %s -> %s" % (dt, e.reason[:120], e.fallback[:80])))
            else:
                results.append(("FAIL", name, "%.1fs  %s" % (dt, e.reason[:180])))
        except Exception as e:  # noqa: BLE001
            dt = time.time() - t0
            results.append(("FAIL", name, "%.1fs  %s: %s"
                            % (dt, type(e).__name__, str(e)[:150])))

    out("=" * 74)
    out("quant_mcp --selftest  (真实联网调用，单工具上限 %.0fs)" % TOOL_TIMEOUT)
    out("=" * 74)

    run("q_env", lambda: q_Env())
    run("q_quote", lambda: q_Quote(symbols=["600519", "000001.SZ", "00700.HK"]))
    run("q_klines", lambda: q_Klines(symbol="600519", period="daily",
                                     start="20260101", end="20261231", limit=250))
    run("q_indicators", lambda: q_Indicators(symbol="600519", period="daily",
                                             indicators=["ma", "macd", "rsi", "boll", "atr", "kdj"]))
    run("q_backtest_ma", lambda: q_BacktestMa(symbol="600519", fast=5, slow=20,
                                              start="20250101", end="20261231",
                                              cash=1000000.0))
    run("q_optimize_grid", lambda: q_OptimizeGrid(
        symbol="600519", grid={"fast": [5, 10], "slow": [20, 60]},
        start="20250101", end="20261231", metric="sharpe", cash=1000000.0))
    run("q_factor_ic", lambda: q_FactorIc(universe="hs300", factor="动量",
                                          period=20, max_stocks=12))
    run("q_market_overview", lambda: q_MarketOverview())
    run("q_screen", lambda: q_Screen(pool="hs300", conditions="pe<40,pb<8,量比>1", limit=15))
    run("q_crypto_klines", lambda: q_CryptoKlines(symbol="BTCUSDT", interval="1d", limit=50))
    run("q_export_csv", lambda: q_ExportCsv(kind="klines", symbol="600519",
                                            start="20260101", end="20261231",
                                            filename="selftest_600519.csv"))
    run("q_cache_clear", lambda: q_CacheClear(scope="quotes"))

    out()
    for st, name, detail in results:
        icon = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[st]
        out("%s %-18s %s" % (icon, name, detail))

    n_pass = sum(1 for r in results if r[0] == "PASS")
    n_fail = sum(1 for r in results if r[0] == "FAIL")
    n_skip = sum(1 for r in results if r[0] == "SKIP")
    out()
    if verbose:
        out("=" * 74)
        out("真实输出抽样（每项尾部 12 行，用于人工核对价格/日期）")
        out("=" * 74)
        for name, txt in full:
            lines = [ln for ln in txt.strip().splitlines() if ln.strip()]
            out("\n----- %s -----" % name)
            for ln in lines[-12:]:
                out("  " + ln[:200])
    out("-" * 74)
    out("汇总: PASS=%d  FAIL=%d  SKIP=%d  总计=%d" % (n_pass, n_fail, n_skip, len(results)))
    if n_skip:
        out("SKIP 原因: 外部数据源不可达（已返回结构化错误与降级方案，非代码缺陷）。")
    out("=" * 74)
    return 0 if n_fail == 0 else 1


# ============================================================================
# 段 14. 入口
# ============================================================================
def main(argv: List[str]) -> int:
    ensure_dirs()
    if "--selftest" in argv:
        return selftest(verbose=("--verbose" in argv or "-v" in argv))
    if "--version" in argv:
        out("%s %s" % (SERVER_NAME, SERVER_VERSION))
        return 0
    if "--tools" in argv:
        for t in TOOLS:
            out("%-20s %s" % (t["name"], t["description"][:70]))
        return 0
    return serve_stdio()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        log("fatal: %s" % traceback.format_exc())
        print("fatal: %s" % e, file=sys.stderr)
        sys.exit(1)
