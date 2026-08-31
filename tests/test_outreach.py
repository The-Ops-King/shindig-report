"""Outreach guards.

The two that matter most encode the requirements directly, using cases found
in the live data rather than invented ones:

  * five organizations sharing mail@haletheater.org produce exactly ONE send
  * user@domain.com and friends are never sent to
"""

import json
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


@pytest.fixture(autouse=True)
def _isolate_from_live_state(monkeypatch, tmp_path):
    """Keep the suite off the real state/ files.

    Learned the hard way: info@burningcoal.org was used here as an example of
    a good address, then genuinely appeared in state/suppressed.json after a
    live purge, and a passing test started failing for a reason that had
    nothing to do with the code. Tests must not read production state.

    Verification is stubbed true for the same reason most tests are not about
    it; the ones that are override this explicitly.
    """
    empty = tmp_path / "suppressed.json"
    empty.write_text("[]")
    monkeypatch.setattr(config, "SUPPRESSED_PATH", empty)
    monkeypatch.setattr(emails, "_suppressed", None)
    monkeypatch.setattr(emails, "is_verified", lambda addr: True)


# --- one email per person ---------------------------------------------------

def test_five_orgs_sharing_an_address_produce_one_send():
    """mail@haletheater.org really does cover 5 organizations in the live data.

    Five organizations, so five pipeline cards -- but one inbox, so exactly one
    email. Those are deliberately different units.
    """
    shared = "mail@haletheater.org"
    prods = [
        (prod(f"mti:{i}", f"Hale Theatre Venue {i}",
              IN_WINDOW + timedelta(days=i), city=f"City{i}"), shared)
        for i in range(5)
    ]
    cands = build(prods, LINKS)
    assert len(cands) == 5, "each organization is still tracked"
    assert len(outreach.select(cands)) == 1, "but only one email goes out"
    held = [c for c in cands if not c.sending]
    assert all("shares this inbox" in c.reason for c in held)


def test_shared_address_hears_about_the_soonest_show():
    shared = "mail@haletheater.org"
    late = prod("mti:1", "Hale A", IN_WINDOW + timedelta(days=20), title="Elf")
    soon = prod("mti:2", "Hale B", IN_WINDOW, title="Annie", city="Other")
    cands = build([(late, shared), (soon, shared)], LINKS)
    sending = outreach.select(cands)
    assert len(sending) == 1
    assert sending[0].production.show_title == "Annie"


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


# --- GHL: contact, tags, pipeline -------------------------------------------

class FakeGHL:
    """Records calls instead of making them."""

    def __init__(self, **kw):
        import ghl
        self.client = ghl.GHLClient(
            api_key="k", location_id="loc", workflow_id="wf",
            pipeline_id=kw.get("pipeline_id", "pipe"),
            stage_id=kw.get("stage_id", "stage"),
        )
        self.calls = []
        self.client._request = self._request

    def _request(self, method, path, payload=None, retries=3):
        self.calls.append((method, path, payload))
        if path == "/contacts/upsert":
            return {"contact": {"id": "contact-1"}}
        if path == "/opportunities/":
            return {"opportunity": {"id": "opp-1"}}
        return {}

    def paths(self):
        return [f"{m} {p}" for m, p, _ in self.calls]

    def payload_for(self, path):
        for _, p, body in self.calls:
            if p == path:
                return body
        return None


def a_candidate(**kw):
    p = prod("mti:1", "Old Courthouse Theatre", IN_WINDOW, title="Annie")
    cands = build([(p, "info@oct.org")], LINKS)
    c = cands[0]
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_contact_is_tagged_with_source_and_licensor():
    fake = FakeGHL()
    fake.client.push(a_candidate())
    tags = fake.payload_for("/contacts/upsert")["tags"]
    assert config.GHL_SOURCE_TAG in tags
    assert "MTI" in tags


@pytest.mark.parametrize("source,tag", [
    ("mti", "MTI"), ("concord", "Concord"), ("trw", "TRW"),
])
def test_licensor_tag_per_source(source, tag):
    p = prod("x:1", "T", IN_WINDOW, source=source)
    cand = build([(p, "a@t.org")], LINKS)[0]
    fake = FakeGHL()
    fake.client.push(cand)
    assert tag in fake.payload_for("/contacts/upsert")["tags"]


def sent_fields(fake):
    """Field values back under their logical names.

    The wire payload references a field by GHL id wherever we have one, so the
    tests read through the same mapping rather than restating it.
    """
    by_ref = {f.get("id") or f.get("key"): f["field_value"]
              for f in fake.payload_for("/contacts/upsert")["customFields"]}
    return {name: by_ref[config.GHL_FIELD_IDS.get(name, name)]
            for name in config.GHL_FIELDS}


def test_contact_carries_next_show_and_date():
    """'What's the next play' and 'when's the next play' on the contact."""
    fake = FakeGHL()
    fake.client.push(a_candidate())
    fields = sent_fields(fake)
    assert fields["next_show_title"] == "Annie"
    assert fields["next_show_start"] == IN_WINDOW.isoformat()
    assert fields["sample_playbill_url"].endswith("/annie")


def test_every_wanted_field_is_sent_exactly_once():
    """Whatever the mix of adopted ids and our own names, all seven go and
    none goes twice -- a duplicate reference is how one silently wins."""
    fake = FakeGHL()
    fake.client.push(a_candidate())
    refs = [f.get("id") or f.get("key")
            for f in fake.payload_for("/contacts/upsert")["customFields"]]
    assert len(refs) == len(config.GHL_FIELDS) == len(set(refs))
    for name in config.GHL_FIELDS:
        assert config.GHL_FIELD_IDS.get(name, name) in refs


def test_sending_creates_an_opportunity_in_the_pipeline():
    fake = FakeGHL()
    cand = a_candidate()
    fake.client.push(cand)
    assert "POST /opportunities/" in fake.paths()
    opp = fake.payload_for("/opportunities/")
    assert opp["pipelineId"] == "pipe"
    assert opp["pipelineStageId"] == "stage"
    assert opp["contactId"] == "contact-1"
    assert opp["status"] == "open"
    assert "Annie" in opp["name"]
    assert cand.ghl_opportunity_id == "opp-1"


def test_rollover_updates_the_existing_card_instead_of_making_another():
    """A company with a full season must not leave a trail of dead cards."""
    fake = FakeGHL()
    cand = a_candidate(ghl_opportunity_id="opp-existing")
    fake.client.push(cand)
    assert "PUT /opportunities/opp-existing" in fake.paths()
    assert "POST /opportunities/" not in fake.paths()


def test_pipeline_is_optional():
    """No pipeline ids configured: contact and workflow still work."""
    fake = FakeGHL(pipeline_id="", stage_id="")
    fake.client.push(a_candidate())
    assert not any("/opportunities" in p for p in fake.paths())
    assert "POST /contacts/upsert" in fake.paths()


def test_enrolment_removes_before_it_adds():
    """GHL silently refuses to re-enrol a contact still active in a workflow,
    and this workflow keeps people active until their show date."""
    fake = FakeGHL()
    fake.client.push(a_candidate())
    paths = fake.paths()
    assert paths.index("DELETE /contacts/contact-1/workflow/wf") < \
        paths.index("POST /contacts/contact-1/workflow/wf")


def test_update_only_org_is_carded_but_never_enrolled():
    """Their show changed but is out of window: the pipeline card and the
    contact fields stay current, and no email goes."""
    cand = a_candidate()
    cand.action, cand.reason = "update_only", "outside window (too far out)"
    fake = FakeGHL()
    fake.client.push(cand)
    assert "POST /contacts/upsert" in fake.paths()
    assert "POST /opportunities/" in fake.paths()
    assert not any("workflow" in p for p in fake.paths())


def test_one_failure_does_not_raise():
    import ghl
    fake = FakeGHL()

    def boom(*a, **k):
        raise ghl.GHLError("upstream exploded")

    fake.client._request = boom
    ok, err = fake.client.push(a_candidate())
    assert ok is False and "exploded" in err


# --- one opportunity per organization ---------------------------------------

def test_every_organization_gets_a_card_even_when_not_emailed():
    """Five venues on one inbox: five pipeline cards, one email."""
    shared = "mail@haletheater.org"
    prods = [
        (prod(f"mti:{i}", f"Hale Venue {i}", IN_WINDOW + timedelta(days=i),
              city=f"City{i}"), shared)
        for i in range(3)
    ]
    cands = build(prods, LINKS)
    fakes = [FakeGHL() for _ in cands]
    for fake, cand in zip(fakes, cands):
        fake.client.push(cand)
    assert all("POST /opportunities/" in f.paths() for f in fakes)
    enrolled = [f for f in fakes if any("workflow" in p for p in f.paths())]
    assert len(enrolled) == 1, "only the sending candidate is enrolled"


def test_opportunity_id_is_keyed_on_the_organization():
    cands = build([
        (prod("mti:1", "Theatre A", IN_WINDOW), "shared@x.org"),
        (prod("mti:2", "Theatre B", IN_WINDOW + timedelta(days=5),
              city="Durham"), "shared@x.org"),
    ], LINKS)
    a, b = sorted(cands, key=lambda c: c.org_name)
    opportunities = {}
    a.ghl_contact_id, b.ghl_contact_id = "c-a", "c-b"
    a.ghl_opportunity_id, b.ghl_opportunity_id = "opp-a", "opp-b"
    outreach.record_opportunity(opportunities, a, TODAY)
    outreach.record_opportunity(opportunities, b, TODAY)
    assert len(opportunities) == 2
    assert opportunities[a.org_key]["opportunity_id"] == "opp-a"
    assert opportunities[b.org_key]["opportunity_id"] == "opp-b"

    # A later run reattaches them rather than creating duplicates.
    again = build([
        (prod("mti:1", "Theatre A", IN_WINDOW), "shared@x.org"),
        (prod("mti:2", "Theatre B", IN_WINDOW + timedelta(days=5),
              city="Durham"), "shared@x.org"),
    ], LINKS)
    outreach.load_opportunity_ids(again, opportunities)
    assert {c.ghl_opportunity_id for c in again} == {"opp-a", "opp-b"}


def test_one_card_per_org_across_seasons_not_one_per_show():
    """Rolling forward refreshes the existing card."""
    opportunities = {}
    first = build([(prod("mti:1", "T", IN_WINDOW, title="Annie"), "a@t.org")],
                  LINKS)[0]
    first.ghl_contact_id, first.ghl_opportunity_id = "contact-1", "opp-1"
    outreach.record_opportunity(opportunities, first, TODAY)

    later = build([(prod("mti:2", "T", IN_WINDOW, title="Elf"), "a@t.org")],
                  LINKS)[0]
    outreach.load_opportunity_ids([later], opportunities)
    assert later.ghl_opportunity_id == "opp-1"

    fake = FakeGHL()
    fake.client.push(later)
    assert "PUT /opportunities/opp-1" in fake.paths()
    assert "POST /opportunities/" not in fake.paths()


# --- tags drive the sequence -------------------------------------------------

def test_enrolment_takes_the_tag_off_before_putting_it_back():
    """A GHL tag trigger fires on a tag being *newly* added. Re-adding a tag
    the contact already carries is not an error, it is simply nothing -- so a
    rollover that skipped the removal would never send."""
    fake = FakeGHL()
    fake.client.push(a_candidate())
    tag_calls = [(m, body.get("tags")) for m, path, body in fake.calls
                 if path == "/contacts/contact-1/tags"
                 and config.GHL_OUTREACH_TAG in (body.get("tags") or [])]
    assert tag_calls == [("DELETE", [config.GHL_OUTREACH_TAG]),
                         ("POST", [config.GHL_OUTREACH_TAG])]


def test_upsert_does_not_carry_the_outreach_tag():
    """Upsert merges tags, so a tag set here would already be present on the
    next run and the trigger would never fire again. enrol() owns it."""
    fake = FakeGHL()
    fake.client.push(a_candidate())
    assert config.GHL_OUTREACH_TAG not in fake.payload_for("/contacts/upsert")["tags"]


def test_a_mapped_field_is_addressed_by_id_not_by_name(monkeypatch):
    """Adopting the field you already have called "What's the next play you
    are doing". A fieldKey that matches nothing is accepted and dropped, so
    the id is the only reference that fails loudly when it is wrong."""
    monkeypatch.setattr(config, "GHL_FIELD_IDS",
                        {"next_show_title": "XW5c99K5MZaogICyK9kd"})
    import ghl
    fields = {f.get("id") or f.get("key"): f["field_value"]
              for f in ghl.GHLClient.custom_fields(a_candidate())}
    assert fields["XW5c99K5MZaogICyK9kd"] == "Annie"
    assert "next_show_title" not in fields
    # Unmapped fields still fall back to the name.
    assert "licensor" in fields


# --- clearing when the show is over ------------------------------------------

def _state_for(addr, show_key, sends=1, sent=None):
    return {addr: {"ghl_contact_id": "contact-1", "org_key": "t|raleigh|nc",
                   "org_name": "T", "current_show_key": show_key,
                   "sends": sends,
                   "last_sent": (sent or (TODAY - timedelta(days=200))).isoformat()}}


def test_org_whose_show_has_passed_is_cleared_once():
    """Their show has been and gone and nothing is booked. Without this they
    stay in the sequence forever, because the organization simply stops
    appearing in the candidate list."""
    state = _state_for("a@t.org", "mti:9")
    cands = build([], state=state)
    clears = [c for c in cands if c.action == "clear"]
    assert len(clears) == 1
    assert clears[0].address == "a@t.org"
    assert clears[0].production is None

    outreach.record_clear(state, clears[0], TODAY)
    assert state["a@t.org"]["current_show_key"] == ""
    assert state["a@t.org"]["cleared"] == TODAY.isoformat()
    # Second run: nothing left to clear.
    assert not [c for c in build([], state=state) if c.action == "clear"]


def test_an_org_we_never_emailed_is_not_cleared():
    state = {"a@t.org": {"ghl_contact_id": "c", "current_show_key": ""}}
    assert not [c for c in build([], state=state) if c.action == "clear"]


def test_a_next_show_rolls_forward_rather_than_clearing():
    p = prod("mti:10", "T", IN_WINDOW)
    state = _state_for("a@t.org", "mti:9")
    cands = build([(p, "a@t.org")], LINKS, state)
    assert [c.action for c in cands] == ["rollover"]


def test_shared_inbox_is_not_cleared_while_one_org_still_has_a_show():
    """Two organizations share the inbox: one has gone quiet, the other opens
    in sixty days. The person must not be pulled out of the sequence."""
    live = prod("mti:11", "Live Co", IN_WINDOW)
    state = _state_for("shared@t.org", "mti:9")
    cands = build([(live, "shared@t.org")], LINKS, state)
    assert not [c for c in cands if c.action == "clear"]


def test_clearing_keeps_the_gap_floor():
    """Clearing must not become a way to email someone twice in a fortnight."""
    state = _state_for("a@t.org", "mti:9", sent=TODAY - timedelta(days=5))
    clears = [c for c in build([], state=state) if c.action == "clear"]
    outreach.record_clear(state, clears[0], TODAY)
    p = prod("mti:12", "T", IN_WINDOW)
    cand = build([(p, "a@t.org")], LINKS, state)[0]
    assert cand.action == "update_only"
    assert cand.reason == "too soon since last send"


def test_clear_stops_the_sequence_and_leaves_the_card_alone():
    cand = a_candidate()
    cand.action, cand.production, cand.ghl_contact_id = "clear", None, "contact-1"
    fake = FakeGHL()
    assert fake.client.push(cand)[0]
    paths = fake.paths()
    assert "DELETE /contacts/contact-1/workflow/wf" in paths
    assert ("DELETE", "/contacts/contact-1/tags",
            {"tags": [config.GHL_OUTREACH_TAG]}) in fake.calls
    assert not any("/opportunities" in p for p in paths)
    # Every show field is blanked, so a hand re-add cannot render a past show.
    fields = fake.payload_for("/contacts/upsert")["customFields"]
    assert all(f["field_value"] == "" for f in fields)


def test_a_clear_without_a_stored_name_does_not_blank_it():
    """Clears are built from the outreach record. An early record may carry no
    org name, and sending an empty one would wipe what is in GHL."""
    cand = a_candidate()
    cand.action, cand.production, cand.org_name = "clear", None, ""
    fake = FakeGHL()
    fake.client.push(cand)
    payload = fake.payload_for("/contacts/upsert")
    assert "name" not in payload and "companyName" not in payload


# --- ingest: write the CRM, send nothing -------------------------------------

def test_ingest_holds_instead_of_sending():
    """Everything that would have gone out is written to GHL and withheld."""
    p = prod("mti:1", "T", IN_WINDOW)
    cands = build([(p, "a@t.org")], LINKS)
    assert cands[0].action == "send"

    held = outreach.build_candidates(
        [p], {p.org_key: org_for(p, "a@t.org")}, LINKS, {}, TODAY, hold=True)
    assert held[0].action == "hold"
    assert not held[0].sending
    assert held[0].ready, "a hold is still what we would send today"


def test_ingest_never_records_a_send():
    """The trap: stamping a send here would make _decide answer 'already
    current' forever, and these people could never be emailed at all."""
    p = prod("mti:1", "T", IN_WINDOW)
    registry = {p.org_key: org_for(p, "a@t.org")}
    state = {}
    outreach.build_candidates([p], registry, LINKS, state, TODAY, hold=True)
    assert state == {}, "ingest leaves the outreach ledger untouched"

    # And once the workflow is live, they are still a first-time send.
    assert outreach.build_candidates(
        [p], registry, LINKS, state, TODAY)[0].action == "send"


def test_a_held_candidate_is_carded_but_never_enrolled():
    cand = a_candidate()
    cand.action, cand.reason = "hold", "workflow not live yet"
    fake = FakeGHL()
    assert fake.client.push(cand)[0]
    paths = fake.paths()
    assert "POST /contacts/upsert" in paths
    assert "POST /opportunities/" in paths
    assert not any("workflow" in p for p in paths)
    tags = [t for _, path, body in fake.calls if path.endswith("/tags")
            for t in (body.get("tags") or [])]
    assert config.GHL_OUTREACH_TAG not in tags
    assert config.GHL_READY_TAG in tags


def test_ready_tag_is_only_written_when_it_changes():
    """Upsert merges tags and never removes one, so the tag has to be set
    explicitly -- but writing it every run would cost a call per contact."""
    cand = a_candidate()
    cand.action, cand.was_ready = "hold", True
    cand.was_unverified = False          # not what this test is about
    fake = FakeGHL()
    fake.client.push(cand)
    assert not any(p.endswith("/tags") for p in fake.paths())

    # And it comes back off once they are no longer sendable.
    cand.action, cand.reason = "update_only", "outside window (too far out)"
    fake = FakeGHL()
    fake.client.push(cand)
    assert ("DELETE", "/contacts/contact-1/tags",
            {"tags": [config.GHL_READY_TAG]}) in fake.calls


def test_unchanged_orgs_are_not_written_again():
    """The opportunities file doubles as the ingest ledger, so the bootstrap
    happens once rather than every morning."""
    p = prod("mti:1", "T", IN_WINDOW)
    registry = {p.org_key: org_for(p, "a@t.org")}
    cand = outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY, hold=True)[0]
    opportunities = {}
    assert outreach.needs_ingest(cand, opportunities)

    cand.ghl_contact_id, cand.ghl_opportunity_id = "contact-1", "opp-1"
    outreach.record_opportunity(opportunities, cand, TODAY)
    assert not outreach.needs_ingest(cand, opportunities)

    # A new show is a change. So is falling out of the ready set.
    later = prod("mti:2", "T", IN_WINDOW + timedelta(days=90))
    fresh = outreach.build_candidates(
        [later], registry, LINKS, {}, TODAY, hold=True)[0]
    assert outreach.needs_ingest(fresh, opportunities)


def test_ingest_needs_no_workflow_id():
    """The whole point: the CRM fills up before the workflow exists."""
    import ghl
    client = ghl.GHLClient(api_key="k", location_id="loc", workflow_id="")
    assert client.configured()[0]
    enrol_ok, missing = client.can_enrol()
    assert not enrol_ok and missing == "GHL_WORKFLOW_ID"


def test_a_contact_written_without_a_card_is_still_remembered():
    """The live failure: with no pipeline ids configured GHL creates no
    opportunity, and bailing on the empty card id left the ledger empty after
    a successful ingest -- so the same organizations were re-pushed every
    morning forever."""
    p = prod("mti:1", "T", IN_WINDOW)
    registry = {p.org_key: org_for(p, "a@t.org")}
    cand = outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY, hold=True)[0]
    cand.ghl_contact_id, cand.ghl_opportunity_id = "contact-1", ""

    opportunities = {}
    outreach.record_opportunity(opportunities, cand, TODAY)
    assert opportunities[cand.org_key]["contact_id"] == "contact-1"
    assert not outreach.needs_ingest(cand, opportunities)

    # ...and adding the pipeline secrets later re-writes it exactly once,
    # so the card it never got gets created.
    assert outreach.needs_ingest(cand, opportunities, want_card=True)
    cand.ghl_opportunity_id = "opp-1"
    outreach.record_opportunity(opportunities, cand, TODAY)
    assert not outreach.needs_ingest(cand, opportunities, want_card=True)


# --- companies that are mid-run ----------------------------------------------

def _mid_run(org="Still Running Rep", email="a@t.org"):
    """A show that opened last week and is still on: no upcoming start date."""
    p = prod("mti:1", org, TODAY - timedelta(days=7),
             end=TODAY + timedelta(days=7))
    return p, {p.org_key: org_for(p, email)}


def test_a_mid_run_company_is_ingested_but_not_a_normal_candidate():
    """true_next_show wants start_date >= today, so a company whose show is on
    right now has no upcoming start and vanished from the run entirely -- 150
    of them in the live data."""
    p, registry = _mid_run()
    assert outreach.true_next_show([p], TODAY) is None
    assert outreach.build_candidates([p], registry, LINKS, {}, TODAY) == []

    cands = outreach.build_candidates([p], registry, LINKS, {}, TODAY, hold=True)
    assert len(cands) == 1
    assert cands[0].production is None
    assert cands[0].action == "update_only"
    assert cands[0].reason == "no upcoming show"
    assert not cands[0].ready, "nothing to pitch yet, so not safe to bulk-tag"


def test_a_mid_run_company_still_has_no_email_no_entry():
    """The one hard rule: nothing without a usable email reaches GHL."""
    p, _ = _mid_run()
    registry = {p.org_key: org_for(p, "user@domain.com")}
    assert outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY, hold=True) == []


def test_no_production_takes_its_address_and_licensor_from_the_registry():
    p, registry = _mid_run()
    org = registry[p.org_key]
    org.street, org.sources = "12 Playhouse Row", {"mti", "trw"}
    cand = outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY, hold=True)[0]

    fake = FakeGHL()
    assert fake.client.push(cand)[0]
    payload = fake.payload_for("/contacts/upsert")
    assert payload["address1"] == "12 Playhouse Row"
    assert payload["city"] == "Raleigh"
    assert set(payload["tags"]) == {config.GHL_SOURCE_TAG, "MTI", "TRW"}
    assert all(f["field_value"] == "" for f in payload["customFields"])


def test_the_card_is_named_for_the_company_when_there_is_no_show():
    p, registry = _mid_run(org="Still Running Rep")
    cand = outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY, hold=True)[0]
    import ghl
    assert ghl.GHLClient.opportunity_name(cand) == "Still Running Rep"


def test_a_shared_inbox_with_no_upcoming_show_does_not_break_the_winner_pass():
    """The winners pass reads production.start_date, so a candidate carrying
    none has to sit outside it rather than crash the run."""
    running, registry = _mid_run(org="Running Co", email="shared@t.org")
    upcoming = prod("mti:2", "Upcoming Co", IN_WINDOW, city="Durham")
    registry[upcoming.org_key] = org_for(upcoming, "shared@t.org")
    cands = outreach.build_candidates(
        [running, upcoming], registry, LINKS, {}, TODAY, hold=True)
    assert len(cands) == 2
    assert sorted(c.action for c in cands) == ["hold", "update_only"]


# --- the brake ---------------------------------------------------------------

class Args:
    """The subset of the CLI namespace run_outreach reads."""
    def __init__(self, **kw):
        self.outreach_dry_run = kw.get("dry", False)
        self.outreach_ingest = kw.get("ingest", False)
        self.outreach_limit = kw.get("limit", None)


def _run(monkeypatch, enabled, **kw):
    """run_outreach against a fake GHL, returning (stats, calls)."""
    import main, ghl, sheets
    fake = FakeGHL()
    monkeypatch.setattr(config, "OUTREACH_ENABLED", enabled)
    # run_outreach imports ghl and sheets inside the function, so patching the
    # modules themselves is what it actually picks up.
    monkeypatch.setattr(ghl, "GHLClient", lambda *a, **k: fake.client)
    monkeypatch.setattr(sheets, "read_show_links", lambda book: LINKS)
    for name in ("load_outreach", "load_opportunities"):
        monkeypatch.setattr(main.state_mod, name, lambda: {})
    for name in ("save_outreach", "save_opportunities"):
        monkeypatch.setattr(main.state_mod, name, lambda *a: None)

    p = prod("mti:1", "Old Courthouse Theatre", IN_WINDOW)
    registry = {p.org_key: org_for(p, "info@oct.org")}
    stats, _ = main.run_outreach([p], registry, object(), TODAY, Args(**kw))
    return stats, fake


def test_a_live_run_emails_nobody_while_the_switch_is_off(monkeypatch):
    """One word in a JSON file should not be able to cold-email 2,700 people."""
    stats, fake = _run(monkeypatch, enabled=False)
    assert stats["mode"] == "ingest"
    assert "refused" in stats
    assert stats.get("sent", 0) == 0
    assert not any("workflow" in path for path in fake.paths())
    tags = [t for _, path, body in fake.calls if path.endswith("/tags")
            for t in ((body or {}).get("tags") or [])]
    assert config.GHL_OUTREACH_TAG not in tags


def test_the_refusal_still_writes_the_contact(monkeypatch):
    """Refusing to send must not become refusing to do the rest of the job."""
    stats, fake = _run(monkeypatch, enabled=False)
    assert "POST /contacts/upsert" in fake.paths()
    assert stats["ingested"] == 1


def test_with_the_switch_on_a_live_run_enrols(monkeypatch):
    stats, fake = _run(monkeypatch, enabled=True)
    assert stats["mode"] == "outreach"
    assert "refused" not in stats
    assert "POST /contacts/contact-1/workflow/wf" in fake.paths()


def test_ingest_needs_no_switch(monkeypatch):
    """Ingest cannot enrol by construction, so it is not gated."""
    stats, fake = _run(monkeypatch, enabled=False, ingest=True)
    assert "refused" not in stats
    assert stats["ingested"] == 1
    assert not any("workflow" in path for path in fake.paths())


# --- rolling forward to the next show ----------------------------------------

def _rolled(tagged, ready=True, last_armed=None, show="mti:2"):
    """A company whose show just changed, with a ledger recording the old one."""
    title = "Annie" if ready else "Obscure Show"
    p = prod(show, "Old Courthouse Theatre", IN_WINDOW, title=title)
    registry = {p.org_key: org_for(p, "info@oct.org")}
    cand = outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY, hold=True)[0]
    record = {"opportunity_id": "opp-1", "contact_id": "contact-1",
              "org_name": cand.org_name, "show_key": "mti:1", "ready": True}
    if last_armed:
        record["last_armed"] = last_armed.isoformat()
    opportunities = {cand.org_key: record}
    outreach.needs_ingest(cand, opportunities)      # sets show_changed
    cand.has_outreach_tag = tagged
    return cand, opportunities


def _push(cand, opportunities, contact_tags=()):
    fake = FakeGHL()
    upsert = {"contact": {"id": "contact-1", "tags": list(contact_tags)}}
    inner = fake._request

    def request(method, path, payload=None, retries=3):
        inner(method, path, payload, retries)
        return upsert if path == "/contacts/upsert" else (
            {"opportunity": {"id": "opp-1"}} if path == "/opportunities/" else {})

    fake.client._request = request
    ok, _ = fake.client.push(
        cand, rearm_check=lambda c: outreach.needs_rearm(c, opportunities, TODAY))
    assert ok
    return fake


def test_the_show_rolling_over_is_detected():
    """Little Mermaid closes, Shrek is next: the ledger sees the change."""
    cand, opportunities = _rolled(tagged=True)
    assert cand.show_changed
    assert outreach.needs_ingest(cand, opportunities)


def test_a_rollover_does_not_re_arm_someone_never_contacted():
    """The whole safety property: re-arming an untagged contact would be a
    FIRST contact, which belongs behind OUTREACH_ENABLED."""
    cand, opportunities = _rolled(tagged=False)
    assert not outreach.needs_rearm(cand, opportunities, TODAY)
    fake = _push(cand, opportunities, contact_tags=[config.GHL_SOURCE_TAG])
    assert "POST /contacts/upsert" in fake.paths()      # fields still updated
    assert not any("workflow" in p for p in fake.paths())
    assert not cand.rearmed


def test_a_rollover_re_arms_someone_already_in_the_sequence():
    cand, opportunities = _rolled(tagged=True)
    assert outreach.needs_rearm(cand, opportunities, TODAY)
    fake = _push(cand, opportunities,
                 contact_tags=[config.GHL_SOURCE_TAG, config.GHL_OUTREACH_TAG])
    assert cand.rearmed
    paths = fake.paths()
    # Same two no-ops as a first enrolment: the tag must come off before it
    # goes back on, and the workflow membership with it.
    assert paths.index("DELETE /contacts/contact-1/workflow/wf") < \
        paths.index("POST /contacts/contact-1/workflow/wf")
    tag_calls = [(m, (b or {}).get("tags")) for m, path, b in fake.calls
                 if path.endswith("/tags")
                 and config.GHL_OUTREACH_TAG in ((b or {}).get("tags") or [])]
    assert tag_calls == [("DELETE", [config.GHL_OUTREACH_TAG]),
                         ("POST", [config.GHL_OUTREACH_TAG])]


def test_the_tag_is_read_off_the_contact_not_assumed():
    """has_outreach_tag comes from what GHL actually holds, so a contact you
    untagged by hand stops being re-armed."""
    cand, opportunities = _rolled(tagged=True)
    _push(cand, opportunities, contact_tags=[config.GHL_SOURCE_TAG])
    assert not cand.has_outreach_tag
    assert not outreach.needs_rearm(cand, opportunities, TODAY)


def test_a_packed_season_does_not_re_enter_every_few_weeks():
    """24% of consecutive-show gaps are under 30 days."""
    cand, opportunities = _rolled(tagged=True,
                                  last_armed=TODAY - timedelta(days=20))
    assert not outreach.needs_rearm(cand, opportunities, TODAY)

    cand, opportunities = _rolled(tagged=True,
                                  last_armed=TODAY - timedelta(days=60))
    assert outreach.needs_rearm(cand, opportunities, TODAY)


def test_rolling_into_a_show_with_no_sample_link_does_not_re_arm():
    """The pitch is "here is what YOUR playbill could look like"."""
    cand, opportunities = _rolled(tagged=True, ready=False)
    assert not cand.ready
    assert not outreach.needs_rearm(cand, opportunities, TODAY)


def test_re_arming_stamps_the_floor_and_the_same_show_does_not_repeat():
    cand, opportunities = _rolled(tagged=True)
    cand.rearmed, cand.ghl_contact_id = True, "contact-1"
    outreach.record_opportunity(opportunities, cand, TODAY)
    assert opportunities[cand.org_key]["last_armed"] == TODAY.isoformat()
    # Same show again tomorrow: nothing changed, so nothing is written.
    cand.rearmed = False
    assert not outreach.needs_ingest(cand, opportunities)
    assert not outreach.needs_rearm(cand, opportunities, TODAY)


def test_the_floor_survives_a_rollover_rewriting_the_record():
    """last_armed must not be lost when the record is rewritten for a new show,
    or the 45-day floor resets every time a company changes shows."""
    cand, opportunities = _rolled(tagged=True,
                                  last_armed=TODAY - timedelta(days=10))
    cand.rearmed, cand.ghl_contact_id = False, "contact-1"
    outreach.record_opportunity(opportunities, cand, TODAY)
    assert opportunities[cand.org_key]["last_armed"] == \
        (TODAY - timedelta(days=10)).isoformat()


def test_the_switch_turns_it_off(monkeypatch):
    cand, opportunities = _rolled(tagged=True)
    monkeypatch.setattr(config, "REARM_ON_ROLLOVER", False)
    assert not outreach.needs_rearm(cand, opportunities, TODAY)


# --- suppression: deleted contacts must not come back ------------------------

@pytest.fixture
def suppress(monkeypatch, tmp_path):
    """Point the suppression list at a temp file and clear the module cache."""
    def _set(addresses):
        path = tmp_path / "suppressed.json"
        path.write_text(json.dumps(list(addresses)))
        monkeypatch.setattr(config, "SUPPRESSED_PATH", path)
        monkeypatch.setattr(emails, "_suppressed", None)
    return _set


def test_a_suppressed_address_is_never_a_candidate(suppress):
    """Deleting a contact is not enough: the daily ingest re-creates anything
    missing from the ledger, so without this the next morning puts it back."""
    suppress(["a@t.org"])
    assert not emails.is_sendable("a@t.org")
    assert emails.rejection_reason("a@t.org") == "suppressed"
    p = prod("mti:1", "T", IN_WINDOW)
    registry = {p.org_key: org_for(p, "a@t.org")}
    assert outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY, hold=True) == []


def test_suppressing_one_address_leaves_the_others_alone(suppress):
    suppress(["a@t.org"])
    a = prod("mti:1", "Gone Co", IN_WINDOW)
    b = prod("mti:2", "Live Co", IN_WINDOW, city="Durham")
    registry = {a.org_key: org_for(a, "a@t.org"),
                b.org_key: org_for(b, "b@t.org")}
    cands = outreach.build_candidates(
        [a, b], registry, LINKS, {}, TODAY, hold=True)
    assert [c.address for c in cands] == ["b@t.org"]


@pytest.mark.parametrize("addr", [
    "%20centrestageinc@yahoo.com",      # a mailto with a leading space
    "05%7c01%7cmdecorre@cbsd.org",      # a fragment of an Outlook safelink
    "tcsme%64ia@tcswv.org",
])
def test_url_encoded_addresses_are_rejected(addr):
    """A percent-escape that survived into an address means it was lifted from
    a URL and never decoded. It cannot deliver, and decoding it back would be
    inventing a recipient."""
    assert emails.rejection_reason(addr) == "url_encoded"


def test_a_normal_address_with_a_percent_is_not_caught():
    assert emails.is_sendable("box%office@theatre.org")


@pytest.mark.parametrize("addr", [
    "co*************@**************my.org",   # masked on the page it came from
    "info@theatre.org",                       # not masked -- sanity anchor
])
def test_masked_addresses_are_rejected(addr):
    """A page that hides its address behind asterisks has not given us one.
    Sending it for verification would spend a credit to be told what the
    asterisks already said."""
    expected = "masked" if "*" in addr else ""
    assert emails.rejection_reason(addr) == expected


def test_a_bulleted_mask_is_caught_too():
    assert emails.rejection_reason("in\u2022\u2022@theatre.org") == "masked"


def test_mailto_prefix_is_stripped_rather_than_rejected():
    """"mailto:x@y" unambiguously names x@y, so the address is recovered.
    Before this, the prefix rode through _ADDR into the verification queue."""
    assert emails.normalize("mailto:hebinger@commack.k12.ny.us") == \
        "hebinger@commack.k12.ny.us"
    assert emails.is_sendable("MailTo:Hebinger@commack.k12.ny.us")


def test_the_prefixed_and_bare_forms_dedupe_onto_one_address():
    """normalize() is the dedupe key, so stripping has to happen there --
    otherwise one inbox is verified twice and becomes two contacts."""
    assert emails.normalize("mailto:a@t.org") == emails.normalize("a@t.org")


def test_a_suppressed_address_stays_suppressed_when_it_arrives_prefixed(
        suppress):
    suppress(["a@t.org"])
    assert emails.rejection_reason("mailto:a@t.org") == "suppressed"


# --- the purge: deleting by contact, not by organization ----------------------

def test_purge_retires_every_org_behind_a_shared_inbox(monkeypatch):
    """2,958 organizations sit on 2,368 inboxes. Deleting a contact must retire
    all of its organizations -- a stale ledger row would make the next run
    believe the contact still exists."""
    import purge_ghl, state as state_mod
    cache = {"a": {"email": "shared@t.org"}, "b": {"email": "shared@t.org"},
             "c": {"email": "keep@t.org"}}
    ledger = {"a": {"contact_id": "c-1"}, "b": {"contact_id": "c-1"},
              "c": {"contact_id": "c-2"}}
    monkeypatch.setattr(state_mod, "load_org_cache", lambda: cache)
    monkeypatch.setattr(state_mod, "load_opportunities", lambda: ledger)

    doomed, kept = purge_ghl.plan({"keep@t.org"})
    assert list(doomed) == ["c-1"], "one contact, not two organizations"
    assert sorted(doomed["c-1"]["orgs"]) == ["a", "b"]
    assert list(kept) == ["c-2"]


def test_purge_keeps_an_address_on_the_verified_list(monkeypatch):
    import purge_ghl, state as state_mod
    monkeypatch.setattr(state_mod, "load_org_cache",
                        lambda: {"a": {"email": "Info@T.org"}})
    monkeypatch.setattr(state_mod, "load_opportunities",
                        lambda: {"a": {"contact_id": "c-1"}})
    # Case differs between the export and the cache; both normalize the same.
    doomed, kept = purge_ghl.plan({"info@t.org"})
    assert not doomed and list(kept) == ["c-1"]


def test_purge_reads_the_real_verified_csv():
    import purge_ghl
    keep = purge_ghl.verified_addresses("data/verified_emails.csv")
    assert len(keep) == 1598
    assert all("@" in a and a == a.strip().lower() for a in keep)


# --- unverified addresses: kept and tagged, never emailed --------------------

@pytest.fixture
def verdicts(monkeypatch):
    """Declare which addresses have a deliverability verdict."""
    def _set(mapping):
        monkeypatch.setattr(emails, "_verified", {
            a: {"status": s} for a, s in mapping.items()})
        monkeypatch.setattr(emails, "is_verified",
                            lambda addr: mapping.get(addr) == "deliverable")
    return _set


def test_an_unverified_address_is_still_ingested(verdicts):
    """Unverified is not bad. Deleting on absence of evidence is what cost 770
    contacts; refusing to send on it is free."""
    verdicts({})
    p = prod("mti:1", "T", IN_WINDOW)
    registry = {p.org_key: org_for(p, "new@t.org")}
    cands = outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY, hold=True)
    assert len(cands) == 1, "still goes into GHL with its show"
    assert cands[0].action == "update_only"
    assert cands[0].reason == "email not verified"


def test_an_unverified_address_is_never_ready_to_send(verdicts):
    """shindig-ready is the set you can safely bulk-tag, so it must not
    contain anyone whose address has never been checked."""
    verdicts({})
    p = prod("mti:1", "T", IN_WINDOW)
    registry = {p.org_key: org_for(p, "new@t.org")}
    cand = outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY, hold=True)[0]
    assert not cand.ready


def test_a_verified_address_sends_normally(verdicts):
    verdicts({"good@t.org": "deliverable"})
    p = prod("mti:1", "T", IN_WINDOW)
    registry = {p.org_key: org_for(p, "good@t.org")}
    assert outreach.build_candidates(
        [p], registry, LINKS, {}, TODAY)[0].action == "send"


def test_an_unverified_contact_carries_the_tag(verdicts):
    verdicts({})
    cand = a_candidate()
    fake = FakeGHL()
    fake.client.push(cand)
    tags = fake.payload_for("/contacts/upsert")["tags"]
    assert config.GHL_UNVERIFIED_TAG in tags
    assert config.GHL_SOURCE_TAG in tags


def test_the_tag_comes_off_once_a_verdict_arrives(verdicts):
    """Upsert adds tags but can never remove one, so clearing it needs an
    explicit call or the contact stays flagged forever after being checked."""
    verdicts({"info@oct.org": "deliverable"})
    cand = a_candidate()
    cand.was_unverified = True
    fake = FakeGHL()
    fake.client.push(cand)
    assert config.GHL_UNVERIFIED_TAG not in \
        fake.payload_for("/contacts/upsert")["tags"]
    assert ("DELETE", "/contacts/contact-1/tags",
            {"tags": [config.GHL_UNVERIFIED_TAG]}) in fake.calls


def test_the_tag_removal_is_not_repeated_every_run(verdicts):
    verdicts({"info@oct.org": "deliverable"})
    cand = a_candidate()
    cand.was_unverified = False          # already cleared on an earlier run
    fake = FakeGHL()
    fake.client.push(cand)
    assert not any(config.GHL_UNVERIFIED_TAG in ((b or {}).get("tags") or [])
                   for _, _, b in fake.calls)


def test_the_seeded_cache_matches_the_verified_export():
    """The 1,598 that already passed are seeded, so connecting the API later
    does not pay to re-check them."""
    import json
    seeded = json.load(open("state/verified.json"))
    assert len(seeded) == 1598
    assert all(v["status"] == "deliverable" for v in seeded.values())


# --- verify before ingest ----------------------------------------------------

class FakeVerifalia:
    """Records what was submitted and answers with a fixed verdict map."""
    def __init__(self, answers=None, boom=False):
        self.answers, self.boom, self.submitted = answers or {}, boom, []

    def configured(self):
        return True

    def verify(self, addresses):
        if self.boom:
            raise RuntimeError("verifalia is down")
        self.submitted.append(list(addresses))
        return {a: self.answers[a] for a in addresses if a in self.answers}


def _gate(monkeypatch, tmp_path, cands, answers=None, in_ghl=(), boom=False):
    """Run main.gate_on_verification with GHL and Verifalia stubbed."""
    import main, verifalia
    fake_v = FakeVerifalia(answers, boom)
    monkeypatch.setattr(verifalia, "Verifalia", lambda *a, **k: fake_v)
    monkeypatch.setattr(config, "VERIFIED_PATH", tmp_path / "verified.json")
    monkeypatch.setattr(config, "SUPPRESSED_PATH", tmp_path / "suppressed.json")
    monkeypatch.setattr(emails, "_verified", {})
    monkeypatch.setattr(emails, "_suppressed", set())
    monkeypatch.setattr(emails, "is_verified",
                        lambda a: emails._verified.get(a, {})
                        .get("status", "") == "deliverable")

    class FakeClient:
        def find_by_email(self, address):
            return "existing-1" if address in in_ghl else ""
    allowed, stats = main.gate_on_verification(cands, FakeClient(), TODAY)
    return allowed, stats, fake_v


def _cand(addr, org="T"):
    p = prod(f"mti:{abs(hash(addr)) % 999}", org, IN_WINDOW)
    return outreach.Candidate(address=addr, org_key=p.org_key,
                              org_name=org, production=p, action="hold")


def test_only_deliverable_addresses_reach_ghl(monkeypatch, tmp_path):
    """The whole point of the reordering: an address that bounces never
    becomes a contact, rather than becoming one and being deleted later."""
    cands = [_cand("good@t.org"), _cand("bad@t.org"), _cand("risky@t.org")]
    allowed, stats, _ = _gate(monkeypatch, tmp_path, cands, answers={
        "good@t.org": "Deliverable",
        "bad@t.org": "Undeliverable",
        "risky@t.org": "Risky",
    })
    assert [c.address for c in allowed] == ["good@t.org"]
    assert stats["held_back"] == 2


def test_undeliverable_is_suppressed_but_risky_is_not(monkeypatch, tmp_path):
    """Suppressing Risky throws away reachable leads; creating it risks the
    domain. It stays cached and undecided."""
    cands = [_cand("bad@t.org"), _cand("risky@t.org")]
    _gate(monkeypatch, tmp_path, cands, answers={
        "bad@t.org": "Undeliverable", "risky@t.org": "Risky"})
    assert "bad@t.org" in emails._suppressed
    assert "risky@t.org" not in emails._suppressed
    assert emails._verified["risky@t.org"]["status"] == "risky"


def test_addresses_are_deduped_before_being_paid_for(monkeypatch, tmp_path):
    """Organizations share inboxes, so verifying per organization would pay
    several times for one address."""
    cands = [_cand("shared@t.org", "A"), _cand("shared@t.org", "B"),
             _cand("shared@t.org", "C")]
    _, stats, fake = _gate(monkeypatch, tmp_path, cands,
                           answers={"shared@t.org": "Deliverable"})
    assert fake.submitted == [["shared@t.org"]]
    assert stats["sent_to_verifalia"] == 1


def test_an_address_already_in_ghl_is_adopted_not_verified(monkeypatch, tmp_path):
    """The ledger only knows contacts we made; intake forms made others.
    Paying to verify one that is already a contact buys nothing."""
    cands = [_cand("existing@t.org")]
    allowed, stats, fake = _gate(monkeypatch, tmp_path, cands,
                                 in_ghl={"existing@t.org"})
    assert fake.submitted == [], "never sent for verification"
    assert stats["already_in_ghl"] == 1
    assert [c.address for c in allowed] == ["existing@t.org"]


def test_a_verifalia_outage_creates_nobody(monkeypatch, tmp_path):
    """Fails closed. A broken integration must under-create, never flood the
    CRM with addresses that bounce."""
    cands = [_cand("a@t.org"), _cand("b@t.org")]
    allowed, stats, _ = _gate(monkeypatch, tmp_path, cands, boom=True)
    assert allowed == []
    assert stats["held_back"] == 2


def test_an_already_verified_address_is_not_resubmitted(monkeypatch, tmp_path):
    """state/verified.json is permanent: an address is paid for once."""
    import main, verifalia
    fake_v = FakeVerifalia({})
    monkeypatch.setattr(verifalia, "Verifalia", lambda *a, **k: fake_v)
    monkeypatch.setattr(emails, "_verified",
                        {"known@t.org": {"status": "deliverable"}})
    monkeypatch.setattr(emails, "is_verified",
                        lambda a: a == "known@t.org")

    class FakeClient:
        def find_by_email(self, address):
            return ""
    allowed, stats = main.gate_on_verification(
        [_cand("known@t.org")], FakeClient(), TODAY)
    assert fake_v.submitted == []
    assert stats["sent_to_verifalia"] == 0
    assert len(allowed) == 1
