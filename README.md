# RouteIQ — IGB Data Backend

Fully automated pipeline that pulls Illinois Gaming Board (IGB) VGT revenue data monthly and makes it available to the RouteIQ frontend as a JSON file.

**Cost: $0. No accounts needed beyond GitHub (which you already have).**

---

## How it works

```
GitHub Actions (free)
  runs on the 10th of every month
       ↓
  Python + Playwright opens igbapps.illinois.gov
  Selects "Municipality" report, last month's date, clicks CSV download
       ↓
  Parser aggregates per-city NTI stats
  Writes data/igb_data.json
       ↓
  Git commits the file back to this repo
       ↓
RouteIQ frontend fetches:
  https://raw.githubusercontent.com/YOUR_USERNAME/routeiq-backend/main/data/igb_data.json
```

The frontend always shows data from the most recently committed `igb_data.json`. If a monthly scrape fails (IGB changes their form, site is down, etc.), the previous month's data stays intact in the repo — it doesn't break.

---

## One-time setup (15 minutes)

### 1. Create the GitHub repo

1. Go to github.com → New repository
2. Name it `routeiq-backend`
3. Set to **Private** (the data is public anyway, but keeps things clean)
4. Don't add a README (you'll push this code instead)

### 2. Push this code

```bash
cd routeiq-backend
git init
git add .
git commit -m "initial: RouteIQ IGB data pipeline"
git remote add origin https://github.com/YOUR_USERNAME/routeiq-backend.git
git push -u origin main
```

### 3. Enable GitHub Actions

Go to your repo → **Actions** tab → Click "I understand my workflows, go ahead and enable them"

That's it. The workflow runs automatically on the 10th of each month.

### 4. Run it manually to test

Go to **Actions** → **IGB Data Sync** → **Run workflow** → Click the green button.

Watch the logs. If it works, you'll see `data/igb_data.json` updated in the repo with a new commit.

### 5. Connect RouteIQ frontend

In `routeiq-v4.html`, replace the hardcoded `IGB` object fetch with:

```javascript
// Add this near the top of your <script> block
async function loadIGBData() {
  const url = 'https://raw.githubusercontent.com/YOUR_USERNAME/routeiq-backend/main/data/igb_data.json';
  try {
    const res = await fetch(url);
    const d   = await res.json();
    // Merge into the IGB object
    IGB.STATE_AVG    = d.state_avg;
    IGB.STATE_TOP25  = d.state_top25;
    IGB.DATA_DATE    = d.data_month;
    IGB.CITIES       = d.cities;
    console.log('IGB data loaded:', d.data_month);
  } catch (err) {
    console.warn('IGB fetch failed, using built-in data', err);
    // Falls back to hardcoded data already in IGB object — no crash
  }
}

// Call it before showing the dashboard
// In your launch() function, change:
//   initDashboard()
// to:
//   loadIGBData().then(() => initDashboard())
```

### 6. (Optional) Slack alerts on failure

If you want to be notified when a monthly scrape fails:

1. Create a Slack incoming webhook at api.slack.com/apps
2. Go to your repo → **Settings** → **Secrets and variables** → **Actions**
3. Add a secret named `SLACK_WEBHOOK` with the webhook URL

You'll get a Slack message if any monthly run fails.

---

## What's in this repo

```
.github/
  workflows/
    igb-sync.yml        ← GitHub Actions cron job (runs 10th of each month)

scripts/
  igb_scraper.py        ← Playwright scraper + CSV parser

data/
  igb_data.json         ← Auto-updated each month by the workflow
                           Also serves as the fallback if a scrape fails
```

---

## If the scraper breaks

The IGB occasionally redesigns their reports page. If the workflow starts failing:

1. Go to **Actions** → click the failed run → read the error log
2. Open `scripts/igb_scraper.py` and update the selectors in `scrape_igb()`
3. The most common breakage: IGB renames the form fields or adds a CAPTCHA
4. If they add a CAPTCHA, the fallback is to download the CSV manually once and
   commit it — the workflow will still push the parsed JSON on next run

The `data/igb_data.json` file is **never auto-deleted** — failed runs preserve
the last good data, so the frontend always has something to work with.

---

## Data structure

`data/igb_data.json`:

```json
{
  "data_month":  "January 2026",
  "fetched_at":  "2026-01-10T15:02:41Z",
  "state_avg":   9347,
  "state_top25": 13200,
  "cities": {
    "cicero":  { "avg": 9820, "top": 13400, "locs": 94 },
    "aurora":  { "avg": 8340, "top": 11800, "locs": 76 },
    ...
  }
}
```

- `avg`  = average NTI per licensed establishment in that city (monthly)
- `top`  = top-quartile NTI per establishment in that city
- `locs` = number of licensed VGT locations in that city
- All NTI figures normalised to a 6-VGT establishment basis for comparability

Source: Illinois Gaming Board monthly municipality revenue reports.
