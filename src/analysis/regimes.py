"""When gaps appear, how they co-move, and how they relate to the price spread.

Design period only (Phase 3 items 3-5). Descriptive; no trading rules.

3. Regime regression on daily data: mean |gap| (% APR) on
     btc_rv      BTC realised volatility, % annualised, from hourly Binance returns
     ret30       the coin's trailing 30-day log return, % (and its absolute value)
     log_vol30   log of the coin's mean daily Binance quote volume over 30 days (time-varying size)
     avg_funding average realised funding across all coins and both venues, % APR
   All regressors are measured over the previous day / window ending the previous day, so they are
   known at the start of the day being explained. Model A has coin fixed effects (within
   transformation); model B drops them and adds the large-cap bucket dummy instead (a
   time-invariant bucket is absorbed by coin fixed effects). Driscoll-Kraay standard errors
   (robust to cross-coin correlation and serial correlation, 7 lags).
4. Cross-section: pairwise correlation of gaps across coins (hourly and daily), the average
   off-diagonal correlation and the implied effective number of independent coins.
5. Gap vs price spread: correlations, and the spread move during gap episodes, signed so that a
   positive number is a move in favour of a position short the higher-funding venue.

    .venv\\Scripts\\python.exe -m src.analysis.regimes
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm

from src.analysis.gap_stats import APR, TABLES, load_design_panel
from src.analysis.half_life import all_episodes
from src.clean.align import _load
from src.clean.normalise import HOUR
from src.config import ROOT, load_config, period_ms

DAY = 24 * HOUR
REGRESSORS_A = ["btc_rv", "ret30", "abs_ret30", "log_vol30", "avg_funding"]
REGRESSORS_B = ["btc_rv", "ret30", "abs_ret30", "avg_funding", "large_cap"]


# --- Inputs from raw Binance klines (hourly prices exist for the whole design period) -------

def binance_daily(cfg: dict) -> dict[str, pd.DataFrame]:
    """Per coin: daily close, daily quote volume and daily realised vol from 1h klines."""
    _, end = period_ms(cfg, "design")
    out = {}
    for coin in cfg["universe"]["coins"]:
        k = _load(ROOT / cfg["paths"]["raw"] / "binance" / "klines_1h" / f"{coin}.parquet",
                  "open_time", end)
        k = k[k["close_time"].astype("int64") < k["fetched_at"].astype("int64")]
        k = k.sort_values("open_time")
        close = k["close"].astype(float).to_numpy()
        r = np.diff(np.log(close), prepend=np.nan)
        day = k["open_time"].astype("int64").to_numpy() // DAY * DAY
        d = pd.DataFrame({"day": day, "close": close, "r2": r ** 2,
                          "qv": k["quote_volume"].astype(float).to_numpy()})
        g = d.groupby("day")
        out[coin] = pd.DataFrame({"close": g["close"].last(), "quote_volume": g["qv"].sum(),
                                  "rv": np.sqrt(g["r2"].sum(min_count=20) * 365) * 100})
    return out


def large_cap_bucket(cfg: dict, daily: dict[str, pd.DataFrame], n_large: int = 5) -> list[str]:
    """Top n coins by Binance quote volume over the universe selection window (Aug-Oct 2024),
    fixed before any regression is run."""
    w = cfg["universe"]["selection_window"]
    lo = int(pd.Timestamp(w["start"], tz="UTC").value // 1_000_000)
    hi = int(pd.Timestamp(w["end"], tz="UTC").value // 1_000_000) + DAY
    vol = {c: d.loc[(d.index >= lo) & (d.index < hi), "quote_volume"].mean() for c, d in daily.items()}
    return sorted(vol, key=vol.get, reverse=True)[:n_large]


def regression_frame(panel: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    daily = binance_daily(cfg)
    large = large_cap_bucket(cfg, daily)
    p = panel.assign(day=panel["hour"] // DAY * DAY)
    y = (p.assign(abs_gap=p["gap"].abs() * APR * 100)
         .groupby(["coin", "day"], observed=True)["abs_gap"].mean())
    fund = ((p["f_hl_realised"] + p["f_bn_realised"]) / 2 * APR * 100).groupby(p["day"]).mean()
    btc_rv = daily["BTC"]["rv"]
    rows = []
    for coin in cfg["universe"]["coins"]:
        d = daily[coin]
        x = pd.DataFrame(index=d.index)
        x["ret30"] = np.log(d["close"] / d["close"].shift(30)) * 100
        x["abs_ret30"] = x["ret30"].abs()
        x["log_vol30"] = np.log(d["quote_volume"].rolling(30).mean())
        x["btc_rv"] = btc_rv.reindex(d.index)
        x["avg_funding"] = fund.reindex(d.index)
        x = x.shift(1)                      # known at the start of the day being explained
        x["abs_gap"] = y.xs(coin, level="coin").reindex(d.index)
        x["coin"] = coin
        x["large_cap"] = float(coin in large)
        rows.append(x.reset_index(names="day"))
    return pd.concat(rows, ignore_index=True).dropna(), large


def _dk_ols(y: pd.Series, X: pd.DataFrame, time: pd.Series, lags: int = 7):
    """OLS with Driscoll-Kraay standard errors."""
    t = pd.factorize(time, sort=True)[0]
    return sm.OLS(y, X).fit(cov_type="hac-groupsum", cov_kwds={"time": t, "maxlags": lags})


def regime_regression(frame: pd.DataFrame) -> dict:
    """Model A (coin fixed effects, within) and model B (pooled, large-cap dummy)."""
    res = {}
    # A: within transformation removes coin means.
    dm = frame.groupby("coin")[["abs_gap"] + REGRESSORS_A].transform(lambda s: s - s.mean())
    fa = _dk_ols(dm["abs_gap"], dm[REGRESSORS_A], frame["day"])
    # B: pooled with a constant and the size bucket.
    fb = _dk_ols(frame["abs_gap"], sm.add_constant(frame[REGRESSORS_B]), frame["day"])
    for name, fit, regs in (("A_coin_fe", fa, REGRESSORS_A), ("B_pooled_size", fb, REGRESSORS_B)):
        sd = frame[regs].std()
        tab = pd.DataFrame({"coef": fit.params, "se_dk": fit.bse, "t": fit.tvalues,
                            "p": fit.pvalues})
        tab["effect_of_1sd"] = tab["coef"] * sd.reindex(tab.index)
        tab.attrs = {"n": int(fit.nobs), "r2": float(fit.rsquared),
                     "coins": frame["coin"].nunique(), "days": frame["day"].nunique()}
        res[name] = tab
    return res


# --- Cross-section --------------------------------------------------------------------------

def gap_correlations(panel: pd.DataFrame) -> dict:
    hourly = panel.pivot_table(index="hour", columns="coin", values="gap", observed=True)
    daily = hourly.groupby(hourly.index // DAY).mean()
    out = {}
    for name, w in (("hourly", hourly), ("daily", daily)):
        c = w.corr()
        n = len(c)
        off = c.to_numpy()[~np.eye(n, dtype=bool)]
        rho = off.mean()
        out[name] = {"matrix": c, "avg_pairwise": rho, "min_pairwise": off.min(),
                     "max_pairwise": off.max(), "n_eff": n / (1 + (n - 1) * rho),
                     "avg_with_others": (c.sum() - 1) / (n - 1)}
    return out


# --- Gap vs price spread --------------------------------------------------------------------

def gap_spread_correlation(panel: pd.DataFrame) -> pd.DataFrame:
    """Per coin, on rows with a spread: correlation of the spread with the known and the
    realised gap, separately for the 4h-price and 1h-price parts of the design period."""
    p = panel.dropna(subset=["spread"]).copy()
    p["realised_gap"] = p["f_hl_realised"] - p["f_bn_realised"]
    first_1h = p.groupby("coin", observed=True)["hour"].transform(
        lambda h: h[h.diff() == HOUR].min() - HOUR if (h.diff() == HOUR).any() else np.inf)
    p["prices"] = np.where(p["hour"] >= first_1h, "1h", "4h")
    rows = []
    for (coin, res), d in p.groupby(["coin", "prices"], observed=True):
        rows.append({"coin": coin, "prices": res, "n": len(d),
                     "corr_known_gap": d["gap"].corr(d["spread"]),
                     "corr_realised_gap": d["realised_gap"].corr(d["spread"]),
                     "spearman_realised_gap": d["realised_gap"].corr(d["spread"], method="spearman"),
                     "median_abs_spread_bps": d["spread"].abs().median() * 1e4})
    return pd.DataFrame(rows).set_index(["coin", "prices"])


def spread_during_episodes(panel: pd.DataFrame, eps: pd.DataFrame) -> pd.DataFrame:
    """Spread move over each finished episode, using exact hourly prices only.

    An episode starting at hour row S (decision at time S) and ending at row E has entry price
    time S and exit price time E; the spread at those moments is the panel spread at rows S-1 and
    E-1 (prices are closes at the end of the row's hour). Episodes without both prices (most of the
    4h-price period) are skipped. favourable_bps = -sign(gap at start) x (s_exit - s_entry) x 1e4:
    positive = the spread moved in favour of a position short the higher-funding venue.
    """
    rows = []
    for coin, d in panel.groupby("coin", observed=True):
        s = d.set_index("hour")["spread"]
        e = eps[(eps["coin"] == coin) & ~eps["censored"]]
        t0 = e["start_hour"].to_numpy() - HOUR
        t1 = e["start_hour"].to_numpy() + (e["duration_h"].to_numpy() - 1) * HOUR
        s0, s1 = s.reindex(t0).to_numpy(), s.reindex(t1).to_numpy()
        ok = ~np.isnan(s0) & ~np.isnan(s1)
        sub = e[ok].assign(s_entry=s0[ok], s_exit=s1[ok])
        sub["favourable_bps"] = -sub["sign"] * (sub["s_exit"] - sub["s_entry"]) * 1e4
        sub["entry_spread_signed_bps"] = sub["sign"] * sub["s_entry"] * 1e4
        rows.append(sub)
    return pd.concat(rows, ignore_index=True)


def spread_episode_summary(se: pd.DataFrame) -> pd.DataFrame:
    def f(d):
        x = d["favourable_bps"]
        return pd.Series({"episodes": len(d), "median_favourable_bps": x.median(),
                          "mean_favourable_bps": x.mean(), "p10_bps": x.quantile(0.1),
                          "p90_bps": x.quantile(0.9), "share_favourable": (x > 0).mean(),
                          "share_adverse_gt_10bps": (x < -10).mean(),
                          "median_entry_spread_signed_bps": d["entry_spread_signed_bps"].median()})
    return se.groupby("threshold").apply(f, include_groups=False)


def run(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    a = cfg["analysis"]
    panel = load_design_panel(cfg)
    frame, large = regression_frame(panel, cfg)
    reg = regime_regression(frame)
    corr = gap_correlations(panel)
    gsc = gap_spread_correlation(panel)
    eps = all_episodes(panel, a["gap_thresholds_apr"],
                       end_on_sign_change=a["episodes_end_on_sign_change"])
    se = spread_during_episodes(panel, eps)
    out = {"large_caps": large, "regression": reg, "correlations": corr,
           "correlation_summary": pd.DataFrame(
               {k: {m: v[m] for m in ("avg_pairwise", "min_pairwise", "max_pairwise", "n_eff")}
                for k, v in corr.items()}).T,
           "gap_spread_correlation": gsc, "spread_episodes": se,
           "spread_episode_summary": spread_episode_summary(se)}
    TABLES.mkdir(parents=True, exist_ok=True)
    for name, tab in reg.items():
        tab.to_csv(TABLES / f"regimes_regression_{name}.csv", float_format="%.6g")
    out["correlation_summary"].to_csv(TABLES / "regimes_gap_correlation_summary.csv",
                                      float_format="%.6g")
    corr["daily"]["matrix"].to_csv(TABLES / "regimes_gap_correlation_daily.csv", float_format="%.4f")
    corr["hourly"]["matrix"].to_csv(TABLES / "regimes_gap_correlation_hourly.csv", float_format="%.4f")
    gsc.to_csv(TABLES / "regimes_gap_spread_correlation.csv", float_format="%.6g")
    out["spread_episode_summary"].to_csv(TABLES / "regimes_spread_during_episodes.csv",
                                         float_format="%.6g")
    return out


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    r = run()
    print("large caps:", r["large_caps"])
    for k, t in r["regression"].items():
        print(f"\n== {k} {t.attrs}\n{t.round(4).to_string()}")
    print(f"\n== correlation summary\n{r['correlation_summary'].round(3).to_string()}")
    print(f"\n== avg daily correlation with others\n"
          f"{r['correlations']['daily']['avg_with_others'].round(3).to_string()}")
    print(f"\n== gap vs spread\n{r['gap_spread_correlation'].round(3).to_string()}")
    print(f"\n== spread during episodes\n{r['spread_episode_summary'].round(2).to_string()}")
