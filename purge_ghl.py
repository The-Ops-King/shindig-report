"""Delete GHL contacts whose address is not on the verified list.

Runs inside the GitHub Action so it uses the GHL_API_KEY secret directly.

Deleting a contact in GHL is irreversible, so the order here is not negotiable:
back up every contact first, and only then delete. A backup that fails or comes
back short aborts the run before a single DELETE is issued.

    python purge_ghl.py                 # report + write the backup, delete nothing
    python purge_ghl.py --apply         # actually delete
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import date

import config
import emails
import state as state_mod
from ghl import GHLClient, GHLError

log = logging.getLogger("purge")

_SUMMARY: list[str] = []


def say(fmt: str, *args) -> None:
    line = fmt % args if args else fmt
    log.info(line)
    _SUMMARY.append(line)


def flush_summary() -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("## GHL purge\n\n```\n" + "\n".join(_SUMMARY) + "\n```\n")


def verified_addresses(path: str) -> set:
    """Every address on the verified list, normalized the same way as ours."""
    keep = set()
    with open(path, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            for key in ("Email", "email", "EmailAddress", "Address"):
                if row.get(key):
                    addr = emails.normalize(row[key])
                    if "@" in addr:
                        keep.add(addr)
                    break
    return keep


def plan(keep: set) -> tuple[dict, dict]:
    """Split the ledger into contacts to delete and contacts to keep.

    Keyed by contact id rather than by organization: 2,958 organizations share
    2,368 inboxes, so one contact can back several ledger records and deleting
    it must retire all of them.
    """
    cache = state_mod.load_org_cache()
    ledger = state_mod.load_opportunities()
    doomed, kept = {}, {}
    for org_key, record in ledger.items():
        cid = record.get("contact_id")
        if not cid:
            continue
        addr = emails.normalize(cache.get(org_key, {}).get("email") or "")
        bucket = kept if addr in keep else doomed
        entry = bucket.setdefault(cid, {"address": addr, "orgs": []})
        entry["orgs"].append(org_key)
    return doomed, kept


def back_up(client: GHLClient, contacts: dict, today: date) -> str:
    """Fetch and save every contact about to be deleted, before deleting any.

    Restoring means replaying a file that is in git, rather than re-deriving
    thousands of contacts from a scrape that no longer knows their GHL ids.
    """
    path = config.STATE_DIR / f"ghl_backup_{today.isoformat()}.json"
    saved, failed = {}, []
    for i, cid in enumerate(contacts, 1):
        try:
            data = client._request("GET", f"/contacts/{cid}", retries=2)
            saved[cid] = data.get("contact") or data
        except GHLError as exc:
            failed.append((cid, str(exc)[:120]))
        if i % 200 == 0:
            log.info("backed up %d/%d", i, len(contacts))
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"taken": today.isoformat(), "contacts": saved,
                   "unreadable": failed}, fh, indent=0, sort_keys=True)
        fh.write("\n")
    say("backup: %d saved, %d unreadable -> %s", len(saved), len(failed),
        path.name)
    for cid, err in failed[:5]:
        say("  could not read %s: %s", cid, err)
    return path.name if not failed else ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (otherwise report and back up only)")
    ap.add_argument("--csv", default="data/verified_emails.csv")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    today = date.today()
    client = GHLClient()
    ok, missing = client.configured()
    if not ok:
        log.error("need %s", missing)
        return 1

    keep_addrs = verified_addresses(args.csv)
    doomed, kept = plan(keep_addrs)
    say("verified list        : %d addresses", len(keep_addrs))
    say("contacts to KEEP     : %d", len(kept))
    say("contacts to DELETE   : %d", len(doomed))
    say("")
    say("first 20 to be deleted:")
    for cid, info in list(doomed.items())[:20]:
        say("  %-24s %s", info["address"] or "(no address)", cid)
    say("")
    say("first 20 kept, as a sanity check:")
    for cid, info in list(kept.items())[:20]:
        say("  %-24s %s", info["address"], cid)
    say("")

    if not doomed:
        say("nothing to do")
        flush_summary()
        return 0

    # Back up on the dry run too: the whole point is to have it in hand and
    # reviewed *before* anyone approves the delete.
    name = back_up(client, doomed, today)
    if not name:
        say("")
        say("ABORT: the backup is incomplete, so nothing will be deleted.")
        say("Deleting without a full backup is not recoverable. Re-run.")
        flush_summary()
        return 1

    if not args.apply:
        say("")
        say("DRY RUN -- nothing deleted. Backup written. Re-run with apply.")
        flush_summary()
        return 0

    deleted, failures = 0, []
    for i, cid in enumerate(doomed, 1):
        try:
            client._request("DELETE", f"/contacts/{cid}", retries=2)
            deleted += 1
        except GHLError as exc:                 # one failure must not stop 770
            failures.append((cid, str(exc)[:120]))
        if i % 200 == 0:
            log.info("deleted %d/%d", i, len(doomed))

    # Retire the ledger records and suppress the addresses. Without the
    # suppression list the next morning's ingest re-creates every one of them.
    ledger = state_mod.load_opportunities()
    for cid, info in doomed.items():
        for org_key in info["orgs"]:
            ledger.pop(org_key, None)
    state_mod.save_opportunities(ledger)

    existing = set(emails.suppressed())
    existing.update(info["address"] for info in doomed.values() if info["address"])
    with open(config.SUPPRESSED_PATH, "w", encoding="utf-8") as fh:
        json.dump(sorted(existing), fh, indent=0)
        fh.write("\n")

    say("")
    say("deleted %d contacts, %d failures", deleted, len(failures))
    for cid, err in failures[:10]:
        say("  FAILED %s: %s", cid, err)
    say("suppressed %d addresses; ledger now holds %d organizations",
        len(existing), len(ledger))
    flush_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
