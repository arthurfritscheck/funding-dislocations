"""Figures for the Phase 4 backtest (design period). Same tokens and palette as gap_stats."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from src.analysis.gap_stats import FIGURES, GRID, INK, INK_2, SURFACE, _style  # noqa: E402,F401

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a"]   # fixed categorical order (validated in Phase 3)
LABELS = {"hl_funding": "Hyperliquid funding", "bn_funding": "Binance funding",
          "price_spread": "Price spread", "fees": "Fees", "slippage": "Slippage",
          "transfer_fees": "Transfer fees", "liquidation": "Liquidation penalty",
          "price_unrealised": "Open position (unrealised)"}


def plot_equity(curves: dict[str, pd.DataFrame], capital: float, path: Path | None = None):
    """Cumulative P&L in dollars per run (daily equity minus capital), one shared axis."""
    fig, ax = plt.subplots(figsize=(10, 4.2), facecolor=SURFACE)
    _style(ax)
    ax.axhline(0, color=INK_2, linewidth=1)
    for (name, daily), color in zip(curves.items(), PALETTE):
        x = pd.to_datetime(daily["day_end"], unit="ms")
        y = daily["equity"] - capital
        ax.plot(x, y, color=color, linewidth=2, label=name, solid_joinstyle="round")
        ax.annotate(f"{name}  {y.iloc[-1]:+,.0f}", (x.iloc[-1], y.iloc[-1]), xytext=(6, 0),
                    textcoords="offset points", va="center", fontsize=8, color=INK)
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 4, 7, 10]))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.set_ylabel("Cumulative P&L, $", color=INK_2, fontsize=9)
    ax.set_title(f"Backtest P&L on ${capital:,.0f} total capital, design period", loc="left",
                 fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8, loc="lower left", labelcolor=INK)
    ax.margins(x=0.0)
    fig.text(0.01, 0.01, "End-of-day mark to market at closes; all fees, slippage, transfers "
             "and liquidation penalties included unless the run says zero cost.",
             color=INK_2, fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 0.9, 1))
    if path:
        fig.savefig(path, dpi=150, facecolor=SURFACE)
    return fig


def plot_decomposition(row: pd.Series, title: str, path: Path | None = None):
    """Horizontal bars: P&L by component in dollars, with the total."""
    keys = [k for k in LABELS if k in row.index and abs(row[k]) > 0.005]
    vals = [row[k] for k in keys] + [row["total"]]
    names = [LABELS[k] for k in keys] + ["Total"]
    fig, ax = plt.subplots(figsize=(10, 0.45 * len(names) + 1.2), facecolor=SURFACE)
    _style(ax)
    y = range(len(names))[::-1]
    colors = [PALETTE[0]] * len(keys) + [INK]
    ax.barh(list(y), vals, color=colors, height=0.6)
    ax.axvline(0, color=INK_2, linewidth=1)
    span = max(abs(v) for v in vals) or 1.0
    for yi, v in zip(y, vals):
        ax.text(v + (0.01 * span if v >= 0 else -0.01 * span), yi, f"{v:+,.0f}",
                va="center", ha="left" if v >= 0 else "right", fontsize=8, color=INK)
    ax.set_yticks(list(y), names, fontsize=9, color=INK)
    ax.set_xlim(-1.2 * span, 1.2 * span)
    ax.set_xlabel("$", color=INK_2, fontsize=9)
    ax.set_title(title, loc="left", fontsize=11, color=INK)
    fig.tight_layout()
    if path:
        fig.savefig(path, dpi=150, facecolor=SURFACE)
    return fig
