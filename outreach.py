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
    production: object = None          # normalize.Production; None on a clear
    sample_url: str = ""
    action: str = "none"      # send | rollover | hold | update_only | clear | none
    reason: str = ""                   # why, when not sending
    ghl_contact_id: str = ""
    ghl_opportunity_id: str = ""
    sends: int = 0
    was_ready: bool = False            # carried the ready tag as of last run
    org: object = None                 # orgs.Organization -- the fallback for
                                       # address and licensor when production
                                       # is None

    @property
    def sending(self) -> bool:
        return self.action in ("send", "rollover")

    @property
    def ready(self) -> bool:
        """Would be emailed right now if the sequence were running.

        `hold` is a send that was withheld only because the workflow does not
        exist yet, so it counts: the point of the ready tag is to name the set
        that is safe to start by hand.
        """
        return self.action in ("send", "rollover", "hold")


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
                     outreach_state: dict, today: date,
                     hold: bool = False) -> list[Candidate]:
    """One Candidate per organization that has a contact and an upcoming show.

    Two different units of identity, deliberately:

      * the **organization** is the unit of pipeline -- each company buys its
        own playbills, so each gets its own opportunity card;
      * the **address** is the unit of email -- 416 addresses are shared across
        several organizations, so only one of them may actually send.

    So every eligible organization becomes a candidate, and then exactly one
    candidate per address is allowed to send: the one whose show opens soonest.
    The rest are update_only, which still keeps their card and custom fields
    current in GHL.
    """
    by_org: dict[str, list] = {}
    for p in productions:
        by_org.setdefault(p.org_key, []).append(p)

    candidates = []
    for org_key, rows in by_org.items():
        org = registry.get(org_key)
        if not org or not emails.is_sendable(org.email):
            continue
        nxt = true_next_show(rows, today)
        if nxt is None and not hold:
            continue

        addr = emails.normalize(org.email)
        record = outreach_state.get(addr) or {}
        cand = Candidate(
            address=addr, org_key=org_key, org_name=org.name, production=nxt,
            ghl_contact_id=record.get("ghl_contact_id", ""),
            sends=int(record.get("sends") or 0),
            org=org,
        )
        if nxt is None:
            # A company whose show opened last week and is still running has no
            # *upcoming* start, so it would otherwise vanish from the run
            # entirely -- 150 of them in the live data. They are exactly as real
            # a lead as one opening next month, so ingest takes them with their
            # show fields blank until they announce something.
            cand.action, cand.reason = "update_only", "no upcoming show"
            candidates.append(cand)
            continue
        cand.sample_url = show_links.get(normalize_title(nxt.show_title), "")
        candidates.append(cand)

    # One sender per address: soonest opening wins the inbox.
    winners: dict[str, Candidate] = {}
    for cand in candidates:
        if cand.production is None:
            continue
        held = winners.get(cand.address)
        if held is None or cand.production.start_date < held.production.start_date:
            winners[cand.address] = cand

    for cand in candidates:
        if cand.production is None:
            continue
        record = outreach_state.get(cand.address) or {}
        if winners.get(cand.address) is not cand:
            cand.action = "update_only"
            cand.reason = "another organization shares this inbox"
            continue
        cand.action, cand.reason = _decide(cand, record, cand.production, today)
        if hold and cand.sending:
            # Ingest mode: everything is written to GHL, nothing is enrolled.
            # Withholding here rather than at the push keeps record_send out of
            # the picture entirely -- stamping a send now would mark all 2,500
            # as already told, and _decide would answer "already current"
            # forever after, so they could never be emailed at all.
            cand.action, cand.reason = "hold", "workflow not live yet"

    candidates.extend(_clears(candidates, outreach_state))
    return candidates


def _clears(candidates: list[Candidate], outreach_state: dict) -> list[Candidate]:
    """Addresses we told about a show that now have nothing upcoming.

    Without this, someone whose show has been and gone simply stops appearing
    in the candidate list and sits in the workflow forever -- the run has no
    way to notice, because the organization drops out of `by_org` silently.

    Guarding on `current_show_key` is what keeps this cheap and idempotent.
    Only people we actually emailed are eligible, and clearing blanks that key,
    so a dormant organization is cleared once rather than re-cleared every
    morning for the rest of its life.
    """
    live = {c.address for c in candidates}
    out = []
    for addr, record in outreach_state.items():
        if addr in live or not record.get("current_show_key"):
            continue
        out.append(Candidate(
            address=addr,
            org_key=record.get("org_key", ""),
            org_name=record.get("org_name", ""),
            production=None,
            action="clear",
            reason="show has passed, nothing upcoming",
            ghl_contact_id=record.get("ghl_contact_id", ""),
            sends=int(record.get("sends") or 0),
        ))
    return out


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


def load_opportunity_ids(cands: list, opportunities: dict) -> None:
    """Attach what GHL already knows about each organization.

    The card id so we update rather than duplicate, and the ready flag so the
    ready tag is only written on a transition instead of on every run.
    """
    for cand in cands:
        record = opportunities.get(cand.org_key) or {}
        cand.ghl_opportunity_id = record.get("opportunity_id", "")
        cand.was_ready = bool(record.get("ready"))


def needs_ingest(cand: Candidate, opportunities: dict,
                 want_card: bool = False) -> bool:
    """Whether this organization has anything new to write to GHL.

    The opportunities file doubles as the ingest ledger: it is already keyed by
    organization and already records the show. So the bootstrap writes ~2,500
    contacts once, and every run after it writes only what actually changed --
    the same bargain the enrichment cache makes.

    `want_card` is what makes adding the pipeline secrets self-healing. An
    ingest run without them writes contacts and no cards; once the ids arrive,
    every organization whose record has no card is written once more to get
    one, and then goes quiet again.
    """
    record = opportunities.get(cand.org_key)
    if not record:
        return True
    if want_card and not record.get("opportunity_id"):
        return True
    show_key = cand.production.key if cand.production else ""
    return (record.get("show_key") != show_key
            or bool(record.get("ready")) != cand.ready)


def record_opportunity(opportunities: dict, cand: Candidate, today: date) -> None:
    """One record per organization -- the pipeline's unit of identity, and the
    ledger of what GHL already holds.

    Recorded even when there is no card. Without the pipeline ids configured
    GHL creates no opportunity, and bailing out here on the empty id left the
    ledger empty after a successful ingest -- so the same organizations were
    re-pushed every single morning, forever. The contact was written; that is
    the fact worth remembering, card or no card.
    """
    if not cand.ghl_contact_id:
        return
    opportunities[cand.org_key] = {
        "opportunity_id": cand.ghl_opportunity_id,
        "contact_id": cand.ghl_contact_id,
        "org_name": cand.org_name,
        "show_key": cand.production.key if cand.production else "",
        "show_title": cand.production.show_title if cand.production else "",
        "ready": cand.ready,
        "updated": today.isoformat(),
    }


def record_clear(outreach_state: dict, cand: Candidate, today: date) -> None:
    """Retire the show state without losing the history.

    Emptying `current_show_key` is what makes a clear happen exactly once, and
    what makes their next announcement a fresh send rather than a rollover.
    `last_sent` and `sends` are deliberately kept, so gap_ok() still holds the
    45-day floor -- clearing can never become a way to email someone twice in
    quick succession.
    """
    record = dict(outreach_state.get(cand.address) or {})
    record.update({
        "ghl_contact_id": cand.ghl_contact_id or record.get("ghl_contact_id", ""),
        "current_show_key": "",
        "current_show_title": "",
        "current_show_start": "",
        "current_show_end": "",
        "sample_url": "",
        "cleared": today.isoformat(),
    })
    outreach_state[cand.address] = record


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
