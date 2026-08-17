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

LOG_HEADERS = [
    "Run At (UTC)", "MTI", "Concord", "TRW", "In Scope", "New Today",
    "Orgs", "Orgs Fetched", "Cache Hits", "With Contact", "Pct Contact",
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


def append_log(book, entry: list):
    ws = _tab(book, config.TAB_LOG, 200, len(LOG_HEADERS))
    if not ws.get_values("A1:A1"):
        ws.update([LOG_HEADERS], "A1")
        ws.freeze(rows=1)
    ws.append_row(entry, value_input_option="RAW")


def publish(productions: list, new_today: list, registry: dict,
            log_entry: list) -> None:
    book = open_sheet(client())
    write_productions(book, config.TAB_ALL, productions, registry)
    write_productions(book, config.TAB_NEW, new_today, registry)
    write_orgs(book, registry)
    append_log(book, log_entry)
