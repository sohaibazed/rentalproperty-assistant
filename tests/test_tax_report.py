import csv
from datetime import date
from decimal import Decimal as D

from openpyxl import load_workbook

from rental_agent.db import ExpenseRow, LeaseRow, Property, RentRow
from rental_agent.jobs.tax_report import build_report, write_csv, write_xlsx

PROPS = [Property(1, "12 Oak St", None), Property(2, "9 Elm Ave", None)]
LEASES = [
    LeaseRow(10, 1, date(2026, 1, 1), date(2026, 12, 31), D(1800)),
    LeaseRow(20, 2, date(2026, 7, 1), date(2027, 6, 30), D(1500)),
]


def _rents():
    rows, i = [], 0
    for month in range(1, 13):
        if month == 3:
            continue  # missing
        i += 1
        amount = D(1000) if month == 6 else D(1800)  # June short
        rows.append(RentRow(i, 1, 10, amount, date(2026, month, 2), "Jane", False))
    for month in range(7, 13):
        i += 1
        rows.append(RentRow(i, 2, 20, D(1500), date(2026, month, 3), "Sam", False))
    rows.append(RentRow(99, None, None, D(500), date(2026, 5, 5), "Unknown", True))
    return rows


EXPENSES = [
    ExpenseRow(1, 1, D(3000), date(2026, 4, 1), "Roofer", "roof patch", "repairs", None, False),
    ExpenseRow(2, 1, D(1200), date(2026, 2, 1), "Insurer", None, "insurance", "r/ins.pdf", False),
    ExpenseRow(3, 2, D(2400), date(2026, 10, 1), "County", None, "taxes", None, False),
    ExpenseRow(4, None, D(600), date(2026, 3, 1), "CPA", None, "legal_professional", None, False),
    ExpenseRow(5, 1, D(50), date(2026, 8, 1), "Store", None, "supplies", None, True),
]


def _report(today=date(2027, 1, 15)):
    return build_report(2026, PROPS, _rents(), EXPENSES, LEASES, today)


def test_totals_per_property():
    r = _report()
    assert r.rent == {1: D(19000), 2: D(9000), None: D(500)}
    assert r.total_expenses(1) == D(4250)
    assert r.total_expenses(2) == D(2400)
    assert r.total_expenses(None) == D(600)
    assert [label for _, label in r.columns] == ["12 Oak St", "9 Elm Ave", "LLC-wide / unassigned"]


def test_flags():
    kinds = sorted(f.kind for f in _report().flags)
    assert kinds == sorted([
        "needs review", "needs review", "possible capital expense", "allocate",
        "missing rent", "short rent", "note",
    ])
    gaps = {f.detail[:8] for f in _report().flags if f.kind.endswith("rent")}
    assert gaps == {"Mar 2026", "Jun 2026"}


def test_unfinished_months_not_flagged_as_missing():
    # Mid-November: Nov and Dec haven't ended, so no gap flags for them even with no payments.
    rents = [r for r in _rents() if r.paid_on.month < 11]
    r = build_report(2026, PROPS, rents, EXPENSES, LEASES, date(2026, 11, 16))
    assert not any("Nov 2026" in f.detail or "Dec 2026" in f.detail for f in r.flags)


def test_outputs(tmp_path):
    r = _report()
    xlsx, csv_path = tmp_path / "s.xlsx", tmp_path / "t.csv"
    write_xlsx(r, xlsx)
    write_csv(r, csv_path)

    wb = load_workbook(xlsx)
    assert wb.sheetnames == ["Review", "Schedule E", "Rent", "Expenses"]
    rows = {row[0]: row for row in wb["Schedule E"].iter_rows(min_row=2, values_only=True)}
    assert rows["3"][-1] == 28500       # total rents
    assert rows["14"][2] == 3000        # repairs, Oak St
    assert rows["20"][-1] == 7250       # total expenses
    assert rows["21"][2:] == (14750, 6600, -100, 21250)

    with csv_path.open() as fh:
        assert len(list(csv.DictReader(fh))) == len(_rents()) + len(EXPENSES)
