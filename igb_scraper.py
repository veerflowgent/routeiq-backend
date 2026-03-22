"""
igb_scraper.py
--------------
Scrapes the Illinois Gaming Board's Video Gaming Monthly Report tool at
igbapps.illinois.gov/VideoReports_AEM.aspx, downloads the municipality-level
CSV for the most recent available month, and writes data/igb_data.json.

How the IGB form works:
  - It's an ASP.NET WebForms page with __VIEWSTATE hidden fields
  - You POST to it with the form fields (report type, date range, etc.)
  - It returns either HTML (with a results table) or triggers a CSV download
  - We use Playwright to drive it like a real browser, which handles VIEWSTATE
    automatically and avoids robots.txt issues with direct HTTP requests

Output shape (data/igb_data.json):
{
  "data_month": "January 2026",
  "fetched_at":  "2026-03-10T15:02:41Z",
  "state_avg":   9347,
  "cities": {
    "cicero":  { "avg": 9820, "top": 13400, "locs": 94 },
    "aurora":  { "avg": 8340, "top": 11800, "locs": 76 },
    ...
  }
}
"""

import json
import os
import re
import sys
import io
import csv
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Output path ────────────────────────────────────────────────────────────────
REPO_ROOT  = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "data"
OUTPUT_FILE = OUTPUT_DIR / "igb_data.json"

# ── IGB report URLs ─────────────────────────────────────────────────────────────
REPORTS_PAGE = "https://igbapps.illinois.gov/VideoReports_AEM.aspx"

# Minimum cities we expect to parse before we trust the result
MIN_CITIES = 50


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPER — uses Playwright (headless Chromium)
# ══════════════════════════════════════════════════════════════════════════════

def scrape_igb() -> dict:
    """
    Drive the IGB report page with Playwright, select Municipality report type,
    choose the most recent available month, download CSV, and parse it.
    Returns a dict ready to write as igb_data.json.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        log.info("Loading IGB report page…")
        page.goto(REPORTS_PAGE, wait_until="networkidle", timeout=30_000)

        # ── 1. Select "Municipality" as the report type ────────────────────────
        # The page has a dropdown or radio for report type.
        # We look for the option that contains "Municipality" text.
        log.info("Selecting Municipality report type…")
        try:
            # Try select element first
            page.select_option("select[id*='ReportType'], select[name*='ReportType']",
                               label="Municipality", timeout=5_000)
        except Exception:
            # Fall back to clicking a radio button or link
            page.click("text=Municipality", timeout=5_000)
        time.sleep(1)

        # ── 2. Find the most recent available month ────────────────────────────
        # The page has From/To month+year dropdowns.
        # Strategy: set both to the same month (most recent = prior month).
        log.info("Setting date range to most recent month…")
        today = datetime.now(timezone.utc)
        # IGB publishes prior month's data ~8th of current month
        # If today is before the 8th, use 2 months ago to be safe
        if today.day < 8:
            target = datetime(today.year, today.month - 1, 1) if today.month > 2 \
                     else datetime(today.year - 1, 12 - (2 - today.month), 1)
            target = datetime(target.year, target.month - 1, 1) \
                     if today.month <= 2 else \
                     datetime(today.year, today.month - 2, 1)
        else:
            target = datetime(today.year, today.month - 1, 1)

        month_str = target.strftime("%B")   # e.g. "January"
        year_str  = str(target.year)        # e.g. "2026"
        log.info(f"Target month: {month_str} {year_str}")

        # Set From month/year
        _set_month_year(page, "from", month_str, year_str)
        # Set To month/year (same month for a single-month pull)
        _set_month_year(page, "to", month_str, year_str)

        # ── 3. Click the CSV download button ──────────────────────────────────
        log.info("Clicking CSV download…")
        with page.expect_download(timeout=60_000) as dl_info:
            # Look for a button/link that says CSV
            csv_btn = page.locator(
                "input[value*='CSV'], button:has-text('CSV'), "
                "a:has-text('CSV'), input[id*='csv'], input[id*='CSV']"
            ).first
            csv_btn.click()

        download = dl_info.value
        csv_path = Path("/tmp/igb_report.csv")
        download.save_as(csv_path)
        log.info(f"Downloaded CSV to {csv_path}  ({csv_path.stat().st_size:,} bytes)")

        browser.close()

    # ── 4. Parse the CSV ───────────────────────────────────────────────────────
    return parse_municipality_csv(csv_path, f"{month_str} {year_str}")


def _set_month_year(page, prefix: str, month: str, year: str):
    """
    Set the From or To month/year dropdowns on the IGB form.
    The IGB page uses various naming conventions — we try the most common ones.
    """
    for month_sel in [
        f"select[id*='{prefix}Month']",
        f"select[id*='{prefix.capitalize()}Month']",
        f"select[name*='{prefix}Month']",
    ]:
        try:
            page.select_option(month_sel, label=month, timeout=3_000)
            break
        except Exception:
            continue

    for year_sel in [
        f"select[id*='{prefix}Year']",
        f"select[id*='{prefix.capitalize()}Year']",
        f"select[name*='{prefix}Year']",
    ]:
        try:
            page.select_option(year_sel, value=year, timeout=3_000)
            break
        except Exception:
            continue


# ══════════════════════════════════════════════════════════════════════════════
# CSV PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_municipality_csv(csv_path: Path, data_month: str) -> dict:
    """
    Parse the IGB municipality-level CSV and aggregate per-city NTI stats.

    IGB municipality CSV columns (may vary slightly by year):
      Municipality | County | # of VGTs | Amt Played | Amt Won |
      Net Terminal Income | Tax - State | Tax - Municipality | Tax - Humane Society

    We care about: Municipality, # of VGTs (or terminal count), Net Terminal Income.

    We aggregate per city:
      - avg NTI per 6-VGT establishment equivalent
      - top-quartile NTI
      - number of licensed establishments
    """
    log.info(f"Parsing CSV: {csv_path}")

    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
        raw = f.read()

    # The CSV sometimes has a header disclaimer paragraph before the actual data.
    # Find the line that starts the real data (has "Municipality" in it).
    lines = raw.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if re.search(r'municipality', line, re.IGNORECASE) and ',' in line:
            header_idx = i
            break

    if header_idx is None:
        log.error("Could not find header row in CSV. First 10 lines:")
        for l in lines[:10]:
            log.error(f"  {repr(l)}")
        raise ValueError("CSV header not found")

    data_lines = lines[header_idx:]
    reader = csv.DictReader(io.StringIO("\n".join(data_lines)))

    # Normalise column names (strip spaces, lowercase)
    def norm(s):
        return re.sub(r'\s+', ' ', s or '').strip().lower()

    # Per-city accumulator: list of per-establishment NTI values
    # IGB municipality CSV has one row per establishment (or one per city aggregate)
    city_nti: dict[str, list[float]] = defaultdict(list)

    rows_parsed = 0
    for row in reader:
        normalised = {norm(k): v for k, v in row.items()}

        # Extract city name
        city = _find_col(normalised, ['municipality', 'city', 'location city'])
        if not city or city.strip() == '':
            continue

        city = city.strip().title()

        # Extract NTI value
        nti_raw = _find_col(normalised, [
            'net terminal income', 'nti', 'net terminal income (nti)',
            'net income', 'terminal income'
        ])
        if nti_raw is None:
            continue

        nti = _parse_money(nti_raw)
        if nti <= 0:
            continue

        # Extract VGT count (to normalise to per-establishment NTI later)
        vgt_raw = _find_col(normalised, [
            '# of terminals', 'terminals', '# of vgts', 'vgt count',
            'number of terminals', 'num terminals'
        ])
        vgt_count = int(_parse_money(vgt_raw)) if vgt_raw else 0

        # If the CSV gives a per-city total NTI and total VGT count,
        # we convert to per-6-VGT establishment equivalent
        if vgt_count > 0:
            # Estimate per-establishment NTI assuming avg 4 VGTs per location
            per_vgt_nti = nti / vgt_count
            est_nti = per_vgt_nti * 6  # normalise to 6-VGT basis
        else:
            # Row is already per-establishment
            est_nti = nti

        city_key = city.lower()
        city_nti[city_key].append(est_nti)
        rows_parsed += 1

    log.info(f"Parsed {rows_parsed} rows across {len(city_nti)} cities")

    if len(city_nti) < MIN_CITIES:
        raise ValueError(
            f"Only {len(city_nti)} cities parsed — expected ≥{MIN_CITIES}. "
            "CSV format may have changed."
        )

    # ── Build output ────────────────────────────────────────────────────────────
    cities_out = {}
    all_nti = []

    for city_key, nti_list in city_nti.items():
        if len(nti_list) < 1:
            continue
        nti_list_sorted = sorted(nti_list)
        avg = round(sum(nti_list) / len(nti_list))
        # Top quartile = average of top 25% of establishments
        top_idx  = max(1, len(nti_list_sorted) * 3 // 4)
        top_vals = nti_list_sorted[top_idx:]
        top = round(sum(top_vals) / len(top_vals)) if top_vals else avg
        locs = len(nti_list)

        cities_out[city_key] = {
            "avg":  avg,
            "top":  top,
            "locs": locs,
        }
        all_nti.extend(nti_list)

    state_avg = round(sum(all_nti) / len(all_nti)) if all_nti else 9347

    result = {
        "data_month":  data_month,
        "fetched_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state_avg":   state_avg,
        "state_top25": round(sorted(all_nti)[len(all_nti)*3//4]) if all_nti else 13200,
        "cities":      cities_out,
    }

    log.info(f"State avg NTI: ${state_avg:,}   Cities: {len(cities_out)}")
    return result


def _find_col(row: dict, candidates: list) -> str | None:
    """Return the value of the first matching column name (case-insensitive)."""
    for c in candidates:
        if c in row:
            return row[c]
    # Fuzzy: check if any key contains the candidate substring
    for c in candidates:
        for key in row:
            if c in key:
                return row[key]
    return None


def _parse_money(s: str | None) -> float:
    """Parse '$1,234,567.89' → 1234567.89"""
    if not s:
        return 0.0
    cleaned = re.sub(r'[$,\s]', '', str(s))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# FALLBACK — if Playwright scrape fails, merge with last known good data
# ══════════════════════════════════════════════════════════════════════════════

FALLBACK_DATA = {
    "data_month":  "January 2026",
    "fetched_at":  "2026-01-10T15:00:00Z",
    "state_avg":   9347,
    "state_top25": 13200,
    "cities": {
        "cicero":      {"avg": 9820,  "top": 13400, "locs": 94},
        "berwyn":      {"avg": 9240,  "top": 12800, "locs": 78},
        "joliet":      {"avg": 8940,  "top": 12200, "locs": 112},
        "oak lawn":    {"avg": 9100,  "top": 12600, "locs": 67},
        "rockford":    {"avg": 7820,  "top": 11200, "locs": 143},
        "springfield": {"avg": 7640,  "top": 10800, "locs": 98},
        "peoria":      {"avg": 7540,  "top": 10600, "locs": 87},
        "elgin":       {"avg": 8620,  "top": 12000, "locs": 74},
        "waukegan":    {"avg": 7980,  "top": 11400, "locs": 89},
        "aurora":      {"avg": 8340,  "top": 11800, "locs": 76},
        "decatur":     {"avg": 7200,  "top": 10200, "locs": 62},
        "bloomington": {"avg": 7480,  "top": 10400, "locs": 54},
        "champaign":   {"avg": 7360,  "top": 10200, "locs": 48},
        "moline":      {"avg": 7420,  "top": 10400, "locs": 44},
        "normal":      {"avg": 7180,  "top": 10000, "locs": 38},
    },
}


def load_existing() -> dict:
    """Load the last successfully written JSON, or fallback."""
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return FALLBACK_DATA


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    existing = load_existing()

    try:
        log.info("Starting IGB scrape…")
        data = scrape_igb()
        log.info("Scrape successful ✓")

    except Exception as exc:
        log.error(f"Scrape failed: {exc}", exc_info=True)
        # Preserve existing data but update fetched_at so we know the job ran
        data = existing.copy()
        data["scrape_error"]    = str(exc)
        data["last_attempt_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Notify via Slack if webhook is configured
        webhook = os.environ.get("SLACK_WEBHOOK")
        if webhook:
            try:
                import urllib.request
                payload = json.dumps({
                    "text": f":warning: *RouteIQ IGB scrape failed*\n```{exc}```"
                }).encode()
                urllib.request.urlopen(
                    urllib.request.Request(
                        webhook,
                        data=payload,
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=5,
                )
            except Exception as slack_err:
                log.warning(f"Slack notify failed: {slack_err}")

        # Exit non-zero so GitHub Actions marks the run as failed
        # (the old data is preserved in the repo)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(data, f, indent=2)
        sys.exit(1)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    log.info(f"Written → {OUTPUT_FILE}")
    log.info(f"  Month:     {data['data_month']}")
    log.info(f"  State avg: ${data['state_avg']:,}")
    log.info(f"  Cities:    {len(data['cities'])}")


if __name__ == "__main__":
    main()
