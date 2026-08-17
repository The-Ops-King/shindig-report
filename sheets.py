"""Google Sheets output.

Four tabs:
  All Productions -- the full current dataset, rewritten each run
  New Today       -- only today's additions
  Organizations   -- deduped org view, shaped for a Go High Level import
  Run Log         -- appended per run, so silent breakage is visible

Writes go through batch update calls rather than per-cell writes; the Sheets
quota is 300 requests/minute and a 20k-row dataset would blow straight through
it one cell at a time.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

import config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

PRODUCTION_HEADERS = [
    "Show", "Venue", "Organization", "Address",
    "City", "State", "Postal", "Country",
    "Start", "End", "Venue Type",
    "Org Website", "Email", "Phone", "Facebook", "Instagram",
    "Source", "Source URL", "First Seen",
]

ORG_HEADERS = [
    "Organization", "City", "State", "Country", "Address",
    "Website", "Email", "Phone", "Facebook", "Instagram",
    "Productions", "Licensors", "Contact Status", "Last Checked",
]

# The Contacts tab is the authoritative enrichment cache. It is deliberately
# visible and hand-editable: put a real address in the Email column, set Source
# to "manual", and the pipeline will use it and never overwrite or re-fetch it.
CONTACT_HEADERS = [
    "Org Key", "Organization", "City", "State", "Website",
    "Email", "Phone", "Facebook", "Instagram",
    "Status", "Last Checked", "Source",
]

LOG_HEADERS = [
    "Run At (UTC)", "MTI", "Concord", "TRW", "In Scope", "New Today",
    "Orgs", "Orgs Fetched", "Cache Hits", "Manual", "With Contact",
    "Pct Contact",
    "Duration (s)", "Status", "Notes",
]


def client() -> gspread.Client:
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet(gc: gspread.Client):
    if not config.SHEET_ID:
        raise RuntimeError("SHEET_ID is not set")
    return gc.open_by_key(config.SHEET_ID)


def _tab(book, title: str, rows: int, cols: int):
    try:
        ws = book.worksheet(title)
    except gspread.WorksheetNotFound:
        return book.add_worksheet(title=title, rows=max(rows, 100),
                                  cols=max(cols, 20))
    # Grow before writing; gspread errors if the range exceeds the grid.
    if ws.row_count < rows:
        ws.add_rows(rows - ws.row_count)
    if ws.col_count < cols:
        ws.add_cols(cols - ws.col_count)
    return ws


def production_row(p, org) -> list:
    return [
        p.show_title,
        p.venue or p.organization,
        p.organization,
        p.address,
        p.city, p.state, p.postal, p.country,
        p.start_date.isoformat() if p.start_date else "",
        p.end_date.isoformat() if p.end_date else "",
        p.venue_type,
        p.org_website or (org.website if org else ""),
        org.email if org else "",
        org.phone if org else "",
        org.facebook if org else "",
        org.instagram if org else "",
        p.source.upper(),
        p.source_url,
        p.first_seen,
    ]


def org_row(o) -> list:
    return [
        o.name, o.city, o.state, o.country, o.street,
        o.website, o.email, o.phone, o.facebook, o.instagram,
        o.production_count, ", ".join(sorted(s.upper() for s in o.sources)),
        o.contact_status, o.contact_checked,
    ]


def _sort_key(p):
    return (p.start_date or date.max, p.organization)


# ~23k rows x 19 columns in a single update is a multi-megabyte request body.
# Chunking keeps each call well inside Google's request-size limit while
# staying far below the 300 req/min quota.
CHUNK_ROWS = 5000


def _write_rows(ws, rows: list) -> None:
    for start in range(0, len(rows), CHUNK_ROWS):
        block = rows[start:start + CHUNK_ROWS]
        ws.update(block, f"A{start + 1}", value_input_option="RAW")


def write_productions(book, title: str, productions: list, registry: dict):
    rows = [PRODUCTION_HEADERS]
    for p in sorted(productions, key=_sort_key):
        rows.append(production_row(p, registry.get(p.org_key)))

    ws = _tab(book, title, len(rows) + 10, len(PRODUCTION_HEADERS))
    ws.clear()
    _write_rows(ws, rows)
    ws.freeze(rows=1)
    log.info("wrote %d rows to %r", len(rows) - 1, title)


def write_orgs(book, registry: dict):
    ordered = sorted(
        registry.values(),
        key=lambda o: (-o.production_count, o.name),
    )
    rows = [ORG_HEADERS] + [org_row(o) for o in ordered]
    ws = _tab(book, config.TAB_ORGS, len(rows) + 10, len(ORG_HEADERS))
    ws.clear()
    _write_rows(ws, rows)
    ws.freeze(rows=1)
    log.info("wrote %d organizations", len(rows) - 1)


def read_contacts(book) -> dict:
    """Load the Contacts tab into the cache shape enrich.py expects.

    Rows the user has edited by hand (Source = "manual") come back flagged so
    nothing downstream re-fetches or overwrites them.
    """
    try:
        ws = book.worksheet(config.TAB_CONTACTS)
    except gspread.WorksheetNotFound:
        log.info("no %r tab yet; starting with an empty cache",
                 config.TAB_CONTACTS)
        return {}

    values = ws.get_all_values()
    if len(values) < 2:
        return {}

    header = [h.strip() for h in values[0]]
    try:
        idx = {name: header.index(name) for name in CONTACT_HEADERS}
    except ValueError:
        log.warning("%r tab headers do not match; ignoring it",
                    config.TAB_CONTACTS)
        return {}

    def cell(row, name):
        i = idx[name]
        return row[i].strip() if i < len(row) else ""

    cache = {}
    for row in values[1:]:
        key = cell(row, "Org Key")
        if not key:
            continue
        source = (cell(row, "Source") or "auto").lower()
        cache[key] = {
            "email": cell(row, "Email"),
            "phone": cell(row, "Phone"),
            "facebook": cell(row, "Facebook"),
            "instagram": cell(row, "Instagram"),
            "status": cell(row, "Status") or "found",
            "checked": cell(row, "Last Checked"),
            "website": cell(row, "Website"),
            "name": cell(row, "Organization"),
            "city": cell(row, "City"),
            "state": cell(row, "State"),
            "manual": source == "manual",
            "requests": 0,
        }
    log.info("loaded %d cached contacts from %r", len(cache),
             config.TAB_CONTACTS)
    return cache


def contact_row(key: str, entry: dict) -> list:
    return [
        key,
        entry.get("name", ""), entry.get("city", ""), entry.get("state", ""),
        entry.get("website", ""),
        entry.get("email", ""), entry.get("phone", ""),
        entry.get("facebook", ""), entry.get("instagram", ""),
        entry.get("status", ""), entry.get("checked", ""),
        "manual" if entry.get("manual") else "auto",
    ]


def write_contacts(book, cache: dict):
    """Rewrite the Contacts tab from the merged cache.

    Manual rows are carried through untouched -- enrichment never queues them,
    so their values arrive here exactly as they were read.
    """
    rows = [CONTACT_HEADERS]
    for key in sorted(cache, key=lambda k: (cache[k].get("name") or "").lower()):
        rows.append(contact_row(key, cache[key]))

    ws = _tab(book, config.TAB_CONTACTS, len(rows) + 10, len(CONTACT_HEADERS))
    ws.clear()
    _write_rows(ws, rows)
    ws.freeze(rows=1)
    manual = sum(1 for e in cache.values() if e.get("manual"))
    log.info("wrote %d contacts (%d manual) to %r", len(rows) - 1, manual,
             config.TAB_CONTACTS)


def append_log(book, entry: list):
    ws = _tab(book, config.TAB_LOG, 200, len(LOG_HEADERS))
    if not ws.get_values("A1:A1"):
        ws.update([LOG_HEADERS], "A1")
        ws.freeze(rows=1)
    ws.append_row(entry, value_input_option="RAW")


def publish(productions: list, new_today: list, registry: dict,
            log_entry: list, cache: dict | None = None, book=None) -> None:
    book = book or open_sheet(client())
    write_productions(book, config.TAB_ALL, productions, registry)
    write_productions(book, config.TAB_NEW, new_today, registry)
    write_orgs(book, registry)
    if cache is not None:
        write_contacts(book, cache)
    append_log(book, log_entry)
