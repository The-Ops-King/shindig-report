"""Persisted state: which productions we have already reported, and the
organization contact cache.

Both files are committed back to the repo by the workflow, which is what makes
"new today" meaningful across runs and what keeps enrichment from re-fetching
the same organizations every morning.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import config

log = logging.getLogger(__name__)


def _load(path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s (%s); starting empty", path, exc)
        return default


def _save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=0, sort_keys=True)
        fh.write("\n")


# --- seen productions ------------------------------------------------------

def load_seen() -> dict:
    """key -> first_seen ISO date."""
    return _load(config.SEEN_PATH, {})


def save_seen(seen: dict) -> None:
    _save(config.SEEN_PATH, seen)


def diff_new(productions, seen: dict, today: date) -> list:
    """Return productions never seen before, and stamp first/last seen.

    Correctness here is what the whole daily email rests on: if keys are
    unstable this returns everything, every day.
    """
    iso = today.isoformat()
    fresh = []
    for p in productions:
        prior = seen.get(p.key)
        if prior is None:
            seen[p.key] = iso
            p.first_seen = iso
            fresh.append(p)
        else:
            p.first_seen = prior
        p.last_seen = iso
    return fresh


# --- organization contact cache -------------------------------------------

def load_org_cache() -> dict:
    return _load(config.ORG_CACHE_PATH, {})


def save_org_cache(cache: dict) -> None:
    _save(config.ORG_CACHE_PATH, cache)


def _ttl_days(status: str) -> int:
    if status == "found":
        return config.ENRICH_SUCCESS_TTL_DAYS
    if status == "error":
        return config.ENRICH_ERROR_TTL_DAYS
    return config.ENRICH_NOT_FOUND_TTL_DAYS


def is_fresh(entry: dict, today: date) -> bool:
    """A cached result is reusable until its status-specific TTL expires.

    Misses live for 90 days on purpose: a dead end costs three requests per
    quarter, not three per day.
    """
    checked = entry.get("checked")
    if not checked:
        return False
    try:
        when = datetime.strptime(checked, "%Y-%m-%d").date()
    except ValueError:
        return False
    return today - when < timedelta(days=_ttl_days(entry.get("status", "")))
