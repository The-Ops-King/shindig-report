"""Theatrical Rights Worldwide.

No API. The WordPress REST endpoint exposes only WooCommerce products -- the
show custom post type is not registered for REST -- so the 381 show URLs come
from the sitemap and each page is fetched and parsed.

Each page may carry an `id="upcoming"` section holding zero or more blocks:

    <div class="upcoming"> ...
      <span class="date-display-start">Aug 06</span> &ndash;
      <span class="date-display-end">23, 2026</span>
      <h5>Davidson Community Players <br/>Cornelius, NC</h5>
    </div>

TRW publishes the least: organization and city/state, no street address and no
website. Duplicate blocks appear on real pages and are deduped here.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

import config
from normalize import (
    Production, content_key, country_for_state, state_code,
)
from scrapers.base import Fetcher, ScrapeError

log = logging.getLogger(__name__)

UPCOMING_RE = re.compile(
    r'date-display-start"\s*>\s*([^<]*?)\s*</span>\s*&ndash;\s*'
    r'<span class="date-display-end"\s*>\s*([^<]*?)\s*</span>\s*'
    r'<h5>\s*(.*?)\s*<br\s*/?>\s*(.*?)\s*</h5>',
    re.S,
)

MONTHS = (
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)
# End dates come in two shapes: "23, 2026" (same month as start) and
# "Mar 14, 2027" (crosses a month boundary).
END_WITH_MONTH = re.compile(rf"^({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})$", re.I)
END_DAY_ONLY = re.compile(r"^(\d{1,2}),\s*(\d{4})$")
START_RE = re.compile(rf"^({MONTHS})\s+(\d{{1,2}})$", re.I)


def _month_num(name: str) -> int | None:
    for fmt in ("%b", "%B"):
        try:
            return datetime.strptime(name.strip()[:len(name.strip())], fmt).month
        except ValueError:
            continue
    try:
        return datetime.strptime(name.strip()[:3], "%b").month
    except ValueError:
        return None


def parse_dates(start_raw: str, end_raw: str) -> tuple[date | None, date | None]:
    """Resolve the pair. The year lives only on the end date."""
    start_raw = (start_raw or "").strip()
    end_raw = (end_raw or "").strip()

    end = None
    end_month = None
    m = END_WITH_MONTH.match(end_raw)
    if m:
        end_month = _month_num(m.group(1))
        year = int(m.group(3))
        if end_month:
            end = _safe_date(year, end_month, int(m.group(2)))
    else:
        m = END_DAY_ONLY.match(end_raw)
        if m:
            year = int(m.group(2))
        else:
            return None, None

    sm = START_RE.match(start_raw)
    if not sm:
        return None, end
    start_month = _month_num(sm.group(1))
    if not start_month:
        return None, end

    # A run that ends in an earlier month than it starts has crossed New Year.
    start_year = year
    if end_month and end_month < start_month:
        start_year = year - 1
    start = _safe_date(start_year, start_month, int(sm.group(2)))

    if end is None:
        # "23, 2026" -- same month as the start.
        end = _safe_date(year, start_month, int(END_DAY_ONLY.match(end_raw).group(1)))
    return start, end


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def show_urls(fetcher: Fetcher) -> list[str]:
    resp = fetcher.get(config.TRW_SITEMAP)
    locs = re.findall(r"<loc>([^<]+)</loc>", resp.text)
    return [u for u in locs if "/show/" in u]


def parse_show_page(html: str, url: str) -> list[Production]:
    idx = html.find('id="upcoming"')
    if idx == -1:
        return []
    # Stop at the Related Shows block so its markup can never be misread as a
    # production listing.
    segment = html[idx:]
    stop = segment.find('id="related"')
    if stop != -1:
        segment = segment[:stop]

    slug = url.rstrip("/").rsplit("/", 1)[-1]
    seen = set()
    out = []
    for start_raw, end_raw, org, location in UPCOMING_RE.findall(segment):
        org = re.sub(r"\s+", " ", org).strip()
        location = re.sub(r"\s+", " ", location).strip()
        if not org:
            continue

        city, st = "", ""
        if "," in location:
            city, tail = location.rsplit(",", 1)
            city, st = city.strip(), state_code(tail.strip())
        else:
            city = location

        start, end = parse_dates(start_raw, end_raw)
        dedupe = (org, city, st, str(start), str(end))
        if dedupe in seen:
            continue
        seen.add(dedupe)

        out.append(Production(
            key=content_key("trw", slug, org, city, st, start, end),
            source="trw",
            show_title=_title_from_slug(html, slug),
            organization=org,
            venue=org,
            city=city,
            state=st,
            country=country_for_state(st),
            start_date=start,
            end_date=end,
            source_url=url,
        ))
    return out


def _title_from_slug(html: str, slug: str) -> str:
    m = re.search(r"<title>\s*(.*?)\s*(?:-\s*Theatrical Rights Worldwide)?\s*</title>",
                  html, re.S | re.I)
    if m and m.group(1).strip():
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return slug.replace("-", " ").title()


def scrape(fetcher: Fetcher | None = None, limit: int | None = None) -> list[Production]:
    fetcher = fetcher or Fetcher(delay=config.TRW_DELAY)
    urls = show_urls(fetcher)
    if limit:
        urls = urls[:limit]
    log.info("TRW: %d show pages to fetch", len(urls))

    out = []
    failures = 0
    for i, url in enumerate(urls, 1):
        try:
            resp = fetcher.get(url)
        except Exception as exc:
            failures += 1
            log.warning("TRW: %s failed: %s", url, exc)
            continue
        out.extend(parse_show_page(resp.text, url))
        if i % 50 == 0:
            log.info("TRW: %d/%d pages, %d productions", i, len(urls), len(out))

    log.info("TRW: %d productions (%d page failures)", len(out), failures)
    if not limit and len(out) < config.MIN_EXPECTED["trw"]:
        raise ScrapeError(
            f"TRW returned only {len(out)} productions "
            f"(expected >= {config.MIN_EXPECTED['trw']}); markup likely changed"
        )
    return out
