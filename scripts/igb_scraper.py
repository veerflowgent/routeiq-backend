"""
igb_scraper.py
--------------
Fetches Illinois Gaming Board VGT municipality revenue data and writes
data/igb_data.json for the RouteIQ frontend.

Strategy: direct HTTP requests to IGB public CSV URLs — no browser,
no Playwright, no IP blocking issues.
"""

import csv
import io
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

REPO_ROOT   = Path(__file__).parent.parent
OUTPUT_DIR  = REPO_ROOT / "data"
OUTPUT_FILE = OUTPUT_DIR / "igb_data.json"
MIN_CITIES  = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def target_month():
    now = datetime.now(timezone.utc)
    if now.day < 8:
        month = now.month - 2
        year  = now.year
        if month <= 0:
            month += 12
            year  -= 1
    else:
        month = now.month - 1
        year  = now.year
        if month <= 0:
            month = 12
            year -= 1
    return year, month


def fetch_csv(year, month):
    month_name = datetime(year, month, 1).strftime("%B")
    month_abbr = datetime(year, month, 1).strftime("%b")
    yr2 = str(year)[2:]
    mm  = f"{month:02d}"

    urls = [
        f"https://igb.illinois.gov/content/dam/soi/en/web/igb/docs/video-gaming/monthly-reports/{year}/{month_name.lower()}-{year}-municipality.csv",
        f"https://igb.illinois.gov/content/dam/soi/en/web/igb/docs/video-gaming/monthly-reports/{year}/{month_abbr}{yr2}Municipality.csv",
        f"https://igb.illinois.gov/content/dam/soi/en/web/igb/docs/video-gaming/monthly-reports/{year}/{month_name}{year}Municipality.csv",
        f"https://igbapps.illinois.gov/VideoReports/MunicipalityReport_{mm}_{year}.csv",
    ]

    session = requests.Session()
    session.headers.update(HEADERS)

    for url in urls:
        try:
            log.info(f"Trying: {url}")
            r = session.get(url, timeout=20, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > 200:
                if re.search(r'municipality|net terminal|NTI', r.text, re.IGNORECASE):
                    log.info(f"  Found CSV ({len(r.text):,} chars)")
                    return r.text
                log.info(f"  Response doesn't look like VGT CSV")
            else:
                log.info(f"  HTTP {r.status_code}")
        except Exception as e:
            log.info(f"  Error: {e}")

    # Also try scraping the HTML page for a CSV link
    try:
        log.info("Trying HTML page for CSV link...")
        r = session.get("https://igb.illinois.gov/video-gaming/video-reports.html", timeout=20)
        if r.status_code == 200:
            patterns = [
                rf'href="([^"]*[Mm]unicipality[^"]*{year}[^"]*\.csv)"',
                rf'href="([^"]*{month_name}[^"]*\.csv)"',
                r'href="([^"]*[Mm]unicipality[^"]*\.csv)"',
            ]
            for pat in patterns:
                matches = re.findall(pat, r.text)
                if matches:
                    csv_url = matches[0]
                    if not csv_url.startswith("http"):
                        csv_url = "https://igb.illinois.gov" + csv_url
                    log.info(f"  Found link: {csv_url}")
                    cr = session.get(csv_url, timeout=20)
                    if cr.status_code == 200 and len(cr.text) > 200:
                        return cr.text
    except Exception as e:
        log.info(f"  HTML scrape error: {e}")

    return None


def parse_csv(raw, data_month):
    lines = raw.splitlines()
    header_idx = next((i for i, l in enumerate(lines) if re.search(r'municipality', l, re.IGNORECASE) and ',' in l), None)
    if header_idx is None:
        raise ValueError("Header row not found in CSV")

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))

    def norm(s): return re.sub(r'\s+', ' ', (s or '')).strip().lower()
    def money(s):
        try: return float(re.sub(r'[$,\s]', '', str(s or 0)))
        except: return 0.0
    def col(row, keys):
        for k in keys:
            if k in row: return row[k]
        for k in keys:
            for rk in row:
                if k in rk: return row[rk]
        return None

    city_nti = defaultdict(list)

    for row in reader:
        nrow = {norm(k): v for k, v in row.items()}
        city = col(nrow, ['municipality', 'city', 'location city', 'location'])
        if not city or not city.strip(): continue
        city = city.strip().title()
        if re.match(r'total|grand total|statewide', city, re.IGNORECASE): continue

        nti = money(col(nrow, ['net terminal income', 'nti', 'net terminal income (nti)', 'net income']))
        if nti <= 0: continue

        vgt = int(money(col(nrow, ['# of terminals', 'terminals', '# of vgts', 'terminal count', '# terminals']))) or 0
        est_nti = (nti / vgt * 6) if vgt > 6 else nti

        city_nti[city.lower()].append(est_nti)

    log.info(f"Parsed {len(city_nti)} cities")
    if len(city_nti) < MIN_CITIES:
        raise ValueError(f"Only {len(city_nti)} cities — expected >={MIN_CITIES}")

    cities_out = {}
    all_nti    = []
    for key, vals in city_nti.items():
        s = sorted(vals)
        avg  = round(sum(vals)/len(vals))
        top  = round(sum(s[len(s)*3//4:])/len(s[len(s)*3//4:])) if s else avg
        cities_out[key] = {"avg": avg, "top": top, "locs": len(vals)}
        all_nti.extend(vals)

    cities_out["default"] = {"avg": 8200, "top": 11600, "locs": 65}
    state_avg   = round(sum(all_nti)/len(all_nti)) if all_nti else 9347
    state_top25 = round(sorted(all_nti)[len(all_nti)*3//4]) if all_nti else 13200

    return {
        "data_month":  data_month,
        "fetched_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state_avg":   state_avg,
        "state_top25": state_top25,
        "cities":      cities_out,
    }


FALLBACK = {
    "data_month": "January 2026", "fetched_at": "2026-01-10T15:00:00Z",
    "state_avg": 9347, "state_top25": 13200,
    "cities": {
        "cicero": {"avg":9820,"top":13400,"locs":94}, "berwyn": {"avg":9240,"top":12800,"locs":78},
        "joliet": {"avg":8940,"top":12200,"locs":112}, "oak lawn": {"avg":9100,"top":12600,"locs":67},
        "rockford": {"avg":7820,"top":11200,"locs":143}, "springfield": {"avg":7640,"top":10800,"locs":98},
        "peoria": {"avg":7540,"top":10600,"locs":87}, "elgin": {"avg":8620,"top":12000,"locs":74},
        "waukegan": {"avg":7980,"top":11400,"locs":89}, "aurora": {"avg":8340,"top":11800,"locs":76},
        "decatur": {"avg":7200,"top":10200,"locs":62}, "bloomington": {"avg":7480,"top":10400,"locs":54},
        "champaign": {"avg":7360,"top":10200,"locs":48}, "moline": {"avg":7420,"top":10400,"locs":44},
        "normal": {"avg":7180,"top":10000,"locs":38}, "default": {"avg":8200,"top":11600,"locs":65},
    },
}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = FALLBACK
    if OUTPUT_FILE.exists():
        try: existing = json.loads(OUTPUT_FILE.read_text())
        except: pass

    year, month = target_month()
    label = datetime(year, month, 1).strftime("%B %Y")
    log.info(f"Target: {label}")

    raw = fetch_csv(year, month)
    if not raw:
        log.error("All fetch attempts failed — keeping existing data")
        existing["scrape_error"] = f"Fetch failed for {label}"
        existing["last_attempt_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        OUTPUT_FILE.write_text(json.dumps(existing, indent=2))
        sys.exit(1)

    try:
        data = parse_csv(raw, label)
    except Exception as e:
        log.error(f"Parse failed: {e}", exc_info=True)
        existing["scrape_error"] = str(e)
        OUTPUT_FILE.write_text(json.dumps(existing, indent=2))
        sys.exit(1)

    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    log.info(f"Done — {data['data_month']} — {len(data['cities'])} cities — state avg ${data['state_avg']:,}")


if __name__ == "__main__":
    main()
