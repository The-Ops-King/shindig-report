"""End-to-end behaviour of the Contacts tab as the enrichment cache.

Simulates the Google Sheet with a fake worksheet so the read -> enrich ->
write -> read cycle can be tested without network or credentials. What matters
here is that a run never re-fetches an organization already listed, and that a
hand-edited row survives being written back and read again.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import enrich as enrich_mod  # noqa: E402
import sheets  # noqa: E402
from normalize import Production  # noqa: E402
from orgs import build_registry  # noqa: E402

TODAY = date(2026, 8, 17)


class FakeWorksheet:
    def __init__(self, values=None):
        self.values = values or []
        self.row_count, self.col_count = 1000, 30

    def get_all_values(self):
        return [list(r) for r in self.values]

    def clear(self):
        self.values = []

    def update(self, block, rng, value_input_option=None):
        start = int(rng.lstrip("A")) - 1
        while len(self.values) < start:
            self.values.append([])
        self.values[start:start + len(block)] = [list(r) for r in block]

    def freeze(self, rows=0):
        pass

    def add_rows(self, n):
        self.row_count += n

    def add_cols(self, n):
        self.col_count += n


class FakeBook:
    def __init__(self):
        self.tabs = {}

    def worksheet(self, title):
        if title not in self.tabs:
            import gspread
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet()
        self.tabs[title] = ws
        return ws


def production(key, org, city="Raleigh", st="NC", website=""):
    return Production(
        key=key, source="mti", show_title="A Show", organization=org,
        city=city, state=st, country="US", street="1 Main St",
        org_website=website, start_date=TODAY,
        end_date=TODAY + timedelta(days=7),
    )


def stub_enrich(found_email="found@auto.org"):
    calls = []

    def _fake(org, session, today):
        calls.append(org.key)
        return {"email": found_email, "phone": "", "facebook": "",
                "instagram": "", "status": "found",
                "checked": today.isoformat(), "requests": 1}

    return _fake, calls


def test_empty_sheet_starts_with_empty_cache():
    assert sheets.read_contacts(FakeBook()) == {}


def test_day_one_writes_contacts_day_two_reads_and_skips(monkeypatch):
    """The whole point: day two must do no HTTP for a known organization."""
    book = FakeBook()

    # --- day one: nothing cached, org gets enriched and written out
    registry = build_registry(
        [production("mti:1", "Burning Coal Theatre",
                    website="http://burningcoal.org")]
    )
    fake, calls = stub_enrich()
    monkeypatch.setattr(enrich_mod, "enrich_one", fake)

    cache = sheets.read_contacts(book)
    enrich_mod.enrich(registry, cache, TODAY)
    assert len(calls) == 1

    sheets.write_contacts(book, cache)
    assert config.TAB_CONTACTS in book.tabs

    # --- day two: same org, cache read back from the sheet
    reread = sheets.read_contacts(book)
    assert len(reread) == 1

    registry2 = build_registry(
        [production("mti:2", "Burning Coal Theatre",
                    website="http://burningcoal.org")]
    )
    monkeypatch.setattr(
        enrich_mod, "enrich_one",
        lambda o, s, t: pytest.fail("re-fetched an org already in Contacts"),
    )
    stats = enrich_mod.enrich(registry2, reread, TODAY + timedelta(days=1))

    assert stats["orgs_fetched"] == 0
    assert stats["cache_hits"] == 1
    assert next(iter(registry2.values())).email == "found@auto.org"


def test_hand_edited_row_survives_a_full_cycle(monkeypatch):
    """Type a better address into the sheet; it must stick and never be
    overwritten by a later automatic pass."""
    book = FakeBook()
    registry = build_registry(
        [production("mti:1", "Old Courthouse Theatre",
                    website="http://oldcourthouse.org")]
    )
    key = next(iter(registry))

    fake, _ = stub_enrich("generic@auto.org")
    monkeypatch.setattr(enrich_mod, "enrich_one", fake)
    cache = sheets.read_contacts(book)
    enrich_mod.enrich(registry, cache, TODAY)
    sheets.write_contacts(book, cache)

    # Someone edits the Email cell and sets Source to "manual".
    ws = book.tabs[config.TAB_CONTACTS]
    header = ws.values[0]
    row = ws.values[1]
    row[header.index("Email")] = "artistic.director@oldcourthouse.org"
    row[header.index("Source")] = "manual"

    reread = sheets.read_contacts(book)
    assert reread[key]["manual"] is True

    registry2 = build_registry(
        [production("mti:2", "Old Courthouse Theatre",
                    website="http://oldcourthouse.org")]
    )
    monkeypatch.setattr(
        enrich_mod, "enrich_one",
        lambda o, s, t: pytest.fail("clobbered a manual row"),
    )
    enrich_mod.enrich(registry2, reread, TODAY + timedelta(days=400))

    sheets.write_contacts(book, reread)
    final = sheets.read_contacts(book)
    assert final[key]["email"] == "artistic.director@oldcourthouse.org"
    assert final[key]["manual"] is True


def test_new_org_is_appended_to_existing_contacts(monkeypatch):
    """A newly discovered company joins the tab without disturbing the rest."""
    book = FakeBook()
    first = build_registry(
        [production("mti:1", "Theatre One", website="http://one.org")]
    )
    fake, _ = stub_enrich("one@org.org")
    monkeypatch.setattr(enrich_mod, "enrich_one", fake)
    cache = sheets.read_contacts(book)
    enrich_mod.enrich(first, cache, TODAY)
    sheets.write_contacts(book, cache)

    both = build_registry([
        production("mti:1", "Theatre One", website="http://one.org"),
        production("mti:2", "Theatre Two", city="Durham",
                   website="http://two.org"),
    ])
    fake2, calls2 = stub_enrich("two@org.org")
    monkeypatch.setattr(enrich_mod, "enrich_one", fake2)

    cache2 = sheets.read_contacts(book)
    stats = enrich_mod.enrich(both, cache2, TODAY + timedelta(days=1))
    sheets.write_contacts(book, cache2)

    assert len(calls2) == 1, "only the brand-new organization should be fetched"
    assert stats["cache_hits"] == 1
    final = sheets.read_contacts(book)
    assert len(final) == 2
    assert {e["email"] for e in final.values()} == {"one@org.org", "two@org.org"}


def test_unrecognised_headers_are_ignored_rather_than_misread():
    book = FakeBook()
    ws = FakeWorksheet([["Something", "Else"], ["a", "b"]])
    book.tabs[config.TAB_CONTACTS] = ws
    assert sheets.read_contacts(book) == {}
