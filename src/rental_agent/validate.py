"""Deterministic checks on model output before anything touches the database."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from rental_agent.extract import SCHEDULE_E_CATEGORIES

REVIEW_CONFIDENCE = 0.8
MAX_AMOUNT = Decimal(100000)
MAX_AGE = timedelta(days=730)


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    kind: str                 # "rent" | "expense" | "ignore"
    amount: Decimal | None
    on: date | None
    counterparty: str | None
    description: str | None
    category: str | None
    property_id: int | None
    needs_review: bool
    review_reasons: tuple[str, ...]


def validate(
    raw: dict[str, Any], *, email_date: date, known_property_ids: set[int], today: date
) -> Candidate:
    """Raise ValidationError for output that can't be stored; flag doubtful-but-storable items."""
    kind = raw.get("kind")
    if kind not in ("rent", "expense", "ignore"):
        raise ValidationError(f"unknown kind: {kind!r}")
    if kind == "ignore":
        return Candidate("ignore", None, None, None, None, None, None, False, ())

    reasons: list[str] = []

    amount = _parse_amount(raw.get("amount"))

    on = _parse_date(raw.get("date")) or email_date
    if on > today:
        raise ValidationError(f"date {on} is in the future")
    if today - on > MAX_AGE:
        raise ValidationError(f"date {on} is more than two years old")

    category = raw.get("category")
    if kind == "expense":
        if category not in SCHEDULE_E_CATEGORIES:
            raise ValidationError(f"invalid expense category: {category!r}")
        if category == "other":
            reasons.append("category is 'other'")
    else:
        category = None

    property_id = raw.get("property_id")
    if property_id is not None and property_id not in known_property_ids:
        reasons.append(f"unknown property_id {property_id}")
        property_id = None
    if kind == "rent" and property_id is None:
        reasons.append("rent not matched to a property")

    if raw.get("from_statement"):
        # A statement says what is due, not that it was paid; the tax report counts payments.
        reasons.append("amount is from a statement; confirm it was paid and the date")

    confidence = raw.get("confidence", 0)
    if not isinstance(confidence, (int, float)) or confidence < REVIEW_CONFIDENCE:
        reasons.append(f"low confidence ({confidence})")

    return Candidate(
        kind=kind,
        amount=amount,
        on=on,
        counterparty=_clean(raw.get("counterparty")),
        description=_clean(raw.get("description")),
        category=category,
        property_id=property_id,
        needs_review=bool(reasons),
        review_reasons=tuple(reasons),
    )


def _parse_amount(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValidationError("missing amount")
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValidationError(f"bad amount: {value!r}") from exc
    if amount <= 0:
        raise ValidationError(f"amount must be positive: {amount}")
    if amount > MAX_AMOUNT:
        raise ValidationError(f"amount {amount} exceeds sanity limit {MAX_AMOUNT}")
    return amount


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"bad date: {value!r}") from exc


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:200] or None
