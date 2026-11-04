"""Binance USD-M futures collector: fundingRate, 1h klines, 1h premiumIndexKlines, fundingInfo.

Raw responses are stored untouched (numbers that arrive as strings stay strings), plus
``fetched_at`` in UTC ms. Kline arrays get the column names from the Binance docs and a
``symbol`` column. Files are keyed by universe coin (the config.yaml ``symbols`` key):
``data/raw/binance/<dataset>/<coin>.parquet``; fundingInfo is a snapshot of all symbols at
``data/raw/binance/funding_info/<fetched_at>.parquet``.

Rate limits: fundingRate and fundingInfo share 500 requests / 5 min / IP, so those calls are
spaced at least 0.75 s apart (400 / 5 min). Other endpoints count request weight (2400 / min);
the client pauses when the X-MBX-USED-WEIGHT-1M header passes a budget.

    .venv\\Scripts\\python.exe -m src.collect.binance funding_info funding klines_1h premium_index_1h --coins universe
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from src.collect import store
from src.config import ROOT, load_config, period_ms

VENUE = "binance"
BASE_URL = "https://fapi.binance.com"
KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                 "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


@dataclass(frozen=True)
class Dataset:
    name: str
    path: str
    key: str
    limit: int
    funding_limited: bool  # counts toward the shared 500 / 5 min funding-endpoint limit
    interval: str | None = None


DATASETS = {
    "funding": Dataset("funding", "/fapi/v1/fundingRate", "fundingTime", 1000, True),
    "klines_1h": Dataset("klines_1h", "/fapi/v1/klines", "open_time", 1500, False, "1h"),
    "premium_index_1h": Dataset("premium_index_1h", "/fapi/v1/premiumIndexKlines", "open_time",
                                1500, False, "1h"),
}
FUNDING_INFO_PATH = "/fapi/v1/fundingInfo"


def now_ms() -> int:
    return int(time.time() * 1000)


class Client:
    """GET with funding-endpoint spacing, a request-weight budget, and backoff."""

    def __init__(self, funding_spacing_s: float = 0.75, min_spacing_s: float = 0.2,
                 weight_budget: int = 1800, max_retries: int = 8,
                 session: requests.Session | None = None):
        self.funding_spacing_s = funding_spacing_s
        self.min_spacing_s = min_spacing_s
        self.weight_budget = weight_budget  # of 2400 per minute
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self._next_funding = 0.0
        self._next_any = 0.0

    def get(self, path: str, params: dict | None = None, funding_limited: bool = False):
        delay = 1.0
        for _ in range(self.max_retries + 1):
            gate = max(self._next_any, self._next_funding if funding_limited else 0.0)
            wait = gate - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            sent = time.monotonic()
            self._next_any = sent + self.min_spacing_s
            if funding_limited:
                self._next_funding = sent + self.funding_spacing_s
            try:
                r = self.session.get(BASE_URL + path, params=params, timeout=60)
            except (requests.ConnectionError, requests.Timeout) as e:
                print(f"  network error {e!r}; retry in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 418:
                raise RuntimeError("HTTP 418: IP banned by Binance for ignoring 429s; stop and wait")
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = float(r.headers.get("Retry-After", delay))
                print(f"  HTTP {r.status_code}; backing off {retry_after:.0f}s", file=sys.stderr)
                time.sleep(retry_after)
                delay = min(delay * 2, 60)
                continue
            r.raise_for_status()
            used = int(r.headers.get("X-MBX-USED-WEIGHT-1M", 0))
            if used > self.weight_budget:
                pause = 60 - (time.time() % 60) + 1  # weight resets each clock minute
                print(f"  used weight {used}; pausing {pause:.0f}s", file=sys.stderr)
                self._next_any = time.monotonic() + pause
            return r.json()
        raise RuntimeError(f"giving up after {self.max_retries} retries: {path} {params}")


def symbol_for(coin: str, cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    return cfg["symbols"][coin]["binance"]


def _rows(ds: Dataset, page: list, symbol: str) -> list[dict]:
    if ds.name == "funding":
        return page
    return [dict(zip(KLINE_COLUMNS, row), symbol=symbol) for row in page]


def fetch(dataset: str, coin: str, start_ms: int, end_ms: int,
          client: Client | None = None, symbol: str | None = None) -> pd.DataFrame:
    """All rows with start_ms <= timestamp < end_ms, paginating by startTime.

    Advances startTime to the last returned timestamp + 1; stops on a page shorter than the
    limit, at end_ms, or if the cursor stops moving.
    """
    ds = DATASETS[dataset]
    client = client or Client()
    symbol = symbol or symbol_for(coin)
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": symbol, "startTime": cursor, "endTime": end_ms - 1, "limit": ds.limit}
        if ds.interval:
            params["interval"] = ds.interval
        page = _rows(ds, client.get(ds.path, params, ds.funding_limited), symbol)
        if not page:
            break
        rows.extend(page)
        last = max(int(r[ds.key]) for r in page)
        if last + 1 <= cursor:
            break
        cursor = last + 1
        if len(page) < ds.limit:
            break
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df[(df[ds.key] >= start_ms) & (df[ds.key] < end_ms)]
    df = df.drop_duplicates(subset=["symbol", ds.key], keep="last")
    df["fetched_at"] = now_ms()
    return df.sort_values(ds.key).reset_index(drop=True)


def raw_path(dataset: str, coin: str, root: Path | None = None) -> Path:
    root = root or ROOT / load_config()["paths"]["raw"]
    return Path(root) / VENUE / dataset / f"{coin}.parquet"


def update(dataset: str, coin: str, start_ms: int, end_ms: int, client: Client | None = None,
           root: Path | None = None, symbol: str | None = None) -> dict:
    """Fetch what is missing at either end of the stored range and merge it in (idempotent)."""
    ds = DATASETS[dataset]
    symbol = symbol or symbol_for(coin)
    path = raw_path(dataset, coin, root)
    existing = store.read(path)
    ranges = store.missing_ranges(existing, ds.key, start_ms, end_ms)
    new = store.concat_nonempty([fetch(dataset, coin, a, b, client, symbol) for a, b in ranges])
    merged = store.merge(existing, new, ds.key, "symbol")
    if not merged.empty:
        store.write_atomic(merged, path)
    return {"coin": coin, "dataset": dataset, "before": len(existing),
            "fetched": len(new), "after": len(merged)}


def snapshot_funding_info(client: Client | None = None, root: Path | None = None) -> Path:
    """Save the current fundingInfo (all symbols with adjusted caps / intervals)."""
    client = client or Client()
    df = pd.DataFrame(client.get(FUNDING_INFO_PATH, funding_limited=True))
    fetched = now_ms()
    df["fetched_at"] = fetched
    root = root or ROOT / load_config()["paths"]["raw"]
    path = Path(root) / VENUE / "funding_info" / f"{fetched}.parquet"
    store.write_atomic(df, path)
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("datasets", nargs="+", choices=sorted(DATASETS) + ["funding_info"])
    p.add_argument("--coins", nargs="+", default=["universe"],
                   help="universe coin keys from config.yaml, or 'universe' for all of them")
    p.add_argument("--start", help="ISO date; default = sample start in config.yaml")
    p.add_argument("--end-ms", type=int, help="exclusive end in UTC ms; default = now")
    args = p.parse_args(argv)

    cfg = load_config()
    start_ms = period_ms(cfg, "sample")[0] if args.start is None else \
        int(pd.Timestamp(args.start, tz="UTC").value // 1_000_000)
    end_ms = args.end_ms or now_ms()
    coins = cfg["universe"]["coins"] if args.coins == ["universe"] else args.coins
    client = Client()

    for dataset in args.datasets:
        if dataset == "funding_info":
            print(f"== funding_info: saved {snapshot_funding_info(client)}", flush=True)
            continue
        print(f"== {dataset}: {len(coins)} coins", flush=True)
        for i, coin in enumerate(coins, 1):
            try:
                s = update(dataset, coin, start_ms, end_ms, client, symbol=symbol_for(coin, cfg))
                print(f"[{i}/{len(coins)}] {dataset} {coin}: before={s['before']} "
                      f"fetched={s['fetched']} after={s['after']}", flush=True)
            except Exception as e:  # keep going; a rerun fills whatever failed
                print(f"[{i}/{len(coins)}] {dataset} {coin}: FAILED {e!r}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
