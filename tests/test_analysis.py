import numpy as np
import pandas as pd
import pytest

from src.analysis.gap_stats import APR, gap_summary, resting_rates, structural_baseline

H = 3_600_000
T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC
REST = 0.0000125


def _panel(coin, f_hl, f_bn):
    n = len(f_hl)
    hl, bn = np.asarray(f_hl, float), np.asarray(f_bn, float)
    return pd.DataFrame({"coin": coin, "hour": T0 + np.arange(n) * H,
                         "f_hl_realised": hl, "f_bn_realised": bn,
                         "f_hl_known": hl, "f_bn_known": bn, "gap": hl - bn})


def test_gap_summary_shares_in_apr():
    # Gaps of 0, 5%, 20%, 30%, -60% APR.
    gaps = np.array([0.0, 0.05, 0.20, 0.30, -0.60]) / APR
    p = _panel("X", gaps, np.zeros(5))
    s = gap_summary(p).loc["X"]
    assert s["hours"] == 5
    assert s["mean"] == pytest.approx(-0.01)
    assert s["median"] == pytest.approx(0.05)
    assert s["share_abs_gt_10%"] == pytest.approx(3 / 5)
    assert s["share_abs_gt_25%"] == pytest.approx(2 / 5)
    assert s["share_abs_gt_50%"] == pytest.approx(1 / 5)


def test_resting_rates_documented_baseline_and_modal_flag():
    # HL sits at the documented 0.01%/8h in 3 of 5 hours; Binance sits at 0 in 3 of 5 (as BNB does).
    p = _panel("BNB", [REST, REST, REST, 0.0001, -0.0001], [0.0, 0.0, 0.0, 0.00002, 0.0001])
    r = resting_rates(p, interest_8h=0.0001, tol=1e-10).loc["BNB"]
    assert r["hl_rest_per_hour"] == pytest.approx(REST)
    assert r["bn_rest_per_hour"] == pytest.approx(REST)               # documented, not inferred
    assert r["hl_share_at_rest"] == pytest.approx(0.6)
    assert r["bn_share_at_rest"] == 0.0
    assert r["mechanical_gap_apr"] == 0.0
    assert r["bn_modal_per_hour"] == 0.0 and bool(r["modal_differs_bn"])   # sanity check flags it
    assert r["hl_modal_per_hour"] == pytest.approx(REST) and not bool(r["modal_differs_hl"])


def test_structural_baseline_split():
    # 31 days at a constant 10% APR gap: the trailing 30-day mean equals the gap, nothing is fast.
    n = 31 * 24
    p = _panel("X", np.full(n, REST + 0.10 / APR), np.full(n, REST))
    s = structural_baseline(p, window_days=30).loc["X"]
    assert s["mean_gap_apr"] == pytest.approx(0.10)
    assert s["std_fast_apr"] == pytest.approx(0.0, abs=1e-9)
    assert s["share_months_positive"] == 1.0
    assert s["share_hours_both_at_rest"] == 0.0
