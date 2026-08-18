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

import re

import config

_ADDR = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def normalize(address: str) -> str:
    """Lowercase and trim. This is the key everything is deduped on."""
    return (address or "").strip().lower()


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
