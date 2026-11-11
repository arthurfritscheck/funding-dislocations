# Limitations

Every simplification, data gap and assumption I know of that could affect the results. Each
entry gives what it is, why it matters, the direction in which it likely biases the results, and
what would fix it with more time or data. The decisions behind them are in the
[decision log](decisions.md).

## Coin universe and raw data

### Survivorship bias in the coin universe
- **What:** Only perpetuals listed on both venues for the whole sample can enter the universe. The
  collector's coin list also skips the 56 perpetuals Hyperliquid marks as delisted, although
  Hyperliquid still serves their candle history (FTM and MKR return all 92 days of August–October
  2024; MATIC 40 days before its rename to POL). Renamed coins (MATIC→POL, RNDR→RENDER, FTM→S) drop
  out the same way.
- **Why it matters:** Coins that were delisted or collapsed during the sample are often the
  illiquid, volatile ones where funding gaps are widest and leg risk is highest.
- **Bias:** Likely overstates strategy returns (the worst blow-ups are excluded) and may understate
  gap size and dispersion; direction for gap persistence unclear. For the coin that matters here
  (below) the effect on P&L is not measured and its direction is unknown.
- **Measured (2026-11-04):** of the 56 delisted Hyperliquid perpetuals, 37 traded in August–October
  2024. Ranked by the same average daily USD volume alongside the listed candidates, exactly one
  would have made the top 15: FTM, combined rank 12 at $8.5m a day (universe cut-off: BNB, $6.0m a
  day). FTM was listed on both venues on 2024-08-01 and later delisted on both (Binance's FTMUSDT
  now settling, after the FTM→S migration), so the "still trading" rule removed it and BNB took the
  last slot. The next delisted coins, TON (rank 20, $3.8m) and MKR (rank 35, $2.2m), fall below the
  cut-off. The bias therefore costs one universe slot, a large-cap coin that was migrated rather
  than one that collapsed.
- **Fix:** Download candles and funding for delisted perpetuals too, rank all coins listed on
  2024-08-01, run the strategy with exits forced at delisting, and stitch renamed tickers (FTM→S is
  the one that matters here).

### Universe volume ranking approximates USD volume as volume × close
- **What:** The August–October 2024 ranking uses daily base-asset volume times the daily close, not
  the sum of trade notionals.
- **Why it matters:** On days with large intraday moves this misstates USD volume, which could swap
  neighbouring coins near the cut-off.
- **Bias:** Mostly noise; slightly favours coins that closed near their daily high on high-volume
  days.
- **Fix:** Use trade-level data (hourly candles are not available for 2024), or rank by volume ×
  (high + low + close) / 3.

### Binance listing dates come from today's exchange information
- **What:** "Trading by 2024-08-01 and still trading" uses the current onboarding date and status.
  Symbols that were delisted or relaunched under a new contract show only their current state.
- **Why it matters:** A coin whose Binance contract was relaunched after 2024-08-01 looks newer than
  it is; a symbol halted mid-sample and later resumed looks continuous.
- **Bias:** Small; can wrongly exclude or include a few borderline coins.
- **Fix:** Confirm continuity from the Binance funding history itself: no settlement gaps longer
  than the interval across the whole sample.

### Design-period prices are 4h candles
- **What:** Hyperliquid's API keeps only the latest 5,000 candles, so hourly prices start on
  2026-04-09 10:00 UTC. The design period (from 2024-08-01) relies on 4h candles.
- **Why it matters:** Entries, exits, spread stops and leg-margin checks in the design period can be
  evaluated only every 4 hours, while the held-out period is hourly, so the two periods are not
  measured at the same resolution.
- **Bias:** Understates intra-bar spread blowouts and liquidations in the design period, so
  design-period risk is understated and returns likely overstated; fills at 4h opens add timing
  noise.
- **Fix:** Buy historical 1h (or tick) Hyperliquid candles from a data vendor, or rerun the held-out
  test on 4h bars as well to compare like with like.

### USDC and USDT treated as equal in value
- **What:** Hyperliquid margins and settles in USDC, Binance USDⓈ-M in USDT. Prices and P&L are
  compared one for one.
- **Why it matters:** Any USDC/USDT basis shows up as a spurious price spread between the venues and
  as unmodelled P&L on the collateral held on each side.
- **Bias:** Usually small (a basis of a few bps) but it can spike in stress (depegs), adding noise
  to the spread and understating risk in those episodes.
- **Fix:** Pull USDC/USDT spot history, convert both legs to one numéraire, and add the conversion
  cost to rebalancing.

## Funding data and cleaning

### Missing Hyperliquid funding hour, 2024-08-15
- **What:** Hyperliquid's funding history has no row stamped 2024-08-15 13:00 UTC (the hour
  12:00–13:00) for any of the 15 coins; it is the only missing design-period hour for any of them.
  It is treated as zero funding in P&L and carried forward for signals.
- **Why it matters:** One hour of carry is lost or misstated, and the carried-forward signal is one
  hour stale.
- **Bias:** Negligible on its own (one hour out of about 19,000); direction depends on that hour's
  sign.
- **Fix:** Check the funding records of an account that held a position then, or ask Hyperliquid
  whether funding was paid for that hour.

### Unresolved `predictedFundings` label
- **What:** During the hour [H, H+1), Hyperliquid's `predictedFundings` reports its
  `nextFundingTime` as H (the start of the current hour), while the Binance and Bybit entries in
  the same response give a future settlement time. The live premium test supports "a row stamped T
  covers [T−1h, T)", but this label points the other way.
- **Why it matters:** If funding were actually paid at the start of the hour, Hyperliquid P&L would
  be shifted by one hour relative to positions.
- **Bias:** Small for multi-day holds (one hour of carry at entry and exit); direction unclear.
  Signals are unaffected thanks to the T+1h rule.
- **Fix:** Compare the funding records of a small live position with its open and close times.

### Missing Binance settlement, 2026-06-24 04:00 UTC (4h symbols)
- **What:** Binance's funding history has no 04:00 settlement on 2026-06-24 for WIFUSDT, TIAUSDT,
  1000BONKUSDT and TAOUSDT (confirmed by a direct API query). Spacing-based inference reads the next
  settlement (08:00) as an 8h interval, and the cleaning step flags it as a suspected missing
  settlement. Whether the 04:00 funding was skipped, merged into 08:00, or paid but not published is
  unknown.
- **Why it matters:** If the 08:00 rate covers only 4h, dividing it by 8 halves that
  hour-equivalent rate; if 04:00 was paid but is missing, one settlement of P&L is lost.
- **Bias:** Negligible on its own (one settlement out of about 4,200 per coin); direction depends
  on the sign of the missing rate.
- **Fix:** Check Binance announcements for a 2026-06-24 funding incident, or compare with the
  income history of an account holding these symbols.

### Binance funding caps known only as of today
- **What:** Binance's funding-info endpoint returns the current adjusted cap, floor and interval
  per symbol (a snapshot is kept with the raw data); there is no history of past caps.
- **Why it matters:** The cleaning step flags funding values at or near the cap. If a symbol's cap
  changed during the sample, values near the old cap are missed or wrongly flagged.
- **Bias:** Mostly affects data-quality flags, not P&L; it could hide periods when funding was
  mechanically clamped on Binance, which would make some gaps look more tradeable than they are.
- **Fix:** Archive funding-info snapshots over time, or reconstruct caps from Binance
  announcements; also check for runs of identical extreme values in the rate series.

### Binance interest rate known only as of today (BNB)
- **What:** Binance's `interestRate` is available only as a current snapshot (2026-11-04): 0 for
  BNBUSDT and 0.0001 per 8h for the other 14 symbols. It is assumed constant over the whole sample.
  BNB's resting rate on Binance is set to 0 from it, so BNB's mechanical gap is +10.95% APR; BNB's
  mean gap is +5.7% APR raw and -5.2% APR net of that constant.
- **Why it matters:** If any symbol's interest rate changed during the sample, its resting share,
  mechanical gap and baseline-adjusted gap are wrong for part of the period.
- **Bias:** Probably small: BNBUSDT settled at exactly 0 in every quarter of the design period
  (49–84% of settlements per quarter) and never at 0.01%, and the other symbols rest at 0.01%
  throughout. Direction unclear for any undetected change.
- **Fix:** Archive premium-index snapshots over time, or check Binance announcements for
  interest-rate changes; per quarter, verify each symbol's most frequent settlement rate matches
  its configured baseline.

### Funding at the edges of the design period
- **What:** The design panel is built from data strictly before 2026-07-01 00:00. The last Binance
  interval of the design period settles at 2026-07-01 00:00, so its hours (the last 8h, or 4h for 4h
  symbols) and the last Hyperliquid hour have no realised funding. At the start, known-at-t Binance
  funding is missing until the first settlement with a known interval is an hour old (09:00 on
  2024-08-01 for 8h symbols).
- **Why it matters:** Positions open on the evening of 2026-06-30 miss up to one interval of carry
  in the design-period backtest; signals cannot fire in the first hours of the sample.
- **Bias:** Negligible (at most 8 of about 16,800 hours per coin); direction depends on the sign.
- **Fix:** Build the panel from data on both sides of the cut-off and check that the boundary hours
  are filled.

### Price spread from last-trade candle closes
- **What:** The price spread uses each venue's last-trade candle close at the same moment, not mid,
  mark or oracle prices. In the 4h regime only every fourth hour has a spread, even though Binance
  1h closes exist, so both venues are compared at the same resolution.
- **Why it matters:** A last trade can sit on either side of the bid-ask spread, so the measured
  spread includes bid-ask bounce. Funding P&L uses mark (Binance) and oracle (Hyperliquid) prices,
  which differ slightly from the closes.
- **Bias:** Adds noise to the spread (median |spread| is 1.7–6.1 bps by coin); slightly overstates
  spread volatility and could trigger spread stops a little more often than in reality.
- **Fix:** Use Binance mark-price klines and Hyperliquid mark or oracle history (logged live), or
  order-book mids.

## Analysis methods

### Half-life from an AR(1) on daily means
- **What:** Persistence is measured with an OLS AR(1) on daily-averaged gaps. Daily averaging cannot
  resolve mean reversion faster than about a day, while hourly episodes close within 3–6 hours
  (median). Newey-West standard errors are 1.3–2.8x the OLS ones (residual autocorrelation and
  heteroskedasticity), so OLS intervals on the half-life are too narrow. OLS is also sensitive to
  single extreme days: 2025-10-10/11 (a market-wide liquidation event) moves WIF's full-period phi
  from 0.49 to 0.57 and drives the October 2025 drops (and the jumps back 90 days later) in several
  coins' rolling 90-day phi. The full-period phi (0.49–0.71) sits above the rolling median
  (0.39–0.60) because slow level shifts in the gap read as persistence.
- **Why it matters:** "A half-life of about 1–2 days" describes the daily mean gap, not how fast an
  individual hourly dislocation closes, and its precision is overstated.
- **Bias:** Overstates the precision of the half-life; the full-period half-life is biased up by
  slow-moving level shifts; mixed effect of extreme days.
- **Fix:** Fit on hourly data with an AR model that allows intraday dynamics (or a continuous-time
  OU fit), report HAC or bootstrap intervals, and rerun excluding stress days.

### Episode definition: hysteresis with sign-change ends
- **What:** An episode starts when |gap| rises above a threshold and ends when |gap| falls below
  half of it or the gap changes sign; a sign change above the threshold starts a new episode. Sign
  changes end 10–34% of 10%-threshold episodes (by coin), 1–18% at 25% and 0–10% at 50%. Trading
  analysis uses BNB's raw gap, so 57% of BNB's hours with |gap| > 10% APR are hours when both venues
  rest at the mechanical +10.95% APR; a net-of-mechanical version is reported for description only.
- **Why it matters:** Episode counts and durations depend on the hysteresis rule and on hourly noise
  in the Hyperliquid rate; BNB's 10% statistics mostly measure a fixed formula difference.
- **Bias:** Hysteresis at half the threshold and sign-change ends both shorten measured episodes
  relative to "the gap stays wide"; BNB's 10% episode time is overstated as dislocation (but it is
  real carry).
- **Fix:** Report alternative exit levels (one third, or zero) and a smoothed gap; keep BNB's net
  and raw versions side by side.

### Break-even carry is carry only
- **What:** Hours to cover one round trip = (four taker fills: fees plus slippage) / (|gap| / 8760),
  at the threshold and at the mean episode |gap|. It ignores price-spread P&L, Binance paying only at
  settlement moments, funding paid on mark or oracle notional, and gaps that move during an episode.
- **Why it matters:** It is a rough yardstick for whether single episodes can pay their own trading
  costs, not a backtest.
- **Bias:** Direction unclear; ignoring Binance's settlement timing overstates carry for episodes
  shorter than one Binance interval.
- **Fix:** A backtest of an episode-trading rule with full P&L accounting.

### Regime regression design
- **What:** Daily mean |gap| on regressors known at the start of the day (the previous day's BTC
  realised volatility, the coin's trailing 30-day return and |return|, log 30-day Binance quote
  volume, the previous day's cross-coin average funding), with coin fixed effects or a large-cap
  dummy (top 5 by Binance quote volume, August–October 2024: BTC, ETH, SOL, kPEPE, SUI) and
  Driscoll-Kraay standard errors. OLS on a skewed, positive dependent variable; one-day lags only;
  15 coins.
- **Why it matters:** Coefficients describe association, not cause; heavy tails (2025-10-10/11) can
  drive the estimates; R² is 0.16.
- **Bias:** Unclear; effects within the day are not captured (lagged design).
- **Fix:** Log |gap| or quantile regression, hourly regressors, and robustness without stress days.

### Spread behaviour during episodes uses only the hourly-price period
- **What:** The spread move over an episode needs exact prices at entry and exit, which exist only
  from 2026-04-09 (hourly Hyperliquid candles). The analysis covers about 82 days of the design
  period (2,516 finished 10%-threshold episodes; 799 at 25%; 204 at 50%).
- **Why it matters:** That window may not represent the whole design period (no 2024–2025 stress
  episodes such as 2025-10-10/11).
- **Bias:** Likely understates adverse spread moves in stress periods.
- **Fix:** Buy historical 1h Hyperliquid candles, or use the nearest 4h prices with an explicit
  staleness adjustment for 2024–2025.

## Backtest model

### Maintenance margins known only as of 2026-11-08
- **What:** Leg liquidation uses one maintenance-margin rate per venue and coin from snapshots taken
  on 2026-11-08 (Hyperliquid's maximum leverage; Binance's public bracket table, effective rate at
  a $10,000 notional). The venues changed maximum leverage and brackets during the sample, and the
  Binance effective rate rises slightly as a leg's notional grows (WIF 1.25% at $10k, 1.42% at
  $13k).
- **Why it matters:** Each leg's liquidation price depends on it.
- **Bias:** Small at 3x leverage (initial margin 33% against maintenance of 0.4–10%); a leg is
  liquidated after a 23–33% adverse move either way. Could slightly understate liquidations where
  past rates were higher.
- **Fix:** Archive bracket and leverage snapshots over time, or reconstruct them from venue
  announcements; apply Binance's full bracket formula at the current notional.

### Funding notional uses Binance last-trade closes as mark and oracle prices
- **What:** Binance funding is booked on quantity × the Binance 1h close at the settlement time (not
  the mark price), and Hyperliquid funding on quantity × the Binance 1h close at the end of the hour
  (not Hyperliquid's oracle, a weighted median of exchange spot prices).
- **Why it matters:** Funding P&L scales with the reference price.
- **Bias:** Second order: the error is (basis / price) × funding, of order 0.05% of a payment; no
  clear direction.
- **Fix:** Use the Binance mark- and index-price klines (already downloaded for the liquidation
  checks) for the funding notional too; I left this unchanged so that the model corrections change
  only liquidation and rebalancing.

### Backtest decisions every 4h in most of the design period
- **What:** Before each coin's first hourly Hyperliquid candle (2026-04-09, 11:00–14:00 UTC by
  coin) the backtest decides only at 4h bar boundaries, because Hyperliquid prices exist only every
  4h; from then on it decides hourly. The 4h candle that straddles the switch is dropped, leaving a
  stretch of up to 3 hours with no decision (funding still accrues).
- **Why it matters:** Entries, exits and spread stops can be up to 3h late in the design period, and
  spread stops are checked on 4h closes only (intra-bar spread blowouts are missed). Liquidation
  checks use each venue's 4h high and low, so they do see intra-bar extremes.
- **Bias:** Small for carry against a 7-day signal; spread-stop losses are likely understated in the
  design period relative to the hourly held-out period.
- **Fix:** Hourly (or finer) historical Hyperliquid candles from a vendor, or rerun the held-out
  test on a 4h clock as well to compare like with like.

### Liquidations judged on mark-price highs and lows (corrected 2026-11-08)
- **What:** In the as-specified run a leg was liquidated when its equity at the bar's last-trade
  high or low reached maintenance. The corrected run uses mark prices: Binance 1h mark-price klines
  for the Binance leg and Binance 1h index-price klines as the proxy for the Hyperliquid leg (next
  entry). Highs and lows are still aggregated over 4h bars in most of the design period, so a mark
  move that reverses within the bar counts as fully as one that holds. The leg still closes at its
  exact liquidation price less 100 bps even if the price gapped through it; no separate fee is
  charged on it; funding for the whole bar is booked as if it ended at the bar close.
- **Why it matters:** Liquidations were the largest single loss in the as-specified run.
- **Bias:** The as-specified run overstated liquidations (last-trade wicks). The corrected run can
  still overstate them slightly (hourly mark extremes within a 4h bar; Binance mark klines are 1h,
  so sub-hour mark spikes are included at their full size), and the fixed penalty may understate
  the cost of a gap-through in a crash.
- **Fix:** Use the venues' own mark-price history at finer resolution; model the liquidation fill
  from order-book depth.

### Hyperliquid mark price proxied by the Binance index price
- **What:** Hyperliquid has no public mark-price history. Its mark price is built from its oracle (a
  weighted median of exchange spot prices, Binance among them) and its own order book and perpetual
  prices. The corrected run uses Binance's index price (a weighted spot index across exchanges) as
  the Hyperliquid leg's mark for liquidation checks.
- **Why it matters:** The Hyperliquid leg's liquidation is checked against this proxy; 43 of the 45
  as-specified liquidations were on the Hyperliquid leg.
- **Bias:** Probably understates Hyperliquid liquidations: the index is a spot composite and
  smoother than Hyperliquid's mark, which also follows Hyperliquid's own book and perpetual premium
  (which can run away in a squeeze, the moment the short Hyperliquid leg is at risk).
- **Fix:** Log Hyperliquid's mark and oracle prices live and compare them with the proxy, or buy
  historical mark data.

### The surviving leg closes at the next bar open after a liquidation
- **What:** When one leg is liquidated, it closes at its liquidation price (less 100 bps) and the
  other leg closes at the next bar open, up to 4h later in most of the design period. On 2025-10-10
  prices wicked down and rebounded within one 4h bar on both venues (AVAX: Binance mark low $8.50
  from about $26, Hyperliquid's own 4h low $7.64, both venues' closes about $20.6). Hyperliquid's own
  lows fell 33–71% and Binance's mark lows 30–68% in that bar. (An earlier version of this entry
  compared the Binance low with the Hyperliquid close and read it as a cross-venue gap; corrected on
  2026-11-09.) The model books the liquidated leg's full loss down to its liquidation price and the
  hedge's gain only at the rebound price. The Binance mark-kline lows are taken at face value.
- **Why it matters:** It drives the leverage sensitivity (added after the first backtest run): at
  1.5x the 5 liquidations on 2025-10-10/11 cost -$15,812 while all other trades make +$892; at 2x,
  7 liquidations cost -$14,381 against +$4,811; at 3x, 8 cost -$3,074. Lower leverage puts the
  liquidation price deeper, so a wick that does reach it costs more.
- **Bias:** Likely overstates the 2025-10-10 tail-event losses and any loss after a wick that
  rebounds (a live trader would close the hedge within seconds, though at Hyperliquid's price at
  that moment, which also fell); understates losses when the move continues. Makes low leverage
  look worse than it probably is.
- **Fix:** Close the surviving leg at the liquidation time using hourly or finer Hyperliquid
  prices; check Binance's mark lows on 2025-10-10 against its published liquidation data.

### Rebalancing restores equal equity but never resizes the position (trigger corrected 2026-11-08)
- **What:** When a leg's equity falls below 50% of its margin requirement, half the equity
  difference moves across ($1 fee, 2h delay). In the as-specified run the requirement was the
  initial margin at entry (a fixed dollar amount); in the corrected run it is the current one,
  quantity × the venue's bar close / leverage, so the trigger rises with the leg's notional. The
  position keeps its entry quantity (no resizing), only one transfer can be in transit at a time,
  the trigger is checked on bar closes (last trade, not mark), and in the 4h regime funds count only
  from the next bar boundary.
- **Why it matters:** After a big move the as-specified trigger sat close to the liquidation price,
  so a single fast bar could liquidate a leg that the rebalance would have saved.
- **Bias:** The as-specified run overstated liquidations. Both runs ignore that a real trader can
  resize or top up continuously, and understate the operational cost of doing so.
- **Fix:** Allow resizing, check the trigger on mark prices, rebalance on a schedule; test with
  hourly prices.

### Rebalancing can withdraw a leg's unrealised gains
- **What:** A rebalance moves half the equity difference between the legs, and equity includes
  unrealised price P&L. In a rally the long leg's equity is mostly unrealised gain, so repeated
  rebalances can take more than its initial margin out of its cash. Both held-out liquidations on
  2026-08-22 had negative collateral when liquidated (WLD's Binance leg -$370, TIA's Hyperliquid
  leg -$685, after 2 and 3 rebalances), and a one-hour wick of 22–26% then liquidated them.
- **Venue rules (checked 2026-11-09 in the official documentation):**
  - Binance ([FAQ "Why Can't I Transfer the Available Margin From My Futures Wallet?", 2022-05-27](https://www.binance.com/en/blog/futures/10-most-frequently-asked-questions-about-binance-futures-421499824684903916)):
    "users can only transfer the available margin for an order but no more than the wallet
    balance"; the USDⓈ-M single-asset formula is max(min(...), 0) over four terms, and only one of
    them adds "∑cross Unrealized PNL". The wallet balance excludes unrealised P&L ("Wallet Balance =
    Total Net Transfer + Total Realized Profit + Total Net Funding Fee - Total Commission",
    [support FAQ](https://www.binance.com/en/support/faq/how-to-check-my-futures-wallet-balance-5d62f8be18d24544b5b6156094c16bf7)).
    So unrealised profit cannot be transferred out of a Binance futures wallet. Caveat: the only
    explicit withdrawal formula I found is in this blog post from 2022, and Binance may have changed
    it since. The 2026 support FAQ defines the wallet balance and "Available for Order" but gives no
    withdrawal formula, and the API's maximum-withdraw field is documented without one. Checking
    that field on a live account with an open, profitable position would settle it.
  - Hyperliquid ([Margining](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining)):
    "Unrealized pnl can be withdrawn from isolated positions or cross account, but only if the
    remaining margin is at least 10% of the total notional position value of all open positions.
    The margin remaining must also meet the initial margin requirement", i.e. the transfer margin
    required is max(initial margin required, 10% of total position value). At 3x the initial margin
    (a third of notional) binds; at a coin's maximum leverage much more could be withdrawn.
  - The frozen engine allows neither limit: it moved enough out of both liquidated legs to leave
    negative collateral. A rough estimate at 3x (maximum transfer at the pre-wick close, ignoring
    timing, the 2-hour delay, funding and fees) leaves WLD's Binance leg at least $865 and TIA's
    Hyperliquid leg at least $658, enough to survive the wick, and both short legs above
    maintenance with the smaller transfers. This is not a backtest result.
- **Measured (post-hoc transfer-cap sensitivity, 2026-11-09):** with each transfer capped at the
  venue's limit, the held-out loss falls from -$3,885 to -$950 (WLD and TIA are not liquidated on
  2026-08-22; liquidations 4 → 6, but smaller), while the design loss grows from -$4,270 to -$7,756
  (38 liquidations either way; the 27 short-Hyperliquid liquidations cost -$3,871 against +$355 for
  26 in the frozen model, and the long-Binance ones -$3,437 for 10 against -$4,375 for 11). The
  earlier rough estimate for the two held-out trades holds.
- **Why it matters:** The transfer rule changes which leg is liquidated and how much it costs; it
  moved the held-out result by +$2,935 and the design result by -$3,486.
- **Bias:** Mixed: the uncapped model overstates long-leg liquidation losses after a rally (the
  held-out period) and understates short-leg liquidation losses in rallies (the design period).
  Neither period is profitable with the cap.
- **Fix:** Use the capped rule (or the venues' exact withdrawable amounts, checked on live
  accounts) in further work, and log each transfer (time, direction, amount). The frozen results
  stay uncapped because the held-out run was made with them.

### Capital buffer is idle cash, and cost multiples scale flat rates
- **What:** The buffer never tops up a leg, so the buffer sensitivity (added after the first backtest
  run) changes only the capital base: P&L, trades and liquidations are identical across buffers at a
  given leverage. Cost multiples scale the flat per-fill fee and slippage rates; transfer fees and
  the liquidation penalty are not scaled, and slippage does not depend on size or volatility.
- **Why it matters:** A buffer's real purpose is to save legs from liquidation; slippage is worst
  exactly on stress days such as 2025-10-10.
- **Bias:** Understates the risk benefit of a larger buffer (some liquidations would be avoided) and
  the risk of a zero buffer; flat slippage understates costs in stress and at larger size.
- **Fix:** Model a shared collateral pool that tops up legs with the same transfer delay, and size-
  and volatility-dependent slippage (as part of a capacity estimate).

### Benchmark construction
- **What:** Random entry keeps each coin's number of trades, holding periods and sides but has no
  spread stop or re-arm rule (liquidations still apply). Always-on holds short Hyperliquid / long
  Binance in every coin, the direction of the documented interest-rate asymmetry, and re-enters
  after a forced close. Fees and slippage are charged on the unslipped bar open.
- **Why it matters:** The benchmarks are only as fair as their construction.
- **Bias:** Random entry may hold through spread moves the strategy would stop out of (no spread
  stop fired in the baseline, so this does not matter yet); charging costs on the unslipped price
  understates them by a fraction of a basis point.
- **Fix:** Apply the same stop rules to random entry; charge fees on the slipped fill price.

## Robustness and sensitivities

### Robustness checks reuse the design period
- **What:** Cost sensitivity, the parameter grid, universe splits, quarterly P&L and the block
  bootstrap all describe the same 699 design-period days on which the signal was chosen. The 7-day
  block bootstrap of daily P&L (95% interval -1.88 to +0.84 around a Sharpe of -0.61) measures
  sampling noise within that sample only, and the coins' gaps co-move (about 2.3–3.5 independent
  coins), so 213 trades carry less information than their count suggests.
- **Why it matters:** None of these checks is out of sample.
- **Bias:** Overstates confidence in any design-period number, positive or negative; the best grid
  cells (30-day signal, 10% entry: +$5k to +$9k) are in-sample maxima over 60 runs.
- **Fix:** The held-out test; longer history; a stationary bootstrap with several block lengths.

### Maker scenario assumes both legs fill as makers at the bar open
- **What:** The maker scenario (both legs at maker fees, half the slippage; a sensitivity added after
  the first backtest run) fills both legs at the same bar open exactly as the taker baseline does,
  as if both resting orders filled at once.
- **Why it matters:** It turns the design-period loss into a small gain (-$4,270 → +$973).
- **Bias:** Overstates what maker execution could deliver: real resting orders may not fill
  (legging risk, chasing with a taker order) and fill most often when the price moves against them
  (adverse selection).
- **Fix:** Simulate queue position and fill probability from order-book data, or test maker on one
  leg with a taker hedge on fill.

### The tail-event table removes two days' P&L, not the event
- **What:** The post-hoc tail-event table (added after the first backtest run) subtracts the daily
  P&L booked on 2025-10-10 and 2025-10-11 (UTC, daily equity curve) from the design-period total. It
  is not a rerun without those days: positions liquidated then stay closed, re-arming and later
  entries are as in the full run, and losses that built up on open positions before 10-10 or were
  realised after 10-11 stay in the "excluding" figure.
- **Why it matters:** Without those two days the strategy makes +$1,146 at 1.5x, +$5,106 at 2x and
  -$894 at 3x (Sharpe +0.20, +1.19, -0.16): 3x goes from the best leverage to the worst. That
  describes one stress event inside the design period; it is not an estimate of returns in a world
  without such events.
- **Bias:** Overstates how good the strategy is "without the crash": the next stress event is not
  excluded, and the two days were chosen after seeing the results.
- **Fix:** Treat stress events as part of the return distribution (held-out test, longer history);
  model the hedge close at the liquidation time (see the surviving-leg entry above).

## Held-out period and venue risk

### Held-out run: warm-up from design data and the period edges
- **What:** The held-out signal at 2026-07-01 is warmed up on the last 7 days of the design period,
  which include the missing Binance 2026-06-24 04:00 settlement for WIF, TIA, kBONK and TAO
  (handled as in the design period). The held-out panel is cut at 2026-11-01 00:00, so, as at the
  design edge, the final Hyperliquid hour and the final Binance interval have no realised funding.
  Before the run only raw-file coverage was checked (pass or fail); panel-level checks (missing
  funding, flags, spread size, the factor-of-eight check) ran only as part of the held-out run.
  Recomputing the signal over other windows has no warm-up, so other windows on the held-out period
  would need a panel built with a longer warm-up.
- **Why it matters:** The first held-out decisions depend on design-period funding; positions open on
  2026-10-31 miss up to one interval of carry; a panel-level data problem would have surfaced only
  during the held-out run.
- **Bias:** Negligible for carry (at most 8 of 2,952 hours per coin); direction depends on the sign.
- **Fix:** Rebuild with data past 2026-10-31 to fill the edge (not done, to keep the held-out window
  fixed); run panel-level checks first in a held-out run and stop if any fails.

### The held-out period is short
- **What:** The held-out period has 123 days and 28 closed trades; one day, 2026-08-22, carries
  -$3,078 of the -$3,885 held-out loss.
- **Why it matters:** One event can dominate the out-of-sample result.
- **Bias:** Direction unclear; it widens the uncertainty around the held-out number.
- **Fix:** A longer, new out-of-sample period (the current one has been used).

### No venue or counterparty risk (the Binance hack, 2026-10-29 to 2026-10-31)
- **What:** The backtest sees only prices and funding. It assumes both venues stay open, every
  collateral transfer arrives after 2 hours, and balances are always safe. During the held-out
  window 2026-10-29 to 2026-10-31 (the Binance hack) the model still moved collateral to and from
  Binance (strategy: 2 transfers, both on 2026-10-29; always-on: 8; an earlier count of 1 for the
  strategy was corrected on 2026-11-09) and had no way to represent a withdrawal halt, frozen
  balances or a loss of funds held on the venue.
- **Why it matters:** A delta-neutral cross-venue position keeps margin on both venues; a hack or a
  withdrawal halt can stop rebalancing (raising liquidation risk on the other leg) or cost the
  collateral itself. In the held-out run the window's measured P&L is small (strategy -$9,
  always-on +$139, no liquidations), but that measures only price and funding moves.
- **Bias:** Understates risk, possibly by a lot in a venue failure; the window's P&L in the backtest
  is not an estimate of what the hack would have cost.
- **Fix:** Model venue states (withdrawals paused, delayed transfers, haircuts on balances) and
  stress them; hold the buffer off-exchange; cap collateral per venue.
