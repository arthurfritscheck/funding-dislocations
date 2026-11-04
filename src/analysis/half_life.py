"""Persistence of the funding gap, design period only (Phase 3 item 2, hypothesis H1).

1. AR(1) on daily-averaged gaps per coin:  g_t = a + phi * g_{t-1} + e_t,
   half-life = ln 0.5 / ln phi (days). 95% CI from phi +/- 1.96 SE mapped through the
   half-life formula (monotone for 0 < phi < 1). OLS and Newey-West (HAC) SEs are reported.
2. The same AR(1) on rolling 90-day windows (step one day).
3. Episodes on the hourly gap: start when |gap| rises above a threshold, end when it falls
   below half of it (Phase 3 item 2). Durations are summarised with Kaplan-Meier survival
   curves; episodes still open at the end of the period are right-censored.

All gaps are the known-at-t series in APR. No trading rules here.

    .venv\\Scripts\\python.exe -m src.analysis.half_life
"""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import statsmodels.api as sm  # noqa: E402
from statsmodels.tsa.stattools import adfuller  # noqa: E402

from src.analysis.gap_stats import (APR, FIGURES, GRID, INK, INK_2, SERIES, SURFACE,  # noqa: E402
                                    TABLES, _style, load_design_panel)
from src.clean.normalise import HOUR  # noqa: E402
from src.config import load_config  # noqa: E402

DAY = 24 * HOUR
Z95 = 1.959964
THRESHOLD_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]  # categorical slots 1-3, validated


# --- AR(1) ---------------------------------------------------------------------------------

def half_life(phi: float) -> float:
    """Days for a deviation to halve: ln 0.5 / ln phi. inf if phi >= 1, nan if phi <= 0."""
    if not np.isfinite(phi) or phi <= 0:
        return np.nan
    if phi >= 1:
        return np.inf
    return math.log(0.5) / math.log(phi)


def _hl_ci(phi: float, se: float) -> tuple[float, float]:
    lo, hi = phi - Z95 * se, phi + Z95 * se
    hl_lo = 0.0 if lo <= 0 else half_life(min(lo, 1.0))
    hl_hi = np.inf if hi >= 1 else (0.0 if hi <= 0 else half_life(hi))
    return hl_lo, hl_hi


def daily_gap(panel: pd.DataFrame) -> pd.DataFrame:
    """Mean known-at-t gap (APR) per coin per UTC day; columns = coins, index = day start ms."""
    d = panel.assign(day=panel["hour"] // DAY * DAY, g=panel["gap"] * APR)
    return d.pivot_table(index="day", columns="coin", values="g", aggfunc="mean", observed=True)


def ar1(y: pd.Series, hac_lags: int = 7) -> dict:
    """OLS AR(1) with intercept on a daily series; OLS and HAC standard errors."""
    y = y.dropna().to_numpy()
    x = sm.add_constant(y[:-1])
    fit = sm.OLS(y[1:], x).fit()
    hac = sm.OLS(y[1:], x).fit(cov_type="HAC", cov_kwds={"maxlags": hac_lags})
    a, phi = fit.params
    se, se_hac = fit.bse[1], hac.bse[1]
    lo, hi = _hl_ci(phi, se)
    lo_h, hi_h = _hl_ci(phi, se_hac)
    return {"n_days": len(y), "a": a, "phi": phi, "se": se, "se_hac": se_hac,
            "half_life_days": half_life(phi), "hl_lo": lo, "hl_hi": hi,
            "hl_lo_hac": lo_h, "hl_hi_hac": hi_h,
            "long_run_mean_apr": a / (1 - phi) if phi != 1 else np.nan,
            "adf_pvalue": adfuller(y, maxlag=7, autolag=None, result_object=False)[1]}


def half_lives(panel: pd.DataFrame) -> pd.DataFrame:
    daily = daily_gap(panel)
    return pd.DataFrame({c: ar1(daily[c]) for c in daily.columns}).T.rename_axis("coin")


def _ols_ar1_fast(y: np.ndarray) -> tuple[float, float]:
    """phi and its OLS SE for one window (closed form, for the rolling fits)."""
    x, z = y[:-1], y[1:]
    xm, zm = x.mean(), z.mean()
    sxx = ((x - xm) ** 2).sum()
    if sxx == 0:
        return np.nan, np.nan
    phi = ((x - xm) * (z - zm)).sum() / sxx
    resid = z - (zm - phi * xm) - phi * x
    s2 = (resid ** 2).sum() / (len(z) - 2)
    return phi, math.sqrt(s2 / sxx)


def rolling_ar1(panel: pd.DataFrame, window_days: int = 90) -> pd.DataFrame:
    """AR(1) on every trailing `window_days` window (step one day). Long format."""
    daily = daily_gap(panel)
    rows = []
    for coin in daily.columns:
        y = daily[coin].to_numpy()
        for end in range(window_days, len(y) + 1):
            w = y[end - window_days:end]
            if np.isnan(w).any():
                continue
            phi, se = _ols_ar1_fast(w)
            rows.append({"coin": coin, "window_end_day": int(daily.index[end - 1]), "phi": phi,
                         "se": se, "half_life_days": half_life(phi)})
    return pd.DataFrame(rows)


def rolling_summary(roll: pd.DataFrame) -> pd.DataFrame:
    def f(d):
        hl = d["half_life_days"]
        return pd.Series({"windows": len(d), "phi_median": d["phi"].median(),
                          "phi_p10": d["phi"].quantile(0.1), "phi_p90": d["phi"].quantile(0.9),
                          "hl_median_days": hl.median(),
                          "share_phi_le_0": (d["phi"] <= 0).mean(),
                          "share_phi_sig": ((d["phi"] - Z95 * d["se"]) > 0).mean()})
    return roll.groupby("coin", observed=True).apply(f, include_groups=False)


# --- Episodes and survival -----------------------------------------------------------------

def episodes(gap_apr: np.ndarray, hours: np.ndarray, theta: float) -> pd.DataFrame:
    """Episodes of |gap| > theta that end when |gap| < theta / 2.

    duration_h counts the hours from the first hour above theta up to (not including) the first
    hour below theta / 2. NaN hours leave the state unchanged. An episode open at the end of the
    series is right-censored. `sign_flip` marks episodes in which the gap changed sign while
    staying above theta / 2 in absolute value.
    """
    rows, start, sign, vals = [], None, 0, []
    for i, v in enumerate(gap_apr):
        if np.isnan(v):
            continue
        if start is None:
            if abs(v) > theta:
                start, sign, vals = i, np.sign(v), [v]
        elif abs(v) < theta / 2:
            rows.append((start, i, False, sign, vals))
            start = None
        else:
            vals.append(v)
    if start is not None:
        rows.append((start, len(gap_apr), True, sign, vals))
    return pd.DataFrame([{"start_hour": int(hours[s]), "duration_h": e - s, "censored": c,
                          "sign": int(sg), "mean_abs_gap_apr": float(np.mean(np.abs(vs))),
                          "sign_flip": bool(np.any(np.sign(vs) == -sg))}
                         for s, e, c, sg, vs in rows],
                        columns=["start_hour", "duration_h", "censored", "sign",
                                 "mean_abs_gap_apr", "sign_flip"])


def all_episodes(panel: pd.DataFrame, thresholds=(0.10, 0.25, 0.50)) -> pd.DataFrame:
    parts = []
    for coin, d in panel.groupby("coin", observed=True):
        d = d.sort_values("hour")
        g, h = (d["gap"] * APR).to_numpy(), d["hour"].to_numpy()
        for t in thresholds:
            e = episodes(g, h, t)
            e.insert(0, "threshold", t)
            e.insert(0, "coin", coin)
            parts.append(e)
    return pd.concat(parts, ignore_index=True)


def kaplan_meier(durations: np.ndarray, censored: np.ndarray) -> pd.DataFrame:
    """Kaplan-Meier survival S(t) = P(duration > t) at each distinct event time."""
    durations, events = np.asarray(durations, float), ~np.asarray(censored, bool)
    times = np.unique(durations[events])
    s, rows = 1.0, [(0.0, 1.0, len(durations))]
    for t in times:
        at_risk = (durations >= t).sum()
        d = ((durations == t) & events).sum()
        s *= 1 - d / at_risk
        rows.append((t, s, at_risk))
    return pd.DataFrame(rows, columns=["t", "survival", "at_risk"])


def km_quantile(km: pd.DataFrame, q: float = 0.5) -> float:
    """Smallest t with S(t) <= 1 - q (e.g. the median duration); inf if never reached."""
    hit = km[km["survival"] <= 1 - q]
    return float(hit["t"].iloc[0]) if len(hit) else np.inf


def episode_summary(eps: pd.DataFrame) -> pd.DataFrame:
    def f(d):
        km = kaplan_meier(d["duration_h"], d["censored"]) if len(d) else None
        return pd.Series({"episodes": len(d), "censored": int(d["censored"].sum()),
                          "median_duration_h": km_quantile(km) if km is not None else np.nan,
                          "p75_duration_h": km_quantile(km, 0.75) if km is not None else np.nan,
                          "mean_abs_gap_apr": d["mean_abs_gap_apr"].mean(),
                          "share_positive": (d["sign"] > 0).mean() if len(d) else np.nan,
                          "share_sign_flip": d["sign_flip"].mean() if len(d) else np.nan,
                          "hours_in_episodes": int(d["duration_h"].sum())})
    return eps.groupby(["threshold", "coin"], observed=True).apply(f, include_groups=False)


# --- Charts --------------------------------------------------------------------------------

def plot_half_lives(hl: pd.DataFrame, path: Path | None = None, max_cap_days: float = 10.0):
    """Half-life by coin with 95% CIs (OLS SE), sorted. The axis fits the widest finite CI, up to
    max_cap_days; CIs beyond the axis are clipped with an arrow."""
    d = hl.sort_values("half_life_days")
    finite_hi = d["hl_hi"][np.isfinite(d["hl_hi"])]
    cap_days = min(max_cap_days, math.ceil(finite_hi.max() * 2) / 2 + 0.5 if len(finite_hi) else max_cap_days)
    y = np.arange(len(d))
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(d) + 1.2), facecolor=SURFACE)
    _style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=1)
    for yi, (coin, r) in zip(y, d.iterrows()):
        lo, hi = r["hl_lo"], min(r["hl_hi"], cap_days)
        ax.plot([lo, hi], [yi, yi], color=SERIES, linewidth=2, solid_capstyle="round")
        if r["hl_hi"] > cap_days:
            ax.annotate("", xy=(cap_days, yi), xytext=(cap_days - 0.25, yi),
                        arrowprops={"arrowstyle": "-|>", "color": SERIES, "lw": 2})
        ax.plot(r["half_life_days"], yi, "o", color=SERIES, markersize=8,
                markeredgecolor=SURFACE, markeredgewidth=2)
        ax.text(min(r["hl_hi"], cap_days) + 0.15, yi, f"{r['half_life_days']:.1f}",
                va="center", fontsize=8, color=INK_2)
    ax.set_yticks(y, d.index.astype(str), color=INK, fontsize=9)
    ax.set_xlim(0, cap_days + 0.8)
    ax.set_xlabel("Half-life of the daily mean gap, days (dot) with 95% CI", color=INK_2, fontsize=9)
    ax.set_title("Funding-gap half-life by coin, AR(1) on daily means", loc="left", fontsize=11,
                 color=INK)
    fig.text(0.01, 0.01, "Design period 2024-08-01 to 2026-06-30. OLS standard errors; "
             f"intervals beyond {cap_days:g} days are clipped (arrow).", color=INK_2, fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    if path:
        fig.savefig(path, dpi=150, facecolor=SURFACE)
    return fig


def plot_survival(eps: pd.DataFrame, path: Path | None = None, max_hours: float = 72.0):
    """Pooled Kaplan-Meier survival of episode durations, one line per threshold."""
    fig, ax = plt.subplots(figsize=(9, 4.2), facecolor=SURFACE)
    _style(ax)
    for color, (t, d) in zip(THRESHOLD_COLORS, eps.groupby("threshold")):
        km = kaplan_meier(d["duration_h"], d["censored"])
        x, s = km["t"].to_numpy(), km["survival"].to_numpy()
        keep = x <= max_hours
        ax.step(np.append(x[keep], max_hours), np.append(s[keep], s[keep][-1]), where="post",
                color=color, linewidth=2, label=f"|gap| > {t:.0%} APR (n={len(d):,})")
    ax.set_xlim(0, max_hours)
    ax.set_xticks(np.arange(0, max_hours + 1, 12))
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Episode duration, hours", color=INK_2, fontsize=9)
    ax.set_ylabel("Share of episodes still open", color=INK_2, fontsize=9)
    ax.set_title("How long gap episodes last (all 15 coins pooled)", loc="left", fontsize=11,
                 color=INK)
    leg = ax.legend(frameon=False, fontsize=9, loc="upper right")
    for txt in leg.get_texts():
        txt.set_color(INK)
    fig.text(0.01, 0.01, "Episode: starts when |gap| rises above the threshold, ends when it falls "
             "below half of it. Kaplan-Meier; open episodes censored. Design period.",
             color=INK_2, fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    if path:
        fig.savefig(path, dpi=150, facecolor=SURFACE)
    return fig


def plot_rolling_phi(roll: pd.DataFrame, hl: pd.DataFrame, path: Path | None = None):
    """Rolling 90-day AR(1) coefficient per coin (small multiples, shared scale); the thin
    horizontal line is the full-period phi."""
    coins = list(hl.index)
    ncol = 3
    nrow = math.ceil(len(coins) / ncol)
    fig, axes = plt.subplots(nrow, ncol, figsize=(11, 1.9 * nrow), sharex=True, sharey=True,
                             facecolor=SURFACE)
    for ax, coin in zip(axes.flat, coins):
        d = roll[roll["coin"] == coin]
        _style(ax)
        ax.axhline(hl.loc[coin, "phi"], color=INK_2, linewidth=1)
        ax.plot(pd.to_datetime(d["window_end_day"], unit="ms"), d["phi"], color=SERIES,
                linewidth=1.5)
        ax.set_title(str(coin), loc="left", fontsize=9, color=INK)
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 7]))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
        ax.tick_params(axis="x", labelsize=7)
    for ax in axes.flat[len(coins):]:
        ax.set_visible(False)
    fig.suptitle("Rolling 90-day AR(1) coefficient of the daily mean gap", x=0.01, ha="left",
                 fontsize=11, color=INK)
    fig.text(0.01, 0.005, "Window end on the x-axis; grey line = full design-period phi. "
             "Higher phi = slower mean reversion.", color=INK_2, fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    if path:
        fig.savefig(path, dpi=150, facecolor=SURFACE)
    return fig


def run(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    thresholds = cfg["analysis"]["gap_thresholds_apr"]
    panel = load_design_panel(cfg)
    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    hl = half_lives(panel)
    roll = rolling_ar1(panel, 90)
    eps = all_episodes(panel, thresholds)
    out = {"half_lives": hl, "rolling_summary": rolling_summary(roll),
           "episode_summary": episode_summary(eps), "rolling": roll, "episodes": eps}
    hl.to_csv(TABLES / "persistence_half_lives.csv", float_format="%.6g")
    out["rolling_summary"].to_csv(TABLES / "persistence_rolling_summary.csv", float_format="%.6g")
    out["episode_summary"].to_csv(TABLES / "persistence_episode_summary.csv", float_format="%.6g")
    roll.to_csv(TABLES / "persistence_rolling_ar1.csv", index=False, float_format="%.6g")
    eps.to_csv(TABLES / "persistence_episodes.csv", index=False, float_format="%.6g")
    out["fig_half_lives"] = plot_half_lives(hl, FIGURES / "persistence_half_lives.png")
    out["fig_survival"] = plot_survival(eps, FIGURES / "persistence_survival.png")
    out["fig_rolling_phi"] = plot_rolling_phi(roll, hl, FIGURES / "persistence_rolling_phi.png")
    return out


if __name__ == "__main__":
    res = run()
    pd.set_option("display.width", 250)
    for k in ("half_lives", "rolling_summary", "episode_summary"):
        print(f"\n== {k}\n{res[k].astype(float).round(3).to_string()}")
