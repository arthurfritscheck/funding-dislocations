"""Backtest metrics (docs/plan.md Phase 4 item 8). Returns are on total capital by default
(`config.yaml` margin.return_base); return on deployed capital is reported alongside."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.engine import COMPONENTS, HOUR, Result

DAYS_PER_YEAR = 365


def daily_pnl(daily: pd.DataFrame, capital: float) -> pd.Series:
    """P&L per day from the end-of-day equity curve (the first day starts from capital)."""
    eq = daily["equity"].to_numpy(float)
    return pd.Series(np.diff(np.concatenate([[capital], eq])), index=daily["day_end"])


def annualised_return(pnl: pd.Series, base: float) -> float:
    """Mean daily P&L x 365 / base (simple, not compounded: notional is fixed)."""
    return float(pnl.mean() * DAYS_PER_YEAR / base) if len(pnl) and base > 0 else np.nan


def sharpe(pnl: pd.Series) -> float:
    """Annualised Sharpe on daily P&L: mean / std (ddof 1) x sqrt(365); zero risk-free rate."""
    sd = pnl.std(ddof=1)
    return float(pnl.mean() / sd * np.sqrt(DAYS_PER_YEAR)) if len(pnl) > 1 and sd > 0 else np.nan


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    """(largest fall from a running peak in dollars, the same as a fraction of that peak)."""
    eq = pd.Series(equity, dtype=float).reset_index(drop=True)
    peak = eq.cummax()
    dd = eq - peak
    i = int(dd.idxmin()) if len(dd) else 0
    return (float(dd.min()), float(dd.min() / peak.iloc[i])) if len(dd) else (np.nan, np.nan)


def trade_stats(trades: pd.DataFrame) -> dict:
    """Closed trades only: count, hit rate, average holding period, worst trade."""
    closed = trades[trades["exit_hour"].notna()] if len(trades) else trades
    if closed.empty:
        return dict(trades=0, hit_rate=np.nan, avg_hold_days=np.nan, worst_trade=np.nan)
    hold = (closed["exit_hour"] - closed["entry_hour"]) / (24 * HOUR)
    return dict(trades=int(len(closed)), hit_rate=float((closed["total"] > 0).mean()),
                avg_hold_days=float(hold.mean()), worst_trade=float(closed["total"].min()))


def decomposition(trades: pd.DataFrame) -> pd.DataFrame:
    """P&L by component, per coin and in total (row ALL). Columns sum to `total`."""
    cols = COMPONENTS + ["price_unrealised"]
    if trades.empty:
        return pd.DataFrame(0.0, index=["ALL"], columns=cols + ["total"])
    by = trades.groupby("coin", sort=False)[cols].sum()
    by.loc["ALL"] = by.sum()
    by["total"] = by[cols].sum(axis=1)
    return by


def summary(r: Result) -> pd.Series:
    pnl = daily_pnl(r.daily, r.capital)
    total = float(pnl.sum())
    years = len(pnl) / DAYS_PER_YEAR
    deployed = float(r.daily["deployed"].mean()) if len(r.daily) else np.nan
    dd_usd, dd_frac = max_drawdown(r.daily["equity"])
    out = dict(total_pnl=total, capital=r.capital,
               ann_return_total_capital=annualised_return(pnl, r.capital),
               avg_deployed_capital=deployed,
               ann_return_deployed=total / years / deployed if deployed else np.nan,
               sharpe=sharpe(pnl), max_drawdown_usd=dd_usd, max_drawdown_frac=dd_frac,
               **trade_stats(r.trades),
               spread_stops=r.counters["spread_stops"], liquidations=r.counters["liquidations"],
               rebalances=r.counters["rebalances"])
    return pd.Series(out, dtype=float)


def sanity(r: Result, inputs: dict[str, pd.DataFrame]) -> dict:
    """Checks printed by the CLI: components sum to totals and to the equity curve; fills only
    at bar starts; Hyperliquid funding booked for exactly the hours held."""
    t = r.trades
    out = dict(r.counters)
    if t.empty:
        return out
    cols = COMPONENTS + ["price_unrealised"]
    out["max_abs_component_sum_error"] = float((t[cols].sum(axis=1) - t["total"]).abs().max())
    last = r.daily.iloc[-1]
    out["equity_vs_trades_error"] = float(abs(last["equity"] - r.capital - t["total"].sum()))
    starts = {c: set(d["hour"][d["bar_start"]]) for c, d in inputs.items()}
    out["fills_off_bar_start"] = int(sum(
        (row.entry_hour not in starts[row.coin])
        or (pd.notna(row.exit_hour) and row.exit_reason != "liquidation"
            and row.exit_hour not in starts[row.coin])
        for row in t.itertuples()))
    closed = t[t["exit_hour"].notna() & (t["exit_reason"] != "liquidation")]
    held = (closed["exit_hour"] - closed["entry_hour"]) // HOUR
    out["hl_hours_mismatch"] = int((held != closed["hl_hours"]).sum())
    return out
