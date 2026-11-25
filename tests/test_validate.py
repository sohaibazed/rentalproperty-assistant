from datetime import date
from decimal import Decimal

import pytest

from rental_agent.categories import SCHEDULE_E_CATEGORIES as CATS
from rental_agent.validate import ValidationError, validate

TODAY = date(2026, 11, 16)


def _raw(**overrides):
    base = {
        "kind": "expense", "amount": 123.456, "date": "2026-11-10", "counterparty": "Home Depot",
        "description": "faucet", "category": "repairs", "property_id": 1, "confidence": 0.95,
        "reason": "receipt",
    }
    return base | overrides


def _v(raw, ids=frozenset({1})):
    return validate(raw, email_date=date(2026, 11, 11), known_property_ids=set(ids),
                    categories=CATS, today=TODAY)


def test_clean_expense_is_stored_without_review():
    c = _v(_raw())
    assert c.amount == Decimal("123.46")
    assert c.on == date(2026, 11, 10)
    assert not c.needs_review


def test_statement_amount_always_needs_review():
    c = _v(_raw(from_statement=True, counterparty="Willow Crest HOA", category="other"))
    assert c.needs_review
    assert "amount is from a statement; confirm it was paid and the date" in c.review_reasons
    assert not _v(_raw(from_statement=False)).needs_review


def test_ignore_passes_through():
    assert _v(_raw(kind="ignore", amount=None)).kind == "ignore"


@pytest.mark.parametrize("amount", [None, 0, -5, "abc", 200000, True])
def test_bad_amounts_rejected(amount):
    with pytest.raises(ValidationError):
        _v(_raw(amount=amount))


def test_future_and_ancient_dates_rejected():
    with pytest.raises(ValidationError):
        _v(_raw(date="2026-12-01"))
    with pytest.raises(ValidationError):
        _v(_raw(date="2020-01-01"))


def test_missing_date_falls_back_to_email_date():
    assert _v(_raw(date=None)).on == date(2026, 11, 11)


def test_invalid_category_rejected():
    with pytest.raises(ValidationError):
        _v(_raw(category="groceries"))


def test_low_confidence_and_unknown_property_flag_review():
    c = _v(_raw(confidence=0.5, property_id=99))
    assert c.needs_review
    assert c.property_id is None
    assert len(c.review_reasons) == 2


def test_rent_without_property_needs_review_and_drops_category():
    c = _v(_raw(kind="rent", property_id=None, category="repairs"))
    assert c.needs_review
    assert c.category is None
