"""Descriptive statistics of the funding gap, design period only (Phase 3 items 1 and 6).

All rates in the panel are hourly fractions; tables report APR = hourly x 8760.
The gap is the known-at-t series (f_hl_known - f_bn_known); venue funding levels use the
realised series. No trading rules here.

    .venv\\Scripts\\python.exe -m src.analysis.gap_stats      # writes report/tables and figures
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.clean.checks import factor_of_eight  # noqa: E402
from src.clean.normalise import HOUR  # noqa: E402
from src.config import ROOT, load_config, period_ms  # noqa: E402

APR = 8760
TABLES = ROOT / "report" / "tables"
FIGURES = ROOT / "report" / "figures"

# Chart tokens (light theme of the reference palette in the dataviz guidance).
SURFACE, INK, INK_2, GRID, SERIES = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1", "#2a78d6"


def load_design_panel(cfg: dict | None = None) -> pd.DataFrame:
    """The clean panel, cut at the design cut-off whatever the file contains (rule 3)."""
    cfg = cfg or load_config()
    _, end = period_ms(cfg, "design")
    p = pd.read_parquet(ROOT / cfg["paths"]["clean"] / "panel.parquet")
    p = p[p["hour"] < end].copy()
    p["coin"] = pd.Categorical(p["coin"], categories=cfg["universe"]["coins"], ordered=True)
    return p


def gap_summary(panel: pd.DataFrame, thresholds=(0.10, 0.25, 0.50)) -> pd.DataFrame:
    """Per coin: distribution of the hourly gap in APR, and the share of hours above thresholds."""
    rows = []
    for coin, d in panel.groupby("coin", observed=True):
        g = d["gap"].dropna() * APR
        row = {"coin": coin, "hours": len(g), "mean": g.mean(), "median": g.median(),
               "std": g.std(), "p1": g.quantile(0.01), "p5": g.quantile(0.05),
               "p95": g.quantile(0.95), "p99": g.quantile(0.99)}
        for t in thresholds:
            row[f"share_abs_gt_{t:.0%}"] = (g.abs() > t).mean()
        rows.append(row)
    return pd.DataFrame(rows).set_index("coin")


INTEREST_8H = 0.0001       # defaults; run() passes the config.yaml values
REST_TOL = 1e-10


def _mode(s: pd.Series) -> float:
    """Most frequent exact value (rounded to 1e-12 to absorb float division noise)."""
    return float(s.dropna().round(12).value_counts().idxmax())


def _at_rest(f_hourly: pd.Series, interest_8h: float, tol: float) -> pd.Series:
    """True where an hourly-equivalent rate sits at the documented baseline.

    For Binance this is the same test as comparing each settlement's per-interval rate r with
    interest x I / 8 (0.01% for 8h, 0.005% for 4h, 0.00125% for 1h symbols) within tol x I,
    because the panel's hourly equivalent is r / I.
    """
    return (f_hourly - interest_8h / 8).abs() <= tol


def resting_rates(panel: pd.DataFrame, interest_8h: float = INTEREST_8H,
                  tol: float = REST_TOL) -> pd.DataFrame:
    """Per coin and venue: the documented resting rate and how often funding sits at it.

    The resting rate is the documented interest baseline, 0.01% per 8h on both venues (hourly
    equivalent 0.0000125). As a sanity check the most frequent observed hourly rate is reported
    too; `modal_differs_*` flags a coin whose modal rate is not the documented baseline.
    """
    rest_h = interest_8h / 8
    rows = []
    for coin, d in panel.groupby("coin", observed=True):
        hl, bn = d["f_hl_realised"].dropna(), d["f_bn_realised"].dropna()
        hl_mode, bn_mode = _mode(hl), _mode(bn)
        rows.append({"coin": coin, "hl_rest_per_hour": rest_h, "bn_rest_per_hour": rest_h,
                     "hl_share_at_rest": _at_rest(hl, interest_8h, tol).mean(),
                     "bn_share_at_rest": _at_rest(bn, interest_8h, tol).mean(),
                     "mechanical_gap_apr": 0.0,
                     "hl_modal_per_hour": hl_mode, "bn_modal_per_hour": bn_mode,
                     "modal_differs_hl": abs(hl_mode - rest_h) > tol,
                     "modal_differs_bn": abs(bn_mode - rest_h) > tol})
    return pd.DataFrame(rows).set_index("coin")


def venue_funding(panel: pd.DataFrame) -> pd.DataFrame:
    """Per coin: median and mean realised funding APR on each venue, and how they compare.

    `median_daily_ratio` is the median of HL/BN daily-mean ratios on days when both are positive
    (the measure behind the BTC 1.87 in report/data_quality.md). `ratio_of_means` is not
    meaningful where the Binance mean is near zero or negative.
    """
    rows = []
    for coin, d in panel.groupby("coin", observed=True):
        hl, bn = d["f_hl_realised"].dropna(), d["f_bn_realised"].dropna()
        f8 = factor_of_eight(d.assign(coin=str(coin)), str(coin))
        rows.append({"coin": coin,
                     "hl_median_apr": hl.median() * APR, "hl_mean_apr": hl.mean() * APR,
                     "bn_median_apr": bn.median() * APR, "bn_mean_apr": bn.mean() * APR,
                     "mean_diff_apr": (hl.mean() - bn.mean()) * APR,
                     "ratio_of_means": hl.mean() / bn.mean() if bn.mean() else np.nan,
                     "median_daily_ratio": f8["median_ratio"],
                     "days_both_positive": f8["days_both_positive"]})
    return pd.DataFrame(rows).set_index("coin")


def structural_baseline(panel: pd.DataFrame, window_days: int = 30,
                        interest_8h: float = INTEREST_8H, tol: float = REST_TOL) -> pd.DataFrame:
    """Separate the constant and slow-moving parts of the gap from its fluctuations.

    mechanical_gap_apr  gap when both venues rest at the documented interest baseline (0 here,
                        since the baseline is the same on both venues)
    slow  = trailing `window_days` mean of the hourly gap (uses only past hours)
    fast  = gap - slow
    Reported per coin, in APR: the full-period mean gap, the mechanical gap, the std of `slow`
    and of `fast`, the share of gap variance that `slow` accounts for, the share of calendar
    months whose mean gap is positive, and the share of hours when both venues' known rates sit at
    the documented baseline (where the gap equals the mechanical gap).
    """
    rest = resting_rates(panel, interest_8h, tol)
    w = window_days * 24
    rows = []
    for coin, d in panel.groupby("coin", observed=True):
        d = d.sort_values("hour")
        g = d["gap"] * APR
        slow = g.rolling(w, min_periods=w).mean()
        fast = g - slow
        ok = slow.notna() & g.notna()
        month = pd.to_datetime(d["hour"], unit="ms", utc=True).dt.strftime("%Y-%m")
        monthly = g.groupby(month).mean()
        r = rest.loc[coin]
        both = _at_rest(d["f_hl_known"], interest_8h, tol) & _at_rest(d["f_bn_known"], interest_8h, tol)
        rows.append({"coin": coin, "mean_gap_apr": g.mean(),
                     "mechanical_gap_apr": r["mechanical_gap_apr"],
                     "mean_gap_ex_mechanical_apr": g.mean() - r["mechanical_gap_apr"],
                     "std_slow_apr": slow[ok].std(), "std_fast_apr": fast[ok].std(),
                     "slow_var_share": slow[ok].var() / g[ok].var() if g[ok].var() > 0 else np.nan,
                     "months": len(monthly), "share_months_positive": (monthly > 0).mean(),
                     "share_hours_both_at_rest": both.mean()})
    return pd.DataFrame(rows).set_index("coin")


# --- Charts --------------------------------------------------------------------------------

def _style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=1, linestyle="-")
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)


def plot_gap_over_time(panel: pd.DataFrame, coins: list[str], path: Path | None = None):
    """Daily mean gap (APR) per coin as small multiples on one shared y-scale."""
    fig, axes = plt.subplots(len(coins), 1, figsize=(10, 2.6 * len(coins)), sharex=True,
                             sharey=True, facecolor=SURFACE)
    axes = np.atleast_1d(axes)
    for ax, coin in zip(axes, coins):
        d = panel[panel["coin"] == coin]
        day = pd.to_datetime(d["hour"] // (24 * HOUR) * 24 * HOUR, unit="ms")
        daily = (d["gap"] * APR).groupby(day).mean()
        _style(ax)
        ax.axhline(0, color=INK_2, linewidth=1)
        ax.plot(daily.index, daily.to_numpy() * 100, color=SERIES, linewidth=1.5,
                solid_joinstyle="round", solid_capstyle="round")
        ax.set_title(f"{coin}: daily mean funding gap (Hyperliquid − Binance)", loc="left",
                     fontsize=11, color=INK)
        ax.set_ylabel("% APR", color=INK_2, fontsize=9)
    fig.text(0.01, 0.005, "Known-at-t hourly gap averaged per UTC day; design period "
             "2024-08-01 to 2026-06-30. Positive = Hyperliquid longs pay more.",
             color=INK_2, fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    if path:
        fig.savefig(path, dpi=150, facecolor=SURFACE)
    return fig


def plot_gap_histogram(panel: pd.DataFrame, path: Path | None = None, clip: float = 1.0,
                       bin_width: float = 0.025):
    """Pooled histogram of the hourly gap in APR, all coins; values beyond ±clip are counted
    in the edge bins and noted in the caption."""
    g = (panel["gap"].dropna() * APR).to_numpy()
    over = int((np.abs(g) > clip).sum())
    # Bins centred on whole multiples of bin_width, so a gap of exactly 0 sits in the 0 bin.
    k = int(round(clip / bin_width))
    bins = (np.arange(-k, k + 2) - 0.5) * bin_width
    fig, ax = plt.subplots(figsize=(10, 4), facecolor=SURFACE)
    _style(ax)
    ax.hist(np.clip(g, -clip, clip) * 100, bins=bins * 100, color=SERIES, rwidth=0.85)
    ax.set_yscale("log")
    ax.tick_params(which="minor", length=0)
    ax.set_xlabel("Hourly funding gap, % APR (Hyperliquid − Binance)", color=INK_2, fontsize=9)
    ax.set_ylabel("Coin-hours (log scale)", color=INK_2, fontsize=9)
    ax.set_title(f"Funding gap, all {panel['coin'].nunique()} coins pooled", loc="left",
                 fontsize=11, color=INK)
    fig.text(0.01, 0.01, f"{len(g):,} coin-hours, {bin_width:.1%} APR bins. {over:,} hours "
             f"beyond ±{clip:.0%} APR are counted in the edge bins.", color=INK_2, fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    if path:
        fig.savefig(path, dpi=150, facecolor=SURFACE)
    return fig


def run(cfg: dict | None = None) -> dict:
    """Compute every table and chart, save them under report/, and return them."""
    cfg = cfg or load_config()
    a = cfg["analysis"]
    panel = load_design_panel(cfg)
    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    out = {"gap_summary": gap_summary(panel, a["gap_thresholds_apr"]),
           "venue_funding": venue_funding(panel),
           "resting_rates": resting_rates(panel, a["interest_rate_per_8h"],
                                          a["resting_tolerance_per_hour"]),
           "structural_baseline": structural_baseline(panel, a["baseline_window_days"],
                                                      a["interest_rate_per_8h"],
                                                      a["resting_tolerance_per_hour"])}
    for name, df in out.items():
        df.to_csv(TABLES / f"eda_{name}.csv", float_format="%.6g")
    out["fig_gap_over_time"] = plot_gap_over_time(panel, ["BTC", a["mid_cap_example"]],
                                                  FIGURES / "eda_gap_over_time.png")
    out["fig_gap_histogram"] = plot_gap_histogram(panel, FIGURES / "eda_gap_histogram.png")
    return out


if __name__ == "__main__":
    res = run()
    for k in ("gap_summary", "venue_funding", "resting_rates", "structural_baseline"):
        print(f"\n== {k}\n{res[k].round(4).to_string()}")
