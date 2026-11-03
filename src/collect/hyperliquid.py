"""Hyperliquid collector: fundingHistory and candleSnapshot via POST /info.

Raw responses are stored untouched (values exactly as the API returns them, numbers that
arrive as strings stay strings), plus a ``fetched_at`` column in UTC ms, at
``data/raw/hyperliquid/<dataset>/<coin>.parquet``.

Usage (always through the project venv):
    .venv\\Scripts\\python.exe -m src.collect.hyperliquid candles_1h candles_4h candles_1d --coins all
    .venv\\Scripts\\python.exe -m src.collect.hyperliquid funding --coins BTC ETH SOL
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from src.config import ROOT, load_config, period_ms

VENUE = "hyperliquid"
API_URL = "https://api.hyperliquid.xyz/info"


@dataclass(frozen=True)
class Dataset:
    name: str
    key: str  # timestamp column used for pagination and deduplication
    coin_col: str
    items_per_weight: int  # API adds 1 weight per this many items returned
    page_limit: int | None  # rows per full page; None = paginate until an empty page
    interval: str | None = None


DATASETS = {
    "funding": Dataset("funding", "time", "coin", 20, 500),
    # candleSnapshot pages are "about 5000" (5003 observed), so no fixed page limit.
    "candles_1h": Dataset("candles_1h", "t", "s", 60, None, "1h"),
    "candles_4h": Dataset("candles_4h", "t", "s", 60, None, "4h"),
    "candles_1d": Dataset("candles_1d", "t", "s", 60, None, "1d"),
}


def now_ms() -> int:
    return int(time.time() * 1000)


class Client:
    """POST /info with weight-based pacing and exponential backoff on 429 / 5xx."""

    def __init__(self, weight_per_min: float = 1000.0, min_sleep_s: float = 0.3,
                 max_retries: int = 8, session: requests.Session | None = None):
        # Hyperliquid allows 1200 weight per minute per IP; stay below it.
        self.weight_per_min = weight_per_min
        self.min_sleep_s = min_sleep_s
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self._next_ok = 0.0

    def post(self, payload: dict, items_per_weight: int | None = None):
        delay = 1.0
        for _ in range(self.max_retries + 1):
            wait = self._next_ok - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.session.post(API_URL, json=payload, timeout=60)
            except (requests.ConnectionError, requests.Timeout) as e:
                print(f"  network error {e!r}; retry in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                print(f"  HTTP {r.status_code}; backing off {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            r.raise_for_status()
            data = r.json()
            weight = 20
            if items_per_weight and isinstance(data, list):
                weight += math.ceil(len(data) / items_per_weight)
            sleep_s = max(self.min_sleep_s, weight * 60.0 / self.weight_per_min)
            self._next_ok = time.monotonic() + sleep_s
            return data
        raise RuntimeError(f"giving up after {self.max_retries} retries: {payload}")


def _payload(ds: Dataset, coin: str, start_ms: int, end_ms: int) -> dict:
    # Hyperliquid treats endTime as inclusive; callers pass end_ms - 1.
    if ds.name == "funding":
        return {"type": "fundingHistory", "coin": coin, "startTime": start_ms, "endTime": end_ms}
    return {"type": "candleSnapshot",
            "req": {"coin": coin, "interval": ds.interval, "startTime": start_ms, "endTime": end_ms}}


def fetch(dataset: str, coin: str, start_ms: int, end_ms: int,
          client: Client | None = None) -> pd.DataFrame:
    """All rows with start_ms <= timestamp < end_ms, paginating by startTime.

    Advances startTime to the last returned timestamp + 1. Stops on an empty page, on a
    page shorter than the dataset's page limit, at end_ms, or if the cursor stops moving.
    """
    ds = DATASETS[dataset]
    client = client or Client()
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        page = client.post(_payload(ds, coin, cursor, end_ms - 1), ds.items_per_weight)
        if not page:
            break
        rows.extend(page)
        last = max(int(r[ds.key]) for r in page)
        if last + 1 <= cursor:
            break  # no progress; avoid an infinite loop
        cursor = last + 1
        if ds.page_limit is not None and len(page) < ds.page_limit:
            break
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df[(df[ds.key] >= start_ms) & (df[ds.key] < end_ms)]
    df = df.drop_duplicates(subset=[ds.coin_col, ds.key], keep="last")
    df["fetched_at"] = now_ms()
    return df.sort_values(ds.key).reset_index(drop=True)


def raw_path(dataset: str, coin: str, root: Path | None = None) -> Path:
    root = root or ROOT / load_config()["paths"]["raw"]
    return Path(root) / VENUE / dataset / f"{coin}.parquet"


def merge(existing: pd.DataFrame, new: pd.DataFrame, dataset: str) -> pd.DataFrame:
    """Union of both frames; on duplicate (coin, timestamp) the most recent fetch wins."""
    ds = DATASETS[dataset]
    frames = [f for f in (existing, new) if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values([ds.key, "fetched_at"], kind="stable")
    df = df.drop_duplicates(subset=[ds.coin_col, ds.key], keep="last")
    return df.reset_index(drop=True)


def update(dataset: str, coin: str, start_ms: int, end_ms: int,
           client: Client | None = None, root: Path | None = None) -> dict:
    """Fetch what is missing at either end of the stored range and merge it in.

    Idempotent: rerunning never duplicates rows. The last stored row is re-fetched so an
    in-progress candle gets refreshed.
    """
    ds = DATASETS[dataset]
    path = raw_path(dataset, coin, root)
    existing = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    if existing.empty:
        ranges = [(start_ms, end_ms)]
    else:
        lo, hi = int(existing[ds.key].min()), int(existing[ds.key].max())
        ranges = []
        if start_ms < lo:
            ranges.append((start_ms, lo))
        if hi < end_ms:
            ranges.append((max(hi, start_ms), end_ms))
    new_parts = [fetch(dataset, coin, a, b, client) for a, b in ranges if a < b]
    new = pd.concat([p for p in new_parts if not p.empty], ignore_index=True) \
        if any(not p.empty for p in new_parts) else pd.DataFrame()
    merged = merge(existing, new, dataset)
    if not merged.empty:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    return {"coin": coin, "dataset": dataset, "before": len(existing),
            "fetched": len(new), "after": len(merged)}


def list_perps(client: Client | None = None) -> list[str]:
    """Currently listed (not delisted) perps on the main Hyperliquid dex."""
    client = client or Client()
    meta = client.post({"type": "meta"})
    return [a["name"] for a in meta["universe"] if not a.get("isDelisted")]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("datasets", nargs="+", choices=sorted(DATASETS))
    p.add_argument("--coins", nargs="+", required=True, help="coin names, or 'all' for every listed perp")
    p.add_argument("--start", help="ISO date; default = sample start in config.yaml")
    p.add_argument("--end-ms", type=int, help="exclusive end in UTC ms; default = now")
    args = p.parse_args(argv)

    cfg = load_config()
    start_ms = period_ms(cfg, "sample")[0] if args.start is None else \
        int(pd.Timestamp(args.start, tz="UTC").value // 1_000_000)
    end_ms = args.end_ms or now_ms()
    client = Client()
    coins = list_perps(client) if args.coins == ["all"] else args.coins
    lowered = [c.lower() for c in coins]
    if len(set(lowered)) != len(lowered):
        raise SystemExit("coin names collide case-insensitively; unsafe on Windows filenames")

    for dataset in args.datasets:  # datasets run in the order given
        print(f"== {dataset}: {len(coins)} coins", flush=True)
        for i, coin in enumerate(coins, 1):
            try:
                s = update(dataset, coin, start_ms, end_ms, client)
                print(f"[{i}/{len(coins)}] {dataset} {coin}: before={s['before']} "
                      f"fetched={s['fetched']} after={s['after']}", flush=True)
            except Exception as e:  # keep going; a rerun fills whatever failed
                print(f"[{i}/{len(coins)}] {dataset} {coin}: FAILED {e!r}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
