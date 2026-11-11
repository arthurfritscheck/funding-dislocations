"""Rebuild every result, figure and the PDF report from the raw data, in order.

    .venv\\Scripts\\python.exe run_all.py              # everything (about 1-2 hours)
    .venv\\Scripts\\python.exe run_all.py --list       # show the steps
    .venv\\Scripts\\python.exe run_all.py --from 9     # restart at step 9
    .venv\\Scripts\\python.exe run_all.py --keep-going # run every step even if one fails

Needs the raw data in data/raw/ (not in git; see README.md) and, for the last step, pandoc and
Chrome/Edge (scripts/render_report.py). Steps that read the held-out period pass
--allow-holdout explicitly (CLAUDE.md rule 3; the held-out run was made once, on v1-frozen).
Rebuilding rewrites the committed tables, figures, notebooks and report.pdf in place.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
NB = [PY, "-m", "jupyter", "nbconvert", "--to", "notebook", "--execute", "--inplace",
      "--ExecutePreprocessor.kernel_name=python3", "--ExecutePreprocessor.timeout=3600"]

STEPS: list[tuple[str, list[str]]] = [
    ("Clean panel and data-quality report, design period", [PY, "-m", "src.clean.align"]),
    ("Clean panel and data-quality report, held-out period",
     [PY, "-m", "src.clean.align", "--period", "holdout", "--allow-holdout"]),
    ("Raw coverage checks, held-out period (pass/fail printout)",
     [PY, "-m", "src.clean.checks", "--period", "holdout", "--allow-holdout"]),
    ("Gap statistics (eda_*)", [PY, "-m", "src.analysis.gap_stats"]),
    ("Persistence: half-lives, episodes, break-even (persistence_*)",
     [PY, "-m", "src.analysis.half_life"]),
    ("Regimes, correlations, gap vs spread (regimes_*)", [PY, "-m", "src.analysis.regimes"]),
    ("Backtest, as-specified model, design (backtest_*; the kept record)",
     [PY, "-m", "src.backtest.engine", "--variant", "as_specified", "--force"]),
    ("Backtest, corrected model, design (backtest_corrected_*, comparison tables)",
     [PY, "-m", "src.backtest.engine"]),
    ("Backtest, corrected model, held-out (holdout_backtest_corrected_*)",
     [PY, "-m", "src.backtest.engine", "--period", "holdout", "--allow-holdout"]),
    ("Backtest, post-hoc transfer cap, design (backtest_corrected_capped_*)",
     [PY, "-m", "src.backtest.engine", "--transfer-cap"]),
    ("Backtest, post-hoc transfer cap, held-out (holdout_backtest_corrected_capped_*)",
     [PY, "-m", "src.backtest.engine", "--transfer-cap", "--period", "holdout",
      "--allow-holdout"]),
    ("Notebook 01: exploratory analysis", NB + ["notebooks/01_eda.ipynb"]),
    ("Notebook 02: strategy figures (backtest_*.png)", NB + ["notebooks/02_strategy.ipynb"]),
    ("Notebook 03: robustness and failure cases (robustness_*, failure_cases_*)",
     NB + ["notebooks/03_robustness.ipynb"]),
    ("Report checks: liquidation bars, stress window, transfer cap by leg",
     [PY, "-m", "src.analysis.report_checks", "--allow-holdout"]),
    ("Final report figures (report/figures/final/)", [PY, "-m", "src.analysis.final_figures"]),
    ("PDF report (report/report.pdf)", [PY, "scripts/render_report.py"]),
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--list", action="store_true", help="list the steps and exit")
    p.add_argument("--from", dest="first", type=int, default=1, help="first step to run (1-based)")
    p.add_argument("--keep-going", action="store_true", help="continue after a failed step")
    args = p.parse_args(argv)
    if args.list:
        for i, (name, cmd) in enumerate(STEPS, 1):
            print(f"{i:2d}. {name}\n    {' '.join(c if c != PY else 'python' for c in cmd)}")
        return 0
    failed, t_all = [], time.time()
    for i, (name, cmd) in enumerate(STEPS, 1):
        if i < args.first:
            continue
        print(f"\n=== [{i}/{len(STEPS)}] {name}", flush=True)
        t = time.time()
        code = subprocess.run(cmd, cwd=ROOT).returncode
        status = "ok" if code == 0 else f"FAILED (exit {code})"
        print(f"=== [{i}/{len(STEPS)}] {status} in {time.time() - t:,.0f} s", flush=True)
        if code:
            failed.append((i, name))
            if not args.keep_going:
                break
    print(f"\nTotal {time.time() - t_all:,.0f} s.", "Failed: " + "; ".join(f"{i}. {n}" for i, n in failed)
          if failed else "All steps ran.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
