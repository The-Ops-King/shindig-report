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

**One organization is enriched once — ever.** Enrichment only ever receives
the collapsed organization set; it has no access to the production list and
structurally cannot fetch a company once per show. Plano Children's Theatre has
26 productions in the current data and costs exactly one fetch.

That result is then stored in the **Contacts tab** of the Sheet, which is the
authoritative cache. Every run reads it first, so an organization already
listed there costs **zero requests** — not tomorrow, not next year. Only
organizations never seen before are looked up.

**A cursory pass, then we stop.** Per organization, forever:

- at most **3 HTTP requests** (homepage + up to 2 contact pages the homepage
  actually links to — no URL guessing)
- 8s timeout, no retries
- stop on the first email found
- no headless browser, no JS rendering, no search fallback, no paid API
- a hit is cached **permanently**; a miss for **90 days**, so a dead end costs
  3 requests per quarter rather than 3 per day

### Cross-source website fill

MTI publishes a website for about 38% of its records. Concord and TRW publish
none. Because the same community theatres license from all three houses,
organizations are linked across sources two ways:

- **by name** — normalized (`The Old Courthouse Theatre, Inc.` → `old courthouse theatre`)
- **by street address** — normalized (`224 Polk Street` → `224 Polk St.`)

Licensors also disagree on capitalisation — Concord shouts `BROADWAY PALM
DINNER THEATRE` where MTI writes `Broadway Palm Dinner Theatre`. Both resolve
to the same organization, and the Sheet shows a single canonical name so one
company reads as one lead.

Address matching matters more than name matching, because Concord names the
*venue* where MTI names the *producing company*. Name matching alone links ~410
organizations; adding address matching takes it to ~980.

## The Sheet

| Tab | Contents |
|---|---|
| **All Productions** | Every live production. Show, Venue, Organization, Address lead the sheet. |
| **New Today** | Only today's additions, rewritten each run. |
| **Organizations** | Deduped org view with contact info — shaped for a Go High Level import. |
| **Contacts** | The enrichment cache, one row per organization. Read before every run; only orgs missing here are looked up. Hand-editable — see below. |
| **Show Links** | Show title → your sample playbill URL. **You fill this in.** Pre-seeded with every title in the outreach window, ranked by production count. |
| **Outreach Log** | Append-only record of every send: who, which show, which link, GHL contact id. |
| **Run Log** | One row per run: counts, enrichment hit rate, duration. Makes silent breakage visible. |

### Fixing a contact by hand

The Contacts tab is meant to be edited. If a lookup found the wrong address, or
found nothing and you tracked one down yourself:

1. type the correct value into the **Email** (or Phone) cell
2. set that row's **Source** column to `manual`

That row is then frozen: it is never re-fetched and never overwritten, even
under `--force-enrich`. Automatic rows stay marked `auto` and follow the normal
rules. Deleting a row simply makes the organization eligible for lookup again.

## Outreach

Turns the report into pipeline: work out the show each contact is doing next,
attach your sample playbill for that title, push it to Go High Level, and enrol
them in a workflow that sends the intro and reminds them up to the show date.

**Two gates, and both must be open before anything is emailed.**

1. `OUTREACH_ENABLED` must be literally `true` — a repository variable, set in
   Settings → Variables. Until then a live run writes contacts and emails
   nobody, whatever the run request says. It does not abort: refusing to send
   should not become refusing to keep GHL current, so the run downgrades to
   `ingest` and reports the refusal in the morning digest.
2. `"outreach": "live"` must be asked for in `.github/run-request.json`.

That is deliberate. `"live"` is a one-word edit in a JSON file, and the people
on the other end have never heard of us — a mistaken send is not a mistake you
can take back. Two changes, in two places, one of them a settings change with
its own audit trail.

Set `"outreach"` in `.github/run-request.json`, or pass the matching flag
locally.

| Mode | What it does |
|---|---|
| `off` | Default. Nothing touches GHL. |
| `dry` | Selects and reports, calls nothing. The only safe way to look at a cold-email queue before it sends. |
| `ingest` | Writes **every** organization to GHL — contact, tags, custom fields, pipeline card — and enrols **nobody**. |
| `live` | The full thing: 25 a day, tagged and enrolled. |

`"outreach_limit": N` caps a run: the daily send in `live`, the number of
organizations written in `ingest`.

### Ingest — fill the CRM before the workflow exists

Building the workflow is a person's job: the email copy and the reminder timing
are yours. `ingest` decouples that from the data, so the CRM can fill up while
the sequence is still being written.

It needs **no `GHL_WORKFLOW_ID`** — that is the whole point. `configured()`
covers what writing contacts needs; `can_enrol()` adds the workflow id and is
only checked when something is actually about to be enrolled.

The load-bearing detail is that ingest **never records a send**. `record_send`
stamps `current_show_key`, and `_decide` answers `"none", "already current"` for
a show it believes is covered — so ingesting through the normal path would mark
all ~2,500 organizations as already told and none of them could ever be emailed.
Instead, anything that would have sent becomes a `hold`, which is written and
withheld.

**Companies mid-run are included.** `true_next_show` requires a start date in
the future, so a company whose show opened last week and is still on has no
*upcoming* show and would drop out of the run entirely — 150 of them in the
live data. Ingest takes them anyway, with blank `next_show_*` fields, and the
ledger refreshes them the moment they announce something. The address and
licensor tags come from the organization registry rather than a production.

**The one hard rule is the email.** `build_candidates` only ever produces a
candidate for an organization whose address passes `emails.is_sendable()`, so
placeholder domains, calendar-feed ids and unattended mailboxes never reach GHL.
Of 15,899 organizations, 2,699 qualify.

`opportunities.json` doubles as the ingest ledger. An organization is written
when it is new, when its next show changes, or when its ready state flips —
otherwise it is skipped. So the bootstrap is ~2,500 contacts once
(bounded per run and logged), then a handful a day forever.

The binding limit is the clock, not the count: `OUTREACH_INGEST_MAX_SECONDS`
stops the loop cleanly at 30 minutes. A run that instead hit the job timeout
mid-ingest would never reach the state commit, losing the ledger and repeating
the whole bootstrap. Stopping early keeps what it wrote and resumes next run.

**The 7am schedule runs `ingest`.** New organizations arrive daily, so the CRM
keeps filling on its own. It still enrols nobody and sends nothing.

**Two tags, doing different jobs:**

- `shindig-outreach` — starts the sequence. Only ever set by a real send.
- `shindig-ready` — *would* be sendable right now: next show in the window, in
  an enabled country, and with a sample link. Ingest sets this one.

`shindig-ready` exists so that bulk-tagging by hand is safe. Starting people
from the GHL UI bypasses every guard the code applies — the daily cap, the
window, the 45-day gap, and the sample link. That last one matters most: the
pitch is "here's what *your* playbill could look like", and an email whose one
asset is missing is worse than no email. Filter on `mass-ingestion` +
`shindig-ready` and everyone you get has a working link.

Because upsert merges tags and never removes one, `shindig-ready` is stored on
the opportunity record and written only on a transition — no tag call for the
~1,800 organizations that are not ready on a given day.

**One consequence of tagging by hand:** it is invisible to `outreach.json`. If
you bulk-tag 300 people and later switch to `live`, the code still considers
those 300 un-contacted and may email them again. Either keep sending manual, or
reconcile first by reading contacts carrying `shindig-outreach` back out of GHL
and stamping them as contacted.

### What it does in GHL

A handful of endpoints, nothing else — no tasks, no notes, no SMS, no sending:

| Call | When |
|---|---|
| `POST /contacts/upsert` | Every organization we touch |
| `POST /opportunities/` or `PUT /opportunities/{id}` | Every organization — one card each |
| `DELETE` then `POST /contacts/{id}/tags` | Whenever someone enters or leaves the sequence |
| `DELETE` then `POST /contacts/{id}/workflow/{id}` | Only the ones actually being emailed |

The contact carries **what** they are doing next and **when**:
`next_show_title`, `next_show_start`, `next_show_end`, `next_show_venue`,
`next_show_city`, `sample_playbill_url`, `licensor` — plus tags recording where
the lead came from: `mass-ingestion` and `MTI` / `Concord` / `TRW`.

Fields are addressed **by id**, via `GHL_FIELD_IDS`. A fieldKey that matches
nothing is not an error in GHL — it is accepted and dropped, so the merge tag
renders empty with nothing anywhere to say why. `python setup_ghl.py` (no
`--apply`) prints every field already in your location and a paste-ready
`GHL_FIELD_IDS` block.

That inventory is also how you adopt a field you already have under another
name rather than creating a near-duplicate beside it. The report flags anything
that looks similar but never adopts on its own: a field whose name is close but
whose meaning is not would silently mis-fill a merge tag. It also warns when
`next_show_start` or `next_show_end` resolves to something that is not a Date —
a reminder sequence cannot be scheduled off a text box or a dropdown, and
changing a field's type later means recreating it and re-syncing every contact.

**Entry is driven by a tag, not by the API alone.** The workflow triggers on
`shindig-outreach` being added, so you can put someone in or pull them out by
hand in the UI, or from another automation, and it behaves exactly as if this
code had done it. The code owns show state; GHL owns the messaging.

**Two different units of identity, on purpose:**

- the **organization** is the unit of *pipeline* — each company buys its own
  playbills, so each gets one opportunity card, refreshed as its show rolls
  forward rather than a new card per season;
- the **address** is the unit of *email* — a shared inbox receives one message
  no matter how many companies sit behind it.

Live numbers: 2,511 organizations, 2,030 distinct inboxes. So 481 companies
share an inbox with another — each still tracked, only one emailed.

### The guarantees

**One email per person, not per organization.** 416 addresses in the live data
are shared across several organizations — keying on the org would send 643
duplicate emails, and `mail@haletheater.org` alone covers five. Addresses are
deduped and the soonest-opening show wins, so one inbox hears about one show.

**Junk is never emailed.** 189 of 2,851 scraped addresses are unusable: 143
website-template placeholders (`user@domain.com` sits on **98** organizations),
25 Google Calendar feed ids, 13 malformed, plus placeholder and unattended
mailboxes. Those hard-bounce, which is what wrecks a cold sending domain.

**No sample link, no send.** The pitch is "here's what *your* playbill could
look like". Without a real sample for their show it falls flat, so they wait for
a future run instead.

### Rolling forward to the next show

The Little Mermaid closes; Shrek is next. Two halves, and they are separate:

**The data rolls itself.** `DROP_ENDED` removes any production whose end date
has passed, so the closed show leaves the data and `true_next_show` returns the
next one. `needs_ingest` sees the changed `show_key` and re-pushes the contact,
moving every `next_show_*` field and the sample link to Shrek and renaming the
card. This happens on the 7am run with no configuration at all.

**The sequence is re-armed, but only for people already in it.** A contact
whose show rolls over re-enters the workflow with the new show's link — if, and
only if, they **already carry `shindig-outreach`**. That tag is set only by a
real send or by you tagging someone by hand, so a re-arm can continue a
conversation a person started and can never begin one. That is why ingest is
allowed to do this while `OUTREACH_ENABLED` is still shut: the branch is
unreachable for anyone who has not been contacted.

The tag is read back off the contact rather than assumed, so untagging someone
in the GHL UI stops their re-arms. Three further conditions: the candidate must
be `ready` (in window, enabled country, sample link present — rolling into a
show with no sample would send the "here's what *your* playbill could look
like" pitch with its one asset missing), and at least `OUTREACH_MIN_GAP_DAYS`
must have passed since the last re-arm, since 24% of consecutive-show gaps are
under 30 days. `REARM_ON_ROLLOVER=false` turns it off.

Mechanically it is `enrol()`: remove from the workflow, drop the tag, re-add
it. Same two silent no-ops to step around, same fix.

### Where the dates come from, and why ~9.5% of MTI has none

The pitch is *"Annie opens in 30 days — want a playbill?"*, so the only date
worth publishing is a real opening night.

Each licensor publishes **one** start/end pair, and it means "the period this
licence covers" — usually the run, sometimes a multi-year rights window. MTI
returns 16 fields and none of them distinguish the two; `venue_type`,
`show_type` and `active` were all tested and none correlate (Schools have the
*highest* long-span rate, at 13.5%, because a district buys one licence
covering several years). Span length is the only signal that exists.

| MTI span | Share | |
|---|---|---|
| ≤ 31 days | 88.2% | a real run — median is 3 days |
| 32–120 days | 2.3% | still a run, professional sit-downs |
| 121–365 days | 9.3% | rights window |
| over a year | 0.2% | up to 3,270 days |

The long ones are touring producers and rights agents — Networks
Presentations, Running Subway, Mini Musicals On The Move — and school
districts. **Concord and TRW are clean**: both sit near 0.3% over 120 days.
This is an MTI-only characteristic, not a parsing bug; the values are faithful
to what is published.

So anything over 120 days is classed `license window` and **publishes no
dates**: Start and End are empty in the Sheet, `next_show_start` is empty in
GHL. `2024-11-01 → 2029-01-30` reads as a fact and is worse than an empty cell,
which reads as the truth — we do not know when this plays. `Run Days` still
carries the raw span so the row explains itself.

Those organizations stay in the data and in GHL. A touring producer buys
playbills, quite possibly more of them than a community theatre; it simply can
never qualify for a date-driven email, because there is no opening night to key
one to. Two locks enforce that: `true_next_show` never selects a window, and
`custom_fields` refuses to write one's dates even if it somehow arrives.

Every run reports the per-source share of rows with no usable date, so a change
in a feed — or a heuristic that starts eating real runs — shows up immediately
rather than being noticed in the Sheet months later.

### Show lifecycle

Each address has a *current show*. When it passes, the contact rolls forward:

| State | What happens |
|---|---|
| No record | Upsert, enrol, send |
| Same show | Nothing |
| Show changed, new one in window + has link + gap ok | Update fields, re-enrol, sequence restarts |
| Show changed, out of window or no link | **GHL updated, no email** — the CRM stays truthful |
| No upcoming show | **Cleared** — tag removed, workflow exited, show fields blanked, card left where it is |

A clear is why nobody gets stranded mid-sequence. An organization whose show has
been and gone simply stops appearing in the candidate list, so without an
explicit sweep of the contacts we have already emailed, they would sit in the
workflow forever. The sweep is guarded on having a current show recorded, which
makes it both cheap and idempotent: only people we actually emailed are
eligible, and clearing empties that record, so a dormant organization is cleared
once rather than every morning. Their opportunity card stays exactly where it
is — the company has not gone away, it just has nothing on right now — and the
show fields are blanked so a hand re-add can never render a show that has
already happened.

A **45-day floor** between sends stops a company with a packed season being
emailed every fortnight; 24% of consecutive-show gaps are under 30 days.

**Enrolment removes before it adds — twice over.** Two GoHighLevel behaviours
look exactly like success while doing nothing at all:

- *Allow Re-Entry* does not admit a contact still **active** in a workflow. It
  skips the request silently, with no error — and this workflow keeps people
  active right up to their show date.
- A tag trigger fires only when the tag is **newly added**. Re-adding a tag the
  contact already carries is not an error either; it is simply nothing.

So a rollover drops them from the workflow, takes the tag off, puts it back, and
adds them directly as well. The `DELETE` is what actually stops a running
sequence, since removing a tag does not; the direct add is what still enrols
someone if the tag trigger is mis-wired. Whichever fires first, the other is a
no-op, so nobody is enrolled twice.

### Before the first send — do these in GHL

1. Create the pipeline and note its id and first stage id (optional — without
   them contacts and workflows still work, just no cards).
2. Run `python setup_ghl.py` to inventory the custom fields you already have,
   then create whatever is genuinely missing of `next_show_title`,
   `next_show_start`, `next_show_end`, `next_show_venue`, `next_show_city`,
   `sample_playbill_url`, `licensor`. The two dates **must** be Date type, or
   the reminder sequence has nothing to schedule against.
3. Build the workflow (intro email + reminders keyed off `next_show_start`).
4. Trigger it on **Contact Tag added = `shindig-outreach`**, and enable
   **Allow Re-Entry**.
5. Set the unsubscribe link and physical postal address CAN-SPAM requires.
6. Fill in the **Show Links** tab — the top 100 titles cover 54% of volume.

Canada is excluded from outreach by default (`OUTREACH_COUNTRIES = {"US"}`):
CASL is materially stricter than CAN-SPAM. Canadian data is still collected.

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
| `GHL_API_KEY` | GHL private integration token (outreach only) |
| `GHL_LOCATION_ID` | GHL location/sub-account id |
| `GHL_WORKFLOW_ID` | The workflow to enrol contacts into |
| `GHL_PIPELINE_ID` | Pipeline the opportunity cards live in (optional) |
| `GHL_PIPELINE_STAGE_ID` | Stage new cards enter at (optional) |

`GHL_WORKFLOW_ID` is only needed for `live`. Without the two pipeline ids the
run still writes contacts, tags and fields — it just creates no cards, silently.

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
python main.py --outreach-dry-run     # build the send queue, touch no GHL
python main.py --outreach --outreach-limit 1   # one real send

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
2s crawl delay. The one-time bootstrap adds **~37 minutes** for the initial
enrichment of ~5,000 organizations (measured, not estimated). Every run after
that only looks up organizations missing from the Contacts tab, which is
typically a handful.

If the budget ever tightens, the cheapest lever is dropping TRW to twice
weekly — it is the smallest dataset and by far the slowest scrape.

## Measured results

From a full live run (August 2026):

| | |
|---|---|
| Productions in scope (US + CA) | 22,646 |
| Distinct organizations | 14,868 |
| Organizations with a website | 4,977 (33.5%) |
| — of those, contact found | 3,553 (**71%**) |
| Organizations with a contact | 3,553 (**23.9%** of all) |
| Requests spent enriching | 7,423 — **1.49 per organization**, cap is 3 |

The gap is not enrichment failing; it is that two thirds of these
organizations publish no website through any licensor. Where a site exists,
the cursory pass finds a contact roughly seven times in ten.

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

**The date pair is a licence period, not always a performance run.** Each
licensor publishes one start/end pair meaning "the period this licence
covers". Usually that is the run: the median MTI gap is **3 days**, a normal
weekend school production, and 57% are 3 days or fewer. But ~10% exceed 120
days and the longest is **3,270 days** — "Mini Musicals On The Move" holds
*The Music Man* from May 2018 to May 2027. That is a touring producer's rights
window, not nine years of performances.

This is the licensors' own data, not a parsing error: MTI publishes both ISO
fields and a human-readable `date_range`, and ours agree with theirs on
**17,433 of 17,433** records (a test pins this). So rather than hide it, the
Sheet labels it in a **Date Type** column:

| Date Type | Gap | Read it as |
|---|---|---|
| `performance run` | ≤ 14 days | Real show dates — the common case |
| `extended run` | 15–120 days | A professional house running a season |
| `license window` | > 120 days | A rights window; actual dates unknown |

Both are real leads, but they are different leads — someone opening in six
weeks is a very different call from someone holding a multi-year touring
licence. Filter on this column rather than trusting every date as a run.

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
