"""Organization registry.

Two jobs, both central to the design:

1. **Collapse productions to organizations.** Enrichment runs against this set,
   never against the production list, so a company staging 15 shows next season
   is fetched exactly once. Rule 1 of the plan.

2. **Cross-source website fill.** MTI publishes a venue_url for roughly 38% of
   its records; Concord and TRW publish none at all. The same community
   theatres license from all three houses, so an MTI-sourced URL back-fills the
   Concord and TRW rows for the same organization. This is the only reason
   those two sources get any contact data.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from normalize import Production, org_key

log = logging.getLogger(__name__)

_STREET_NOISE = re.compile(
    r"\b(street|st|avenue|ave|road|rd|drive|dr|boulevard|blvd|lane|ln|"
    r"court|ct|place|pl|highway|hwy|route|rte|suite|ste|unit|apt|"
    r"north|south|east|west|n|s|e|w|ne|nw|se|sw)\b",
    re.I,
)


def normalize_street(street: str) -> str:
    """Collapse a street address for comparison across licensors.

    MTI writes "224 Polk Street" where Concord writes "224 Polk St."; stripping
    thoroughfare words and punctuation makes those the same venue.
    """
    s = _STREET_NOISE.sub(" ", (street or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


@dataclass
class Organization:
    key: str
    name: str
    city: str = ""
    state: str = ""
    country: str = ""
    website: str = ""
    website_source: str = ""
    street: str = ""
    sources: set = field(default_factory=set)
    production_count: int = 0

    # Filled by enrich.py
    email: str = ""
    phone: str = ""
    facebook: str = ""
    instagram: str = ""
    contact_status: str = ""
    contact_checked: str = ""


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I
)


def _clean_url(url: str) -> str:
    """Normalise a licensor-supplied website, rejecting unusable ones.

    Licensor data carries genuinely malformed hostnames -- an empty label in
    "starlights-youth-theatre..org" raised a parse error deep inside urllib3
    and aborted a whole run. Validating the host here means such rows are
    simply treated as having no website.
    """
    u = (url or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    try:
        host = urlparse(u).hostname or ""
    except ValueError:
        return ""
    return u if _HOSTNAME_RE.match(host) else ""


def build_registry(productions: list[Production]) -> dict[str, Organization]:
    """Collapse productions into one Organization per (name, city, state).

    Longest name wins as the display name -- "Cape Cod Repertory Theatre
    D.b.a. Cape Rep Theatre" is more useful for a human than "Cape Rep".
    """
    registry: dict[str, Organization] = {}

    for p in productions:
        k = p.org_key
        org = registry.get(k)
        if org is None:
            org = Organization(
                key=k, name=p.organization, city=p.city,
                state=p.state, country=p.country,
            )
            registry[k] = org

        org.production_count += 1
        org.sources.add(p.source)

        if len(p.organization) > len(org.name):
            org.name = p.organization
        if not org.street and p.street:
            org.street = p.street
        if not org.country and p.country:
            org.country = p.country

        url = _clean_url(p.org_website)
        if url and not org.website:
            org.website = url
            org.website_source = p.source

    _fill_by_address(registry)
    return registry


def _fill_by_address(registry: dict[str, Organization]) -> int:
    """Share websites between organizations sitting at the same address.

    Name matching alone links only ~410 Concord organizations to an MTI
    website, because Concord names the venue ("The Ashby Stage") where MTI
    names the producing company. Matching on street address as well takes that
    to ~980 -- these are the same building, so the same contact.
    """
    index: dict[tuple, str] = {}
    for org in registry.values():
        if not (org.website and org.street):
            continue
        key = (normalize_street(org.street), org.city.lower().strip())
        if key[0]:
            index.setdefault(key, org.website)

    filled = 0
    for org in registry.values():
        if org.website or not org.street:
            continue
        key = (normalize_street(org.street), org.city.lower().strip())
        hit = index.get(key) if key[0] else None
        if hit:
            org.website = hit
            org.website_source = "address-match"
            filled += 1

    if filled:
        log.info("filled %d org websites by address match", filled)
    return filled


def apply_registry(productions: list[Production],
                   registry: dict[str, Organization]) -> None:
    """Push each organization's website and contact info back onto its rows.

    After this, a Concord or TRW production carries the website MTI supplied
    for the same company.
    """
    for p in productions:
        org = registry.get(p.org_key)
        if org and org.website and not p.org_website:
            p.org_website = org.website


def coverage(registry: dict[str, Organization]) -> dict:
    total = len(registry)
    with_site = sum(1 for o in registry.values() if o.website)
    cross = sum(
        1 for o in registry.values()
        if o.website and o.sources - {"mti"}
        and o.website_source in ("mti", "address-match")
    )
    return {
        "orgs": total,
        "with_website": with_site,
        "cross_source_filled": cross,
        "pct_with_website": round(100 * with_site / total, 1) if total else 0.0,
    }
