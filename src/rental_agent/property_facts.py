"""Set a property's facts from the chat's property card.

Rent research and the tax protest need the type, bedrooms, bathrooms and square feet. The tax
report estimates depreciation from the purchase date and the cost basis. A lease states none
of these, so the owner gives them in the chat and confirms the card.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from rental_agent import db

FIELDS = ("property_type", "beds", "baths", "sqft", "purchase_date", "cost_basis")
LABELS = {"property_type": "Type", "beds": "Beds", "baths": "Baths", "sqft": "Sq ft",
          "purchase_date": "Purchase date", "cost_basis": "Cost basis without land"}
MAX_ROOMS = 20
MAX_SQFT = 50000
MAX_COST_BASIS = Decimal(100_000_000)
EARLIEST_PURCHASE = date(1900, 1, 1)


def _number(value: Any, field: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{field} must be a number") from None
    if not number.is_finite():
        raise ValueError(f"{field} must be a number")
    return number


def _plain(number: Decimal) -> str:
    """3.0 as "3" and 2.5 as "2.5", so a saved value and a typed one compare equal."""
    return format(number.normalize(), "f")


def _rooms(value: Any, field: str) -> str | None:
    number = _number(value, field)
    if number is None:
        return None
    if not 0 <= number <= MAX_ROOMS or number != number.quantize(Decimal("0.1")):
        raise ValueError(f"{field} must be between 0 and {MAX_ROOMS}, with at most one decimal")
    return _plain(number)


def validate(raw: dict[str, Any], today: date) -> dict[str, Any]:
    """Check facts from the chat or from the card's form. An empty value means "not known".
    Returns clean, JSON-safe values or raises ValueError."""
    sqft = _number(raw.get("sqft"), "sqft")
    if sqft is not None and (sqft != sqft.to_integral_value() or not 100 <= sqft <= MAX_SQFT):
        raise ValueError(f"sqft must be a whole number between 100 and {MAX_SQFT:,}")

    purchased = raw.get("purchase_date") or None
    if purchased is not None:
        try:
            purchased = date.fromisoformat(str(purchased))
        except ValueError:
            raise ValueError("purchase_date must be YYYY-MM-DD") from None
        if not EARLIEST_PURCHASE <= purchased <= today:
            raise ValueError(f"purchase_date {purchased} can't be in the future or before 1900")

    basis = _number(raw.get("cost_basis"), "cost_basis")
    if basis is not None and not 0 < basis <= MAX_COST_BASIS:
        raise ValueError("cost_basis must be positive and under $100,000,000")

    return {"property_type": " ".join(str(raw.get("property_type") or "").split())[:60] or None,
            "beds": _rooms(raw.get("beds"), "beds"),
            "baths": _rooms(raw.get("baths"), "baths"),
            "sqft": None if sqft is None else int(sqft),
            "purchase_date": None if purchased is None else purchased.isoformat(),
            "cost_basis": None if basis is None else str(basis.quantize(Decimal("0.01")))}


def _show(field: str, value: Any) -> str:
    if value is None:
        return "-"
    if field == "sqft":
        return f"{value:,}"
    return f"${Decimal(value):,.2f}" if field == "cost_basis" else str(value)


def describe(facts: dict[str, Any]) -> str:
    return " · ".join(f"{LABELS[f]} {_show(f, facts[f])}" for f in FIELDS)


def _property(conn: psycopg.Connection, property_id: Any) -> dict:
    for row in db.properties_list(conn):
        if row["id"] == property_id:
            return row
    raise ValueError(f"no property with id {property_id}; look it up with list_properties")


def propose(conn: psycopg.Connection, args: dict[str, Any], today: date) -> dict[str, Any]:
    """Check the facts the chat suggests and return the data for the confirm card.
    Changes nothing."""
    row = _property(conn, args.get("property_id"))
    changes = args.get("changes") or {}
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise ValueError(f"a property has no fact {', '.join(unknown)}; use {', '.join(FIELDS)}")
    before = validate(row, today)
    after = validate({**before, **changes}, today)
    diff = [{"field": LABELS[f], "from": _show(f, before[f]), "to": _show(f, after[f])}
            for f in FIELDS if before[f] != after[f]]
    if not diff:
        raise ValueError("nothing to change: the property already has those facts")
    return {"property_id": row["id"], "address": row["address"], "facts": after,
            "changes": diff, "reason": " ".join(str(args.get("reason") or "").split())[:300]}


def save(conn: psycopg.Connection, property_id: int, raw: dict[str, Any], today: date) -> str:
    """The owner confirmed the card, perhaps after editing it. Returns a one-line result.
    The caller commits."""
    facts = validate(raw, today)
    if not db.update_property_facts(conn, property_id, **facts):
        raise ValueError(f"no property with id {property_id}")
    return f"Saved: {describe(facts)}."
