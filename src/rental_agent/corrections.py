"""Fix a recorded rent payment or expense: change its values, remove it, or put it back.

Used by the Review, Rent and Expenses pages and by the chat's correction card, so both check
and save a change the same way.

Rows are never deleted. A removed row is left out of totals and reports, but it keeps its
source id, so the email sync and statement imports don't add it again, and it can be restored.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from rental_agent import db
from rental_agent.extract import SCHEDULE_E_CATEGORIES

KINDS = ("rent", "expense")
FIELDS = {
    "rent": ("date", "amount", "payer", "property_id"),
    "expense": ("date", "amount", "vendor", "category", "property_id", "description"),
}
LABELS = {"date": "Date", "amount": "Amount", "payer": "Payer", "vendor": "Vendor",
          "category": "Category", "property_id": "Property", "description": "Note"}
MAX_AMOUNT = Decimal(100000)
EARLIEST = date(2000, 1, 1)


def load(conn: psycopg.Connection, kind: str, row_id: int) -> dict | None:
    return db.rent_payment(conn, row_id) if kind == "rent" else db.expense(conn, row_id)


def values(kind: str, row: dict) -> dict[str, Any]:
    """A row's editable values, in the shape `validate` returns."""
    if kind == "rent":
        return {"date": row["paid_on"].isoformat(), "amount": str(row["amount"]),
                "payer": row["payer"], "property_id": row["property_id"]}
    return {"date": row["spent_on"].isoformat(), "amount": str(row["amount"]),
            "vendor": row["vendor"], "category": row["category"],
            "property_id": row["property_id"], "description": row["description"]}


def describe(kind: str, row: dict) -> str:
    v = values(kind, row)
    who = v["payer"] if kind == "rent" else v["vendor"]
    what = "rent" if kind == "rent" else v["category"].replace("_", " ")
    return f"{v['date']} · ${Decimal(v['amount']):,.2f} · {what} · {who or '-'}"


def _text(value: Any, limit: int) -> str | None:
    return " ".join(str(value or "").split())[:limit] or None


def validate(kind: str, raw: dict[str, Any], property_ids: set[int], today: date) -> dict[str, Any]:
    """Check values from a form or from the chat; returns clean values or raises ValueError."""
    try:
        on = date.fromisoformat(str(raw.get("date")))
        amount = Decimal(str(raw.get("amount"))).quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation):
        raise ValueError("date must be YYYY-MM-DD and amount a number") from None
    if not EARLIEST <= on <= today:
        raise ValueError(f"date {on} can't be in the future or before 2000")
    if not Decimal(0) < amount <= MAX_AMOUNT:
        raise ValueError("amount must be positive and under $100,000")
    prop = raw.get("property_id")
    try:
        property_id = None if prop in (None, "") else int(prop)
    except (TypeError, ValueError):
        raise ValueError(f"unknown property {prop!r}") from None
    if property_id is not None and property_id not in property_ids:
        raise ValueError(f"unknown property_id {property_id}")
    out: dict[str, Any] = {"date": on.isoformat(), "amount": str(amount),
                           "property_id": property_id}
    if kind == "rent":
        out["payer"] = _text(raw.get("payer"), 200)
        return out
    if raw.get("category") not in SCHEDULE_E_CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(SCHEDULE_E_CATEGORIES)}")
    out["category"] = raw["category"]
    out["vendor"] = _text(raw.get("vendor"), 200)
    out["description"] = _text(raw.get("description"), 500)
    return out


def _lease_id(conn: psycopg.Connection, property_id: int | None, on: date) -> int | None:
    """Rent is matched to the lease that covers its date, as the sync and imports do."""
    if property_id is None:
        return None
    lease_id = db.active_lease_id(conn, property_id, on)
    if lease_id is None:
        raise ValueError(f"no lease for that property covers {on}. Import the lease first, "
                         "or leave the property unmatched")
    return lease_id


def save(conn: psycopg.Connection, kind: str, row_id: int, raw: dict[str, Any],
         today: date) -> str:
    """Save values to a row and mark it checked. Rent with no property stays unmatched and
    stays on the review list. Returns a one-line result. The caller commits."""
    data = validate(kind, raw, {p["id"] for p in db.properties_list(conn)}, today)
    on, amount = date.fromisoformat(data["date"]), Decimal(data["amount"])
    result = f"Saved: ${amount:,.2f} on {on}."
    if kind == "rent":
        lease_id = _lease_id(conn, data["property_id"], on)
        saved = db.update_rent_payment(conn, row_id, lease_id=lease_id, amount=amount,
                                       paid_on=on, payer=data["payer"],
                                       needs_review=lease_id is None)
        if lease_id is None:
            result += " Still unmatched: choose its property to clear the check."
    else:
        saved = db.update_expense(conn, row_id, property_id=data["property_id"], amount=amount,
                                  spent_on=on, vendor=data["vendor"],
                                  description=data["description"], category=data["category"])
    if not saved:
        raise ValueError("that row was removed; restore it first")
    return result


def mark_checked(conn: psycopg.Connection, kind: str, row_id: int, today: date) -> str:
    """The owner looked at a flagged row and it is right as recorded."""
    row = load(conn, kind, row_id)
    if row is None:
        raise ValueError("no such row")
    return save(conn, kind, row_id, values(kind, row), today)


def set_removed(conn: psycopg.Connection, kind: str, row_id: int, removed: bool) -> str:
    if not db.set_removed(conn, kind, row_id, removed):
        raise ValueError("that row was already " + ("removed" if removed else "restored"))
    return ("Removed. It is left out of totals and reports; you can restore it."
            if removed else "Restored.")


def _show(field: str, value: Any, addresses: dict[int, str], kind: str) -> str:
    if field == "property_id":
        if value is None:
            return "unmatched" if kind == "rent" else "LLC-wide"
        return addresses.get(value, f"property {value}")
    if value is None:
        return "-"
    if field == "amount":
        return f"${Decimal(value):,.2f}"
    return str(value).replace("_", " ") if field == "category" else str(value)


def propose(conn: psycopg.Connection, args: dict[str, Any], today: date) -> dict[str, Any]:
    """Check a correction the chat suggests and return the data for its confirm card.
    Changes nothing."""
    kind, action = args.get("kind"), args.get("action")
    if kind not in KINDS or action not in ("update", "remove"):
        raise ValueError("kind must be rent or expense, and action update or remove")
    row = load(conn, kind, args["id"]) if isinstance(args.get("id"), int) else None
    if row is None or row["removed_at"]:
        raise ValueError(f"no {kind} row with id {args.get('id')}; look it up with the list "
                         "tools first")
    card = {"kind": kind, "row_id": row["id"], "action": action,
            "summary": describe(kind, row), "reason": _text(args.get("reason"), 300)}
    if action == "remove":
        return card
    changes = args.get("changes") or {}
    unknown = sorted(set(changes) - set(FIELDS[kind]))
    if unknown:
        raise ValueError(f"{kind} has no field {', '.join(unknown)}; use "
                         + ", ".join(FIELDS[kind]))
    properties = db.properties_list(conn)
    before = values(kind, row)
    after = validate(kind, {**before, **changes}, {p["id"] for p in properties}, today)
    if kind == "rent":
        _lease_id(conn, after["property_id"], date.fromisoformat(after["date"]))
    addresses = {p["id"]: p["address"] for p in properties}
    diff = [{"field": LABELS[f], "from": _show(f, before[f], addresses, kind),
             "to": _show(f, after[f], addresses, kind)}
            for f in FIELDS[kind] if before[f] != after[f]]
    if not diff and not row["needs_review"]:
        raise ValueError("nothing to change: the row already has those values")
    return {**card, "values": after, "changes": diff}


def apply(conn: psycopg.Connection, card: dict[str, Any], today: date) -> str:
    """The owner confirmed a correction card. The caller commits."""
    if card["action"] == "remove":
        return set_removed(conn, card["kind"], card["row_id"], True)
    return save(conn, card["kind"], card["row_id"], card["values"], today)
