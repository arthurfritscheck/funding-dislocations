"""Tests for src/analysis/robustness.py, with hand-computed expected values."""

import math

import numpy as np
import pandas as pd
import pytest

from src.analysis import robustness as R
from src.backtest import costs, engine as E
from src.config import load_config


def _ms(ts: str) -> int:
    return int(pd.Timestamp(ts, tz="UTC").value // 10**6)


def test_scale_costs_scales_fees_and_slippage_separately():
    c = R.scale_costs(load_config(), 2.0, 0.5)
    assert c["fees"]["hyperliquid"] == {"taker_bps": 9.0, "maker_bps": 3.0}
    assert c["fees"]["binance"] == {"taker_bps": 10.0, "maker_bps": 4.0}
    assert c["slippage"]["majors_bps"] == 1.0 and c["slippage"]["others_bps"] == 4.0
    # Round trip, taker: 2 x (9 + 4) + 2 x (10 + 4) = 54 bps for a non-major.
    assert costs.round_trip_cost_bps(c, "SUI") == pytest.approx(54.0)


def test_maker_cfg_round_trip():
    # Maker 1.5 (HL) and 2.0 (BN) bps, half slippage (1 bp majors, 4 bps others):
    # majors 2 x (1.5 + 1) + 2 x (2 + 1) = 11 bps; others 2 x (1.5 + 4) + 2 x (2 + 4) = 23 bps.
    c = R.maker_cfg(load_config())
    assert costs.round_trip_cost_bps(c, "BTC") == pytest.approx(11.0)
    assert costs.round_trip_cost_bps(c, "SUI") == pytest.approx(23.0)


def test_subset_and_leverage_buffer_capital():
    cfg = load_config()
    # 3 coins x 2 legs x $10,000 / 3 x 1.5 = $30,000.
    assert E.total_capital(R.subset_cfg(cfg, ["BTC", "ETH", "SOL"])) == pytest.approx(30_000)
    # 15 x 2 x $10,000 / 1.5 x 1.25 = $250,000; buffer 0 at 3x: $100,000.
    assert E.total_capital(R.leverage_buffer_cfg(cfg, 1.5, 0.25)) == pytest.approx(250_000)
    assert E.total_capital(R.leverage_buffer_cfg(cfg, 3, 0.0)) == pytest.approx(100_000)


def test_breakeven_multiple_bisection():
    # P&L = 100 - 40 m crosses zero at m = 2.5; known points bracket it in [2, 3].
    calls = []

    def pnl(m):
        calls.append(m)
        return 100 - 40 * m
    known = {m: pnl(m) for m in (0.0, 0.5, 1.0, 2.0, 3.0)}
    calls.clear()
    assert R.breakeven_multiple(pnl, known, 0.001) == pytest.approx(2.5, abs=0.001)
    assert all(2.0 <= m <= 3.0 for m in calls)
    assert math.isnan(R.breakeven_multiple(pnl, {0.0: -1.0, 3.0: -5.0}, 0.001))


def test_daily_increments_and_quarterly():
    # Three daily snapshots; P&L belongs to the day before each 00:00 stamp:
    # 2024-09-29 and 2024-09-30 (Q3), 2024-10-01 (Q4).
    # Cumulative hl_funding 10, 15, 25; fees -2, -2, -5; unrealised 1, -1, 0.
    # Daily P&L: 10 - 2 + 1 = 9; 5 + 0 - 2 = 3; 10 - 3 + 1 = 8. Q3 = 12, Q4 = 8.
    daily = pd.DataFrame({"day_end": [_ms("2024-09-30"), _ms("2024-10-01"), _ms("2024-10-02")]})
    for c in E.COMPONENTS:
        daily[c] = 0.0
    daily["hl_funding"] = [10.0, 15.0, 25.0]
    daily["fees"] = [-2.0, -2.0, -5.0]
    daily["price_unrealised"] = [1.0, -1.0, 0.0]
    daily["equity"] = 1000 + daily[E.COMPONENTS].sum(axis=1) + daily["price_unrealised"]
    inc = R.daily_increments(daily)
    assert list(inc["total"]) == [9.0, 3.0, 8.0]
    assert str(inc.index[0].date()) == "2024-09-29"
    trades = pd.DataFrame({"entry_hour": [_ms("2024-09-29 04:00"), _ms("2024-10-01 08:00")],
                           "exit_hour": [_ms("2024-10-01 04:00"), np.nan],
                           "exit_reason": ["liquidation", "open_at_end"]})
    q = R.quarterly(E.Result(trades, daily, {}, 1000.0))
    assert list(q.index) == ["2024Q3", "2024Q4"]
    assert list(q["total"]) == [12.0, 8.0]
    assert list(q["hl_funding"]) == [15.0, 10.0] and list(q["price_unrealised"]) == [-1.0, 1.0]
    assert list(q["days"]) == [2, 1]
    # Q3: 12 / 2 days x 365 / 1000 = 2.19.
    assert q.loc["2024Q3", "ann_return_total_capital"] == pytest.approx(2.19)
    assert list(q["trades_entered"]) == [1, 1] and list(q["liquidations"]) == [0, 1]


def test_rolling_sharpe():
    # Window 3 on [1, 2, 3, 5]: [1, 2, 3] mean 2, sd 1 -> 2 x sqrt(365);
    # [2, 3, 5] mean 10/3, sd sqrt(7/3) -> (10/3) / 1.527525 x sqrt(365).
    rs = R.rolling_sharpe(pd.Series([1.0, 2.0, 3.0, 5.0]), 3)
    assert rs.iloc[:2].isna().all()
    assert rs.iloc[2] == pytest.approx(2 * math.sqrt(365))
    assert rs.iloc[3] == pytest.approx((10 / 3) / math.sqrt(7 / 3) * math.sqrt(365))


def test_block_bootstrap_indices_are_contiguous_blocks():
    idx = R.block_bootstrap_indices(10, 3, 5, np.random.default_rng(1))
    assert idx.shape == (5, 10)                      # 4 blocks of 3, cut to 10
    assert idx.min() >= 0 and idx.max() <= 9
    for r in idx:
        for b in range(0, 10, 3):
            blk = r[b:b + 3]
            assert np.all(np.diff(blk) == 1)


def test_block_bootstrap_with_one_full_block_returns_the_sample_sharpe():
    # Block = n: the only start is 0, so every resample is the sample itself.
    # [1, 2, 3, 4]: mean 2.5, sd 1.290994 -> 1.936492 x sqrt(365) = 36.9966.
    s = R.block_bootstrap_sharpe(pd.Series([1.0, 2.0, 3.0, 4.0]), 4, 7, seed=0, chunk=3)
    assert len(s) == 7
    assert np.allclose(s, 2.5 / math.sqrt(5 / 3) * math.sqrt(365))
    assert s[0] == pytest.approx(36.9966, abs=1e-4)
