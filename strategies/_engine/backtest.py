# -*- coding: utf-8 -*-
"""
D:\\量化\\strategies\\_engine\\backtest.py  —  可复用事件驱动回测引擎 v1.0

设计要点（为什么这么写）
====================================================================
1) 【权益制记账，不是"现金×杠杆"】
   上一轮出过一个真实错误：把"整个现金 × 杠杆倍数"直接当成持仓名义额，既不扣
   保证金、也不算维持保证金率与爆仓，结果跑出 10^26 % 的荒谬收益。根因是这套记法
   在数学上允许仓位随权益无限复利而不受任何约束。

   本引擎采用的正确记法：
       entry_equity  = 建仓前那一刻的权益（cash 全额）
       notional      = entry_equity * leverage        <-- 名义额，只在建仓时刻定一次
       margin        = notional / leverage = entry_equity  <-- 全额保证金占用
       cash          = 剩余可用资金（=0，因为全额保证金）
       equity(t)     = cash + margin + qty * price(t)  <-- 逐 bar 盯市
   由此 equity 只能随价格线性变动，杠杆放大的是**盈亏**而不是**复利基数**，
   物理上封死了指数爆炸。

2) 【爆仓检测】权益 <= 0 即归零并终止回测，设 liquidated 标志。
   同时给出"理论爆仓价"：多头 entry_price*(1-1/lev)，空头 entry_price*(1+1/lev)。
   实践中经纪商会提前强平（维持保证金），所以真实爆仓更早——报告里按此说明。

3) 【成交时点】信号在 bar t 收盘产生 -> 必须在 bar t+1 开盘成交。
   引擎内部统一用 shift：signal.shift(1) 之后落在 bar t+1 的 open 上执行。
   禁止用当根收盘价成交（那是未来函数）。

4) 【成本】每笔成交扣 fee_bps + slippage_bps；
   多头买入价 = open*(1+slip)，卖出价 = open*(1-slip)；空头反向。
   永续持仓按 8 小时结算计资金费率（可传 funding_series）。

5) 【指标口径】全部基于逐 bar 盯市的 equity 序列；夏普用逐 bar 收益按
   bars_per_year 年化；卡玛 = 年化 / |最大回撤|。
"""

from __future__ import annotations

import sys
import math
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BAR_WIDTH_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000,
    "1D": 86_400_000, "1W": 604_800_000,
}
BAR_WIDTH_DAYS = {k: v / 86_400_000.0 for k, v in BAR_WIDTH_MS.items()}
DAYS_PER_YEAR = 365.0          # 加密市场 7x24，用自然日年化


# ----------------------------------------------------------------------
# 数据
# ----------------------------------------------------------------------
def load_parquet(path: str) -> pd.DataFrame:
    """读取 fetch_okx.py 落盘的 parquet，返回标准列。"""
    df = pd.read_parquet(path)
    tcol = "ts" if "ts" in df.columns else df.columns[0]
    df = df.rename(columns={tcol: "ts"})
    need = ["ts", "open", "high", "low", "close", "vol"]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise ValueError("parquet 缺列: %s (实际: %s)" % (miss, list(df.columns)))
    df = df[need + [c for c in ("volCcy", "confirm") if c in df.columns]].copy()
    df["ts"] = df["ts"].astype("int64")
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    return df


# ----------------------------------------------------------------------
# 资金费率：把 8 小时结算点对齐到 bar
# ----------------------------------------------------------------------
def load_funding(path: str) -> pd.DataFrame:
    """读取资金费率 parquet -> DataFrame[ts(ms,UTC), fundingRate(float)]。"""
    f = pd.read_parquet(path)
    f = f.rename(columns={"fundingTime": "ts"})
    f = f[["ts", "fundingRate"]].copy()
    f["ts"] = f["ts"].astype("int64")
    f["fundingRate"] = f["fundingRate"].astype(float)
    return f.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def apply_funding(events: pd.DataFrame, bar_ts: np.ndarray, bar_width_ms: int,
                  on_pay) -> float:
    """
    把资金费率**逐个结算点**作用到仓位，而不是"整根 bar 求和后扣一次"。

    为什么必须这样：OKX 每 8 小时结算一次。一根 1D bar 覆盖 3 个结算点，
    一根 4H bar 覆盖 0~1 个。如果先把一根 bar 内的费率求和、再当作一次结算，
    语义就变成了"一天只结一次"，金额会错 3 倍（1D）或 0/1 倍（4H）。
    这里保持"每次结算发生在它自己的时间点"，只把费率乘当前名义额的负数加到现金。

    on_pay(rate) 由调用方提供，负责把这个结算点造成的现金变动写进 cash，
    并返回实际扣款额（用于统计）。返回值为累计扣款。
    """
    if events is None or len(events) == 0:
        return 0.0
    fts = events["ts"].to_numpy(dtype=np.int64)
    fr = events["fundingRate"].to_numpy(dtype=float)
    order = np.argsort(fts)
    fts, fr = fts[order], fr[order]
    total = 0.0
    n = len(bar_ts)
    for j in range(len(fts)):
        # 找到覆盖该结算点的 bar 下标：最大的 i 使 bar_ts[i] <= fts[j]
        i = int(np.searchsorted(bar_ts, fts[j], side="right") - 1)
        if 0 <= i < n:
            total += on_pay(fts[j], float(fr[j]))
    return total


# ----------------------------------------------------------------------
# 核心回测
# ----------------------------------------------------------------------
def run_backtest(
    df: pd.DataFrame,
    signals: pd.Series,
    *,
    bar: str = "4H",
    init_cash: float = 10_000.0,
    leverage: float = 1.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    allow_short: bool = True,
    funding: np.ndarray | None = None,
    name: str = "strategy",
    TRACE: list | None = None,
    TRACE_I: list | None = None,
) -> dict:
    """
    signals: 与 df 等长的 Series，取值 {1 做多, -1 做空, 0 空仓}，索引对齐 df。
             **信号在 bar t 收盘产生，引擎在 bar t+1 开盘成交**（内部 shift）。
    返回 dict：metrics / equity_curve / trades / liquidated
    """
    if bar not in BAR_WIDTH_MS:
        raise ValueError("未知 bar: %s" % bar)
    w_ms = BAR_WIDTH_MS[bar]
    bars_per_year = DAYS_PER_YEAR * 86_400_000.0 / w_ms
    if TRACE_I is None:
        TRACE_I = [0]

    df = df.reset_index(drop=True)
    n = len(df)
    ts = df["ts"].to_numpy(dtype=np.int64)
    op = df["open"].to_numpy(dtype=float)
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    cl = df["close"].to_numpy(dtype=float)

    # --- t+1 开盘成交：把 t 收盘的信号搬到 t+1 执行 ---
    sig_raw = pd.Series(signals).reset_index(drop=True).astype(float).fillna(0.0)
    sig = np.zeros(n, dtype=float)
    sig[1:] = np.sign(sig_raw.to_numpy()[: n - 1])       # 关键：shift(1)
    if not allow_short:
        sig = np.where(sig < 0, 0.0, sig)

    fee = fee_bps / 10_000.0
    slip = slippage_bps / 10_000.0
    inv_lev = 1.0 / leverage

    funding_df = funding
    if funding_df is not None and len(funding_df) > 0:
        _f = funding_df.sort_values("ts").reset_index(drop=True)
        fts = _f["ts"].to_numpy(dtype=np.int64)
        frate = _f["fundingRate"].to_numpy(dtype=float)
        _newest = int(ts.max()) + w_ms
        funding_ok = bool(fts.min() <= _newest and fts.max() >= int(ts.min()))
        # 把结算点按 bar 分桶：[idx_start, idx_end) 落在该 bar 内
        slot = np.searchsorted(ts, fts, side="right") - 1
    else:
        fts = np.array([], dtype=np.int64)
        frate = np.array([], dtype=float)
        slot = np.array([], dtype=np.int64)
        funding_ok = False

    cash = float(init_cash)          # 未占用资金
    qty = 0.0                        # >0 多头, <0 空头
    avg_cost = 0.0                   # 开仓价（含滑点后的实际成交价）
    margin = 0.0                     # 占用保证金
    side = 0                         # 当前仓位方向

    equity_curve = np.empty(n, dtype=float)
    pos_curve = np.zeros(n, dtype=float)
    fee_paid_total = 0.0
    traded_notional = 0.0
    entry_equity = max(init_cash, 1e-12)

    trades: list[dict] = []
    open_trade: dict | None = None
    liquidated = False
    liq_idx = None
    funding_paid_total = 0.0
    funding_events_applied = 0
    if TRACE is not None:
        TRACE.clear()

    # ==================================================================
    # 现金流记账（无歧义版）
    # ------------------------------------------------------------------
    # 状态只有三个：cash / qty / avg_cost。保证金不再单独记账——因为
    # "保证金"本身就是被锁住的现金，再单独加一次就会把权益重复计入
    # （上一版就是栽在这里：equity = cash + margin + qty*px 中 margin 与
    #  qty*avg_cost 描述的是同一笔钱，导致权益被高估一个名义额）。
    #
    # 恒等式（可逐 bar 验证，也是 selfcheck 的回归判据）：
    #     equity(t) = init_cash - 累计手续费 - 累计资金费 + qty*(price(t) - avg_cost)
    # 建仓：cash -= notional + fee      （把名义额连同手续费一起从现金里划走）
    # 平仓：cash += notional + pnl - fee
    # 盯市：equity = cash + qty*price   （cash 里含被锁住的保证金）
    #
    # 这样 equity 只能随价格线性变动，杠杆放大的是盈亏而不是复利基数，
    # 从数学上封死了"10^26%"式的指数爆炸。
    # ==================================================================
    def mark(px: float) -> float:
        return cash + qty * px

    def fill_price(px: float, direction: int) -> float:
        """direction=+1 买入(做多/平空), -1 卖出(做空/平多)"""
        return px * (1.0 + slip) if direction > 0 else px * (1.0 - slip)

    def close_position(i: int, px: float, reason: str) -> None:
        """按 px 平掉当前仓位，把盈亏与手续费落回现金。"""
        nonlocal cash, qty, avg_cost, side, open_trade
        nonlocal fee_paid_total, traded_notional
        pnl = qty * (px - avg_cost)
        notional_close = abs(qty) * px
        c = notional_close * fee
        # 现金回笼 = 卖出所得 qty*px，减去"当初买它花掉的成本" qty*avg_cost，
        # 差额即盈亏。等价于 cash += abs(qty)*avg_cost + pnl - c。
        # 注意**不能**写 notional_close + pnl：notional_close 里已经含了盈亏，
        # 再加 pnl 就等于把名义额算两遍，会让总收益凭空翻倍（本轮修掉的 bug）。
        cash += abs(qty) * avg_cost + pnl - c
        fee_paid_total += c
        traded_notional += notional_close
        if open_trade is not None:
            open_trade.update(
                exit_ts=int(ts[i]), exit_price=float(px), exit_idx=i,
                exit_reason=reason, gross_pnl=float(pnl),
                fees=float(open_trade.get("fees", 0.0) + c),
            )
            open_trade["net_pnl"] = float(pnl - open_trade["fees"])
            open_trade["ret_pct"] = float(
                100.0 * open_trade["net_pnl"] / max(open_trade["entry_equity"], 1e-12))
            open_trade["bars_held"] = i - open_trade["entry_idx"]
            trades.append(open_trade)
            open_trade = None
        qty = 0.0
        avg_cost = 0.0
        side = 0

    for i in range(n):
        TRACE_I[0] = i
        # ---------- 1) 本 bar 开盘执行上一根收盘产生的信号 ----------
        target = sig[i]
        if side != target and not liquidated:
            px_raw = op[i]
            if side != 0:
                close_position(i, fill_price(px_raw, -1 if side > 0 else 1),
                               "signal_flip")
            if target != 0 and cash > 0:
                px = fill_price(px_raw, 1 if target > 0 else -1)
                entry_equity = cash
                notional = entry_equity * leverage
                q = notional / px
                c = notional * fee                # 按名义额计手续费
                cash -= notional + c              # 名义额 + 手续费一起划走
                fee_paid_total += c
                traded_notional += notional
                qty = q if target > 0 else -q
                avg_cost = px
                side = 1 if target > 0 else -1
                open_trade = dict(
                    entry_ts=int(ts[i]), entry_price=float(px), entry_idx=i,
                    side="long" if side > 0 else "short",
                    qty=float(qty), notional=float(notional),
                    leverage=float(leverage), entry_equity=float(entry_equity),
                    fees=float(c),
                )

        # ---------- 2) 本 bar 内的资金费率结算（逐个结算点，非求和扣一次） ----------
        if side != 0 and len(fts):
            for j in np.where(slot == i)[0]:
                if fts[j] < ts[i]:
                    continue
                pay = -qty * avg_cost * frate[j]   # 多头付正费率；空头相反
                cash += pay
                funding_paid_total += pay
                funding_events_applied += 1

        # ---------- 3) 逐 bar 盯市 ----------
        eq = mark(cl[i])
        if TRACE is not None and i < 3:
            TRACE.append(dict(bar=i, stage="mark", px=float(cl[i]), cash=cash,
                              margin=abs(qty) * avg_cost * inv_lev, qty=qty,
                              side=side, eq=float(eq)))
        equity_curve[i] = eq
        pos_curve[i] = qty * cl[i]

        # ---------- 4) 爆仓检测（盘中 low/high + 收盘兜底） ----------
        # 只按收盘判爆仓会高估高杠杆存活率：真实强平发生在盘中触达维持保证金时。
        if side > 0:
            worst_px = lo[i]
        elif side < 0:
            worst_px = hi[i]
        else:
            worst_px = None
        if worst_px is not None and mark(worst_px) <= 0.0:
            loss = -float(open_trade["entry_equity"]) if open_trade else 0.0
            if open_trade is not None:
                open_trade.update(
                    exit_ts=int(ts[i]), exit_price=float(worst_px), exit_idx=i,
                    exit_reason="Liquidation(intrabar)",
                    gross_pnl=loss, net_pnl=loss,
                    fees=float(open_trade.get("fees", 0.0)),
                    ret_pct=-100.0, bars_held=i - open_trade["entry_idx"],
                )
                trades.append(open_trade)
                open_trade = None
            liquidated = True
            liq_idx = i
            cash = 0.0
            qty = 0.0
            avg_cost = 0.0
            side = 0
            equity_curve[i] = 0.0
            pos_curve[i] = 0.0
            equity_curve[i + 1:] = 0.0
            pos_curve[i + 1:] = 0.0
            break

        if side != 0 and eq <= 0.0:
            loss = -float(open_trade["entry_equity"]) if open_trade else 0.0
            if open_trade is not None:
                open_trade.update(
                    exit_ts=int(ts[i]), exit_price=float(cl[i]), exit_idx=i,
                    exit_reason="Liquidation", gross_pnl=loss, net_pnl=loss,
                    fees=float(open_trade.get("fees", 0.0)),
                    ret_pct=-100.0, bars_held=i - open_trade["entry_idx"],
                )
                trades.append(open_trade)
                open_trade = None
            liquidated = True
            liq_idx = i
            cash = 0.0
            qty = 0.0
            avg_cost = 0.0
            side = 0
            equity_curve[i] = 0.0
            pos_curve[i] = 0.0
            equity_curve[i + 1:] = 0.0
            pos_curve[i + 1:] = 0.0
            break

    # 收尾：最后一根仍持仓则按最后一根收盘价平掉（记为 eod_close）
    if side != 0 and not liquidated:
        close_position(n - 1, fill_price(cl[-1], -1 if side > 0 else 1), "eod_close")
        equity_curve[-1] = cash

    eq_series = pd.Series(equity_curve)
    metrics = compute_metrics(
        eq_series, trades, bars_per_year=bars_per_year, init_cash=init_cash,
        fee_total=fee_paid_total, traded_notional=traded_notional,
        liquidated=liquidated, funding_total=funding_paid_total,
        leverage=leverage, bar=bar,
    )
    metrics["name"] = name
    metrics["funding_coverage_ok"] = bool(funding_ok)
    metrics["funding_events_applied"] = int(funding_events_applied)
    if not funding_ok:
        metrics["funding_note"] = "资金费率历史未覆盖本回测区间 -> 未计资金成本（成本被低估）"
    return {
        "metrics": metrics,
        "equity_curve": eq_series,
        "position": pd.Series(pos_curve),
        "trades": trades,
        "liquidated": liquidated,
        "liquidated_idx": liq_idx,
        "ts": ts,
    }


# ----------------------------------------------------------------------
# 指标
# ----------------------------------------------------------------------
def compute_metrics(eq: pd.Series, trades, *, bars_per_year: float, init_cash: float,
                    fee_total: float, traded_notional: float, liquidated: bool,
                    funding_total: float, leverage: float, bar: str) -> dict:
    eq = pd.Series(eq).astype(float).reset_index(drop=True)
    n = len(eq)
    final = float(eq.iloc[-1])
    total_ret = (final / init_cash - 1.0) * 100.0 if init_cash > 0 else float("nan")

    years = n / bars_per_year if bars_per_year > 0 else float("nan")
    if final > 0 and years > 0:
        annual = ((final / init_cash) ** (1.0 / years) - 1.0) * 100.0
    else:
        annual = -100.0

    # 最大回撤（在权益序列上）
    running_max = eq.cummax()
    # 避免除零：初始权益为兜底
    denom = running_max.replace(0.0, np.nan)
    dd = (eq / denom - 1.0) * 100.0
    max_dd = float(dd.min()) if len(dd) else 0.0
    if not np.isfinite(max_dd):
        max_dd = 0.0

    # 逐 bar 收益
    rets = eq.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(rets) > 1 and rets.std(ddof=1) > 0:
        sharpe = float(rets.mean() / rets.std(ddof=1) * math.sqrt(bars_per_year))
    else:
        sharpe = 0.0
    calmar = float(annual / abs(max_dd)) if max_dd < -1e-9 else 0.0
    vol_annual = float(rets.std(ddof=1) * math.sqrt(bars_per_year) * 100.0) if len(rets) > 1 else 0.0

    # 交易统计
    closed = [t for t in trades if t.get("net_pnl") is not None]
    wins = [t for t in closed if t["net_pnl"] > 0]
    losses = [t for t in closed if t["net_pnl"] <= 0]
    win_rate = 100.0 * len(wins) / len(closed) if closed else 0.0
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    profit_factor = float(gross_win / gross_loss) if gross_loss > 1e-12 else (
        float("inf") if gross_win > 0 else 0.0)
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0

    avg_eq = float(eq.mean()) if n else init_cash
    turnover_pct = 100.0 * traded_notional / avg_eq if avg_eq > 0 else 0.0
    fee_pct_of_init = 100.0 * fee_total / init_cash if init_cash > 0 else 0.0

    return {
        "bar": bar,
        "bars": n,
        "years": round(years, 3),
        "init_cash": init_cash,
        "final_equity": round(final, 4),
        "total_return_pct": round(total_ret, 4),
        "annual_return_pct": round(annual, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "calmar": round(calmar, 4),
        "annual_vol_pct": round(vol_annual, 4),
        "trades": len(closed),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": (round(profit_factor, 4) if np.isfinite(profit_factor) else None),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "turnover_pct": round(turnover_pct, 2),
        "fee_total": round(fee_total, 4),
        "fee_pct_of_init": round(fee_pct_of_init, 4),
        "funding_total": round(funding_total, 4),
        "funding_pct_of_init": round(100.0 * funding_total / init_cash, 4) if init_cash > 0 else 0.0,
        "leverage": leverage,
        "liquidated": bool(liquidated),
    }


# ----------------------------------------------------------------------
# 基准：buy & hold（引擎自检的关键对照）
# ----------------------------------------------------------------------
def buy_and_hold(df: pd.DataFrame, init_cash=10_000.0, fee_bps=5.0,
                 slippage_bps=2.0, bar="4H", leverage=1.0) -> dict:
    """永远持有多头：t=0 之后一直 signal=1。"""
    sig = pd.Series(np.ones(len(df)))
    return run_backtest(df, sig, bar=bar, init_cash=init_cash, leverage=leverage,
                        fee_bps=fee_bps, slippage_bps=slippage_bps,
                        allow_short=False, name="buy_and_hold")


def naive_return(df: pd.DataFrame) -> dict:
    """对照口径：直接用收盘价算的区间涨跌幅（不含成本、不复利、无撮合）。"""
    c0 = float(df["close"].iloc[0])
    c1 = float(df["close"].iloc[-1])
    return {
        "close_first": c0, "close_last": c1,
        "pct": round((c1 / c0 - 1.0) * 100.0, 4),
    }


# ----------------------------------------------------------------------
# 样本内/外切分
# ----------------------------------------------------------------------
def is_oos_split(df: pd.DataFrame, ratio: float = 0.7):
    k = int(len(df) * ratio)
    return df.iloc[:k].reset_index(drop=True), df.iloc[k:].reset_index(drop=True)


# ----------------------------------------------------------------------
# Walk-forward
# ----------------------------------------------------------------------
def walk_forward(df: pd.DataFrame, strategy_fn, param_grid: list[dict],
                 *, n_folds: int = 5, is_frac: float = 0.6,
                 objective: str = "sharpe", warmup: int = 250, **bt_kw) -> dict:
    """
    滚动窗口重优化：
      - 把 OOS 区切成 n_folds 段（跳过前 warmup 根作为首段样本内的最小长度）
      - 第 k 折：IS = [0, start_k)，OOS = [start_k, end_k)
      - 在 IS 上按 objective 选最优参数，在 OOS 上运行，拼出连续 OOS 净值
    返回: 拼接后的 OOS 权益曲线 + 各折选中的参数 + 拼接指标
    """
    n = len(df)
    if n <= warmup + n_folds:
        return {"ok": False, "reason": "样本太短: n=%d warmup=%d" % (n, warmup)}
    start0 = max(warmup, int(n * (1 - 1.0 / n_folds)))
    # OOS 区 = [start0, n)，切成 n_folds 段
    bounds = np.linspace(start0, n, n_folds + 1).astype(int)

    folds = []
    stitched_eq = []
    cum = 1.0
    for k in range(n_folds):
        oos_lo, oos_hi = int(bounds[k]), int(bounds[k + 1])
        if oos_hi - oos_lo < 5:
            continue
        is_df = df.iloc[:oos_lo].reset_index(drop=True)
        oos_df = df.iloc[oos_lo:oos_hi].reset_index(drop=True)
        best, best_score = None, -np.inf
        for p in param_grid:
            try:
                sig = strategy_fn(is_df, p)
                r = run_backtest(is_df, sig, **bt_kw)
                s = r["metrics"].get(objective) or -np.inf
                s = float(s)
                if r["liquidated"]:
                    s = -abs(s) - 100.0        # 爆仓的参数直接淘汰
            except Exception:
                continue
            if s > best_score:
                best_score, best = s, p
        if best is None:
            continue
        sig_oos = strategy_fn(oos_df, best)
        r_oos = run_backtest(oos_df, sig_oos, **bt_kw)
        eq = r_oos["equity_curve"].to_numpy(dtype=float)
        eq0 = float(eq[0]) if len(eq) else 1.0
        base = bt_kw.get("init_cash", 10_000.0)
        eq = eq / (eq0 if eq0 > 0 else 1.0) * base if eq0 > 0 else np.zeros_like(eq)
        stitched_eq.append(eq)
        folds.append({
            "fold": k, "is_bars": len(is_df), "oos_bars": len(oos_df),
            "is_end_ts": int(is_df["ts"].iloc[-1]) if len(is_df) else None,
            "oos_start_ts": int(oos_df["ts"].iloc[0]) if len(oos_df) else None,
            "oos_end_ts": int(oos_df["ts"].iloc[-1]) if len(oos_df) else None,
            "best_params": best,
            "is_score": round(best_score, 4),
            "oos_total_return_pct": r_oos["metrics"]["total_return_pct"],
            "oos_max_dd_pct": r_oos["metrics"]["max_drawdown_pct"],
            "oos_trades": r_oos["metrics"]["trades"],
            "oos_liquidated": r_oos["liquidated"],
        })
    if not stitched_eq:
        return {"ok": False, "reason": "无有效折"}
    all_eq = np.concatenate(stitched_eq)
    base = bt_kw.get("init_cash", 10_000.0)
    eqs = pd.Series(all_eq)
    rets = eqs.pct_change().dropna()
    bar = bt_kw.get("bar", "4H")
    bpy = DAYS_PER_YEAR * 86_400_000.0 / BAR_WIDTH_MS[bar]
    total = (eqs.iloc[-1] / base - 1.0) * 100.0
    years = len(eqs) / bpy
    annual = ((eqs.iloc[-1] / base) ** (1 / years) - 1) * 100 if eqs.iloc[-1] > 0 and years > 0 else -100.0
    rm = eqs.cummax().replace(0, np.nan)
    mdd = float(((eqs / rm - 1) * 100).min())
    sharpe = float(rets.mean() / rets.std(ddof=1) * math.sqrt(bpy)) if len(rets) > 1 and rets.std(ddof=1) > 0 else 0.0
    return {
        "ok": True,
        "folds": folds,
        "stitched_bars": len(eqs),
        "stitched_total_return_pct": round(float(total), 4),
        "stitched_annual_pct": round(float(annual), 4),
        "stitched_max_dd_pct": round(mdd if np.isfinite(mdd) else 0.0, 4),
        "stitched_sharpe": round(sharpe, 4),
        "param_stability": [f["best_params"] for f in folds],
    }


# ----------------------------------------------------------------------
# 便捷：网格搜索（样本内选参数，报样本外）
# ----------------------------------------------------------------------
def grid_is_oos(df, strategy_fn, param_grid, *, objective="sharpe",
                split=0.7, **bt_kw) -> dict:
    is_df, oos_df = is_oos_split(df, split)
    rows = []
    for p in param_grid:
        try:
            sig = strategy_fn(is_df, p)
            r = run_backtest(is_df, sig, **bt_kw)
            m = dict(r["metrics"])
        except Exception as e:
            rows.append({"params": p, "error": str(e)[:80]})
            continue
        m["params"] = p
        m["liquidated"] = r["liquidated"]
        rows.append(m)
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"ok": False, "reason": "全部参数报错", "rows": rows}
    ok_sorted = sorted(ok, key=lambda r: (float(r.get(objective) or -1e9)), reverse=True)
    best = ok_sorted[0]
    sig_oos = strategy_fn(oos_df, best["params"])
    r_oos = run_backtest(oos_df, sig_oos, **bt_kw)
    return {
        "ok": True,
        "is_df_bars": len(is_df), "oos_df_bars": len(oos_df),
        "is_start_ts": int(is_df["ts"].iloc[0]), "is_end_ts": int(is_df["ts"].iloc[-1]),
        "oos_start_ts": int(oos_df["ts"].iloc[0]), "oos_end_ts": int(oos_df["ts"].iloc[-1]),
        "best_params": best["params"],
        "is_metrics": {k: v for k, v in best.items() if k != "params"},
        "oos_metrics": r_oos["metrics"],
        "oos_liquidated": r_oos["liquidated"],
        "oos_trades_sample": r_oos["trades"][:5],
        "all_is_rows": [
            {"params": r["params"], "total_return_pct": r.get("total_return_pct"),
             "annual_return_pct": r.get("annual_return_pct"),
             "max_drawdown_pct": r.get("max_drawdown_pct"),
             "sharpe": r.get("sharpe"), "trades": r.get("trades"),
             "liquidated": r.get("liquidated")}
            for r in ok_sorted
        ],
    }
