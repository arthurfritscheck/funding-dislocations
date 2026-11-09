"""Tests for src/analysis/report_checks.py on hand-made inputs (no results files or market data)."""

import numpy as np
import pandas as pd
import pytest

from src.analysis import report_checks as RC
from src.backtest import engine as E
from src.config import day_start_ms

H = E.HOUR


def test_liquidated_collateral_recovers_margin_plus_cash_from_the_liquidation_price():
    # Side +1: short Hyperliquid, long Binance. qty 10.
    # Long Binance leg: entry 100, liquidated at 75, mm 5%:
    #   10 x (100 - 75 x 0.95) = 287.5; check: 287.5 + 10 x (75 - 100) = 37.5 = 0.05 x 10 x 75.
    # Short Hyperliquid leg: entry 100, liquidated at 130, mm 10%:
    #   10 x (-100 + 130 x 1.1) = 430; check: 430 - 10 x 30 = 130 = 0.1 x 10 x 130.
    tr = pd.Series(dict(side=1, qty=10.0, entry_hl=100.0, exit_hl=130.0, entry_bn=100.0,
                        exit_bn=75.0))
    assert RC.leg_sides(tr) == {"hyperliquid": -1, "binance": 1}
    assert RC.liquidated_collateral(tr, "binance", 0.05) == pytest.approx(287.5)
    assert RC.liquidated_collateral(tr, "hyperliquid", 0.10) == pytest.approx(430.0)


def _path():
    """Hourly rows stamped at the end of each hour: a 4h bar closing at T0 (closes 100 on both
    venues) preceded by 24 hours, then the liquidation bar of 4 hours ending at T0 + 4h. Binance
    close 24 hours before the bar start: 80."""
    t0 = pd.Timestamp("2030-01-02 00:00")
    times = pd.date_range(t0 - pd.Timedelta(hours=24), t0 + pd.Timedelta(hours=4), freq="h")
    n = len(times)
    p = pd.DataFrame({"time": times, "hl_close": np.nan, "hl_high": np.nan, "hl_low": np.nan,
                      "bn_close": 90.0, "bn_mark_high": 101.0, "bn_mark_low": 99.0,
                      "bn_index_high": 101.0, "bn_index_low": 99.0})
    p.loc[0, "bn_close"] = 80.0
    for i in range(0, n, 4):                       # 4h bar ends
        p.loc[i, ["hl_close", "hl_high", "hl_low"]] = [95.0, 96.0, 94.0]
    p.loc[n - 5, ["hl_close", "bn_close"]] = [100.0, 100.0]     # bar start (T0)
    bar = slice(n - 4, n - 1)
    p.loc[bar, "bn_mark_low"] = [95.0, 60.0, 90.0, 92.0]
    p.loc[bar, "bn_index_low"] = [96.0, 70.0, 91.0, 93.0]
    p.loc[bar, "bn_mark_high"] = [101.0, 112.0, 103.0, 104.0]
    p.loc[bar, "bn_index_high"] = [101.0, 115.0, 103.0, 104.0]
    p.loc[n - 1, ["hl_close", "hl_high", "hl_low"]] = [91.0, 120.0, 70.0]
    exit_ms = int((t0 + pd.Timedelta(hours=4)).value // 10**6)
    return p, t0, exit_ms


def test_liquidation_bar_moves_for_a_long_and_a_short_leg():
    p, t0, exit_ms = _path()
    long_ = RC.liquidation_bar(p, exit_ms, +1)
    assert long_["bar_start"] == t0 and long_["bar_hours"] == 4
    assert long_["adverse_side"] == "down"
    assert long_["hl_own_move"] == pytest.approx(70 / 100 - 1)       # -0.30
    assert long_["bn_mark_move"] == pytest.approx(60 / 100 - 1)      # -0.40
    assert long_["bn_index_move"] == pytest.approx(70 / 100 - 1)     # -0.30
    assert long_["bn_close_change_24h_before"] == pytest.approx(100 / 80 - 1)   # +0.25
    short = RC.liquidation_bar(p, exit_ms, -1)
    assert short["adverse_side"] == "up"
    assert short["hl_own_move"] == pytest.approx(0.20)
    assert short["bn_mark_move"] == pytest.approx(0.12)
    assert short["bn_index_move"] == pytest.approx(0.15)


def test_window_split_pnl_transfers_positions_and_liquidations():
    # Days 2030-01-01..03 (snapshots at the following midnights), capital 1000.
    # Equity 1010, 1004, 1007 -> daily P&L +10, -6, +3; total +7. Window: 2030-01-02.
    # Cumulative transfer fees -1, -3, -3 at $1 each -> 2 transfers on the window day.
    # One liquidation whose bar ended at 12:00 on the window day.
    ends = [day_start_ms(d) for d in ("2030-01-02", "2030-01-03", "2030-01-04")]
    daily = pd.DataFrame({"day_end": ends, "equity": [1010.0, 1004.0, 1007.0],
                          "transfer_fees": [-1.0, -3.0, -3.0], "open_positions": [1, 3, 2]})
    trades = pd.DataFrame({"exit_reason": ["liquidation", "below_exit"],
                           "exit_hour": [day_start_ms("2030-01-02") + 12 * H,
                                         day_start_ms("2030-01-03") + 5 * H],
                           "total": [-5.0, 12.0]})
    r = E.Result(trades, daily, {}, 1000.0)
    s = RC.window_split(r, ["2030-01-02"], withdrawal_fee=1.0)
    assert s["total_pnl"] == pytest.approx(7.0)
    assert s["pnl_window"] == pytest.approx(-6.0)
    assert s["pnl_excl_window"] == pytest.approx(13.0)
    assert s["transfers_window"] == 2
    assert s["max_open_positions_window"] == 3
    assert s["liquidations_window"] == 1
    assert s["sharpe_excl_window"] == pytest.approx(6.5 / np.std([10.0, 3.0], ddof=1)
                                                   * np.sqrt(365))
