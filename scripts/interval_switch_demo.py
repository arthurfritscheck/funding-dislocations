"""Show interval inference on a Binance symbol outside the universe that switched intervals.

Fetches funding for the design period only (nothing is saved) and prints the interval runs plus
the settlements around each switch.

    .venv\\Scripts\\python.exe -m scripts.interval_switch_demo RVNUSDT
"""

import sys

import pandas as pd

from src.clean.normalise import binance_intervals, interval_history
from src.collect import binance as bn
from src.config import load_config, period_ms


def fmt(ms) -> str:
    return pd.to_datetime(ms, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M")


def main(symbol: str) -> None:
    cfg = load_config()
    start, end = period_ms(cfg, "design")
    df = binance_intervals(bn.fetch("funding", symbol, start, end, symbol=symbol), cfg)
    hist = interval_history(df)
    print(f"{symbol}: {len(df)} settlements, design period\n")
    print(hist.assign(**{"from": hist.from_ms.map(fmt), "to": hist.to_ms.map(fmt)})
          [["from", "to", "interval_h", "n_settlements"]].to_string(index=False))
    for _, run in hist.iloc[1:].iterrows():
        i = df.index[df["settle_ms"] == run.from_ms][0]
        rows = df.loc[i - 3:i + 2, ["settle_ms", "interval_h", "fundingRate", "flags"]]
        print(f"\naround {fmt(run.from_ms)} (switch to {run.interval_h:g}h):")
        print(rows.assign(settle=rows.settle_ms.map(fmt))[["settle", "interval_h", "fundingRate", "flags"]]
              .to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "RVNUSDT")
