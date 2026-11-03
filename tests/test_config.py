from src.config import load_config, period_ms

# Hand-computed: 2024-08-01 is day 19936 since the epoch; 2026-07-01 is 699 days later
# (day 20635); 2026-11-01 is 123 days after that (day 20758). One day = 86_400_000 ms.
SAMPLE_START_MS = 19936 * 86_400_000  # 1_722_470_400_000
HOLDOUT_START_MS = 20635 * 86_400_000  # 1_782_864_000_000
SAMPLE_END_MS = 20758 * 86_400_000  # 1_793_491_200_000


def test_period_bounds():
    cfg = load_config()
    assert period_ms(cfg, "sample") == (SAMPLE_START_MS, SAMPLE_END_MS)
    assert period_ms(cfg, "design") == (SAMPLE_START_MS, HOLDOUT_START_MS)
    assert period_ms(cfg, "holdout") == (HOLDOUT_START_MS, SAMPLE_END_MS)


def test_design_and_holdout_tile_the_sample():
    cfg = load_config()
    d_start, d_end = period_ms(cfg, "design")
    h_start, h_end = period_ms(cfg, "holdout")
    s_start, s_end = period_ms(cfg, "sample")
    assert d_start == s_start and d_end == h_start and h_end == s_end


def test_cost_fields_present():
    cfg = load_config()
    for venue in ("hyperliquid", "binance"):
        assert {"taker_bps", "maker_bps"} <= cfg["fees"][venue].keys()
    assert {"majors_bps", "others_bps", "majors"} <= cfg["slippage"].keys()
