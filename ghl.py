"""Go High Level client: upsert a contact, then enrol it in a workflow.

Two things about GHL shape this module.

**Re-entry is silently skipped.** GHL's "Allow Re-Entry" setting does not let a
contact re-enter a workflow they are still *active* in -- the request succeeds
and simply does nothing. This pipeline walks straight into that, because the
workflow reminds people right up to their show date, so they stay active until
it passes. Every enrolment therefore removes first and adds second, which
guarantees a clean restart when a contact rolls forward to a new show.

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
        missing = [n for n, v in (
            ("GHL_API_KEY", self.api_key),
            ("GHL_LOCATION_ID", self.location_id),
            ("GHL_WORKFLOW_ID", self.workflow_id),
        ) if not v]
        return (not missing), ", ".join(missing)

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
        p = cand.production
        values = {
            "next_show_title": p.show_title,
            "next_show_start": p.start_date.isoformat() if p.start_date else "",
            "next_show_end": p.end_date.isoformat() if p.end_date else "",
            "next_show_venue": p.venue or p.organization,
            "next_show_city": ", ".join(x for x in (p.city, p.state) if x),
            "sample_playbill_url": cand.sample_url,
            "licensor": p.source.upper(),
        }
        return [{"key": k, "field_value": v} for k, v in values.items()]

    def upsert_contact(self, cand) -> str:
        """Create or update, returning the GHL contact id.

        Upsert matches on email per the location's duplicate settings, so
        re-running a day is safe rather than duplicating people.
        """
        p = cand.production
        payload = {
            "locationId": self.location_id,
            "email": cand.address,
            "name": cand.org_name,
            "companyName": cand.org_name,
            "address1": p.street,
            "city": p.city,
            "state": p.state,
            "postalCode": p.postal,
            "country": p.country or "US",
            "source": "Shindig Report",
            "tags": [
                config.GHL_SOURCE_TAG,
                config.GHL_LICENSOR_TAGS.get(p.source, p.source.upper()),
            ],
            "customFields": self.custom_fields(cand),
        }
        data = self._request("POST", "/contacts/upsert", payload)
        contact = data.get("contact") or data
        contact_id = contact.get("id") or contact.get("contactId") or ""
        if not contact_id:
            raise GHLError(f"upsert returned no contact id: {str(data)[:200]}")
        return contact_id

    # --- workflow ----------------------------------------------------------

    def remove_from_workflow(self, contact_id: str) -> None:
        """Best effort. A contact who was never enrolled 404s, which is fine."""
        try:
            self._request(
                "DELETE",
                f"/contacts/{contact_id}/workflow/{self.workflow_id}",
                retries=1,
            )
        except GHLError as exc:
            log.debug("remove from workflow (ignored): %s", exc)

    def add_to_workflow(self, contact_id: str) -> None:
        self._request(
            "POST", f"/contacts/{contact_id}/workflow/{self.workflow_id}", {}
        )

    def enrol(self, contact_id: str) -> None:
        """Restart the sequence cleanly.

        Removing first is what makes a rollover actually send. Skip it and GHL
        silently declines to re-enrol anyone still active in the workflow, with
        no error to notice.
        """
        self.remove_from_workflow(contact_id)
        self.add_to_workflow(contact_id)

    # --- opportunities ------------------------------------------------------

    @staticmethod
    def opportunity_name(cand) -> str:
        p = cand.production
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

    def push(self, cand) -> tuple[bool, str]:
        """Upsert and enrol. Returns (ok, error). Never raises."""
        try:
            contact_id = self.upsert_contact(cand)
            cand.ghl_contact_id = contact_id
            # Every organization gets a card, whether or not it is the one
            # sending today -- the pipeline tracks companies, not emails.
            cand.ghl_opportunity_id = self.sync_opportunity(cand)
            if cand.sending:
                self.enrol(contact_id)
            return True, ""
        except Exception as exc:            # one contact must not kill the run
            log.warning("GHL push failed for %s: %s", cand.address, exc)
            return False, str(exc)[:200]
