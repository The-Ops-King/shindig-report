"""Resolve state/province and country from coordinates, offline.

Concord's markers endpoint always returns city, street address and lat/lng but
*never* populates `state` (verified: 0 of 9,408 markers). Scope filtering and
the Sheet both need it.

Rather than take on a reverse-geocoding dependency (reverse_geocoder pulls in
scipy and fails to build cleanly) or an API key, this reuses data the pipeline
already downloads: MTI publishes ~17k venues with city, state, country *and*
coordinates, and MTI's venues are drawn from the same community-theatre
ecosystem as Concord's, so the overlap is high.

Resolution order, cheapest first:
  1. exact city-name match against the MTI reference, disambiguated by
     distance when a city name repeats across states (Springfield, Portland)
  2. nearest reference venue within a bucketed grid search
  3. a coarse lat/lng fallback that can at least separate US from CA
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict

log = logging.getLogger(__name__)

_NORM = re.compile(r"[^a-z0-9]+")
# Nearest-neighbour matches further out than this are not trustworthy enough
# to assign a state.
MAX_NEIGHBOUR_KM = 60.0


def _norm_city(city: str) -> str:
    return _NORM.sub(" ", (city or "").lower()).strip()


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


class GeoResolver:
    """Nearest-neighbour lookup over a set of already-geocoded venues."""

    def __init__(self):
        self.by_city: dict[str, list] = defaultdict(list)
        self.grid: dict[tuple, list] = defaultdict(list)
        self.size = 0

    def add_reference(self, productions) -> None:
        """Index every production that already carries coordinates and a state."""
        for p in productions:
            if p.lat is None or p.lng is None or not p.state:
                continue
            point = (p.lat, p.lng, p.state, p.country)
            self.by_city[_norm_city(p.city)].append(point)
            self.grid[(round(p.lat), round(p.lng))].append(point)
            self.size += 1
        log.info("geo: indexed %d reference venues", self.size)

    def _nearest(self, lat, lng, points):
        best, best_d = None, float("inf")
        for plat, plng, state, country in points:
            d = _haversine_km(lat, lng, plat, plng)
            if d < best_d:
                best, best_d = (state, country), d
        return best, best_d

    def resolve(self, lat, lng, city: str) -> tuple[str, str, str]:
        """Return (state, country, method)."""
        key = _norm_city(city)
        candidates = self.by_city.get(key)

        if candidates:
            states = {c[2] for c in candidates}
            if len(states) == 1:
                only = candidates[0]
                return only[2], only[3], "city"
            if lat is not None and lng is not None:
                hit, dist = self._nearest(lat, lng, candidates)
                if hit and dist <= MAX_NEIGHBOUR_KM:
                    return hit[0], hit[1], "city+distance"

        if lat is not None and lng is not None:
            # Widen the grid search one ring at a time.
            for radius in (0, 1, 2):
                points = []
                for dlat in range(-radius, radius + 1):
                    for dlng in range(-radius, radius + 1):
                        if radius and max(abs(dlat), abs(dlng)) != radius:
                            continue  # only the new ring
                        points.extend(
                            self.grid.get((round(lat) + dlat, round(lng) + dlng), [])
                        )
                if points:
                    hit, dist = self._nearest(lat, lng, points)
                    if hit and dist <= MAX_NEIGHBOUR_KM:
                        return hit[0], hit[1], "nearest"

        country = coarse_country(lat, lng)
        return "", country, "coarse" if country else "unresolved"


def coarse_country(lat, lng) -> str:
    """Last-resort US/CA/MX separation from coordinates alone.

    Only reached when no reference venue sits within 60km, so it decides
    country for scope filtering and leaves state blank rather than guessing.
    """
    if lat is None or lng is None:
        return ""
    # Alaska.
    if lat >= 51 and lng <= -130:
        return "US"
    # Canada sits above the 49th parallel in the west; in the east the border
    # dips, so treat the Great Lakes longitudes conservatively.
    if lat >= 49.5:
        return "CA"
    if lat >= 45 and lng >= -95:
        return ""  # ambiguous NE border region -- do not guess
    if 24 <= lat < 49.5:
        return "US"
    return ""


def apply(resolver: GeoResolver, productions) -> dict:
    """Fill in missing state/country in place. Returns method counts."""
    stats = defaultdict(int)
    for p in productions:
        if p.state and p.country:
            stats["already"] += 1
            continue
        state, country, method = resolver.resolve(p.lat, p.lng, p.city)
        if not p.state and state:
            p.state = state
        if not p.country and country:
            p.country = country
        stats[method] += 1
    return dict(stats)
