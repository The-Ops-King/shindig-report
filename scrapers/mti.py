"""MTI (Music Theatre International).

The public /externalmap/{node_id} page iframes /productions-map.php, whose JS
calls map-search-ajax.php. That endpoint honours an arbitrary `limit`, so the
entire worldwide catalogue arrives in a single request -- no map iteration and
no per-show crawl. Observed: 17,434 records spanning 2018-05-18 to 2029-01-18.

Note: the endpoint accepts a `country` parameter but silently ignores it
(US and CA both return the full 17,434), so scope filtering happens
client-side against the parsed address.
"""

from __future__ import annotations

import logging

import config
from normalize import Production, parse_iso, parse_mti_address
from scrapers.base import Fetcher, ScrapeError

log = logging.getLogger(__name__)

# The endpoint requires a bounding box; this one covers the planet. Scope is
# narrowed later so a single request can serve any future scope change.
WORLD = {
    "bounds_north": 85,
    "bounds_south": -85,
    "bounds_east": 180,
    "bounds_west": -180,
}


def fetch_raw(fetcher: Fetcher | None = None) -> list[dict]:
    fetcher = fetcher or Fetcher()
    params = dict(WORLD)
    params["limit"] = config.MTI_LIMIT
    params["include_jr_shows"] = 1
    resp = fetcher.get(config.MTI_ENDPOINT, params=params)
    payload = resp.json()
    if not payload.get("success"):
        raise ScrapeError(f"MTI returned success=false: {str(payload)[:200]}")
    return payload.get("data") or []


def parse(rows: list[dict]) -> list[Production]:
    out = []
    for r in rows:
        addr = parse_mti_address(r.get("address", ""))
        # location_text ("Raleigh, NC") is the fallback when the address blob
        # has no parseable "City, ST" line.
        if not addr["city"] and r.get("location_text"):
            bits = r["location_text"].rsplit(",", 1)
            addr["city"] = bits[0].strip()
            if len(bits) == 2:
                from normalize import state_code
                addr["state"] = state_code(bits[1].strip())

        org = (r.get("organization") or r.get("venue") or "").strip()
        if not org:
            continue

        out.append(Production(
            key=f"mti:{r['id']}",
            source="mti",
            show_title=(r.get("show_title") or r.get("title") or "").strip(),
            organization=org,
            venue=(r.get("venue") or "").strip(),
            venue_type=(r.get("venue_type") or "").strip(),
            street=addr["street"],
            city=addr["city"],
            state=addr["state"],
            postal=addr["postal"],
            country=addr["country"],
            lat=_float(r.get("lat")),
            lng=_float(r.get("lng")),
            start_date=parse_iso(r.get("start", "")),
            end_date=parse_iso(r.get("end", "")),
            source_url="https://www.mtishows.com/shows",
            org_website=(r.get("venue_url") or "").strip(),
        ))
    return out


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def scrape(fetcher: Fetcher | None = None) -> list[Production]:
    rows = fetch_raw(fetcher)
    log.info("MTI: %d raw records", len(rows))
    prods = parse(rows)
    if len(prods) < config.MIN_EXPECTED["mti"]:
        raise ScrapeError(
            f"MTI returned only {len(prods)} productions "
            f"(expected >= {config.MIN_EXPECTED['mti']}); endpoint likely changed"
        )
    return prods
