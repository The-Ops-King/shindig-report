"""Fixture-based parser tests. No network.

The point of these is that a licensor changing their markup fails a test
rather than silently emptying the Sheet.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from normalize import (  # noqa: E402
    normalize_org_name, org_key, parse_mti_address, state_code,
)
from scrapers import concord, mti, trw  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def load(name):
    with open(FIX / name, encoding="utf-8") as fh:
        return fh.read()


# --- MTI -------------------------------------------------------------------

def test_mti_parses_fixture():
    rows = json.loads(load("mti_sample.json"))["data"]
    prods = mti.parse(rows)
    assert prods, "MTI fixture produced no productions"
    p = prods[0]
    assert p.key.startswith("mti:")
    assert p.show_title and p.organization


def test_mti_key_is_native_id():
    rows = json.loads(load("mti_sample.json"))["data"]
    prods = mti.parse(rows)
    assert prods[0].key == f"mti:{rows[0]['id']}"


def test_mti_keys_unique():
    rows = json.loads(load("mti_sample.json"))["data"]
    keys = [p.key for p in mti.parse(rows)]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("raw,street,city,st,postal,country", [
    ("224 Polk Street<br>Raleigh, NC<br>27604<br>US",
     "224 Polk Street", "Raleigh", "NC", "27604", "US"),
    ("100 Main St<br>Suite 2<br>Toronto, ON<br>M5V 2T6<br>CA",
     "100 Main St, Suite 2", "Toronto", "ON", "M5V 2T6", "CA"),
    ("210 Cypress Gardens Blvd<br>Winter Haven, FL<br>33880<br>US",
     "210 Cypress Gardens Blvd", "Winter Haven", "FL", "33880", "US"),
])
def test_mti_address_shapes(raw, street, city, st, postal, country):
    got = parse_mti_address(raw)
    assert got["street"] == street
    assert got["city"] == city
    assert got["state"] == st
    assert got["postal"] == postal
    assert got["country"] == country


def test_mti_address_country_survives_foreign_formats():
    """Street/city parsing is tuned for US+CA, but the country segment must
    still be read correctly -- it is what the scope filter runs on."""
    for raw, want in [
        ("The Hall<br>London<br>W1<br>GB", "GB"),
        ("Storgata 1<br>Oslo<br>0155<br>Norway", "Norway"),
    ]:
        assert parse_mti_address(raw)["country"] == want


# --- Concord ---------------------------------------------------------------

def test_concord_parses_fixture():
    markers = json.loads(load("concord_markers.json"))["Markers"]
    indexed = {concord.marker_key(m): m
               for m in markers if m.get("Type") == 1 and m.get("name")}
    prods = concord.to_productions(indexed)
    assert prods
    assert all(p.source == "concord" for p in prods)
    assert any(p.street for p in prods)


def test_concord_dates_are_day_first():
    """Concord serialises DD/MM/YYYY; reading it as US order silently
    corrupts every date past the 12th of a month."""
    markers = json.loads(load("concord_markers.json"))["Markers"]
    m = next(x for x in markers
             if x.get("Type") == 1 and (x.get("startdate") or "").strip())
    prods = concord.to_productions({concord.marker_key(m): m})
    day, month, year = (int(v) for v in m["startdate"].split("/"))
    assert prods[0].start_date == date(year, month, day)


def test_concord_key_ignores_unstable_uid():
    """Uid changes on every response; a key built from it would report the
    whole catalogue as new every morning."""
    markers = json.loads(load("concord_markers.json"))["Markers"]
    m = dict(next(x for x in markers if x.get("Type") == 1))
    before = concord.marker_key(m)
    m["Uid"] = (m.get("Uid") or 0) + 999999
    assert concord.marker_key(m) == before


def test_concord_key_separates_shows_at_one_venue():
    """`I` is the venue id, shared across every show a venue stages -- keying
    on it would collapse distinct productions into one lead."""
    markers = json.loads(load("concord_markers.json"))["Markers"]
    a = dict(next(x for x in markers if x.get("Type") == 1))
    b = dict(a)
    b["name"] = "A Completely Different Show"
    b["productid"] = "999999"
    assert a.get("I") == b.get("I")
    assert concord.marker_key(a) != concord.marker_key(b)


def test_concord_keys_stable_across_identical_input():
    markers = json.loads(load("concord_markers.json"))["Markers"]
    first = [concord.marker_key(m) for m in markers]
    second = [concord.marker_key(json.loads(json.dumps(m))) for m in markers]
    assert first == second


# --- TRW -------------------------------------------------------------------

def test_trw_parses_upcoming_block():
    html = load("trw_swept_away.html")
    prods = trw.parse_show_page(
        html, "https://www.theatricalrights.com/show/swept-away/")
    assert prods
    orgs = {p.organization for p in prods}
    assert "Apollo Civic Theatre" in orgs


def test_trw_dedupes_repeated_blocks():
    """TRW renders some listings twice on the page."""
    html = load("trw_swept_away.html")
    prods = trw.parse_show_page(
        html, "https://www.theatricalrights.com/show/swept-away/")
    seen = [(p.organization, p.city, p.start_date, p.end_date) for p in prods]
    assert len(seen) == len(set(seen))


def test_trw_no_upcoming_section_returns_empty():
    assert trw.parse_show_page("<html><body>nothing</body></html>", "u") == []


@pytest.mark.parametrize("start,end,want_start,want_end", [
    # Same month: the end carries only a day and the year.
    ("Aug 06", "23, 2026", date(2026, 8, 6), date(2026, 8, 23)),
    # Crosses a month boundary: the end carries its own month.
    ("Feb 20", "Mar 14, 2027", date(2027, 2, 20), date(2027, 3, 14)),
    # Crosses New Year: the start belongs to the previous year.
    ("Dec 28", "Jan 03, 2027", date(2026, 12, 28), date(2027, 1, 3)),
    ("May 07", "17, 2027", date(2027, 5, 7), date(2027, 5, 17)),
])
def test_trw_date_shapes(start, end, want_start, want_end):
    assert trw.parse_dates(start, end) == (want_start, want_end)


def test_trw_rejects_garbage_dates():
    assert trw.parse_dates("", "") == (None, None)
    assert trw.parse_dates("not a date", "also not") == (None, None)


def test_trw_key_is_stable():
    html = load("trw_swept_away.html")
    url = "https://www.theatricalrights.com/show/swept-away/"
    a = [p.key for p in trw.parse_show_page(html, url)]
    b = [p.key for p in trw.parse_show_page(html, url)]
    assert a == b and len(a) == len(set(a))


# --- normalization ---------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("The Old Courthouse Theatre, Inc.", "old courthouse theatre"),
    ("Smith & Jones Players LLC", "smith and jones players"),
    ("Burning Coal Theatre Company", "burning coal theatre co."),
    ("Theatre Winter Haven", "  theatre   winter  haven  "),
])
def test_org_name_normalization(a, b):
    assert normalize_org_name(a) == normalize_org_name(b)


def test_org_name_normalization_keeps_meaningful_words():
    """Corporate suffixes are noise; 'Theatre' and 'Repertory' are identity.
    Stripping them would merge genuinely different companies in one city."""
    assert normalize_org_name("Raleigh Repertory") != \
        normalize_org_name("Raleigh Theatre")


def test_org_key_separates_same_name_different_city():
    """Chapter-style names repeat nationwide and must not merge into one lead."""
    assert org_key("Community Players", "Fresno", "CA") != \
        org_key("Community Players", "Raleigh", "NC")


@pytest.mark.parametrize("raw,want", [
    ("Maine", "ME"), ("New York", "NY"), ("ny", "NY"),
    ("Ontario", "ON"), ("british columbia", "BC"),
])
def test_state_code(raw, want):
    assert state_code(raw) == want


# --- what the date pair actually means -------------------------------------

@pytest.mark.parametrize("start,end,days,kind", [
    # A normal weekend school run -- the median MTI record is 3 days.
    (date(2026, 12, 10), date(2026, 12, 13), 3, "performance run"),
    (date(2026, 12, 10), date(2026, 12, 10), 0, "performance run"),
    (date(2026, 12, 1), date(2026, 12, 15), 14, "performance run"),
    # A professional house running a show for a season.
    (date(2026, 1, 18), date(2026, 2, 18), 31, "extended run"),
    # A touring producer's multi-year rights window, not a performance run.
    (date(2018, 5, 18), date(2027, 5, 1), 3270, "license window"),
    (date(2026, 1, 1), date(2026, 12, 31), 364, "license window"),
])
def test_date_type_classification(start, end, days, kind):
    from normalize import Production
    p = Production(key="k", source="mti", show_title="S", organization="O",
                   start_date=start, end_date=end)
    assert p.run_days == days
    assert p.date_type == kind


def test_date_type_handles_missing_and_reversed_dates():
    from normalize import Production
    none = Production(key="k", source="mti", show_title="S", organization="O")
    assert none.run_days is None and none.date_type == "unknown"
    backwards = Production(key="k", source="mti", show_title="S",
                           organization="O", start_date=date(2026, 5, 1),
                           end_date=date(2026, 4, 1))
    assert backwards.date_type == "invalid"


def test_mti_start_end_match_its_own_date_range_text():
    """MTI publishes both ISO fields and a human-readable range. They must
    agree, or we are misreading the feed rather than reporting it."""
    import datetime
    import re
    rows = json.loads(load("mti_sample.json"))["data"]
    checked = 0
    for r in rows:
        m = re.match(r"^(.+?) to (.+)$", (r.get("date_range") or "").strip())
        if not m:
            continue
        fmt = "%b %d, %Y"
        start = datetime.datetime.strptime(m.group(1).strip(), fmt).date()
        end = datetime.datetime.strptime(m.group(2).strip(), fmt).date()
        assert start.isoformat() == r["start"]
        assert end.isoformat() == r["end"]
        checked += 1
    assert checked, "fixture carried no parseable date_range values"


# --- a licence window is not a date -------------------------------------------

def _span(days, start=date(2026, 9, 1), source="mti"):
    from normalize import Production
    return Production(
        key=f"{source}:x", source=source, show_title="The Music Man",
        organization="Mini Musicals On The Move", venue="Touring",
        city="Raleigh", state="NC", country="US", street="1 Main St",
        start_date=start, end_date=start + timedelta(days=days),
    )


def test_a_three_thousand_day_span_publishes_no_dates():
    """Mini Musicals On The Move really does hold The Music Man from May 2018
    to May 2027. That is a rights window, not nine years of performances."""
    import sheets
    p = _span(3270, start=date(2018, 5, 18))
    assert p.date_type == "license window"
    assert not p.has_real_dates
    row = sheets.production_row(p, None)
    start_i = sheets.PRODUCTION_HEADERS.index("Start")
    end_i = sheets.PRODUCTION_HEADERS.index("End")
    assert row[start_i] == "" and row[end_i] == ""
    # The row still explains itself rather than going silently blank.
    assert row[sheets.PRODUCTION_HEADERS.index("Run Days")] == 3270
    assert row[sheets.PRODUCTION_HEADERS.index("Date Type")] == "license window"


@pytest.mark.parametrize("days,dated", [
    (0, True),      # a one-night school show
    (3, True),      # the median MTI run
    (17, True),
    (120, True),    # a long professional sit-down still has an opening night
    (121, False),
    (845, False),
])
def test_only_real_runs_publish_dates(days, dated):
    import sheets
    p = _span(days)
    assert p.has_real_dates is dated
    row = sheets.production_row(p, None)
    assert bool(row[sheets.PRODUCTION_HEADERS.index("Start")]) is dated


def test_a_licence_window_is_never_the_next_show():
    """The guarantee that matters: GHL's next_show_start can only ever hold a
    real opening night, so the pitch is "Annie opens in 30 days" and never
    "you hold the rights to Annie"."""
    import outreach
    today = date(2026, 8, 22)
    window = _span(1500, start=today + timedelta(days=5))   # soonest by far
    real = _span(3, start=today + timedelta(days=60))
    assert outreach.true_next_show([window, real], today) is real


def test_a_window_never_reaches_the_ghl_date_field():
    import ghl, outreach
    cand = outreach.Candidate(address="a@t.org", org_key="k", org_name="T",
                              production=_span(1500))
    fields = {f.get("id") or f.get("key"): f["field_value"]
              for f in ghl.GHLClient.custom_fields(cand)}
    for name in ("next_show_start", "next_show_end"):
        assert fields[config.GHL_FIELD_IDS.get(name, name)] == ""
