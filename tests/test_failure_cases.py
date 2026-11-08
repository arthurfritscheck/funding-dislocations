"""Tests for src/analysis/failure_cases.py and robustness.tail_event_split (hand-computed)."""

import math

import numpy as np
import pandas as pd
import pytest

from src.analysis import failure_cases as F, robustness as R
from src.backtest import engine as E

H = E.HOUR
T0 = 1_750_000_000_000 // H * H


def _ms(ts: str) -> int:
    return int(pd.Timestamp(ts, tz="UTC").value // 10**6)


def test_worst_trades_ranks_closed_trades_only():
    t = pd.DataFrame({"total": [5.0, -3.0, -10.0, -99.0], "exit_hour": [1, 2, 3, np.nan]})
    w = F.worst_trades(t, 2)
    assert list(w["total"]) == [-10.0, -3.0] and list(w["rank"]) == [1, 2]


def test_liquidated_leg():
    d = pd.DataFrame({"hour": [T0], "o_hl": [100.0], "o_bn": [50.0]})
    tr = lambda reason, hl, bn: pd.Series(dict(exit_reason=reason, exit_hour=T0, exit_hl=hl,
                                               exit_bn=bn))
    assert F.liquidated_leg(tr("liquidation", 100.0, 45.0), d) == "binance"
    assert F.liquidated_leg(tr("liquidation", 120.0, 50.0), d) == "hyperliquid"
    assert F.liquidated_leg(tr("liquidation", 120.0, 45.0), d) == "both"
    assert F.liquidated_leg(tr("below_exit", 100.0, 50.0), d) == ""


def test_trade_path_reconstructs_funding_and_price():
    # Short HL / long BN (side +1), qty 10, entry at row 1, exit at row 4 (held rows 1-3).
    # HL funding 0.0001 per hour on ref 100: +0.0001 x 10 x 100 = +0.1 per held hour -> 0.3.
    # BN settles at row 2, rate 0.00005 over 8h: -0.00005 x 8 x 10 x 100 = -0.4.
    # Row 3 ends a bar at closes 101 (HL) and 102 (BN): -10 x (101 - 100) + 10 x (102 - 100) = 10.
    n = 6
    d = pd.DataFrame({"hour": T0 + np.arange(n) * H, "f_hl": 0.0001, "f_bn": 0.00005,
                      "bn_interval_h": 8.0, "bn_settle": [False, False, True, False, False, True],
                      "signal": 0.12, "ref_px": 100.0, "bar_end": [False, False, False, True, False, False],
                      "c_hl": [np.nan, np.nan, np.nan, 101.0, np.nan, np.nan],
                      "c_bn": [np.nan, np.nan, np.nan, 102.0, np.nan, np.nan]})
    marks = pd.DataFrame({"hour": d["hour"], "bn_mark_high": 1.0, "bn_mark_low": 1.0,
                          "bn_mark_close": 1.0, "bn_index_high": 1.0, "bn_index_low": 1.0,
                          "bn_index_close": 1.0})
    trade = pd.Series(dict(side=1, qty=10.0, entry_hour=T0 + H, exit_hour=T0 + 4 * H,
                           entry_hl=100.0, entry_bn=100.0, hl_funding=0.3, bn_funding=-0.4))
    p = F.trade_path(d, trade, marks, pad_hours=1)
    assert list(p["hour"]) == list(T0 + np.arange(5) * H)        # 1h before entry to 1h after exit
    assert list(p["held"]) == [False, True, True, True, False]
    assert p["hl_funding_cum"].iloc[-1] == pytest.approx(0.3)
    assert p["bn_funding_cum"].iloc[-1] == pytest.approx(-0.4)   # row 5 settlement is after exit
    assert p.loc[3, "price_mtm"] == pytest.approx(10.0)
    assert p.loc[3, "spread_bps"] == pytest.approx(math.log(101 / 102) * 1e4)
    assert p.loc[0, "f_hl_apr"] == pytest.approx(0.876)          # 0.0001 x 8760
    assert F.funding_recon_error(trade, p) == pytest.approx((0.0, 0.0))


def test_tail_event_split():
    # Equity 1010, 960, 950, 980 on capital 1000 -> daily P&L 10, -50, -10, +30 for
    # 2025-10-09 .. 2025-10-12. Tail days 10-10 and 10-11: -60; total -20; excluded +40;
    # share -60 / -20 = 3.0. Remaining days [10, 30]: mean 20, sd 14.1421 -> Sharpe
    # 1.414214 x sqrt(365); annualised 20 x 365 / 1000 = 7.3.
    daily = pd.DataFrame({"day_end": [_ms(f"2025-10-{d}") for d in (10, 11, 12, 13)],
                          "equity": [1010.0, 960.0, 950.0, 980.0]})
    # Liquidations at bar ends 2025-10-11 00:00 (day 10-10, counted) and 2025-10-13 00:00.
    trades = pd.DataFrame({"exit_reason": ["liquidation", "liquidation", "below_exit"],
                           "exit_hour": [_ms("2025-10-11"), _ms("2025-10-13"), _ms("2025-10-12")]})
    s = R.tail_event_split(E.Result(trades, daily, {}, 1000.0), ["2025-10-10", "2025-10-11"])
    assert s["total_pnl"] == pytest.approx(-20.0)
    assert s["pnl_tail_days"] == pytest.approx(-60.0)
    assert s["pnl_excl_tail_days"] == pytest.approx(40.0)
    assert s["tail_share_of_total"] == pytest.approx(3.0)
    assert s["liquidations"] == 2 and s["liquidations_tail_days"] == 1
    assert s["ann_return_excl_tail_days"] == pytest.approx(7.3)
    assert s["sharpe_excl_tail_days"] == pytest.approx(math.sqrt(2) * math.sqrt(365))
