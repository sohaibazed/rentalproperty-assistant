"""Postgres access for the least-privilege rental_agent role."""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import psycopg
import psycopg.rows

log = logging.getLogger(__name__)


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
           where extract(year from r.paid_on) = %s and r.removed_at is null
           order by r.paid_on, r.id""",
        (year,),
    ).fetchall()
    return [RentRow(*r) for r in rows]


def expenses_for_year(conn: psycopg.Connection, year: int) -> list[ExpenseRow]:
    rows = conn.execute(
        """select id, property_id, amount, spent_on, vendor, description,
                  schedule_e_category::text, receipt_path, needs_review
           from expenses
           where extract(year from spent_on) = %s and removed_at is null
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
    other_terms: str | None = None
    pdf_path: str | None = None

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


def _lease_term(conn: psycopg.Connection, property_id: int, which: str) -> CurrentLease | None:
    """The property's lease term with the latest end date among those `which` selects."""
    row = conn.execute(
        f"""select l.id, l.start_date, l.end_date, l.monthly_rent, l.deposit, l.original_lease_date,
                   coalesce(array_agg(t.id order by t.id) filter (where t.id is not null), '{{}}'),
                   coalesce(array_agg(t.full_name order by t.id) filter (where t.id is not null), '{{}}'),
                   l.other_terms, l.pdf_path
            from leases l
            left join lease_tenants lt on lt.lease_id = l.id
            left join tenants t on t.id = lt.tenant_id
            where l.property_id = %s and {which}
            group by l.id
            order by l.end_date desc, l.id desc
            limit 1""",
        (property_id,),
    ).fetchone()
    return CurrentLease(*row) if row else None


def latest_lease(conn: psycopg.Connection, property_id: int) -> CurrentLease | None:
    """The latest signed term. An extension that is written but not signed doesn't count."""
    return _lease_term(conn, property_id, "not l.awaiting_signature")


def draft_lease(conn: psycopg.Connection, property_id: int) -> CurrentLease | None:
    """The extension generate-lease wrote that isn't signed yet, if there is one. It counts
    only while it runs past the latest signed term: a later signed lease supersedes it."""
    return _lease_term(conn, property_id, """l.awaiting_signature and l.end_date > (
        select max(s.end_date) from leases s
        where s.property_id = l.property_id and not s.awaiting_signature)""")


def recommended_rent(conn: psycopg.Connection, property_id: int, year: int) -> Decimal | None:
    row = conn.execute(
        "select recommended_rent from rent_research where property_id = %s and year = %s",
        (property_id, year),
    ).fetchone()
    return row[0] if row else None


def original_lease_pdf(conn: psycopg.Connection, property_id: int, lease_date: date) -> str | None:
    """pdf_path of the original lease (not an extension) that started on lease_date."""
    row = conn.execute(
        """select pdf_path from leases
           where property_id = %s and start_date = %s and original_lease_date is null
             and pdf_path is not null
           order by id limit 1""",
        (property_id, lease_date),
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
    pdf_path: str,
) -> int:
    lease_id = conn.execute(
        """insert into leases (property_id, start_date, end_date, monthly_rent, deposit,
                               original_lease_date, other_terms, pdf_path, awaiting_signature)
           values (%s, %s, %s, %s, %s, %s, %s, %s, true) returning id""",
        (property_id, start_date, end_date, monthly_rent, deposit, original_lease_date,
         other_terms, pdf_path),
    ).fetchone()[0]
    for tenant_id in tenant_ids:
        conn.execute(
            "insert into lease_tenants (lease_id, tenant_id) values (%s, %s)", (lease_id, tenant_id)
        )
    return lease_id


def update_lease_draft(
    conn: psycopg.Connection,
    lease_id: int,
    *,
    start_date: date,
    end_date: date,
    monthly_rent: Decimal,
    other_terms: str | None,
    pdf_path: str,
) -> None:
    """Replace the unsigned extension with the one generate-lease just wrote."""
    conn.execute(
        """update leases set start_date = %s, end_date = %s, monthly_rent = %s, other_terms = %s,
                             pdf_path = %s
           where id = %s and awaiting_signature""",
        (start_date, end_date, monthly_rent, other_terms, pdf_path, lease_id),
    )


# --- Rent research ------------------------------------------------------------------------


@dataclass(frozen=True)
class PropertySize:
    property_type: str | None
    beds: Decimal | None
    baths: Decimal | None
    sqft: int | None


def property_size(conn: psycopg.Connection, property_id: int) -> PropertySize | None:
    row = conn.execute(
        "select property_type, beds, baths, sqft from properties where id = %s", (property_id,)
    ).fetchone()
    return PropertySize(*row) if row else None


def rent_research_for(conn: psycopg.Connection, property_id: int, year: int) -> dict | None:
    rows = _dicts(conn, """select recommended_rent, report_path, created_at from rent_research
                           where property_id = %s and year = %s""", (property_id, year))
    return rows[0] if rows else None


def rent_research_history(conn: psycopg.Connection, property_id: int) -> list[tuple[int, Decimal]]:
    rows = conn.execute(
        """select year, recommended_rent from rent_research
           where property_id = %s and recommended_rent is not null order by year""",
        (property_id,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def upsert_rent_research(
    conn: psycopg.Connection,
    *,
    property_id: int,
    year: int,
    comps: str,               # JSON text
    recommended_rent: Decimal,
    rationale: str,
    report_path: str,
) -> None:
    conn.execute(
        """insert into rent_research (property_id, year, comps, recommended_rent, rationale, report_path)
           values (%s, %s, %s::jsonb, %s, %s, %s)
           on conflict (property_id, year) do update set
             comps = excluded.comps, recommended_rent = excluded.recommended_rent,
             rationale = excluded.rationale, report_path = excluded.report_path,
             created_at = now()""",
        (property_id, year, comps, recommended_rent, rationale, report_path),
    )


# --- Web app: job runs and read-only views ------------------------------------------------


def ping(conn: psycopg.Connection) -> None:
    """Cheap query; the scheduler runs it daily so a free Supabase project stays active."""
    conn.execute("select 1").fetchone()


def start_job_run(conn: psycopg.Connection, job: str, params_json: str, trigger: str) -> int:
    run_id = conn.execute(
        "insert into job_runs (job, params, trigger) values (%s, %s::jsonb, %s) returning id",
        (job, params_json, trigger),
    ).fetchone()[0]
    conn.commit()
    return run_id


def finish_job_run(
    conn: psycopg.Connection, run_id: int, *, ok: bool, summary: str, outputs: list[str], log: str
) -> None:
    conn.execute(
        """update job_runs set status = %s, finished_at = now(), summary = %s, outputs = %s,
           log = %s where id = %s""",
        ("success" if ok else "failed", summary, outputs, log, run_id),
    )
    conn.commit()


def fail_stale_job_runs(conn: psycopg.Connection) -> None:
    """Runs left 'running' by a container restart can never finish; mark them failed."""
    conn.execute("""update job_runs set status = 'failed', finished_at = now(),
                    summary = 'Interrupted (app restarted)' where status = 'running'""")
    conn.commit()


def _dicts(conn: psycopg.Connection, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def job_runs(conn: psycopg.Connection, limit: int = 50, job: str | None = None) -> list[dict]:
    return _dicts(conn, """select id, job, trigger, status, started_at, finished_at, summary
                           from job_runs where %s::text is null or job = %s
                           order by id desc limit %s""", (job, job, limit))


def mark_research_saved(conn: psycopg.Connection, run_id: int, report: str, note: str) -> None:
    """After a dry-run recommendation is saved: the run no longer counts as a dry run."""
    conn.execute(
        """update job_runs set params = params || '{"dry_run": false}'::jsonb,
                  outputs = array[%s],
                  summary = replace(summary, 'Dry run: recommendation not saved.', %s)
           where id = %s""",
        (report, note, run_id),
    )


def job_run(conn: psycopg.Connection, run_id: int) -> dict | None:
    rows = _dicts(conn, "select * from job_runs where id = %s", (run_id,))
    return rows[0] if rows else None


def last_job_run(conn: psycopg.Connection, job: str, trigger: str) -> dict | None:
    rows = _dicts(conn, """select * from job_runs where job = %s and trigger = %s
                           order by id desc limit 1""", (job, trigger))
    return rows[0] if rows else None


def last_research_run(conn: psycopg.Connection, property_id: int) -> dict | None:
    """The newest research-rent run for a property, however it was started."""
    rows = _dicts(conn, """select id, status, trigger, started_at from job_runs
                           where job = 'research-rent' and params->>'property_id' = %s
                           order by id desc limit 1""", (str(property_id),))
    return rows[0] if rows else None


def research_was_scheduled(conn: psycopg.Connection, property_id: int, year: int) -> bool:
    """Whether the scheduler already started research-rent for this property and year."""
    row = conn.execute(
        """select 1 from job_runs
           where job = 'research-rent' and trigger = 'schedule'
             and params->>'property_id' = %s and params->>'year' = %s
           limit 1""",
        (str(property_id), str(year)),
    ).fetchone()
    return row is not None


_RENT_ROWS = """select r.id, r.paid_on, r.amount, r.payer, r.needs_review, r.removed_at,
                       l.property_id, p.address
                from rent_payments r left join leases l on l.id = r.lease_id
                left join properties p on p.id = l.property_id"""
_EXPENSE_ROWS = """select e.id, e.spent_on, e.amount, e.schedule_e_category::text as category,
                          e.vendor, e.description, e.receipt_path, e.needs_review, e.removed_at,
                          e.property_id, p.address
                   from expenses e left join properties p on p.id = e.property_id"""


def rent_payments_for_year(conn: psycopg.Connection, year: int, *,
                           include_removed: bool = False) -> list[dict]:
    return _dicts(conn, _RENT_ROWS + """ where extract(year from r.paid_on) = %s
                                         and (%s or r.removed_at is null)
                                         order by r.paid_on desc""", (year, include_removed))


def expenses_list(conn: psycopg.Connection, year: int, *,
                  include_removed: bool = False) -> list[dict]:
    return _dicts(conn, _EXPENSE_ROWS + """ where extract(year from e.spent_on) = %s
                                            and (%s or e.removed_at is null)
                                            order by e.spent_on desc""", (year, include_removed))


def year_totals(conn: psycopg.Connection, year: int) -> dict:
    row = _dicts(conn, """select
        (select coalesce(sum(amount), 0) from rent_payments
         where extract(year from paid_on) = %s and removed_at is null) as rent,
        (select coalesce(sum(amount), 0) from expenses
         where extract(year from spent_on) = %s and removed_at is null) as expenses,
        (select count(*) from rent_payments where needs_review and removed_at is null) +
        (select count(*) from expenses where needs_review and removed_at is null)
          as needs_review""", (year, year))
    return row[0]


# --- Review: fix, remove or restore one recorded row ----------------------------------------

_TABLES = {"rent": "rent_payments", "expense": "expenses"}


def review_count(conn: psycopg.Connection) -> int:
    return conn.execute(
        """select (select count(*) from rent_payments where needs_review and removed_at is null)
                + (select count(*) from expenses where needs_review and removed_at is null)"""
    ).fetchone()[0]


def rent_payments_to_review(conn: psycopg.Connection) -> list[dict]:
    return _dicts(conn, _RENT_ROWS + " where r.needs_review and r.removed_at is null "
                                     "order by r.paid_on")


def expenses_to_review(conn: psycopg.Connection) -> list[dict]:
    return _dicts(conn, _EXPENSE_ROWS + " where e.needs_review and e.removed_at is null "
                                        "order by e.spent_on")


def rent_payment(conn: psycopg.Connection, row_id: int) -> dict | None:
    rows = _dicts(conn, _RENT_ROWS + " where r.id = %s", (row_id,))
    return rows[0] if rows else None


def expense(conn: psycopg.Connection, row_id: int) -> dict | None:
    rows = _dicts(conn, _EXPENSE_ROWS + " where e.id = %s", (row_id,))
    return rows[0] if rows else None


def update_rent_payment(conn: psycopg.Connection, row_id: int, *, lease_id: int | None,
                        amount: Decimal, paid_on: date, payer: str | None,
                        needs_review: bool) -> bool:
    """Returns False if the row doesn't exist or was removed."""
    cur = conn.execute(
        """update rent_payments set lease_id = %s, amount = %s, paid_on = %s, payer = %s,
                  needs_review = %s
           where id = %s and removed_at is null""",
        (lease_id, amount, paid_on, payer, needs_review, row_id))
    return cur.rowcount == 1


def update_expense(conn: psycopg.Connection, row_id: int, *, property_id: int | None,
                   amount: Decimal, spent_on: date, vendor: str | None,
                   description: str | None, category: str) -> bool:
    """Also clears the review flag. Returns False if the row doesn't exist or was removed."""
    cur = conn.execute(
        """update expenses set property_id = %s, amount = %s, spent_on = %s, vendor = %s,
                  description = %s, schedule_e_category = %s, needs_review = false
           where id = %s and removed_at is null""",
        (property_id, amount, spent_on, vendor, description, category, row_id))
    return cur.rowcount == 1


def set_removed(conn: psycopg.Connection, kind: str, row_id: int, removed: bool) -> bool:
    """Remove or restore a row. Returns False if it was already in that state (or missing)."""
    cur = conn.execute(
        f"""update {_TABLES[kind]} set removed_at = case when %s then now() end
            where id = %s and (removed_at is not null) <> %s""",
        (removed, row_id, removed))
    return cur.rowcount == 1


def rent_research_list(conn: psycopg.Connection) -> list[dict]:
    return _dicts(conn, """select rr.year, rr.recommended_rent, rr.rationale, rr.report_path,
                                  rr.created_at, p.address,
                                  jsonb_array_length(rr.comps) as comp_count
                           from rent_research rr join properties p on p.id = rr.property_id
                           order by rr.year desc, p.address""")


def leases_list(conn: psycopg.Connection) -> list[dict]:
    return _dicts(conn, """select l.id, p.address, l.start_date, l.end_date, l.monthly_rent,
                                  l.original_lease_date, l.docx_path, l.pdf_path,
                                  l.awaiting_signature,
                                  string_agg(t.full_name, ', ' order by t.id) as tenants
                           from leases l join properties p on p.id = l.property_id
                           left join lease_tenants lt on lt.lease_id = l.id
                           left join tenants t on t.id = lt.tenant_id
                           group by l.id, p.address order by l.start_date desc""")


def properties_list(conn: psycopg.Connection) -> list[dict]:
    return _dicts(conn, """select id, address, unit, city, state, zip, property_type, beds,
                                  baths, sqft from properties order by id""")


def reference_documents_list(conn: psycopg.Connection) -> list[dict]:
    return _dicts(conn, """select path, category, title, pages, length(content) as chars
                           from reference_documents order by category, path""")


def reference_document(conn: psycopg.Connection, path: str) -> dict | None:
    rows = _dicts(conn, """select path, title, pages, content from reference_documents
                           where path = %s""", (path,))
    return rows[0] if rows else None


# --- Chat history -------------------------------------------------------------------------


def create_chat(conn: psycopg.Connection, title: str, model: str) -> int:
    return conn.execute("insert into chats (title, model) values (%s, %s) returning id",
                        (title, model)).fetchone()[0]


def add_chat_message(conn: psycopg.Connection, chat_id: int, role: str, content: str, *,
                     attachments: list[str] | None = None, proposals_json: str = "[]",
                     model: str | None = None) -> int:
    msg_id = conn.execute(
        """insert into chat_messages (chat_id, role, content, attachments, proposals, model)
           values (%s, %s, %s, %s, %s::jsonb, %s) returning id""",
        (chat_id, role, content, attachments or None, proposals_json, model),
    ).fetchone()[0]
    conn.execute("update chats set updated_at = now() where id = %s", (chat_id,))
    return msg_id


def update_chat(conn: psycopg.Connection, chat_id: int, *, model: str,
                sdk_session_id: str | None) -> None:
    conn.execute("""update chats set model = %s, updated_at = now(),
                    sdk_session_id = coalesce(%s, sdk_session_id) where id = %s""",
                 (model, sdk_session_id, chat_id))


def chats_list(conn: psycopg.Connection, search: str | None = None, limit: int = 100) -> list[dict]:
    if search:
        like = f"%{search}%"
        return _dicts(conn, """select c.id, c.title, c.updated_at from chats c
                               where c.title ilike %s or exists (select 1 from chat_messages m
                                 where m.chat_id = c.id and m.content ilike %s)
                               order by c.updated_at desc limit %s""", (like, like, limit))
    return _dicts(conn, "select id, title, updated_at from chats order by updated_at desc limit %s",
                  (limit,))


def chat(conn: psycopg.Connection, chat_id: int) -> dict | None:
    rows = _dicts(conn, "select * from chats where id = %s", (chat_id,))
    return rows[0] if rows else None


def chat_messages(conn: psycopg.Connection, chat_id: int) -> list[dict]:
    return _dicts(conn, "select * from chat_messages where chat_id = %s order by id", (chat_id,))


def chat_message(conn: psycopg.Connection, chat_id: int, msg_id: int) -> dict | None:
    rows = _dicts(conn, "select * from chat_messages where chat_id = %s and id = %s",
                  (chat_id, msg_id))
    return rows[0] if rows else None


def set_message_proposals(conn: psycopg.Connection, msg_id: int, proposals_json: str) -> None:
    conn.execute("update chat_messages set proposals = %s::jsonb where id = %s",
                 (proposals_json, msg_id))


def record_claude_cost(db_url: str, source: str, model: str, cost_usd: float | None) -> None:
    """Log one Claude call's cost. Best effort: a logging failure never fails the caller."""
    if cost_usd is None:
        return
    try:
        with connect(db_url) as conn:
            conn.execute("insert into claude_usage (source, model, cost_usd) values (%s, %s, %s)",
                         (source, model, cost_usd))
    except psycopg.Error as exc:
        log.warning("Could not record Claude cost: %s", exc)


def month_claude_cost(conn: psycopg.Connection, today: date) -> dict:
    """Cost and call count for the calendar month containing `today`."""
    return _dicts(conn, """select coalesce(sum(cost_usd), 0) as cost, count(*) as calls
                           from claude_usage
                           where used_at >= date_trunc('month', %s::date)
                             and used_at < date_trunc('month', %s::date) + interval '1 month'""",
                  (today, today))[0]
