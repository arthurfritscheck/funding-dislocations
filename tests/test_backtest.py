"""Backtest tests: every function in src/backtest/ against hand-computed values."""

import copy
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.backtest import costs, engine as E, metrics as M
from src.config import ROOT, load_config

H = 3_600_000
T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC
FIXTURES = Path(__file__).parent / "fixtures"


def _cfg():
    """Real config with every value these tests depend on pinned explicitly."""
    c = copy.deepcopy(load_config())
    c["fees"] = {"hyperliquid": {"taker_bps": 4.5, "maker_bps": 1.5},
                 "binance": {"taker_bps": 5.0, "maker_bps": 2.0}}
    c["slippage"] = {"majors_bps": 2.0, "others_bps": 8.0, "majors": ["BTC", "ETH"]}
    c["execution"] = {"fill": "next_bar_open", "hyperliquid_leg": "taker", "binance_leg": "taker"}
    c["strategy"].update(theta_in_apr=0.10, theta_out_apr=0.03, exit_on_sign_change=True,
                         spread_stop_frac=0.01, notional_per_position_usd=10000,
                         max_concurrent_positions=15, max_positions_per_coin=1,
                         rearm_after_stop="exit_rule")
    c["margin"].update(leverage_per_leg=3, capital_buffer_frac=0.5, forced_close_penalty_bps=100,
                       rebalance_trigger_frac=0.5, withdrawal_fee_usd=1.0, transfer_delay_hours=2,
                       maintenance_margin_frac={"hyperliquid": {"BTC": 0.0125, "WIF": 0.10},
                                                "binance": {"BTC": 0.004, "WIF": 0.0125}})
    return c


def _frame(n, bar=1, px=100.0, signal=np.nan):
    """Synthetic engine input: n hours, bars of `bar` hours, flat prices, zero funding."""
    i = np.arange(n)
    d = pd.DataFrame({"hour": T0 + i * H, "f_hl": 0.0, "f_bn": 0.0, "bn_interval_h": 8.0,
                      "bn_settle": False, "gap": 0.0, "signal": signal, "ref_px": px,
                      "bar_start": i % bar == 0, "bar_end": i % bar == bar - 1})
    for c in ("o_hl", "o_bn"):
        d[c] = np.where(d["bar_start"], px, np.nan)
    for c in ("h_hl", "l_hl", "c_hl", "h_bn", "l_bn", "c_bn"):
        d[c] = np.where(d["bar_end"], px, np.nan)
    return d[E.INPUT_COLUMNS]


# --- costs ---------------------------------------------------------------------------------

def test_fee_and_slippage_per_fill():
    c = _cfg()
    assert costs.fee_bps(c, "hyperliquid") == 4.5 and costs.fee_bps(c, "binance") == 5.0
    assert costs.slippage_bps(c, "BTC") == 2.0 and costs.slippage_bps(c, "WIF") == 8.0
    fee, slip = costs.fill_costs(c, "BTC", "hyperliquid", 2.0, 50_000.0)   # $100,000 notional
    assert fee == pytest.approx(45.0) and slip == pytest.approx(20.0)
    fee, slip = costs.fill_costs(c, "WIF", "binance", -1000.0, 2.0)        # $2,000, short
    assert fee == pytest.approx(1.0) and slip == pytest.approx(1.6)
    assert costs.round_trip_cost_bps(c, "BTC") == pytest.approx(27.0)
    assert costs.round_trip_cost_bps(c, "WIF") == pytest.approx(51.0)


def test_total_capital_and_zero_cost():
    c = _cfg()
    assert E.total_capital(c) == pytest.approx(15 * 2 * 10000 / 3 * 1.5)     # $150,000
    z = E.zero_cost(c)
    assert costs.round_trip_cost_bps(z, "WIF") == 0.0 and c["fees"]["binance"]["taker_bps"] == 5.0


# --- signal and inputs ---------------------------------------------------------------------

def test_signal_trailing_mean_needs_full_window():
    g = pd.Series(np.r_[np.zeros(168), np.full(5, 1 / 8760)])
    s = E.signal(g, 7)
    assert np.isnan(s.iloc[166]) and s.iloc[167] == 0.0
    assert s.iloc[168] == pytest.approx(1 / 168)          # one hour at 100% APR in 168
    assert s.iloc[172] == pytest.approx(5 / 168)
    d = E.with_signal({"X": _frame(400).assign(gap=0.2 / 8760)}, 14)["X"]
    assert np.isnan(d["signal"].iloc[334]) and d["signal"].iloc[335] == pytest.approx(0.2)


def _candles(ts, length_h, o, h, l, c):
    return pd.DataFrame({"t": ts, "T": [t + length_h * H - 1 for t in ts], "o": o, "h": h,
                         "l": l, "c": c, "fetched_at": T0 + 10**9})


def test_coin_inputs_4h_then_1h_bars():
    # Hyperliquid: 4h candles at 0h, 4h, 8h; hourly from 10h. The 8h candle ends at 12h, after
    # hourly history starts, so it is dropped and 8h-10h has no bar.
    n = 13
    hours = T0 + np.arange(n) * H
    panel = pd.DataFrame({"hour": hours, "f_hl_realised": 0.0, "f_bn_realised": 0.0,
                          "bn_interval_h": 8.0, "bn_settle": False, "gap": 0.0})
    h4 = _candles([T0, T0 + 4 * H, T0 + 8 * H], 4, [10, 20, 30], [11, 21, 31], [9, 19, 29],
                  [10.5, 20.5, 30.5])
    h1 = _candles([T0 + k * H for k in (10, 11, 12)], 1, [40, 41, 42], [40.5, 41.5, 42.5],
                  [39.5, 40.5, 41.5], [40.2, 41.2, 42.2])
    bn = pd.DataFrame({"open_time": hours, "close_time": hours + H - 1,
                       "open": 1000.0 + np.arange(n), "high": 2000.0 + np.arange(n),
                       "low": 500.0 + np.arange(n), "close": 1500.0 + np.arange(n),
                       "fetched_at": T0 + 10**9})
    d = E.coin_inputs(panel, h1, h4, bn, hl_unit=1, bn_unit=1000, window_days=7)
    assert np.flatnonzero(d["bar_start"]).tolist() == [0, 4, 10, 11, 12]
    assert np.flatnonzero(d["bar_end"]).tolist() == [3, 7, 10, 11, 12]
    assert d.loc[4, "o_hl"] == 20 and d.loc[4, "o_bn"] == pytest.approx(1.004)
    assert d.loc[7, "h_hl"] == 21 and d.loc[7, "c_hl"] == 20.5
    assert d.loc[7, "h_bn"] == pytest.approx(2.007)       # max of hours 4-7 highs, /1000
    assert d.loc[7, "l_bn"] == pytest.approx(0.504)       # min of hours 4-7 lows
    assert d.loc[7, "c_bn"] == pytest.approx(1.507)       # close of hour 7
    assert d.loc[10, "o_hl"] == 40 and d.loc[10, "c_hl"] == 40.2
    assert d.loc[9, "ref_px"] == pytest.approx(1.509)     # Binance 1h close, used for funding


# --- the to-the-cent trade -----------------------------------------------------------------

def _one_trade_frame():
    d = _frame(24, bar=4)
    d.loc[4:15, "signal"] = 0.20
    d.loc[16:23, "signal"] = 0.01
    d["f_hl"] = 0.0005                        # outside the holding period: must not be booked
    d.loc[4:15, "f_hl"] = 0.0001
    d["ref_px"] = 999.0
    d.loc[4:9, "ref_px"] = 100.0
    d.loc[10:15, "ref_px"] = 102.0
    d.loc[[7, 15, 23], "bn_settle"] = True
    d.loc[[7, 15, 23], "f_bn"] = [0.00002, 0.00003, 0.0001]
    d.loc[16, ["o_hl", "o_bn"]] = [101.0, 101.5]
    return d


def test_one_trade_matches_hand_computation_to_the_cent():
    exp = pd.read_csv(FIXTURES / "one_trade.csv", index_col="component")["expected_usd"]
    r = E.run(_cfg(), {"BTC": _one_trade_frame()})
    assert len(r.trades) == 1
    t = r.trades.iloc[0]
    assert t["entry_hour"] == T0 + 4 * H and t["exit_hour"] == T0 + 16 * H
    assert t["exit_reason"] == "below_exit" and t["side"] == 1 and t["hl_hours"] == 12
    assert t["qty"] == pytest.approx(exp["qty"])
    for comp in E.COMPONENTS + ["total"]:
        assert round(t[comp], 2) == pytest.approx(round(exp[comp], 2), abs=0.005), comp
    assert r.daily["equity"].iloc[-1] == pytest.approx(r.capital + exp["total"])
    s = M.sanity(r, {"BTC": _one_trade_frame()})
    assert s["max_abs_component_sum_error"] < 1e-9 and s["fills_off_bar_start"] == 0
    assert s["hl_hours_mismatch"] == 0 and s["equity_vs_trades_error"] < 1e-9


# --- funding timing ------------------------------------------------------------------------

def test_binance_funding_only_at_settlements_while_open():
    # 4h settlements at 4h (row 3) and 8h (row 7). Entry fills at 4h, just after the first: not
    # paid. The 8h settlement is paid: -(+1) x 0.0004 x 4 x q(100) x 100 = -16.
    d = _frame(12, bar=4)
    d.loc[4:7, "signal"] = 0.20
    d.loc[8:, "signal"] = 0.0
    d["bn_interval_h"] = 4.0
    d.loc[[3, 7, 11], "bn_settle"] = True
    d.loc[[3, 7, 11], "f_bn"] = 0.0004
    t = E.run(_cfg(), {"BTC": d}).trades.iloc[0]
    assert t["entry_hour"] == T0 + 4 * H and t["exit_hour"] == T0 + 8 * H
    assert t["bn_funding"] == pytest.approx(-16.0)


def test_hyperliquid_funding_for_hours_held_on_reference_price():
    # Short HL (signal > 0) held hours 1-2: +0.001 x 100 x (100 + 110) = +21.
    d = _frame(6)
    d.loc[1:2, "signal"] = 0.2
    d.loc[3:, "signal"] = 0.0
    d["f_hl"] = 0.001
    d.loc[1, "ref_px"], d.loc[2, "ref_px"] = 100.0, 110.0
    t = E.run(_cfg(), {"BTC": d}).trades.iloc[0]
    assert t["hl_hours"] == 2 and t["hl_funding"] == pytest.approx(21.0)
    # Reversed direction (signal < 0): long HL pays.
    t = E.run(_cfg(), {"BTC": d.assign(signal=-d["signal"])}).trades.iloc[0]
    assert t["side"] == -1 and t["hl_funding"] == pytest.approx(-21.0)


# --- state machine -------------------------------------------------------------------------

def test_entry_and_exit_thresholds_are_strict():
    # Hourly bars. Decision at the end of row i uses signal[i+1] and fills at row i+1.
    d = _frame(10)
    d["signal"] = [np.nan, 0.10, 0.11, 0.05, 0.03, 0.029, 0.5, 0.2, -0.2, 0.0]
    r = E.run(_cfg(), {"BTC": d})
    t = r.trades
    # 0.10 is not > 0.10; enter at row 2 (0.11); 0.03 is not < 0.03; exit at row 5 (0.029).
    assert t["entry_hour"].tolist()[:2] == [T0 + 2 * H, T0 + 6 * H]
    assert t["exit_hour"].iloc[0] == T0 + 5 * H and t["exit_reason"].iloc[0] == "below_exit"
    assert t["exit_hour"].iloc[1] == T0 + 8 * H and t["exit_reason"].iloc[1] == "sign_change"


def test_concurrency_cap():
    c = _cfg()
    c["strategy"]["max_concurrent_positions"] = 1
    d = _frame(4, signal=0.2)
    r = E.run(c, {"BTC": d, "WIF": d.copy()})
    assert r.trades["coin"].tolist() == ["BTC"]


def test_spread_stop_at_exactly_100_bps_and_rearm():
    # Short HL from row 1 at spread 0. Row 2 close: HL at 100 x e^0.0099 -> no stop.
    # Row 3 close: 100 x e^0.01 -> stop, exit at row 4. Signal stays 0.2: no re-entry until it
    # meets the exit rule (0.02 at row 7), then re-enters at row 8.
    d = _frame(10, signal=0.2)
    d.loc[2, ["c_hl", "h_hl"]] = 100 * math.exp(0.0099)
    d.loc[3, ["c_hl", "h_hl"]] = 100 * math.exp(0.0100)
    d.loc[7, "signal"] = 0.02
    r = E.run(_cfg(), {"BTC": d})
    t = r.trades
    assert t["exit_reason"].iloc[0] == "spread_stop" and t["exit_hour"].iloc[0] == T0 + 4 * H
    assert t["entry_hour"].tolist() == [T0 + 1 * H, T0 + 8 * H]
    assert r.counters["spread_stops"] == 1


def test_policy_exit_and_entry_rules_directly():
    p = E.SignalPolicy(_cfg())
    pos = E.Position("BTC", 1, 1.0, T0, E.Leg("hyperliquid", -1, 100, 33), E.Leg("binance", 1, 100, 33), 0.0)
    assert p.exit("BTC", pos, 0.2, T0, 100.0, 100.0) is None
    assert p.exit("BTC", pos, -0.05, T0, 100.0, 100.0) == "sign_change"
    assert p.exit("BTC", pos, 0.2, T0, 101.1, 100.0) == "spread_stop"        # ln(1.011) > 0.01
    st = E.SlotState(armed=False, last_side=1)
    assert p.entry("BTC", st, 0.2, T0) == 0 and not st.armed
    assert p.entry("BTC", st, -0.2, T0) == -1 and st.armed                   # sign change re-arms
    assert E.AlwaysOnPolicy(1).entry("BTC", st, np.nan, T0) == 1
    sp = E.SchedulePolicy({"BTC": [(T0 + H, T0 + 3 * H, -1)]})
    assert sp.entry("BTC", st, 0.0, T0) == 0 and sp.entry("BTC", st, 0.0, T0 + H) == -1
    assert sp.exit("BTC", pos, 0, T0 + 2 * H, 1, 1) is None
    assert sp.exit("BTC", pos, 0, T0 + 3 * H, 1, 1) == "scheduled"


# --- leg risk ------------------------------------------------------------------------------

def test_rebalance_amount_fee_and_two_hour_delay():
    # Entry row 1 at 100/100, q = 100, initial margin 3,333.33 per leg. Row 2 closes at 117 on
    # both venues. HL (short) equity = 3,333.33 - 6.50 (fee+slip) - 1,700 = 1,626.83 < 1,666.67.
    # BN equity = 3,333.33 - 7.00 + 1,700 = 5,026.33. Transfer x = (5,026.33 - 1,626.83) / 2 =
    # 1,699.75 out of BN now; 1,698.75 reaches HL at 3h + 2h = 5h (bar start, row 5).
    c = _cfg()
    d = _frame(7, signal=0.2)
    d.loc[2:, ["h_hl", "l_hl", "c_hl", "h_bn", "l_bn", "c_bn", "o_hl", "o_bn", "ref_px"]] = 117.0
    eng = E.Engine(c, {"BTC": d.iloc[:5]}, E.SignalPolicy(c))
    eng.run()
    pos = eng.st["BTC"].pos
    assert pos.bn.cash == pytest.approx(-7.0 - 1699.75)
    assert pos.transfer[1] == pytest.approx(1698.75) and pos.hl.cash == pytest.approx(-6.5)
    eng = E.Engine(c, {"BTC": d.iloc[:6]}, E.SignalPolicy(c))
    eng.run()
    pos = eng.st["BTC"].pos
    assert pos.transfer is None and pos.hl.cash == pytest.approx(-6.5 + 1698.75)
    assert pos.comp["transfer_fees"] == -1.0 and pos.n_rebalances == 1


def test_transfer_in_4h_regime_arrives_at_next_boundary_only():
    # Trigger at the 8h close; funds arrive at 10h, inside the 8h-12h bar, so they are credited
    # at the 12h boundary (not counted in that bar's liquidation check).
    c = _cfg()
    d = _frame(13, bar=4, signal=0.2)
    d.loc[5:, ["h_hl", "l_hl", "c_hl", "h_bn", "l_bn", "c_bn", "o_hl", "o_bn"]] = 117.0
    for n, credited in ((12, False), (13, True)):
        eng = E.Engine(c, {"BTC": d.iloc[:n]}, E.SignalPolicy(c))
        eng.run()
        assert (eng.st["BTC"].pos.transfer is None) == credited


def test_liquidation_price_penalty_and_forced_close():
    # Short HL leg: margin 3,333.33, cash -6.50, q = 100, mm 1.25%. Equity = mm x q x P at
    # P = (100 + 33.268333) / 1.0125 = 131.623045. HL price P&L = -100 x 31.623045; penalty
    # = 1% x 100 x 131.623045. BN leg closes at row 3 open 131: +3,100, fee 6.55, slip 2.62.
    c = _cfg()
    d = _frame(6, signal=0.2)
    d.loc[2, "h_hl"] = 140.0
    d.loc[2, "c_hl"] = d.loc[2, "c_bn"] = 130.0
    d.loc[3, ["o_hl", "o_bn"]] = 131.0
    r = E.run(c, {"BTC": d})
    t = r.trades.iloc[0]
    p_liq = (100 + (10000 / 3 - 6.5) / 100) / 1.0125
    assert p_liq == pytest.approx(131.623045, abs=1e-6)
    assert t["exit_reason"] == "liquidation" and t["exit_hl"] == pytest.approx(p_liq)
    assert t["liquidation"] == pytest.approx(-131.623045, abs=1e-5)
    assert t["price_spread"] == pytest.approx(-3162.3045 + 3100.0, abs=1e-4)
    assert t["fees"] == pytest.approx(-(4.5 + 5.0 + 6.55))
    assert t["slippage"] == pytest.approx(-(2.0 + 2.0 + 2.62))
    assert t["exit_hour"] == T0 + 3 * H and r.counters["liquidations"] == 1
    assert len(r.trades) == 1          # disarmed: signal still 0.2 but no re-entry
    # A high of 131 stays above maintenance: equity 226.83 > 163.75.
    d.loc[2, "h_hl"] = 131.0
    assert E.run(c, {"BTC": d}).counters["liquidations"] == 0


# --- 4h clock ------------------------------------------------------------------------------

def test_decisions_and_fills_only_at_bar_boundaries():
    # Bars as in test_coin_inputs: 4h bars at 0h and 4h, no bar 8h-10h, hourly from 10h.
    d = _frame(14)
    d["bar_start"] = d.index.isin([0, 4, 10, 11, 12, 13])
    d["bar_end"] = d.index.isin([3, 7, 10, 11, 12, 13])
    d.loc[~d["bar_start"], ["o_hl", "o_bn"]] = np.nan
    d.loc[~d["bar_end"], ["h_hl", "l_hl", "c_hl", "h_bn", "l_bn", "c_bn"]] = np.nan
    d["signal"] = 0.2
    d.loc[8:, "signal"] = 0.0
    t = E.run(_cfg(), {"BTC": d}).trades.iloc[0]
    # Signal is 0.2 from the start, but the first decision is at the 4h close -> fill at 4h.
    # Exit decided at the 8h close (signal[8] = 0) can fill only at the next bar start, 10h.
    assert t["entry_hour"] == T0 + 4 * H and t["exit_hour"] == T0 + 10 * H
    assert t["hl_hours"] == 6
    assert E.decision_times(d).tolist() == [T0 + k * H for k in (4, 8, 11, 12, 13)]


# --- benchmarks ----------------------------------------------------------------------------

def test_random_schedule_keeps_count_durations_and_no_overlap():
    d = _frame(400, bar=4, signal=0.0)
    trades = pd.DataFrame({"coin": "BTC", "entry_hour": [T0 + 8 * H, T0 + 100 * H],
                           "exit_hour": [T0 + 48 * H, T0 + 120 * H], "side": [1, -1]})
    s = E.random_schedule({"BTC": d}, trades, np.random.default_rng(0))["BTC"]
    assert len(s) == 2 and sorted(x[2] for x in s) == [-1, 1]
    durations = sorted((b - a) // H for a, b, _ in s)
    assert durations == [20, 40]                    # 4h-aligned, so durations are exact here
    assert s[0][1] < s[1][0]                        # no overlap
    starts = set(E.decision_times(d))
    assert all(a in starts and b in starts for a, b, _ in s)


# --- metrics -------------------------------------------------------------------------------

def test_metrics_hand_computed():
    daily = pd.DataFrame({"day_end": [1, 2, 3], "equity": [101.0, 99.0, 102.0]})
    pnl = M.daily_pnl(daily, 100.0)
    assert pnl.tolist() == [1.0, -2.0, 3.0]
    assert M.annualised_return(pnl, 100.0) == pytest.approx((2 / 3) * 365 / 100)
    assert M.sharpe(pnl) == pytest.approx((2 / 3) / math.sqrt(19 / 3) * math.sqrt(365))
    assert M.max_drawdown(pd.Series([100, 110, 99, 105, 120, 90.0])) == pytest.approx((-30.0, -0.25))
    day = 24 * H
    trades = pd.DataFrame({"coin": ["A", "A", "B", "B"], "entry_hour": [0, 0, 0, 0],
                           "exit_hour": [day, 2 * day, 3 * day, np.nan],
                           "total": [10.0, -5.0, 3.0, 7.0]})
    s = M.trade_stats(trades)
    assert s == pytest.approx(dict(trades=3, hit_rate=2 / 3, avg_hold_days=2.0, worst_trade=-5.0))
    comp = pd.DataFrame({"coin": ["A", "A", "B"], **{k: [1.0, 2.0, 3.0] for k in E.COMPONENTS},
                         "price_unrealised": [0.0, 0.0, 0.5]})
    dec = M.decomposition(comp)
    assert dec.loc["A", "fees"] == 3.0 and dec.loc["ALL", "fees"] == 6.0
    assert dec.loc["ALL", "total"] == pytest.approx(6.0 * len(E.COMPONENTS) + 0.5)


def test_summary_on_one_trade():
    r = E.run(_cfg(), {"BTC": _one_trade_frame()})
    s = M.summary(r)
    assert s["total_pnl"] == pytest.approx(30.902) and s["trades"] == 1
    assert s["ann_return_total_capital"] == pytest.approx(30.902 * 365 / 150000)


# --- real inputs ---------------------------------------------------------------------------

PANEL = ROOT / "data" / "clean" / "panel.parquet"


@pytest.mark.skipif(not PANEL.exists(), reason="run python -m src.clean.align first")
def test_load_inputs_design_period_bars():
    cfg = load_config()
    with pytest.raises(ValueError):
        E.load_inputs(cfg, period="holdout")
    d = E.load_inputs(cfg)["BTC"]
    _, end = E.period_ms(cfg, "design")
    assert d["hour"].max() < end
    # Hourly HL history starts per coin (BTC: 2026-04-09 11:00 UTC), from the raw file.
    first_1h = int(pd.read_parquet(ROOT / "data/raw/hyperliquid/candles_1h/BTC.parquet")["t"].min())
    early = d[d["hour"] < first_1h - 4 * H]
    assert (early.loc[early["bar_start"], "hour"] % (4 * H) == 0).all()
    late = d[d["hour"] >= first_1h]
    assert late["bar_start"].all() and late["bar_end"].all()
    assert d["bar_start"].sum() == d["bar_end"].sum()
