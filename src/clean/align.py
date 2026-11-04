"""Build the clean hourly panel: one row per coin per UTC hour.

Row ``hour = H`` describes the hour [H, H+1h). All timestamps are UTC ms.

Funding (hourly-equivalent rates, fractions per hour):
  f_hl_realised  Hyperliquid funding for [H, H+1h): the fundingHistory row stamped H+1h
                 (DECISIONS.md: a row stamped T covers [T-1h, T)). Already hourly.
  f_bn_realised  Binance settlement rate / interval for every hour of the interval it covers:
                 a settlement at T with interval I covers [T-I, T).
  f_*_known      value usable for a decision taken at time H (the start of the row's hour): the
                 latest row stamped T with T + 1h <= H (DECISIONS.md: known only from T+1h).
                 Missing rows are therefore carried forward automatically.
  bn_interval_h  interval of the Binance settlement covering the hour.
  bn_settle      True if a Binance settlement happens at the end of this hour (at H+1h); Binance
                 funding is paid only to positions open at that moment.
  gap            f_hl_known - f_bn_known. Positive: Hyperliquid longs pay more.

Missing funding is kept as NaN in the realised series (book zero in P&L) and flagged.

Prices (per single coin unit, after dividing by the contract unit; unclosed candles dropped):
  p_hl, p_bn     closes at the end of hour H, i.e. at H+1h. Before a coin's first stored 1h
                 Hyperliquid candle (2026-04-09) only every 4th hour has a price (4h candles,
                 rows with (H+1h) % 4h == 0); from then on every hour. p_bn uses the Binance 1h
                 close at the same moment and the same rows.
  spread         ln(p_hl / p_bn).

    .venv\\Scripts\\python.exe -m src.clean.align            # design period (rule 3)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from src.clean.normalise import HOUR, binance_intervals, round_to_hour
from src.config import ROOT, load_config, period_ms

PANEL_COLUMNS = ["coin", "hour", "f_hl_realised", "f_bn_realised", "f_hl_known", "f_bn_known",
                 "bn_interval_h", "bn_settle", "gap", "p_hl", "p_bn", "spread", "flags"]


# --- Hyperliquid funding -------------------------------------------------------------------

def hl_funding(raw: pd.DataFrame, hours: np.ndarray, cfg: dict) -> pd.DataFrame:
    tol = cfg["cleaning"]["off_hour_tolerance_min"] * 60_000
    cap = cfg["cleaning"]["hl_funding_cap_per_hour"] * cfg["cleaning"]["cap_near_frac"]
    out = pd.DataFrame(index=pd.Index(hours, name="hour"))
    if raw.empty:
        out["f_hl_realised"] = out["f_hl_known"] = np.nan
        for f in ("hl_off_hour", "hl_duplicate_hour", "hl_near_cap", "hl_after_end"):
            out[f] = False
        return out
    r = pd.DataFrame({"time": raw["time"].astype("int64"),
                      "rate": raw["fundingRate"].astype(float)})
    r["T"] = round_to_hour(r["time"])
    r["off_hour"] = (r["time"] - r["T"]).abs() > tol
    r = r.sort_values("time")
    r["dup"] = r.duplicated("T", keep=False)
    r = r.drop_duplicates("T", keep="last").sort_values("T").reset_index(drop=True)

    by_hour = r.set_index(r["T"] - HOUR)
    out["f_hl_realised"] = by_hour["rate"].reindex(hours).to_numpy()
    out["hl_off_hour"] = by_hour["off_hour"].reindex(hours, fill_value=False).to_numpy()
    out["hl_duplicate_hour"] = by_hour["dup"].reindex(hours, fill_value=False).to_numpy()
    out["hl_near_cap"] = out["f_hl_realised"].abs() >= cap
    out["hl_after_end"] = hours >= r["T"].max()
    out["f_hl_known"] = _known(hours, r["T"].to_numpy(), r["rate"].to_numpy())
    return out


def _known(hours: np.ndarray, stamps: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Latest value whose stamp T satisfies T + 1h <= H, for every hour H."""
    idx = np.searchsorted(stamps, hours - HOUR, side="right") - 1
    known = np.where(idx >= 0, values[np.clip(idx, 0, None)], np.nan)
    return known.astype(float)


# --- Binance funding -----------------------------------------------------------------------

def bn_funding(raw: pd.DataFrame, symbol: str, hours: np.ndarray, cfg: dict,
               caps: pd.DataFrame | None = None) -> pd.DataFrame:
    out = pd.DataFrame(index=pd.Index(hours, name="hour"))
    flag_cols = ("bn_off_hour", "bn_irregular_interval", "bn_suspect_missing",
                 "bn_interval_override", "bn_near_cap", "bn_settle", "bn_after_end")
    if raw.empty:
        out["f_bn_realised"] = out["f_bn_known"] = out["bn_interval_h"] = np.nan
        for f in flag_cols:
            out[f] = False
        return out

    st = binance_intervals(raw, cfg)
    st["rate"] = st["fundingRate"].astype(float)
    st["override"] = False
    for case in cfg["cleaning"].get("binance_missing_settlements", []):
        if symbol in case["symbols"]:
            t = int(pd.Timestamp(case["settle"], tz="UTC").value // 1_000_000)
            hit = st["settle_ms"] == t
            st.loc[hit, "interval_h"] = case["interval_h"]
            st.loc[hit, "override"] = True
    has = lambda name: st["flags"].str.contains(name)
    st["off_hour"] = has("off_hour")
    st["irregular"] = has("irregular_interval")
    st["suspect"] = has("suspect_missing") & ~st["override"]
    st["near_cap"] = False
    if caps is not None and symbol in set(caps["symbol"]):
        c = caps.set_index("symbol").loc[symbol]
        near = cfg["cleaning"]["cap_near_frac"]
        st["near_cap"] = ((st["rate"] >= near * float(c["adjustedFundingRateCap"]))
                          | (st["rate"] <= near * float(c["adjustedFundingRateFloor"])))

    # Expand each settlement over the hours it covers: [T - I, T).
    cov = st[st["interval_h"].notna() & (st["interval_h"] > 0)].copy()
    n = cov["interval_h"].astype(int)
    rep = cov.loc[cov.index.repeat(n)].copy()
    k = rep.groupby(level=0).cumcount().to_numpy()
    rep["hour"] = rep["settle_ms"].to_numpy() - (np.repeat(n.to_numpy(), n.to_numpy()) - k) * HOUR
    rep["f"] = rep["rate"] / rep["interval_h"]
    rep["bn_settle"] = rep["hour"] == rep["settle_ms"] - HOUR
    rep = rep.drop_duplicates("hour", keep="last").set_index("hour")

    cols = {"f_bn_realised": "f", "bn_interval_h": "interval_h"}
    for dst, src in cols.items():
        out[dst] = rep[src].reindex(hours).to_numpy()
    for dst, src in {"bn_off_hour": "off_hour", "bn_irregular_interval": "irregular",
                     "bn_suspect_missing": "suspect", "bn_interval_override": "override",
                     "bn_near_cap": "near_cap", "bn_settle": "bn_settle"}.items():
        out[dst] = rep[src].reindex(hours, fill_value=False).astype(bool).to_numpy()
    out["bn_after_end"] = hours >= st["settle_ms"].max()
    out["f_bn_known"] = _known(hours, st["settle_ms"].to_numpy(),
                               (st["rate"] / st["interval_h"]).to_numpy())
    return out


# --- Prices --------------------------------------------------------------------------------

def _closed(c: pd.DataFrame, close_col: str) -> pd.DataFrame:
    """Drop candles that had not closed when they were fetched (DECISIONS.md)."""
    return c[c[close_col].astype("int64") < c["fetched_at"].astype("int64")] if not c.empty else c


def prices(hl_1h: pd.DataFrame, hl_4h: pd.DataFrame, bn_1h: pd.DataFrame, hl_unit: float,
           bn_unit: float, hours: np.ndarray) -> pd.DataFrame:
    h1, h4, b1 = _closed(hl_1h, "T"), _closed(hl_4h, "T"), _closed(bn_1h, "close_time")
    first_1h = h1["t"].min() if not h1.empty else np.iinfo(np.int64).max
    expected = (hours >= first_1h) | ((hours + HOUR) % (4 * HOUR) == 0)

    def series(df, t_col, close_col, shift):
        if df.empty:
            return pd.Series(np.nan, index=hours)
        s = pd.Series(df[close_col].astype(float).to_numpy(), index=df[t_col].astype("int64") + shift)
        return s[~s.index.duplicated(keep="last")].reindex(hours)

    # Hyperliquid candles: t = open time, c = close. Binance klines: open_time, close.
    p_hl = series(h1, "t", "c", 0).combine_first(series(h4, "t", "c", 3 * HOUR)) / hl_unit
    p_bn = series(b1, "open_time", "close", 0) / bn_unit
    out = pd.DataFrame(index=pd.Index(hours, name="hour"))
    out["p_hl"] = p_hl.where(expected).to_numpy()
    out["p_bn"] = p_bn.where(expected).to_numpy()
    out["price_expected"] = expected
    return out


# --- One coin ------------------------------------------------------------------------------

FLAG_ORDER = ["hl_funding_missing", "bn_funding_missing", "hl_off_hour", "hl_duplicate_hour",
              "bn_off_hour", "bn_irregular_interval", "bn_suspect_missing", "bn_interval_override",
              "hl_near_cap", "bn_near_cap", "hl_price_missing", "bn_price_missing",
              "spread_gt_2pct", "after_period_end"]


def coin_panel(coin: str, start_ms: int, end_ms: int, cfg: dict, hl_fund: pd.DataFrame,
               bn_fund: pd.DataFrame, hl_1h: pd.DataFrame, hl_4h: pd.DataFrame,
               bn_1h: pd.DataFrame, caps: pd.DataFrame | None = None) -> pd.DataFrame:
    sym = cfg["symbols"][coin]
    hours = np.arange(start_ms, end_ms, HOUR, dtype="int64")
    hl = hl_funding(hl_fund, hours, cfg)
    bn = bn_funding(bn_fund, sym["binance"], hours, cfg, caps)
    px = prices(hl_1h, hl_4h, bn_1h, sym["hl_unit"], sym["bn_unit"], hours)
    d = pd.concat([hl, bn, px], axis=1).reset_index()
    d.insert(0, "coin", coin)
    d["gap"] = d["f_hl_known"] - d["f_bn_known"]
    d["spread"] = np.log(d["p_hl"] / d["p_bn"])

    after_hl, after_bn = d["hl_after_end"], d["bn_after_end"]
    d["after_period_end"] = after_hl | after_bn
    d["hl_funding_missing"] = d["f_hl_realised"].isna() & ~after_hl
    d["bn_funding_missing"] = d["f_bn_realised"].isna() & ~after_bn
    d["hl_price_missing"] = d["price_expected"] & d["p_hl"].isna()
    d["bn_price_missing"] = d["price_expected"] & d["p_bn"].isna()
    d["spread_gt_2pct"] = d["spread"].abs() > cfg["cleaning"]["spread_flag_abs"]

    flags = np.full(len(d), "", dtype=object)
    for name in FLAG_ORDER:
        flags = np.where(d[name].to_numpy(), flags + name + ";", flags)
    d["flags"] = pd.Series(flags).str.rstrip(";")
    return d[PANEL_COLUMNS]


# --- Loading and building ------------------------------------------------------------------

def _load(path: Path, key: str, end_ms: int) -> pd.DataFrame:
    """Read a raw file and drop everything at or after end_ms before any other processing."""
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    return df[df[key].astype("int64") < end_ms].reset_index(drop=True)


def build_panel(cfg: dict | None = None, period: str = "design") -> pd.DataFrame:
    cfg = cfg or load_config()
    start_ms, end_ms = period_ms(cfg, period)
    raw = ROOT / cfg["paths"]["raw"]
    snaps = sorted((raw / "binance" / "funding_info").glob("*.parquet"))
    caps = pd.read_parquet(snaps[-1]) if snaps else None
    parts = []
    for coin in cfg["universe"]["coins"]:
        hl = lambda ds, key: _load(raw / "hyperliquid" / ds / f"{coin}.parquet", key, end_ms)
        bn = lambda ds, key: _load(raw / "binance" / ds / f"{coin}.parquet", key, end_ms)
        parts.append(coin_panel(coin, start_ms, end_ms, cfg,
                                hl_fund=hl("funding", "time"), bn_fund=bn("funding", "fundingTime"),
                                hl_1h=hl("candles_1h", "t"), hl_4h=hl("candles_4h", "t"),
                                bn_1h=bn("klines_1h", "open_time"), caps=caps))
    return pd.concat(parts, ignore_index=True)


def main(argv: list[str] | None = None) -> None:
    from src.clean import checks

    p = argparse.ArgumentParser()
    p.add_argument("--period", default="design", choices=["design"],
                   help="only the design period is allowed before Phase 5 (CLAUDE.md rule 3)")
    args = p.parse_args(argv)
    cfg = load_config()
    panel = build_panel(cfg, args.period)
    out = ROOT / cfg["paths"]["clean"] / "panel.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out, index=False)
    report = checks.write_report(panel, cfg, args.period, ROOT / "report" / "data_quality.md")
    print(f"panel: {len(panel):,} rows -> {out.relative_to(ROOT)}; report -> {report.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
