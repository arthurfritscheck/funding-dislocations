"""Raw parquet storage shared by the venue collectors: merge, gap ranges, atomic writes."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


def read(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def merge(existing: pd.DataFrame, new: pd.DataFrame, key: str, coin_col: str) -> pd.DataFrame:
    """Union of both frames; on duplicate (coin, timestamp) the most recent fetch wins."""
    frames = [f for f in (existing, new) if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values([key, "fetched_at"], kind="stable")
    df = df.drop_duplicates(subset=[coin_col, key], keep="last")
    return df.reset_index(drop=True)


def missing_ranges(existing: pd.DataFrame, key: str, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Ranges to fetch at either end of what is stored. The last stored row is re-fetched so an
    in-progress candle gets refreshed."""
    if existing.empty:
        return [(start_ms, end_ms)]
    lo, hi = int(existing[key].min()), int(existing[key].max())
    ranges = []
    if start_ms < lo:
        ranges.append((start_ms, lo))
    if hi < end_ms:
        ranges.append((max(hi, start_ms), end_ms))
    return [(a, b) for a, b in ranges if a < b]


def write_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def concat_nonempty(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
