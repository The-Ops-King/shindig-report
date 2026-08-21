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
import outreach as outreach_mod
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


def take_sample(productions: list, n: int) -> list:
    """Keep every production belonging to N organizations.

    Sampling by organization rather than by production keeps the run coherent:
    the Sheet still shows a company's full season, and enrichment still does
    exactly one lookup per company, which is the behaviour worth eyeballing.
    Organizations with a website are preferred so enrichment actually has
    something to do, and sources are interleaved so all three are represented.
    """
    by_org: dict[str, list] = {}
    for p in productions:
        by_org.setdefault(p.org_key, []).append(p)

    def rank(item):
        key, rows = item
        has_site = any(r.org_website for r in rows)
        # Deterministic: no randomness, so a repeat run samples the same orgs.
        return (0 if has_site else 1, rows[0].source, -len(rows), key)

    chosen, seen_sources = [], set()
    ordered = sorted(by_org.items(), key=rank)
    # First pass: guarantee at least one organization from each source.
    for key, rows in ordered:
        src = rows[0].source
        if src not in seen_sources and len(chosen) < n:
            seen_sources.add(src)
            chosen.append(key)
    for key, _ in ordered:
        if len(chosen) >= n:
            break
        if key not in chosen:
            chosen.append(key)

    picked = [p for key in chosen for p in by_org[key]]
    log.info("sample: %d organizations, %d productions (sources: %s)",
             len(chosen), len(picked),
             ", ".join(sorted({p.source for p in picked})))
    return picked


def scrape_all(skip: set[str], trw_pages: int = 0) -> tuple[dict, list]:
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
        jobs.append(("trw", lambda: trw.scrape(
            Fetcher(delay=config.TRW_DELAY), limit=trw_pages or None)))

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


def title_counts(productions, today) -> dict:
    """Show title -> how many productions of it open in the outreach window.

    Drives the ordering of the Show Links tab: filling the top 100 rows covers
    over half the sendable volume.
    """
    counts = {}
    for p in productions:
        if outreach_mod.in_window(p, today) and p.date_type != "license window":
            counts[p.show_title] = counts.get(p.show_title, 0) + 1
    return counts


def run_outreach(productions, registry, book, today, args):
    """Select who to contact, push them to GHL, and record it.

    Returns (stats, sheet rows). A dry run does everything except touch GHL,
    which is the only safe way to look at a cold-email queue before it sends.
    """
    import ghl
    import sheets

    dry = args.outreach_dry_run
    ingest = args.outreach_ingest
    refused = ""
    if not ingest and not dry and not config.OUTREACH_ENABLED:
        # Downgrade rather than abort. Bailing out here would also skip the
        # contact and card writes that ARE wanted, so GHL would quietly stop
        # being kept current -- a refusal to send should not become a refusal
        # to do the rest of the job.
        refused = ("live outreach requested but OUTREACH_ENABLED is not set; "
                   "writing contacts only, emailing nobody")
        log.warning("outreach: %s", refused)
        ingest = True

    outreach_state = state_mod.load_outreach()
    opportunities = state_mod.load_opportunities()

    show_links = sheets.read_show_links(book) if book else {}
    if not show_links:
        log.warning("no sample links available; nothing can be sent")

    candidates = outreach_mod.build_candidates(
        productions, registry, show_links, outreach_state, today, hold=ingest
    )
    outreach_mod.load_opportunity_ids(candidates, opportunities)
    # Ingest writes the CRM and sends nothing, so there is nobody to select.
    sending = [] if ingest else outreach_mod.select(candidates, args.outreach_limit)
    stats = outreach_mod.summarize(candidates)
    stats["selected"] = len(sending)
    stats["mode"] = "ingest" if ingest else "outreach"
    if refused:
        # Surfaced in the morning digest, not just a collapsed Actions log: a
        # live request that was declined is exactly the thing worth noticing.
        stats["refused"] = refused
    log.info("outreach: %s", stats)

    client = ghl.GHLClient()
    # Ingest needs no workflow id -- that is the whole point of the mode.
    ok, missing = client.configured() if ingest else client.can_enrol()
    if not dry and not ok:
        log.error("outreach skipped: %s not set", missing)
        stats["error"] = f"missing {missing}"
        return stats, []

    rows, sent = [], 0
    for cand in sending:
        if dry:
            rows.append(sheets.outreach_row(cand, today, "DRY RUN"))
            continue
        pushed, err = client.push(cand)
        if pushed:
            outreach_mod.record_send(outreach_state, cand, today)
            outreach_mod.record_opportunity(opportunities, cand, today)
            sent += 1
            rows.append(sheets.outreach_row(cand, today, "sent"))
        else:
            rows.append(sheets.outreach_row(cand, today, "failed", err))

    if ingest:
        # New organizations, and ones whose next show or ready state moved.
        # Everything else is already correct in GHL and is not written again.
        want_card = client.pipeline_configured()
        if not want_card:
            log.warning("no GHL_PIPELINE_ID/STAGE_ID: writing contacts with no "
                        "pipeline cards. Adding the secrets later re-writes "
                        "each organization once to create its card.")
        todo = [c for c in candidates
                if c.action != "clear"
                and outreach_mod.needs_ingest(c, opportunities, want_card)]
        # Ready first: under a cap, the organizations you could actually start
        # today are the ones worth having landed.
        todo.sort(key=lambda c: (not c.ready, c.production.start_date
                                 if c.production else date.max, c.org_name))
        # --outreach-limit doubles as the ingest cap, so a first run can be
        # held to a handful and eyeballed in GHL before the full bootstrap.
        cap = (args.outreach_limit if args.outreach_limit is not None
               else config.OUTREACH_INGEST_CAP)
        deferred = max(0, len(todo) - cap) if cap else 0
        if deferred:
            log.info("ingest: %d organizations deferred to the next run "
                     "(cap %d)", deferred, cap)
        stats["ingest_pending"] = len(todo)
        written = 0
        # A run that hits the job timeout mid-ingest never reaches the state
        # commit, so the ledger is lost and the whole bootstrap repeats.
        # Stopping on the clock instead means whatever was written is kept.
        deadline = time.time() + config.OUTREACH_INGEST_MAX_SECONDS
        batch = todo[:cap] if cap else todo
        for i, cand in enumerate(batch):
            if dry:
                rows.append(sheets.outreach_row(cand, today, "DRY RUN"))
                continue
            if time.time() > deadline:
                deferred += len(batch) - i
                log.warning("ingest: %ds budget spent, %d left for the next run",
                            config.OUTREACH_INGEST_MAX_SECONDS, len(batch) - i)
                break
            pushed, err = client.push(cand)
            if pushed:
                outreach_mod.record_opportunity(opportunities, cand, today)
                written += 1
                rows.append(sheets.outreach_row(cand, today, "ingested"))
            else:
                rows.append(sheets.outreach_row(cand, today, "failed", err))
        stats["ingested"] = written
        stats["ingest_deferred"] = deferred
        log.info("ingest: %d written, %d pending, %d deferred",
                 written, len(todo), deferred)

    # Every other organization still gets its card and custom fields refreshed:
    # the pipeline tracks companies, and their next show changed even though we
    # are not emailing about it.
    if not dry and ok and not ingest:
        for cand in candidates:
            if cand.action == "update_only" and cand.ghl_contact_id:
                if client.push(cand)[0]:
                    outreach_mod.record_opportunity(opportunities, cand, today)

    # Anyone whose show has been and gone with nothing booked comes back out of
    # the sequence. Their card stays where it is -- the company has not gone
    # away, it just has nothing on right now.
    for cand in candidates:
        if cand.action != "clear" or not cand.ghl_contact_id:
            continue
        if dry:
            rows.append(sheets.outreach_row(cand, today, "DRY RUN"))
        elif ok and client.push(cand)[0]:
            outreach_mod.record_clear(outreach_state, cand, today)
            rows.append(sheets.outreach_row(cand, today, "cleared"))

    if not dry:
        state_mod.save_outreach(outreach_state)
        state_mod.save_opportunities(opportunities)
    stats["sent"] = sent if not dry else 0
    stats["dry_run"] = dry
    log.info("outreach: %d sent, %d queued rows", stats["sent"], len(rows))
    return stats, rows


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
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="keep only N organizations (and their productions). "
                         "A real run end to end -- Sheet and email included -- "
                         "just small enough to eyeball before the full one.")
    ap.add_argument("--outreach", action="store_true",
                    help="run the outreach stage: push contacts to GHL and "
                         "enrol them in the workflow")
    ap.add_argument("--outreach-ingest", action="store_true",
                    help="write every organization to GHL -- contact, tags, "
                         "fields, card -- and enrol nobody. For before the "
                         "workflow exists.")
    ap.add_argument("--outreach-dry-run", action="store_true",
                    help="build the send queue and log it, but touch no GHL "
                         "endpoint. Run this first.")
    ap.add_argument("--outreach-limit", type=int, default=None, metavar="N",
                    help="override the daily send cap for this run")
    ap.add_argument("--trw-pages", type=int, default=0, metavar="N",
                    help="fetch only N TRW show pages; TRW's full sweep takes "
                         "~13 minutes, which is a long wait for a smoke test")
    args = ap.parse_args(argv)

    _setup_logging()
    started = time.time()
    today = date.today()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    try:
        results, _ = scrape_all(skip, trw_pages=args.trw_pages)

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

        if args.sample:
            scoped = take_sample(scoped, args.sample)

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

        # --- outreach ------------------------------------------------------
        outreach_stats = {}
        outreach_rows = []
        if args.outreach or args.outreach_dry_run or args.outreach_ingest:
            outreach_stats, outreach_rows = run_outreach(
                scoped, registry, book, today, args
            )

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
        # Keep the Show Links tab stocked with every title in the window,
        # ranked by production count, so the rows worth filling are on top.
        # Never rewrites a URL someone has typed.
        try:
            sheets.seed_show_links(book, title_counts(scoped, today))
        except Exception as exc:
            log.warning("could not seed %r: %s", config.TAB_SHOW_LINKS, exc)
        if outreach_rows:
            sheets.append_outreach(book, outreach_rows)
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
