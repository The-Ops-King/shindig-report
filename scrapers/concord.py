"""Concord Theatricals (incl. Samuel French, Dramatists, Playscripts, Stage Rights).

concordtheatricals.com/now-playing iframes the legacy ASP.NET storefront at
shop.concordtheatricals.com, whose NowPlayingMarkersSource endpoint returns map
markers for a bounding box at a given zoom.

Clustering is purely a function of the `zoom` parameter, not of how large the
box is: the same North America box returns 95 clusters at zoom 12 and zero at
zoom 16. So the whole continent arrives in a single request at zoom 16 --
~9,400 markers in under three seconds. The adaptive descent below exists only
as a fallback in case Concord ever changes that behaviour.

Identity is the subtle part. Neither ID field the payload offers can be used:

  * `I`  is the *venue* id, not the production id. The Ashby Stage carries one
         `I` across ten different shows; keying on it silently collapses 9,408
         productions into 6,241 and loses real leads.
  * `Uid` is a per-response sequence number. Verified unstable: two identical
         requests seconds apart share zero Uids, so keying on it would report
         every Concord production as new, every morning, forever.

So the key is a content hash over the fields that actually identify a run.
"""

from __future__ import annotations

import json
import logging
import re

import config
from normalize import (
    Production, content_key, country_for_state, parse_dmy, state_code,
)
from scrapers.base import Fetcher, ScrapeError

log = logging.getLogger(__name__)


def _query(bbox: dict, zoom: int, sendid: int) -> dict:
    return {
        "neLat": bbox["neLat"], "neLon": bbox["neLon"],
        "swLat": bbox["swLat"], "swLon": bbox["swLon"],
        "zoom": zoom,
        "productId": 0, "authorAgentId": 0,
        "keywords": "", "filter": "", "sendid": str(sendid),
        "startDate": "", "endDate": "", "zipCode": "", "miles": "",
        # Finished runs are dead leads.
        "showHistorical": False,
    }


def _quadrants(bbox: dict) -> list[dict]:
    mid_lat = (bbox["neLat"] + bbox["swLat"]) / 2
    mid_lon = (bbox["neLon"] + bbox["swLon"]) / 2
    return [
        {"neLat": bbox["neLat"], "neLon": mid_lon,
         "swLat": mid_lat, "swLon": bbox["swLon"]},
        {"neLat": bbox["neLat"], "neLon": bbox["neLon"],
         "swLat": mid_lat, "swLon": mid_lon},
        {"neLat": mid_lat, "neLon": mid_lon,
         "swLat": bbox["swLat"], "swLon": bbox["swLon"]},
        {"neLat": mid_lat, "neLon": bbox["neLon"],
         "swLat": bbox["swLat"], "swLon": mid_lon},
    ]


def _contains(bbox: dict, lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return (bbox["swLat"] <= lat <= bbox["neLat"]
            and bbox["swLon"] <= lon <= bbox["neLon"])


def _live_quadrants(bbox: dict, clusters: list[dict]) -> list[dict]:
    """Only quadrants that actually hold an unresolved cluster.

    Cluster markers carry coordinates, so empty quadrants are pruned without
    spending a request. Only reached if Concord starts clustering at max zoom.
    """
    live = [
        q for q in _quadrants(bbox)
        if any(_contains(q, c.get("Lat"), c.get("Lon")) for c in clusters)
    ]
    return live or _quadrants(bbox)


def marker_key(m: dict) -> str:
    """Content hash. See the module docstring for why no native ID works.

    `productid` is the title id, which combined with the producer, venue
    address and the run's dates identifies a production stably across runs.
    """
    return content_key(
        "concord",
        m.get("productid"), m.get("producer"), m.get("address"),
        m.get("city"), m.get("startdate"), m.get("enddate"),
    )


class ConcordSweep:
    def __init__(self, fetcher: Fetcher | None = None):
        self.fetcher = fetcher or Fetcher(
            delay=config.CONCORD_DELAY, referer=config.CONCORD_REFERER
        )
        self.markers: dict[str, dict] = {}
        self.requests = 0
        self.sendid = 0
        self.unresolved = 0

    def _markers_for(self, bbox: dict, zoom: int) -> list[dict]:
        self.sendid += 1
        self.requests += 1
        resp = self.fetcher.post(
            config.CONCORD_MARKERS,
            data={"query": json.dumps(_query(bbox, zoom, self.sendid))},
        )
        payload = resp.json()
        if str(payload.get("Ok")) != "1":
            raise ScrapeError(f"Concord markers refused: {payload.get('EMsg')!r}")
        return payload.get("Markers") or []

    def sweep(self, bbox: dict = None, zoom: int = None) -> None:
        bbox = bbox or config.CONCORD_BBOX
        zoom = zoom if zoom is not None else config.CONCORD_ZOOM

        markers = self._markers_for(bbox, zoom)
        if not markers:
            return

        clusters = []
        for m in markers:
            if m.get("Type") == 1 and m.get("name"):
                # Edge-overlapping boxes re-report markers; the dict makes that
                # idempotent instead of duplicating leads.
                self.markers.setdefault(marker_key(m), m)
            else:
                clusters.append(m)

        if not clusters:
            return

        if zoom >= config.CONCORD_MAX_ZOOM:
            stuck = sum(c.get("C", 0) or 0 for c in clusters)
            self.unresolved += stuck
            log.warning(
                "Concord: %d markers unresolved at max zoom in %s", stuck, bbox
            )
            return

        log.info("Concord: %d clusters at zoom %d; descending", len(clusters), zoom)
        for quad in _live_quadrants(bbox, clusters):
            self.sweep(quad, zoom + config.CONCORD_ZOOM_STEP)


def to_productions(markers: dict[str, dict]) -> list[Production]:
    out = []
    for key, m in markers.items():
        org = (m.get("producer") or "").strip()
        if not org:
            continue
        st = state_code((m.get("state") or "").strip())
        out.append(Production(
            key=key,
            source="concord",
            show_title=(m.get("name") or "").strip(),
            organization=org,
            venue=org,
            venue_type=(m.get("performancetype") or "").strip(),
            street=(m.get("address") or "").strip(),
            city=(m.get("city") or "").strip(),
            state=st,
            country=country_for_state(st),
            lat=_float(m.get("Lat")),
            lng=_float(m.get("Lon")),
            start_date=parse_dmy(m.get("startdate", "")),
            end_date=parse_dmy(m.get("enddate", "")),
            source_url=_product_url(m),
            extra={"authors": (m.get("authors") or "").strip()},
        ))
    return out


def _product_url(m: dict) -> str:
    pid = m.get("productid")
    if not pid:
        return "https://www.concordtheatricals.com/now-playing"
    slug = re.sub(r"[^a-z0-9]+", "-", (m.get("name") or "").lower()).strip("-")
    return f"https://www.concordtheatricals.com/p/{pid}/{slug}"


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def scrape(fetcher: Fetcher | None = None) -> list[Production]:
    sweep = ConcordSweep(fetcher)
    sweep.sweep()
    prods = to_productions(sweep.markers)
    log.info(
        "Concord: %d productions from %d request(s), %d unresolved",
        len(prods), sweep.requests, sweep.unresolved,
    )
    if len(prods) < config.MIN_EXPECTED["concord"]:
        raise ScrapeError(
            f"Concord returned only {len(prods)} productions "
            f"(expected >= {config.MIN_EXPECTED['concord']}); endpoint likely changed"
        )
    return prods
