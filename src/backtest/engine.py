"""Hourly backtest engine for the funding-gap trade (docs/plan.md Phase 4, DECISIONS.md).

A plain loop over UTC hours with one position slot per coin and a small state machine:

    FLAT --(entry decided at a bar close)--> pending entry --(fill at next bar open)--> OPEN
    OPEN --(exit, spread stop or liquidation decided)--> CLOSING --(fill at next bar open)--> FLAT

Inputs, one frame per coin with one row per hour H (the hour [H, H+1h)):
  f_hl, f_bn       realised funding (hourly-equivalent; NaN = missing, books zero)
  bn_interval_h    Binance interval covering the hour; bn_settle: a settlement happens at H+1h
  signal           trailing mean of the known-at-H gap, APR (usable for a decision at time H)
  ref_px           Binance 1h close at H+1h, the proxy for HL oracle and Binance mark prices
  bar_start        a price bar starts at H; o_hl, o_bn = both venues' opens at H
  bar_end          a price bar ends at H+1h; h_*, l_*, c_* = that bar's high, low and close
  mh_*, ml_*       optional mark-price high/low over the bar (at bar_end rows) for liquidation
                   checks: Binance mark klines for the BN leg, Binance index klines as the proxy
                   for Hyperliquid's oracle-based mark price for the HL leg (DECISIONS.md
                   2026-11-08). Missing -> the check falls back to the trade high/low (counted).
Bars are Hyperliquid candles (4h before its hourly history starts, 1h after); Binance 1h klines
are aggregated over the same span. Hours not covered by a Hyperliquid candle have no boundary:
funding still accrues there, but nothing is decided or filled.

Order of events for each coin and hour H:
  1. bar starts at H: fill pending orders at the opens; credit collateral transfers that arrived.
  2. funding for hour H: Hyperliquid every hour a leg is held; Binance only if it settles at H+1h.
  3. bar ends at H+1h: leg liquidation on each venue's own high/low, mark to market at the closes,
     collateral rebalance, then the policy decides for the next boundary at T = H+1h using
     signal[T] (known at T) and the closes at T. Fills happen at the open of the bar starting at T.

P&L components per trade (they sum to the trade total exactly):
  hl_funding, bn_funding, price_spread (legs at unslipped fill prices), fees, slippage,
  transfer_fees, liquidation (penalty beyond the liquidation price).
"""

from __future__ import annotations

import argparse
import copy
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.analysis.gap_stats import APR
from src.backtest import costs
from src.clean.align import _closed, _load
from src.clean.normalise import HOUR
from src.config import ROOT, load_config, period_ms

DAY = 24 * HOUR
COMPONENTS = ["hl_funding", "bn_funding", "price_spread", "fees", "slippage", "transfer_fees",
              "liquidation"]
STOP_REASONS = ("spread_stop", "liquidation")
INPUT_COLUMNS = ["hour", "f_hl", "f_bn", "bn_interval_h", "bn_settle", "gap", "signal", "ref_px",
                 "bar_start", "bar_end", "o_hl", "o_bn", "h_hl", "l_hl", "c_hl", "h_bn", "l_bn",
                 "c_bn"]
MARK_COLUMNS = ["mh_hl", "ml_hl", "mh_bn", "ml_bn"]
VARIANTS = {"as_specified": {"liquidation_price_source": "last_trade",
                             "rebalance_trigger_base": "initial_margin"},
            "corrected": {"liquidation_price_source": "mark",
                          "rebalance_trigger_base": "current_requirement"}}


# --- Inputs --------------------------------------------------------------------------------

def signal(gap: pd.Series, window_days: float) -> pd.Series:
    """Trailing mean of the known gap over window_days x 24 rows, in APR; NaN until full."""
    n = int(round(window_days * 24))
    return gap.rolling(n, min_periods=n).mean() * APR


def coin_inputs(panel: pd.DataFrame, hl_1h: pd.DataFrame, hl_4h: pd.DataFrame,
                bn_1h: pd.DataFrame, hl_unit: float, bn_unit: float, window_days: float,
                bn_mark: pd.DataFrame | None = None,
                bn_index: pd.DataFrame | None = None) -> pd.DataFrame:
    """Engine input frame for one coin from its panel rows and raw candles. `bn_mark` and
    `bn_index` are Binance 1h mark- and index-price klines (same columns as klines)."""
    p = panel.sort_values("hour").reset_index(drop=True)
    hours = p["hour"].to_numpy("int64")
    start, end, n = int(hours[0]), int(hours[-1]) + HOUR, len(hours)
    h1, h4, b1 = _closed(hl_1h, "T"), _closed(hl_4h, "T"), _closed(bn_1h, "close_time")
    first_1h = int(h1["t"].min()) if not h1.empty else end

    d = pd.DataFrame({"hour": hours, "f_hl": p["f_hl_realised"], "f_bn": p["f_bn_realised"],
                      "bn_interval_h": p["bn_interval_h"], "bn_settle": p["bn_settle"].astype(bool),
                      "gap": p["gap"]})
    d["signal"] = signal(d["gap"], window_days)

    def by_hour(df, key, col, unit):
        a = np.full(n, np.nan)
        if df.empty:
            return a
        t = df[key].astype("int64").to_numpy()
        ok = (t >= start) & (t < end)
        a[(t[ok] - start) // HOUR] = df[col].astype(float).to_numpy()[ok] / unit
        return a

    b1 = b1.drop_duplicates("open_time", keep="last")
    bo, bh, bl, bc = (by_hour(b1, "open_time", c, bn_unit) for c in ("open", "high", "low", "close"))
    d["ref_px"] = pd.Series(bc).ffill().to_numpy()
    marks = {}
    for name, df in (("bn", bn_mark), ("hl", bn_index)):
        df = pd.DataFrame() if df is None else df
        if not df.empty:
            df = _closed(df, "close_time").drop_duplicates("open_time", keep="last")
        marks[name] = (by_hour(df, "open_time", "high", bn_unit),
                       by_hour(df, "open_time", "low", bn_unit))

    for c in ("o_hl", "o_bn", "h_hl", "l_hl", "c_hl", "h_bn", "l_bn", "c_bn"):
        d[c] = np.nan
    d["bar_start"] = False
    d["bar_end"] = False
    bars = []
    for df, length, keep in ((h4, 4, lambda t: t + 4 * HOUR <= first_1h), (h1, 1, lambda t: t >= first_1h)):
        if df.empty:
            continue
        df = df.drop_duplicates("t", keep="last")
        for t, o, h, l, c in df[["t", "o", "h", "l", "c"]].itertuples(index=False):
            t = int(t)
            if t >= start and t + length * HOUR <= end and keep(t):
                bars.append((t, length, float(o) / hl_unit, float(h) / hl_unit,
                             float(l) / hl_unit, float(c) / hl_unit))
    cols = {c: d[c].to_numpy().copy() for c in ("o_hl", "o_bn", "h_hl", "l_hl", "c_hl", "h_bn",
                                                 "l_bn", "c_bn", "bar_start", "bar_end")}
    cols.update({c: np.full(n, np.nan) for c in MARK_COLUMNS})
    for t, length, o, h, l, c in sorted(bars):
        i, j = (t - start) // HOUR, (t - start) // HOUR + length - 1
        cols["bar_start"][i], cols["bar_end"][j] = True, True
        cols["o_hl"][i], cols["o_bn"][i] = o, bo[i]
        cols["h_hl"][j], cols["l_hl"][j], cols["c_hl"][j] = h, l, c
        seg_h, seg_l = bh[i:j + 1], bl[i:j + 1]
        cols["h_bn"][j] = np.nanmax(seg_h) if np.isfinite(seg_h).any() else np.nan
        cols["l_bn"][j] = np.nanmin(seg_l) if np.isfinite(seg_l).any() else np.nan
        cols["c_bn"][j] = bc[j]
        for name, (mh, ml) in marks.items():
            seg_h, seg_l = mh[i:j + 1], ml[i:j + 1]
            if np.isfinite(seg_h).any():
                cols[f"mh_{name}"][j] = np.nanmax(seg_h)
            if np.isfinite(seg_l).any():
                cols[f"ml_{name}"][j] = np.nanmin(seg_l)
    for c, v in cols.items():
        d[c] = v
    return d[INPUT_COLUMNS + MARK_COLUMNS]


def load_inputs(cfg: dict, window_days: float | None = None,
                period: str = "design") -> dict[str, pd.DataFrame]:
    """Engine inputs for every universe coin, from data/clean/panel.parquet and raw candles.

    Only the design period is available before Phase 5 (CLAUDE.md rule 3): raw files are cut at
    the period end on load, and the panel itself is built for the design period only.
    """
    if period != "design":
        raise ValueError("only the design period is allowed before Phase 5 (CLAUDE.md rule 3)")
    window_days = window_days or cfg["strategy"]["signal_window_days"]
    _, end_ms = period_ms(cfg, period)
    panel = pd.read_parquet(ROOT / cfg["paths"]["clean"] / "panel.parquet")
    panel = panel[panel["hour"] < end_ms]
    raw = ROOT / cfg["paths"]["raw"]
    out = {}
    for coin in cfg["universe"]["coins"]:
        sym = cfg["symbols"][coin]
        hl = lambda ds: _load(raw / "hyperliquid" / ds / f"{coin}.parquet", "t", end_ms)
        bn = lambda ds: _load(raw / "binance" / ds / f"{coin}.parquet", "open_time", end_ms)
        out[coin] = coin_inputs(panel[panel["coin"] == coin], hl("candles_1h"), hl("candles_4h"),
                                bn("klines_1h"), sym["hl_unit"], sym["bn_unit"], window_days,
                                bn_mark=bn("mark_price_1h"), bn_index=bn("index_price_1h"))
    return out


def with_signal(inputs: dict[str, pd.DataFrame], window_days: float) -> dict[str, pd.DataFrame]:
    """Copy of the inputs with the signal recomputed over another window (robustness)."""
    out = {}
    for coin, d in inputs.items():
        d = d.copy()
        d["signal"] = signal(d["gap"], window_days)
        out[coin] = d
    return out


def zero_cost(cfg: dict) -> dict:
    """Copy of cfg with all fees and slippage set to zero (benchmark)."""
    c = copy.deepcopy(cfg)
    for v in costs.VENUES:
        c["fees"][v] = {k: 0.0 for k in c["fees"][v]}
    c["slippage"]["majors_bps"] = c["slippage"]["others_bps"] = 0.0
    return c


def variant_cfg(cfg: dict, variant: str) -> dict:
    """Copy of cfg with the model settings of a named variant: "as_specified" (liquidation on
    last-trade highs/lows, rebalance trigger on initial margin) or "corrected" (mark prices,
    current margin requirement). Nothing else changes."""
    c = copy.deepcopy(cfg)
    c["margin"].update(VARIANTS[variant])
    return c


# --- Positions -----------------------------------------------------------------------------

@dataclass
class Leg:
    venue: str
    side: int            # +1 long, -1 short
    entry: float         # unslipped fill price
    margin0: float       # initial margin = qty x entry / leverage
    cash: float = 0.0    # funding, fees, slippage and arrived transfers booked to this margin
    alive: bool = True
    exit: float = math.nan

    def equity(self, qty: float, price: float) -> float:
        return self.margin0 + self.cash + self.side * qty * (price - self.entry)


@dataclass
class Position:
    coin: str
    side: int            # +1: short Hyperliquid, long Binance; -1: the reverse
    qty: float
    entry_hour: int
    hl: Leg
    bn: Leg
    entry_spread: float  # ln(o_hl / o_bn) at entry
    state: str = "open"  # open | closing
    comp: dict = field(default_factory=lambda: dict.fromkeys(COMPONENTS, 0.0))
    exit_hour: int | None = None
    exit_reason: str | None = None
    transfer: tuple | None = None   # (receiving leg, amount, arrival ms)
    n_rebalances: int = 0
    hl_hours: int = 0

    @property
    def legs(self):
        return (self.hl, self.bn)


@dataclass
class SlotState:
    pos: Position | None = None
    pending: tuple | None = None    # ("enter", side) or ("exit", reason)
    armed: bool = True
    last_side: int | None = None
    c_hl: float = math.nan          # last known closes, for marking to market
    c_bn: float = math.nan


# --- Policies (what to do at a decision time) ----------------------------------------------

class SignalPolicy:
    """Baseline rule (DECISIONS.md 2026-11-04/08): enter when |signal| > theta_in, short the
    higher-funding venue; exit when |signal| < theta_out or the sign flips; spread stop; after a
    stop, re-arm only once the signal meets the exit rule."""

    def __init__(self, cfg: dict, theta_in: float | None = None, theta_out: float | None = None):
        s = cfg["strategy"]
        self.theta_in = s["theta_in_apr"] if theta_in is None else theta_in
        self.theta_out = s["theta_out_apr"] if theta_out is None else theta_out
        self.stop = s["spread_stop_frac"]
        self.sign_exit = s["exit_on_sign_change"]

    def exit(self, coin, pos: Position, sig: float, t: int, c_hl: float, c_bn: float):
        if self.stop is not None and np.isfinite(c_hl) and np.isfinite(c_bn):
            if pos.side * (math.log(c_hl / c_bn) - pos.entry_spread) >= self.stop - 1e-12:
                return "spread_stop"
        if not np.isfinite(sig):
            return None
        if abs(sig) < self.theta_out:
            return "below_exit"
        if self.sign_exit and np.sign(sig) != pos.side:
            return "sign_change"
        return None

    def entry(self, coin, st: SlotState, sig: float, t: int) -> int:
        if not np.isfinite(sig):
            return 0
        if not st.armed and (abs(sig) < self.theta_out
                             or (st.last_side is not None and np.sign(sig) != st.last_side)):
            st.armed = True
        return int(np.sign(sig)) if st.armed and abs(sig) > self.theta_in else 0


class AlwaysOnPolicy:
    """Benchmark: hold a fixed direction in every coin; re-enter after a forced close."""

    def __init__(self, side: int = 1):
        self.side = side

    def exit(self, *args):
        return None

    def entry(self, coin, st, sig, t) -> int:
        return self.side


class SchedulePolicy:
    """Benchmark: enter and exit at given decision times. schedule[coin] = [(entry_t, exit_t,
    side), ...], non-overlapping and sorted."""

    def __init__(self, schedule: dict[str, list[tuple[int, int, int]]]):
        self.schedule = {c: list(v) for c, v in schedule.items()}
        self.current: dict[str, tuple] = {}

    def exit(self, coin, pos, sig, t, c_hl, c_bn):
        cur = self.current.get(coin)
        return "scheduled" if cur is not None and t >= cur[1] else None

    def entry(self, coin, st, sig, t) -> int:
        q = self.schedule.get(coin, [])
        while q and q[0][0] < t:          # a slot missed (e.g. after a forced close) is dropped
            q.pop(0)
        if q and q[0][0] == t:
            self.current[coin] = q.pop(0)
            return self.current[coin][2]
        return 0


# --- Engine --------------------------------------------------------------------------------

@dataclass
class Result:
    trades: pd.DataFrame
    daily: pd.DataFrame
    counters: dict
    capital: float


def total_capital(cfg: dict) -> float:
    """Initial margin on both legs of every slot plus the idle buffer."""
    s, m = cfg["strategy"], cfg["margin"]
    margin = s["max_concurrent_positions"] * 2 * s["notional_per_position_usd"] / m["leverage_per_leg"]
    return margin * (1 + m["capital_buffer_frac"])


class Engine:
    def __init__(self, cfg: dict, inputs: dict[str, pd.DataFrame], policy):
        self.cfg, self.policy = cfg, policy
        self.coins = list(inputs)
        hours = {c: d["hour"].to_numpy("int64") for c, d in inputs.items()}
        self.hours = hours[self.coins[0]]
        for c in self.coins:
            if not np.array_equal(hours[c], self.hours):
                raise ValueError(f"{c}: hour grid differs from {self.coins[0]}")
        nan = np.full(len(self.hours), np.nan)
        self.a = {c: {k: d[k].to_numpy() if k in d else nan for k in INPUT_COLUMNS + MARK_COLUMNS}
                  for c, d in inputs.items()}
        s, m = cfg["strategy"], cfg["margin"]
        self.notional, self.lev = s["notional_per_position_usd"], m["leverage_per_leg"]
        self.cap = s["max_concurrent_positions"]
        self.mm = m["maintenance_margin_frac"]
        self.penalty = m["forced_close_penalty_bps"] / 1e4
        self.trigger, self.wfee = m["rebalance_trigger_frac"], m["withdrawal_fee_usd"]
        self.delay = m["transfer_delay_hours"] * HOUR
        self.disarm = s.get("rearm_after_stop") == "exit_rule"
        self.use_mark = m.get("liquidation_price_source", "last_trade") == "mark"
        self.trigger_on_current = m.get("rebalance_trigger_base",
                                        "initial_margin") == "current_requirement"
        self.capital = total_capital(cfg)
        self.st = {c: SlotState() for c in self.coins}
        self.cum = dict.fromkeys(COMPONENTS, 0.0)
        self.trades: list[dict] = []
        self.daily: list[dict] = []
        self.n = dict(missing_hl_funding=0, missing_bn_funding=0, deferred_fills=0,
                      liquidations=0, rebalances=0, rebalance_no_surplus=0, spread_stops=0,
                      mark_fallbacks=0)

    # -- booking --
    def _book(self, pos: Position, key: str, amount: float, leg: Leg | None = None):
        pos.comp[key] += amount
        self.cum[key] += amount
        if leg is not None:
            leg.cash += amount

    def _fill_leg(self, pos: Position, leg: Leg, price: float):
        fee, slip = costs.fill_costs(self.cfg, pos.coin, leg.venue, pos.qty, price)
        self._book(pos, "fees", -fee, leg)
        self._book(pos, "slippage", -slip, leg)

    def _close_leg(self, pos: Position, leg: Leg, price: float):
        self._book(pos, "price_spread", leg.side * pos.qty * (price - leg.entry), leg)
        leg.exit, leg.alive = price, False

    # -- events --
    def _fill(self, coin: str, i: int):
        st, a = self.st[coin], self.a[coin]
        if st.pending is None:
            return
        o_hl, o_bn = a["o_hl"][i], a["o_bn"][i]
        if not (np.isfinite(o_hl) and np.isfinite(o_bn)):
            self.n["deferred_fills"] += 1
            return
        kind, arg = st.pending
        st.pending = None
        h = int(self.hours[i])
        if kind == "enter":
            s = arg
            q = self.notional / ((o_hl + o_bn) / 2)
            pos = Position(coin, s, q, h,
                           hl=Leg("hyperliquid", -s, o_hl, q * o_hl / self.lev),
                           bn=Leg("binance", s, o_bn, q * o_bn / self.lev),
                           entry_spread=math.log(o_hl / o_bn))
            for leg in pos.legs:
                self._fill_leg(pos, leg, leg.entry)
            st.pos = pos
        else:
            pos = st.pos
            for leg, px in ((pos.hl, o_hl), (pos.bn, o_bn)):
                if leg.alive:
                    self._fill_leg(pos, leg, px)
                    self._close_leg(pos, leg, px)
            pos.exit_reason = pos.exit_reason or arg
            self._finish(coin, h)

    def _finish(self, coin: str, hour: int):
        st = self.st[coin]
        pos = st.pos
        pos.exit_hour = hour
        self.trades.append(self._record(pos))
        st.pos, st.last_side = None, pos.side

    def _record(self, pos: Position, unrealised: float = 0.0) -> dict:
        r = dict(coin=pos.coin, side=pos.side, qty=pos.qty, entry_hour=pos.entry_hour,
                 exit_hour=pos.exit_hour, exit_reason=pos.exit_reason,
                 entry_hl=pos.hl.entry, entry_bn=pos.bn.entry, exit_hl=pos.hl.exit,
                 exit_bn=pos.bn.exit, margin0=pos.hl.margin0 + pos.bn.margin0,
                 n_rebalances=pos.n_rebalances, hl_hours=pos.hl_hours, **pos.comp,
                 price_unrealised=unrealised)
        r["total"] = sum(pos.comp.values()) + unrealised
        return r

    def _credit(self, coin: str, h: int):
        pos = self.st[coin].pos
        if pos is not None and pos.transfer is not None and pos.transfer[2] <= h:
            leg, amount, _ = pos.transfer
            leg.cash += amount
            pos.transfer = None

    def _funding(self, coin: str, i: int):
        pos = self.st[coin].pos
        if pos is None:
            return
        a = self.a[coin]
        px = a["ref_px"][i]
        if pos.hl.alive:
            pos.hl_hours += 1
            f = a["f_hl"][i]
            if np.isfinite(f) and np.isfinite(px):
                self._book(pos, "hl_funding", -pos.hl.side * f * pos.qty * px, pos.hl)
            else:
                self.n["missing_hl_funding"] += 1
        if pos.bn.alive and a["bn_settle"][i]:
            f, interval = a["f_bn"][i], a["bn_interval_h"][i]
            if np.isfinite(f) and np.isfinite(interval) and np.isfinite(px):
                self._book(pos, "bn_funding", -pos.bn.side * f * interval * pos.qty * px, pos.bn)
            else:
                self.n["missing_bn_funding"] += 1

    def _liquidate(self, pos: Position, leg: Leg, mm: float):
        """Close a leg where its equity reaches maintenance, plus the penalty."""
        k = (leg.margin0 + leg.cash) / pos.qty
        p_liq = (leg.side * leg.entry - k) / (leg.side - mm)
        self._close_leg(pos, leg, p_liq)
        self._book(pos, "liquidation", -pos.qty * p_liq * self.penalty, leg)
        self.n["liquidations"] += 1

    def _adverse(self, a: dict, key: str, side: int, i: int) -> float:
        """The leg's adverse price extreme in the bar: high for a short, low for a long; on mark
        prices when configured, falling back to the trade candle if the mark is missing."""
        hi_lo = "h" if side < 0 else "l"
        if self.use_mark:
            ext = a[f"m{hi_lo}_{key}"][i]
            if np.isfinite(ext):
                return ext
            self.n["mark_fallbacks"] += 1
        return a[f"{hi_lo}_{key}"][i]

    def _bar_end(self, coin: str, i: int):
        st, a = self.st[coin], self.a[coin]
        pos = st.pos
        h_end = int(self.hours[i]) + HOUR
        if pos is not None and pos.state == "open":
            hit = False
            for leg, key in ((pos.hl, "hl"), (pos.bn, "bn")):
                ext = self._adverse(a, key, leg.side, i) if leg.alive else np.nan
                mm = self.mm[leg.venue][coin]
                if leg.alive and np.isfinite(ext) and leg.equity(pos.qty, ext) <= mm * pos.qty * ext:
                    self._liquidate(pos, leg, mm)
                    hit = True
            if hit:
                pos.state, pos.exit_reason = "closing", "liquidation"
                if self.disarm:
                    st.armed = False
                if pos.hl.alive or pos.bn.alive:
                    st.pending = ("exit", "liquidation")
                else:
                    self._finish(coin, h_end)
                    pos = None
        if np.isfinite(a["c_hl"][i]):
            st.c_hl = a["c_hl"][i]
        if np.isfinite(a["c_bn"][i]):
            st.c_bn = a["c_bn"][i]
        pos = st.pos
        if pos is not None and pos.state == "open" and pos.transfer is None:
            self._rebalance(pos, a["c_hl"][i], a["c_bn"][i], h_end)
        if i + 1 >= len(self.hours):
            return
        t, sig = int(self.hours[i + 1]), a["signal"][i + 1]
        if pos is not None and pos.state == "open":
            reason = self.policy.exit(coin, pos, sig, t, a["c_hl"][i], a["c_bn"][i])
            if reason:
                pos.state, st.pending = "closing", ("exit", reason)
                if reason == "spread_stop":
                    self.n["spread_stops"] += 1
                    if self.disarm:
                        st.armed = False
        elif pos is None and st.pending is None:
            side = self.policy.entry(coin, st, sig, t)
            active = sum(s.pos is not None or (s.pending is not None and s.pending[0] == "enter")
                         for s in self.st.values())
            if side and active < self.cap:
                st.pending = ("enter", side)

    def _rebalance(self, pos: Position, c_hl: float, c_bn: float, h_end: int):
        if not (np.isfinite(c_hl) and np.isfinite(c_bn)):
            return
        eq = {"hl": pos.hl.equity(pos.qty, c_hl), "bn": pos.bn.equity(pos.qty, c_bn)}
        for low, high in (("hl", "bn"), ("bn", "hl")):
            leg_low, leg_high = getattr(pos, low), getattr(pos, high)
            price = c_hl if low == "hl" else c_bn
            base = (pos.qty * price / self.lev) if self.trigger_on_current else leg_low.margin0
            if eq[low] < self.trigger * base:
                x = (eq[high] - eq[low]) / 2
                if x <= self.wfee:
                    self.n["rebalance_no_surplus"] += 1
                    return
                leg_high.cash -= x
                pos.transfer = (leg_low, x - self.wfee, h_end + self.delay)
                self._book(pos, "transfer_fees", -self.wfee)
                pos.n_rebalances += 1
                self.n["rebalances"] += 1
                return

    def _snapshot(self, h_end: int):
        unreal, deployed, n_open = 0.0, 0.0, 0
        for st in self.st.values():
            pos = st.pos
            if pos is None:
                continue
            n_open += 1
            deployed += pos.hl.margin0 + pos.bn.margin0
            unreal += self._unrealised(pos, st)
        realised = sum(self.cum.values())
        self.daily.append(dict(day_end=h_end, equity=self.capital + realised + unreal,
                               deployed=deployed, open_positions=n_open,
                               price_unrealised=unreal, **self.cum))

    @staticmethod
    def _unrealised(pos: Position, st: SlotState) -> float:
        u = 0.0
        for leg, c in ((pos.hl, st.c_hl), (pos.bn, st.c_bn)):
            if leg.alive and np.isfinite(c):
                u += leg.side * pos.qty * (c - leg.entry)
        return u

    def run(self) -> Result:
        coins, a = self.coins, self.a
        for i, h in enumerate(self.hours):
            h = int(h)
            for c in coins:
                if a[c]["bar_start"][i]:
                    self._fill(c, i)
                    self._credit(c, h)
                self._funding(c, i)
                if a[c]["bar_end"][i]:
                    self._bar_end(c, i)
            if (h + HOUR) % DAY == 0 or i == len(self.hours) - 1:
                self._snapshot(h + HOUR)
        for c in coins:
            st = self.st[c]
            if st.pos is not None:
                st.pos.exit_reason = st.pos.exit_reason or "open_at_end"
                self.trades.append(self._record(st.pos, self._unrealised(st.pos, st)))
        trades = pd.DataFrame(self.trades)
        return Result(trades, pd.DataFrame(self.daily), dict(self.n), self.capital)


def run(cfg: dict, inputs: dict[str, pd.DataFrame], policy=None) -> Result:
    """Run one backtest; the baseline SignalPolicy unless another policy is given."""
    return Engine(cfg, inputs, policy or SignalPolicy(cfg)).run()


# --- Benchmarks ----------------------------------------------------------------------------

def decision_times(d: pd.DataFrame) -> np.ndarray:
    """Times at which a decision can be taken (bar ends), with a finite signal."""
    idx = np.flatnonzero(d["bar_end"].to_numpy())
    idx = idx[idx + 1 < len(d)]
    idx = idx[np.isfinite(d["signal"].to_numpy()[idx + 1])]
    return d["hour"].to_numpy("int64")[idx] + HOUR


def random_schedule(inputs: dict[str, pd.DataFrame], trades: pd.DataFrame,
                    rng: np.random.Generator) -> dict[str, list[tuple[int, int, int]]]:
    """Same number of trades, holding periods and sides per coin as `trades`, at random
    non-overlapping decision times within the span where the signal exists."""
    sched = {}
    for coin, d in inputs.items():
        tr = trades[trades["coin"] == coin] if len(trades) else trades
        times = decision_times(d)
        if tr.empty or len(times) == 0:
            sched[coin] = []
            continue
        end = np.where(tr["exit_hour"].notna(), tr["exit_hour"], times[-1]).astype("int64")
        dur = end - tr["entry_hour"].to_numpy("int64")
        sides = tr["side"].to_numpy()
        order = rng.permutation(len(dur))
        free = max(times[-1] - times[0] - int(dur.sum()), 0)
        gaps = np.diff(np.concatenate([[0], np.sort(rng.uniform(0, free, len(dur)))]))
        out, t = [], times[0]
        for g, j in zip(gaps, order):
            k = np.searchsorted(times, t + g)
            if k >= len(times):
                break
            entry = int(times[k])
            k2 = min(np.searchsorted(times, entry + dur[j]), len(times) - 1)
            exit_ = int(times[k2])
            out.append((entry, exit_, int(sides[j])))
            t = exit_ + HOUR
        sched[coin] = out
    return sched


# --- CLI -----------------------------------------------------------------------------------

PREFIX = {"as_specified": "backtest_", "corrected": "backtest_corrected_"}


def write_comparison(out) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Side-by-side tables of the as-specified and corrected runs from their saved CSVs."""
    runs = ["strategy", "zero_cost", "always_on"]
    summ, dec = {}, {}
    for variant, prefix in PREFIX.items():
        s = pd.read_csv(out / f"{prefix}summary.csv", index_col=0)
        d = pd.read_csv(out / f"{prefix}decomposition_benchmarks.csv", index_col=0)
        for r in s.columns:
            summ[(variant, r)] = s[r]
        for r in runs:
            dec[(variant, r)] = d[r]
    summ, dec = pd.DataFrame(summ), pd.DataFrame(dec)
    summ.to_csv(out / "backtest_comparison_summary.csv")
    dec.to_csv(out / "backtest_comparison_decomposition.csv")
    return summ, dec


def main(argv: list[str] | None = None) -> None:
    from src.backtest import metrics

    p = argparse.ArgumentParser()
    p.add_argument("--period", default="design", choices=["design"],
                   help="only the design period is allowed before Phase 5 (CLAUDE.md rule 3)")
    p.add_argument("--variant", default="corrected", choices=sorted(VARIANTS),
                   help="model variant (DECISIONS.md 2026-11-08); outputs are prefixed per variant")
    p.add_argument("--seeds", type=int, default=None, help="random-entry seeds (default: config)")
    p.add_argument("--force", action="store_true",
                   help="allow overwriting the saved as-specified tables")
    args = p.parse_args(argv)
    out = ROOT / "report" / "tables"
    out.mkdir(parents=True, exist_ok=True)
    prefix = PREFIX[args.variant]
    if args.variant == "as_specified" and (out / f"{prefix}summary.csv").exists() and not args.force:
        raise SystemExit("as-specified tables exist and are kept as the record; use --force")
    cfg = variant_cfg(load_config(), args.variant)
    inputs = load_inputs(cfg, period=args.period)

    base = run(cfg, inputs)
    zc = run(zero_cost(cfg), inputs)
    side = 1 if cfg["benchmarks"]["always_on_direction"] == "short_hl" else -1
    always = run(cfg, inputs, AlwaysOnPolicy(side))
    seeds = args.seeds if args.seeds is not None else cfg["benchmarks"]["random_entry_seeds"]
    rand, rand_dec = [], []
    for seed in range(seeds):
        sched = random_schedule(inputs, base.trades, np.random.default_rng(seed))
        r = run(cfg, inputs, SchedulePolicy(sched))
        rand.append(metrics.summary(r).rename(seed))
        rand_dec.append(metrics.decomposition(r.trades).loc["ALL"].rename(seed))
    rand = pd.DataFrame(rand)

    summary = pd.DataFrame({"strategy": metrics.summary(base), "zero_cost": metrics.summary(zc),
                            "always_on": metrics.summary(always)})
    if len(rand):
        summary["random_mean"] = rand.mean()
        summary["random_p05"] = rand.quantile(0.05)
        summary["random_p95"] = rand.quantile(0.95)
    base.trades.to_csv(out / f"{prefix}trades.csv", index=False)
    base.daily.to_csv(out / f"{prefix}daily.csv", index=False)
    summary.to_csv(out / f"{prefix}summary.csv")
    metrics.decomposition(base.trades).to_csv(out / f"{prefix}decomposition.csv")
    dec = pd.DataFrame({"strategy": metrics.decomposition(base.trades).loc["ALL"],
                        "zero_cost": metrics.decomposition(zc.trades).loc["ALL"],
                        "always_on": metrics.decomposition(always.trades).loc["ALL"]})
    if rand_dec:
        dec["random_mean"] = pd.DataFrame(rand_dec).mean()
    dec.to_csv(out / f"{prefix}decomposition_benchmarks.csv")
    rand.to_csv(out / f"{prefix}random_entry.csv", index_label="seed")

    for name, r in (("strategy", base), ("zero_cost", zc), ("always_on", always)):
        print(name, metrics.sanity(r, inputs))
    print(summary.round(4).to_string())
    if all((out / f"{pre}summary.csv").exists() for pre in PREFIX.values()):
        summ, dec = write_comparison(out)
        print(dec.round(0).to_string())


if __name__ == "__main__":
    main()
