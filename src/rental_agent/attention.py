"""Things that need the owner's attention, for the Dashboard and the chat home page.

Each check compares what is recorded with today's date:
- rent that is missing or short in a recent month;
- a lease whose yearly extension is due, overdue, or that has run out;
- rows waiting on the Review page;
- an email sync that failed.

Nothing here writes to the database or sends anything.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from rental_agent import db
from rental_agent.jobs import tax_report
from rental_agent.jobs.lease import NOTICE_DAYS, long_date, money

# Rent is due on the 1st and reaches the database with the email sync on the 11th
# (web/jobs.py SYNC_DAYS). From the 12th, a month with no rent recorded is really missing.
RENT_CHECK_DAY = 12
# An alert is about recent months. Older gaps stay in the tax report, not on the Dashboard.
RENT_MONTHS = 3
# Start the renewal reminder this many days before the notice date: time to research the
# rent, write the extension and get it signed.
RENEWAL_LEAD_DAYS = 45


@dataclass(frozen=True)
class Item:
    level: str        # "now": something is wrong or overdue; "soon": coming up or routine
    text: str
    link: str         # the page in this app that deals with it
    link_text: str


def _month_start(day: date, months_back: int = 0) -> date:
    """First day of the month `months_back` months before the month `day` is in."""
    index = day.year * 12 + day.month - 1 - months_back
    return date(index // 12, index % 12 + 1, 1)


def rent_items(conn: psycopg.Connection, today: date) -> list[Item]:
    # rent_gaps judges the months that ended before the date it is given. From the check day
    # on, this month is judged too, so give it the 1st of next month.
    judged_before = _month_start(today, -1) if today.day >= RENT_CHECK_DAY else today
    latest = _month_start(judged_before, 1)
    earliest = _month_start(latest, RENT_MONTHS - 1)
    labels = {p.id: p.label for p in db.load_properties(conn)}
    items = []
    for year in range(earliest.year, latest.year + 1):
        gaps = tax_report.rent_gaps(year, db.leases_active_in_year(conn, year),
                                    db.rent_for_year(conn, year), judged_before)
        for gap in gaps:
            if not earliest <= gap.month <= latest:
                continue
            where = labels.get(gap.property_id, f"property {gap.property_id}")
            if gap.paid == 0:
                text = f"{where}: no rent recorded for {gap.month:%B %Y} ({money(gap.due)} due)."
            else:
                text = (f"{where}: {money(gap.paid)} of {money(gap.due)} recorded for "
                        f"{gap.month:%B %Y}.")
            items.append(Item("now", text, f"/rent?year={gap.month.year}", "Open Rent"))
    return items


def renewal_items(conn: psycopg.Connection, today: date) -> list[Item]:
    """One item per property whose latest lease term needs an extension. A recorded extension
    is the latest term, so its property drops off the list."""
    items = []
    for prop in db.load_properties(conn):
        lease = db.latest_lease(conn, prop.id)
        if lease is None:
            continue
        end, sign_by = lease.end_date, lease.end_date - timedelta(days=NOTICE_DAYS)
        if today > end:
            items.append(Item("now", f"{prop.label}: the lease term ended on {long_date(end)} "
                              "and no extension is recorded. Import the signed extension, or "
                              "generate one.", "/jobs", "Run a job"))
        elif today > sign_by:
            items.append(Item("now", f"{prop.label}: the {NOTICE_DAYS}-day notice date "
                              f"({long_date(sign_by)}) has passed and the lease ends on "
                              f"{long_date(end)}. Without a signed extension it rolls to "
                              "month-to-month at the current rent.", "/jobs", "Run a job"))
        elif today >= sign_by - timedelta(days=RENEWAL_LEAD_DAYS):
            left = (sign_by - today).days
            when = "today" if left == 0 else f"{left} day{'' if left == 1 else 's'} left"
            items.append(Item("soon", f"{prop.label}: the lease ends on {long_date(end)}. Get "
                              f"the extension signed by {long_date(sign_by)} ({when}). Start "
                              "with rent research.", "/jobs", "Run a job"))
    return items


def review_items(conn: psycopg.Connection) -> list[Item]:
    count = db.review_count(conn)
    if not count:
        return []
    rows = "1 row needs" if count == 1 else f"{count} rows need"
    return [Item("soon", f"{rows} a look on the Review page.", "/review", "Open Review")]


def sync_items(conn: psycopg.Connection) -> list[Item]:
    runs = db.job_runs(conn, limit=1, job="sync-email")
    if not runs or runs[0]["status"] != "failed":
        return []
    started = runs[0]["started_at"].astimezone().date()      # the database stores UTC
    return [Item("now", f"The last email sync failed on {long_date(started)}. "
                 "Rent and expenses since then may be missing.", f"/runs/{runs[0]['id']}",
                 "Open the run")]


def items(conn: psycopg.Connection, today: date) -> list[Item]:
    """Everything that needs attention, most urgent first."""
    found = (sync_items(conn) + rent_items(conn, today) + renewal_items(conn, today)
             + review_items(conn))
    return sorted(found, key=lambda item: item.level != "now")
