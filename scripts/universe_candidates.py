"""Universe candidate table (does not choose the universe).

For every Hyperliquid perp whose stored 4h candles reach back to the sample start:
  * average daily USD volume (1d volume x close) over the universe selection window
  * the matching Binance USD-M USDT perpetual, with contract units (k / 1000 / 1000000 / 1M)
  * whether that contract was onboarded by the sample start and is still TRADING

Saves the raw Binance exchangeInfo snapshot to data/raw/binance/exchangeInfo/<fetched_at>.json
and the table to report/universe_candidates.csv.

    .venv\\Scripts\\python.exe -m scripts.universe_candidates
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import requests

from src.collect.hyperliquid import now_ms, raw_path
from src.config import ROOT, day_start_ms, load_config, period_ms

EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BN_PREFIX = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")
BN_UNITS = {"1000000": 1_000_000, "100000": 100_000, "10000": 10_000, "1000": 1000, "1M": 1_000_000}


def hl_base(coin: str) -> tuple[str, int]:
    """'kPEPE' -> ('PEPE', 1000); 'BTC' -> ('BTC', 1)."""
    if len(coin) > 1 and coin[0] == "k" and coin[1].isupper():
        return coin[1:], 1000
    return coin, 1


def bn_base(base_asset: str) -> tuple[str, int]:
    """'1000PEPE' -> ('PEPE', 1000); '1MBABYDOGE' -> ('BABYDOGE', 1_000_000)."""
    m = BN_PREFIX.match(base_asset)
    if not m:
        return base_asset, 1
    return base_asset[m.end():], BN_UNITS[m.group(1)]


def fetch_exchange_info() -> dict:
    r = requests.get(EXCHANGE_INFO_URL, timeout=30)
    r.raise_for_status()
    info = r.json()
    out = ROOT / load_config()["paths"]["raw"] / "binance" / "exchangeInfo" / f"{now_ms()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(info), encoding="utf-8")
    return info


def binance_perps(info: dict) -> pd.DataFrame:
    rows = []
    for s in info["symbols"]:
        if s["contractType"] != "PERPETUAL" or s["quoteAsset"] != "USDT":
            continue
        base, unit = bn_base(s["baseAsset"])
        rows.append({"bn_symbol": s["symbol"], "base": base, "bn_unit": unit,
                     "bn_status": s["status"], "bn_onboard_ms": s["onboardDate"]})
    return pd.DataFrame(rows)


def hl_volumes(cfg: dict) -> pd.DataFrame:
    sample_start = period_ms(cfg, "sample")[0]
    w = cfg["universe"]["selection_window"]
    w_start = day_start_ms(w["start"])
    w_end = period_ms({"periods": {"w": w}}, "w")[1]
    rows = []
    for f in sorted(raw_path("candles_4h", "_").parent.glob("*.parquet")):
        coin = f.stem
        if pd.read_parquet(f, columns=["t"])["t"].min() > sample_start:
            continue  # no 4h candles back to the sample start
        d = pd.read_parquet(raw_path("candles_1d", coin))
        d = d[(d["t"] >= w_start) & (d["t"] < w_end)]
        usd = d["v"].astype(float) * d["c"].astype(float)
        base, unit = hl_base(coin)
        rows.append({"hl_coin": coin, "base": base, "hl_unit": unit,
                     "avg_daily_usd_vol": usd.mean(), "n_days": len(d)})
    return pd.DataFrame(rows)


def main() -> None:
    cfg = load_config()
    sample_start = period_ms(cfg, "sample")[0]
    hl = hl_volumes(cfg)
    bn = binance_perps(fetch_exchange_info())

    # Prefer a TRADING contract, then the oldest one, when several share a base.
    bn = bn.assign(_trading=bn["bn_status"] == "TRADING")
    bn = bn.sort_values(["base", "_trading", "bn_onboard_ms"], ascending=[True, False, True])
    n_contracts = bn.groupby("base").size().rename("bn_contracts")
    bn = bn.drop_duplicates("base").drop(columns="_trading").join(n_contracts, on="base")

    t = hl.merge(bn, on="base", how="left")
    t["bn_onboard"] = pd.to_datetime(t["bn_onboard_ms"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    t["bn_by_start"] = t["bn_onboard_ms"] <= sample_start
    t["bn_trading"] = t["bn_status"] == "TRADING"
    t["eligible"] = t["bn_by_start"] & t["bn_trading"]
    t = t.sort_values("avg_daily_usd_vol", ascending=False).reset_index(drop=True)
    t.insert(0, "rank", t.index + 1)
    cols = ["rank", "hl_coin", "avg_daily_usd_vol", "n_days", "bn_symbol", "hl_unit", "bn_unit",
            "bn_onboard", "bn_status", "bn_by_start", "bn_trading", "eligible", "bn_contracts"]
    out = ROOT / "report" / "universe_candidates.csv"
    t[cols].to_csv(out, index=False)
    print(f"{len(t)} HL perps with 4h candles from the sample start; "
          f"{int(t['eligible'].sum())} eligible; {int(t['bn_symbol'].isna().sum())} without a Binance match")
    print(f"saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
