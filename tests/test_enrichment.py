"""Guards on the two enrichment rules and on new-today detection.

These encode the requirements directly:
  * a company with many productions is enriched once, not once per show
  * a cursory pass has a hard ceiling and gives up rather than digging
  * a second identical run reports nothing new
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import enrich as enrich_mod  # noqa: E402
import state as state_mod  # noqa: E402
from enrich import Budget, _rank_email, _phones_from  # noqa: E402
from normalize import Production  # noqa: E402
from orgs import Organization, build_registry, normalize_street  # noqa: E402

TODAY = date(2026, 8, 17)


def make_production(key, org, city="Plano", st="TX", show="A Show",
                    website="", street="100 Main St"):
    return Production(
        key=key, source="mti", show_title=show, organization=org,
        city=city, state=st, country="US", street=street,
        org_website=website, start_date=TODAY,
        end_date=TODAY + timedelta(days=7),
    )


# --- Rule 1: one organization, one enrichment ------------------------------

def test_fifteen_productions_collapse_to_one_org():
    """The exact case this was built to avoid: a company with a full season
    must be one enrichment target, not fifteen."""
    prods = [
        make_production(f"mti:{i}", "Plano Children's Theatre",
                        show=f"Show {i}")
        for i in range(15)
    ]
    registry = build_registry(prods)
    assert len(registry) == 1
    assert next(iter(registry.values())).production_count == 15


def test_enrichment_queue_has_one_entry_per_org(monkeypatch):
    prods = [
        make_production(f"mti:{i}", "Plano Children's Theatre",
                        show=f"Show {i}", website="http://example.org")
        for i in range(15)
    ]
    registry = build_registry(prods)

    fetched = []

    def fake(org, session, today):
        fetched.append(org.key)
        return {"email": "info@example.org", "phone": "", "facebook": "",
                "instagram": "", "status": "found",
                "checked": today.isoformat(), "requests": 1}

    monkeypatch.setattr(enrich_mod, "enrich_one", fake)
    stats = enrich_mod.enrich(registry, {}, TODAY)

    assert len(fetched) == 1, f"expected 1 fetch for 15 shows, got {len(fetched)}"
    assert stats["orgs_fetched"] == 1


def test_same_org_across_licensors_enriched_once(monkeypatch):
    """A company appearing in all three catalogues is still one fetch."""
    prods = []
    for i, src in enumerate(("mti", "concord", "trw")):
        p = make_production(f"{src}:{i}", "Old Courthouse Theatre",
                            city="Concord", st="NC")
        p.source = src
        prods.append(p)
    prods[0].org_website = "http://example.org"

    registry = build_registry(prods)
    assert len(registry) == 1

    calls = []
    monkeypatch.setattr(enrich_mod, "enrich_one", lambda o, s, t: (
        calls.append(o.key) or {"email": "a@b.org", "phone": "", "facebook": "",
                                "instagram": "", "status": "found",
                                "checked": t.isoformat(), "requests": 1}
    ))
    enrich_mod.enrich(registry, {}, TODAY)
    assert len(calls) == 1


def test_second_run_fetches_nothing(monkeypatch):
    prods = [make_production("mti:1", "Some Theatre",
                             website="http://example.org")]
    registry = build_registry(prods)
    cache = {}

    calls = []
    monkeypatch.setattr(enrich_mod, "enrich_one", lambda o, s, t: (
        calls.append(o.key) or {"email": "a@b.org", "phone": "", "facebook": "",
                                "instagram": "", "status": "found",
                                "checked": t.isoformat(), "requests": 1}
    ))

    enrich_mod.enrich(registry, cache, TODAY)
    assert len(calls) == 1

    registry2 = build_registry(prods)
    stats = enrich_mod.enrich(registry2, cache, TODAY)
    assert len(calls) == 1, "cached org was re-fetched"
    assert stats["orgs_fetched"] == 0
    assert stats["cache_hits"] == 1


def test_org_without_website_is_cached_not_retried(monkeypatch):
    registry = build_registry([make_production("mti:1", "No Site Players")])
    cache = {}
    monkeypatch.setattr(enrich_mod, "enrich_one",
                        lambda o, s, t: pytest.fail("should not fetch"))
    stats = enrich_mod.enrich(registry, cache, TODAY)
    assert stats["orgs_fetched"] == 0
    assert list(cache.values())[0]["status"] == "no_website"


# --- Rule 2: cursory pass, hard ceiling ------------------------------------

def test_budget_is_a_hard_ceiling():
    b = Budget(config.ENRICH_MAX_REQUESTS_PER_ORG)
    spent = sum(1 for _ in range(20) if b.spend())
    assert spent == config.ENRICH_MAX_REQUESTS_PER_ORG == 3


def test_misses_are_cached_for_a_quarter():
    """A dead end must cost 3 requests per quarter, not 3 per day."""
    entry = {"status": "not_found", "checked": TODAY.isoformat()}
    assert state_mod.is_fresh(entry, TODAY + timedelta(days=30))
    assert state_mod.is_fresh(entry, TODAY + timedelta(days=89))
    assert not state_mod.is_fresh(entry, TODAY + timedelta(days=91))


def test_transient_errors_retry_sooner_than_misses():
    err = {"status": "error", "checked": TODAY.isoformat()}
    assert not state_mod.is_fresh(err, TODAY + timedelta(days=8))
    miss = {"status": "not_found", "checked": TODAY.isoformat()}
    assert state_mod.is_fresh(miss, TODAY + timedelta(days=8))


def test_role_mailboxes_outrank_personal():
    ranked = sorted(
        ["bob.smith@theatre.org", "info@theatre.org", "boxoffice@theatre.org"],
        key=_rank_email,
    )
    assert ranked[0] in ("info@theatre.org", "boxoffice@theatre.org")
    assert ranked[-1] == "bob.smith@theatre.org"


@pytest.mark.parametrize("text,expect", [
    ("Call (919) 834-4001 today", "(919) 834-4001"),
    ("919-834-4001", "(919) 834-4001"),
    ("919.834.4001", "(919) 834-4001"),
])
def test_phone_extraction(text, expect):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(f"<p>{text}</p>", "lxml")
    assert _phones_from(soup, str(soup))[0] == expect


@pytest.mark.parametrize("junk", [
    "1786795128",       # unix timestamp -- the bug this regex was written for
    "cachebust=1234567890",
    "111-222-3333",     # NANP forbids a leading 1 in an area code
])
def test_phone_extraction_rejects_junk(junk):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(f"<p>{junk}</p>", "lxml")
    assert _phones_from(soup, str(soup)) == []


# --- cross-source website fill ---------------------------------------------

def test_address_match_shares_website_across_licensors():
    """Concord names the venue where MTI names the company; the shared street
    address is what links them."""
    a = make_production("mti:1", "Burning Coal Theatre Company",
                        city="Raleigh", st="NC", street="224 Polk Street",
                        website="http://burningcoal.org")
    b = make_production("concord:1", "Murphey School Auditorium",
                        city="Raleigh", st="NC", street="224 Polk St.")
    b.source = "concord"
    registry = build_registry([a, b])
    assert len(registry) == 2
    sites = {o.name: o.website for o in registry.values()}
    assert sites["Murphey School Auditorium"] == "http://burningcoal.org"


# --- the Contacts tab as the authoritative cache ---------------------------

def test_org_already_in_contacts_is_never_refetched(monkeypatch):
    """If the contact already exists, use it -- no HTTP at all."""
    registry = build_registry(
        [make_production("mti:1", "Some Theatre", website="http://example.org")]
    )
    key = next(iter(registry))
    cache = {key: {"email": "boxoffice@some.org", "phone": "", "facebook": "",
                   "instagram": "", "status": "found", "checked": "2020-01-01",
                   "manual": False}}

    monkeypatch.setattr(enrich_mod, "enrich_one",
                        lambda o, s, t: pytest.fail("re-fetched a cached org"))
    stats = enrich_mod.enrich(registry, cache, TODAY)

    assert stats["orgs_fetched"] == 0
    assert registry[key].email == "boxoffice@some.org"


def test_found_contacts_do_not_expire():
    """A hit years old is still reused; an org is visited once in its life."""
    entry = {"status": "found", "checked": "2019-01-01", "manual": False}
    assert state_mod.is_fresh(entry, TODAY)
    assert state_mod.is_fresh(entry, TODAY + timedelta(days=3650))


def test_manual_row_is_never_refetched_or_overwritten(monkeypatch):
    """Someone fixing an email by hand must have that stick, permanently."""
    registry = build_registry(
        [make_production("mti:1", "Some Theatre", website="http://example.org")]
    )
    key = next(iter(registry))
    cache = {key: {"email": "artistic.director@some.org", "phone": "",
                   "facebook": "", "instagram": "", "status": "found",
                   "checked": "2019-01-01", "manual": True,
                   "website": "http://hand-typed.org"}}

    monkeypatch.setattr(enrich_mod, "enrich_one",
                        lambda o, s, t: pytest.fail("re-fetched a manual row"))
    stats = enrich_mod.enrich(registry, cache, TODAY)

    assert stats["manual_hits"] == 1
    assert registry[key].email == "artistic.director@some.org"
    assert cache[key]["email"] == "artistic.director@some.org"


def test_manual_row_survives_force_enrich(monkeypatch):
    """--force-enrich re-checks automatic rows, but must not clobber manual ones."""
    registry = build_registry(
        [make_production("mti:1", "Some Theatre", website="http://example.org")]
    )
    key = next(iter(registry))
    cache = {key: {"email": "hand@typed.org", "phone": "", "facebook": "",
                   "instagram": "", "status": "found", "checked": "2019-01-01",
                   "manual": True}}

    monkeypatch.setattr(enrich_mod, "enrich_one",
                        lambda o, s, t: pytest.fail("clobbered a manual row"))
    enrich_mod.enrich(registry, cache, TODAY, force=True)
    assert cache[key]["email"] == "hand@typed.org"


def test_only_brand_new_orgs_are_fetched(monkeypatch):
    """The steady-state case: one new company among many known ones."""
    prods = [make_production(f"mti:{i}", f"Theatre {i}",
                             website="http://example.org") for i in range(5)]
    registry = build_registry(prods)
    known = list(registry)[:4]
    cache = {k: {"email": "a@b.org", "phone": "", "facebook": "",
                 "instagram": "", "status": "found", "checked": "2024-01-01",
                 "manual": False} for k in known}

    fetched = []
    monkeypatch.setattr(enrich_mod, "enrich_one", lambda o, s, t: (
        fetched.append(o.key) or {"email": "new@org.org", "phone": "",
                                  "facebook": "", "instagram": "",
                                  "status": "found",
                                  "checked": t.isoformat(), "requests": 1}
    ))
    stats = enrich_mod.enrich(registry, cache, TODAY)

    assert len(fetched) == 1
    assert stats["cache_hits"] == 4
    assert len(cache) == 5, "the new org was not added to the cache"


def test_contact_round_trips_through_sheet_rows():
    """A cache entry must survive being written to the Contacts tab and read
    back, or manual edits would silently reset on the next run."""
    import sheets
    entry = {"email": "info@x.org", "phone": "(919) 834-4001",
             "facebook": "https://facebook.com/x", "instagram": "",
             "status": "found", "checked": "2026-08-17", "manual": True,
             "name": "X Theatre", "city": "Raleigh", "state": "NC",
             "website": "http://x.org"}
    row = sheets.contact_row("x|raleigh|NC", entry)
    header = sheets.CONTACT_HEADERS
    rebuilt = dict(zip(header, row))

    assert rebuilt["Org Key"] == "x|raleigh|NC"
    assert rebuilt["Email"] == "info@x.org"
    assert rebuilt["Source"] == "manual"
    assert rebuilt["Organization"] == "X Theatre"
    assert len(row) == len(header)


# --- malformed licensor data must not take down a run ----------------------

@pytest.mark.parametrize("bad", [
    "http://starlights-youth-theatre..org",  # empty label -- aborted a real run
    "http://.org", "http://-bad-.com", "http://localhost",
    "n/a", "tbd", "", "   ",
])
def test_malformed_websites_are_rejected(bad):
    from orgs import _clean_url
    assert _clean_url(bad) == ""


@pytest.mark.parametrize("good,want", [
    ("www.burningcoal.org", "http://www.burningcoal.org"),
    ("http://theatre.org/contact", "http://theatre.org/contact"),
    ("https://x.co", "https://x.co"),
])
def test_valid_websites_survive_cleaning(good, want):
    from orgs import _clean_url
    assert _clean_url(good) == want


def test_org_with_malformed_url_is_treated_as_having_none():
    p = make_production("mti:1", "Starlights Youth Theatre",
                        website="http://starlights-youth-theatre..org")
    org = next(iter(build_registry([p]).values()))
    assert org.website == ""


def test_one_broken_site_does_not_abort_enrichment(monkeypatch):
    """A single third-party failure must cost one organization, not the run."""
    import enrich as e

    class Boom:
        def get(self, *a, **k):
            raise Exception("LocationParseError from deep inside urllib3")

    org = Organization(key="k", name="Broken", website="http://example.org")
    result = e.enrich_one(org, Boom(), TODAY)
    assert result["status"] == "error"
    assert result["email"] == ""


def test_street_normalization_matches_abbreviations():
    assert normalize_street("224 Polk Street") == normalize_street("224 Polk St.")
    assert normalize_street("100 N Main Ave") == normalize_street("100 North Main Avenue")


# --- new-today detection ---------------------------------------------------

def test_second_identical_run_reports_nothing_new():
    """The single most important check in the system."""
    prods = [make_production(f"mti:{i}", f"Theatre {i}") for i in range(50)]
    seen = {}
    first = state_mod.diff_new(prods, seen, TODAY)
    assert len(first) == 50

    again = [make_production(f"mti:{i}", f"Theatre {i}") for i in range(50)]
    second = state_mod.diff_new(again, seen, TODAY + timedelta(days=1))
    assert second == [], f"{len(second)} productions wrongly reported as new"


def test_genuinely_new_production_is_detected():
    seen = {}
    state_mod.diff_new([make_production("mti:1", "A")], seen, TODAY)
    fresh = state_mod.diff_new(
        [make_production("mti:1", "A"), make_production("mti:2", "B")],
        seen, TODAY,
    )
    assert [p.key for p in fresh] == ["mti:2"]


def test_first_seen_is_preserved_across_runs():
    seen = {}
    state_mod.diff_new([make_production("mti:1", "A")], seen, TODAY)
    later = make_production("mti:1", "A")
    state_mod.diff_new([later], seen, TODAY + timedelta(days=5))
    assert later.first_seen == TODAY.isoformat()
    assert later.last_seen == (TODAY + timedelta(days=5)).isoformat()
