"""Postgres access for the least-privilege rental_agent role."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import psycopg


@dataclass(frozen=True)
class Property:
    id: int
    address: str
    unit: str | None

    @property
    def label(self) -> str:
        return f"{self.address} {self.unit}".strip() if self.unit else self.address


def connect(db_url: str) -> psycopg.Connection:
    return psycopg.connect(db_url)


def load_properties(conn: psycopg.Connection) -> list[Property]:
    rows = conn.execute("select id, address, unit from properties order by id").fetchall()
    return [Property(*r) for r in rows]


def processed_message_ids(conn: psycopg.Connection, message_ids: list[str]) -> set[str]:
    rows = conn.execute(
        "select message_id from email_ingest_log where message_id = any(%s)", (message_ids,)
    ).fetchall()
    return {r[0] for r in rows}


def active_lease_id(conn: psycopg.Connection, property_id: int, on: date) -> int | None:
    row = conn.execute(
        """select id from leases
           where property_id = %s and start_date <= %s and end_date >= %s
           order by start_date desc limit 1""",
        (property_id, on, on),
    ).fetchone()
    return row[0] if row else None


def last_successful_sync_start(conn: psycopg.Connection) -> datetime | None:
    row = conn.execute(
        "select max(started_at) from sync_runs where status = 'success'"
    ).fetchone()
    return row[0] if row else None


def start_sync_run(conn: psycopg.Connection) -> int:
    run_id = conn.execute("insert into sync_runs default values returning id").fetchone()[0]
    conn.commit()
    return run_id


def finish_sync_run(conn: psycopg.Connection, run_id: int, status: str, inserted: int) -> None:
    conn.execute(
        "update sync_runs set finished_at = now(), status = %s, inserted = %s where id = %s",
        (status, inserted, run_id),
    )
    conn.commit()


def insert_rent_payment(
    conn: psycopg.Connection,
    *,
    lease_id: int | None,
    amount: Decimal,
    paid_on: date,
    payer: str | None,
    message_id: str,
    needs_review: bool,
) -> bool:
    """Returns False if this message was already recorded."""
    cur = conn.execute(
        """insert into rent_payments (lease_id, amount, paid_on, payer, source_message_id, needs_review)
           values (%s, %s, %s, %s, %s, %s)
           on conflict (source_message_id) do nothing""",
        (lease_id, amount, paid_on, payer, message_id, needs_review),
    )
    return cur.rowcount == 1


def insert_expense(
    conn: psycopg.Connection,
    *,
    property_id: int | None,
    amount: Decimal,
    spent_on: date,
    vendor: str | None,
    description: str | None,
    category: str,
    receipt_path: str | None,
    message_id: str | None,
    needs_review: bool,
) -> bool:
    """Returns False if this message was already recorded. message_id is None for manual entries."""
    cur = conn.execute(
        """insert into expenses (property_id, amount, spent_on, vendor, description,
                                 schedule_e_category, receipt_path, source_message_id, needs_review)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (source_message_id) do nothing""",
        (property_id, amount, spent_on, vendor, description, category, receipt_path,
         message_id, needs_review),
    )
    return cur.rowcount == 1


def log_ingested(conn: psycopg.Connection, message_id: str, outcome: str) -> None:
    conn.execute(
        "insert into email_ingest_log (message_id, outcome) values (%s, %s) "
        "on conflict (message_id) do nothing",
        (message_id, outcome),
    )
