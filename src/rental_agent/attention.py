"""Things that need the owner's attention, for the Dashboard and the chat home page.

Each check compares what is recorded with today's date:
- rent that is missing or short in a recent month;
- a lease whose yearly extension is due, overdue, or that has run out;
- a property-tax protest to file, from the district's notice to the filing deadline;
- rows waiting on the Review page;
- an email sync that failed;
- a backup that failed, or no backup for several days.

Nothing here writes to the database or sends anything.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from rental_agent import db, property_tax, renewal
from rental_agent.jobs import tax_report
from rental_agent.jobs.lease import NOTICE_DAYS, long_date, money

# Rent is due on the 1st and reaches the database with the email sync on the 11th
# (web/jobs.py SYNC_DAYS). From the 12th, a month with no rent recorded is really missing.
RENT_CHECK_DAY = 12
# An alert is about recent months. Older gaps stay in the tax report, not on the Dashboard.
RENT_MONTHS = 3
# The renewal reminder ends with the step to do next (renewal.Renewal.step).
# The scheduler backs up daily (web/jobs.py). Older than this, something has stopped.
BACKUP_MAX_AGE_DAYS = 3
NEXT_STEP = {
    "research": "Start with rent research.",
    "generate": "The rent research is saved. Generate the extension next.",
    "sign": "The extension is written. Get it signed, then import the signed copy.",
}


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
    """One item per property whose renewal is open, until its signed extension is imported."""
    items = []
    for r in renewal.load(conn, today):
        if r.step == "wait":
            continue
        end, sign_by = long_date(r.current.end_date), long_date(r.sign_by)
        if r.timing == "ended":
            text = f"the lease term ended on {end} and no signed extension is recorded."
        elif r.timing == "late":
            text = (f"the {NOTICE_DAYS}-day notice date ({sign_by}) has passed and the lease "
                    f"ends on {end}. Without a signed extension it rolls to month-to-month at "
                    "the current rent.")
        else:
            text = (f"the lease ends on {end}. Get the extension signed by {sign_by} "
                    f"({r.time_left}).")
        level = "now" if r.timing in ("late", "ended") else "soon"
        items.append(Item(level, f"{r.label}: {text} {NEXT_STEP[r.step]}", "/renewal",
                          "Open Renewal"))
    return items


def tax_items(conn: psycopg.Connection, today: date) -> list[Item]:
    """One item per property from the day the district mails its notice until the protest is
    filed or its deadline passes."""
    items = []
    for s in property_tax.load(conn, today):
        if s.step not in ("prepare", "file"):
            continue
        mailed = (f"mailed the {s.year} appraisal notice on {long_date(s.notice)}"
                  if s.saved.get("notice_date") else
                  f"mails the {s.year} appraisal notice about {long_date(s.notice)}")
        text = (f"{s.label}: the district {mailed}. File the protest by "
                f"{long_date(s.deadline)} ({property_tax.time_left(s.deadline, today)})"
                + ("." if s.deadline_is_official else "; the notice prints the exact date."))
        if s.step == "prepare":
            text += " Start by preparing the evidence."
        items.append(Item("soon", text, "/property-tax", "Open Property tax"))
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


def backup_items(conn: psycopg.Connection, today: date) -> list[Item]:
    runs = db.job_runs(conn, limit=BACKUP_MAX_AGE_DAYS + 2, job="backup")
    if not runs:
        return []
    started = runs[0]["started_at"].astimezone().date()      # the database stores UTC
    if runs[0]["status"] == "failed":
        return [Item("now", f"The last backup failed on {long_date(started)}.",
                     f"/runs/{runs[0]['id']}", "Open the run")]
    if today - started > timedelta(days=BACKUP_MAX_AGE_DAYS):
        return [Item("now", f"The newest backup is from {long_date(started)}. Backups run "
                     "daily while the app is running.", "/jobs", "Open Jobs")]
    return []


def items(conn: psycopg.Connection, today: date) -> list[Item]:
    """Everything that needs attention, most urgent first."""
    found = (sync_items(conn) + backup_items(conn, today) + rent_items(conn, today)
             + renewal_items(conn, today) + tax_items(conn, today) + review_items(conn))
    return sorted(found, key=lambda item: item.level != "now")
