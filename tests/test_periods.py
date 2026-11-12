"""Period-generic pipeline on synthetic data only: config periods and CLI dates, the held-out opt-in,
the panel's signal warm-up, an engine that starts flat at the period start with a signal built
only from earlier data, benchmarks, and pass/fail raw coverage. No real data is read.

Synthetic world (one coin, BTC), all prices 100 on both venues:
  sample 2030-01-01..2030-01-12, design 2030-01-01..2030-01-08, holdout 2030-01-09..2030-01-12;
  signal window 2 days (48 rows).
  Hyperliquid funding 0.0002 per hour for rows stamped up to the design end (2030-01-09 00:00),
  0.0005 after. Binance settles 0.0008 every 8h (0.0001 per hour). Raw data runs to 2030-01-14,
  past the holdout end, to check the cut.
"""

import copy

import numpy as np
import pandas as pd
import pytest
import yaml

from src.backtest import engine as E
from src.clean import align, checks
from src.config import (check_period_allowed, day_start_ms, load_config, period_bounds,
                        warmup_start_ms)

H = 3_600_000
S0 = day_start_ms("2030-01-01")          # sample and design start
HS = day_start_ms("2030-01-09")          # holdout start = design end (exclusive)
HE = day_start_ms("2030-01-13")          # holdout end (exclusive)
RAW_END = day_start_ms("2030-01-15")
FETCHED = RAW_END + 10 * H
HL_DESIGN, HL_HOLDOUT, BN_8H = 0.0002, 0.0005, 0.0008


def _cfg(tmp_path) -> dict:
    c = copy.deepcopy(load_config())
    c["periods"] = {"sample": {"start": "2030-01-01", "end": "2030-01-12"},
                    "design": {"start": "2030-01-01", "end": "2030-01-08"},
                    "holdout": {"start": "2030-01-09", "end": "2030-01-12"}}
    c["universe"]["coins"] = ["BTC"]
    c["strategy"]["signal_window_days"] = 2
    c["benchmarks"]["random_entry_seeds"] = 2
    c["paths"] = {"raw": str(tmp_path / "raw"), "clean": str(tmp_path / "clean"),
                  "report": str(tmp_path / "report")}
    return c


def _klines(hours, symbol="BTCUSDT"):
    return pd.DataFrame({"open_time": hours, "open": "100", "high": "100", "low": "100",
                         "close": "100", "volume": "1", "close_time": hours + H - 1,
                         "quote_volume": "100", "trades": 1, "taker_buy_volume": "0",
                         "taker_buy_quote_volume": "0", "ignore": "0", "symbol": symbol,
                         "fetched_at": FETCHED})


def _write_raw(root):
    hours = np.arange(S0, RAW_END, H, dtype="int64")
    stamps = hours + H                                   # HL row stamped T covers [T-1h, T)
    files = {
        ("hyperliquid", "funding"): pd.DataFrame({
            "coin": "BTC", "fundingRate": np.where(stamps <= HS, str(HL_DESIGN), str(HL_HOLDOUT)),
            "premium": "0", "time": stamps, "fetched_at": FETCHED}),
        ("hyperliquid", "candles_1h"): pd.DataFrame({
            "t": hours, "T": hours + H - 1, "s": "BTC", "i": "1h", "o": "100", "c": "100",
            "h": "100", "l": "100", "v": "1", "n": 1, "fetched_at": FETCHED}),
        ("binance", "funding"): pd.DataFrame({
            "symbol": "BTCUSDT", "fundingTime": hours[hours % (8 * H) == 0],
            "fundingRate": str(BN_8H), "markPrice": "100", "rateType": "Regular",
            "fetched_at": FETCHED}),
        ("binance", "klines_1h"): _klines(hours),
        ("binance", "mark_price_1h"): _klines(hours),
        ("binance", "index_price_1h"): _klines(hours),
    }
    for (venue, ds), df in files.items():
        path = root / venue / ds / "BTC.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)


@pytest.fixture
def world(tmp_path):
    cfg = _cfg(tmp_path)
    _write_raw(tmp_path / "raw")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg, path


def test_period_bounds_named_and_explicit():
    cfg = _cfg(__import__("pathlib").Path("."))
    assert period_bounds(cfg, "holdout") == ("holdout", HS, HE)
    label, s, e = period_bounds(cfg, None, "2030-01-03", "2030-01-04")
    assert (label, s, e) == ("2030-01-03_to_2030-01-04", day_start_ms("2030-01-03"),
                             day_start_ms("2030-01-05"))
    with pytest.raises(ValueError):
        period_bounds(cfg, "design", start="2030-01-03")          # start without end
    with pytest.raises(ValueError):
        period_bounds(cfg, "nonexistent")


def test_holdout_needs_explicit_opt_in():
    cfg = _cfg(__import__("pathlib").Path("."))
    check_period_allowed(cfg, period_bounds(cfg, "design")[2], False)       # design: fine
    with pytest.raises(ValueError):
        check_period_allowed(cfg, HE, False)
    check_period_allowed(cfg, HE, True)


def test_warmup_start_is_window_before_start_but_not_before_sample():
    cfg = _cfg(__import__("pathlib").Path("."))
    assert warmup_start_ms(cfg, HS, 2) == HS - 48 * H
    assert warmup_start_ms(cfg, S0, 2) == S0                     # design: no data before it


def test_holdout_panel_holds_warmup_from_design_data(world):
    cfg, path = world
    with pytest.raises(ValueError):
        align.main(["--config", str(path), "--period", "holdout"])   # no opt-in
    align.main(["--config", str(path), "--period", "holdout", "--allow-holdout"])
    p = pd.read_parquet(align.panel_path(cfg, "holdout"))
    assert p["hour"].min() == HS - 48 * H and p["hour"].max() == HE - H   # cut at the end
    assert (align.panel_path(cfg, "holdout").parent / "panel.parquet").exists() is False
    assert (align.report_dir(cfg) / "data_quality_holdout.md").exists()
    # Known gap at hour H uses rows stamped T <= H - 1h. At the holdout start and one hour
    # later only design-period HL rows are known: gap 0.0002 - 0.0001. From HS + 2h the HL row
    # stamped HS + 1h (the first holdout hour, 0.0005) is known: gap 0.0004.
    g = p.set_index("hour")["gap"]
    assert g[HS] == pytest.approx(HL_DESIGN - BN_8H / 8)
    assert g[HS + H] == pytest.approx(HL_DESIGN - BN_8H / 8)
    assert g[HS + 2 * H] == pytest.approx(HL_HOLDOUT - BN_8H / 8)


def test_engine_starts_flat_at_holdout_start_with_warmed_up_signal(world):
    cfg, path = world
    align.main(["--config", str(path), "--period", "holdout", "--allow-holdout"])
    with pytest.raises(ValueError):
        E.load_inputs(cfg, period="holdout")                     # no opt-in
    d = E.load_inputs(cfg, period="holdout", allow_holdout=True)["BTC"]
    assert d["hour"].iloc[0] == HS and d["hour"].iloc[-1] == HE - H
    # Signal at HS: mean of the 48 known gaps HS-47h..HS, all 0.0001 -> 0.0001 x 8760 = 0.876.
    # At HS + 2h: 47 x 0.0001 + 1 x 0.0004 over 48 rows -> 0.0051 / 48 x 8760 = 0.93075.
    sig = d.set_index("hour")["signal"]
    assert sig[HS] == pytest.approx(0.876)
    assert sig[HS + H] == pytest.approx(0.876)
    assert sig[HS + 2 * H] == pytest.approx(0.93075)
    assert d["signal"].notna().all()
    # The first decision is at the end of the first bar (HS + 1h); the entry fills at its open.
    r = E.run(E.variant_cfg(cfg, "corrected"), {"BTC": d})
    assert len(r.trades) == 1 and r.trades["entry_hour"].iloc[0] == HS + H
    assert r.daily["day_end"].iloc[0] == HS + 24 * H


def test_design_has_no_warmup_and_signal_waits_for_a_full_window(world):
    cfg, path = world
    align.main(["--config", str(path)])                          # design, the default
    d = E.load_inputs(cfg)["BTC"]
    assert d["hour"].iloc[0] == S0
    assert d["signal"].iloc[:48].isna().all()


def test_load_inputs_refuses_panel_without_enough_warmup(world):
    cfg, path = world
    align.main(["--config", str(path), "--period", "holdout", "--allow-holdout"])
    with pytest.raises(ValueError, match="warm-up"):
        E.load_inputs(cfg, window_days=5, period="holdout", allow_holdout=True)


def test_explicit_dates_inside_design_need_no_opt_in(world):
    cfg, path = world
    align.main(["--config", str(path), "--start", "2030-01-05", "--end", "2030-01-07"])
    d = E.load_inputs(cfg, start="2030-01-05", end="2030-01-07")["BTC"]
    assert d["hour"].iloc[0] == day_start_ms("2030-01-05")
    assert d["hour"].iloc[-1] == day_start_ms("2030-01-08") - H
    assert d["signal"].notna().all()


def test_engine_cli_runs_strategy_and_benchmarks_on_holdout(world, tmp_path):
    cfg, path = world
    align.main(["--config", str(path), "--period", "holdout", "--allow-holdout"])
    out = tmp_path / "tables"
    E.main(["--config", str(path), "--period", "holdout", "--allow-holdout", "--out-dir", str(out)])
    for suffix in ("summary", "trades", "daily", "decomposition", "decomposition_benchmarks",
                   "random_entry"):
        assert (out / f"holdout_backtest_corrected_{suffix}.csv").exists()
    assert not (out / "backtest_corrected_summary.csv").exists()   # design names untouched
    summary = pd.read_csv(out / "holdout_backtest_corrected_summary.csv", index_col=0)
    assert {"strategy", "zero_cost", "always_on", "random_mean"} <= set(summary.columns)
    assert E.out_prefix("design", "corrected") == "backtest_corrected_"
    assert E.out_prefix("holdout", "as_specified") == "holdout_backtest_"


def test_raw_coverage_pass_fail(world):
    cfg, _ = world
    start = warmup_start_ms(cfg, HS, 2)
    res = checks.coverage_summary(checks.coverage_checks(cfg, start, HE))
    assert set(res["result"]) == {"PASS"}
    # Remove one Binance mark kline inside the holdout: only that check fails.
    f = __import__("pathlib").Path(cfg["paths"]["raw"]) / "binance" / "mark_price_1h" / "BTC.parquet"
    k = pd.read_parquet(f)
    k[k["open_time"] != HS + 5 * H].to_parquet(f, index=False)
    res = checks.coverage_summary(checks.coverage_checks(cfg, start, HE))
    assert res.loc["bn_mark_price_1h_complete", "result"] == "FAIL"
    assert res.loc["bn_mark_price_1h_complete", "failing_coins"] == "BTC"
    assert (res.drop("bn_mark_price_1h_complete")["result"] == "PASS").all()
