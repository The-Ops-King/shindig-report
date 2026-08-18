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
                 workflow_id: str = "", session: requests.Session | None = None):
        self.api_key = api_key or config.GHL_API_KEY
        self.location_id = location_id or config.GHL_LOCATION_ID
        self.workflow_id = workflow_id or config.GHL_WORKFLOW_ID
        self.session = session or requests.Session()

    def configured(self) -> tuple[bool, str]:
        missing = [n for n, v in (
            ("GHL_API_KEY", self.api_key),
            ("GHL_LOCATION_ID", self.location_id),
            ("GHL_WORKFLOW_ID", self.workflow_id),
        ) if not v]
        return (not missing), ", ".join(missing)

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
            "tags": ["shindig-report", f"licensor-{p.source}"],
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

    # --- the whole operation for one candidate ------------------------------

    def push(self, cand) -> tuple[bool, str]:
        """Upsert and enrol. Returns (ok, error). Never raises."""
        try:
            contact_id = self.upsert_contact(cand)
            cand.ghl_contact_id = contact_id
            if cand.sending:
                self.enrol(contact_id)
            return True, ""
        except Exception as exc:            # one contact must not kill the run
            log.warning("GHL push failed for %s: %s", cand.address, exc)
            return False, str(exc)[:200]
