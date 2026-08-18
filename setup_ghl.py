"""One-shot GHL setup: create the pipeline and the custom fields.

Runs inside the GitHub Action so it uses the GHL_API_KEY secret directly --
the key never has to be pasted anywhere or shared.

Idempotent by design: it reads what already exists and creates only what is
missing, so running it twice is harmless. At the end it prints the ids you need
to paste back as GHL_PIPELINE_ID and GHL_PIPELINE_STAGE_ID.

    python setup_ghl.py            # show what exists and what is missing
    python setup_ghl.py --apply    # actually create it
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import config
from ghl import GHLClient, GHLError

log = logging.getLogger("setup")

_SUMMARY: list[str] = []


def say(fmt: str, *args) -> None:
    """Log, and also collect for the run summary.

    Actions collapses step logs by default, so a report nobody expands is a
    report nobody reads. Everything here is also written to the run summary
    page, where it shows without clicking into anything.
    """
    line = fmt % args if args else fmt
    log.info(line)
    _SUMMARY.append(line)


def flush_summary() -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("## GHL setup\n\n```\n" + "\n".join(_SUMMARY) + "\n```\n")

PIPELINE_NAME = "Playbill Outreach"

# Mirrors the life of one lead, so a card's position tells you where it is.
STAGES = [
    "Show Detected",      # scraped, not yet contacted
    "Sample Sent",        # the intro email went out
    "Engaged",            # opened, clicked, or replied
    "In Conversation",    # a real thread with a human
    "Won",
    "Lost",
]

# GHL dataType per field. Dates must be DATE, or the workflow cannot schedule
# reminders off next_show_start.
FIELDS = [
    ("next_show_title", "TEXT"),
    ("next_show_start", "DATE"),
    ("next_show_end", "DATE"),
    ("next_show_venue", "TEXT"),
    ("next_show_city", "TEXT"),
    ("sample_playbill_url", "TEXT"),
    ("licensor", "TEXT"),
]


def get_pipelines(client: GHLClient) -> list:
    data = client._request(
        "GET", f"/opportunities/pipelines?locationId={client.location_id}"
    )
    return data.get("pipelines") or []


def create_pipeline(client: GHLClient) -> dict:
    payload = {
        "name": PIPELINE_NAME,
        "locationId": client.location_id,
        "stages": [
            {"name": name, "position": i} for i, name in enumerate(STAGES)
        ],
    }
    data = client._request("POST", "/opportunities/pipelines", payload)
    return data.get("pipeline") or data


def get_custom_fields(client: GHLClient) -> list:
    data = client._request(
        "GET", f"/locations/{client.location_id}/customFields"
    )
    return data.get("customFields") or []


def create_custom_field(client: GHLClient, name: str, data_type: str) -> dict:
    data = client._request(
        "POST", f"/locations/{client.location_id}/customFields",
        {"name": name, "dataType": data_type, "model": "contact"},
    )
    return data.get("customField") or data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="create what is missing (otherwise just report)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = GHLClient()
    ok, missing = client.configured()
    if not client.api_key or not client.location_id:
        log.error("need GHL_API_KEY and GHL_LOCATION_ID (missing: %s)", missing)
        return 1

    say("=== pipelines ===")
    try:
        pipelines = get_pipelines(client)
    except GHLError as exc:
        log.error("could not read pipelines: %s", exc)
        return 1

    existing = next(
        (p for p in pipelines
         if (p.get("name") or "").strip().lower() == PIPELINE_NAME.lower()),
        None,
    )
    if existing:
        say("pipeline %r already exists", PIPELINE_NAME)
    elif args.apply:
        existing = create_pipeline(client)
        say("created pipeline %r", PIPELINE_NAME)
    else:
        say("would create pipeline %r with stages: %s",
                 PIPELINE_NAME, ", ".join(STAGES))

    if existing:
        stages = existing.get("stages") or []
        say("")
        say("  GHL_PIPELINE_ID       = %s", existing.get("id", "?"))
        if stages:
            first = stages[0]
            say("  GHL_PIPELINE_STAGE_ID = %s   (%r)",
                     first.get("id", "?"), first.get("name", "?"))
        say("")
        for s in stages:
            say("    stage: %-18s %s", s.get("name"), s.get("id"))

    say("")
    say("=== custom fields ===")
    try:
        fields = get_custom_fields(client)
    except GHLError as exc:
        log.error("could not read custom fields: %s", exc)
        return 1

    have = {(f.get("fieldKey") or f.get("name") or "").split(".")[-1].lower()
            for f in fields}
    for name, data_type in FIELDS:
        if name.lower() in have:
            say("  ok      %s", name)
        elif args.apply:
            try:
                create_custom_field(client, name, data_type)
                say("  created %-22s (%s)", name, data_type)
            except GHLError as exc:
                log.error("  FAILED  %-22s %s", name, exc)
        else:
            say("  missing %-22s (%s)", name, data_type)

    say("")
    if not args.apply:
        say("DRY RUN -- nothing was created. Re-run with apply=true.")
    else:
        say("Done. Copy the two ids above into repo secrets:")
        say("  GHL_PIPELINE_ID, GHL_PIPELINE_STAGE_ID")
    flush_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
