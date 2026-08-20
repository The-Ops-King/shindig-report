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
    "Start", "End", "Run Days", "Date Type", "Venue Type",
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

# Hand-filled: paste a sample playbill URL next to a title and the pipeline
# starts using it on the next run. Pre-seeded with every title in the outreach
# window, ranked by production count, so the highest-value rows sit at the top.
SHOW_LINK_HEADERS = ["Show Title", "Sample URL", "Productions", "Notes"]

OUTREACH_HEADERS = [
    "Date", "Email", "Organization", "Show", "Show Start", "Show End",
    "Sample URL", "Action", "Send #", "GHL Contact", "Status", "Detail",
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
    book = gc.open_by_key(config.SHEET_ID)
    # Name the document explicitly. "The run succeeded but I see nothing in my
    # sheet" is almost always a SHEET_ID pointing at a different spreadsheet,
    # and without this the logs cannot tell you which one was written.
    log.info("opened spreadsheet %r -> %s", book.title, book.url)
    log.info("existing tabs: %s", [ws.title for ws in book.worksheets()])
    return book


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
        # The canonical name, not this licensor's raw spelling, so one company
        # reads as one lead rather than several.
        org.name if org else p.organization,
        p.address,
        p.city, p.state, p.postal, p.country,
        p.start_date.isoformat() if p.start_date else "",
        p.end_date.isoformat() if p.end_date else "",
        p.run_days if p.run_days is not None else "",
        p.date_type,
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


def read_show_links(book) -> dict:
    """Normalised title -> sample URL, for titles that have one.

    Blank URLs are simply absent from the mapping, which is what makes
    "no link, no send" work without any extra flag.
    """
    import outreach
    try:
        ws = book.worksheet(config.TAB_SHOW_LINKS)
    except gspread.WorksheetNotFound:
        log.info("no %r tab yet; no sample links available",
                 config.TAB_SHOW_LINKS)
        return {}

    values = ws.get_all_values()
    if len(values) < 2:
        return {}
    header = [h.strip() for h in values[0]]
    try:
        t_i, u_i = header.index("Show Title"), header.index("Sample URL")
    except ValueError:
        log.warning("%r headers do not match; ignoring", config.TAB_SHOW_LINKS)
        return {}

    links = {}
    for row in values[1:]:
        title = row[t_i].strip() if t_i < len(row) else ""
        url = row[u_i].strip() if u_i < len(row) else ""
        if title and url:
            links[outreach.normalize_title(title)] = url
    log.info("loaded %d sample links from %r", len(links), config.TAB_SHOW_LINKS)
    return links


def seed_show_links(book, title_counts: dict) -> int:
    """Add any title we have never listed, keeping every URL already typed in.

    Rewriting the tab wholesale would erase hand-entered links, so existing
    rows are read back and preserved verbatim; only genuinely new titles are
    appended. Ordered by production count so the rows worth filling come first.
    """
    try:
        ws = book.worksheet(config.TAB_SHOW_LINKS)
        existing = ws.get_all_values()
    except gspread.WorksheetNotFound:
        ws, existing = None, []

    kept, seen = {}, set()
    if len(existing) >= 2 and existing[0]:
        header = [h.strip() for h in existing[0]]
        idx = {n: header.index(n) for n in SHOW_LINK_HEADERS if n in header}
        for row in existing[1:]:
            def cell(name):
                i = idx.get(name)
                return row[i].strip() if i is not None and i < len(row) else ""
            title = cell("Show Title")
            if not title:
                continue
            kept[title] = [title, cell("Sample URL"),
                           title_counts.get(title, cell("Productions")),
                           cell("Notes")]
            seen.add(title)

    added = 0
    for title, count in sorted(title_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if title not in seen:
            kept[title] = [title, "", count, ""]
            added += 1

    rows = [SHOW_LINK_HEADERS] + sorted(
        kept.values(), key=lambda r: (-int(r[2] or 0), r[0].lower())
    )
    ws = _tab(book, config.TAB_SHOW_LINKS, len(rows) + 10, len(SHOW_LINK_HEADERS))
    ws.clear()
    _write_rows(ws, rows)
    ws.freeze(rows=1)
    filled = sum(1 for r in kept.values() if r[1])
    log.info("%r: %d titles (%d added, %d with links)",
             config.TAB_SHOW_LINKS, len(kept), added, filled)
    return added


def append_outreach(book, rows: list) -> None:
    """Append sends to the log; never rewrite, this is the audit trail."""
    if not rows:
        return
    ws = _tab(book, config.TAB_OUTREACH, 200, len(OUTREACH_HEADERS))
    if not ws.get_values("A1:A1"):
        ws.update([OUTREACH_HEADERS], "A1")
        ws.freeze(rows=1)
    ws.append_rows(rows, value_input_option="RAW")
    log.info("appended %d rows to %r", len(rows), config.TAB_OUTREACH)


def outreach_row(cand, today, status: str, detail: str = "") -> list:
    p = cand.production
    # A clear has no production: the show columns stay blank, and the send
    # count is what it already was, because nothing was sent.
    show = [p.show_title,
            p.start_date.isoformat() if p.start_date else "",
            p.end_date.isoformat() if p.end_date else ""] if p else ["", "", ""]
    number = cand.sends if cand.action in ("clear", "hold") else cand.sends + 1
    return [
        today.isoformat(), cand.address, cand.org_name, *show,
        cand.sample_url, cand.action, number,
        cand.ghl_contact_id, status, detail,
    ]


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
