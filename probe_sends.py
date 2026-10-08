"""Find out how GoHighLevel reports a sent email, by asking it.

Brian's digest should say how many emails actually went out. That is a GHL
fact the pipeline has never seen: this code applies the mass-marketing tag,
and GHL sends on its own schedule from there, so a contact tagged today may
get their first email six months from now. Nothing local can infer it.

Which endpoint reports a send, and in what shape, is not something to guess
at. This codebase has been caught three times doing that -- a fieldKey that
matched nothing and was silently accepted, a field that looked missing but was
in another folder, and a Verifalia envelope the parser had to be checked
against. So, as with `verifalia.py --probe`: hit the candidates, print exactly
what comes back, write the counter afterwards.

Read-only by construction. Every request is a GET or a search; nothing is
created, updated or deleted, and no state file is touched. A 404 is a useful
answer, not a failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import config
import ghl

log = logging.getLogger(__name__)

# How much of each response to show. Enough to see the shape of a record and
# the names of its fields; not so much that the summary is unreadable.
SAMPLE = 2


def say(line: str = "") -> None:
    """Print, and also write to the Actions step summary when there is one.

    A probe whose output is buried in a collapsed log is a diagnostic nobody
    reads -- the same lesson as the GHL setup report.
    """
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _shrink(data, keep=SAMPLE):
    """Trim long lists so the shape shows without a whole page of records."""
    if isinstance(data, dict):
        return {k: _shrink(v, keep) for k, v in data.items()}
    if isinstance(data, list):
        out = [_shrink(v, keep) for v in data[:keep]]
        if len(data) > keep:
            out.append(f"... {len(data) - keep} more")
        return out
    return data


def _block(label: str, value) -> None:
    say(f"### {label}")
    say("```json")
    say(json.dumps(_shrink(value), indent=2, default=str)[:4000])
    say("```")
    say()


def _try(client, label: str, method: str, path: str,
         payload: dict | None = None, params: dict | None = None):
    """Make one request and report it, whatever happens.

    Returns the parsed body, or None. An error is printed rather than raised:
    the point of a probe is to learn which endpoints exist, so a 404 or a 422
    is a result worth recording, not a reason to stop.
    """
    say(f"## {label}")
    say(f"`{method} {path}`"
        + (f" params=`{params}`" if params else "")
        + (f" body=`{json.dumps(payload)}`" if payload else ""))
    say()
    try:
        data = client._request(method, path, payload, retries=1, params=params)
    except Exception as exc:                        # noqa: BLE001 -- see above
        say(f"**no good:** `{str(exc)[:500]}`")
        say()
        return None
    _block("response", data)
    return data


def probe() -> int:
    client = ghl.GHLClient()
    ok, missing = client.configured()
    if not ok:
        say(f"**{missing} not set** -- nothing to probe against.")
        return 1

    loc = config.GHL_LOCATION_ID
    say("# GHL send-reporting probe")
    say()
    say("Read-only. Looking for an endpoint that reports emails GHL has sent, "
        "with a timestamp, so the daily digest can count the last 24 hours.")
    say()

    # 1. Conversations. The most likely home for a sent email: GHL files
    #    outbound mail under a conversation with the contact.
    convos = _try(
        client, "Conversations search", "POST", "/conversations/search",
        payload={"locationId": loc, "limit": SAMPLE},
    )
    if convos is None:
        # The same search is documented as a GET in places. Worth one try
        # before concluding the endpoint is unavailable.
        convos = _try(
            client, "Conversations search (GET)", "GET", "/conversations/search",
            params={"locationId": loc, "limit": SAMPLE},
        )

    # 2. Messages inside one conversation -- where a per-send record, with a
    #    type and a date, would have to live if it exists anywhere.
    conv_id = ""
    if isinstance(convos, dict):
        for key in ("conversations", "data", "items"):
            rows = convos.get(key)
            if isinstance(rows, list) and rows:
                conv_id = (rows[0] or {}).get("id") or ""
                break
    if conv_id:
        _try(client, f"Messages in conversation {conv_id}", "GET",
             f"/conversations/{conv_id}/messages", params={"limit": 5})
    else:
        say("## Messages in a conversation")
        say("Skipped -- the search returned no conversation id to follow.")
        say()

    # 3. Anything that reports on email directly. Both of these may well 404;
    #    that is worth knowing in one run rather than three.
    _try(client, "Email statistics", "GET", "/emails/statistics",
         params={"locationId": loc})
    _try(client, "Scheduled emails", "GET", "/emails/schedule",
         params={"locationId": loc, "limit": SAMPLE})

    say("---")
    say("**What to read this for:** whether any response above contains a "
        "sent email with a timestamp; whether it can be filtered or sorted by "
        "date, or whether counting a day means paging everything; and whether "
        "a workflow send can be told apart from one a human sent from the "
        "GHL inbox.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argparse.ArgumentParser(
        description="Print how GHL reports sent emails. Changes nothing."
    ).parse_args()
    sys.exit(probe())
