"""Statement import: validation and stable row ids (no database)."""

from datetime import date

import pytest

from rental_agent import chat, statement_import
from rental_agent.categories import SCHEDULE_E_CATEGORIES as CATS

TODAY = date(2026, 11, 16)


def tx(**over):
    row = {"date": "2026-09-01", "amount": 2500, "description": "Zelle from MAN W BLOUNT",
           "kind": "rent", "category": None, "property_id": 1, "payer_or_vendor": "Man W Blount",
           "include": True, "note": None}
    return {**row, **over}


def test_ids_are_stable_and_distinguish_identical_rows():
    rows = statement_import.validate([tx(), tx(), tx(amount=-2500.0)], {1}, CATS, TODAY)
    assert rows[0]["id"] != rows[1]["id"]                # two identical payments, same day
    again = statement_import.validate([tx(), tx()], {1}, CATS, TODAY)
    assert [r["id"] for r in again] == [rows[0]["id"], rows[1]["id"]]   # re-import: same ids
    assert rows[2]["amount"] == "2500.00"                # sign dropped
    assert rows[0]["category"] is None and rows[0]["party"] == "Man W Blount"


def test_expense_rows():
    [row] = statement_import.validate(
        [tx(kind="expense", category="repairs", amount=100, property_id=None,
            description="Zelle to Asbel Amin Lopez AC repair", include=False, note="check")],
        {1}, CATS, TODAY)
    assert (row["kind"], row["category"], row["property_id"], row["include"]) == \
        ("expense", "repairs", None, False)


@pytest.mark.parametrize("over,error", [
    ({"property_id": None}, "rent needs the property_id"),
    ({"kind": "transfer"}, "kind must be rent or expense"),
    ({"kind": "expense", "category": "mortgage"}, "category must be one of"),
    ({"date": "2026-12-01"}, "within the last three years"),
    ({"amount": 0}, "positive"),
    ({"description": " "}, "description is required"),
])
def test_bad_rows_rejected(over, error):
    with pytest.raises(ValueError, match=error):
        statement_import.validate([tx(**over)], {1}, CATS, TODAY)


def test_limits_and_upload_types():
    with pytest.raises(ValueError, match="no transactions"):
        statement_import.validate([], {1}, CATS, TODAY)
    assert chat.check_upload("stmt.csv", b"date,amount\n") == ".csv"
    assert chat.check_upload("stmt.qfx", b"OFXHEADER:100") == ".qfx"
    assert "rental:import-statement" in chat.SKILLS
    assert (chat.PLUGIN_DIR / "skills" / "import-statement" / "SKILL.md").is_file()
