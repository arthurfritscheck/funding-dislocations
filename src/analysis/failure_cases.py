"""Failure cases: the worst trades of the corrected baseline, design period only (docs/plan.md
Phase 5 item 7; DECISIONS.md 2026-11-08). For each trade: its loss by component, summary
statistics, and the price, mark and funding paths around it, so the cause can be classified.

Paths are hourly and stamped at the end of each hour (a close at H+1h, funding for [H, H+1h)):
  hl_close, bn_close   Hyperliquid bar close (every 4h in most of the design period), Binance 1h close
  hl_high, hl_low      Hyperliquid bar high and low (its own trades; the as-specified liquidation check)
  bn_mark_*            Binance mark-price kline (the Binance leg's liquidation reference)
  bn_index_*           Binance index-price kline (the proxy for Hyperliquid's mark, HL leg)
  f_hl_apr, f_bn_apr   realised funding, hourly-equivalent, annualised (x 8760)
  signal_apr           the strategy signal usable at that time
  *_funding_cum        the trade's funding, reconstructed exactly as the engine books it
  price_mtm            both legs marked at bar closes against their entry prices (before costs)
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.analysis.backtest_figures import PALETTE  # noqa: E402
from src.analysis.gap_stats import APR, FIGURES, INK, INK_2, SURFACE, _style  # noqa: E402
from src.backtest import engine as E  # noqa: E402
from src.clean.align import _closed, _load  # noqa: E402
from src.config import ROOT, period_ms  # noqa: E402

HOUR = E.HOUR
YELLOW = "#eda100"   # slot 4 of the categorical palette


def side_text(side: int) -> str:
    return "short HL / long BN" if side > 0 else "long HL / short BN"


def worst_trades(trades: pd.DataFrame, n: int) -> pd.DataFrame:
    """The n closed trades with the lowest total P&L, worst first, with a 1-based rank."""
    t = trades[trades["exit_hour"].notna()].nsmallest(n, "total").copy()
    t.insert(0, "rank", range(1, len(t) + 1))
    return t


def liquidated_leg(trade, d: pd.DataFrame) -> str:
    """Which leg a liquidation hit: the surviving leg closes at the open of the bar starting at
    the exit hour, the liquidated one at its liquidation price. "" if not a liquidation."""
    if trade.exit_reason != "liquidation":
        return ""
    row = d[d["hour"] == int(trade.exit_hour)]
    o_hl = float(row["o_hl"].iloc[0]) if len(row) else np.nan
    o_bn = float(row["o_bn"].iloc[0]) if len(row) else np.nan
    hl_hit = not np.isclose(trade.exit_hl, o_hl)
    bn_hit = not np.isclose(trade.exit_bn, o_bn)
    return "both" if hl_hit and bn_hit else "hyperliquid" if hl_hit else "binance"


def load_marks(cfg: dict, coin: str) -> pd.DataFrame:
    """Binance 1h mark- and index-price klines for one coin, by kline open hour, unit-scaled,
    cut at the design-period end (CLAUDE.md rule 3)."""
    _, end_ms = period_ms(cfg, "design")
    raw, unit = ROOT / cfg["paths"]["raw"] / "binance", cfg["symbols"][coin]["bn_unit"]
    out = None
    for ds, name in (("mark_price_1h", "bn_mark"), ("index_price_1h", "bn_index")):
        df = _closed(_load(raw / ds / f"{coin}.parquet", "open_time", end_ms), "close_time")
        df = df.drop_duplicates("open_time", keep="last")
        df = pd.DataFrame({"hour": df["open_time"].astype("int64"),
                           **{f"{name}_{c}": df[c].astype(float) / unit
                              for c in ("high", "low", "close")}})
        out = df if out is None else out.merge(df, on="hour", how="outer")
    return out.sort_values("hour").reset_index(drop=True)


def trade_path(d: pd.DataFrame, trade, marks: pd.DataFrame, pad_hours: int) -> pd.DataFrame:
    """Hourly paths from pad_hours before entry to pad_hours after exit (see module docstring).
    Funding is booked for hours in [entry_hour, exit_hour), as in the engine."""
    exit_ms = int(trade.exit_hour) if pd.notna(trade.exit_hour) else int(d["hour"].iloc[-1]) + HOUR
    lo, hi = int(trade.entry_hour) - pad_hours * HOUR, exit_ms + pad_hours * HOUR
    p = d[(d["hour"] >= lo) & (d["hour"] < hi)].merge(marks, on="hour", how="left")
    s, q = int(trade.side), float(trade.qty)
    held = ((p["hour"] >= trade.entry_hour) & (p["hour"] < exit_ms)).to_numpy()
    px = p["ref_px"].to_numpy(float)
    f_hl, f_bn = p["f_hl"].to_numpy(float), p["f_bn"].to_numpy(float)
    interval = p["bn_interval_h"].to_numpy(float)
    ok_hl = held & np.isfinite(f_hl) & np.isfinite(px)
    ok_bn = held & p["bn_settle"].to_numpy(bool) & np.isfinite(f_bn) & np.isfinite(interval) \
        & np.isfinite(px)
    hl_f = np.where(ok_hl, s * np.nan_to_num(f_hl) * q * np.nan_to_num(px), 0.0)
    bn_f = np.where(ok_bn, -s * np.nan_to_num(f_bn) * np.nan_to_num(interval) * q
                    * np.nan_to_num(px), 0.0)
    mtm = (-s * q * (p["c_hl"] - trade.entry_hl) + s * q * (p["c_bn"] - trade.entry_bn))
    out = pd.DataFrame({
        "time": pd.to_datetime(p["hour"] + HOUR, unit="ms"), "hour": p["hour"], "held": held,
        "hl_close": p["c_hl"].where(p["bar_end"]), "bn_close": p["ref_px"],
        **{f"hl_{k}": (p[c].where(p["bar_end"]) if c in p else np.nan)   # optional columns
           for k, c in (("high", "h_hl"), ("low", "l_hl"))},
        **{c: p[c] for c in marks.columns if c != "hour"},
        "spread_bps": np.log(p["c_hl"] / p["c_bn"]) * 1e4,
        "f_hl_apr": p["f_hl"] * APR, "f_bn_apr": p["f_bn"] * APR, "signal_apr": p["signal"],
        "hl_funding_cum": np.cumsum(hl_f), "bn_funding_cum": np.cumsum(bn_f),
        "price_mtm": mtm.where(p["bar_end"] & held)})
    return out.reset_index(drop=True)


def trade_summary(trade, path: pd.DataFrame, d: pd.DataFrame) -> dict:
    """Statistics for classifying a trade, from its record and its path."""
    s = int(trade.side)
    h = path[path["held"]]
    hl_side, bn_side = -s, s

    def adverse(series_hi, series_lo, side, entry):
        return (h[series_hi].max() / entry - 1) if side < 0 else (h[series_lo].min() / entry - 1)

    sig = d.set_index("hour")["signal"]
    return dict(
        coin=trade.coin, position=side_text(s),
        entry=pd.to_datetime(trade.entry_hour, unit="ms"),
        exit=pd.to_datetime(trade.exit_hour, unit="ms"),
        hold_days=(trade.exit_hour - trade.entry_hour) / E.DAY, exit_reason=trade.exit_reason,
        liquidated_leg=liquidated_leg(trade, d), rebalances=int(trade.n_rebalances),
        signal_at_entry_apr=float(sig.get(int(trade.entry_hour), np.nan)),
        signal_at_exit_apr=float(sig.get(int(trade.exit_hour), np.nan)),
        entry_spread_bps=float(np.log(trade.entry_hl / trade.entry_bn) * 1e4),
        exit_spread_bps=float(np.log(trade.exit_hl / trade.exit_bn) * 1e4),
        hl_leg_worst_index_move=float(adverse("bn_index_high", "bn_index_low", hl_side, trade.entry_hl)),
        hl_leg_worst_own_trade_move=float(adverse("hl_high", "hl_low", hl_side, trade.entry_hl)),
        bn_leg_worst_mark_move=float(adverse("bn_mark_high", "bn_mark_low", bn_side, trade.entry_bn)),
        mean_f_hl_apr=float(h["f_hl_apr"].mean()), mean_f_bn_apr=float(h["f_bn_apr"].mean()))


def funding_recon_error(trade, path: pd.DataFrame) -> tuple[float, float]:
    """(HL, BN) reconstructed funding at the end of the path minus the trade's booked funding."""
    return (float(path["hl_funding_cum"].iloc[-1] - trade.hl_funding),
            float(path["bn_funding_cum"].iloc[-1] - trade.bn_funding))


def plot_trade(trade, path: pd.DataFrame, summary: dict, path_out=None):
    """Four stacked panels on one time axis: prices with each leg's adverse mark extreme, the
    price spread, funding and the signal, and the trade's running P&L (funding and price)."""
    fig, axes = plt.subplots(4, 1, figsize=(11, 10), sharex=True, facecolor=SURFACE,
                             gridspec_kw=dict(height_ratios=[3, 1.4, 2, 2]))
    x = path["time"]
    entry = pd.to_datetime(trade.entry_hour, unit="ms")
    exit_ = pd.to_datetime(trade.exit_hour, unit="ms")
    for ax in axes:
        _style(ax)
        ax.axvspan(entry, exit_, color=INK_2, alpha=0.06, linewidth=0)
        for t in (entry, exit_):
            ax.axvline(t, color=INK_2, linewidth=1, linestyle=":")
        ax.tick_params(colors=INK_2, labelsize=8)
    s = int(trade.side)
    hl_adv, bn_adv = ("bn_index_high", "bn_mark_low") if s > 0 else ("bn_index_low", "bn_mark_high")
    ax = axes[0]
    hl = path[["time", "hl_close"]].dropna()
    ax.plot(hl["time"], hl["hl_close"], color=PALETTE[0], linewidth=2, label="Hyperliquid close")
    hl_ext = path[["time", "hl_high" if s > 0 else "hl_low"]].dropna()
    ax.plot(hl_ext["time"], hl_ext.iloc[:, 1], color=PALETTE[0], linewidth=1, linestyle="--",
            label=f"Hyperliquid bar {'high' if s > 0 else 'low'} (own trades)")
    ax.plot(x, path["bn_close"], color=PALETTE[1], linewidth=1.5, label="Binance close (1h)")
    ax.plot(x, path[bn_adv], color=PALETTE[2], linewidth=1,
            label=f"Binance mark {'low' if s > 0 else 'high'} (BN leg's liquidation check)")
    ax.plot(x, path[hl_adv], color=YELLOW, linewidth=1,
            label=f"Binance index {'high' if s > 0 else 'low'} (HL leg's proxy check)")
    ax.scatter([exit_, exit_], [trade.exit_hl, trade.exit_bn], s=40, zorder=5,
               color=[PALETTE[0], PALETTE[1]], edgecolor=SURFACE, linewidth=2)
    for y, name in ((trade.exit_hl, "HL exit"), (trade.exit_bn, "BN exit")):
        ax.annotate(f"{name} {y:,.4g}", (exit_, y), xytext=(6, 0), textcoords="offset points",
                    fontsize=8, color=INK, va="center")
    ax.set_ylabel("Price, $", color=INK_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8, loc="best", labelcolor=INK)
    ax = axes[1]
    sp = path[["time", "spread_bps"]].dropna()
    ax.plot(sp["time"], sp["spread_bps"], color=PALETTE[0], linewidth=1.5)
    ax.axhline(0, color=INK_2, linewidth=1)
    ax.set_ylabel("Spread HL vs BN\nclose, bps", color=INK_2, fontsize=9)
    ax = axes[2]
    ax.plot(x, path["f_hl_apr"] * 100, color=PALETTE[0], linewidth=1, label="Hyperliquid funding")
    ax.plot(x, path["f_bn_apr"] * 100, color=PALETTE[1], linewidth=1.5,
            label="Binance funding (hourly-equivalent)")
    ax.plot(x, path["signal_apr"] * 100, color=PALETTE[2], linewidth=2, label="Signal (7-day mean gap)")
    ax.axhline(0, color=INK_2, linewidth=1)
    ax.set_ylabel("Funding, % APR", color=INK_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8, loc="best", labelcolor=INK)
    ax = axes[3]
    held = path[path["held"]]
    funding = held["hl_funding_cum"] + held["bn_funding_cum"]   # zero before entry
    ax.plot(held["time"], funding, color=PALETTE[2], linewidth=2, label="Funding (both legs)")
    mtm = held[["time", "price_mtm"]].dropna()
    ax.plot(mtm["time"], mtm["price_mtm"], color=PALETTE[0], linewidth=2,
            label="Price, both legs at bar closes (before costs)")
    ax.scatter([exit_], [trade.total], s=40, color=INK, zorder=5)
    ax.annotate(f"trade total {trade.total:+,.0f}", (exit_, trade.total), xytext=(6, 0),
                textcoords="offset points", fontsize=8, color=INK, va="center")
    ax.axhline(0, color=INK_2, linewidth=1)
    ax.set_ylabel("Trade P&L, $", color=INK_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8, loc="best", labelcolor=INK)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M"))
    leg = f", {summary['liquidated_leg']} leg liquidated" if summary["liquidated_leg"] else ""
    fig.suptitle(f"#{int(trade['rank'])} {trade.coin}, {side_text(s)}, {trade.total:+,.0f} "
                 f"({trade.exit_reason}{leg}). Shaded: position open. Times UTC, end of hour",
                 x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    if path_out:
        fig.savefig(path_out, dpi=130, bbox_inches="tight", facecolor=SURFACE)
    return fig


def run(cfg: dict, inputs: dict, trades: pd.DataFrame) -> dict:
    """Worst trades with components and statistics, their paths, and one figure each
    (report/figures/failure_case_<rank>_<coin>.png). Writes report/tables/failure_cases_*.csv."""
    fc = cfg["robustness"]["failure_cases"]
    worst = worst_trades(trades, fc["n_worst"])
    pad = int(fc["pad_days"] * 24)
    rows, paths, errors = [], [], []
    for trade in worst.itertuples(index=False):
        tr = pd.Series(trade._asdict())
        d = inputs[tr.coin]
        path = trade_path(d, tr, load_marks(cfg, tr.coin), pad)
        summ = trade_summary(tr, path, d)
        rows.append({"rank": int(tr["rank"]), **summ,
                     **{c: tr[c] for c in E.COMPONENTS + ["total"]}})
        errors.append(funding_recon_error(tr, path))
        paths.append(path.assign(rank=int(tr["rank"]), coin=tr.coin))
        plot_trade(tr, path, summ, FIGURES / f"failure_case_{int(tr['rank'])}_{tr.coin}.png")
        plt.close("all")
    table = pd.DataFrame(rows).set_index("rank")
    table["hl_funding_recon_error"], table["bn_funding_recon_error"] = zip(*errors)
    paths = pd.concat(paths, ignore_index=True)
    out = ROOT / "report" / "tables"
    table.to_csv(out / "failure_cases_worst_trades.csv")
    paths.to_csv(out / "failure_cases_paths.csv", index=False)
    return dict(table=table, paths=paths)
