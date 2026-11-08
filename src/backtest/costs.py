"""Trading costs: fees and slippage per fill, all from config.yaml (CLAUDE.md rule 4).

Costs are in basis points of the fill's notional. Both are charged on the unslipped fill price
(the bar open) so the price-spread component stays free of costs and the zero-cost benchmark only
drops these two components.
"""

from __future__ import annotations

VENUES = ("hyperliquid", "binance")


def fee_bps(cfg: dict, venue: str) -> float:
    """Fee for one fill on `venue` at the order type set in `execution` (taker by default)."""
    order_type = cfg["execution"][f"{venue}_leg"]
    return float(cfg["fees"][venue][f"{order_type}_bps"])


def slippage_bps(cfg: dict, coin: str) -> float:
    s = cfg["slippage"]
    return float(s["majors_bps"] if coin in s["majors"] else s["others_bps"])


def fill_costs(cfg: dict, coin: str, venue: str, qty: float, price: float) -> tuple[float, float]:
    """(fee, slippage) in dollars, both positive, for one fill of `qty` coins at `price`."""
    notional = abs(qty) * price
    return notional * fee_bps(cfg, venue) / 1e4, notional * slippage_bps(cfg, coin) / 1e4


def round_trip_cost_bps(cfg: dict, coin: str) -> float:
    """Fees + slippage for one round trip: open and close both legs (four fills), in bps of
    one leg's notional, using the execution legs, fees and slippage in config.yaml."""
    s = slippage_bps(cfg, coin)
    return 2 * (fee_bps(cfg, "hyperliquid") + s) + 2 * (fee_bps(cfg, "binance") + s)
