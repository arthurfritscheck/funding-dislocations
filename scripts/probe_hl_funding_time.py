"""Evidence for what Hyperliquid fundingHistory `time` means (start or end of the funding hour).

Polls live state and compares it with the rows fundingHistory later publishes:
  * every 20 s: metaAndAssetCtxs -> live predicted `funding` and instantaneous `premium`
  * every 60 s: predictedFundings -> HL `nextFundingTime` for the accumulating rate
  * every 60 s: fundingHistory for the last 3 hours -> when each row first appears

    .venv\\Scripts\\python.exe -m scripts.probe_hl_funding_time collect --until-ms <ms>
    .venv\\Scripts\\python.exe -m scripts.probe_hl_funding_time analyse
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import pandas as pd

from src.collect.hyperliquid import Client, now_ms
from src.config import ROOT

COINS = ["BTC", "ETH", "SOL"]
OUT = ROOT / "data" / "raw" / "hyperliquid" / "probe"
HOUR = 3_600_000


def _append(path: Path, row: dict) -> None:
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def collect(until_ms: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    client = Client(weight_per_min=600, min_sleep_s=0.0)
    seen: set[tuple[str, int]] = set()
    last_minute = 0.0
    while now_ms() < until_ms:
        t0 = time.monotonic()
        try:
            meta, ctxs = client.post({"type": "metaAndAssetCtxs"})
            ts = now_ms()
            names = [a["name"] for a in meta["universe"]]
            for coin in COINS:
                c = ctxs[names.index(coin)]
                _append(OUT / "ctx.csv", {"poll_ms": ts, "coin": coin, "funding": c["funding"],
                                          "premium": c.get("premium"), "oraclePx": c["oraclePx"],
                                          "markPx": c["markPx"]})
            if t0 - last_minute >= 60:
                last_minute = t0
                pf = client.post({"type": "predictedFundings"})
                ts = now_ms()
                for coin, venues in pf:
                    if coin not in COINS:
                        continue
                    for venue, info in venues:
                        if venue == "HlPerp" and info:
                            _append(OUT / "predicted.csv", {"poll_ms": ts, "coin": coin,
                                    "fundingRate": info["fundingRate"],
                                    "nextFundingTime": info["nextFundingTime"]})
                for coin in COINS:
                    rows = client.post({"type": "fundingHistory", "coin": coin,
                                        "startTime": now_ms() - 3 * HOUR}, 20)
                    ts = now_ms()
                    for r in rows:
                        if (coin, r["time"]) not in seen:
                            seen.add((coin, r["time"]))
                            _append(OUT / "history_first_seen.csv",
                                    {"first_seen_ms": ts, "coin": coin, "time": r["time"],
                                     "fundingRate": r["fundingRate"], "premium": r["premium"]})
        except Exception as e:  # keep polling through transient errors
            print(f"poll error {e!r}", flush=True)
        time.sleep(max(0.0, 20 - (time.monotonic() - t0)))
    print("probe finished", flush=True)


def _fmt(ms: int) -> str:
    return pd.to_datetime(ms, unit="ms", utc=True).strftime("%H:%M:%S.%f")[:-3]


def analyse() -> None:
    ctx = pd.read_csv(OUT / "ctx.csv")
    hist = pd.read_csv(OUT / "history_first_seen.csv")
    pred = pd.read_csv(OUT / "predicted.csv")
    first_poll, last_poll = ctx.poll_ms.min(), ctx.poll_ms.max()
    hist["hour"] = (hist.time // HOUR) * HOUR  # strip settlement jitter
    pred["next_hour"] = (pred.nextFundingTime // HOUR) * HOUR
    print(f"probe ran {_fmt(first_poll)} to {_fmt(last_poll)} UTC, {len(ctx)//len(COINS)} polls per coin\n")

    boundaries = [b for b in range((first_poll // HOUR + 1) * HOUR, last_poll + 1, HOUR)]
    for coin in COINS:
        c = ctx[ctx.coin == coin]
        h = hist[hist.coin == coin].set_index("hour")
        print(f"=== {coin}")
        for b in boundaries:
            before = c[c.poll_ms < b]
            last_pred = before.iloc[-1] if len(before) else None
            window = c[(c.poll_ms >= b - HOUR) & (c.poll_ms < b)]
            cover = (window.poll_ms.max() - window.poll_ms.min()) / HOUR if len(window) > 1 else 0
            print(f"boundary {_fmt(b)}:")
            if last_pred is not None:
                print(f"  last live `funding` before boundary (polled {_fmt(last_pred.poll_ms)}): "
                      f"{last_pred.funding:.10f}")
            print(f"  mean live premium over [{_fmt(b - HOUR)}, {_fmt(b)}) "
                  f"({len(window)} polls, {cover:.0%} of hour): {window.premium.mean():.10f}")
            p = pred[(pred.coin == coin) & (pred.poll_ms < b) & (pred.poll_ms >= b - HOUR)]
            if len(p):
                print(f"  predictedFundings nextFundingTime during that hour: "
                      f"{sorted({_fmt(x) for x in p.nextFundingTime})}")
            for label, hr in (("stamped at boundary", b), ("stamped 1h earlier", b - HOUR)):
                if hr in h.index:
                    r = h.loc[hr]
                    seen = "already present when probe started" if r.first_seen_ms <= first_poll + 90_000 \
                        else f"first seen {_fmt(r.first_seen_ms)}"
                    print(f"  fundingHistory row {label} (time={_fmt(r.time)}): rate={r.fundingRate:.10f} "
                          f"premium={r.premium:.10f}  [{seen}]")
        print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--until-ms", type=int, required=True)
    sub.add_parser("analyse")
    a = ap.parse_args()
    collect(a.until_ms) if a.cmd == "collect" else analyse()
