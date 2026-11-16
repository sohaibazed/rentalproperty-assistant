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


# --- Year-end reporting -------------------------------------------------------------------


@dataclass(frozen=True)
class RentRow:
    id: int
    property_id: int | None       # None when the payment isn't matched to a lease
    lease_id: int | None
    amount: Decimal
    paid_on: date
    payer: str | None
    needs_review: bool


@dataclass(frozen=True)
class ExpenseRow:
    id: int
    property_id: int | None       # None = LLC-wide
    amount: Decimal
    spent_on: date
    vendor: str | None
    description: str | None
    category: str
    receipt_path: str | None
    needs_review: bool


@dataclass(frozen=True)
class LeaseRow:
    id: int
    property_id: int
    start_date: date
    end_date: date
    monthly_rent: Decimal


def rent_for_year(conn: psycopg.Connection, year: int) -> list[RentRow]:
    rows = conn.execute(
        """select r.id, l.property_id, r.lease_id, r.amount, r.paid_on, r.payer, r.needs_review
           from rent_payments r left join leases l on l.id = r.lease_id
           where extract(year from r.paid_on) = %s
           order by r.paid_on, r.id""",
        (year,),
    ).fetchall()
    return [RentRow(*r) for r in rows]


def expenses_for_year(conn: psycopg.Connection, year: int) -> list[ExpenseRow]:
    rows = conn.execute(
        """select id, property_id, amount, spent_on, vendor, description,
                  schedule_e_category::text, receipt_path, needs_review
           from expenses
           where extract(year from spent_on) = %s
           order by spent_on, id""",
        (year,),
    ).fetchall()
    return [ExpenseRow(*r) for r in rows]


def leases_active_in_year(conn: psycopg.Connection, year: int) -> list[LeaseRow]:
    rows = conn.execute(
        """select id, property_id, start_date, end_date, monthly_rent
           from leases
           where start_date <= make_date(%s, 12, 31) and end_date >= make_date(%s, 1, 1)
           order by property_id, start_date""",
        (year, year),
    ).fetchall()
    return [LeaseRow(*r) for r in rows]


# --- Lease extensions ---------------------------------------------------------------------


@dataclass(frozen=True)
class PropertyDetail:
    id: int
    address: str
    unit: str | None
    city: str | None
    county: str | None
    state: str
    zip: str | None
    landlord_names: list[str]


@dataclass(frozen=True)
class CurrentLease:
    id: int
    start_date: date
    end_date: date
    monthly_rent: Decimal
    deposit: Decimal | None
    original_lease_date: date | None
    tenant_ids: list[int]
    tenant_names: list[str]

    @property
    def lease_being_extended(self) -> date:
        """The original lease's date: carried forward on extensions, else this lease's start."""
        return self.original_lease_date or self.start_date


def property_detail(conn: psycopg.Connection, property_id: int) -> PropertyDetail | None:
    row = conn.execute(
        """select id, address, unit, city, county, state, zip, coalesce(landlord_names, '{}')
           from properties where id = %s""",
        (property_id,),
    ).fetchone()
    return PropertyDetail(*row) if row else None


def latest_lease(conn: psycopg.Connection, property_id: int) -> CurrentLease | None:
    row = conn.execute(
        """select l.id, l.start_date, l.end_date, l.monthly_rent, l.deposit, l.original_lease_date,
                  coalesce(array_agg(t.id order by t.id) filter (where t.id is not null), '{}'),
                  coalesce(array_agg(t.full_name order by t.id) filter (where t.id is not null), '{}')
           from leases l
           left join lease_tenants lt on lt.lease_id = l.id
           left join tenants t on t.id = lt.tenant_id
           where l.property_id = %s
           group by l.id
           order by l.end_date desc, l.id desc
           limit 1""",
        (property_id,),
    ).fetchone()
    return CurrentLease(*row) if row else None


def recommended_rent(conn: psycopg.Connection, property_id: int, year: int) -> Decimal | None:
    row = conn.execute(
        "select recommended_rent from rent_research where property_id = %s and year = %s",
        (property_id, year),
    ).fetchone()
    return row[0] if row else None


def insert_lease_extension(
    conn: psycopg.Connection,
    *,
    property_id: int,
    start_date: date,
    end_date: date,
    monthly_rent: Decimal,
    deposit: Decimal | None,
    original_lease_date: date,
    other_terms: str | None,
    tenant_ids: list[int],
    docx_path: str,
    pdf_path: str,
) -> int:
    lease_id = conn.execute(
        """insert into leases (property_id, start_date, end_date, monthly_rent, deposit,
                               original_lease_date, other_terms, docx_path, pdf_path)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
        (property_id, start_date, end_date, monthly_rent, deposit, original_lease_date,
         other_terms, docx_path, pdf_path),
    ).fetchone()[0]
    for tenant_id in tenant_ids:
        conn.execute(
            "insert into lease_tenants (lease_id, tenant_id) values (%s, %s)", (lease_id, tenant_id)
        )
    return lease_id
