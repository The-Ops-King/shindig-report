"""Verifalia email verification.

Deliverability is checked BEFORE a contact is created, so an address that
cannot receive mail never becomes a GHL contact at all. That ordering is the
whole point: the alternative is what happened in August, when 770 contacts had
to be deleted after the fact.

Two rules shape this module.

**It fails closed.** Any error, timeout, or classification it does not
recognise is treated as "not deliverable". A broken integration therefore
creates too few contacts, never too many. The failure that matters is the one
that floods a CRM with addresses that bounce.

**Nothing is assumed about the wire format.** `python verifalia.py --probe
<address>` submits a single address and dumps the raw response, so the parser
can be checked against what the API actually returns rather than what it was
expected to return. This codebase has twice been bitten by an API that accepted
a request and quietly did something other than expected.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import requests

import config

log = logging.getLogger(__name__)

BASE = "https://api.verifalia.com/v2.6"

# Verifalia's own vocabulary. Only the first is allowed to create a contact.
DELIVERABLE = "Deliverable"
UNDELIVERABLE = "Undeliverable"


class VerifaliaError(RuntimeError):
    pass


class Verifalia:
    def __init__(self, username: str = "", password: str = "",
                 session: requests.Session | None = None):
        self.username = username or config.VERIFALIA_USERNAME
        self.password = password or config.VERIFALIA_PASSWORD
        self.session = session or requests.Session()

    def configured(self) -> bool:
        return bool(self.username and self.password)

    def _request(self, method: str, path: str, payload: dict | None = None):
        resp = self.session.request(
            method, f"{BASE}{path}", json=payload,
            auth=(self.username, self.password),
            timeout=config.VERIFALIA_TIMEOUT,
        )
        if resp.status_code >= 300:
            raise VerifaliaError(
                f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError:
            return {}

    def submit(self, addresses: list[str]) -> dict:
        """Start a validation job. Returns the raw response."""
        return self._request("POST", "/email-validations", {
            "entries": [{"inputData": a} for a in addresses],
        })

    def collect(self, job_id: str) -> dict:
        return self._request("GET", f"/email-validations/{job_id}")

    @staticmethod
    def _entries(payload: dict) -> list:
        """Pull the per-address entries out, whatever the envelope looks like.

        Verifalia nests them under "entries", which is itself either a list or
        an object with "data". Reading both costs nothing and means a shape
        change does not silently return zero results -- which would read as
        "nothing was deliverable" and quietly create no contacts.
        """
        entries = payload.get("entries")
        if isinstance(entries, dict):
            entries = entries.get("data")
        return entries if isinstance(entries, list) else []

    @staticmethod
    def _done(payload: dict) -> bool:
        overview = payload.get("overview") or {}
        return (overview.get("status") or "").lower() == "completed"

    def verify(self, addresses: list[str]) -> dict:
        """Address -> classification. Anything unresolved is left out.

        Absent from the result means "no verdict", which the caller treats as
        not deliverable. That is deliberate: a partial result must never be
        read as a set of failures, only as a set of unknowns.
        """
        if not addresses:
            return {}
        payload = self.submit(addresses)
        job = (payload.get("overview") or {}).get("id", "")
        deadline = time.time() + config.VERIFALIA_POLL_SECONDS
        while not self._done(payload):
            if time.time() > deadline:
                log.warning("verifalia: job %s still running after %ds; "
                            "leaving these addresses for the next run",
                            job, config.VERIFALIA_POLL_SECONDS)
                return {}
            time.sleep(config.VERIFALIA_POLL_INTERVAL)
            payload = self.collect(job)

        out = {}
        for entry in self._entries(payload):
            addr = (entry.get("inputData") or "").strip().lower()
            classification = entry.get("classification") or ""
            if addr and classification:
                out[addr] = classification
        if not out:
            log.warning("verifalia: job %s completed but yielded no entries; "
                        "treating every address as unverified", job)
        return out


def _say(line: str) -> None:
    """Print, and also write to the Actions run summary.

    Actions collapses step logs, so a diagnostic nobody expands is a
    diagnostic nobody reads -- the same lesson as the GHL setup report.
    """
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def probe(addresses: list[str]) -> int:
    """Submit addresses and print the raw response, creating nothing.

    The point is to read the real shape before trusting a parser against it,
    which is why the raw JSON is printed alongside the parsed result: if the
    two disagree, that is visible here rather than as an empty merge tag in a
    live email weeks later.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = Verifalia()
    if not client.configured():
        log.error("need VERIFALIA_USERNAME and VERIFALIA_PASSWORD")
        return 1

    _say("## Verifalia probe\n")
    _say("submitting: " + ", ".join(addresses) + "\n")
    _say("```")
    try:
        payload = client.submit(addresses)
    except VerifaliaError as exc:
        _say(f"SUBMIT FAILED: {exc}")
        _say("```")
        return 1
    _say("=== submit response ===")
    _say(json.dumps(payload, indent=2)[:3000])

    job = (payload.get("overview") or {}).get("id", "")
    deadline = time.time() + config.VERIFALIA_POLL_SECONDS
    while job and not Verifalia._done(payload) and time.time() < deadline:
        time.sleep(config.VERIFALIA_POLL_INTERVAL)
        payload = client.collect(job)
    _say("")
    _say("=== after polling ===")
    _say(json.dumps(payload, indent=2)[:6000])

    _say("")
    _say("=== what the parser makes of it ===")
    parsed = {}
    for entry in Verifalia._entries(payload):
        addr = (entry.get("inputData") or "").strip().lower()
        parsed[addr] = entry.get("classification") or "(no classification)"
    _say(json.dumps(parsed, indent=2) if parsed
         else "NOTHING PARSED -- _entries() does not match this envelope")
    _say("")
    _say(f"job complete: {Verifalia._done(payload)}")
    _say("```")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="EMAILS", required=True,
                    help="comma-separated addresses")
    args = ap.parse_args()
    sys.exit(probe([a.strip() for a in args.probe.split(",") if a.strip()]))
