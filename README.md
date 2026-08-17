# Shindig Report

Daily scrape of **MTI**, **Concord Theatricals**, and **TRW** for licensed
theatre productions across the US and Canada, enriched with contact details
from each organization's own website, published to a Google Sheet, with a
morning email digest of what is new.

The point is outreach: every row is a company that has already paid for the
rights to stage a show, with dates, an address, and — where we can find one —
an email or phone number.

## What it collects

| Source | Method | Cost | What you get |
|---|---|---|---|
| **MTI** | One request to `map-search-ajax.php` returns the entire worldwide catalogue (~17.4k productions) | 1 request, ~4s | show, venue, organization, full street address, dates, venue website |
| **Concord** | One request to `NowPlayingMarkersSource` at zoom 16 returns all of North America (~9.4k markers) | 1 request, ~3s | show, producer, street address, city, coordinates, professional/amateur |
| **TRW** | 381 show pages from the sitemap, each carrying an "Upcoming Productions" block | 381 requests, ~12.8 min | show, organization, city/state, dates |

Measured against live data: MTI 17,434 raw / Concord 7,778 / TRW 1,082. After
scoping to US + Canada and dropping finished runs: **~23,700 productions across
~15,700 organizations**.

## How it works

```
scrape  →  geo-resolve  →  scope filter  →  organizations  →  enrich  →  publish
```

1. **Scrape** — the three sources run concurrently; TRW is the long pole.
2. **Geo-resolve** — Concord's markers endpoint never populates `state`
   (0 of 9,408), so state and country are derived from coordinates using MTI's
   ~15k geocoded venues as an offline reference set. No API, no extra requests,
   resolves >99% in about 0.1s.
3. **Scope** — US + Canada, dropping anything whose run has already ended.
4. **Organizations** — productions collapse to one record per
   `(normalized name, city, state)`. This is what makes enrichment cheap.
5. **Enrich** — contact details scraped from the organization's own site.
6. **Publish** — four Sheet tabs, plus the email digest.

### Enrichment rules

Two guarantees, both enforced in code and covered by tests:

**One organization is enriched once.** Enrichment only ever receives the
collapsed organization set — it has no access to the production list and
structurally cannot fetch a company once per show. Plano Children's Theatre
has 26 productions in the current data and costs exactly one fetch. Results
persist in `state/org_cache.json`, so tomorrow it costs zero.

**A cursory pass, then we stop.** Per organization, forever:

- at most **3 HTTP requests** (homepage + up to 2 contact pages the homepage
  actually links to — no URL guessing)
- 8s timeout, no retries
- stop on the first email found
- no headless browser, no JS rendering, no search fallback, no paid API
- a miss is cached for **90 days**, so a dead end costs 3 requests per quarter

### Cross-source website fill

MTI publishes a website for about 38% of its records. Concord and TRW publish
none. Because the same community theatres license from all three houses,
organizations are linked across sources two ways:

- **by name** — normalized (`The Old Courthouse Theatre, Inc.` → `old courthouse theatre`)
- **by street address** — normalized (`224 Polk Street` → `224 Polk St.`)

Address matching matters more than name matching, because Concord names the
*venue* where MTI names the *producing company*. Name matching alone links ~410
organizations; adding address matching takes it to ~980.

## The Sheet

| Tab | Contents |
|---|---|
| **All Productions** | Every live production. Show, Venue, Organization, Address lead the sheet. |
| **New Today** | Only today's additions, rewritten each run. |
| **Organizations** | Deduped org view with contact info — shaped for a Go High Level import. |
| **Run Log** | One row per run: counts, enrichment hit rate, duration. Makes silent breakage visible. |

## Setup

### 1. Google Sheet

Create a sheet, then create a Google Cloud service account with the Sheets API
enabled and download its JSON key. **Share the sheet with the service account's
email address as an Editor** — this is the step people miss.

### 2. Gmail app password

With 2FA enabled on the sending account, create an app password at
<https://myaccount.google.com/apppasswords>.

### 3. Repository secrets

| Secret | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The full service-account JSON, pasted as-is |
| `SHEET_ID` | The `/d/<this-part>/` of the sheet URL |
| `GMAIL_USER` | Sending Gmail address |
| `GMAIL_APP_PASSWORD` | The app password from step 2 |
| `REPORT_TO` | Where the digest goes |

### 4. Schedule

`.github/workflows/daily.yml` runs at 14:00 UTC — **7am Arizona (MST)**.
Arizona does not observe DST and GitHub cron is always UTC, so this stays at
7am year-round with no seasonal drift.

## Running it

```bash
pip install -r requirements.txt

python main.py --dry-run              # scrape only, writes out/preview.csv
python main.py --dry-run --skip trw   # fast iteration (skips the 13-min source)
python main.py --no-enrich            # skip contact lookup
python main.py --sheet-only           # write the Sheet, send no email
python main.py                        # full run
python notify.py --test               # send a sample digest

python -m pytest tests/ -q
```

## Cost

**$0/month**, by design.

| Component | Cost |
|---|---|
| GitHub Actions | Free — the repo is public, so Actions minutes are unlimited |
| Google Sheets API | Free — no billing tier; ~10 batched requests per run |
| Gmail SMTP | Free — 500 sends/day allowance, we send 1 |
| Scraping | Free — all endpoints public and unauthenticated |
| LLM / AI calls | **None.** Every parser is deterministic. |

Steady-state runtime is roughly 25 minutes/day, nearly all of it TRW's polite
2s crawl delay. The one-time bootstrap adds ~12 minutes for the initial
enrichment of ~5,000 organizations; every run after that enriches only
organizations never seen before.

If the budget ever tightens, the cheapest lever is dropping TRW to twice
weekly — it is the smallest dataset and by far the slowest scrape.

## Design notes worth knowing

**Concord has no usable ID.** The payload offers two, and both are traps.
`I` is the *venue* id — The Ashby Stage carries one `I` across ten different
shows, so keying on it collapses 9,408 productions into 6,241 and loses real
leads. `Uid` is a per-response sequence number: two identical requests seconds
apart share **zero** Uids, so keying on it would report every Concord
production as new, every morning, forever. The key is therefore a content hash
over `(productid, producer, address, city, startdate, enddate)`. Tests pin
both failure modes.

**Concord dates are `DD/MM/YYYY`.** Reading them as US order silently corrupts
every date past the 12th of a month.

**TRW end dates come in two shapes.** `23, 2026` (same month as the start) and
`Mar 14, 2027` (crosses a month). Runs crossing New Year need the start year
decremented. All three shapes are tested.

**Zero is a failure, not a quiet day.** Each source has a floor in
`config.MIN_EXPECTED`; falling below it raises rather than writing an empty
Sheet. The digest sends even on a zero-new day, because a silent morning is
indistinguishable from a broken cron.

## Scraping conduct

All three endpoints are public and unauthenticated. The scraper identifies
itself, rate-limits TRW to 0.5 req/s and Concord to 2 req/s, backs off on 5xx,
and reads only pages organizations publish for exactly this purpose.

Outreach itself is your responsibility — CAN-SPAM and (for the Canadian
organizations in scope) CASL apply, and CASL is materially stricter.
