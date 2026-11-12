"""Post-hoc transfer cap (report/decisions.md, 2026-11-09), hand-computed."""

import copy

import pytest

from src.backtest import engine as E
from tests.test_backtest import _cfg, _frame

PRICE_COLS = ["h_hl", "l_hl", "c_hl", "h_bn", "l_bn", "c_bn", "o_hl", "o_bn", "ref_px"]


def test_binance_transferable_wallet_and_available_margin():
    # Available margin binds: wallet 3,000, uPnL +1,500, IM 3,800 -> min(3,000, 700) = 700.
    assert E.binance_transferable(3000.0, 1500.0, 3800.0) == pytest.approx(700.0)
    # Wallet binds: wallet 1,000, uPnL +5,000, IM 2,000 -> min(1,000, 4,000) = 1,000
    # (unrealised profit never leaves the account).
    assert E.binance_transferable(1000.0, 5000.0, 2000.0) == pytest.approx(1000.0)
    # Loss: wallet 3,000, uPnL -500, IM 3,000 -> min(3,000, -500) -> 0.
    assert E.binance_transferable(3000.0, -500.0, 3000.0) == 0.0


def test_hyperliquid_transferable_initial_margin_or_ten_percent():
    # 3x: IM = 12,000 / 3 = 4,000 > 10% (1,200); equity 5,000 -> 1,000.
    assert E.hyperliquid_transferable(5000.0, 12000.0, 3, 0.10) == pytest.approx(1000.0)
    # 20x: IM = 600 < 10% = 1,200, so the 10% floor binds; equity 5,000 -> 3,800.
    assert E.hyperliquid_transferable(5000.0, 12000.0, 20, 0.10) == pytest.approx(3800.0)
    # Below the requirement: equity 3,000 < 4,000 -> 0.
    assert E.hyperliquid_transferable(3000.0, 12000.0, 3, 0.10) == 0.0


def _capped(c):
    c = copy.deepcopy(E.variant_cfg(c, "corrected"))
    c["margin"]["transfer_cap"] = True
    c["margin"]["transfer_cap_hl_min_frac"] = 0.10
    return c


def test_engine_caps_a_binance_leg_transfer():
    # Short HL / long BN (signal +0.2) from row 1 at 100/100, q = 100 (BTC). Row 2 closes at 115.
    # HL equity = 3,333.33 - 6.50 - 1,500 = 1,826.83 < 50% x 100 x 115 / 3 = 1,916.67 -> rebalance
    # from BN. Uncapped x = (4,826.33 - 1,826.83) / 2 = 1,499.75.
    # BN wallet = 3,333.33 - 7.00 = 3,326.33; uPnL +1,500; IM = 100 x 115 / 3 = 3,833.33.
    # Cap = min(3,326.33, 3,326.33 + 1,500 - 3,833.33 = 993.00) = 993.00.
    c = _cfg()
    d = _frame(3, signal=0.2)
    d.loc[2:, PRICE_COLS] = 115.0
    eng = E.Engine(_capped(c), {"BTC": d}, E.SignalPolicy(c))
    eng.run()
    pos = eng.st["BTC"].pos
    assert pos.side == 1 and eng.n["rebalances"] == 1 and eng.n["transfers_capped"] == 1
    assert pos.bn.cash == pytest.approx(-7.0 - 993.0)
    assert pos.transfer[1] == pytest.approx(992.0)          # less the $1 fee


def test_engine_caps_a_hyperliquid_leg_transfer():
    # Long HL / short BN (signal -0.2), same prices. BN equity = 3,333.33 - 7.00 - 1,500 =
    # 1,826.33 < 1,916.67 -> rebalance from HL. HL equity = 3,333.33 - 6.50 + 1,500 = 4,826.83;
    # uncapped x = (4,826.83 - 1,826.33) / 2 = 1,500.25. Requirement = max(100 x 115 / 3 =
    # 3,833.33, 10% x 11,500 = 1,150) = 3,833.33; cap = 4,826.83 - 3,833.33 = 993.50.
    c = _cfg()
    d = _frame(3, signal=-0.2)
    d.loc[2:, PRICE_COLS] = 115.0
    eng = E.Engine(_capped(c), {"BTC": d}, E.SignalPolicy(c))
    eng.run()
    pos = eng.st["BTC"].pos
    assert pos.side == -1 and eng.n["rebalances"] == 1 and eng.n["transfers_capped"] == 1
    assert pos.hl.cash == pytest.approx(-6.5 - 993.5)
    assert pos.transfer[1] == pytest.approx(992.5)


def test_liquidations_by_leg_groups_by_venue_and_side():
    # Side +1 = short HL / long BN. Trade A: HL exit 130 differs from the next open 120 -> the HL
    # leg (short) was liquidated. Trade B: BN exit 80 differs from the next open 90 -> the BN leg
    # (long). Trade C: side -1, HL exit 70 vs open 75 -> HL leg (long). D is not a liquidation.
    import pandas as pd
    from src.analysis import report_checks as RC
    T = 1_735_689_600_000
    inputs = {"BTC": pd.DataFrame({"hour": [T], "o_hl": [120.0], "o_bn": [90.0]}),
              "ETH": pd.DataFrame({"hour": [T], "o_hl": [75.0], "o_bn": [80.0]})}
    trades = pd.DataFrame({
        "coin": ["BTC", "BTC", "ETH", "ETH"], "side": [1, 1, -1, 1],
        "exit_reason": ["liquidation"] * 3 + ["below_exit"], "exit_hour": [T] * 4,
        "exit_hl": [130.0, 120.0, 70.0, 75.0], "exit_bn": [90.0, 80.0, 80.0, 80.0],
        "total": [-100.0, -40.0, -10.0, 5.0], "price_spread": [-60.0, -30.0, -5.0, 2.0]})
    g = RC.liquidations_by_leg(inputs, trades).set_index("leg")
    assert g.loc["hyperliquid short", "liquidated_trades"] == 1
    assert g.loc["hyperliquid short", "total"] == pytest.approx(-100.0)
    assert g.loc["binance long", "price_spread"] == pytest.approx(-30.0)
    assert g.loc["hyperliquid long", "total"] == pytest.approx(-10.0)
    assert g["liquidated_trades"].sum() == 3


def test_cap_off_by_default_leaves_the_transfer_unchanged():
    # Same as the Binance case with the cap off: x = 1,499.75, as in the existing corrected test.
    c = _cfg()
    d = _frame(3, signal=0.2)
    d.loc[2:, PRICE_COLS] = 115.0
    eng = E.Engine(E.variant_cfg(c, "corrected"), {"BTC": d}, E.SignalPolicy(c))
    eng.run()
    assert eng.n["transfers_capped"] == 0
    assert eng.st["BTC"].pos.bn.cash == pytest.approx(-7.0 - 1499.75)
