"""Raw coverage honours cleaning.binance_missing_settlements (synthetic data only)."""

import copy

import numpy as np
import pandas as pd

from src.clean import checks
from src.config import day_start_ms, load_config

H = 3_600_000
S = day_start_ms("2030-01-01")


def test_listed_missing_settlement_is_not_suspect(tmp_path):
    # 4h settlements over two days with the 08:00 one on day 2 missing: the 12:00 settlement
    # then measures 8h between two 4h steps (suspect_missing) unless it is listed as checked.
    t = np.arange(S, S + 48 * H, 4 * H, dtype="int64")
    t = t[t != S + 32 * H]
    raw = tmp_path / "binance" / "funding"
    raw.mkdir(parents=True)
    pd.DataFrame({"symbol": "BTCUSDT", "fundingTime": t, "fundingRate": "0.0001",
                  "markPrice": "1", "rateType": "Regular", "fetched_at": S + 100 * H}
                 ).to_parquet(raw / "BTC.parquet", index=False)
    cfg = copy.deepcopy(load_config())
    cfg["cleaning"]["binance_missing_settlements"] = []
    res = checks.coverage_checks(cfg, S + 8 * H, S + 48 * H, coins=["BTC"], raw=tmp_path)
    reg = res.set_index("check").loc["bn_funding_regular", "passed"]
    assert not reg
    cfg["cleaning"]["binance_missing_settlements"] = [
        {"symbols": ["BTCUSDT"], "settle": "2030-01-02T12:00", "interval_h": 4}]
    res = checks.coverage_checks(cfg, S + 8 * H, S + 48 * H, coins=["BTC"], raw=tmp_path)
    assert res.set_index("check").loc["bn_funding_regular", "passed"]
