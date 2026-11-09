"""Checks behind statements in report/report.md (Phase 6), saved as results files.

1. Liquidation bars (report/tables/liquidation_bars.csv): for the design period's worst trades
   (from the failure-case results files) and every held-out liquidation, the moves in the bar in
   which a leg was liquidated, on each venue's own prices, and the liquidated leg's collateral at
   that moment. Used to classify the failure cases and to test whether the held-out liquidations
   fit the same types.
2. Held-out stress window (report/tables/holdout_stress_window.csv): the held-out P&L of the
   strategy, zero-cost and always-on runs on the Binance-hack days
   (`robustness.holdout_stress_days`), with and without those days. Splitting the daily P&L is
   not a rerun without the days.

The held-out part reruns the frozen held-out strategy, zero-cost and always-on runs exactly as
`python -m src.backtest.engine --period holdout --allow-holdout` does, and stops unless their
totals match the saved holdout_backtest_corrected_summary.csv to the cent. It reads held-out data,
so it needs the explicit opt-in (CLAUDE.md rule 3):

    python -m src.analysis.report_checks --allow-holdout
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from src.analysis import failure_cases as F
from src.analysis.robustness import day_index, tail_event_split
from src.backtest import engine as E
from src.config import ROOT, check_period_allowed, load_config, period_ms

TABLES = ROOT / "report" / "tables"
HOLDOUT_PREFIX = "holdout_backtest_corrected_"


# --- Liquidation bars ----------------------------------------------------------------------

def leg_sides(trade) -> dict[str, int]:
    """+1 long, -1 short per venue: side +1 is short Hyperliquid / long Binance."""
    s = int(trade["side"])
    return {"hyperliquid": -s, "binance": s}


def liquidated_collateral(trade, leg: str, mm: float) -> float:
    """The liquidated leg's collateral when it was liquidated: initial margin plus everything
    booked to its cash (funding, fees, slippage, arrived transfers). The engine closes the leg
    exactly at its liquidation price p, where margin0 + cash + side x qty x (p - entry) =
    mm x qty x p, so margin0 + cash = qty x (side x entry - p x (side - mm))."""
    side = leg_sides(trade)[leg]
    key = "hl" if leg == "hyperliquid" else "bn"
    entry, p = float(trade[f"entry_{key}"]), float(trade[f"exit_{key}"])
    return float(trade["qty"]) * (side * entry - p * (side - mm))


def liquidation_bar(path: pd.DataFrame, exit_ms: int, leg_side: int) -> dict:
    """Moves in the bar in which a leg was liquidated, from a trade path (the columns of
    failure_cases.trade_path, rows stamped at the end of each hour).

    The bar runs from the last Hyperliquid bar close before the exit hour to the exit hour (4
    hours in most of the design period, 1 hour in the held-out period). Each move is the bar's
    extreme on the liquidated leg's adverse side (the low for a long leg, the high for a short)
    against that venue's close at the bar start: Hyperliquid's own trade candle, Binance's mark
    price and Binance's index price (the latter two against the Binance close). Also the change
    in the Binance close over the 24 hours before the bar."""
    t = pd.to_datetime(path["time"])
    exit_t = pd.Timestamp(exit_ms, unit="ms")
    start = t[path["hl_close"].notna() & (t < exit_t)].max()
    bar = path[(t > start) & (t <= exit_t)]
    prior = path[t == start].iloc[0]
    low = leg_side > 0
    pick = (lambda c: float(bar[c].min())) if low else (lambda c: float(bar[c].max()))
    suffix = "low" if low else "high"
    day_before = path[t == start - pd.Timedelta(hours=24)]
    run_up = (float(prior["bn_close"]) / float(day_before["bn_close"].iloc[0]) - 1
              if len(day_before) else np.nan)
    return dict(bar_start=start, bar_hours=int(len(bar)), adverse_side="down" if low else "up",
                hl_own_move=pick(f"hl_{suffix}") / float(prior["hl_close"]) - 1,
                bn_mark_move=pick(f"bn_mark_{suffix}") / float(prior["bn_close"]) - 1,
                bn_index_move=pick(f"bn_index_{suffix}") / float(prior["bn_close"]) - 1,
                bn_close_change_24h_before=run_up)


def liquidation_row(period: str, trade, path: pd.DataFrame, leg: str, mm: float,
                    leverage: float) -> dict:
    side = leg_sides(trade)[leg]
    entry = float(trade["entry_hl" if leg == "hyperliquid" else "entry_bn"])
    return dict(period=period, coin=trade["coin"], position=F.side_text(int(trade["side"])),
                entry=pd.to_datetime(trade["entry_hour"], unit="ms"),
                exit=pd.to_datetime(trade["exit_hour"], unit="ms"),
                liquidated_leg=leg, liquidated_leg_side="long" if side > 0 else "short",
                **liquidation_bar(path, int(trade["exit_hour"]), side),
                rebalances=int(trade["n_rebalances"]),
                liquidated_leg_collateral_usd=liquidated_collateral(trade, leg, mm),
                leg_initial_margin_usd=float(trade["qty"]) * entry / leverage,
                price_spread=float(trade["price_spread"]), total=float(trade["total"]))


def design_liquidations(cfg: dict) -> list[dict]:
    """The design period's worst trades, from the failure-case results files (no market data)."""
    worst = pd.read_csv(TABLES / "failure_cases_worst_trades.csv")
    paths = pd.read_csv(TABLES / "failure_cases_paths.csv")
    trades = pd.read_csv(TABLES / "backtest_corrected_trades.csv")
    rows = []
    for w in worst.itertuples(index=False):
        if w.exit_reason != "liquidation":
            continue
        entry_ms = int(pd.Timestamp(w.entry, tz="UTC").value // 10**6)
        tr = trades[(trades["coin"] == w.coin) & (trades["entry_hour"] == entry_ms)].iloc[0]
        p = paths[(paths["rank"] == w.rank) & (paths["coin"] == w.coin)]
        venue = "hyperliquid" if w.liquidated_leg == "hyperliquid" else "binance"
        mm = cfg["margin"]["maintenance_margin_frac"][venue][w.coin]
        rows.append(liquidation_row("design", tr, p, venue, mm,
                                    cfg["margin"]["leverage_per_leg"]))
    return rows


def holdout_liquidations(cfg: dict, inputs: dict, trades: pd.DataFrame) -> list[dict]:
    """Every liquidated held-out trade, with its path rebuilt as for the failure cases."""
    pad = int(cfg["robustness"]["failure_cases"]["pad_days"] * 24)
    rows = []
    for tr in trades[trades["exit_reason"] == "liquidation"].itertuples(index=False):
        tr = pd.Series(tr._asdict())
        d = inputs[tr["coin"]]
        path = F.trade_path(d, tr, F.load_marks(cfg, tr["coin"], "holdout", allow_holdout=True),
                            pad)
        leg = F.liquidated_leg(tr, d)
        mm = cfg["margin"]["maintenance_margin_frac"][leg][tr["coin"]]
        rows.append(liquidation_row("holdout", tr, path, leg, mm,
                                    cfg["margin"]["leverage_per_leg"]))
    return rows


# --- Stress window -------------------------------------------------------------------------

def window_split(r: E.Result, days: list[str], withdrawal_fee: float) -> dict:
    """P&L with and without the given UTC days (robustness.tail_event_split), plus the
    collateral transfers made and the most positions open on those days."""
    s = tail_event_split(r, days)
    on = day_index(r.daily).isin(pd.DatetimeIndex(days))
    fees = np.diff(np.r_[0.0, r.daily["transfer_fees"].to_numpy(float)])
    return dict(total_pnl=s["total_pnl"], pnl_window=s["pnl_tail_days"],
                pnl_excl_window=s["pnl_excl_tail_days"],
                liquidations_window=s["liquidations_tail_days"],
                transfers_window=int(round(-fees[on].sum() / withdrawal_fee)),
                max_open_positions_window=int(r.daily.loc[on, "open_positions"].max()),
                sharpe=s["sharpe"], sharpe_excl_window=s["sharpe_excl_tail_days"])


def holdout_runs(cfg: dict, inputs: dict) -> dict[str, E.Result]:
    """The held-out strategy, zero-cost and always-on runs, checked against the saved totals."""
    side = 1 if cfg["benchmarks"]["always_on_direction"] == "short_hl" else -1
    runs = {"strategy": E.run(cfg, inputs), "zero_cost": E.run(E.zero_cost(cfg), inputs),
            "always_on": E.run(cfg, inputs, E.AlwaysOnPolicy(side))}
    saved = pd.read_csv(TABLES / f"{HOLDOUT_PREFIX}summary.csv", index_col=0).loc["total_pnl"]
    for name, r in runs.items():
        total = float(r.trades["total"].sum()) if len(r.trades) else 0.0
        if abs(total - float(saved[name])) > 0.01:
            raise SystemExit(f"{name}: rerun total {total:.2f} does not match the saved held-out "
                             f"total {float(saved[name]):.2f}; nothing written")
    return runs


def stress_window(cfg: dict, runs: dict[str, E.Result]) -> pd.DataFrame:
    days = cfg["robustness"]["holdout_stress_days"]
    fee = cfg["margin"]["withdrawal_fee_usd"]
    out = pd.DataFrame({name: window_split(r, days, fee) for name, r in runs.items()}).T
    out.insert(0, "days", ", ".join(days))
    return out.rename_axis("run")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--allow-holdout", action="store_true",
                   help="required: reads held-out data (CLAUDE.md rule 3)")
    args = p.parse_args(argv)
    cfg = load_config()
    check_period_allowed(cfg, period_ms(cfg, "holdout")[1], args.allow_holdout)
    inputs = E.load_inputs(cfg, period="holdout", allow_holdout=True)
    runs = holdout_runs(cfg, inputs)
    liq = pd.DataFrame(design_liquidations(cfg)
                       + holdout_liquidations(cfg, inputs, runs["strategy"].trades))
    liq.to_csv(TABLES / "liquidation_bars.csv", index=False)
    win = stress_window(cfg, runs)
    win.to_csv(TABLES / "holdout_stress_window.csv")
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(liq.to_string(float_format=lambda v: f"{v:.4f}"))
        print(win.to_string())


if __name__ == "__main__":
    main()
