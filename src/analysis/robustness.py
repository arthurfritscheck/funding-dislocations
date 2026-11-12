"""Robustness checks on the corrected backtest, design period only (specified in
report/decisions.md, 2026-11-08). Every check is a config variant run through `src.backtest.engine`.
None of these results may be used to change the baseline parameters.

Checks: cost sensitivity and the breakeven cost multiple; a maker scenario (reported separately);
the entry/exit/window grid; majors versus the rest and leave-one-coin-out; net P&L by quarter, a
rolling Sharpe ratio and a block bootstrap of the Sharpe ratio. The maker scenario, leverage x
buffer and the tail-event table are sensitivities added after the first backtest run (labelled
`POST_HOC` in every table).

    python -m src.analysis.robustness      # writes report/tables/robustness_*.csv and figures
"""

from __future__ import annotations

import copy
import math
from typing import Callable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from src.analysis.gap_stats import FIGURES, INK, INK_2, SURFACE, _style  # noqa: E402
from src.backtest import costs, engine as E, metrics as M  # noqa: E402
from src.config import ROOT, load_config  # noqa: E402

TABLES = ROOT / "report" / "tables"
POST_HOC = "sensitivity added after the first backtest run"
MAKER_NOTE = ("maker fills are not guaranteed: an unfilled leg leaves the position unhedged "
              "(legging risk) and resting orders fill most often when the price moves against "
              "them (adverse selection)")
COLS = ["total_pnl", "capital", "ann_return_total_capital", "sharpe", "max_drawdown_usd",
        "trades", "hit_rate", "liquidations", "rebalances"]
POS, NEG, MID = "#2a78d6", "#e34948", "#f0efec"   # diverging pair of the reference palette
DIVERGING = LinearSegmentedColormap.from_list("gain_loss", [NEG, MID, POS])


# --- Config variants -------------------------------------------------------------------------

def baseline_cfg(cfg: dict | None = None) -> dict:
    """The corrected model (report/decisions.md, 2026-11-08) with the baseline strategy parameters."""
    return E.variant_cfg(cfg if cfg is not None else load_config(), "corrected")


def scale_costs(cfg: dict, fee_mult: float, slip_mult: float) -> dict:
    """Copy of cfg with every fee (maker and taker, both venues) and slippage rate scaled."""
    c = copy.deepcopy(cfg)
    for v in costs.VENUES:
        c["fees"][v] = {k: float(x) * fee_mult for k, x in c["fees"][v].items()}
    for k in ("majors_bps", "others_bps"):
        c["slippage"][k] = float(c["slippage"][k]) * slip_mult
    return c


def maker_cfg(cfg: dict) -> dict:
    """Both legs at maker fees and slippage scaled by robustness.maker_scenario."""
    m = cfg["robustness"]["maker_scenario"]
    c = scale_costs(cfg, 1.0, m["slippage_multiple"])
    c["execution"]["hyperliquid_leg"] = m["hyperliquid_leg"]
    c["execution"]["binance_leg"] = m["binance_leg"]
    return c


def subset_cfg(cfg: dict, coins: list[str]) -> dict:
    """Capital scaled to a coin subset: one slot per coin, so total capital per coin is
    unchanged and the concurrency cap never binds (one position per coin)."""
    c = copy.deepcopy(cfg)
    c["strategy"]["max_concurrent_positions"] = len(coins)
    return c


def leverage_buffer_cfg(cfg: dict, leverage: float, buffer: float) -> dict:
    c = copy.deepcopy(cfg)
    c["margin"]["leverage_per_leg"] = leverage
    c["margin"]["capital_buffer_frac"] = buffer
    return c


def row(r: E.Result, **labels) -> dict:
    """Labels plus the headline metrics of one run."""
    return {**labels, **M.summary(r)[COLS].to_dict()}


# --- 1. Cost sensitivity ---------------------------------------------------------------------

def breakeven_multiple(pnl_at: Callable[[float], float], known: dict[float, float],
                       tol: float) -> float:
    """Smallest cost multiple at which P&L crosses from positive to non-positive, by bisection.

    `known` maps multiples already run to their P&L; the first sign change between consecutive
    known multiples brackets the search. NaN if P&L never crosses zero among them."""
    pts = sorted(known.items())
    for (lo, f_lo), (hi, f_hi) in zip(pts, pts[1:]):
        if f_lo > 0 >= f_hi:
            break
    else:
        return math.nan
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if pnl_at(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def cost_sensitivity(cfg: dict, inputs: dict) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """(joint multiples table, fee x slippage grid, breakeven joint multiple)."""
    mults = cfg["robustness"]["cost_multiples"]
    runs = {}

    def pnl_at(m: float) -> float:
        if m not in runs:
            runs[m] = E.run(scale_costs(cfg, m, m), inputs)
        return float(runs[m].trades["total"].sum())

    joint = []
    for m in mults:
        pnl_at(m)
        dec = M.decomposition(runs[m].trades).loc["ALL"]
        joint.append({**row(runs[m], cost_multiple=m), "fees": dec["fees"],
                      "slippage": dec["slippage"]})
    joint = pd.DataFrame(joint).set_index("cost_multiple")
    grid = []
    for fm in mults:
        for sm in mults:
            r = runs[fm] if fm == sm else E.run(scale_costs(cfg, fm, sm), inputs)
            grid.append(row(r, fee_multiple=fm, slippage_multiple=sm))
    grid = pd.DataFrame(grid)
    pnl_at(0.0)
    be = breakeven_multiple(pnl_at, {m: pnl_at(m) for m in [0.0, *mults]},
                            cfg["robustness"]["breakeven_tol"])
    return joint, grid, be


# --- 2. Maker scenario -----------------------------------------------------------------------

def maker_scenario(cfg: dict, inputs: dict, base: E.Result | None = None) -> pd.DataFrame:
    """Baseline (taker on both legs) against both legs maker with reduced slippage: headline
    metrics and the P&L decomposition, one column each. The maker case is a sensitivity added
    after the first backtest run (row `label`)."""
    base = base or E.run(cfg, inputs)
    maker = E.run(maker_cfg(cfg), inputs)
    out = {}
    for name, r in (("taker (baseline)", base), ("maker, both legs", maker)):
        s = pd.Series(row(r))
        d = M.decomposition(r.trades).loc["ALL"].drop("total")
        out[name] = pd.concat([s, d])
    out = pd.DataFrame(out).astype(object)
    out.loc["label"] = ["baseline", POST_HOC]
    out.loc["round_trip_bps_majors"] = [costs.round_trip_cost_bps(c, "BTC")
                                       for c in (cfg, maker_cfg(cfg))]
    out.loc["round_trip_bps_others"] = [costs.round_trip_cost_bps(c, "SUI")
                                       for c in (cfg, maker_cfg(cfg))]
    return out


# --- 3. Parameter grid -----------------------------------------------------------------------

def parameter_grid(cfg: dict, inputs: dict) -> pd.DataFrame:
    """Every entry x exit x window combination in strategy.robustness (long format)."""
    g = cfg["strategy"]["robustness"]
    out = []
    for w in g["signal_window_days"]:
        inp = E.with_signal(inputs, w)
        for t_in in g["theta_in_apr"]:
            for t_out in g["theta_out_apr"]:
                r = E.run(cfg, inp, E.SignalPolicy(cfg, theta_in=t_in, theta_out=t_out))
                out.append(row(r, window_days=w, theta_in_apr=t_in, theta_out_apr=t_out))
    return pd.DataFrame(out)


# --- 4. Universe -----------------------------------------------------------------------------

def run_subset(cfg: dict, inputs: dict, coins: list[str]) -> E.Result:
    return E.run(subset_cfg(cfg, coins), {c: inputs[c] for c in coins})


def universe_split(cfg: dict, inputs: dict) -> pd.DataFrame:
    coins = list(inputs)
    majors = [c for c in coins if c in cfg["robustness"]["majors"]]
    rest = [c for c in coins if c not in majors]
    out = [row(run_subset(cfg, inputs, s), universe=name, coins=", ".join(s), n_coins=len(s))
           for name, s in (("all", coins), ("majors", majors), ("rest", rest))]
    return pd.DataFrame(out).set_index("universe")


def leave_one_out(cfg: dict, inputs: dict, base: E.Result | None = None) -> pd.DataFrame:
    """One run per coin without that coin; `coin_pnl` is the dropped coin's P&L in the full
    run, so `total_pnl` should equal the full run's total minus it (coins do not interact)."""
    base = base or E.run(cfg, inputs)
    by_coin = M.decomposition(base.trades)["total"]
    coins = list(inputs)
    out = []
    for c in coins:
        rest = [x for x in coins if x != c]
        out.append(row(run_subset(cfg, inputs, rest), dropped=c,
                       coin_pnl=float(by_coin.get(c, 0.0))))
    return pd.DataFrame(out).set_index("dropped")


# --- 5. Time stability -----------------------------------------------------------------------

def day_index(daily: pd.DataFrame) -> pd.DatetimeIndex:
    """The UTC day each snapshot's P&L belongs to (a snapshot at 00:00 closes the day before)."""
    return pd.DatetimeIndex(pd.to_datetime(daily["day_end"] - E.DAY, unit="ms"), name="day")


def daily_increments(daily: pd.DataFrame) -> pd.DataFrame:
    """Daily P&L split into components (from the cumulative columns of the equity snapshots)
    plus the change in unrealised price P&L; `total` is the daily P&L."""
    cols = E.COMPONENTS + ["price_unrealised"]
    cum = daily[cols].reset_index(drop=True)
    inc = cum.diff()
    inc.iloc[0] = cum.iloc[0]
    inc["total"] = inc.sum(axis=1)
    inc.index = day_index(daily)
    return inc


def quarterly(r: E.Result) -> pd.DataFrame:
    """Net P&L per calendar quarter (UTC) by component, with days, annualised return on total
    capital, trades entered and liquidations in the quarter."""
    inc = daily_increments(r.daily)
    q = inc.index.to_period("Q")
    out = inc.groupby(q).sum()
    out.insert(0, "days", inc.groupby(q).size())
    out["ann_return_total_capital"] = out["total"] / out["days"] * M.DAYS_PER_YEAR / r.capital
    t = r.trades
    to_q = lambda ms: pd.to_datetime(ms, unit="ms").dt.to_period("Q")
    out["trades_entered"] = t.groupby(to_q(t["entry_hour"])).size().reindex(out.index).fillna(0)
    liq = t[t["exit_reason"] == "liquidation"]
    out["liquidations"] = liq.groupby(to_q(liq["exit_hour"])).size().reindex(out.index).fillna(0)
    out.index = out.index.astype(str)
    out.index.name = "quarter"
    return out


def rolling_sharpe(pnl: pd.Series, days: int) -> pd.Series:
    """Annualised Sharpe over a trailing window of `days` daily P&L values (NaN until full)."""
    roll = pnl.rolling(days, min_periods=days)
    return roll.mean() / roll.std(ddof=1) * math.sqrt(M.DAYS_PER_YEAR)


def block_bootstrap_indices(n: int, block: int, resamples: int,
                            rng: np.random.Generator) -> np.ndarray:
    """Moving-block bootstrap: each row joins ceil(n / block) blocks of `block` consecutive
    days, starts drawn uniformly from the n - block + 1 possible, cut to n days."""
    k = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(resamples, k))
    return (starts[:, :, None] + np.arange(block)).reshape(resamples, k * block)[:, :n]


def block_bootstrap_sharpe(pnl: pd.Series, block: int, resamples: int, seed: int,
                           chunk: int = 1000) -> np.ndarray:
    """Annualised Sharpe of each block-bootstrap resample of the daily P&L."""
    x = np.asarray(pnl, float)
    rng = np.random.default_rng(seed)
    out = []
    for size in [chunk] * (resamples // chunk) + ([resamples % chunk] if resamples % chunk else []):
        s = x[block_bootstrap_indices(len(x), block, size, rng)]
        out.append(s.mean(axis=1) / s.std(axis=1, ddof=1) * math.sqrt(M.DAYS_PER_YEAR))
    return np.concatenate(out)


def bootstrap_summary(pnl: pd.Series, samples: np.ndarray, ci: float, block: int) -> pd.Series:
    a = (1 - ci) / 2
    return pd.Series({"sharpe": M.sharpe(pnl), "ci_low": float(np.quantile(samples, a)),
                      "ci_high": float(np.quantile(samples, 1 - a)),
                      "share_above_zero": float((samples > 0).mean()),
                      "days": len(pnl), "block_days": block, "resamples": len(samples)})


# --- 6. Leverage and buffer (added after the first backtest run) -----------------------------

def liquidated_split(trades: pd.DataFrame, start_ms: int | None = None,
                     end_ms: int | None = None) -> dict:
    """Count and total P&L of liquidated trades (exit in [start_ms, end_ms) if given) against
    all other trades."""
    t = trades
    m = t["exit_reason"] == "liquidation"
    if start_ms is not None:
        m &= t["exit_hour"] >= start_ms
    if end_ms is not None:
        m &= t["exit_hour"] < end_ms
    return dict(liquidated_trades=int(m.sum()), pnl_liquidated_trades=float(t.loc[m, "total"].sum()),
                pnl_other_trades=float(t.loc[~m, "total"].sum()))


def leverage_buffer(cfg: dict, inputs: dict) -> pd.DataFrame:
    p = cfg["robustness"]["post_hoc"]
    out = []
    for lev in p["leverage_per_leg"]:
        for buf in p["capital_buffer_frac"]:
            r = E.run(leverage_buffer_cfg(cfg, lev, buf), inputs)
            s = liquidated_split(r.trades)
            out.append(row(r, label=POST_HOC, leverage_per_leg=lev, capital_buffer_frac=buf)
                       | {k: s[k] for k in ("pnl_liquidated_trades", "pnl_other_trades")})
    return pd.DataFrame(out)


def tail_event_split(r: E.Result, days: list[str]) -> dict:
    """Daily P&L (equity curve, UTC days) with and without the given days, their share of the
    total, and the liquidations whose leg was liquidated on those days. Removing the days' P&L
    is not a rerun: positions and re-arming after those days are unchanged."""
    pnl = M.daily_pnl(r.daily, r.capital)
    pnl.index = day_index(r.daily)
    on = pnl.index.normalize().isin(pd.DatetimeIndex(days))
    total, tail = float(pnl.sum()), float(pnl[on].sum())
    t = r.trades
    liq = t[t["exit_reason"] == "liquidation"]
    liq_day = pd.to_datetime(liq["exit_hour"] - 1, unit="ms").dt.normalize()   # bar end -> its day
    rest = pnl[~on]
    return dict(total_pnl=total, pnl_tail_days=tail, pnl_excl_tail_days=total - tail,
                tail_share_of_total=tail / total if total else np.nan,
                liquidations=int(len(liq)), liquidations_tail_days=int(liq_day.isin(pd.DatetimeIndex(days)).sum()),
                ann_return_total_capital=M.annualised_return(pnl, r.capital),
                ann_return_excl_tail_days=M.annualised_return(rest, r.capital),
                sharpe=M.sharpe(pnl), sharpe_excl_tail_days=M.sharpe(rest))


def tail_event_sensitivity(cfg: dict, inputs: dict) -> pd.DataFrame:
    """tail_event_split for each post-hoc leverage at the baseline buffer (the buffer does not
    change P&L)."""
    p = cfg["robustness"]["post_hoc"]
    buf = cfg["margin"]["capital_buffer_frac"]
    out = []
    for lev in p["leverage_per_leg"]:
        r = E.run(leverage_buffer_cfg(cfg, lev, buf), inputs)
        out.append(dict(label=POST_HOC, leverage_per_leg=lev, capital=r.capital,
                        tail_days=", ".join(p["tail_event_days"]),
                        **tail_event_split(r, p["tail_event_days"])))
    return pd.DataFrame(out).set_index("leverage_per_leg")


# --- Figures ---------------------------------------------------------------------------------

def plot_heatmap(grid: pd.DataFrame, baseline: dict, path=None):
    """Annualised net return on total capital over entry x exit, one panel per signal window,
    on a shared diverging scale centred at zero; the baseline cell is outlined."""
    windows = sorted(grid["window_days"].unique())
    v = grid["ann_return_total_capital"] * 100
    lim = max(float(v.abs().max()), 0.1)
    norm = TwoSlopeNorm(vmin=-lim, vcenter=0, vmax=lim)
    fig, axes = plt.subplots(1, len(windows), figsize=(4.0 * len(windows) + 1, 4.4),
                             facecolor=SURFACE, sharey=True)
    for ax, w in zip(np.atleast_1d(axes), windows):
        g = grid[grid["window_days"] == w]
        ret = g.pivot(index="theta_in_apr", columns="theta_out_apr",
                      values="ann_return_total_capital") * 100
        n = g.pivot(index="theta_in_apr", columns="theta_out_apr", values="trades")
        im = ax.imshow(ret.to_numpy(), cmap=DIVERGING, norm=norm, aspect="auto", origin="lower")
        for i in range(ret.shape[0]):
            for j in range(ret.shape[1]):
                ax.text(j, i, f"{ret.iat[i, j]:+.1f}%\nn={n.iat[i, j]:.0f}", ha="center",
                        va="center", fontsize=7.5, color=INK)
        ax.set_xticks(range(ret.shape[1]), [f"{x:.1%}" for x in ret.columns], fontsize=8)
        ax.set_yticks(range(ret.shape[0]), [f"{x:.1%}" for x in ret.index], fontsize=8)
        ax.set_xlabel("Exit below |signal|, APR", color=INK_2, fontsize=9)
        ax.set_title(f"{w}-day signal", loc="left", fontsize=10, color=INK)
        ax.tick_params(colors=INK_2, length=0)
        for s in ax.spines.values():
            s.set_visible(False)
        if w == baseline["window_days"]:
            i = list(ret.index).index(baseline["theta_in_apr"])
            j = list(ret.columns).index(baseline["theta_out_apr"])
            ax.add_patch(Rectangle((j - 0.5, i - 0.5), 1, 1, fill=False, edgecolor=INK,
                                   linewidth=2))
    np.atleast_1d(axes)[0].set_ylabel("Enter above |signal|, APR", color=INK_2, fontsize=9)
    cb = fig.colorbar(im, ax=axes, shrink=0.85, pad=0.02)
    cb.set_label("Annualised net return on total capital, %", color=INK_2, fontsize=9)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=INK_2, labelsize=8)
    fig.suptitle("Parameter grid, design period (baseline outlined; n = trades). "
                 "Shown for stability only: not used to pick parameters",
                 x=0.01, ha="left", fontsize=11, color=INK)
    if path:
        fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    return fig


def plot_quarterly(q: pd.DataFrame, path=None):
    fig, ax = plt.subplots(figsize=(10, 4), facecolor=SURFACE)
    _style(ax)
    y = q["total"]
    ax.bar(range(len(y)), y, width=0.6, color=[POS if v >= 0 else NEG for v in y],
           edgecolor=SURFACE, linewidth=2)
    ax.axhline(0, color=INK_2, linewidth=1)
    for i, v in enumerate(y):
        ax.annotate(f"{v:+,.0f}", (i, v), xytext=(0, 4 if v >= 0 else -4),
                    textcoords="offset points", ha="center", va="bottom" if v >= 0 else "top",
                    fontsize=8, color=INK)
    labels = [f"{p}\n({d:.0f}d)" if d < 85 else p for p, d in zip(q.index, q["days"])]
    ax.set_xticks(range(len(y)), labels, fontsize=8, color=INK_2)
    ax.set_ylabel("Net P&L, $", color=INK_2, fontsize=9)
    ax.set_title("Net P&L by quarter, corrected model, design period (blue gain, red loss)",
                 loc="left", fontsize=11, color=INK)
    if path:
        fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    return fig


def plot_rolling_sharpe(rs: pd.Series, full: float, days: int, path=None):
    fig, ax = plt.subplots(figsize=(10, 4), facecolor=SURFACE)
    _style(ax)
    ax.axhline(0, color=INK_2, linewidth=1)
    ax.plot(rs.index, rs, color=POS, linewidth=2)
    ax.axhline(full, color=INK_2, linewidth=1, linestyle="--")
    first = rs.first_valid_index()
    ax.annotate(f"full period {full:+.2f}", (first, full), xytext=(4, -12),
                textcoords="offset points", fontsize=8, color=INK_2, ha="left")
    ax.set_ylabel("Annualised Sharpe", color=INK_2, fontsize=9)
    ax.set_title(f"Rolling {days}-day Sharpe ratio of daily net P&L, design period",
                 loc="left", fontsize=11, color=INK)
    ax.margins(x=0)
    if path:
        fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    return fig


def plot_bootstrap(samples: np.ndarray, s: pd.Series, path=None):
    fig, ax = plt.subplots(figsize=(8, 3.8), facecolor=SURFACE)
    _style(ax)
    ax.hist(samples, bins=60, color=POS, edgecolor=SURFACE, linewidth=0.5)
    for x, style, text in ((s["sharpe"], "-", f"point {s['sharpe']:+.2f}"),
                           (s["ci_low"], "--", f"{s['ci_low']:+.2f}"),
                           (s["ci_high"], "--", f"{s['ci_high']:+.2f}")):
        ax.axvline(x, color=INK, linewidth=1.2, linestyle=style)
        ax.annotate(text, (x, ax.get_ylim()[1]), xytext=(3, -12), textcoords="offset points",
                    fontsize=8, color=INK)
    ax.axvline(0, color=INK_2, linewidth=1)
    ax.set_xlabel("Annualised Sharpe of a resample", color=INK_2, fontsize=9)
    ax.set_ylabel("Resamples", color=INK_2, fontsize=9)
    ax.set_title(f"{int(s['block_days'])}-day block bootstrap of the Sharpe ratio "
                 f"({int(s['resamples']):,} resamples; dashed: 95% interval)",
                 loc="left", fontsize=11, color=INK)
    if path:
        fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    return fig


# --- Runner ----------------------------------------------------------------------------------

def save(df: pd.DataFrame | pd.Series, name: str) -> None:
    TABLES.mkdir(parents=True, exist_ok=True)
    df.to_csv(TABLES / f"robustness_{name}.csv")


def run_all(cfg: dict | None = None, inputs: dict | None = None) -> dict:
    """Every check, writing report/tables/robustness_*.csv and report/figures/robustness_*.png."""
    cfg = baseline_cfg(cfg)
    inputs = inputs if inputs is not None else E.load_inputs(cfg)
    base = E.run(cfg, inputs)
    out = {}
    joint, grid, be = cost_sensitivity(cfg, inputs)
    out.update(cost_joint=joint, cost_grid=grid,
               breakeven=pd.Series({"breakeven_cost_multiple": be}))
    out["maker_post_hoc"] = maker_scenario(cfg, inputs, base)
    out["parameter_grid"] = parameter_grid(cfg, inputs)
    out["universe_split"] = universe_split(cfg, inputs)
    out["leave_one_out"] = leave_one_out(cfg, inputs, base)
    out["quarterly"] = quarterly(base)
    pnl = M.daily_pnl(base.daily, base.capital)
    pnl.index = day_index(base.daily)
    rc = cfg["robustness"]
    out["rolling_sharpe"] = rolling_sharpe(pnl, rc["rolling_sharpe_days"]).rename("sharpe")
    b = rc["bootstrap"]
    samples = block_bootstrap_sharpe(pnl, b["block_days"], b["resamples"], b["seed"])
    out["bootstrap"] = bootstrap_summary(pnl, samples, b["ci"], b["block_days"])
    out["leverage_buffer_post_hoc"] = leverage_buffer(cfg, inputs)
    out["tail_event_post_hoc"] = tail_event_sensitivity(cfg, inputs)
    for k, v in out.items():
        save(v, k)
    s = cfg["strategy"]
    plot_heatmap(out["parameter_grid"], dict(window_days=s["signal_window_days"],
                 theta_in_apr=s["theta_in_apr"], theta_out_apr=s["theta_out_apr"]),
                 FIGURES / "robustness_parameter_grid.png")
    plot_quarterly(out["quarterly"], FIGURES / "robustness_quarterly.png")
    plot_rolling_sharpe(out["rolling_sharpe"], M.sharpe(pnl), rc["rolling_sharpe_days"],
                        FIGURES / "robustness_rolling_sharpe.png")
    plot_bootstrap(samples, out["bootstrap"], FIGURES / "robustness_bootstrap_sharpe.png")
    plt.close("all")
    out["_base"], out["_samples"] = base, samples
    return out


if __name__ == "__main__":
    res = run_all()
    for k, v in res.items():
        if not k.startswith("_"):
            print(f"\n== {k}\n{v.round(4).to_string() if hasattr(v, 'round') else v}")
