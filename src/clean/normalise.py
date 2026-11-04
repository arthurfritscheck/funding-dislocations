"""Binance funding settlements: drop Special rows, align stamps, infer each interval.

Binance `fundingTime` is the settlement moment; the rate applies to the interval that ends there.
The interval for settlement i is the spacing to the previous Regular settlement, after rounding
both stamps to the nearest hour (DECISIONS.md, 2026-11-04). `fundingInfo` only shows today's
interval, so history must come from the spacing.

Flags (semicolon-separated in `flags`):
  first               no earlier settlement in the data, so the interval is unknown
  irregular_interval  spacing not in config `cleaning.binance_intervals_h` (listing gap, outage...)
  duplicate_hour      two settlements round to the same hour
  off_hour            stamp more than `cleaning.off_hour_tolerance_min` from the hour
  suspect_missing     a valid-looking interval that is a whole multiple of the same shorter
                      interval on both sides (e.g. one 8h step inside a 4h regime): more likely a
                      missing settlement than a real switch. `interval_h` is left as measured.
"""

from __future__ import annotations

import pandas as pd

from src.config import load_config

HOUR = 3_600_000


def round_to_hour(ms):
    """Nearest whole UTC hour in ms (half an hour rounds up). Works on ints and Series."""
    return ((ms + HOUR // 2) // HOUR) * HOUR


def binance_intervals(raw: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """One row per Regular settlement with `settle_ms`, `interval_h` and `flags`.

    Input: raw `fundingRate` rows (symbol, fundingTime, fundingRate, markPrice, rateType).
    Special rows are dropped before spacing is measured.
    """
    cfg = cfg or load_config()
    tol_ms = cfg["cleaning"]["off_hour_tolerance_min"] * 60_000
    valid = set(cfg["cleaning"]["binance_intervals_h"])

    df = raw[raw["rateType"] != "Special"].sort_values("fundingTime").reset_index(drop=True)
    df = df[["symbol", "fundingTime", "fundingRate", "markPrice", "rateType"]].copy()
    df["settle_ms"] = round_to_hour(df["fundingTime"].astype("int64"))
    df["interval_h"] = df["settle_ms"].diff() / HOUR

    first = df.index == 0
    duplicate = df["interval_h"] == 0
    irregular = ~first & ~duplicate & ~df["interval_h"].isin(valid)
    off_hour = (df["fundingTime"] - df["settle_ms"]).abs() > tol_ms
    prev, nxt = df["interval_h"].shift(1), df["interval_h"].shift(-1)
    suspect = (df["interval_h"].isin(valid) & (prev == nxt) & (df["interval_h"] > prev)
               & (df["interval_h"] % prev == 0))
    labels = [(first, "first"), (irregular, "irregular_interval"),
              (duplicate, "duplicate_hour"), (off_hour, "off_hour"),
              (suspect, "suspect_missing")]
    df["flags"] = [";".join(name for mask, name in labels if mask[i]) for i in range(len(df))]
    return df


def interval_history(df: pd.DataFrame) -> pd.DataFrame:
    """Runs of consecutive settlements with the same inferred interval.

    Columns: from_ms / to_ms (first and last settlement of the run), interval_h, n_settlements.
    The `first` row (unknown interval) is skipped.
    """
    d = df[~df["flags"].str.contains("first")]
    run = (d["interval_h"] != d["interval_h"].shift()).cumsum()
    out = d.groupby(run).agg(from_ms=("settle_ms", "first"), to_ms=("settle_ms", "last"),
                             interval_h=("interval_h", "first"),
                             n_settlements=("settle_ms", "size"))
    return out.reset_index(drop=True)
