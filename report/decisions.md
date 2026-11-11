# Decision log

A dated record of every design choice in this study and the reason for it, oldest first. All
parameters named here are set in the project's configuration file, so a decision is changed in
one place. Dates are when the decision was made; times are UTC.

## 2026-11-04 — Data conventions

- **Hyperliquid funding timestamps.** I treat a `fundingHistory` row stamped T as the funding for
  the hour [T−1h, T), paid to positions held through that hour. A live test of the premium between
  22:00 and 23:00 UTC on 2026-11-03 matched the T row for BTC, ETH and SOL, and historical stamps
  arrive 0–195 ms after the hour (99th percentile, design period). One point stays unresolved:
  `predictedFundings` labels Hyperliquid's accumulating rate with the start of the current hour as
  its `nextFundingTime` (see the limitations log).
- **Signal lag.** On both venues, a funding row stamped T counts as known only from T+1h, because
  rows are published about 30 seconds after T and some stamps are minutes late; publication delay
  can then never leak into a decision.
- **Timestamp alignment.** I round funding stamps to the nearest hour on both venues and flag any
  stamp more than 5 minutes off the hour, instead of rounding to the nearest minute, because two
  design-period Hyperliquid stamps are 1m50s and 14m47s late and nearest-minute rounding would
  leave them off the hourly grid.
- **Missing Hyperliquid funding hour.** The row stamped 2024-08-15 13:00 (the hour 12:00–13:00) is
  absent from the API for every coin. I keep it missing, book zero funding in P&L, carry the last
  value forward for signals and list it in the data-quality report, so that no value is invented
  for P&L.
- **Unclosed candles.** I drop candles that had not closed when they were fetched, because an
  in-progress candle's close and volume are not final.
- **Repository workflow.** I commit directly to the main branch of this single-author repository;
  the `v1-frozen` tag marks the code frozen before the held-out run.
- **Coin universe (15).** BTC, ETH, SOL, kPEPE (1000PEPEUSDT on Binance), SUI, WIF, TIA, DOGE, WLD,
  AAVE, APT, kBONK (1000BONKUSDT), AVAX, TAO and BNB; every other Binance symbol is `<COIN>USDT`. I
  set the rule before looking at funding data: 4h Hyperliquid candles from 2024-08-01, a Binance
  USDT perpetual trading by 2024-08-01 and still trading, and the top 15 by Hyperliquid USD volume
  over August–October 2024, with BTC, ETH and SOL always included. POPCAT (rank 8) failed because
  its Binance perpetual listed on 2024-08-22, so BNB (rank 16) entered.
- **Binance settlement of 2026-06-24 08:00 (WIF, TIA, kBONK, TAO).** I treat it as a 4h interval
  [04:00, 08:00) with the 04:00 settlement missing: zero funding in P&L for 00:00–04:00 and the last
  value carried forward for signals. Other suspected missing settlements are not treated this way
  until checked. The reason: its rate is in line with the surrounding 4h settlements, not doubled.
  It is 0.19–1.00x the median |rate| of the ±48h neighbours and inside their 10th–90th percentile
  range for all four symbols (WIF +0.0000236, TIA +0.0000164, kBONK +0.0000096, TAO +0.0000396),
  where the 4h interest baseline is 0.00005 and an 8h-accrued rate would be about 0.0001.
- **BNB's interest rate on Binance.** I set BNBUSDT's resting rate to 0 per 8h instead of 0.01%, so
  BNB's mechanical gap is +10.95% APR. Binance's own reported `interestRate` (premium-index
  endpoint, snapshot of 2026-11-04) is 0 for BNBUSDT and 0.0001 for the other 14 symbols, and
  BNBUSDT settled at exactly 0 in 64.6% of design-period settlements.
- **Binance fees.** Base-tier USDⓈ-M fees: 5.0 bps taker and 2.0 bps maker (VIP 0, no BNB
  discount), which I confirmed against Binance's fee schedule. With Hyperliquid's 4.5 bps taker
  fee, a four-taker-fill round trip costs about 0.19% of notional, as expected.

## 2026-11-04 — Exploratory analysis and the signal

- **Episodes end on a sign change.** A gap episode also ends when the gap changes sign; if the new
  sign's |gap| is above the threshold, that hour starts a new episode in the other direction,
  because the trade direction flips at a sign change.
- **BNB's raw gap in trading analysis.** Trading-related analysis uses BNB's raw gap, including the
  mechanical +10.95% APR, and reports the net-of-mechanical episodes alongside as description
  only, because the mechanical part is real carry: Binance's interest rate for BNBUSDT is 0 while
  Hyperliquid charges the 0.01%/8h interest component.
- **Half-life intervals.** The half-life chart shows Newey-West (HAC) 95% intervals instead of
  OLS ones, because OLS standard errors understate the uncertainty by 1.3–2.8x (residual
  autocorrelation).
- **Findings of the exploratory analysis (design period).**
  1. Episodes close in a median 3–5 hours; no episode lasts long enough to pay its own round trip
     (27 bps BTC/ETH, 51 bps others) from carry alone.
  2. Hyperliquid funding exceeds Binance's on average for all 15 coins (mean gap 3–11% APR); this
     persistent component is the tradable part.
  3. The price spread moves about ±10 bps during episodes, so the hedge's price leg is a small
     risk.
  4. |gap| is larger with high BTC volatility, large coin moves, rallies and high market-wide
     funding, and smaller for large caps; I do not use these as filters in the baseline, to avoid
     tuning extra parameters.
  5. Gaps co-move (average correlation 0.23 hourly, 0.40 daily), so the universe behaves like
     roughly 2.3–3.5 independent coins.
- **Baseline signal**, replacing the initially planned rule (enter at a 20% APR gap held for 3
  hours):
  - Signal: trailing 7-day mean of the known-at-t gap, per coin.
  - Entry: |signal| > 10% APR, shorting the higher-funding venue and going long the other.
  - Exit: |signal| < 3% APR, or the signal changes sign.
  - No regime filters. 14- and 30-day windows and other thresholds are robustness checks only,
    never used to re-pick parameters.

  The reason: short episodes cannot pay their costs, while the persistent gap can over multi-week
  holding periods.

## 2026-11-08 — Backtest design, fixed before any backtest existed

- **Parameters.** I wrote these down before building the engine, so results could not steer them:
  - Robustness grid: entry 5, 7.5, 10, 15 and 20% APR; exit 0, 1.5, 3 and 5% APR; signal windows of
    7, 14 and 30 days. Checks only, never used to re-pick parameters.
  - No maximum holding period; the exit rule closes positions.
  - Spread stop: close if the price spread moves 100 bps against the position from its entry
    value.
  - Sizing: $10,000 notional per leg, one position per coin, up to 15 at once.
  - Leverage: 3x per leg.
  - Rebalancing: move collateral when a leg's margin falls below 50% of its initial margin, with a
    $1 withdrawal fee and a 2-hour transfer delay.
  - Returns are reported on total capital (margin on both venues plus a buffer).
- **Margin and capital**, needed for the leg-level risk model: maintenance margin per venue and
  coin (Hyperliquid: 1/(2 × maximum leverage); Binance: the effective rate at a $10,000 notional
  from its public bracket table; both snapshots of 2026-11-08, assumed constant); a capital buffer
  of 50% of initial margin, so total capital is 15 × 2 × $10,000 / 3 × 1.5 = $150,000; a liquidated
  leg closes 100 bps worse than its liquidation price, and the other leg closes as a taker at the
  next bar open.
- **Re-arm rule** (a definition change, made before the first run): after a spread stop or a
  forced close, a coin cannot re-enter until its signal has first met the exit rule (|signal| <
  3% APR or a sign change). Without it the 7-day signal is usually still above 10% after a stop,
  so the coin would re-enter at the next bar and pay a second round trip; the rule adds no new
  parameter.
- **Decision clock and funding proxies.** Decisions happen only at bar boundaries: every 4h before
  each coin's first hourly Hyperliquid candle (2026-04-09, 11:00–14:00 UTC by coin) and hourly
  after it, with both legs filled at the open of the bar that starts at the decision time. The
  Binance mark price is proxied by the Binance 1h close at the settlement time, and the Hyperliquid
  oracle price by the Binance 1h close at the end of the funding hour. Hyperliquid has only 4h
  candles in most of the design period, and inventing hourly Hyperliquid fill prices would be worse
  than a decision delay of up to 3 hours against a 7-day signal.

## 2026-11-08 — Model corrections, robustness and the held-out run

- **Two model-fidelity corrections**, decided before seeing their effect, so that the model works
  the way the venues do. The first run is kept as the "as-specified" result.
  1. Liquidation checks use mark prices, not last-trade candles. Binance leg: Binance 1h mark-price
     klines. Hyperliquid leg: Binance 1h index-price klines, as a proxy for Hyperliquid's
     oracle-based mark price. Highs and lows are aggregated over each bar, like the trade candles.
     Both series cover the whole design period for all 15 coins (first klines 2019-12 to 2024-04).
  2. The rebalance trigger follows the current margin requirement (quantity × current price /
     leverage, at the venue's bar close) instead of the initial margin at entry.

  Nothing else changes: strategy parameters, leverage, buffer, costs, rebalance amount and delay,
  the liquidation price formula and the penalty are the same. The reason: the venues liquidate on
  mark price and set margin requirements on current notional, and the as-specified model did
  neither.
- **Robustness specification** (design period only, corrected model), written down before any of
  it was run. None of these results may be used to change the baseline parameters.
  - Cost sensitivity: fees and slippage scaled together by 0.5, 1, 2 and 3x, plus a 4 × 4 grid of
    separate fee and slippage multiples. The break-even multiple is the joint multiple at which
    design-period net P&L is zero, found by bisection on the engine. Transfer fees and the
    liquidation penalty are not scaled.
  - Maker scenario: both legs at maker fees (Hyperliquid 1.5, Binance 2.0 bps) with half the
    slippage, reported separately and never as the headline, since maker fills are not guaranteed
    (legging and adverse-selection risk). The originally planned case was maker on one leg; I
    specified both legs after the first backtest run, so it is labelled a sensitivity added after
    the first run (see below).
  - Entry/exit/window grid: 5 × 4 × 3 = 60 runs, shown as a heat map.
  - Universe: majors (BTC, ETH, SOL) against the other 12 coins, and leave-one-coin-out (15 runs).
    A subset's capital scales with its number of coins ($10,000 of total capital per coin); with
    one position per coin the concurrency cap never binds, so subsets trade exactly as in the full
    run.
  - Time: net P&L by calendar quarter (UTC), a rolling 90-day Sharpe, and a moving-block bootstrap
    of daily P&L with 7-day blocks (10,000 resamples, seed 0, percentile 95% interval).
  - Sensitivities added after the first backtest run (not pre-registered): leverage 1.5x, 2x and
    3x crossed with capital buffers of 0%, 25% and 50% (9 runs), labelled as such in every table.
    In this model the buffer is idle cash, so it changes only the capital base, not P&L.

  Configurations run (counted afterwards): 108 besides the baseline (cost 23 including 7 bisection
  steps, maker 1, grid 59, universe subsets 17, leverage × buffer 8, giving only one P&L per
  leverage). Before that: the baseline in the two model variants, each with zero cost, always-on
  and 200 random-entry seeds.
- **Maker scenario labelled post hoc.** The both-legs maker scenario is labelled "sensitivity added
  after the first backtest run", like the leverage and buffer sensitivities, because the
  pre-registered case was maker on one leg and both legs were specified after the first run had
  been seen.
- **Tail-event sensitivity (added after the first backtest run).** For leverage 1.5x, 2x and 3x
  (buffer 50%; the buffer does not change P&L): design-period P&L with and without the daily P&L
  booked on 2025-10-10 and 2025-10-11 (UTC), and those two days' share of total P&L. Excluding the
  days removes their P&L only; it is not a rerun without them. I added it because the robustness
  run showed that liquidations on those days drive the leverage ranking.
- **Failure cases.** The five worst trades of the corrected baseline by total P&L, each with its
  loss by component and its price, mark and funding paths from 3 days before entry to 3 days after
  exit; I left the cause to be classified after looking at them (done on 2026-11-09, below).
- **Capacity estimate skipped.** Capacity is moot for a strategy that loses money before scaling
  (corrected baseline -$4,270 on the design period), so I report the break-even cost multiple
  (0.55x fees and slippage) instead.
- **Held-out run preparation**, done without computing, printing or plotting any held-out
  statistic, return or gap value:
  - Data: Binance 1h mark-price and index-price klines downloaded up to 2026-10-31 23:00 UTC
    (exclusive end 2026-11-01 00:00) for all 15 coins; I looked only at row counts (+2,952 hours per
    coin). The other held-out raw data was already on disk.
  - Periods: cleaning, the engine with its benchmarks and the raw coverage checks run on any named
    period or on explicit dates. A period reaching past the design cut-off needs an explicit
    opt-in; without it the code refuses.
  - Warm-up: a period's panel starts 7 days (the signal window) before the period start, never
    before the sample start, so the design panel and every design result are unchanged (rebuilt
    panel identical; -$4,270.38 and -$8,871.47 reproduced). The signal is computed over warm-up plus
    period and the engine inputs are then cut to the period, so the held-out run starts flat at
    2026-07-01 00:00 UTC with a 7-day signal built only from design-period data (with the T+1h
    rule, the first decision at 01:00 uses only rows stamped up to 2026-07-01 00:00, which cover
    design-period hours). Raw data at or after the period end is dropped on load, and the engine
    refuses a panel that does not cover the warm-up.
  - Outputs: the design period keeps its file names; any other period's outputs carry its label.
  - Checks before the run: raw-file coverage on the held-out period and its warm-up, reported as
    pass or fail only (timestamps and settlement metadata, no prices or rates); all 8 checks pass.
    The coverage check treats the listed 2026-06-24 settlement the same way as the cleaning step
    (before that fix it failed the regular-interval check for WIF, TIA, kBONK and TAO inside the
    warm-up; the held-out period alone passed). The held-out panel was not built and no
    panel-level check or backtest was run before the held-out run itself.
- **The held-out run (2026-07-01 to 2026-10-31).** It uses the corrected model, chosen before the
  run, on the code tagged `v1-frozen` with the configuration unchanged: the corrected baseline
  strategy, its zero-cost version and the benchmarks (always-on, random entry with 200 seeds),
  run once. If any held-out data-quality check failed, the run would stop before the backtest.
  2026-10-29 to 2026-10-31 (the Binance hack) is also reported separately. I fixed all of this
  before seeing any held-out result.

## 2026-11-09 — After the held-out run

- **Failure-case classification.** TAO, SOL, DOGE and AVAX (2025-10-10) are a cross-venue tail
  event: the long Binance leg was liquidated at the low, and the Hyperliquid hedge closed only after
  the rebound. TIA (2025-05-08) is a fast rally that liquidated the short Hyperliquid leg at 3x. I
  checked this against the liquidation-bar data before writing it into the report. My first
  description ("Binance mark prices fell 40–70%, far more than Hyperliquid") is not supported by
  Hyperliquid's own 4h lows, which fell -33% to -71% in the same bar against -30% to -68% for
  Binance's mark, and the TIA rally was +16% within the 4h bar (+22% above entry). The report states
  what the data show. The held-out WLD and TIA liquidations (2026-08-22) do not fit the fast-rally
  type: long legs, a one-hour wick of 22–26% on both venues after a 22–24% rally.
- **Held-out stress window as a results table.** The strategy, zero-cost and always-on results for
  2026-10-29 to 2026-10-31 are saved as a results table, so that the report uses only numbers from
  results files. The script reruns the frozen held-out runs unchanged and stops unless their
  totals match the saved held-out summary to the cent (they match). No parameter or definition
  changed.
- **Post-hoc model correction: transfer cap** (added after the held-out run, so a sensitivity and
  never the headline). The venues' documentation limits transfers out of a futures account:
  Binance allows only "the available margin for an order but no more than the wallet balance"
  (blog FAQ dated 2022-05-27; the wallet balance excludes unrealised P&L), and Hyperliquid allows
  unrealised P&L to be withdrawn only while the remaining margin is at least max(initial margin,
  10% of position value) (its Margining documentation). The frozen engine ignores both. I added an
  option, off by default, so every frozen result is unchanged (checked to the cent on both periods).
  When it is on, a rebalance moves at most: for a Binance leg, min(wallet balance, wallet +
  unrealised P&L − initial margin at the current price and leverage); for a Hyperliquid leg, equity
  − max(initial margin at the configured leverage, 10% of position value). I ran it once on each
  period, with zero cost, always-on and 200 random-entry seeds: design -$7,756 (frozen -$4,270),
  held-out -$950 (frozen -$3,885). It was not used to change any parameter, and the frozen held-out
  result remains the headline.
- **Review of interpretive sentences.** I checked every sentence of the report that interprets
  rather than reports against the results files, and rewrote the 17 that the numbers supported
  only partly so that they say exactly what the numbers show; one of them was then shortened to
  avoid repeating the paragraph on the 4h bar mechanic.
