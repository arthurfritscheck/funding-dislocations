"""Render report/report.md to report/report.pdf (A4, 10.5 pt; style in report/report.css).

Two external programs are needed, neither installed by pip:
  pandoc            https://pandoc.org (on PATH, or set PANDOC to its path)
  Chrome/Chromium   or Microsoft Edge, run headless to print the HTML (on PATH, in the usual
                    install location, or set CHROME to its path)

    .venv\\Scripts\\python.exe scripts/render_report.py

Prints the page count and the page on which the appendix starts.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "report"
BROWSERS = ["chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
            "msedge", "microsoft-edge"]
BROWSER_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def find_program(env: str, names: list[str], paths: list[str] = ()) -> str:
    if os.environ.get(env):
        return os.environ[env]
    for n in names:
        if shutil.which(n):
            return shutil.which(n)
    for p in paths:
        if Path(p).exists():
            return p
    raise SystemExit(f"{names[0]} not found: install it or set {env} to its path")


def page_count(pdf: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))


def main() -> None:
    pandoc = find_program("PANDOC", ["pandoc"])
    browser = find_program("CHROME", BROWSERS, BROWSER_PATHS)
    pdf = REPORT / "report.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        html = Path(tmp) / "report.html"
        subprocess.run([pandoc, "report.md", "--standalone", "--embed-resources",
                        "--css", "report.css", "--metadata", "pagetitle=Funding-rate dislocations",
                        "--output", str(html)], cwd=REPORT, check=True)
        if pdf.exists():
            pdf.unlink()
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--user-data-dir={Path(tmp) / 'profile'}",
                        f"--print-to-pdf={pdf}", html.as_uri()],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not pdf.exists():
        raise SystemExit("the browser did not write report/report.pdf")
    print(f"report/report.pdf: {page_count(pdf)} pages")
    try:                                   # optional: where the appendix starts
        import pymupdf
        doc = pymupdf.open(pdf)
        start = [i + 1 for i, p in enumerate(doc) if p.get_text().lstrip().startswith("Appendix")]
        if start:
            print(f"main report pages 1-{start[0] - 1}, appendix pages {start[0]}-{len(doc)}")
    except ImportError:
        pass


if __name__ == "__main__":
    sys.exit(main())
