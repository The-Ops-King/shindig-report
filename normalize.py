"""Canonical production record, address parsing, and identity keys.

The dedupe key is the load-bearing part of this module. If keys are unstable,
every run reports the entire dataset as new and the morning email becomes
noise. MTI and Concord both expose native production IDs; TRW exposes none, so
it gets a content hash over the fields that identify a run.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime

# US states + DC + territories, and Canadian provinces. Used to recognise a
# "City, ST" tail and to infer country when the source does not state one.
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI", "GU", "AS", "MP",
}
CA_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK",
    "YT",
}

# Full names appear in Concord's `state` field; MTI uses abbreviations.
STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL",
    "nova scotia": "NS", "northwest territories": "NT", "nunavut": "NU",
    "ontario": "ON", "prince edward island": "PE", "quebec": "QC",
    "québec": "QC", "saskatchewan": "SK", "yukon": "YT",
}

CA_POSTAL_RE = re.compile(r"^[A-Z]\d[A-Z]\s?\d[A-Z]\d$", re.I)
US_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")


def state_code(value: str) -> str:
    """Normalise a state/province to its two-letter code where possible."""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) == 2 and v.upper() in (US_STATES | CA_PROVINCES):
        return v.upper()
    return STATE_NAME_TO_CODE.get(v.lower(), v)


def country_for_state(code: str) -> str:
    if code in US_STATES:
        return "US"
    if code in CA_PROVINCES:
        return "CA"
    return ""


def normalize_country(value: str) -> str:
    """Map the many spellings a licensor may use onto an ISO-2 code."""
    v = (value or "").strip()
    if not v:
        return ""
    upper = v.upper()
    if upper in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        return "US"
    if upper in {"CA", "CAN", "CANADA"}:
        return "CA"
    if len(v) == 2:
        return upper
    return v


_ORG_NOISE = re.compile(
    r"\b(the|inc|inc\.|incorporated|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|"
    r"corporation|co|company|dba|d\.b\.a\.|d b a|foundation|assn|association|"
    r"society)\b",
    re.I,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_org_name(name: str) -> str:
    """Collapse an organization name to a comparison key.

    This is what makes "Cape Cod Repertory Theatre D.b.a. Cape Rep Theatre"
    and "Cape Cod Repertory Theatre" land on the same organization, which is
    what stops the same company being enriched once per licensor.
    """
    n = (name or "").lower()
    n = n.replace("&", " and ")
    n = _NON_ALNUM.sub(" ", n)
    n = _ORG_NOISE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def org_key(name: str, city: str, state: str) -> str:
    """Identity for an organization: normalised name plus where it sits.

    City is part of the key because chapter-style names ("Community Players")
    repeat across the country and must not be merged into one contact.
    """
    return "|".join(
        [
            normalize_org_name(name),
            _NON_ALNUM.sub(" ", (city or "").lower()).strip(),
            state_code(state).upper(),
        ]
    )


def parse_mti_address(raw: str) -> dict:
    """MTI packs the address into `<br>`-joined segments.

    Observed shapes:
      "224 Polk Street<br>Raleigh, NC<br>27604<br>US"
      "Some Hall<br>London<br>W1<br>GB"
    The last segment is the country; the second-to-last is usually a postal
    code; the "City, ST" segment is found by pattern rather than position,
    because the leading street lines vary in count.
    """
    parts = [p.strip() for p in re.split(r"<br\s*/?>", raw or "") if p.strip()]
    out = {"street": "", "city": "", "state": "", "postal": "", "country": ""}
    if not parts:
        return out

    if len(parts) > 1:
        out["country"] = normalize_country(parts[-1])
        parts = parts[:-1]

    if parts and (US_ZIP_RE.match(parts[-1]) or CA_POSTAL_RE.match(parts[-1])):
        out["postal"] = parts[-1].upper()
        parts = parts[:-1]

    city_idx = None
    for i in range(len(parts) - 1, -1, -1):
        m = re.match(r"^(.*),\s*([A-Za-z][A-Za-z\. ]{1,30})$", parts[i])
        if m:
            code = state_code(m.group(2).replace(".", "").strip())
            if code in (US_STATES | CA_PROVINCES) or len(code) == 2:
                out["city"] = m.group(1).strip()
                out["state"] = code
                city_idx = i
                break
    if city_idx is None and parts:
        # No "City, ST" line at all -- treat the last remaining line as city.
        out["city"] = parts[-1]
        city_idx = len(parts) - 1

    out["street"] = ", ".join(parts[:city_idx]).strip()
    if not out["country"]:
        out["country"] = country_for_state(out["state"])
    return out


def parse_iso(value: str):
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def parse_dmy(value: str):
    """Concord serialises dates as DD/MM/YYYY."""
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None


@dataclass
class Production:
    key: str
    source: str
    show_title: str
    organization: str
    venue: str = ""
    venue_type: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    postal: str = ""
    country: str = ""
    lat: float | None = None
    lng: float | None = None
    start_date: date | None = None
    end_date: date | None = None
    source_url: str = ""
    org_website: str = ""
    first_seen: str = ""
    last_seen: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def address(self) -> str:
        """Single-line address for the Sheet's Address column."""
        bits = [self.street, self.city]
        tail = " ".join(x for x in (self.state, self.postal) if x).strip()
        if tail:
            bits.append(tail)
        if self.country and self.country != "US":
            bits.append(self.country)
        return ", ".join(b for b in bits if b)

    @property
    def org_key(self) -> str:
        return org_key(self.organization, self.city, self.state)

    @property
    def run_days(self) -> int | None:
        if not (self.start_date and self.end_date):
            return None
        return (self.end_date - self.start_date).days

    @property
    def date_type(self) -> str:
        """What the start/end pair actually represents.

        Licensors publish one date pair meaning "the period this licence
        covers", which is not always a performance run. Verified against MTI's
        own data: the median gap is 3 days (a normal weekend school run), but
        ~10% exceed 120 days and the longest is 3,270 -- "Mini Musicals On The
        Move" holds The Music Man from May 2018 to May 2027. That is a rights
        window for a touring producer, not nine years of performances.

        Both kinds are real leads, but they are different leads, so the Sheet
        labels which is which rather than presenting them identically.
        """
        days = self.run_days
        if days is None:
            return "unknown"
        if days < 0:
            return "invalid"
        if days <= 14:
            return "performance run"
        if days <= 120:
            return "extended run"
        return "license window"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start_date"] = self.start_date.isoformat() if self.start_date else ""
        d["end_date"] = self.end_date.isoformat() if self.end_date else ""
        return d


def content_key(source: str, *parts) -> str:
    """Stable hash key for sources that expose no native production ID."""
    joined = "|".join(str(p or "").strip().lower() for p in parts)
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


def in_scope(p: Production, countries: set, today: date, drop_ended: bool) -> bool:
    if p.country not in countries:
        return False
    if drop_ended and p.end_date and p.end_date < today:
        return False
    return True
