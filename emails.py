"""Email address hygiene, shared by enrichment and outreach.

Enrichment scrapes whatever a site publishes, and a lot of what sites publish
is not a contact. Measured against the live cache of 2,831 scraped addresses:

  * 151 (5.3%) are template placeholders that were never real --
    "user@domain.com" alone appears on 98 organizations, plus mysite.com,
    website.com, email.com, info@yourdomain.com, firstname.lastname@...
  * 19 are "...@group.calendar.google.com" -- Google Calendar feed ids lifted
    from embedded calendars, not addresses at all.

Sending to these is not merely wasted: placeholder domains hard-bounce, and
hard bounces are what destroy a cold sending domain's reputation. So the same
rejection runs in two places -- in enrichment, so junk never reaches the
Contacts tab, and again at send time, so rows cached before this existed are
still caught.
"""

from __future__ import annotations

import json
import re

import config

_ADDR = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# A percent-escape that survived into an address means it was lifted out of a
# URL and never decoded -- "%20centrestageinc@yahoo.com" is a mailto with a
# leading space, "05%7c01%7cmdecorre@cbsd.org" is a fragment of an Outlook
# safelink. Seven of these reached GHL. They cannot deliver, so they are
# rejected outright rather than guessed at: decoding one back to a plausible
# address would be inventing a recipient.
_URL_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
# Pages that hide an address from scrapers often print it masked --
# "co*************@**************my.org". The real address is not recoverable
# from that, so it is rejected outright rather than sent for verification,
# where it would spend a credit to come back undeliverable.
_MASKED = re.compile(r"[*\u2022]")

_suppressed: set | None = None
_verified: dict | None = None


def verdicts() -> dict:
    """Address -> what verification said about it, and when.

    Cached on disk so an address is checked once and never paid for again --
    the same bargain the enrichment cache makes. Seeded from the Verifalia
    export; the API will write the same shape.
    """
    global _verified
    if _verified is None:
        try:
            with open(config.VERIFIED_PATH, encoding="utf-8") as fh:
                _verified = {normalize(k): v for k, v in json.load(fh).items()}
        except (OSError, ValueError):
            _verified = {}
    return _verified


def record_verdicts(results: dict, today) -> dict:
    """Persist Verifalia's answers, and suppress what cannot be delivered.

    Returns a count per classification for the run log. Undeliverable is added
    to the suppression list here rather than at the call site, so there is one
    place where "this address bounces" turns into "never touch it again".
    """
    if not results:
        return {}
    verdicts()                                  # ensure the cache is loaded
    counts = {}
    bad = set(suppressed())
    for address, classification in results.items():
        addr = normalize(address)
        _verified[addr] = {"status": classification.lower(),
                           "source": "verifalia",
                           "checked": today.isoformat()}
        counts[classification] = counts.get(classification, 0) + 1
        if classification.lower() == "undeliverable":
            bad.add(addr)

    config.VERIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(config.VERIFIED_PATH, "w", encoding="utf-8") as fh:
        json.dump(_verified, fh, indent=0, sort_keys=True)
        fh.write("\n")
    if bad != set(suppressed()):
        with open(config.SUPPRESSED_PATH, "w", encoding="utf-8") as fh:
            json.dump(sorted(bad), fh, indent=0)
            fh.write("\n")
        _suppressed.update(bad)
    return counts


def is_verified(address: str) -> bool:
    """Whether this address has a positive deliverability verdict.

    Unknown is not the same as bad. An unverified address is kept, tagged and
    left out of sending until someone checks it -- deleting on absence of
    evidence is what cost 770 contacts.
    """
    return (verdicts().get(normalize(address), {})
            .get("status", "").lower() == "deliverable")


def suppressed() -> set:
    """Addresses removed from GHL and never to be re-ingested.

    Deleting a contact is not enough on its own: the daily ingest re-creates any
    organization it cannot find in the ledger, so without this the next morning
    would put every deleted address straight back.
    """
    global _suppressed
    if _suppressed is None:
        try:
            with open(config.SUPPRESSED_PATH, encoding="utf-8") as fh:
                _suppressed = {normalize(a) for a in json.load(fh)}
        except (OSError, ValueError):
            _suppressed = set()
    return _suppressed


def normalize(address: str) -> str:
    """Lowercase, trim, and drop a "mailto:" prefix.

    This is the key everything is deduped on, so the prefix has to come off
    here rather than at the point of rejection -- otherwise the same inbox
    appears twice, once wearing the scheme and once without it. Unlike a
    percent-escape, "mailto:x@y" is not a guess: the scheme unambiguously
    names x@y, so stripping it invents no recipient.
    """
    addr = (address or "").strip().lower()
    while addr.startswith("mailto:"):
        addr = addr[len("mailto:"):].strip()
    return addr


def split(address: str) -> tuple[str, str]:
    addr = normalize(address)
    if "@" not in addr:
        return addr, ""
    mailbox, _, domain = addr.rpartition("@")
    return mailbox, domain


def rejection_reason(address: str) -> str:
    """Return why an address is unusable, or "" if it looks sendable.

    Returning the reason rather than a bool keeps the Contacts tab honest --
    a rejected row shows *why* instead of silently vanishing.
    """
    addr = normalize(address)
    if not addr:
        return "empty"
    if not _ADDR.match(addr):
        return "malformed"
    if _URL_ESCAPE.search(addr):
        return "url_encoded"
    if _MASKED.search(addr):
        return "masked"
    if addr in suppressed():
        return "suppressed"

    mailbox, domain = split(addr)

    if domain in config.PLACEHOLDER_DOMAINS:
        return "placeholder_domain"
    if any(domain == suffix or domain.endswith("." + suffix)
           for suffix in config.MACHINE_DOMAIN_SUFFIXES):
        return "machine_address"
    if mailbox in config.PLACEHOLDER_MAILBOXES:
        return "placeholder_mailbox"
    if mailbox in config.UNATTENDED_MAILBOXES:
        return "unattended_mailbox"
    if any(bad in addr for bad in config.EMAIL_BLOCKLIST_SUBSTRINGS):
        return "blocklisted"
    return ""


def is_sendable(address: str) -> bool:
    return not rejection_reason(address)
