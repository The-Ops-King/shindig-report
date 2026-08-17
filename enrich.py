"""Cursory contact enrichment, once per organization.

Rule 1: this module only ever accepts a set of Organizations. It has no access
to the production list and structurally cannot fetch a company once per show.

Rule 2: a hard budget per organization, enforced by a counter rather than by
convention:
  * at most 3 HTTP requests, ever, per refresh window
  * 8s timeout, zero retries
  * stop the instant an email is found
  * homepage plus at most two contact pages that the homepage actually links to
  * no headless browser, no JS rendering, no search fallback, no paid API
A miss is cached for 90 days, so a dead end costs 3 requests per quarter.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config
import state
from orgs import Organization

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
# A separator is mandatory. Matching a bare run of ten digits pulls in cache
# busters, unix timestamps and asset hashes -- observed returning "1786795128"
# as a theatre's phone number. Real numbers on real pages are punctuated.
# NANP also forbids 0 or 1 as the leading digit of an area code or exchange.
PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?"
    r"(?:\((?P<area_p>[2-9]\d{2})\)|(?P<area>[2-9]\d{2}))"
    r"[\s.\-]"
    r"(?P<exchange>[2-9]\d{2})"
    r"[\s.\-]"
    r"(?P<line>\d{4})"
    r"(?!\d)"
)
CONTACT_HINT = re.compile(r"contact|about|staff|connect|info", re.I)


class Budget:
    """Per-organization request counter. The cap is not advisory."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def spend(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _valid_email(addr: str) -> bool:
    low = addr.lower()
    if any(bad in low for bad in config.EMAIL_BLOCKLIST_SUBSTRINGS):
        return False
    if len(low) > 100:
        return False
    return True


def _rank_email(addr: str) -> int:
    """Lower sorts first. Role mailboxes beat personal ones for cold outreach."""
    mailbox = addr.split("@", 1)[0].lower()
    for i, pref in enumerate(config.PREFERRED_MAILBOXES):
        if mailbox == pref or mailbox.startswith(pref):
            return i
    return len(config.PREFERRED_MAILBOXES)


def _emails_from(soup: BeautifulSoup, html: str) -> list[str]:
    found = []
    for a in soup.select('a[href^="mailto:"]'):
        addr = a["href"][7:].split("?", 1)[0].strip()
        if addr and _valid_email(addr):
            found.append(addr)
    for m in EMAIL_RE.finditer(html):
        if _valid_email(m.group(0)):
            found.append(m.group(0))
    # Preserve discovery order within each rank.
    seen, ordered = set(), []
    for e in found:
        low = e.lower()
        if low not in seen:
            seen.add(low)
            ordered.append(e)
    return sorted(ordered, key=_rank_email)


def _format_nanp(digits: str) -> str:
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:]}"


def _phones_from(soup: BeautifulSoup, html: str) -> list[str]:
    """`tel:` links first -- they are declared, not inferred."""
    found = []
    for a in soup.select('a[href^="tel:"]'):
        digits = re.sub(r"\D", "", a["href"][4:])
        pretty = _format_nanp(digits)
        if pretty:
            found.append(pretty)

    # Strip tags before scanning text, so attribute values and inline scripts
    # cannot contribute digits.
    text = soup.get_text(" ", strip=True) if soup else html
    for m in PHONE_RE.finditer(text):
        area = m.group("area_p") or m.group("area")
        pretty = _format_nanp(area + m.group("exchange") + m.group("line"))
        if pretty:
            found.append(pretty)
    return list(dict.fromkeys(found))


def _jsonld(soup: BeautifulSoup) -> dict:
    """Organization schema, when a site publishes one, is the cleanest source."""
    out = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_nodes(data):
            if not isinstance(node, dict):
                continue
            if node.get("email") and "email" not in out:
                val = node["email"]
                val = val[0] if isinstance(val, list) and val else val
                if isinstance(val, str):
                    out["email"] = val.replace("mailto:", "").strip()
            if node.get("telephone") and "phone" not in out:
                val = node["telephone"]
                val = val[0] if isinstance(val, list) and val else val
                if isinstance(val, str):
                    out["phone"] = val.strip()
    return out


def _iter_nodes(data):
    if isinstance(data, list):
        for item in data:
            yield from _iter_nodes(item)
    elif isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _iter_nodes(value)


def _socials(soup: BeautifulSoup) -> dict:
    out = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "facebook.com" in href and "facebook" not in out:
            if not re.search(r"facebook\.com/(sharer|share|plugins)", href):
                out["facebook"] = href.split("?", 1)[0]
        elif "instagram.com" in href and "instagram" not in out:
            out["instagram"] = href.split("?", 1)[0]
    return out


def _contact_links(soup: BeautifulSoup, base: str) -> list[str]:
    """Contact-ish links the homepage actually offers. No URL guessing."""
    host = urlparse(base).netloc
    candidates = []
    for a in soup.find_all("a", href=True):
        text = " ".join(a.stripped_strings)[:60]
        href = a["href"]
        if not CONTACT_HINT.search(text) and not CONTACT_HINT.search(href):
            continue
        full = urljoin(base, href).split("#", 1)[0]
        if urlparse(full).netloc != host:
            continue
        if full.rstrip("/") == base.rstrip("/"):
            continue
        if full not in candidates:
            candidates.append(full)
    # Prefer an explicit "contact" over "about".
    candidates.sort(key=lambda u: 0 if re.search(r"contact", u, re.I) else 1)
    return candidates


def enrich_one(org: Organization, session: requests.Session,
               today: date) -> dict:
    """Fetch at most 3 pages for one organization and return a cache entry."""
    budget = Budget(config.ENRICH_MAX_REQUESTS_PER_ORG)
    result = {
        "email": "", "phone": "", "facebook": "", "instagram": "",
        "status": "no_website", "checked": today.isoformat(),
        "requests": 0,
    }
    if not org.website:
        return result

    def fetch(url):
        if not budget.spend():
            return None
        try:
            r = session.get(
                url, timeout=config.ENRICH_TIMEOUT, allow_redirects=True,
                headers={"User-Agent": config.USER_AGENT},
            )
            if r.status_code >= 400 or "html" not in r.headers.get(
                    "Content-Type", "text/html"):
                return None
            return r
        except requests.RequestException:
            return None

    resp = fetch(org.website)
    if resp is None:
        result["status"] = "error"
        result["requests"] = budget.used
        return result

    soup = BeautifulSoup(resp.text, "lxml")
    ld = _jsonld(soup)
    emails = _emails_from(soup, resp.text)
    phones = _phones_from(soup, resp.text)
    socials = _socials(soup)

    if ld.get("email") and _valid_email(ld["email"]):
        emails.insert(0, ld["email"])
    if ld.get("phone"):
        phones.insert(0, ld["phone"])

    # Only spend the remaining budget if the homepage gave us nothing.
    if not emails:
        for link in _contact_links(soup, resp.url)[:2]:
            page = fetch(link)
            if page is None:
                continue
            psoup = BeautifulSoup(page.text, "lxml")
            pld = _jsonld(psoup)
            if pld.get("email") and _valid_email(pld["email"]):
                emails.append(pld["email"])
            emails.extend(_emails_from(psoup, page.text))
            phones.extend(_phones_from(psoup, page.text))
            for k, v in _socials(psoup).items():
                socials.setdefault(k, v)
            if emails:
                break  # stop on first hit -- no hunting for a better address

    result["email"] = sorted(dict.fromkeys(emails), key=_rank_email)[0] if emails else ""
    result["phone"] = phones[0] if phones else ""
    result["facebook"] = socials.get("facebook", "")
    result["instagram"] = socials.get("instagram", "")
    result["requests"] = budget.used
    result["status"] = "found" if (result["email"] or result["phone"]) else "not_found"
    return result


def enrich(registry: dict[str, Organization], cache: dict,
           today: date | None = None, force: bool = False) -> dict:
    """Enrich every organization that needs it -- and only those.

    Returns run statistics. `orgs_fetched` must stay far below `orgs_seen` in
    steady state; if it ever approaches it, org-key normalization has broken.
    """
    today = today or date.today()

    todo = []
    cache_hits = 0
    for key, org in registry.items():
        entry = cache.get(key)
        if entry and not force and state.is_fresh(entry, today):
            cache_hits += 1
            _apply(org, entry)
            continue
        if not org.website:
            # Nothing to fetch; record it so it is not reconsidered daily.
            cache[key] = {
                "email": "", "phone": "", "facebook": "", "instagram": "",
                "status": "no_website", "checked": today.isoformat(),
                "requests": 0,
            }
            _apply(org, cache[key])
            continue
        todo.append(org)

    log.info(
        "enrichment: %d orgs seen, %d cache hits, %d to fetch",
        len(registry), cache_hits, len(todo),
    )
    # Rule 1, asserted rather than assumed.
    assert len(todo) <= len(registry), "enrichment queue exceeded org count"

    if todo:
        session = requests.Session()
        with ThreadPoolExecutor(max_workers=config.ENRICH_CONCURRENCY) as pool:
            results = pool.map(lambda o: (o, enrich_one(o, session, today)), todo)
            for org, entry in results:
                cache[org.key] = entry
                _apply(org, entry)

    requests_spent = sum(
        cache[o.key].get("requests", 0) for o in todo if o.key in cache
    )
    over_budget = [
        o.key for o in todo
        if cache.get(o.key, {}).get("requests", 0)
        > config.ENRICH_MAX_REQUESTS_PER_ORG
    ]
    assert not over_budget, f"budget exceeded for {over_budget[:3]}"

    found = sum(1 for o in registry.values() if o.email or o.phone)
    return {
        "orgs_seen": len(registry),
        "cache_hits": cache_hits,
        "orgs_fetched": len(todo),
        "requests_spent": requests_spent,
        "with_contact": found,
        "pct_with_contact": round(100 * found / len(registry), 1) if registry else 0.0,
    }


def _apply(org: Organization, entry: dict) -> None:
    org.email = entry.get("email", "")
    org.phone = entry.get("phone", "")
    org.facebook = entry.get("facebook", "")
    org.instagram = entry.get("instagram", "")
    org.contact_status = entry.get("status", "")
    org.contact_checked = entry.get("checked", "")
