"""Test for robustness.liquidated_split (hand-computed values)."""

import numpy as np
import pandas as pd

from src.analysis import robustness as R


def test_liquidated_split():
    # Liquidations at hours 10 (-50) and 30 (-20); other trades +5 and +7 (one still open).
    t = pd.DataFrame({"exit_reason": ["liquidation", "below_exit", "liquidation", "open_at_end"],
                      "exit_hour": [10, 20, 30, np.nan], "total": [-50.0, 5.0, -20.0, 7.0]})
    assert R.liquidated_split(t) == dict(liquidated_trades=2, pnl_liquidated_trades=-70.0,
                                         pnl_other_trades=12.0)
    # Window [25, 40): only the second liquidation; the first counts with the others.
    assert R.liquidated_split(t, 25, 40) == dict(liquidated_trades=1, pnl_liquidated_trades=-20.0,
                                                 pnl_other_trades=-38.0)
