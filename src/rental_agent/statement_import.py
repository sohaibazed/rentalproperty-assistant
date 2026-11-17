"""Import rent and expenses from a bank or card statement attached in the chat.

The chat model reads the statement (CSV or PDF) and, following the import-statement skill,
lists the rental-related transactions. This module checks them (`validate`), marks ones that
look already recorded (`mark_existing`), and after the owner ticks rows and confirms, saves
them (`save`).

Each row gets a stable id from its date, amount and description, so importing the same
statement again, or an overlapping one, never adds a row twice.
"""

import hashlib
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from rental_agent import db
from rental_agent.extract import SCHEDULE_E_CATEGORIES

MAX_ROWS = 300
MAX_AMOUNT = Decimal(100000)
MATCH_DAYS = 3  # a recorded payment this close in date, same amount, is probably the same one

TRANSACTIONS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD, as posted"},
            "amount": {"type": "number", "description": "positive, in dollars"},
            "description": {"type": "string", "description": "the statement's description, "
                            "as printed"},
            "kind": {"type": "string", "enum": ["rent", "expense"]},
            "category": {"type": ["string", "null"], "enum": [*SCHEDULE_E_CATEGORIES, None],
                         "description": "Schedule E category for an expense; null for rent"},
            "property_id": {"type": ["integer", "null"],
                            "description": "required for rent; null = LLC-wide expense"},
            "payer_or_vendor": {"type": ["string", "null"]},
            "include": {"type": "boolean", "description": "false when unsure it's a rental "
                        "transaction; the owner decides"},
            "note": {"type": ["string", "null"], "description": "why it's unsure, or what to "
                     "check"},
        },
        "required": ["date", "amount", "description", "kind", "category", "property_id",
                     "payer_or_vendor", "include", "note"],
    },
}


def _row_id(on: str, amount: str, description: str, occurrence: int) -> str:
    key = f"{on}|{amount}|{' '.join(description.lower().split())}|{occurrence}"
    return "stmt:" + hashlib.sha256(key.encode()).hexdigest()[:24]


def validate(rows: list[dict[str, Any]], property_ids: set[int], today: date) -> list[dict]:
    """Check each transaction; returns clean rows with ids, or raises ValueError."""
    if not rows:
        raise ValueError("no transactions to import")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"at most {MAX_ROWS} transactions per import")
    seen: Counter[tuple[str, str, str]] = Counter()
    out = []
    for i, raw in enumerate(rows, 1):
        where = f"row {i}"
        try:
            on = date.fromisoformat(str(raw["date"]))
            amount = abs(Decimal(str(raw["amount"]))).quantize(Decimal("0.01"))
        except (KeyError, ValueError, InvalidOperation):
            raise ValueError(f"{where}: date must be YYYY-MM-DD and amount a number") from None
        if on > today or today - on > timedelta(days=3 * 365):
            raise ValueError(f"{where}: date {on} must be within the last three years")
        if not Decimal(0) < amount <= MAX_AMOUNT:
            raise ValueError(f"{where}: amount must be positive and under $100,000")
        description = " ".join(str(raw.get("description") or "").split())[:300]
        if not description:
            raise ValueError(f"{where}: description is required")
        kind = raw.get("kind")
        category = raw.get("category")
        property_id = raw.get("property_id")
        if kind == "rent":
            if property_id not in property_ids:
                raise ValueError(f"{where}: rent needs the property_id it was paid for")
            category = None
        elif kind == "expense":
            if category not in SCHEDULE_E_CATEGORIES:
                raise ValueError(f"{where}: category must be one of "
                                 + ", ".join(SCHEDULE_E_CATEGORIES))
            if property_id is not None and property_id not in property_ids:
                raise ValueError(f"{where}: unknown property_id {property_id}")
        else:
            raise ValueError(f"{where}: kind must be rent or expense")
        key = (on.isoformat(), str(amount), description.lower())
        seen[key] += 1
        out.append({
            "id": _row_id(on.isoformat(), str(amount), description, seen[key]),
            "date": on.isoformat(), "amount": str(amount), "description": description,
            "kind": kind, "category": category, "property_id": property_id,
            "party": " ".join(str(raw.get("payer_or_vendor") or "").split())[:200] or None,
            "include": bool(raw.get("include", True)),
            "note": " ".join(str(raw.get("note") or "").split())[:300] or None,
        })
    return out


def mark_existing(conn: psycopg.Connection, rows: list[dict]) -> list[dict]:
    """Untick rows that are already recorded: imported before (even if the owner removed the
    row since), or (same amount, within a few days) recorded by the email sync or by hand.
    Sets row["existing"] to the reason."""
    for row in rows:
        table, date_col = (("rent_payments", "paid_on") if row["kind"] == "rent"
                           else ("expenses", "spent_on"))
        hit = conn.execute(
            f"""select {date_col}, coalesce(source_message_id = %s, false) as same,
                       removed_at is not null
                from {table}
                where source_message_id = %s
                   or (removed_at is null and amount = %s
                       and {date_col} between %s::date - %s and %s::date + %s)
                order by same desc limit 1""",
            (row["id"], row["id"], row["amount"], row["date"], MATCH_DAYS, row["date"],
             MATCH_DAYS),
        ).fetchone()
        row["existing"] = None
        if hit:
            on, same, removed = hit
            page = "Rent" if row["kind"] == "rent" else "Expenses"
            row["existing"] = (f"removed earlier; restore it on the {page} page" if removed
                               else "already imported" if same
                               else f"same amount already recorded on {on:%b %-d}")
            row["include"] = False
    return rows


def save(conn: psycopg.Connection, rows: list[dict], selected: set[str]) -> dict[str, int]:
    """Save the selected rows. Re-imports are skipped by id. The caller commits."""
    counts = {"rent": 0, "expense": 0, "skipped": 0}
    for row in rows:
        if row["id"] not in selected:
            continue
        on, amount = date.fromisoformat(row["date"]), Decimal(row["amount"])
        if row["kind"] == "rent":
            lease_id = db.active_lease_id(conn, row["property_id"], on)
            added = db.insert_rent_payment(conn, lease_id=lease_id, amount=amount, paid_on=on,
                                           payer=row["party"], message_id=row["id"],
                                           needs_review=lease_id is None)
        else:
            added = db.insert_expense(conn, property_id=row["property_id"], amount=amount,
                                      spent_on=on, vendor=row["party"],
                                      description=row["description"], category=row["category"],
                                      receipt_path=None, message_id=row["id"],
                                      needs_review=False)
        counts[row["kind"] if added else "skipped"] += 1
    return counts
