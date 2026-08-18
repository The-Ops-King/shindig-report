"""Decide who to contact, about which show, with which sample link.

The unit of outreach is a *person* -- one email address -- not an organization
and not a production. That distinction is load-bearing: 416 addresses in the
live data are shared across several organizations, so keying on the org would
send 643 duplicate emails, and `mail@haletheater.org` alone would get five.

Each address has one "current show". When that show passes, the address rolls
forward to whatever it is doing next and the sequence restarts with the new
link. The code owns show state; GHL owns messaging.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import config
import emails

log = logging.getLogger(__name__)

_TITLE_NOISE = re.compile(r"^(the|a|an)\s+", re.I)
_TITLE_PUNCT = re.compile(r"[^a-z0-9 ]+")


def normalize_title(title: str) -> str:
    """Key for matching a show against the Show Links tab.

    Deliberately conservative. Case, punctuation and a leading article are
    noise ("Disney's The Little Mermaid Jr" vs "Disney's the Little Mermaid
    JR"), but "Jr", "KIDS" and "Teen Edition" are NOT: they are separate
    licensed products with their own playbills. No fuzzy matching either --
    linking a school's Annie Jr to the full Annie sample is worse than sending
    nothing, because it is visibly wrong to the one person we want to impress.
    """
    t = (title or "").lower().replace("&", " and ")
    t = t.replace("'", "").replace("’", "")
    t = _TITLE_PUNCT.sub(" ", t)
    t = _TITLE_NOISE.sub("", t.strip())
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class Candidate:
    """One address, the show it is doing next, and what we intend to do."""
    address: str
    org_key: str
    org_name: str
    production: object                 # normalize.Production
    sample_url: str = ""
    action: str = "none"               # send | rollover | update_only | clear | none
    reason: str = ""                   # why, when not sending
    ghl_contact_id: str = ""
    sends: int = 0

    @property
    def sending(self) -> bool:
        return self.action in ("send", "rollover")


def true_next_show(productions: list, today: date):
    """Soonest upcoming production, regardless of the outreach window.

    Licence windows are excluded: a touring producer holding rights from 2018
    to 2027 has no meaningful opening night, so it would otherwise mask the
    real next show forever.
    """
    upcoming = [
        p for p in productions
        if p.start_date and p.start_date >= today
        and p.date_type != "license window"
    ]
    return min(upcoming, key=lambda p: (p.start_date, p.key)) if upcoming else None


def in_window(production, today: date) -> bool:
    if not (production and production.start_date):
        return False
    lo = today + timedelta(days=config.OUTREACH_WINDOW_MIN_DAYS)
    hi = today + timedelta(days=config.OUTREACH_WINDOW_MAX_DAYS)
    return lo <= production.start_date <= hi


def _parse(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def gap_ok(record: dict, today: date) -> bool:
    """Enough time since the last send to this address.

    72% of organizations have a single future show, so rollover is rare for
    them -- but 24% of consecutive-show gaps are under 30 days, and without a
    floor a company with a packed season gets an email every fortnight.
    """
    last = _parse((record or {}).get("last_sent", ""))
    if not last:
        return True
    return (today - last).days >= config.OUTREACH_MIN_GAP_DAYS


def build_candidates(productions: list, registry: dict, show_links: dict,
                     outreach_state: dict, today: date) -> list[Candidate]:
    """One Candidate per sendable address, with its decided action."""
    by_org: dict[str, list] = {}
    for p in productions:
        by_org.setdefault(p.org_key, []).append(p)

    # Collapse organizations onto addresses; the soonest-opening org wins the
    # address, so a shared inbox hears about one show, not five.
    per_address: dict[str, tuple] = {}
    for org_key, rows in by_org.items():
        org = registry.get(org_key)
        if not org or not emails.is_sendable(org.email):
            continue
        nxt = true_next_show(rows, today)
        if nxt is None:
            continue
        addr = emails.normalize(org.email)
        current = per_address.get(addr)
        if current is None or nxt.start_date < current[1].start_date:
            per_address[addr] = (org, nxt)

    candidates = []
    for addr, (org, production) in per_address.items():
        record = outreach_state.get(addr) or {}
        cand = Candidate(
            address=addr, org_key=org.key, org_name=org.name,
            production=production,
            ghl_contact_id=record.get("ghl_contact_id", ""),
            sends=int(record.get("sends") or 0),
        )
        cand.sample_url = show_links.get(normalize_title(production.show_title), "")
        cand.action, cand.reason = _decide(cand, record, production, today)
        candidates.append(cand)

    return candidates


def _decide(cand: Candidate, record: dict, production, today: date):
    known = record.get("current_show_key")
    if known == production.key:
        return "none", "already current"

    # Anything below is a new contact or a genuine roll-forward.
    if production.country not in config.OUTREACH_COUNTRIES:
        return "update_only", f"country {production.country or '?'} not enabled"
    if not in_window(production, today):
        # Bucketed, not per-day: an exact day count makes every row its own
        # "reason" and turns the digest summary into noise.
        days = (production.start_date - today).days
        side = "too soon" if days < config.OUTREACH_WINDOW_MIN_DAYS else "too far out"
        return "update_only", f"outside window ({side})"
    if not cand.sample_url:
        return "update_only", "no sample link for this show"
    if not gap_ok(record, today):
        return "update_only", "too soon since last send"
    return ("rollover" if known else "send"), ""


def select(candidates: list[Candidate], cap: int | None = None) -> list[Candidate]:
    """Apply the daily cap. Soonest opening first -- most time-critical."""
    cap = config.OUTREACH_DAILY_CAP if cap is None else cap
    sending = [c for c in candidates if c.sending]
    sending.sort(key=lambda c: (c.production.start_date, c.address))
    if cap >= 0:
        held = sending[cap:]
        for c in held:
            c.action, c.reason = "update_only", "held by daily cap"
        sending = sending[:cap]
    return sending


def summarize(candidates: list[Candidate]) -> dict:
    counts = {"total": len(candidates)}
    for c in candidates:
        counts[c.action] = counts.get(c.action, 0) + 1
    reasons = {}
    for c in candidates:
        if c.action == "update_only" and c.reason:
            reasons[c.reason] = reasons.get(c.reason, 0) + 1
    counts["reasons"] = reasons
    return counts


def record_send(outreach_state: dict, cand: Candidate, today: date) -> None:
    p = cand.production
    outreach_state[cand.address] = {
        "ghl_contact_id": cand.ghl_contact_id,
        "org_key": cand.org_key,
        "org_name": cand.org_name,
        "current_show_key": p.key,
        "current_show_title": p.show_title,
        "current_show_start": p.start_date.isoformat() if p.start_date else "",
        "current_show_end": p.end_date.isoformat() if p.end_date else "",
        "sample_url": cand.sample_url,
        "last_sent": today.isoformat(),
        "sends": cand.sends + 1,
    }
