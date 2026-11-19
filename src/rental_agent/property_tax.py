"""Where each Collin County property's tax protest stands, for the Property tax page and the
Dashboard reminder.

A protest has three steps: prepare the evidence (the tax-protest job), file the protest, and
go to the hearing. The job saves what it found to tax_protests, and the owner records the
filing and the hearing date on the page. Every date shown comes from that row, or from the
yearly rule while the district's notice is not out. Nothing here reads the district's data.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from rental_agent import db
from rental_agent.jobs import tax_protest as tp
from rental_agent.jobs.lease import long_date


@dataclass(frozen=True)
class DateRow:
    when: str
    what: str
    key: bool        # a day not to miss
    status: str      # "passed", "done" (the protest is filed), "next" (the next day not to
                     # miss), or "" for later


@dataclass(frozen=True)
class Headline:
    """The one date that matters now."""
    day: date | None
    text: str
    left: str        # "12 days left", "today", or "" once the day has passed
    late: bool = False


@dataclass(frozen=True)
class Season:
    property_id: int
    label: str
    year: int
    saved: dict[str, Any]              # the tax_protests row; empty if there is none
    run: dict[str, Any] | None         # the newest tax-protest run for this property
    notice: date                       # when the notice was mailed, or is expected
    deadline: date
    deadline_is_official: bool         # printed on the notice, not worked out from the rule
    step: str      # "wait": no notice yet; then "prepare", "file" or "hearing"; "late": missed
    headline: Headline
    dates: list[DateRow]

    @property
    def prepared(self) -> bool:
        """The job has run on this year's values. A run before the notice is a preview."""
        return self.saved.get("value_year") == self.year

    @property
    def filed_on(self) -> date | None:
        return self.saved.get("filed_on")

    @property
    def hearing(self) -> date | None:
        return self.saved.get("hearing_date")

    @property
    def evidence_due(self) -> date | None:
        return tp.evidence_due(self.hearing) if self.hearing else None


def time_left(day: date, today: date) -> str:
    days = (day - today).days
    if days < 0:
        return ""
    return "today" if days == 0 else f"{days} day{'' if days == 1 else 's'} left"


def _headline(step: str, saved: dict[str, Any], deadline: date, official: bool,
              today: date) -> Headline:
    hearing = saved.get("hearing_date")
    if step == "late":
        records = date(deadline.year, *tp.RECORDS_APPROVED)
        return Headline(deadline, "The last day to file the protest has passed. A late protest "
                        "needs good cause and must reach the board before it approves the "
                        f"records, about {long_date(records)}.", "", late=True)
    if step != "hearing":
        return Headline(deadline, "Last day to file the protest"
                        + ("." if official else " (expected; the notice prints the exact date)."),
                        time_left(deadline, today))
    if hearing is None:
        return Headline(None, f"Filed on {long_date(saved['filed_on'])}. The board sends the "
                        "hearing date at least 15 days ahead. Record it in step 3.", "")
    if today > hearing:
        return Headline(hearing, "The hearing was on this day. To take the board's order "
                        "further, you have 60 days from the day you receive it.", "")
    due = tp.evidence_due(hearing)
    if today <= due:
        return Headline(due, "Last day to upload your evidence. The hearing is on "
                        f"{long_date(hearing)}.", time_left(due, today))
    return Headline(hearing, "Your hearing.", time_left(hearing, today))


def _dates(year: int, saved: dict[str, Any], deadline: date, official: bool,
           today: date) -> list[DateRow]:
    days = tp.key_dates(year, saved.get("notice_date"), deadline, official,
                        saved.get("hearing_date"))
    # The filing deadline is the first key day on that date; an evidence day can share it.
    filing = next(d for d in days if d.key and d.day == deadline)
    done = filing if saved.get("filed_on") else None
    ahead = [d.day for d in days if d.key and d.day >= today and d is not done]
    rows = []
    for d in days:
        if d is done:
            status = "done"
        elif d.day and d.day < today:
            status = "passed"
        elif d.key and d.day == min(ahead):
            status = "next"
        else:
            status = ""
        rows.append(DateRow(d.when, d.what, d.key, status))
    return rows


def _season(conn: psycopg.Connection, prop: db.Property, year: int, today: date) -> Season:
    saved = db.tax_protest(conn, prop.id, year) or {}
    official = saved.get("deadline") is not None
    deadline = saved.get("deadline") or tp.filing_deadline(year, saved.get("notice_date"))
    notice = saved.get("notice_date") or date(year, *tp.NOTICE_DAY)

    if saved.get("filed_on"):
        step = "hearing"
    elif today > deadline:
        step = "late"
    elif saved.get("value_year") == year:      # prepared on this year's values
        step = "file"
    elif today < notice:
        step = "wait"
    else:
        step = "prepare"

    return Season(
        property_id=prop.id, label=prop.label, year=year, saved=saved,
        run=db.last_property_run(conn, "tax-protest", prop.id),
        notice=notice, deadline=deadline, deadline_is_official=official, step=step,
        headline=_headline(step, saved, deadline, official, today),
        dates=_dates(year, saved, deadline, official, today))


def shown_years(conn: psycopg.Connection, property_id: int, today: date) -> list[int]:
    """The season that is open or comes next; before it, the earlier year's while its hearing
    is still to come."""
    year = tp.default_year(today)
    earlier = db.tax_protest(conn, property_id, year - 1)
    pending = earlier and earlier["hearing_date"] and earlier["hearing_date"] >= today
    return [year - 1, year] if pending else [year]


def load(conn: psycopg.Connection, today: date) -> list[Season]:
    """The seasons to show, for every property in Collin County."""
    found = []
    for prop in db.load_properties(conn):
        detail = db.property_detail(conn, prop.id)
        if "collin" not in (detail.county or "").lower():
            continue
        found += [_season(conn, prop, year, today) for year in shown_years(conn, prop.id, today)]
    return found
