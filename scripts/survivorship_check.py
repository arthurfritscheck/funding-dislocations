"""Survivorship check: would any delisted Hyperliquid perp have ranked in the top 15?

Fetches 1d candles over the universe selection window for every perp Hyperliquid marks as
delisted (stored like any other raw candles), computes the same average daily USD volume as
scripts/universe_candidates.py, and ranks them together with the listed candidates.
Binance mapping uses the latest saved exchangeInfo snapshot. Does not change the universe.

    .venv\\Scripts\\python.exe -m scripts.survivorship_check
"""

from __future__ import annotations

import json

import pandas as pd

from scripts.universe_candidates import binance_perps, hl_base
from src.collect.hyperliquid import Client, raw_path, update
from src.config import ROOT, day_start_ms, load_config, period_ms


def delisted_perps(client: Client) -> list[str]:
    meta = client.post({"type": "meta"})
    return [a["name"] for a in meta["universe"] if a.get("isDelisted")]


def main() -> None:
    cfg = load_config()
    w = cfg["universe"]["selection_window"]
    w_start = day_start_ms(w["start"])
    w_end = period_ms({"periods": {"w": w}}, "w")[1]
    n_universe = len(cfg["universe"]["coins"])

    client = Client(weight_per_min=150)  # a funding download may be sharing the IP budget
    rows = []
    for coin in delisted_perps(client):
        update("candles_1d", coin, w_start, w_end, client)
        path = raw_path("candles_1d", coin)
        d = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["t", "v", "c"])
        d = d[(d["t"] >= w_start) & (d["t"] < w_end)]
        base, unit = hl_base(coin)
        rows.append({"hl_coin": coin, "base": base, "hl_unit": unit, "delisted": True,
                     "avg_daily_usd_vol": (d["v"].astype(float) * d["c"].astype(float)).mean(),
                     "n_days": len(d),
                     "listed_at_start": bool(len(d)) and int(d["t"].min()) == w_start})
    dl = pd.DataFrame(rows)

    snap = max((ROOT / cfg["paths"]["raw"] / "binance" / "exchangeInfo").glob("*.json"))
    bn = binance_perps(json.loads(snap.read_text(encoding="utf-8")))
    bn = bn.sort_values(["base", "bn_onboard_ms"]).drop_duplicates("base")
    dl = dl.merge(bn[["base", "bn_symbol", "bn_status", "bn_onboard_ms"]], on="base", how="left")
    dl["bn_onboard"] = pd.to_datetime(dl["bn_onboard_ms"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")

    listed = pd.read_csv(ROOT / "report" / "universe_candidates.csv")
    listed = listed.assign(delisted=False, listed_at_start=True)
    both = pd.concat([listed, dl[dl["n_days"] > 0]], ignore_index=True)
    both = both.sort_values("avg_daily_usd_vol", ascending=False).reset_index(drop=True)
    both["rank_all"] = both.index + 1
    cutoff = listed[listed["hl_coin"].isin(cfg["universe"]["coins"])]["avg_daily_usd_vol"].min()

    out = both[both["delisted"]].copy()
    out["above_universe_cutoff"] = out["avg_daily_usd_vol"] >= cutoff
    cols = ["rank_all", "hl_coin", "avg_daily_usd_vol", "n_days", "listed_at_start", "bn_symbol",
            "bn_onboard", "bn_status", "above_universe_cutoff"]
    out[cols].to_csv(ROOT / "report" / "survivorship_check.csv", index=False)
    print(f"{len(dl)} delisted perps; {int((dl['n_days'] > 0).sum())} traded in the window; "
          f"source snapshot {snap.name}")
    print(f"universe cut-off (lowest volume in universe): {cutoff / 1e6:.1f} $m/day")
    top = out[out["rank_all"] <= n_universe + 5]
    print(top[cols].assign(avg_daily_usd_vol=(top["avg_daily_usd_vol"] / 1e6).round(1))
          .to_string(index=False))


if __name__ == "__main__":
    main()
