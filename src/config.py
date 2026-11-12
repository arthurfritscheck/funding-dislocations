"""Load config.yaml and turn its calendar dates into UTC-millisecond bounds."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
HOUR_MS = 3_600_000


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def day_start_ms(day: str) -> int:
    """UTC midnight at the start of an ISO date ('YYYY-MM-DD'), in milliseconds."""
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _bounds(start: str, end: str) -> tuple[int, int]:
    end_exclusive = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    return day_start_ms(start), day_start_ms(end_exclusive)


def period_ms(cfg: dict, name: str) -> tuple[int, int]:
    """Half-open [start_ms, end_ms) for a period whose dates are inclusive calendar days."""
    p = cfg["periods"][name]
    return _bounds(p["start"], p["end"])


def period_bounds(cfg: dict, period: str | None = "design", start: str | None = None,
                  end: str | None = None) -> tuple[str, int, int]:
    """(label, start_ms, end_ms) for a period named in config.yaml, or for explicit inclusive
    ISO dates (`start` and `end` together, label "<start>_to_<end>"), which take precedence."""
    if (start is None) != (end is None):
        raise ValueError("give both start and end dates, or neither")
    if start is not None:
        s, e = _bounds(start, end)
        label = f"{start}_to_{end}"
    else:
        if period not in cfg["periods"]:
            raise ValueError(f"unknown period {period!r}; config.yaml has {sorted(cfg['periods'])}")
        s, e = period_ms(cfg, period)
        label = period
    if e <= s:
        raise ValueError(f"empty period {label}")
    return label, s, e


def check_period_allowed(cfg: dict, end_ms: int, allow_holdout: bool) -> None:
    """Data after the design cut-off is read only with an explicit opt-in
    (`allow_holdout=True`, CLI `--allow-holdout`), i.e. only for the held-out test."""
    if end_ms > period_ms(cfg, "design")[1] and not allow_holdout:
        raise ValueError("this period reaches past the design cut-off; held-out data is read only "
                         "with allow_holdout=True / --allow-holdout")


def warmup_start_ms(cfg: dict, start_ms: int, window_days: float) -> int:
    """Start of the signal warm-up: window_days before start_ms, but never before the sample
    start (no data exists earlier), so the design period has no warm-up."""
    return max(start_ms - int(round(window_days * 24)) * HOUR_MS, period_ms(cfg, "sample")[0])


def output_suffix(label: str) -> str:
    """'' for the design period (existing file names unchanged), else '_<label>'."""
    return "" if label == "design" else f"_{label}"


def report_dir(cfg: dict) -> Path:
    """Directory for tables, figures and the data-quality report (`paths.report`)."""
    return ROOT / cfg["paths"].get("report", "report")
