"""Helpers of the final report figures, on hand-made inputs (no results files read)."""

import numpy as np
import pandas as pd
import pytest

from src.analysis import final_figures as FF
from src.config import day_start_ms

D = 86_400_000


def _row(**kw):
    base = {k: 0.0 for k, _ in FF.COMPONENTS}
    base.update(kw)
    return pd.Series(base)


def test_waterfall_steps_run_from_zero_through_each_component_to_the_total():
    # 100 - 20 - 30 - 10 - 5 - 1 - 4 + 0.5 = 30.5
    row = _row(hl_funding=100, bn_funding=-20, price_spread=-30, fees=-10, slippage=-5,
               transfer_fees=-1, liquidation=-4, price_unrealised=0.5, total=30.5)
    s = FF.waterfall_steps(row)
    assert s["start"].tolist() == [0, 100, 80, 50, 40, 35, 34, 30, 0]
    assert s["end"].tolist() == [100, 80, 50, 40, 35, 34, 30, 30.5, 30.5]
    assert s["kind"].tolist() == ["gain"] + ["loss"] * 6 + ["gain", "total"]
    assert s["label"].iloc[-1] == "Net P&L"


def test_waterfall_refuses_components_that_do_not_add_up():
    with pytest.raises(ValueError):
        FF.waterfall_steps(_row(hl_funding=100, total=99))


def test_period_span_and_cumulative_start_at_zero_on_the_first_day():
    first_end = day_start_ms("2030-01-02")              # end of 2030-01-01
    daily = pd.DataFrame({"day_end": [first_end, first_end + D, first_end + 2 * D],
                          "equity": [100.0, 98.0, 101.0]})
    assert FF.period_span(daily) == ("2030-01-01", "2030-01-03", 3)
    x, y = FF.cumulative(daily, 100.0)
    assert x[0] == pd.Timestamp("2030-01-01") and x[-1] == pd.Timestamp("2030-01-04")
    np.testing.assert_allclose(y, [0, 0, -2, 1])


def test_text_formats():
    assert FF.short_days("2025-10-10, 2025-10-11") == "2025-10-10/11"
    assert FF.short_days("2025-10-31, 2025-11-01") == "2025-10-31 and 2025-11-01"
    assert FF.money(-4270.38) == "−$4,270"
    assert FF.money(497.0) == "+$497"
    assert FF.usd(-1657.12) == "−1,657"
