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


def _rows_html(added: list) -> str:
    """One row per contact put into the sequence this run.

    Built from candidates rather than productions because the candidate is
    what carries the address that was actually written to GHL -- re-deriving
    it from the registry would risk showing a different one.
    """
    if not added:
        return (
            '<p style="color:#666">Nobody was added today. Either no new '
            "organization cleared verification, or none of the new ones had "
            "both a show title and an opening date.</p>"
        )

    cells = []
    for c in added[:config.EMAIL_TABLE_LIMIT]:
        p = c.production
        when = ""
        if p is not None and p.start_date:
            when = p.start_date.strftime("%b %d, %Y")
        cells.append(
            "<tr>"
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee">{_esc(c.org_name)}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee">'
            f'{_esc(p.show_title if p is not None else "")}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee">'
            f'{_esc(p.city if p is not None else "")}, '
            f'{_esc(p.state if p is not None else "")}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;'
            f'white-space:nowrap">{_esc(when)}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee">{_esc(c.address)}</td>'
            "</tr>"
        )

    more = ""
    if len(added) > config.EMAIL_TABLE_LIMIT:
        more = (f'<p style="color:#666;font-size:13px">'
                f'&hellip; and {len(added) - config.EMAIL_TABLE_LIMIT} more.</p>')
    return (
        '<table style="border-collapse:collapse;width:100%;font-size:14px">'
        '<tr style="text-align:left;color:#666">'
        '<th style="padding:6px 10px">Organization</th>'
        '<th style="padding:6px 10px">Next show</th>'
        '<th style="padding:6px 10px">Where</th>'
        '<th style="padding:6px 10px">Opens</th>'
        '<th style="padding:6px 10px">Email</th></tr>'
        + "".join(cells) + "</table>" + more
    )


def send_digest(added: list, stats: dict, run_date: date | None = None,
                seen_total: int | None = None) -> None:
    """Report who was added to the mass-marketing sequence today.

    `added` is the candidates that received the tag in this run, so each one
    is by construction a new organization with a verified address, a show
    title and a real opening date. `seen_total` is how many new productions
    the scrape found, which stays in the footer: a bare "0 added" is
    indistinguishable from a broken run without it.
    """
    run_date = run_date or date.today()
    n = len(added)
    found = 0 if seen_total is None else seen_total

    subject = (
        f"Shindig — {n} added to {config.GHL_OUTREACH_TAG} "
        f"({run_date.strftime('%b %d')})"
    )

    by_source = stats.get("new_by_source", {})
    source_line = " \u00b7 ".join(
        f"{k.upper()}: {v}" for k, v in sorted(by_source.items())
    ) or "\u2014"

    body = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:760px;color:#222">
  <h2 style="margin:0 0 4px">{n} added to {_esc(config.GHL_OUTREACH_TAG)}</h2>
  <p style="margin:0 0 18px;color:#666">
    {run_date.strftime('%A, %B %d, %Y')} &middot; {source_line}
  </p>

  <p style="margin:0 0 18px">
    <a href="{config.sheet_url()}"
       style="background:#1a73e8;color:#fff;padding:10px 18px;border-radius:4px;
              text-decoration:none;display:inline-block">Open the sheet</a>
  </p>

  {_rows_html(added)}

  <hr style="border:0;border-top:1px solid #eee;margin:22px 0">
  <p style="font-size:13px;color:#666;margin:0">
    {found:,} new production{'s' if found != 1 else ''} found today;
    {n} became {'a contact' if n == 1 else 'contacts'} in the sequence.
    The rest were already known, had no usable email, or had no opening
    date.<br>
    Tracking {stats.get('total', 0):,} live productions across
    {stats.get('orgs', 0):,} organizations
    ({stats.get('pct_with_contact', 0)}% with contact info).<br>
    Enrichment this run: {stats.get('orgs_fetched', 0)} organizations fetched,
    {stats.get('cache_hits', 0)} served from cache.
  </p>
</div>"""

    text = (
        f"{n} added to {config.GHL_OUTREACH_TAG} \u2014 {run_date}\n"
        f"{found} new productions found\n\n"
        + "\n".join(
            f"- {c.org_name} | "
            f"{c.production.show_title if c.production else ''} | "
            f"{c.address}" for c in added[:config.EMAIL_TABLE_LIMIT]
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
