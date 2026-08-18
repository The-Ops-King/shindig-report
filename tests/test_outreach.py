"""Outreach guards.

The two that matter most encode the requirements directly, using cases found
in the live data rather than invented ones:

  * five organizations sharing mail@haletheater.org produce exactly ONE send
  * user@domain.com and friends are never sent to
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import emails  # noqa: E402
import outreach  # noqa: E402
from normalize import Production  # noqa: E402
from orgs import Organization  # noqa: E402

TODAY = date(2026, 8, 18)
IN_WINDOW = TODAY + timedelta(days=60)      # comfortably inside 30-120
TOO_SOON = TODAY + timedelta(days=5)
TOO_FAR = TODAY + timedelta(days=300)


def prod(key, org, start, title="Annie", end=None, city="Raleigh", st="NC",
         country="US", source="mti"):
    return Production(
        key=key, source=source, show_title=title, organization=org,
        venue=org, city=city, state=st, country=country, street="1 Main St",
        start_date=start, end_date=end or (start + timedelta(days=3)),
    )


def org_for(p, email):
    o = Organization(key=p.org_key, name=p.organization, city=p.city,
                     state=p.state, country=p.country)
    o.email = email
    o.production_count = 1
    return o


def build(prods, links=None, state=None, today=TODAY):
    registry = {}
    for p, addr in prods:
        registry.setdefault(p.org_key, org_for(p, addr))
    return outreach.build_candidates(
        [p for p, _ in prods], registry, links or {}, state or {}, today
    )


LINKS = {outreach.normalize_title("Annie"): "https://shindig.test/annie"}


# --- one email per person ---------------------------------------------------

def test_five_orgs_sharing_an_address_produce_one_send():
    """mail@haletheater.org really does cover 5 organizations in the live
    data. Deduping by organization would send that person five emails."""
    shared = "mail@haletheater.org"
    prods = [
        (prod(f"mti:{i}", f"Hale Theatre Venue {i}",
              IN_WINDOW + timedelta(days=i), city=f"City{i}"), shared)
        for i in range(5)
    ]
    cands = build(prods, LINKS)
    assert len(cands) == 1
    assert cands[0].address == shared
    assert len(outreach.select(cands)) == 1


def test_shared_address_hears_about_the_soonest_show():
    shared = "mail@haletheater.org"
    late = prod("mti:1", "Hale A", IN_WINDOW + timedelta(days=20), title="Elf")
    soon = prod("mti:2", "Hale B", IN_WINDOW, title="Annie", city="Other")
    cands = build([(late, shared), (soon, shared)], LINKS)
    assert len(cands) == 1
    assert cands[0].production.show_title == "Annie"


def test_distinct_addresses_are_separate_sends():
    a = prod("mti:1", "Theatre A", IN_WINDOW)
    b = prod("mti:2", "Theatre B", IN_WINDOW, city="Durham")
    cands = build([(a, "a@theatre.org"), (b, "b@theatre.org")], LINKS)
    assert len(outreach.select(cands)) == 2


# --- junk addresses ---------------------------------------------------------

@pytest.mark.parametrize("junk", [
    "user@domain.com",                 # on 98 organizations in the live cache
    "example@mysite.com",
    "info@website.com",
    "abc123@group.calendar.google.com",   # a Google Calendar feed id
    "noreply@theatre.org",
    "firstname.lastname@usd305.com",
])
def test_junk_addresses_are_never_contacted(junk):
    assert not emails.is_sendable(junk)
    cands = build([(prod("mti:1", "Some Theatre", IN_WINDOW), junk)], LINKS)
    assert cands == []


def test_real_addresses_survive():
    for good in ("info@burningcoal.org", "boxoffice@realtheatre.com"):
        assert emails.is_sendable(good)


# --- which show -------------------------------------------------------------

def test_licence_windows_never_count_as_the_next_show():
    """A touring producer holding rights 2018-2027 would otherwise mask the
    real next show forever."""
    window = prod("mti:1", "Touring Co", TODAY + timedelta(days=10),
                  end=TODAY + timedelta(days=2000), title="The Music Man")
    real = prod("mti:2", "Touring Co", IN_WINDOW, title="Annie")
    assert window.date_type == "license window"
    assert outreach.true_next_show([window, real], TODAY).show_title == "Annie"


def test_show_too_soon_or_too_far_is_not_emailed():
    for start in (TOO_SOON, TOO_FAR):
        cands = build([(prod("mti:1", "T", start), "a@t.org")], LINKS)
        assert cands[0].action == "update_only"
        assert "window" in cands[0].reason


def test_missing_sample_link_means_no_send():
    cands = build([(prod("mti:1", "T", IN_WINDOW, title="Obscure Show"),
                    "a@t.org")], LINKS)
    assert cands[0].action == "update_only"
    assert cands[0].reason == "no sample link for this show"


def test_annie_jr_does_not_match_annie():
    """Distinct licensed products with different playbills. A visibly wrong
    sample is worse than no email."""
    assert outreach.normalize_title("Annie Jr") != outreach.normalize_title("Annie")
    cands = build([(prod("mti:1", "School", IN_WINDOW, title="Annie Jr"),
                    "a@school.org")], LINKS)
    assert cands[0].action == "update_only"


@pytest.mark.parametrize("a,b", [
    ("Annie", "annie"),
    ("The Little Mermaid", "Little Mermaid"),
    ("Disney's Frozen JR", "Disneys Frozen Jr"),
])
def test_title_normalisation_matches_harmless_variants(a, b):
    assert outreach.normalize_title(a) == outreach.normalize_title(b)


def test_country_not_enabled_is_not_emailed():
    p = prod("mti:1", "Canadian Co", IN_WINDOW, country="CA", st="ON",
             city="Toronto")
    cands = build([(p, "a@t.ca")], LINKS)
    assert cands[0].action == "update_only"
    assert "not enabled" in cands[0].reason


# --- the daily cap ----------------------------------------------------------

def test_daily_cap_is_never_exceeded():
    prods = [(prod(f"mti:{i}", f"Theatre {i}", IN_WINDOW + timedelta(days=i),
                   city=f"City{i}"), f"a{i}@t.org") for i in range(60)]
    cands = build(prods, LINKS)
    assert len(outreach.select(cands, cap=25)) == 25
    assert len(outreach.select(cands, cap=1)) == 1


def test_cap_sends_the_soonest_openings_first():
    prods = [(prod(f"mti:{i}", f"T{i}", IN_WINDOW + timedelta(days=i),
                   city=f"C{i}"), f"a{i}@t.org") for i in range(10)]
    picked = outreach.select(build(prods, LINKS), cap=3)
    assert [c.production.start_date for c in picked] == \
        sorted(c.production.start_date for c in picked)
    assert picked[0].production.start_date == IN_WINDOW


# --- lifecycle: roll forward when a show passes ------------------------------

def test_same_show_is_not_emailed_again():
    p = prod("mti:1", "T", IN_WINDOW)
    state = {"a@t.org": {"current_show_key": "mti:1",
                         "last_sent": "2026-08-01", "sends": 1}}
    cands = build([(p, "a@t.org")], LINKS, state)
    assert cands[0].action == "none"
    assert outreach.select(cands) == []


def test_show_rolls_forward_once_the_old_one_passes():
    """The behaviour asked for: old show done, new show gets a new email."""
    nxt = prod("mti:2", "T", IN_WINDOW, title="Annie")
    state = {"a@t.org": {"current_show_key": "mti:1",
                         "current_show_end": "2026-07-01",
                         "last_sent": "2026-05-01", "sends": 1,
                         "ghl_contact_id": "abc"}}
    cands = build([(nxt, "a@t.org")], LINKS, state)
    assert cands[0].action == "rollover"
    assert cands[0].sample_url.endswith("/annie")
    assert cands[0].sends == 1        # next send will be #2


def test_rollover_respects_the_minimum_gap():
    """24% of consecutive-show gaps are under 30 days; without a floor a busy
    company gets an email every fortnight."""
    nxt = prod("mti:2", "T", IN_WINDOW)
    recent = (TODAY - timedelta(days=10)).isoformat()
    state = {"a@t.org": {"current_show_key": "mti:1", "last_sent": recent,
                         "sends": 1}}
    cands = build([(nxt, "a@t.org")], LINKS, state)
    assert cands[0].action == "update_only"
    assert cands[0].reason == "too soon since last send"


def test_rollover_out_of_window_updates_but_does_not_email():
    """Their next show is real but 300 days out: GHL should say so, and no
    email should go."""
    nxt = prod("mti:2", "T", TOO_FAR)
    state = {"a@t.org": {"current_show_key": "mti:1", "last_sent": "2026-01-01",
                         "sends": 1, "ghl_contact_id": "abc"}}
    cands = build([(nxt, "a@t.org")], LINKS, state)
    assert cands[0].action == "update_only"
    assert not cands[0].sending


def test_record_send_advances_the_counter():
    p = prod("mti:9", "T", IN_WINDOW)
    cands = build([(p, "a@t.org")], LINKS)
    state = {}
    outreach.record_send(state, cands[0], TODAY)
    rec = state["a@t.org"]
    assert rec["current_show_key"] == "mti:9"
    assert rec["sends"] == 1
    assert rec["last_sent"] == TODAY.isoformat()

    # A second run for the same show now finds it current and stays quiet.
    again = build([(p, "a@t.org")], LINKS, state)
    assert again[0].action == "none"
