"""Corrections: checking the values for a recorded row (no database)."""

from datetime import date

import pytest

from rental_agent import corrections
from rental_agent.categories import SCHEDULE_E_CATEGORIES as CATS

TODAY = date(2026, 11, 16)
EXPENSE = {"date": "2026-05-10", "amount": "54.2", "vendor": "  Home   Depot ",
           "category": "repairs", "property_id": "1", "description": ""}


def test_form_values_are_cleaned():
    assert corrections.validate("expense", EXPENSE, {1}, CATS, TODAY) == {
        "date": "2026-05-10", "amount": "54.20", "vendor": "Home Depot", "category": "repairs",
        "property_id": 1, "description": None}
    rent = corrections.validate(
        "rent", {"date": "2026-03-02", "amount": 2500, "payer": "Jane", "property_id": ""},
        {1}, CATS, TODAY)
    assert rent == {"date": "2026-03-02", "amount": "2500.00", "payer": "Jane",
                    "property_id": None}


@pytest.mark.parametrize("change,error", [
    ({"date": "03/02/2026"}, "date must be YYYY-MM-DD"),
    ({"date": "2026-11-17"}, "can't be in the future"),
    ({"date": "0202-05-10"}, "before 2000"),
    ({"amount": "0"}, "amount must be positive"),
    ({"amount": "abc"}, "amount a number"),
    ({"amount": "250000"}, "under \\$100,000"),
    ({"category": "groceries"}, "category must be one of"),
    ({"property_id": "7"}, "unknown property_id 7"),
    ({"property_id": "Oak St"}, "unknown property"),
])
def test_bad_values_are_refused(change, error):
    with pytest.raises(ValueError, match=error):
        corrections.validate("expense", {**EXPENSE, **change}, {1}, CATS, TODAY)
