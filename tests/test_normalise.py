import pandas as pd

from src.clean.normalise import HOUR, binance_intervals, interval_history, round_to_hour

T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC
CFG = {"cleaning": {"off_hour_tolerance_min": 5, "binance_intervals_h": [1, 4, 8]}}


def _raw(rows):
    return pd.DataFrame([{"symbol": "XUSDT", "fundingTime": t, "fundingRate": "0.0001",
                          "markPrice": "1.0", "rateType": k} for t, k in rows])


def test_round_to_hour():
    assert round_to_hour(T0 + 7) == T0
    assert round_to_hour(T0 + 14 * 60_000 + 47_645) == T0       # 14m47s late -> same hour
    assert round_to_hour(T0 + 30 * 60_000 - 1) == T0
    assert round_to_hour(T0 + 30 * 60_000) == T0 + HOUR
    assert round_to_hour(T0 - 3) == T0                            # slightly early


def test_intervals_flags_and_special_rows():
    h = lambda x: T0 + int(x * HOUR)
    raw = _raw([
        (h(0), "Regular"), (h(8), "Regular"), (h(16), "Regular"),     # 8h
        (h(20), "Regular"), (h(24), "Regular"),                       # switch to 4h
        (h(25), "Regular"), (h(26), "Regular"),                       # switch to 1h
        (h(26.5), "Special"),                                         # dropped
        (h(27) + 7, "Regular"),                                       # ms jitter is fine
        (h(29), "Regular"),                                           # 2h gap -> irregular
        (h(30) + 6 * 60_000, "Regular"),                              # 6 min late -> off_hour
    ])
    out = binance_intervals(raw, CFG)
    assert len(out) == 10                                             # Special row gone
    assert out["interval_h"].tolist()[1:] == [8, 8, 4, 4, 1, 1, 1, 2, 1]
    assert pd.isna(out["interval_h"].iloc[0])
    assert out["flags"].tolist() == ["first", "", "", "", "", "", "", "",
                                     "irregular_interval", "off_hour"]
    assert out["settle_ms"].iloc[-1] == h(30)


def test_duplicate_hour_flag():
    out = binance_intervals(_raw([(T0, "Regular"), (T0 + 8 * HOUR, "Regular"),
                                  (T0 + 8 * HOUR + 60_000, "Regular")]), CFG)
    assert out["flags"].tolist() == ["first", "", "duplicate_hour"]


def test_interval_history_runs():
    h = lambda x: T0 + int(x * HOUR)
    raw = _raw([(h(x), "Regular") for x in (0, 8, 16, 20, 24, 28, 29, 30)])
    hist = interval_history(binance_intervals(raw, CFG))
    assert hist["interval_h"].tolist() == [8, 4, 1]
    assert hist["n_settlements"].tolist() == [2, 3, 2]
    assert hist["from_ms"].tolist() == [h(8), h(20), h(29)]
    assert hist["to_ms"].tolist() == [h(16), h(28), h(30)]


def test_suspect_missing_settlement_inside_4h_regime():
    # 4h settlements with the 12:00 one missing: 08:00 -> 16:00 looks like a valid 8h interval.
    h = lambda x: T0 + int(x * HOUR)
    out = binance_intervals(_raw([(h(x), "Regular") for x in (0, 4, 8, 16, 20, 24)]), CFG)
    assert out["interval_h"].tolist()[1:] == [4, 4, 8, 4, 4]
    assert out["flags"].tolist() == ["first", "", "", "suspect_missing", "", ""]


def test_genuine_switch_is_not_suspect():
    # 8h regime switching to 4h for good: no row is flagged.
    h = lambda x: T0 + int(x * HOUR)
    out = binance_intervals(_raw([(h(x), "Regular") for x in (0, 8, 16, 20, 24, 28)]), CFG)
    assert out["flags"].tolist() == ["first", "", "", "", "", ""]
