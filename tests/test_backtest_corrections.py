"""Tests for the 2026-11-08 model-fidelity corrections (hand-computed values):
liquidation on mark prices and the rebalance trigger on the current margin requirement."""

import numpy as np
import pandas as pd
import pytest

from src.backtest import engine as E
from tests.test_backtest import H, T0, _candles, _cfg, _frame


def test_variant_cfg_sets_only_the_two_model_switches():
    c = _cfg()
    a, b = E.variant_cfg(c, "as_specified"), E.variant_cfg(c, "corrected")
    assert a["margin"]["liquidation_price_source"] == "last_trade"
    assert a["margin"]["rebalance_trigger_base"] == "initial_margin"
    assert b["margin"]["liquidation_price_source"] == "mark"
    assert b["margin"]["rebalance_trigger_base"] == "current_requirement"
    for k in ("strategy", "fees", "slippage", "execution"):
        assert a[k] == b[k] == c[k]
    assert {k: v for k, v in a["margin"].items() if k not in E.VARIANTS["corrected"]} == \
           {k: v for k, v in c["margin"].items() if k not in E.VARIANTS["corrected"]}


def test_coin_inputs_aggregates_mark_and_index_over_each_bar():
    # One 4h Hyperliquid bar (0h-4h) and hourly from 4h. Mark and index highs/lows are taken
    # over the bar's hours and divided by the contract unit (1000).
    n = 6
    hours = T0 + np.arange(n) * H
    panel = pd.DataFrame({"hour": hours, "f_hl_realised": 0.0, "f_bn_realised": 0.0,
                          "bn_interval_h": 8.0, "bn_settle": False, "gap": 0.0})
    h4 = _candles([T0], 4, [10], [11], [9], [10])
    h1 = _candles([T0 + 4 * H, T0 + 5 * H], 1, [10, 10], [11, 11], [9, 9], [10, 10])

    def kl(high, low):
        return pd.DataFrame({"open_time": hours, "close_time": hours + H - 1, "open": 1000.0,
                             "high": high, "low": low, "close": 1000.0, "fetched_at": T0 + 10**9})
    bn = kl(1000.0, 1000.0)
    mark = kl([1010.0, 1050.0, 1020.0, 1001.0, 1003.0, 1004.0], [990.0, 980.0, 950.0, 999.0, 997.0, 996.0])
    index = kl([1005.0, 1006.0, 1060.0, 1002.0, 1001.0, 1002.0], [995.0, 990.0, 994.0, 970.0, 999.0, 998.0])
    d = E.coin_inputs(panel, h1, h4, bn, 1, 1000, 7, bn_mark=mark, bn_index=index)
    assert d.loc[3, "mh_bn"] == pytest.approx(1.050) and d.loc[3, "ml_bn"] == pytest.approx(0.950)
    assert d.loc[3, "mh_hl"] == pytest.approx(1.060) and d.loc[3, "ml_hl"] == pytest.approx(0.970)
    assert d.loc[4, "mh_bn"] == pytest.approx(1.003) and d.loc[5, "ml_hl"] == pytest.approx(0.998)
    assert np.isnan(d.loc[2, "mh_bn"])                    # not a bar end


def _liq_frame(trade_high, mark_high):
    # Short HL from row 1 at 100 (q = 100). Liquidation price 131.623045 (see test_backtest).
    d = _frame(6, signal=0.2)
    for c in E.MARK_COLUMNS:
        d[c] = np.where(d["bar_end"], 100.0, np.nan)
    d.loc[2, "h_hl"] = trade_high
    d.loc[2, "mh_hl"] = mark_high
    return d


def test_liquidation_checked_on_mark_not_trade_wick():
    c = _cfg()
    corrected, as_spec = E.variant_cfg(c, "corrected"), E.variant_cfg(c, "as_specified")
    wick = _liq_frame(trade_high=140.0, mark_high=120.0)       # trade wick only
    assert E.run(as_spec, {"BTC": wick}).counters["liquidations"] == 1
    r = E.run(corrected, {"BTC": wick})
    assert r.counters["liquidations"] == 0 and r.counters["mark_fallbacks"] == 0
    move = _liq_frame(trade_high=120.0, mark_high=140.0)       # mark moves through 131.62
    t = E.run(corrected, {"BTC": move}).trades.iloc[0]
    assert t["exit_reason"] == "liquidation"
    assert t["exit_hl"] == pytest.approx((100 + (10000 / 3 - 6.5) / 100) / 1.0125)
    assert t["liquidation"] == pytest.approx(-131.623045, abs=1e-5)


def test_missing_mark_falls_back_to_trade_extreme_and_is_counted():
    d = _liq_frame(trade_high=140.0, mark_high=np.nan)
    r = E.run(E.variant_cfg(_cfg(), "corrected"), {"BTC": d})
    assert r.counters["liquidations"] == 1 and r.counters["mark_fallbacks"] >= 1


def test_rebalance_trigger_on_current_requirement():
    # Short HL from row 1 at 100, q = 100. Row 2 closes at 115 on both venues.
    # HL equity = 3,333.33 - 6.50 - 1,500 = 1,826.83.
    # As specified: trigger 50% x 3,333.33 = 1,666.67 -> no rebalance.
    # Corrected: requirement 100 x 115 / 3 = 3,833.33, trigger 1,916.67 -> rebalance.
    # BN equity = 3,333.33 - 7.00 + 1,500 = 4,826.33; x = (4,826.33 - 1,826.83) / 2 = 1,499.75.
    c = _cfg()
    d = _frame(4, signal=0.2)
    d.loc[2:, ["h_hl", "l_hl", "c_hl", "h_bn", "l_bn", "c_bn", "o_hl", "o_bn", "ref_px"]] = 115.0
    eng = E.Engine(E.variant_cfg(c, "as_specified"), {"BTC": d}, E.SignalPolicy(c))
    eng.run()
    assert eng.n["rebalances"] == 0
    eng = E.Engine(E.variant_cfg(c, "corrected"), {"BTC": d.iloc[:3]}, E.SignalPolicy(c))
    eng.run()
    pos = eng.st["BTC"].pos
    assert eng.n["rebalances"] == 1
    assert pos.bn.cash == pytest.approx(-7.0 - 1499.75)
    assert pos.transfer[1] == pytest.approx(1498.75)
