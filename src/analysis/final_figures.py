"""Final report figures (Phase 6), one consistent style, written to report/figures/final/.

Every number a figure draws or prints is read from a CSV in report/tables/ (the results files):
nothing is recomputed from market data and nothing is typed in by hand. Plotting arithmetic is
limited to the cumulative positions of a waterfall's own components (which must add up to the
table's total), equity minus the table's capital, and the Kaplan-Meier curve of the episode
durations in persistence_episodes.csv (the estimator behind persistence_carry_breakeven.csv).

    python -m src.analysis.final_figures          # all eight figures
    python -m src.analysis.final_figures 3 8      # selected figures
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from src.analysis.half_life import kaplan_meier  # noqa: E402
from src.config import ROOT  # noqa: E402

TABLES = ROOT / "report" / "tables"
OUT = ROOT / "report" / "figures" / "final"

# Tokens: the light theme used by every earlier figure; categorical slots 1-4 in fixed order
# (validated: lightness, chroma, CVD and normal-vision separation pass; slots 3-4 are below 3:1
# contrast, so they always appear with a legend). Gains and losses use the diverging pair.
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
BLUE, ORANGE, GREEN, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
GAIN, LOSS = "#2a78d6", "#e34948"
WIDTH, DPI = 9.0, 200
HALF = WIDTH / 2          # figures 1 and 2 sit side by side in the report
RC = {"font.family": "DejaVu Sans", "font.size": 8.5, "axes.labelsize": 8.5,
      "axes.labelcolor": INK_2, "xtick.labelsize": 8, "ytick.labelsize": 8,
      "legend.fontsize": 8, "figure.facecolor": SURFACE, "savefig.facecolor": SURFACE,
      "text.parse_math": False}

LEGEND_BOX = {"frameon": True, "facecolor": SURFACE, "edgecolor": "none", "framealpha": 0.9}

COMPONENTS = [("hl_funding", "Hyperliquid funding"), ("bn_funding", "Binance funding"),
              ("price_spread", "Price spread"), ("fees", "Fees"), ("slippage", "Slippage"),
              ("transfer_fees", "Transfer fees"), ("liquidation", "Liquidation penalties"),
              ("price_unrealised", "Open positions (unrealised)")]
DAY_MS = 86_400_000


# --- Helpers -----------------------------------------------------------------------------------

def table(name: str, **kw) -> pd.DataFrame:
    """A results file from report/tables/; the only data source of this module."""
    return pd.read_csv(TABLES / f"{name}.csv", **kw)


def minus(s: str) -> str:
    return s.replace("-", "−")


def usd(v: float, sign: bool = True) -> str:
    return minus(f"{v:+,.0f}" if sign else f"{v:,.0f}")


def money(v: float) -> str:
    """Signed dollars for running text: -4270.4 -> '−$4,270'."""
    return minus(f"{'-' if v < 0 else '+'}${abs(v):,.0f}")


def to_time(ms) -> pd.DatetimeIndex:
    return pd.to_datetime(np.asarray(ms, dtype="int64"), unit="ms")


def period_span(daily: pd.DataFrame) -> tuple[str, str, int]:
    """First and last UTC day covered by a daily table (rows stamped at each day's end)."""
    d = to_time(daily["day_end"])
    return ((d[0] - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            (d[-1] - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), len(daily))


def cumulative(daily: pd.DataFrame, capital: float) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """Cumulative P&L (equity minus capital), starting at 0 at the start of the first day."""
    ms = np.r_[daily["day_end"].iloc[0] - DAY_MS, daily["day_end"].to_numpy()]
    return to_time(ms), np.r_[0.0, daily["equity"].to_numpy() - capital]


def short_days(days: str) -> str:
    """'2025-10-10, 2025-10-11' -> '2025-10-10/11' (same month), else 'a and b'."""
    d = [x.strip() for x in days.split(",")]
    if len(d) == 2 and d[0][:8] == d[1][:8]:
        return f"{d[0]}/{d[1][8:]}"
    return " and ".join(d)


def waterfall_steps(row: pd.Series) -> pd.DataFrame:
    """Floating bars from 0 through each component to the net total; the components must add up
    to the table's total."""
    rows, cum = [], 0.0
    for key, label in COMPONENTS:
        v = float(row[key])
        rows.append({"label": label, "value": v, "start": cum, "end": cum + v,
                     "kind": "gain" if v >= 0 else "loss"})
        cum += v
    if abs(cum - float(row["total"])) > 1e-6:
        raise ValueError(f"components sum to {cum}, table total is {row['total']}")
    rows.append({"label": "Net P&L", "value": float(row["total"]), "start": 0.0,
                 "end": float(row["total"]), "kind": "total"})
    return pd.DataFrame(rows)


def style(ax, grid: str = "y") -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis=grid, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, length=0)


def dollars(ax, axis: str = "y") -> None:
    f = FuncFormatter(lambda v, _: minus(f"{v:,.0f}"))
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(f)


def zero_line(ax, axis: str = "y") -> None:
    (ax.axhline if axis == "y" else ax.axvline)(0, color=INK_2, linewidth=0.9, zorder=1)


def panel_title(ax, text: str) -> None:
    ax.set_title(text, loc="left", fontsize=9.5, color=INK, pad=6)


def finish(fig, name: str, title: str, subtitle: str | None, note: str | None,
           sources: list[str], legend: tuple | None = None) -> Path:
    """Shared frame: bold title and grey subtitle top left, optional figure legend row, caption
    and source files bottom left. Saves report/figures/final/<name>.png."""
    h = fig.get_figheight()
    n = int(round(150 * fig.get_figwidth() / WIDTH))   # caption characters per line
    cap = textwrap.wrap(note, n) if note else []
    cap += textwrap.wrap("Source: " + ", ".join(f"report/tables/{s}.csv" for s in sources), n,
                         subsequent_indent="    ")
    sub = subtitle.split("\n") if subtitle else []
    top_in = 0.44 + 0.2 * len(sub)
    bottom_in = 0.14 + 0.15 * len(cap) + (0.34 if legend else 0.0)
    fig.tight_layout(rect=(0, bottom_in / h, 1, 1 - top_in / h))
    fig.text(0.012, 1 - 0.14 / h, title, fontsize=12, color=INK, weight="bold", va="top")
    for i, line in enumerate(sub):
        fig.text(0.012, 1 - (0.40 + 0.2 * i) / h, line, fontsize=9, color=INK_2, va="top")
    if legend:
        fig.legend(*legend, loc="lower left", bbox_to_anchor=(0.006, (0.12 + 0.15 * len(cap)) / h),
                   ncol=len(legend[0]), frameon=False, labelcolor=INK, handlelength=1.8)
    for i, line in enumerate(cap):
        fig.text(0.012, (0.07 + 0.15 * (len(cap) - 1 - i)) / h, line, fontsize=7.5, color=INK_2)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def date_axis(ax, months=(1, 4, 7, 10)) -> None:
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=list(months)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))


# --- 1. Gap over time ----------------------------------------------------------------------------

def fig1_gap_over_time(coins=("BTC", "WLD")) -> Path:
    g = table("persistence_daily_gap", index_col="day")
    summ = table("eda_gap_summary", index_col="coin")
    x = to_time(g.index)
    first, last = x[0].strftime("%Y-%m-%d"), x[-1].strftime("%Y-%m-%d")
    fig, axes = plt.subplots(len(coins), 1, figsize=(HALF, 6.4), sharex=True, sharey=True)
    for ax, coin in zip(axes, coins):
        style(ax)
        zero_line(ax)
        ax.plot(x, g[coin] * 100, color=BLUE, linewidth=1.1, solid_joinstyle="round")
        m = summ.loc[coin, "mean"] * 100
        ax.axhline(m, color=INK, linewidth=1, linestyle=(0, (4, 3)))
        panel_title(ax, minus(f"{coin}: mean hourly gap {m:+.1f}% APR (dashed)"))
        ax.set_ylabel("Daily mean gap, % APR")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: minus(f"{v:.0f}")))
    date_axis(axes[-1])
    axes[-1].margins(x=0.005)
    return finish(fig, "01_gap_over_time",
                  "Funding gap over time: BTC and WLD",
                  "Hyperliquid minus Binance funding, % APR, per UTC day;\n"
                  f"design period {first} to {last}",
                  "Known-at-t hourly gap (a funding row counts only from one hour after its stamp). "
                  "Positive: Hyperliquid funding is higher, so short Hyperliquid / long Binance "
                  "collects it. Dashed: mean hourly gap over the period. WLD is the mid-cap example "
                  "fixed before any statistic was computed. Same scale in both panels.",
                  ["persistence_daily_gap", "eda_gap_summary"])


# --- 2. Half-lives -------------------------------------------------------------------------------

def fig2_half_lives() -> Path:
    hl = table("persistence_half_lives", index_col="coin").sort_values("half_life_days",
                                                                       ascending=False)
    y = np.arange(len(hl))
    fig, ax = plt.subplots(figsize=(HALF, 6.4))
    style(ax, grid="x")
    ax.hlines(y, hl["hl_lo_hac"], hl["hl_hi_hac"], color=BLUE, linewidth=2.2,
              capstyle="round")
    ax.plot(hl["half_life_days"], y, "o", color=BLUE, markersize=7.5, markeredgecolor=SURFACE,
            markeredgewidth=1.6, zorder=3)
    for yi, (_, r) in zip(y, hl.iterrows()):
        ax.text(r["hl_hi_hac"] + 0.06, yi, f"{r['half_life_days']:.1f}", va="center",
                fontsize=8, color=INK_2)
    ax.set_yticks(y, hl.index, color=INK, fontsize=8.5)
    ax.set_xlim(0, hl["hl_hi_hac"].max() + 0.4)
    ax.set_xlabel("Half-life of the daily mean gap, days")
    n_days = int(hl["n_days"].iloc[0])
    return finish(fig, "02_half_lives",
                  "Half-life of the funding gap by coin",
                  "AR(1) on daily mean gaps: estimate (dot, value at right)\n"
                  "with 95% Newey-West interval",
                  f"Design period, {n_days} daily observations per coin. Intervals use Newey-West "
                  "(HAC) standard errors on the AR(1) coefficient, mapped to half-lives; OLS "
                  "intervals (se in the source table) are narrower than these (se_hac). "
                  + (f"The ADF test rejects a unit root for every coin (largest p-value "
                     f"{hl['adf_pvalue'].max():.1g})." if (hl["adf_pvalue"] < 0.05).all() else
                     "See adf_pvalue in the source table for the unit-root test."),
                  ["persistence_half_lives"])


# --- 3. Episode durations against break-even carry ---------------------------------------------

def _group_name(coins: list[str]) -> str:
    return ", ".join(coins) if len(coins) <= 3 else f"other {len(coins)} coins"


def fig3_episodes_vs_breakeven() -> Path:
    eps = table("persistence_episodes")
    be = table("persistence_carry_breakeven")
    be["coin_list"] = be["coins"].str.split(", ")
    thresholds = sorted(be["threshold_apr"].unique())
    costs = sorted(be["cost_bps"].unique())
    colors = dict(zip(costs, [BLUE, ORANGE]))
    xmax = 10 ** np.ceil(np.log10(be["breakeven_h_at_threshold"].max() * 1.3))
    fig, axes = plt.subplots(1, len(thresholds), figsize=(WIDTH, 4.4), sharey=True)
    for ax, t in zip(axes, thresholds):
        style(ax)
        ax.set_xscale("log")
        rows = be[be["threshold_apr"] == t].sort_values("cost_bps")
        medians = []
        for k, (_, r) in enumerate(rows.iterrows()):
            c = colors[r["cost_bps"]]
            d = eps[(eps["threshold"] == t) & eps["coin"].isin(r["coin_list"])]
            km = kaplan_meier(d["duration_h"].to_numpy(), d["censored"].to_numpy())
            xs = np.r_[1.0, km["t"].to_numpy()[1:]]
            ys = np.r_[1.0, km["survival"].to_numpy()[1:]]
            ax.step(xs, ys * 100, where="post", color=c, linewidth=1.8)
            lo, hi = r["breakeven_h_at_mean_gap"], r["breakeven_h_at_threshold"]
            ax.axvspan(lo, hi, color=c, alpha=0.14, linewidth=0)
            ax.axvline(lo, color=c, linewidth=1.1, linestyle=(0, (3, 2)))
            short = ", ".join(r["coin_list"]) if len(r["coin_list"]) <= 3 else "others"
            ax.text(np.sqrt(lo * hi), 95 - 10 * k, f"{short}: {lo:.0f}–{hi:.0f} h",
                    ha="center", va="center", fontsize=7.5, color=INK,
                    bbox={"boxstyle": "round,pad=0.2", "fc": SURFACE, "ec": "none", "alpha": 0.9})
            ax.plot(r["median_duration_h"], 50, "o", color=c, markersize=6.5,
                    markeredgecolor=SURFACE, markeredgewidth=1.4, zorder=4)
            medians.append(r["median_duration_h"])
        mtxt = (f"median {medians[0]:g} h" if len(set(medians)) == 1 else
                "medians " + ", ".join(f"{m:g} h" for m in medians))
        ax.annotate(mtxt, (max(medians), 50), xytext=(8, 4), textcoords="offset points",
                    fontsize=7.5, color=INK, va="bottom")
        ax.set_xlim(1, xmax)
        ax.set_ylim(0, 102)
        panel_title(ax, f"|gap| above {t:.0%} APR")
        ax.set_xlabel("Hours since the episode began (log)")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    axes[0].set_ylabel("Episodes still open, %")
    first = be[be["threshold_apr"] == thresholds[0]].sort_values("cost_bps")
    handles = [Line2D([], [], color=colors[r["cost_bps"]], linewidth=1.8) for _, r in first.iterrows()]
    labels = [f"{_group_name(r['coin_list'])}: {r['cost_bps']:g} bps round trip"
              for _, r in first.iterrows()]
    handles.append(Patch(facecolor=INK_2, alpha=0.2, edgecolor="none"))
    labels.append("hours of carry to pay the round trip")
    shares = be["share_lasting_past_breakeven"]
    share_txt = ("In every panel and cost group, 0% of episodes last past the break-even at the "
                 "threshold." if (shares == 0).all() else
                 "Share of episodes lasting past the break-even at the threshold: "
                 + ", ".join(f"{s:.1%}" for s in shares) + ".")
    return finish(fig, "03_episodes_vs_breakeven",
                  "Gap episodes end long before carry pays for the trade",
                  "Share of episodes still open after h hours (Kaplan-Meier) against the hours of "
                  "carry needed to pay one round trip",
                  "An episode starts when |gap| crosses the threshold and ends when it falls below "
                  "half of it or changes sign. Break-even hours = round-trip cost / (|gap| / 8760), "
                  "on one leg's notional, carry only; each band runs from the break-even at the "
                  "mean episode |gap| (dashed, left edge) to the break-even at the threshold (right "
                  "edge). Dots: median duration. " + share_txt + " Design period.",
                  ["persistence_episodes", "persistence_carry_breakeven"],
                  legend=(handles, labels))


# --- 4. P&L waterfall ----------------------------------------------------------------------------

def _waterfall(ax, steps: pd.DataFrame) -> None:
    y = np.arange(len(steps))[::-1]
    span = max(steps[["start", "end"]].abs().max())
    color = {"gain": GAIN, "loss": LOSS, "total": INK}
    for yi, (_, s) in zip(y, steps.iterrows()):
        left, width = min(s["start"], s["end"]), abs(s["end"] - s["start"])
        ax.barh(yi, max(width, span * 0.002), left=left, height=0.62, color=color[s["kind"]],
                edgecolor=SURFACE, linewidth=1)
        x_text = max(s["start"], s["end"]) + 0.015 * span if s["value"] >= 0 else \
            min(s["start"], s["end"]) - 0.015 * span
        ax.text(x_text, yi, usd(s["value"]), va="center", fontsize=8, color=INK,
                ha="left" if s["value"] >= 0 else "right",
                weight="bold" if s["kind"] == "total" else "normal")
    for (yi, (_, a)), y_next in zip(zip(y[:-1], steps.iloc[:-1].iterrows()), y[1:]):
        ax.plot([a["end"]] * 2, [yi - 0.31, y_next + 0.31], color=INK_2, linewidth=0.7)
    zero_line(ax, axis="x")
    lo = min(steps[["start", "end"]].min().min(), 0)
    hi = max(steps[["start", "end"]].max().max(), 0)
    pad = 0.22 * (hi - lo)
    ax.set_xlim(lo - pad, hi + pad)
    ax.set_yticks(y, steps["label"], color=INK, fontsize=8.5)
    dollars(ax, "x")
    ax.set_xlabel("$")


def fig4_waterfall() -> Path:
    runs = {"Design": ("backtest_corrected_decomposition_benchmarks", "backtest_corrected_daily"),
            "Held-out": ("holdout_backtest_corrected_decomposition_benchmarks",
                         "holdout_backtest_corrected_daily")}
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 4.6), sharey=True)
    for ax, (name, (dec, daily)) in zip(axes, runs.items()):
        style(ax, grid="x")
        _waterfall(ax, waterfall_steps(table(dec, index_col=0)["strategy"]))
        s, e, n = period_span(table(daily))
        panel_title(ax, f"{name}: {s} to {e} ({n} days)")
    return finish(fig, "04_pnl_waterfall",
                  "From gross funding to net P&L: corrected baseline strategy",
                  "Each bar moves the running total by one P&L component; the last bar is net P&L",
                  "The first two bars are the funding booked on each venue's legs over all trades "
                  "(gross funding, before price moves and costs). Price spread: both legs at "
                  "unslipped fill prices. Components add up exactly to net P&L. Each panel has its own dollar "
                  "scale. Held-out run once, on the frozen version.",
                  [v[0] for v in runs.values()] + [v[1] for v in runs.values()])


# --- 5. Cumulative net P&L -----------------------------------------------------------------------

def fig5_cumulative() -> Path:
    runs = {"Design": ("backtest_corrected_daily", "backtest_corrected_summary"),
            "Held-out": ("holdout_backtest_corrected_daily", "holdout_backtest_corrected_summary")}
    data = {k: (table(d), table(s, index_col=0)["strategy"]) for k, (d, s) in runs.items()}
    capital = {k: s["capital"] for k, (_, s) in data.items()}
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 4.2), sharey=True,
                             gridspec_kw={"width_ratios": [len(d) for d, _ in data.values()]})
    stats = []
    for ax, (name, (daily, summ)) in zip(axes, data.items()):
        style(ax)
        zero_line(ax)
        x, y = cumulative(daily, summ["capital"])
        if abs(y[-1] - summ["total_pnl"]) > 0.01:
            raise ValueError(f"{name}: final equity does not match total_pnl")
        ax.plot(x, y, color=BLUE, linewidth=1.6, solid_joinstyle="round")
        ax.plot(x[-1], y[-1], "o", color=BLUE, markersize=6, markeredgecolor=SURFACE,
                markeredgewidth=1.5)
        ax.annotate(usd(summ["total_pnl"]), (x[-1], y[-1]), xytext=(0, -12),
                    textcoords="offset points", ha="right", va="top", fontsize=8, color=INK)
        panel_title(ax, name)
        ax.margins(x=0.01)
        s, e, _ = period_span(daily)
        stats.append(f"{name} {s} to {e}: {money(summ['total_pnl'])}, "
                     + minus(f"{summ['ann_return_total_capital']:+.1%}/yr, "
                             f"Sharpe {summ['sharpe']:+.2f}"))
    date_axis(axes[0])
    date_axis(axes[1], months=(8, 10))
    dollars(axes[0])
    axes[0].set_ylabel("Cumulative net P&L, $")
    if len(set(capital.values())) != 1:
        raise ValueError("capital differs between periods")
    return finish(fig, "05_cumulative_pnl",
                  "Cumulative net P&L, design and held-out periods",
                  "\n".join(stats),
                  f"Corrected baseline strategy on ${list(capital.values())[0]:,.0f} total capital; "
                  "end-of-day mark to market, all fees, slippage, transfer fees and liquidation "
                  "penalties included. Both panels use the same time and dollar scales. The "
                  "held-out period was run once, on the frozen version, after the design work.",
                  [v for pair in runs.values() for v in pair])


# --- 6. Cost sensitivity -------------------------------------------------------------------------

def fig6_cost_sensitivity() -> Path:
    cj = table("robustness_cost_joint")
    be = float(table("robustness_breakeven", index_col=0).loc["breakeven_cost_multiple"].iloc[0])
    fig, ax = plt.subplots(figsize=(WIDTH, 4.3))
    style(ax)
    zero_line(ax)
    ax.plot(cj["cost_multiple"], cj["total_pnl"], color=BLUE, linewidth=1.8, marker="o",
            markersize=7.5, markeredgecolor=SURFACE, markeredgewidth=1.6, zorder=3)
    for _, r in cj.iterrows():
        up = r["total_pnl"] >= 0
        left = r["cost_multiple"] < be
        ax.annotate(usd(r["total_pnl"]), (r["cost_multiple"], r["total_pnl"]),
                    xytext=(-8 if left else 8, 6 if up else -6), textcoords="offset points",
                    ha="right" if left else "left", va="bottom" if up else "top", fontsize=8,
                    color=INK)
    ax.axvline(be, color=INK_2, linewidth=1, linestyle=":")
    ax.plot(be, 0, "D", color=INK, markersize=6, zorder=4)
    ax.annotate(f"break-even {be:.2f}x", (be, 0), xytext=(-8, -10), textcoords="offset points",
                ha="right", va="top", fontsize=8.5, color=INK, weight="bold")
    ticks = cj["cost_multiple"].tolist()
    ax.set_xticks(ticks, [f"{m:g}x" + (" (baseline)" if m == 1 else "") for m in ticks])
    ax.set_xlim(0, max(ticks) + 0.3)
    dollars(ax)
    ax.set_xlabel("Fees and slippage, multiple of the assumed rates")
    ax.set_ylabel("Net P&L, design period, $")
    return finish(fig, "06_cost_sensitivity",
                  "Net P&L against trading costs",
                  "Design period, corrected baseline; fees and slippage scaled together",
                  "Transfer fees and liquidation penalties are not scaled. The break-even multiple "
                  "is found by bisection on the engine (net P&L = 0), not read off the line; "
                  "lines between points are for reading only.",
                  ["robustness_cost_joint", "robustness_breakeven"])


# --- 7. Failure case: TAO -------------------------------------------------------------------------

def fig7_failure_case(coin: str = "TAO") -> Path:
    worst = table("failure_cases_worst_trades")
    wt = worst[worst["coin"] == coin]
    if len(wt) != 1:
        raise ValueError(f"expected one {coin} failure case, found {len(wt)}")
    w = wt.iloc[0]
    paths = table("failure_cases_paths")
    p = paths[(paths["rank"] == w["rank"]) & (paths["coin"] == coin)].copy()
    p["time"] = pd.to_datetime(p["time"])
    trades = table("backtest_corrected_trades")
    entry_ms = pd.Timestamp(w["entry"], tz="UTC").value // 10**6
    tr = trades[(trades["coin"] == coin) & (trades["entry_hour"] == entry_ms)]
    if len(tr) != 1:
        raise ValueError("trade not found in the ledger")
    tr = tr.iloc[0]
    entry, exit_ = to_time([tr["entry_hour"]])[0], to_time([tr["exit_hour"]])[0]
    liq_day = to_time([tr["exit_hour"] - 1])[0].strftime("%Y-%m-%d")
    s = int(tr["side"])
    fig, axes = plt.subplots(4, 1, figsize=(WIDTH, 10), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1.3, 1.8, 1.8]})
    for ax in axes:
        style(ax)
        ax.axvspan(entry, exit_, color=INK_2, alpha=0.07, linewidth=0)
        for tt in (entry, exit_):
            ax.axvline(tt, color=INK_2, linewidth=0.9, linestyle=":")
    ax = axes[0]
    hl = p[["time", "hl_close"]].dropna()
    hl_ext = "hl_high" if s > 0 else "hl_low"
    bn_adv, hl_adv = ("bn_mark_low", "bn_index_high") if s > 0 else ("bn_mark_high", "bn_index_low")
    word = ("high", "low") if s > 0 else ("low", "high")
    ext = p[["time", hl_ext]].dropna()
    ax.plot(p["time"], p[hl_adv], color=YELLOW, linewidth=1,
            label=f"Binance index {word[0]} (Hyperliquid leg's mark proxy)")
    ax.plot(p["time"], p[bn_adv], color=GREEN, linewidth=1,
            label=f"Binance mark {word[1]} (Binance leg's liquidation check)")
    ax.plot(p["time"], p["bn_close"], color=ORANGE, linewidth=1.3, label="Binance close (1h)")
    ax.plot(ext["time"], ext[hl_ext], color=BLUE, linewidth=1, linestyle=(0, (4, 2)),
            label=f"Hyperliquid bar {word[0]} (4h)")
    ax.plot(hl["time"], hl["hl_close"], color=BLUE, linewidth=1.8, label="Hyperliquid close (4h)")
    for price, name, c, dy in ((tr["exit_hl"], "HL exit", BLUE, 8), (tr["exit_bn"], "BN exit",
                                                                    ORANGE, -8)):
        ax.plot(exit_, price, "o", color=c, markersize=6.5, markeredgecolor=SURFACE,
                markeredgewidth=1.5, zorder=5)
        ax.annotate(f"{name} {price:,.2f}", (exit_, price), xytext=(9, dy),
                    textcoords="offset points", fontsize=8, color=INK, va="center",
                    bbox={"boxstyle": "round,pad=0.2", "fc": SURFACE, "ec": "none", "alpha": 0.9})
    ax.set_ylabel("Price, $")
    ax.legend(frameon=False, loc="lower left", labelcolor=INK)
    panel_title(ax, "Prices and each leg's adverse extreme")
    ax = axes[1]
    sp = p[["time", "spread_bps"]].dropna()
    zero_line(ax)
    ax.plot(sp["time"], sp["spread_bps"], color=BLUE, linewidth=1.4)
    ax.set_ylabel("bps")
    panel_title(ax, "Price spread, Hyperliquid vs Binance close")
    ax = axes[2]
    zero_line(ax)
    ax.plot(p["time"], p["f_hl_apr"] * 100, color=BLUE, linewidth=0.9, label="Hyperliquid funding")
    ax.plot(p["time"], p["f_bn_apr"] * 100, color=ORANGE, linewidth=1.3,
            label="Binance funding (hourly equivalent)")
    ax.plot(p["time"], p["signal_apr"] * 100, color=GREEN, linewidth=2,
            label="Signal (7-day mean gap)")
    ax.set_ylabel("% APR")
    # Hourly funding spikes on 10-11 Oct would flatten the panel: clip the axis and print the
    # off-scale extremes from the path table.
    ylo, yhi = -60, 60
    ax.set_ylim(ylo, yhi)
    off = []
    for col, name in (("f_hl_apr", "Hyperliquid"), ("f_bn_apr", "Binance")):
        v = p[col] * 100
        if v.min() < ylo:
            off.append(minus(f"{name} min {v.min():,.0f}% at {p.loc[v.idxmin(), 'time']:%d %b %H:%M}"))
        if v.max() > yhi:
            off.append(minus(f"{name} max {v.max():,.0f}% at {p.loc[v.idxmax(), 'time']:%d %b %H:%M}"))
    if off:
        ax.text(0.99, 0.04, "Off scale: " + "; ".join(off), transform=ax.transAxes, ha="right",
                va="bottom", fontsize=7.5, color=INK,
                bbox={"boxstyle": "round,pad=0.25", "fc": SURFACE, "ec": "none", "alpha": 0.9})
    ax.legend(loc="upper left", labelcolor=INK, ncol=3, **LEGEND_BOX)
    panel_title(ax, "Funding rates and the entry signal (axis clipped)")
    ax = axes[3]
    zero_line(ax)
    held = p[p["held"]]
    ax.plot(held["time"], held["hl_funding_cum"] + held["bn_funding_cum"], color=GREEN,
            linewidth=1.8, label="Funding, both legs")
    mtm = held[["time", "price_mtm"]].dropna()
    ax.plot(mtm["time"], mtm["price_mtm"], color=BLUE, linewidth=1.8,
            label="Price, both legs at closes (before costs)")
    ax.plot(exit_, tr["total"], "o", color=INK, markersize=6, zorder=5)
    ax.annotate(f"trade total {usd(tr['total'])}", (exit_, tr["total"]), xytext=(7, 0),
                textcoords="offset points", fontsize=8, color=INK, va="center", weight="bold")
    dollars(ax)
    ax.set_ylabel("$")
    ax.legend(frameon=False, loc="lower left", labelcolor=INK)
    panel_title(ax, "Running trade P&L")
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%Y"))
    ax.margins(x=0.01)
    side = w["position"].replace("HL", "Hyperliquid").replace("BN", "Binance")
    leg = str(w["liquidated_leg"]).capitalize()
    return finish(fig, f"07_failure_case_{coin}",
                  f"Failure case: {coin}, {side}, {leg} leg liquidated on {liq_day}",
                  f"Entry {entry:%Y-%m-%d %H:%M}, exit {exit_:%Y-%m-%d %H:%M} UTC "
                  f"({w['hold_days']:.1f} days). Total {money(w['total'])}, by component:\n"
                  f"price spread {money(w['price_spread'])}, liquidation penalty "
                  f"{money(w['liquidation'])}, slippage {money(w['slippage'])}, fees "
                  f"{money(w['fees'])}, transfer fees {money(w['transfer_fees'])}\n"
                  f"Hyperliquid funding {money(w['hl_funding'])}, Binance funding "
                  f"{money(w['bn_funding'])}",
                  "Shaded: position open; times UTC at the end of each hour. Liquidation is checked "
                  "on mark prices: the Binance leg on Binance's mark-price low, the Hyperliquid leg "
                  "on the Binance index high (proxy for Hyperliquid's oracle-based mark). The "
                  "surviving leg closes at the next bar open. Corrected baseline, design period; "
                  f"rank {int(w['rank'])} of the {len(worst)} worst trades.",
                  ["failure_cases_worst_trades", "failure_cases_paths", "backtest_corrected_trades"])


# --- 8. Leverage with and without the tail days (post hoc) -------------------------------------

def fig8_leverage_tail() -> Path:
    t = table("robustness_tail_event_post_hoc").sort_values("leverage_per_leg")
    label = t["label"].iloc[0]
    days = short_days(t["tail_days"].iloc[0])
    x = np.arange(len(t))
    w = 0.36
    fig, ax = plt.subplots(figsize=(WIDTH, 4.4))
    style(ax)
    zero_line(ax)
    series = [("total_pnl", "All days", BLUE, -1), ("pnl_excl_tail_days", f"Excluding {days}",
                                                     ORANGE, 1)]
    for col, name, c, side in series:
        xs = x + side * (w / 2 + 0.01)
        ax.bar(xs, t[col], w, color=c, label=name, edgecolor=SURFACE, linewidth=1)
        for xi, v in zip(xs, t[col]):
            ax.annotate(usd(v), (xi, v), xytext=(0, 3 if v >= 0 else -3),
                        textcoords="offset points", ha="center",
                        va="bottom" if v >= 0 else "top", fontsize=8, color=INK)
    ax.set_xticks(x, [f"{lev:g}x per leg" + (" (baseline)" if lev == 3 else "")
                      + f"\n${cap:,.0f} capital"
                      for lev, cap in zip(t["leverage_per_leg"], t["capital"])])
    lo, hi = min(t["total_pnl"].min(), t["pnl_excl_tail_days"].min(), 0), \
        max(t["total_pnl"].max(), t["pnl_excl_tail_days"].max(), 0)
    ax.set_ylim(lo - 0.12 * (hi - lo), hi + 0.12 * (hi - lo))
    dollars(ax)
    ax.set_ylabel("Net P&L, design period, $")
    ax.legend(frameon=False, loc="lower right", labelcolor=INK)
    return finish(fig, "08_leverage_tail_post_hoc",
                  f"Net P&L by leverage, with and without {days} (post hoc)",
                  f"{label[0].upper() + label[1:]}: not pre-registered and not used to choose any "
                  "parameter",
                  f"Excluding removes the daily P&L booked on {days} (UTC) only; it is not a rerun "
                  "without those days, so positions liquidated then stay closed. Capital includes the "
                  "same idle buffer at every leverage; the buffer changes only the capital base, not "
                  "P&L. Design period, corrected model.",
                  ["robustness_tail_event_post_hoc"])


FIGURES = {1: fig1_gap_over_time, 2: fig2_half_lives, 3: fig3_episodes_vs_breakeven,
           4: fig4_waterfall, 5: fig5_cumulative, 6: fig6_cost_sensitivity,
           7: fig7_failure_case, 8: fig8_leverage_tail}


def main(argv: list[str] | None = None) -> list[Path]:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("figures", nargs="*", type=int, choices=sorted(FIGURES),
                   help="figure numbers (default: all)")
    args = p.parse_args(argv)
    out = []
    with plt.rc_context(RC):
        for n in args.figures or sorted(FIGURES):
            out.append(FIGURES[n]())
            print(out[-1].relative_to(ROOT).as_posix())
    return out


if __name__ == "__main__":
    main()
