"""Where each property's yearly lease renewal stands, for the Renewal page, the Dashboard
reminder and the scheduler.

A renewal has three steps: research the rent, generate the extension, and import the signed
copy. Nothing extra is stored for it. The step comes from the lease terms, the saved rent
research and today's date.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import psycopg

from rental_agent import db
from rental_agent.jobs.lease import NOTICE_DAYS, default_term

# The renewal opens this many days before the notice date: time to research the rent, write
# the extension and get it signed. With the 45-day notice, that is 90 days before the term ends.
RENEWAL_LEAD_DAYS = 45


@dataclass(frozen=True)
class Renewal:
    property_id: int
    label: str
    current: db.CurrentLease           # the latest signed term
    draft: db.CurrentLease | None      # its extension: written, not signed yet
    research: dict[str, Any] | None    # saved rent research for the year the next term starts
    research_run: dict[str, Any] | None    # the newest research-rent run for this property
    next_start: date                   # the next term, unless other dates are given
    next_end: date
    opens_on: date
    sign_by: date
    step: str      # "wait": not open yet; then "research", "generate" or "sign": what to do next
    timing: str    # "early", "open", "late" (the notice date has passed) or "ended"
    days_left: int                     # until the sign-by date; negative after it
    auto_research: bool                # the scheduler's one research run is still to come

    @property
    def year(self) -> int:
        return self.next_start.year

    @property
    def time_left(self) -> str:
        if self.days_left == 0:
            return "today"
        return f"{self.days_left} day{'' if self.days_left == 1 else 's'} left"


def load(conn: psycopg.Connection, today: date) -> list[Renewal]:
    """One renewal per property that has a signed lease."""
    found = []
    for prop in db.load_properties(conn):
        current = db.latest_lease(conn, prop.id)
        if current is None:
            continue
        draft = db.draft_lease(conn, prop.id)
        next_start, next_end = default_term(current.end_date)
        sign_by = current.end_date - timedelta(days=NOTICE_DAYS)
        opens_on = sign_by - timedelta(days=RENEWAL_LEAD_DAYS)
        research = db.rent_research_for(conn, prop.id, next_start.year)

        if draft is not None:
            step = "sign"
        elif today < opens_on:
            step = "wait"
        elif research is None:
            step = "research"
        else:
            step = "generate"

        if today > current.end_date:
            timing = "ended"
        elif today > sign_by:
            timing = "late"
        elif today >= opens_on:
            timing = "open"
        else:
            timing = "early"

        # The scheduler starts rent research once per renewal: while the renewal is open, the
        # term hasn't ended and no research is saved. A run that failed is not repeated, so a
        # broken setup can't spend money every hour; the owner starts the next one.
        auto_research = (step == "research" and timing != "ended"
                         and not db.research_was_scheduled(conn, prop.id, next_start.year))

        found.append(Renewal(
            property_id=prop.id, label=prop.label, current=current, draft=draft,
            research=research, research_run=db.last_property_run(conn, "research-rent", prop.id),
            next_start=next_start, next_end=next_end, opens_on=opens_on, sign_by=sign_by,
            step=step, timing=timing, days_left=(sign_by - today).days,
            auto_research=auto_research))
    return found


def research_to_start(conn: psycopg.Connection, today: date) -> dict[str, Any] | None:
    """Options for the research-rent run the scheduler should start now, if any."""
    for renewal in load(conn, today):
        if renewal.auto_research:
            return {"property_id": renewal.property_id, "year": renewal.year, "dry_run": False}
    return None
