"""Go High Level client: upsert a contact, then enrol it in a workflow.

Entry into the sequence is driven by a **tag**, not by the workflow API alone.
That is what lets a person be added or removed by hand in the UI, or by another
automation, and behave exactly as if this code had done it. The code owns show
state -- which show, when it opens, which sample link -- and GHL owns the
messaging.

Three things about GHL shape this module.

**Re-entry is silently skipped.** GHL's "Allow Re-Entry" setting does not let a
contact re-enter a workflow they are still *active* in -- the request succeeds
and simply does nothing. This pipeline walks straight into that, because the
workflow reminds people right up to their show date, so they stay active until
it passes. Every enrolment therefore removes first and adds second, which
guarantees a clean restart when a contact rolls forward to a new show.

**A tag trigger fires only on a tag being newly added.** Re-adding a tag the
contact already carries is not an error and not a trigger -- it is nothing. So
the tag comes off before it goes back on, for the same reason the workflow
membership does.

**One contact must never break the run.** The same lesson as the malformed URL
that killed a whole scrape: failures are captured per contact and returned, not
raised.
"""

from __future__ import annotations

import logging
import time

import requests

import config

log = logging.getLogger(__name__)


class GHLError(RuntimeError):
    pass


class GHLClient:
    def __init__(self, api_key: str = "", location_id: str = "",
                 workflow_id: str = "", pipeline_id: str = "",
                 stage_id: str = "", session: requests.Session | None = None):
        self.api_key = api_key or config.GHL_API_KEY
        self.location_id = location_id or config.GHL_LOCATION_ID
        self.workflow_id = workflow_id or config.GHL_WORKFLOW_ID
        self.pipeline_id = pipeline_id or config.GHL_PIPELINE_ID
        self.stage_id = stage_id or config.GHL_PIPELINE_STAGE_ID
        self.session = session or requests.Session()

    def configured(self) -> tuple[bool, str]:
        """Enough to write contacts, fields, tags and cards."""
        missing = [n for n, v in (
            ("GHL_API_KEY", self.api_key),
            ("GHL_LOCATION_ID", self.location_id),
        ) if not v]
        return (not missing), ", ".join(missing)

    def can_enrol(self) -> tuple[bool, str]:
        """Enough to also put someone into the workflow.

        Separate from configured() so the whole ingest is not blocked by a
        workflow that has not been built yet: writing the CRM and starting the
        emails are different decisions, made at different times.
        """
        ok, missing = self.configured()
        if not self.workflow_id:
            return False, ", ".join(x for x in (missing, "GHL_WORKFLOW_ID") if x)
        return ok, missing

    def pipeline_configured(self) -> bool:
        """Opportunities are optional: without ids we still upsert and enrol."""
        return bool(self.pipeline_id and self.stage_id)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Version": config.GHL_API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, payload: dict | None = None,
                 retries: int = 3):
        url = f"{config.GHL_BASE}{path}"
        last = None
        for attempt in range(retries):
            try:
                resp = self.session.request(
                    method, url, json=payload, headers=self._headers(),
                    timeout=config.GHL_TIMEOUT,
                )
            except requests.RequestException as exc:
                last = exc
            else:
                if resp.status_code < 300:
                    try:
                        return resp.json()
                    except ValueError:
                        return {}
                # 429 and 5xx are worth another go; 4xx is our mistake.
                if resp.status_code != 429 and resp.status_code < 500:
                    raise GHLError(
                        f"{method} {path} -> {resp.status_code}: {resp.text[:300]}"
                    )
                last = GHLError(f"{method} {path} -> {resp.status_code}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
        raise GHLError(str(last))

    # --- contacts ----------------------------------------------------------

    @staticmethod
    def custom_fields(cand) -> list[dict]:
        """Field values for one candidate, under whatever keys GHL uses.

        A clear (no production) blanks every field rather than leaving last
        season's values sitting there: if someone is added back to the sequence
        by hand, stale values would render a show that has already happened.
        """
        p = cand.production
        if p is None:
            values = {name: "" for name in config.GHL_FIELDS}
        else:
            values = {
                "next_show_title": p.show_title,
                "next_show_start": p.start_date.isoformat() if p.start_date else "",
                "next_show_end": p.end_date.isoformat() if p.end_date else "",
                "next_show_venue": p.venue or p.organization,
                "next_show_city": ", ".join(x for x in (p.city, p.state) if x),
                "sample_playbill_url": cand.sample_url,
                "licensor": p.source.upper(),
            }
        out = []
        for name, value in values.items():
            field_id = config.GHL_FIELD_IDS.get(name)
            # Prefer the id. A fieldKey that matches nothing is not an error in
            # GHL -- it is accepted and dropped, so the merge tag renders empty
            # with nothing anywhere to say why.
            ref = {"id": field_id} if field_id else {"key": name}
            out.append({**ref, "field_value": value})
        return out

    def upsert_contact(self, cand) -> str:
        """Create or update, returning the GHL contact id.

        Upsert matches on email per the location's duplicate settings, so
        re-running a day is safe rather than duplicating people.
        """
        p = cand.production
        payload = {
            "locationId": self.location_id,
            "email": cand.address,
            "source": "Shindig Report",
            # Provenance only. The outreach tag is deliberately NOT set here:
            # upsert merges tags, so a tag already present stays present and
            # the workflow's trigger never fires. enrol() owns that tag.
            "tags": [config.GHL_SOURCE_TAG],
            "customFields": self.custom_fields(cand),
        }
        # Never blank a name we do not have. A clear is built from the stored
        # outreach record, and an early record may not carry one -- sending ""
        # would overwrite whatever is in GHL with nothing.
        if cand.org_name:
            payload["name"] = cand.org_name
            payload["companyName"] = cand.org_name
        if p is not None:
            payload.update({
                "address1": p.street,
                "city": p.city,
                "state": p.state,
                "postalCode": p.postal,
                "country": p.country or "US",
            })
            payload["tags"].append(
                config.GHL_LICENSOR_TAGS.get(p.source, p.source.upper()))
        elif cand.org is not None:
            # No production to read: a company mid-run, or one being cleared.
            # The registry holds the same address, and `sources` records every
            # licensor it was seen under rather than just the one show's.
            org = cand.org
            payload.update({
                "address1": org.street,
                "city": org.city,
                "state": org.state,
                "country": org.country or "US",
            })
            for source in sorted(org.sources or ()):
                payload["tags"].append(
                    config.GHL_LICENSOR_TAGS.get(source, source.upper()))
        data = self._request("POST", "/contacts/upsert", payload)
        contact = data.get("contact") or data
        contact_id = contact.get("id") or contact.get("contactId") or ""
        if not contact_id:
            raise GHLError(f"upsert returned no contact id: {str(data)[:200]}")
        cand.has_outreach_tag = self._carries_outreach_tag(contact_id, contact)
        return contact_id

    def _carries_outreach_tag(self, contact_id: str, contact: dict) -> bool:
        """Whether this contact is already in the sequence.

        The upsert response normally carries `tags`, which makes this free. The
        fallback read only happens if it does not, and only for the handful of
        contacts whose show rolled over on a given day.

        Absent is the safe answer: a missing tag means no re-arm, and no re-arm
        means nothing is sent. A failed read must never be mistaken for "yes".
        """
        tags = contact.get("tags")
        if tags is None:
            try:
                data = self._request("GET", f"/contacts/{contact_id}", retries=1)
                tags = (data.get("contact") or data).get("tags") or []
            except GHLError as exc:
                log.debug("could not read tags for %s (%s)", contact_id, exc)
                return False
        wanted = config.GHL_OUTREACH_TAG.lower()
        return any((t or "").strip().lower() == wanted for t in tags)

    # --- workflow ----------------------------------------------------------

    def remove_from_workflow(self, contact_id: str) -> None:
        """Best effort. A contact who was never enrolled 404s, which is fine."""
        if not self.workflow_id:
            return
        try:
            self._request(
                "DELETE",
                f"/contacts/{contact_id}/workflow/{self.workflow_id}",
                retries=1,
            )
        except GHLError as exc:
            log.debug("remove from workflow (ignored): %s", exc)

    def add_to_workflow(self, contact_id: str) -> None:
        if not self.workflow_id:
            log.debug("no GHL_WORKFLOW_ID; tag alone has to carry the enrolment")
            return
        self._request(
            "POST", f"/contacts/{contact_id}/workflow/{self.workflow_id}", {}
        )

    def set_tags(self, contact_id: str, add=(), remove=()) -> None:
        """Add and/or remove tags explicitly.

        Upsert merges tags and never takes one away, so this is the only route
        back out of the sequence. Removal is best effort -- a tag the contact
        does not carry is not an error worth failing a run over.
        """
        if remove:
            try:
                self._request("DELETE", f"/contacts/{contact_id}/tags",
                              {"tags": list(remove)}, retries=1)
            except GHLError as exc:
                log.debug("remove tags (ignored): %s", exc)
        if add:
            self._request("POST", f"/contacts/{contact_id}/tags",
                          {"tags": list(add)})

    def enrol(self, contact_id: str) -> None:
        """Restart the sequence cleanly.

        Two separate silent no-ops have to be stepped around, and both look
        exactly like success:

        * GHL declines to re-enrol a contact still *active* in the workflow,
          whatever "Allow Re-Entry" says -- and this sequence reminds people
          right up to their show date, so they are always still active.
        * A Contact-Tag trigger fires only when the tag is *newly added*.
          Re-adding a tag someone already carries does nothing at all.

        So: drop them out of the workflow, take the tag off, put it back, and
        then add them directly as well. The two halves cover each other. The
        DELETE is what actually stops a running sequence, since dropping a tag
        does not; and the direct add is what still enrols someone if the tag
        trigger is mis-wired in the UI. Whichever fires first, the other is a
        no-op, so nobody is enrolled twice.
        """
        self.remove_from_workflow(contact_id)
        self.set_tags(contact_id, remove=[config.GHL_OUTREACH_TAG])
        self.set_tags(contact_id, add=[config.GHL_OUTREACH_TAG])
        self.add_to_workflow(contact_id)

    def rearm(self, contact_id: str) -> None:
        """Put someone already in the sequence back through it for a new show.

        Mechanically identical to enrol(), and for the identical reasons: GHL
        fires a tag trigger only on a tag *newly* added, and declines to
        re-enrol a contact still active in a workflow. Both refusals look
        exactly like success.

        The difference is who it may touch. enrol() can start a conversation;
        this can only continue one, because push() reaches it solely for a
        contact already carrying the tag.
        """
        self.enrol(contact_id)

    def clear(self, contact_id: str) -> None:
        """Their show has passed and nothing is booked: stop the sequence."""
        self.remove_from_workflow(contact_id)
        self.set_tags(contact_id, remove=[config.GHL_OUTREACH_TAG])

    def sync_ready_tag(self, cand) -> None:
        """Add or drop the ready tag, but only when it actually changed.

        Upsert merges tags and never removes one, so the tag cannot simply be
        set from the payload -- it would stick after the show passed. Writing
        only on a transition also keeps the cost off the ~1,800 organizations
        that are not ready on any given day.
        """
        if cand.ready == cand.was_ready:
            return
        if cand.ready:
            self.set_tags(cand.ghl_contact_id, add=[config.GHL_READY_TAG])
        else:
            self.set_tags(cand.ghl_contact_id, remove=[config.GHL_READY_TAG])

    # --- opportunities ------------------------------------------------------

    @staticmethod
    def opportunity_name(cand) -> str:
        p = cand.production
        if p is None:
            # Mid-run, or nothing announced. The card is the company, so its
            # name alone is a truthful label until there is a show to add.
            return cand.org_name
        when = p.start_date.strftime("%b %Y") if p.start_date else "TBD"
        return f"{cand.org_name} - {p.show_title} ({when})"

    def create_opportunity(self, cand) -> str:
        payload = {
            "pipelineId": self.pipeline_id,
            "pipelineStageId": self.stage_id,
            "locationId": self.location_id,
            "contactId": cand.ghl_contact_id,
            "name": self.opportunity_name(cand),
            "status": "open",
            "monetaryValue": 0,
            "source": "Shindig Report",
        }
        data = self._request("POST", "/opportunities/", payload)
        opp = data.get("opportunity") or data
        return opp.get("id") or opp.get("_id") or ""

    def update_opportunity(self, opportunity_id: str, cand) -> None:
        """Rename to the new show and put the card back at the first stage.

        One open opportunity per contact, refreshed on rollover, rather than a
        new card per show -- otherwise a company with a full season leaves a
        trail of dead cards in the pipeline.
        """
        self._request("PUT", f"/opportunities/{opportunity_id}", {
            "pipelineId": self.pipeline_id,
            "pipelineStageId": self.stage_id,
            "name": self.opportunity_name(cand),
            "status": "open",
        })

    def sync_opportunity(self, cand) -> str:
        """Create or refresh, returning the opportunity id ("" if disabled)."""
        if not self.pipeline_configured():
            return cand.ghl_opportunity_id
        if cand.ghl_opportunity_id:
            try:
                self.update_opportunity(cand.ghl_opportunity_id, cand)
                return cand.ghl_opportunity_id
            except GHLError as exc:
                # Card deleted in the UI, most likely. Make a fresh one.
                log.info("opportunity %s not updatable (%s); recreating",
                         cand.ghl_opportunity_id, exc)
        return self.create_opportunity(cand)

    # --- the whole operation for one candidate ------------------------------

    def push(self, cand, rearm_check=None) -> tuple[bool, str]:
        """Upsert and enrol. Returns (ok, error). Never raises.

        `rearm_check` is asked, after the upsert, whether a contact whose show
        just rolled over should re-enter the sequence. It is a callback rather
        than a field because the answer depends on the ingest ledger, which is
        main's business, while whether the contact carries the tag is only
        known once the upsert has answered.
        """
        try:
            contact_id = self.upsert_contact(cand)
            cand.ghl_contact_id = contact_id
            self.sync_ready_tag(cand)
            if cand.action == "clear":
                # The card stays exactly where it is. The company has not gone
                # away, it just has nothing on the books; only the show state
                # is retired, so their next announcement reuses this card.
                self.clear(contact_id)
                return True, ""
            # Every organization gets a card, whether or not it is the one
            # sending today -- the pipeline tracks companies, not emails.
            cand.ghl_opportunity_id = self.sync_opportunity(cand)
            if cand.sending:
                self.enrol(contact_id)
            elif rearm_check is not None and rearm_check(cand):
                # Their show moved and they are already in the sequence, so the
                # new show is the next beat of a conversation someone else
                # started. Never a first contact: rearm_check only says yes for
                # a contact already carrying the tag.
                self.rearm(contact_id)
                cand.rearmed = True
            return True, ""
        except Exception as exc:            # one contact must not kill the run
            log.warning("GHL push failed for %s: %s", cand.address, exc)
            return False, str(exc)[:200]
