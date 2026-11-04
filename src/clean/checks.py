"""Data-quality checks on the hourly panel and the report/data_quality.md writer."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.clean.align import FLAG_ORDER
from src.clean.normalise import HOUR


def utc(ms) -> str:
    return pd.to_datetime(ms, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M")


def has_flag(panel: pd.DataFrame, name: str) -> pd.Series:
    return panel["flags"].str.split(";").apply(lambda fs: name in fs)


def duplicate_rows(panel: pd.DataFrame) -> int:
    return int(panel.duplicated(["coin", "hour"]).sum())


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) index pairs, end exclusive, of consecutive True values."""
    m = np.concatenate([[False], mask.astype(bool), [False]])
    d = np.diff(m.astype(int))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def missing_summary(panel: pd.DataFrame) -> pd.DataFrame:
    """Per coin and venue: missing funding hours and missing price bars, with the longest gap.

    Funding gaps are in hours of the grid (hours after the period's last settlement excluded).
    Price gaps count only rows where a price is expected (every 4th hour in the 4h regime) and
    are reported as the span in hours from the first to the end of the last missing bar.
    """
    rows = []
    for coin, d in panel.groupby("coin", sort=False):
        d = d.sort_values("hour")
        hours = d["hour"].to_numpy()
        after = has_flag(d, "after_period_end").to_numpy()
        for venue in ("hl", "bn"):
            fmiss = d[f"f_{venue}_realised"].isna().to_numpy() & ~after
            fr = _runs(fmiss)
            exp = d[f"p_{venue}"].notna().to_numpy() | has_flag(d, f"{venue}_price_missing").to_numpy()
            ph, pmiss = hours[exp], has_flag(d, f"{venue}_price_missing").to_numpy()[exp]
            pr = _runs(pmiss)
            bar = np.diff(ph, append=ph[-1] + HOUR) if len(ph) else ph
            longest_f = max(((b - a, a) for a, b in fr), default=(0, None))
            longest_p = max((((ph[b - 1] + bar[b - 1] - ph[a]) // HOUR, ph[a]) for a, b in pr),
                            default=(0, None))
            rows.append({"coin": coin, "venue": "hyperliquid" if venue == "hl" else "binance",
                         "funding_missing_h": int(fmiss.sum()),
                         "funding_longest_gap_h": int(longest_f[0]),
                         "funding_longest_gap_from": utc(hours[longest_f[1]]) if longest_f[1] is not None else "",
                         "price_missing_bars": int(pmiss.sum()),
                         "price_longest_gap_h": int(longest_p[0]),
                         "price_longest_gap_from": utc(longest_p[1]) if longest_p[1] is not None else ""})
    return pd.DataFrame(rows)


def flag_counts(panel: pd.DataFrame) -> pd.DataFrame:
    counts = {f: panel.groupby("coin", sort=False).apply(lambda d, f=f: int(has_flag(d, f).sum()),
                                                          include_groups=False)
              for f in FLAG_ORDER}
    out = pd.DataFrame(counts)
    return out.loc[:, out.sum() > 0]


def factor_of_eight(panel: pd.DataFrame, coin: str = "BTC") -> dict:
    """Compare Hyperliquid hourly x 8 with Binance's 8-hour-equivalent rate, day by day.

    Both series are hourly-equivalent in the panel, so a correct normalisation gives a ratio of
    daily means near 1; a factor of ~8 or ~1/8 means a rate was divided twice or not at all.
    Ratios are taken on days where both daily means are positive.
    """
    d = panel[panel["coin"] == coin]
    day = d["hour"] // (24 * HOUR)
    daily = pd.DataFrame({"hl": (d["f_hl_realised"] * 8).groupby(day).mean(),
                          "bn": (d["f_bn_realised"] * 8).groupby(day).mean()}).dropna()
    pos = daily[(daily["hl"] > 0) & (daily["bn"] > 0)]
    ratio = pos["hl"] / pos["bn"]
    return {"days": len(daily), "days_both_positive": len(pos),
            "median_ratio": float(ratio.median()) if len(ratio) else np.nan,
            "share_within_10x": float(((ratio > 0.1) & (ratio < 10)).mean()) if len(ratio) else np.nan,
            "mean_hl_8h": float(daily["hl"].mean()), "mean_bn_8h": float(daily["bn"].mean())}


def spot_check_sample(panel: pd.DataFrame, n: int = 5, seed: int = 20261104) -> pd.DataFrame:
    """n random coin-hours with every value present, for checking by hand on the exchanges."""
    full = panel.dropna(subset=["f_hl_realised", "f_bn_realised", "p_hl", "p_bn"])
    return full.sample(n=n, random_state=seed).sort_values(["coin", "hour"])


def _md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def write_report(panel: pd.DataFrame, cfg: dict, period: str, path: Path) -> Path:
    p = cfg["periods"][period]
    miss = missing_summary(panel)
    flags = flag_counts(panel)
    f8 = factor_of_eight(panel)
    sample = spot_check_sample(panel)
    spread_rows = panel[has_flag(panel, "spread_gt_2pct")]
    off = panel[has_flag(panel, "hl_off_hour") | has_flag(panel, "bn_off_hour")]
    near = panel[has_flag(panel, "hl_near_cap") | has_flag(panel, "bn_near_cap")]
    override = panel[has_flag(panel, "bn_interval_override")]

    def hours_by_coin(df: pd.DataFrame, flag: str) -> pd.DataFrame:
        d = df[has_flag(df, flag)]
        g = d.groupby("hour")["coin"].apply(lambda c: ", ".join(c) if len(c) < 15 else "all 15")
        return pd.DataFrame({"hour (UTC)": [utc(h) for h in g.index], "coins": g.to_numpy()})

    s = [f"# Data quality — {period} period ({p['start']} to {p['end']})",
         "",
         f"Generated by `python -m src.clean.align` from `data/clean/panel.parquet`: "
         f"{len(panel):,} rows, {panel['coin'].nunique()} coins, {panel['hour'].nunique():,} hours. "
         "All times UTC. Held-out data is not included (CLAUDE.md rule 3).",
         "",
         "## Missing data and longest gap per coin and venue",
         "",
         "Funding: grid hours with no realised rate (hours after the period's last settlement are "
         "excluded). Prices: missing bars among rows where a price is expected (every 4th hour "
         "before a coin's first 1h Hyperliquid candle, then hourly); gap = span in hours.",
         "",
         _md(miss),
         "",
         f"Duplicate (coin, hour) rows: {duplicate_rows(panel)}.",
         "",
         "## Flag counts per coin",
         "",
         _md(flags.reset_index().rename(columns={"index": "coin"})),
         "",
         "## Hours with missing funding",
         "",
         "Hyperliquid (realised NaN; zero funding in P&L, last value carried forward for signals):",
         "",
         _md(hours_by_coin(panel, "hl_funding_missing")),
         "",
         "Binance:",
         "",
         _md(hours_by_coin(panel, "bn_funding_missing")),
         "",
         f"Binance interval overrides (`cleaning.binance_missing_settlements`): "
         f"{override.groupby('coin').size().to_dict()} hours.",
         "",
         "## Funding stamps more than "
         f"{cfg['cleaning']['off_hour_tolerance_min']} minutes off the hour",
         "",
         _md(hours_by_coin(off, "hl_off_hour")) if len(off) else "None.",
         "",
         "## Funding at or near the cap",
         "",
         f"Flagged, not removed (|rate| >= {cfg['cleaning']['cap_near_frac']:.0%} of the cap; "
         "Hyperliquid 4%/h, Binance caps from today's fundingInfo): "
         f"{len(near)} coin-hours" + (f", by coin {near.groupby('coin').size().to_dict()}." if len(near) else "."),
         "",
         f"## Absolute price spread above {cfg['cleaning']['spread_flag_abs']:.0%}",
         "",
         f"{len(spread_rows)} coin-hours flagged."]
    if len(spread_rows):
        top = spread_rows.reindex(spread_rows["spread"].abs().sort_values(ascending=False).index).head(20)
        s += ["", _md(pd.DataFrame({"coin": top["coin"], "hour (UTC)": top["hour"].map(utc),
                                    "p_hl": top["p_hl"], "p_bn": top["p_bn"],
                                    "spread": top["spread"].round(4)}))]
    med = panel.groupby("coin")["spread"].apply(lambda x: x.abs().median())
    s += ["", "Median |spread| by coin (a unit error would show up here as a large value): " +
          ", ".join(f"{c} {v:.4%}" for c, v in med.items()) + ".",
          "",
          "## Normalisation check (BTC): Hyperliquid hourly x 8 vs Binance 8-hour rate",
          "",
          f"Days compared: {f8['days']}; days with both daily means positive: "
          f"{f8['days_both_positive']}; median ratio HL/BN on those days: {f8['median_ratio']:.2f}; "
          f"share within 10x: {f8['share_within_10x']:.1%}. Mean 8h-equivalent rate: "
          f"HL {f8['mean_hl_8h']:.6f}, BN {f8['mean_bn_8h']:.6f}. A persistent factor of 8 would "
          "mean a normalisation bug.",
          "",
          "## Spot check: 5 random coin-hours to verify by hand",
          "",
          "Hyperliquid: funding for the hour is the fundingHistory row stamped at hour + 1h; price is "
          "the 1h or 4h candle close at hour + 1h. Binance: rate per interval = f_bn_realised x "
          "bn_interval_h, paid at the settlement ending the interval; price is the 1h kline close at "
          "hour + 1h. Prices are per single coin unit (k/1000 contracts divided by 1000).",
          "",
          _md(pd.DataFrame({"coin": sample["coin"], "hour (UTC)": sample["hour"].map(utc),
                            "f_hl_realised": sample["f_hl_realised"],
                            "bn_rate_per_interval": (sample["f_bn_realised"] * sample["bn_interval_h"]),
                            "bn_interval_h": sample["bn_interval_h"].astype(int),
                            "p_hl": sample["p_hl"], "p_bn": sample["p_bn"]})),
          ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(s), encoding="utf-8")
    return path
