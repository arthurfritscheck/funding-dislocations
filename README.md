# Funding-rate dislocations: Hyperliquid vs Binance

- **Gaps exist and close:** Hyperliquid funding exceeds Binance's for all 15 coins (3.1–10.9% APR on average, design period), and the daily gap reverts with half-lives of 1.0–2.0 days, but hourly episodes last a median 3–5 hours, far too short to pay a 27–51 bps round trip.
- **A carry strategy on the persistent gap loses:** -$4,270 on $150,000 over the design period (2024-08-01 to 2026-06-30, Sharpe -0.61); it makes +$5,686 before fees and slippage and would need costs at 0.55x to break even.
- **Held-out result (2026-07-01 to 2026-10-31, run once on frozen code):** -$3,885 (Sharpe -2.15), a loss even before costs and worse than 97% of 200 random-entry runs.

**Report:** [report/report.md](report/report.md) · [report/report.pdf](report/report.pdf) (main report 8 pages, appendix 5).
**Logs:** [decisions](report/decisions.md) (every design choice, dated, with its reason) ·
[limitations](report/limitations.md) (every known simplification with its likely bias).

## The question

When funding on the same perpetual differs between Hyperliquid and Binance, does the gap close,
and can a delta-neutral position (short the perp with the higher funding, long the other) collect
it after fees, slippage, margin and liquidation risk? The study uses hourly data for 15 coins
from 2024-08-01 to 2026-10-31. The universe was chosen by a volume rule fixed before any funding
data was examined. Everything was designed on 2024-08-01 to 2026-06-30, and the held-out period
was run once on the code tagged `v1-frozen`.

## Rebuild everything

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"      # macOS/Linux: .venv/bin/python
.venv\Scripts\python.exe run_all.py
```

`run_all.py` rebuilds the clean panels, every table in `report/tables/`, every figure, the three
notebooks and `report/report.pdf`, in order, from the raw data in `data/raw/`
(`run_all.py --list` shows the 17 steps; `--from N` restarts at step N). A full run takes about
30 minutes on a desktop PC. The last step needs two programs that pip does not install:
[pandoc](https://pandoc.org) and Chrome, Chromium or Edge (set `PANDOC` or `CHROME` if they are
not found). Steps that read the held-out period pass `--allow-holdout` explicitly.

Checked on 2026-11-11 from a fresh clone, with a new virtual environment and the original raw
data. All 17 steps ran, and every table in `report/tables/` and every figure came out
byte-identical to the committed files, with one exception: the as-specified record
`backtest_decomposition_benchmarks.csv` gains a `random_mean` column, which the engine added
after that record was written; its other columns are identical. Re-executed notebooks differ only
in random HTML ids, memory addresses and two new zero-valued counters in a printout.

Tests: `.venv\Scripts\python.exe -m pytest -m "not network"`.

## Raw data (not in this repository)

Raw API responses are kept out of git (about 170 MB). They are public and need no API key; the
collectors download them into `data/raw/<venue>/<dataset>/<coin>.parquet`:

```
.venv\Scripts\python.exe -m src.collect.hyperliquid funding candles_1h candles_4h --coins BTC ETH SOL kPEPE SUI WIF TIA DOGE WLD AAVE APT kBONK AVAX TAO BNB
.venv\Scripts\python.exe -m src.collect.binance funding klines_1h mark_price_1h index_price_1h --coins universe
```

Hyperliquid serves only its latest 5,000 candles per interval, so **a new download cannot
reproduce the data used here**:

- Hourly candles before 2026-04-09 could not be downloaded even when the data was collected
  (2026-11-03); the design period before that date uses 4h candles. A download today starts
  later still (about 5,000 hours before the download), so part of the hourly stretch used here,
  from 2026-04-09, is no longer available either.
- 4h candles reach back to 2024-08-01 only for downloads made before about 2026-11-12.

Funding history and all Binance data can still be downloaded in full. To reproduce the results
exactly, use the archived raw data from the original download; with newly downloaded data the
pipeline runs, but the backtest uses fewer hourly Hyperliquid prices and its numbers will differ.

The coin universe itself (`report/universe_candidates.csv`, `report/survivorship_check.csv`) was
built once from live API snapshots by `scripts/universe_candidates.py` and
`scripts/survivorship_check.py`; it is fixed in `config.yaml` and is not part of the rebuild.

## Repository map

```
config.yaml          every date, cost, margin and strategy parameter (the only place they live)
run_all.py           one-command rebuild, raw data -> tables, figures, notebooks, PDF
src/collect/         Hyperliquid and Binance collectors (raw responses, untouched)
src/clean/           hourly alignment, interval inference, data-quality checks
src/analysis/        gap statistics, persistence, regimes, robustness, failure cases,
                     report checks, final report figures
src/backtest/        hourly engine (leg margin, liquidation, rebalancing), costs, metrics
notebooks/           01 exploration, 02 strategy, 03 robustness (they call src/ only)
scripts/             universe selection, survivorship check, PDF render, one-off probes
tests/               pytest suite (hand-computed expected values for the backtest)
report/              report.md and report.pdf, decisions.md and limitations.md (project logs),
                     tables/ (every number in the report), figures/ (final/ holds the
                     report's figures), data-quality reports
data/                raw/ and clean/ (empty in git)
```

## What I would do next

The largest open question is the model, not the market. The liquidations that drive the losses
depend on how the surviving leg is closed after a liquidation (at the next 4h bar open in most of
the design period) and on how collateral moves between venues. A post-hoc cap at the venues'
documented withdrawal limits moves the held-out result from -$3,885 to -$950 but the design result
from -$4,270 to -$7,756. So next I would buy historical hourly or finer Hyperliquid candles and log
Hyperliquid's own mark and oracle prices, close the surviving leg at the liquidation moment,
model venue states such as paused withdrawals (the Binance hack fell inside the held-out window),
and use order-book data for slippage and maker fills. I would then test on a new out-of-sample
period, since the current held-out period has been used.
