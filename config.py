"""Configuration for the Shindig licensor scraper.

Every tunable lives here. Anything secret comes from the environment so the
GitHub Actions workflow can inject it without the values touching the repo.
"""

import os
from pathlib import Path

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
OUT_DIR = ROOT / "out"
FIXTURE_DIR = ROOT / "tests" / "fixtures"

SEEN_PATH = STATE_DIR / "seen.json"
ORG_CACHE_PATH = STATE_DIR / "org_cache.json"
OUTREACH_PATH = STATE_DIR / "outreach.json"
OPPORTUNITIES_PATH = STATE_DIR / "opportunities.json"

# --- Scope -----------------------------------------------------------------
# US + Canada. MTI accepts a `country` param but silently ignores it, so this
# filter is always applied client-side against the parsed address.
COUNTRIES = {"US", "CA"}

# Productions whose end date has passed are dropped; there is nobody left to
# call. No forward cutoff -- a company announcing a 2028 season is a lead.
DROP_ENDED = True

# --- HTTP ------------------------------------------------------------------
USER_AGENT = (
    "ShindigReport/1.0 (+https://github.com/the-ops-king/shindig-report) "
    "theatre production aggregator"
)
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
HTTP_BACKOFF = 2.0

# Per-host politeness. TRW's robots.txt carries two conflicting `User-agent: *`
# blocks (one declaring Crawl-delay: 10, the Yoast-generated one declaring
# none). 2s is a deliberate middle ground: 0.5 req/s single-threaded against a
# WordPress site, which keeps the daily Actions run inside the free tier. See
# the Cost section of the plan.
TRW_DELAY = 2.0
CONCORD_DELAY = 0.5

# --- Sources ---------------------------------------------------------------
MTI_ENDPOINT = "https://www.mtishows.com/map-search-ajax.php"
MTI_LIMIT = 30000

CONCORD_MARKERS = (
    "https://shop.concordtheatricals.com/NowPlaying/NowPlayingMarkersSource"
)
CONCORD_TABLE = (
    "https://shop.concordtheatricals.com/NowPlaying/NowPlayingTableSource"
)
CONCORD_REFERER = "https://shop.concordtheatricals.com/now-playing?embed=1"

# Bounding box covering the US, Canada, and Alaska.
CONCORD_BBOX = {"neLat": 72.0, "neLon": -52.0, "swLat": 23.0, "swLon": -172.0}
# Concord clusters markers purely by zoom, not by box size: this same box
# returns 95 clusters at zoom 12 and zero at zoom 16. So one request at 16
# returns the whole continent individually (~9,400 markers, <3s). The descent
# in the scraper is a fallback if that ever changes.
CONCORD_ZOOM = 16
CONCORD_ZOOM_STEP = 2
CONCORD_MAX_ZOOM = 20

TRW_SITEMAP = "https://www.theatricalrights.com/trw-shows-sitemap.xml"

# --- Enrichment ------------------------------------------------------------
# Hard budget. See "Rule 2 -- cursory pass, then give up" in the plan.
# A miss must cost 3 requests per quarter, never 3 per day.
ENRICH_MAX_REQUESTS_PER_ORG = 3
ENRICH_TIMEOUT = 8
# Enrichment fans out across thousands of *different* hosts, so a wide pool is
# not hard on any single site. Measured: ~0.35 s/org at 10 workers, which made
# the one-time bootstrap of ~5,000 orgs take 29 minutes; 24 brings it to ~12.
# Steady-state runs only ever see organizations never seen before.
ENRICH_CONCURRENCY = 24
# A successful lookup is kept forever: once an organization's contact is in the
# Contacts tab it is reused and never re-fetched, so an org is visited exactly
# once in its lifetime. Set ENRICH_REFRESH_FOUND=True (or pass --force-enrich)
# to re-check them on a TTL instead.
ENRICH_REFRESH_FOUND = False
ENRICH_SUCCESS_TTL_DAYS = 365
# Dead ends are retried a quarter later; transient failures within the week.
ENRICH_NOT_FOUND_TTL_DAYS = 90
ENRICH_ERROR_TTL_DAYS = 7

# Role addresses we prefer over personal ones for cold outreach.
PREFERRED_MAILBOXES = (
    "info", "boxoffice", "box_office", "admin", "contact", "hello",
    "office", "tickets", "theatre", "theater", "mail", "general",
)

# Emails that are never a real contact for the organization.
EMAIL_BLOCKLIST_SUBSTRINGS = (
    "example.com", "sentry.io", "wixpress.com", "squarespace.com",
    "godaddy.com", "wordpress.com", "@2x", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".svg", ".css", ".js",
)

# Placeholder addresses left in website templates. Measured on the live cache:
# 151 of 2,831 scraped addresses (5.3%) are these -- "user@domain.com" alone
# appears on 98 organizations. They are not merely useless: placeholder domains
# hard-bounce, and hard bounces are what destroy a cold sending domain.
PLACEHOLDER_DOMAINS = (
    "domain.com", "mysite.com", "website.com", "example.com", "example.org",
    "yourdomain.com", "yoursite.com", "email.com", "address.com",
    "yourcompany.com", "company.com", "test.com",
)
PLACEHOLDER_MAILBOXES = (
    "user", "example", "email", "yourname", "youremail", "your-email",
    "firstname", "firstname.lastname", "name", "test", "sample", "someone",
    "username", "myemail",
)
# Unattended mailboxes -- a reply goes nowhere, so outreach there is wasted.
UNATTENDED_MAILBOXES = ("noreply", "no-reply", "donotreply", "do-not-reply")
# Machine-generated addresses scraped from embedded widgets. 19 Google Calendar
# feed ids were sitting in the cache looking like contacts.
MACHINE_DOMAIN_SUFFIXES = ("group.calendar.google.com", "calendar.google.com")

# --- Outreach --------------------------------------------------------------
# Emailing Canada is deliberately off by default: CASL is materially stricter
# than CAN-SPAM and carries real penalties. Canadian data is still collected --
# this only governs who gets contacted. Flip when you have made that call.
OUTREACH_COUNTRIES = {"US"}

# A show must be far enough out that playbills are not ordered yet, and close
# enough to be real and budgeted.
OUTREACH_WINDOW_MIN_DAYS = 30
OUTREACH_WINDOW_MAX_DAYS = 120

# A cold domain sending thousands on day one loses the channel outright.
OUTREACH_DAILY_CAP = 25

# Minimum days between any two sends to one address. The median gap between an
# organization's consecutive shows is 63 days, but 24% of gaps are under 30 --
# without a floor, a company with a packed season gets an email every week.
OUTREACH_MIN_GAP_DAYS = 45

GHL_BASE = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"
GHL_API_KEY = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
GHL_WORKFLOW_ID = os.environ.get("GHL_WORKFLOW_ID", "")
GHL_PIPELINE_ID = os.environ.get("GHL_PIPELINE_ID", "")
GHL_PIPELINE_STAGE_ID = os.environ.get("GHL_PIPELINE_STAGE_ID", "")
GHL_TIMEOUT = 20

# Where the lead came from. "mass-ingestion" separates these from contacts who
# arrived by any other route; the licensor tag says which catalogue found them.
GHL_SOURCE_TAG = "mass-ingestion"
GHL_LICENSOR_TAGS = {"mti": "MTI", "concord": "Concord", "trw": "TRW"}

# Custom fields the GHL workflow's merge tags read. Create these in GHL first.
GHL_FIELDS = (
    "next_show_title", "next_show_start", "next_show_end",
    "next_show_venue", "next_show_city", "sample_playbill_url", "licensor",
)

# --- Delivery --------------------------------------------------------------
SHEET_ID = os.environ.get("SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
REPORT_TO = os.environ.get("REPORT_TO", "")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

TAB_ALL = "All Productions"
TAB_NEW = "New Today"
TAB_ORGS = "Organizations"
TAB_CONTACTS = "Contacts"
TAB_SHOW_LINKS = "Show Links"
TAB_OUTREACH = "Outreach Log"
TAB_LOG = "Run Log"

EMAIL_TABLE_LIMIT = 15

# A source returning zero rows means it broke, not that the world went quiet.
# These floors are deliberately far below observed volumes (MTI ~15.5k future
# worldwide, Concord ~11k, TRW ~1-2k) so normal drift never trips them.
MIN_EXPECTED = {"mti": 1000, "concord": 500, "trw": 50}


def sheet_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
