"""Property facts: checking the values from the chat or the card's form (no database)."""

from datetime import date

import pytest

from rental_agent import property_facts

TODAY = date(2026, 11, 17)
FACTS = {"property_type": "  Town   house ", "beds": 3.0, "baths": "2.5", "sqft": "1950",
         "purchase_date": "2021-05-14", "cost_basis": 250000}


def test_values_are_cleaned():
    assert property_facts.validate(FACTS, TODAY) == {
        "property_type": "Town house", "beds": "3", "baths": "2.5", "sqft": 1950,
        "purchase_date": "2021-05-14", "cost_basis": "250000.00"}


def test_an_empty_value_means_not_known():
    empty = property_facts.validate({"beds": "", "sqft": None, "action": "confirm"}, TODAY)
    assert set(empty) == set(property_facts.FIELDS) and not any(empty.values())


@pytest.mark.parametrize("change,error", [
    ({"beds": "2.25"}, "beds must be between 0 and 20"),
    ({"baths": 21}, "baths must be between 0 and 20"),
    ({"beds": "three"}, "beds must be a number"),
    ({"sqft": 50}, "sqft must be a whole number"),
    ({"sqft": "1950.5"}, "sqft must be a whole number"),
    ({"purchase_date": "05/14/2021"}, "purchase_date must be YYYY-MM-DD"),
    ({"purchase_date": "2026-11-18"}, "can't be in the future"),
    ({"cost_basis": 0}, "cost_basis must be positive"),
    ({"cost_basis": "NaN"}, "cost_basis must be a number"),
])
def test_bad_values_are_refused(change, error):
    with pytest.raises(ValueError, match=error):
        property_facts.validate({**FACTS, **change}, TODAY)


def test_result_line_lists_every_fact():
    facts = property_facts.validate({**FACTS, "purchase_date": None}, TODAY)
    assert property_facts.describe(facts) == (
        "Type Town house · Beds 3 · Baths 2.5 · Sq ft 1,950 · Purchase date - · "
        "Cost basis without land $250,000.00")
