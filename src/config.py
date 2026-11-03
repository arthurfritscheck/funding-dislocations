"""Load config.yaml and turn its calendar dates into UTC-millisecond bounds."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def day_start_ms(day: str) -> int:
    """UTC midnight at the start of an ISO date ('YYYY-MM-DD'), in milliseconds."""
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def period_ms(cfg: dict, name: str) -> tuple[int, int]:
    """Half-open [start_ms, end_ms) for a period whose dates are inclusive calendar days."""
    p = cfg["periods"][name]
    end_exclusive = (date.fromisoformat(p["end"]) + timedelta(days=1)).isoformat()
    return day_start_ms(p["start"]), day_start_ms(end_exclusive)
