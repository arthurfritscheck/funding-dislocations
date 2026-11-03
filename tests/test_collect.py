import pandas as pd
import pytest

from src.collect import hyperliquid as hl
from src.config import day_start_ms

DAY = 86_400_000
HOUR = 3_600_000


@pytest.mark.network
def test_hyperliquid_btc_known_day_has_24_funding_rows():
    # 2025-01-15 is inside the design period. One funding row per hour.
    start = day_start_ms("2025-01-15")
    df = hl.fetch("funding", "BTC", start, start + DAY)
    assert len(df) == 24
    assert df["time"].is_unique
    assert ((df["time"] // HOUR) * HOUR).nunique() == 24  # one row per distinct hour


class FakeClient:
    """Serves synthetic hourly funding rows the way fundingHistory pages them."""

    def __init__(self, start_ms: int, n_rows: int, page_limit: int = 500):
        self.rows = [{"coin": "BTC", "fundingRate": "0.0000125", "premium": "0.0",
                      "time": start_ms + i * HOUR + 7} for i in range(n_rows)]  # +7 ms jitter
        self.page_limit = page_limit
        self.calls = 0

    def post(self, payload, items_per_weight=None):
        self.calls += 1
        lo, hi = payload["startTime"], payload["endTime"]
        hits = [r for r in self.rows if lo <= r["time"] <= hi]
        return hits[: self.page_limit]


def test_fetch_paginates_until_short_page():
    start = day_start_ms("2025-01-01")
    client = FakeClient(start, 1200)
    df = hl.fetch("funding", "BTC", start, start + 1200 * HOUR, client)
    assert len(df) == 1200
    assert df["time"].is_unique
    assert client.calls == 3  # pages of 500, 500, 200


def test_fetch_is_half_open():
    start = day_start_ms("2025-01-01")
    client = FakeClient(start, 100)
    df = hl.fetch("funding", "BTC", start + 10 * HOUR, start + 20 * HOUR, client)
    assert len(df) == 10


def test_update_is_idempotent_and_fills_ends(tmp_path):
    start = day_start_ms("2025-01-01")
    client = FakeClient(start, 300)
    # First run stores only the middle 100 hours.
    s1 = hl.update("funding", "BTC", start + 100 * HOUR, start + 200 * HOUR, client, tmp_path)
    assert s1["after"] == 100
    # Rerun of the full range fills both ends; a second rerun changes nothing.
    s2 = hl.update("funding", "BTC", start, start + 300 * HOUR, client, tmp_path)
    s3 = hl.update("funding", "BTC", start, start + 300 * HOUR, client, tmp_path)
    assert s2["after"] == 300 and s3["after"] == 300
    stored = pd.read_parquet(hl.raw_path("funding", "BTC", tmp_path))
    assert stored["time"].is_unique and len(stored) == 300
    assert "fetched_at" in stored.columns


def test_merge_keeps_latest_fetch_on_duplicates():
    old = pd.DataFrame({"s": ["BTC", "BTC"], "t": [0, 1], "c": ["1.0", "2.0"], "fetched_at": [10, 10]})
    new = pd.DataFrame({"s": ["BTC"], "t": [1], "c": ["2.5"], "fetched_at": [20]})
    out = hl.merge(old, new, "candles_1h")
    assert list(out["t"]) == [0, 1]
    assert list(out["c"]) == ["1.0", "2.5"]
