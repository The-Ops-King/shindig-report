"""Daily orchestration: scrape → scope → organizations → enrich → publish.

Run modes:
    python main.py                 full run: Sheet + email + state commit
    python main.py --dry-run       scrape only, write out/preview.csv
    python main.py --sheet-only    scrape + Sheet, no email
    python main.py --no-enrich     skip contact enrichment (fast iteration)
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import config
import enrich as enrich_mod
import geo
import orgs as orgs_mod
import state as state_mod
from normalize import in_scope
from scrapers import concord, mti, trw
from scrapers.base import Fetcher

log = logging.getLogger("shindig")


def _setup_logging(verbose: bool = True):
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def scrape_all(skip: set[str]) -> tuple[dict, list]:
    """Run the three scrapers. Concord and TRW overlap; MTI is one request.

    A source that raises propagates: an empty Sheet is worse than a loud
    failure, so `main` turns this into a failure email rather than a quiet
    partial write.
    """
    results, errors = {}, []

    def run(name, fn):
        started = time.time()
        try:
            out = fn()
            log.info("%s: %d productions in %.1fs", name, len(out),
                     time.time() - started)
            return name, out
        except Exception as exc:
            log.error("%s failed: %s", name, exc)
            errors.append(f"{name}: {exc}")
            return name, None

    jobs = []
    if "mti" not in skip:
        jobs.append(("mti", lambda: mti.scrape()))
    if "concord" not in skip:
        jobs.append(("concord", lambda: concord.scrape()))
    if "trw" not in skip:
        jobs.append(("trw", lambda: trw.scrape(Fetcher(delay=config.TRW_DELAY))))

    # TRW is the long pole (~13 min); running it alongside the others keeps
    # total wall clock near TRW's own runtime.
    with ThreadPoolExecutor(max_workers=3) as pool:
        for name, out in pool.map(lambda j: run(*j), jobs):
            if out is not None:
                results[name] = out

    if errors:
        raise RuntimeError("; ".join(errors))
    return results, errors


def write_preview(productions, registry, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    from sheets import PRODUCTION_HEADERS, production_row
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(PRODUCTION_HEADERS)
        for p in sorted(productions, key=lambda x: (x.source, x.organization)):
            w.writerow(production_row(p, registry.get(p.org_key)))
    log.info("wrote %s (%d rows)", path, len(productions))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="scrape only; write out/preview.csv, no Sheet or email")
    ap.add_argument("--sheet-only", action="store_true",
                    help="scrape and write the Sheet, but send no email")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip contact enrichment")
    ap.add_argument("--skip", default="",
                    help="comma-separated sources to skip (mti,concord,trw)")
    ap.add_argument("--force-enrich", action="store_true",
                    help="ignore the contact cache TTL")
    ap.add_argument("--save-cache", action="store_true",
                    help="persist the contact cache even on a dry run, so the "
                         "one-time bootstrap is not thrown away")
    args = ap.parse_args(argv)

    _setup_logging()
    started = time.time()
    today = date.today()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    try:
        results, _ = scrape_all(skip)

        everything = [p for rows in results.values() for p in rows]

        # MTI is the only source with reliable state/country, so it seeds the
        # geo reference that fills Concord's null states.
        resolver = geo.GeoResolver()
        resolver.add_reference(results.get("mti", []))
        geo_stats = geo.apply(resolver, everything)
        log.info("geo: %s", geo_stats)

        scoped = [
            p for p in everything
            if in_scope(p, config.COUNTRIES, today, config.DROP_ENDED)
        ]
        log.info("scope: %d of %d productions in %s",
                 len(scoped), len(everything), sorted(config.COUNTRIES))

        # Organizations first, enrichment second: this ordering is what makes a
        # company with 15 shows cost exactly one fetch.
        registry = orgs_mod.build_registry(scoped)
        orgs_mod.apply_registry(scoped, registry)
        cov = orgs_mod.coverage(registry)
        log.info("orgs: %s", cov)

        # The Contacts tab is the authoritative cache. Read it before
        # enrichment so an organization already listed there costs zero
        # requests, and so hand-edited rows are honoured rather than clobbered.
        book = None
        cache = {}
        if not args.dry_run:
            import sheets
            book = sheets.open_sheet(sheets.client())
            cache = sheets.read_contacts(book)
        if not cache:
            # Local fallback for dry runs and first-ever runs.
            cache = state_mod.load_org_cache()

        if args.no_enrich:
            enrich_stats = {"orgs_seen": len(registry), "orgs_fetched": 0,
                            "cache_hits": 0, "manual_hits": 0,
                            "with_contact": 0, "pct_with_contact": 0.0}
        else:
            enrich_stats = enrich_mod.enrich(
                registry, cache, today, force=args.force_enrich
            )
            log.info("enrichment: %s", enrich_stats)
            if not args.dry_run or args.save_cache:
                state_mod.save_org_cache(cache)

        seen = state_mod.load_seen()
        was_empty = not seen
        new_today = state_mod.diff_new(scoped, seen, today)
        if was_empty:
            log.info("first run: all %d productions are new", len(new_today))

        by_source = {}
        for p in new_today:
            by_source[p.source] = by_source.get(p.source, 0) + 1

        stats = {
            "total": len(scoped),
            "orgs": len(registry),
            "new_by_source": by_source,
            **cov, **enrich_stats,
        }

        if args.dry_run:
            write_preview(scoped, registry, config.OUT_DIR / "preview.csv")
            log.info(
                "DRY RUN: %d in scope, %d new, %d orgs, %.1f%% with contact",
                len(scoped), len(new_today), len(registry),
                enrich_stats.get("pct_with_contact", 0.0),
            )
            return 0

        duration = round(time.time() - started, 1)
        log_entry = [
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            len(results.get("mti", [])),
            len(results.get("concord", [])),
            len(results.get("trw", [])),
            len(scoped), len(new_today), len(registry),
            enrich_stats.get("orgs_fetched", 0),
            enrich_stats.get("cache_hits", 0),
            enrich_stats.get("manual_hits", 0),
            enrich_stats.get("with_contact", 0),
            enrich_stats.get("pct_with_contact", 0.0),
            duration, "ok", "",
        ]

        import sheets
        sheets.publish(scoped, new_today, registry, log_entry,
                       cache=cache, book=book)
        state_mod.save_seen(seen)

        if not args.sheet_only:
            import notify
            notify.send_digest(new_today, registry, stats, today)

        log.info("done in %.0fs — %d new of %d", duration, len(new_today),
                 len(scoped))
        return 0

    except Exception as exc:
        log.error("run failed: %s", exc)
        detail = traceback.format_exc()
        if not args.dry_run and not args.sheet_only:
            try:
                import notify
                notify.send_failure(detail, today)
            except Exception as mail_exc:
                log.error("could not send failure email: %s", mail_exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
