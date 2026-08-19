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
import re
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

# The pipeline built by hand in GHL. Matching the name here means the script
# adopts it rather than creating a second one beside it. Override with
# GHL_PIPELINE_NAME, or pin it exactly with the GHL_PIPELINE_ID secret.
PIPELINE_NAME = "Mass Ingestion"

# Only used if the pipeline has to be created from scratch; an existing one is
# left exactly as it is. First stage matches the one already in GHL.
STAGES = [
    "Newly Ingested / no response",   # scraped and emailed, nothing back yet
    "Engaged",                        # opened, clicked, or replied
    "In Conversation",                # a real thread with a human
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


def create_pipeline(client: GHLClient, name: str = PIPELINE_NAME) -> dict:
    payload = {
        "name": name,
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


def _tail(value: str) -> str:
    """The part of a fieldKey that identifies it: 'contact.next_show' -> 'next_show'."""
    return (value or "").split(".")[-1]


def _tokens(value: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", _tail(value).lower()) if t}


# What each field would plausibly be called if someone built it by hand. Token
# overlap alone is not enough: "What's the next play you are doing" shares only
# the word "next" with next_show_title, and would otherwise be reported missing
# right next to the field that already holds exactly that.
ALIASES = {
    "next_show_title": ("next play", "next show", "upcoming play",
                        "upcoming show", "current show", "next production"),
    "next_show_start": ("next performance date", "show date", "next show date",
                        "opening night", "performance date", "show start"),
    "next_show_end": ("closing night", "show end", "last performance"),
    "next_show_venue": ("venue", "theater", "theatre", "performance space"),
    "next_show_city": ("city", "show city"),
    "sample_playbill_url": ("playbill sample", "sample playbill",
                            "playbill link", "sample link"),
    "licensor": ("licensor", "licensing house", "rights holder"),
}


def looks_similar(wanted: str, field: dict) -> bool:
    """Whether an existing field is plausibly the one we were about to create.

    Advisory only -- never used to adopt automatically. A field whose name is
    close but whose meaning is not (say a "Show Date" that holds the date of a
    *past* show) would silently mis-fill a merge tag, and a wrong date in a
    reminder sequence is worse than no date at all. So this prints candidates
    for a human to judge; adoption is an explicit entry in GHL_FIELD_IDS.
    """
    have = _tokens(field.get("fieldKey") or "") | _tokens(field.get("name") or "")
    if not have:
        return False
    for phrase in (wanted,) + ALIASES.get(wanted, ()):
        want = _tokens(phrase.replace(" ", "_"))
        if not want:
            continue
        if len(want & have) >= 2 or want <= have or have <= want:
            return True
    return False


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

    # Show every pipeline with its ids, whatever it is called. If the pipeline
    # was built by hand in the UI, this is how you get its ids without copying
    # anything out of the browser -- and without guessing our own name for it.
    if pipelines:
        say("found %d existing pipeline(s):", len(pipelines))
        for pl in pipelines:
            say("")
            say("  %r", pl.get("name", "?"))
            say("    GHL_PIPELINE_ID       = %s", pl.get("id", "?"))
            for i, st in enumerate(pl.get("stages") or []):
                marker = "  <- GHL_PIPELINE_STAGE_ID" if i == 0 else ""
                say("    stage %-20s %s%s",
                    st.get("name", "?"), st.get("id", "?"), marker)
        say("")
    else:
        say("no pipelines exist in this location yet")

    # Match on the configured id first, then on our own default name. If you
    # built the pipeline by hand, set GHL_PIPELINE_ID and this will adopt it
    # rather than creating a second one alongside it.
    wanted_name = (os.environ.get("GHL_PIPELINE_NAME") or PIPELINE_NAME).strip()
    existing = None
    if config.GHL_PIPELINE_ID:
        existing = next(
            (p for p in pipelines if p.get("id") == config.GHL_PIPELINE_ID), None
        )
        if existing:
            say("adopting the pipeline named by GHL_PIPELINE_ID")
        else:
            say("WARNING: GHL_PIPELINE_ID is set but no pipeline has that id")
    if existing is None:
        existing = next(
            (p for p in pipelines
             if (p.get("name") or "").strip().lower() == wanted_name.lower()),
            None,
        )
    if existing:
        say("using existing pipeline %r -- nothing to create",
            existing.get("name", wanted_name))
    elif args.apply:
        existing = create_pipeline(client, wanted_name)
        say("created pipeline %r", wanted_name)
    else:
        say("would create pipeline %r with stages: %s",
            wanted_name, ", ".join(STAGES))

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

    # Print the whole inventory, not just what we came looking for. The wanted
    # list below matches on the exact key, so a field you already have under
    # another name ("Next Show", "Show Date") reads as missing and would get a
    # near-duplicate created beside it. Seeing everything is what prevents that.
    if fields:
        say("%d contact field(s) already exist:", len(fields))
        say("")
        # Nothing is truncated: the key and the id are what you actually paste
        # somewhere, and a key clipped to fit a column is worse than useless.
        for f in sorted(fields, key=lambda x: (x.get("name") or "").lower()):
            say("  %s", f.get("name") or "?")
            say("      key=%s  type=%s  id=%s",
                f.get("fieldKey") or "?", f.get("dataType") or "?",
                f.get("id") or "?")
        say("")
    else:
        say("no contact custom fields exist in this location yet")
        say("")

    by_key = {}
    by_id = {}
    for f in fields:
        by_key.setdefault(_tail(f.get("fieldKey") or f.get("name") or "").lower(), f)
        if f.get("id"):
            by_id[f["id"]] = f

    resolved: dict[str, dict] = {}
    say("wanted:")
    for name, data_type in FIELDS:
        adopted = config.GHL_FIELD_IDS.get(name)
        if adopted:
            field = by_id.get(adopted)
            say("  adopted %-22s -> %s (%s)", name, adopted,
                (field or {}).get("name", "NOT FOUND IN THIS LOCATION"))
            if field:
                resolved[name] = field
            continue
        if name.lower() in by_key:
            resolved[name] = by_key[name.lower()]
            say("  ok      %-22s id=%s", name, by_key[name.lower()].get("id", "?"))
            continue
        near = [f for f in fields if looks_similar(name, f)]
        if near:
            say("  similar %-22s (%s) -- already have:", name, data_type)
            for f in near:
                say("            %r  key=%s  type=%s",
                    f.get("name", "?"), f.get("fieldKey", "?"),
                    f.get("dataType", "?"))
            say("            map it in GHL_FIELD_IDS to adopt it, or apply to")
            say("            create a separate field beside it")
        if args.apply:
            try:
                made = create_custom_field(client, name, data_type)
                resolved[name] = made
                say("  created %-22s (%s) id=%s", name, data_type,
                    made.get("id", "?"))
            except GHLError as exc:
                log.error("  FAILED  %-22s %s", name, exc)
        elif not near:
            say("  missing %-22s (%s)", name, data_type)

    # The two dates are the one thing that is painful to fix later: a reminder
    # sequence cannot be scheduled off a text box or a dropdown, and changing a
    # field's type means recreating it and re-syncing every contact.
    for name, data_type in FIELDS:
        field = resolved.get(name)
        if field and data_type == "DATE" and field.get("dataType") != "DATE":
            say("  WARNING %-22s resolves to a %s field, not DATE -- reminders",
                name, field.get("dataType"))
            say("          cannot be scheduled off it")

    say("")
    if resolved:
        say("Paste into config.GHL_FIELD_IDS:")
        say("")
        say("GHL_FIELD_IDS = {")
        for name, _ in FIELDS:
            field = resolved.get(name)
            if field:
                say('    %-24s %r,   # %s', f'"{name}":', field.get("id", ""),
                    field.get("name", ""))
            else:
                say('    # %-22s still missing', name)
        say("}")

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
