"""Alignment tests (hand-computed synthetic cases) and Phase 2 item 8 data-quality checks."""

import glob

import numpy as np
import pandas as pd
import pytest
import requests

from src.clean import checks
from src.clean.align import coin_panel
from src.clean.normalise import HOUR
from src.config import ROOT, load_config

T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC
h = lambda x: T0 + int(x * HOUR)
EMPTY = pd.DataFrame()


def _cfg(hl_unit=1, bn_unit=1, missing=None):
    return {"symbols": {"X": {"hyperliquid": "X", "binance": "XUSDT",
                              "hl_unit": hl_unit, "bn_unit": bn_unit}},
            "cleaning": {"off_hour_tolerance_min": 5, "binance_intervals_h": [1, 4, 8],
                         "binance_missing_settlements": missing or [],
                         "hl_funding_cap_per_hour": 0.04, "cap_near_frac": 0.95,
                         "spread_flag_abs": 0.02}}


def _hl_fund(rows):
    return pd.DataFrame([{"coin": "X", "fundingRate": str(r), "premium": "0", "time": t}
                         for t, r in rows])


def _bn_fund(rows):
    return pd.DataFrame([{"symbol": "XUSDT", "fundingTime": t, "fundingRate": str(r),
                          "markPrice": "1", "rateType": "Regular"} for t, r in rows])


def _flags(p, i):
    return set(p["flags"].iloc[i].split(";")) - {""}


# --- Hyperliquid funding: realised, known-at-t, missing hour, rounding ------------------------

def test_hl_realised_known_missing_and_rounding():
    raw = _hl_fund([(h(1), 0.0001), (h(2) + 7, 0.0002),            # (no row stamped 03:00)
                    (h(4) + 14 * 60_000 + 47_645, 0.0004), (h(5), 0.0005)])
    p = coin_panel("X", h(0), h(6), _cfg(), raw, EMPTY, EMPTY, EMPTY, EMPTY)
    # Row stamped T covers [T-1h, T).
    assert p["f_hl_realised"].tolist()[:5] == pytest.approx([0.0001, 0.0002, np.nan, 0.0004, 0.0005],
                                                            nan_ok=True)
    assert "hl_funding_missing" in _flags(p, 2)
    assert "hl_off_hour" in _flags(p, 3)                         # 14m47s late, still hour 3
    assert "after_period_end" in _flags(p, 5)                    # stamped 06:00 = period end
    # Known at H: latest row with T + 1h <= H; the missing hour carries the last value forward.
    assert p["f_hl_known"].tolist() == pytest.approx([np.nan, np.nan, 0.0001, 0.0002, 0.0002, 0.0004],
                                                     nan_ok=True)


# --- Binance funding: interval spreading, settlement hours, override, known-at-t --------------

def test_bn_spreading_override_and_known():
    missing = [{"symbols": ["XUSDT"], "settle": "2025-01-02T04:00", "interval_h": 4}]
    raw = _bn_fund([(h(0), 0.0003), (h(8), 0.0008), (h(16) + 5, 0.0016),   # 8h
                    (h(20), 0.0004),                                        # 4h
                    (h(28), 0.0012)])                 # 24:00 missing; 28:00 listed as 4h
    p = coin_panel("X", h(0), h(32), _cfg(missing=missing), EMPTY, raw, EMPTY, EMPTY, EMPTY)
    f = p["f_bn_realised"].to_numpy()
    assert f[0:8] == pytest.approx([0.0001] * 8)          # 0.0008 / 8 over [00:00, 08:00)
    assert f[8:16] == pytest.approx([0.0002] * 8)         # 0.0016 / 8
    assert f[16:20] == pytest.approx([0.0001] * 4)        # 0.0004 / 4
    assert np.isnan(f[20:24]).all()                       # settlement 24:00 missing
    assert f[24:28] == pytest.approx([0.0003] * 4)        # 0.0012 / 4 (override, not / 8)
    assert np.isnan(f[28:32]).all()
    assert p["bn_interval_h"].iloc[24] == 4
    assert all("bn_funding_missing" in _flags(p, i) for i in range(20, 24))
    assert all("bn_interval_override" in _flags(p, i) for i in range(24, 28))
    assert all("after_period_end" in _flags(p, i) for i in range(28, 32))
    assert np.flatnonzero(p["bn_settle"].to_numpy()).tolist() == [7, 15, 19, 27]
    # Known at H: latest settlement with T + 1h <= H, as rate / interval.
    k = p["f_bn_known"].to_numpy()
    assert np.isnan(k[:9]).all()        # first settlement has no known interval; next usable at 09:00
    assert k[9] == pytest.approx(0.0001) and k[16] == pytest.approx(0.0001)
    assert k[17] == pytest.approx(0.0002) and k[21] == pytest.approx(0.0001)
    assert k[25] == pytest.approx(0.0001)                 # carried forward over the missing one
    assert k[29] == pytest.approx(0.0003)


def test_gap_is_hl_known_minus_bn_known():
    hl = _hl_fund([(h(x), 0.00005) for x in range(1, 13)])
    bn = _bn_fund([(h(0), 0.0), (h(8), 0.0008)])
    p = coin_panel("X", h(0), h(12), _cfg(), hl, bn, EMPTY, EMPTY, EMPTY)
    assert p["gap"].iloc[9] == pytest.approx(0.00005 - 0.0001)


# --- Prices: 4h then 1h, unclosed candles, unit scaling, spread -------------------------------

def _hl_candles(rows, interval_h, fetched_at):
    return pd.DataFrame([{"t": t, "T": t + interval_h * HOUR - 1, "s": "kX", "i": f"{interval_h}h",
                          "o": "0", "c": str(c), "h": "0", "l": "0", "v": "0", "n": 0,
                          "fetched_at": fetched_at} for t, c in rows])


def _bn_klines(rows, fetched_at):
    return pd.DataFrame([{"open_time": t, "close": str(c), "close_time": t + HOUR - 1,
                          "symbol": "XUSDT", "fetched_at": fetched_at} for t, c in rows])


def test_prices_resolution_unclosed_units_and_spread():
    fetched = h(11) + 30 * 60_000                        # the 11:00 candles had not closed yet
    c4 = _hl_candles([(h(0), 4000), (h(4), 4400)], 4, fetched)
    c1 = _hl_candles([(h(8), 4800), (h(9), 4900), (h(10), 5000), (h(11), 5100)], 1, fetched)
    bn = _bn_klines([(h(x), 4.0 + 0.1 * x) for x in range(12)], fetched)
    p = coin_panel("X", h(0), h(12), _cfg(hl_unit=1000, bn_unit=1), EMPTY, EMPTY, c1, c4, bn)
    # 4h regime: prices only on rows whose close lands on a 4h boundary (03:00, 07:00).
    assert p["p_hl"].tolist()[:8] == pytest.approx([np.nan] * 3 + [4.0] + [np.nan] * 3 + [4.4], nan_ok=True)
    assert p["p_bn"].tolist()[:8] == pytest.approx([np.nan] * 3 + [4.3] + [np.nan] * 3 + [4.7], nan_ok=True)
    # 1h regime from the first 1h candle; the unclosed 11:00 candle is dropped.
    assert p["p_hl"].tolist()[8:] == pytest.approx([4.8, 4.9, 5.0, np.nan], nan_ok=True)
    assert np.isnan(p["p_bn"].iloc[11])
    assert {"hl_price_missing", "bn_price_missing"} <= _flags(p, 11)
    assert p["spread"].iloc[3] == pytest.approx(np.log(4.0 / 4.3))
    assert "spread_gt_2pct" in _flags(p, 3)               # |ln(4.0/4.3)| = 7.2%
    assert p["spread"].iloc[8] == pytest.approx(np.log(4.8 / 4.8))
    assert "spread_gt_2pct" not in _flags(p, 8)


def test_near_cap_flagged_not_removed_synthetic():
    hl = _hl_fund([(h(1), 0.039), (h(2), -0.04), (h(3), 0.01)])
    caps = pd.DataFrame({"symbol": ["XUSDT"], "adjustedFundingRateCap": ["0.02"],
                         "adjustedFundingRateFloor": ["-0.02"]})
    bn = _bn_fund([(h(0), 0.0), (h(4), 0.0195)])
    p = coin_panel("X", h(0), h(4), _cfg(), hl, bn, EMPTY, EMPTY, EMPTY, caps)
    assert "hl_near_cap" in _flags(p, 0) and "hl_near_cap" in _flags(p, 1)
    assert "hl_near_cap" not in _flags(p, 2)
    assert p["f_hl_realised"].iloc[0] == pytest.approx(0.039)   # kept, not removed
    assert all("bn_near_cap" in _flags(p, i) for i in range(4))


# --- Phase 2 item 8 checks on the real design-period panel ------------------------------------

PANEL = ROOT / "data" / "clean" / "panel.parquet"
needs_panel = pytest.mark.skipif(not PANEL.exists(), reason="run python -m src.clean.align first")


@pytest.fixture(scope="module")
def panel():
    return pd.read_parquet(PANEL)


@needs_panel
def test_panel_no_duplicate_coin_hours(panel):
    assert checks.duplicate_rows(panel) == 0


@needs_panel
def test_panel_missing_hours_and_longest_gap(panel):
    m = checks.missing_summary(panel)
    assert len(m) == 2 * panel["coin"].nunique()          # every coin and venue reported
    assert (m["funding_longest_gap_h"] <= 8).all(), m      # no funding hole longer than one 8h interval
    assert (m["price_longest_gap_h"] <= 24).all(), m       # no price hole longer than a day


@needs_panel
def test_panel_near_cap_values_flagged_not_removed(panel):
    cfg = load_config()
    near = cfg["cleaning"]["hl_funding_cap_per_hour"] * cfg["cleaning"]["cap_near_frac"]
    at_cap = panel["f_hl_realised"].abs() >= near
    assert checks.has_flag(panel[at_cap], "hl_near_cap").all()
    flagged = panel[checks.has_flag(panel, "hl_near_cap") | checks.has_flag(panel, "bn_near_cap")]
    assert flagged[["f_hl_realised", "f_bn_realised"]].notna().any(axis=1).all()
    caps = pd.read_parquet(sorted(glob.glob(str(ROOT / "data/raw/binance/funding_info/*.parquet")))[-1])
    caps = caps.set_index("symbol")
    for coin, d in panel.groupby("coin"):
        sym = cfg["symbols"][coin]["binance"]
        if sym not in caps.index:
            continue
        rate = d["f_bn_realised"] * d["bn_interval_h"]
        hit = ((rate >= cfg["cleaning"]["cap_near_frac"] * float(caps.loc[sym, "adjustedFundingRateCap"]))
               | (rate <= cfg["cleaning"]["cap_near_frac"] * float(caps.loc[sym, "adjustedFundingRateFloor"])))
        assert checks.has_flag(d[hit], "bn_near_cap").all()


@needs_panel
def test_panel_large_spreads_flagged(panel):
    big = panel["spread"].abs() > load_config()["cleaning"]["spread_flag_abs"]
    assert checks.has_flag(panel[big], "spread_gt_2pct").all()
    # A unit or symbol error would push the typical spread far above a few bps.
    assert (panel.groupby("coin")["spread"].apply(lambda s: s.abs().median()) < 0.005).all()


@needs_panel
def test_panel_btc_factor_of_eight(panel):
    r = checks.factor_of_eight(panel, "BTC")
    assert 1 / 3 < r["median_ratio"] < 3, r
    assert r["share_within_10x"] >= 0.5, r


@needs_panel
@pytest.mark.network
def test_panel_spot_check_five_coin_hours_against_api(panel):
    """The automated half of the spot check; reports/data_quality.md lists the same rows by hand."""
    cfg = load_config()
    for _, r in checks.spot_check_sample(panel).iterrows():
        sym = cfg["symbols"][r.coin]
        t_end = int(r.hour) + HOUR
        hl = requests.post("https://api.hyperliquid.xyz/info", timeout=30, json={
            "type": "fundingHistory", "coin": sym["hyperliquid"],
            "startTime": t_end - 60_000, "endTime": t_end + 15 * 60_000}).json()
        assert float(hl[0]["fundingRate"]) == pytest.approx(r.f_hl_realised)
        settle = int(r.hour) + HOUR * int(
            np.flatnonzero(panel[(panel.coin == r.coin) & (panel.hour >= r.hour)]["bn_settle"].to_numpy())[0] + 1)
        bn = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", timeout=30, params={
            "symbol": sym["binance"], "startTime": settle - 60_000, "endTime": settle + 60_000}).json()
        assert float(bn[0]["fundingRate"]) == pytest.approx(r.f_bn_realised * r.bn_interval_h)
        k = requests.get("https://fapi.binance.com/fapi/v1/klines", timeout=30, params={
            "symbol": sym["binance"], "interval": "1h", "startTime": int(r.hour), "limit": 1}).json()
        assert float(k[0][4]) / sym["bn_unit"] == pytest.approx(r.p_bn)
        # Hyperliquid close at hour + 1h: the 1h candle at `hour`, or the 4h candle ending then.
        for interval, t_open in (("1h", int(r.hour)), ("4h", t_end - 4 * HOUR)):
            c = requests.post("https://api.hyperliquid.xyz/info", timeout=30, json={
                "type": "candleSnapshot", "req": {"coin": sym["hyperliquid"], "interval": interval,
                                                  "startTime": t_open, "endTime": t_open}}).json()
            c = [x for x in c if x["t"] == t_open]
            if c:  # older candles drop out of the API's 5,000-candle window
                assert float(c[0]["c"]) / sym["hl_unit"] == pytest.approx(r.p_hl)
                break
