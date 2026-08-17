"""Morning email digest over Gmail SMTP.

Sends on every run, including zero-new days: a silent morning is
indistinguishable from a broken cron, which is exactly the failure mode this
report cannot afford. Scraper failures get their own, louder email.
"""

from __future__ import annotations

import argparse
import html
import logging
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage

import config

log = logging.getLogger(__name__)


def _send(subject: str, html_body: str, text_body: str) -> None:
    missing = [
        n for n, v in (
            ("GMAIL_USER", config.GMAIL_USER),
            ("GMAIL_APP_PASSWORD", config.GMAIL_APP_PASSWORD),
            ("REPORT_TO", config.REPORT_TO),
        ) if not v
    ]
    if missing:
        raise RuntimeError(f"email not configured: {', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_USER
    msg["To"] = config.REPORT_TO
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
        smtp.starttls(context=ctx)
        smtp.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    log.info("sent %r to %s", subject, config.REPORT_TO)


def _esc(v) -> str:
    return html.escape(str(v or ""))


def _rows_html(productions: list, registry: dict) -> str:
    if not productions:
        return (
            '<p style="color:#666">No new productions today. '
            "The sheet is still current.</p>"
        )

    cells = []
    for p in productions[:config.EMAIL_TABLE_LIMIT]:
        org = registry.get(p.org_key)
        contact = ""
        if org and org.email:
            contact = _esc(org.email)
        elif org and org.phone:
            contact = _esc(org.phone)
        else:
            contact = '<span style="color:#b00">—</span>'
        when = ""
        if p.start_date:
            when = p.start_date.strftime("%b %d, %Y")
            if p.end_date and p.end_date != p.start_date:
                when += f" – {p.end_date.strftime('%b %d')}"
        cells.append(
            "<tr>"
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee">{_esc(p.show_title)}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee">{_esc(p.organization)}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee">{_esc(p.city)}, {_esc(p.state)}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;white-space:nowrap">{_esc(when)}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee">{contact}</td>'
            "</tr>"
        )

    more = ""
    if len(productions) > config.EMAIL_TABLE_LIMIT:
        more = (
            f'<p style="color:#666;font-size:13px">'
            f"…and {len(productions) - config.EMAIL_TABLE_LIMIT} more in the sheet.</p>"
        )

    head = "".join(
        f'<th style="text-align:left;padding:6px 10px;border-bottom:2px solid #333;'
        f'font-size:12px;text-transform:uppercase;color:#555">{h}</th>'
        for h in ("Show", "Organization", "Where", "Dates", "Contact")
    )
    return (
        '<table style="border-collapse:collapse;width:100%;font-size:14px">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(cells)}</tbody></table>{more}"
    )


def send_digest(new_today: list, registry: dict, stats: dict,
                run_date: date | None = None) -> None:
    run_date = run_date or date.today()
    n = len(new_today)
    subject = (
        f"Shindig Report — {n} new production{'s' if n != 1 else ''} "
        f"({run_date.strftime('%b %d')})"
    )

    by_source = stats.get("new_by_source", {})
    source_line = " · ".join(
        f"{k.upper()}: {v}" for k, v in sorted(by_source.items())
    ) or "—"

    with_contact = sum(
        1 for p in new_today
        if (registry.get(p.org_key) and
            (registry[p.org_key].email or registry[p.org_key].phone))
    )

    body = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:760px;color:#222">
  <h2 style="margin:0 0 4px">{n} new production{'s' if n != 1 else ''}</h2>
  <p style="margin:0 0 18px;color:#666">
    {run_date.strftime('%A, %B %d, %Y')} &middot; {source_line}
  </p>

  <p style="margin:0 0 18px">
    <a href="{config.sheet_url()}"
       style="background:#1a73e8;color:#fff;padding:10px 18px;border-radius:4px;
              text-decoration:none;display:inline-block">Open the sheet</a>
  </p>

  {_rows_html(new_today, registry)}

  <hr style="border:0;border-top:1px solid #eee;margin:22px 0">
  <p style="font-size:13px;color:#666;margin:0">
    Contact on file for {with_contact} of {n} new production{'s' if n != 1 else ''}.<br>
    Tracking {stats.get('total', 0):,} live productions across
    {stats.get('orgs', 0):,} organizations
    ({stats.get('pct_with_contact', 0)}% with contact info).<br>
    Enrichment this run: {stats.get('orgs_fetched', 0)} organizations fetched,
    {stats.get('cache_hits', 0)} served from cache.
  </p>
</div>"""

    text = (
        f"{n} new productions — {run_date}\n{source_line}\n\n"
        + "\n".join(
            f"- {p.show_title} | {p.organization} | {p.city}, {p.state} | "
            f"{p.start_date}" for p in new_today[:config.EMAIL_TABLE_LIMIT]
        )
        + f"\n\n{config.sheet_url()}\n"
    )
    _send(subject, body, text)


def send_failure(error: str, run_date: date | None = None) -> None:
    run_date = run_date or date.today()
    subject = f"Shindig Report FAILED — {run_date.strftime('%b %d')}"
    body = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
        '<h2 style="color:#b00">Daily run failed</h2>'
        "<p>The sheet was not updated. Most likely a licensor changed their "
        "endpoint or markup.</p>"
        f'<pre style="background:#f6f6f6;padding:12px;border-radius:4px;'
        f'white-space:pre-wrap;font-size:13px">{_esc(error)}</pre></div>'
    )
    _send(subject, body, f"Daily run failed:\n\n{error}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                    help="send a sample digest to REPORT_TO")
    args = ap.parse_args()
    if args.test:
        from orgs import Organization
        from normalize import Production
        org = Organization(key="k", name="Sample Players", city="Raleigh",
                           state="NC", email="info@sampleplayers.org")
        p = Production(key="mti:1", source="mti", show_title="Les Misérables",
                       organization="Sample Players", city="Raleigh",
                       state="NC", start_date=date.today())
        send_digest([p], {p.org_key: org},
                    {"total": 20000, "orgs": 12000, "pct_with_contact": 58.0,
                     "new_by_source": {"mti": 1}, "orgs_fetched": 0,
                     "cache_hits": 12000})
