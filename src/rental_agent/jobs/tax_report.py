"""tax-report: year-end Schedule E summary from the rent and expense ledger.

Pure SQL + arithmetic; no model call. Output goes to artifacts/<year>/tax/:
- schedule-e-<year>.xlsx  (Review, Schedule E, Rent, Expenses sheets)
- transactions-<year>.csv (every rent and expense row, with receipt paths)

This is a bookkeeping summary for your CPA, not tax advice.
"""

import calendar
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from rental_agent import db, move_out
from rental_agent.categories import SCHEDULE_E_CATEGORIES
from rental_agent.config import Settings

# Schedule E (Form 1040) Part I expense lines, in form order.
SCHEDULE_E_LINES: list[tuple[str, str, str]] = [
    ("5", "Advertising", "advertising"),
    ("6", "Auto and travel", "auto_travel"),
    ("7", "Cleaning and maintenance", "cleaning_maintenance"),
    ("8", "Commissions", "commissions"),
    ("9", "Insurance", "insurance"),
    ("10", "Legal and other professional fees", "legal_professional"),
    ("11", "Management fees", "management_fees"),
    ("12", "Mortgage interest paid to banks, etc.", "mortgage_interest"),
    ("13", "Other interest", "other_interest"),
    ("14", "Repairs", "repairs"),
    ("15", "Supplies", "supplies"),
    ("16", "Taxes", "taxes"),
    ("17", "Utilities", "utilities"),
    ("18", "Depreciation expense or depletion", "depreciation"),
    ("19", "Other", "other"),
]

# IRS de minimis safe harbor (taxpayers without audited financial statements).
DE_MINIMIS_LIMIT = Decimal(2500)
CAPITALIZABLE_CATEGORIES = {"repairs", "supplies", "cleaning_maintenance"}

# Residential rental property (IRS Publication 527): straight line over 27.5 years.
RECOVERY_MONTHS = 330

UNASSIGNED = None  # column key for LLC-wide expenses and rent not matched to a property
MONEY_FORMAT = '"$"#,##0.00'


@dataclass(frozen=True)
class Flag:
    kind: str          # e.g. "needs review", "possible capital expense", "missing rent"
    on: date | None
    amount: Decimal | None
    who: str
    detail: str


@dataclass
class Report:
    year: int
    columns: list[tuple[int | None, str]]                       # (property_id, label)
    rent: dict[int | None, Decimal] = field(default_factory=dict)
    expenses: dict[tuple[int | None, str], Decimal] = field(default_factory=dict)
    flags: list[Flag] = field(default_factory=list)
    rent_rows: list[db.RentRow] = field(default_factory=list)
    expense_rows: list[db.ExpenseRow] = field(default_factory=list)

    def total_expenses(self, key: int | None) -> Decimal:
        return sum((v for (k, _), v in self.expenses.items() if k == key), Decimal(0))

    def grand(self, values: dict) -> Decimal:
        return sum(values.values(), Decimal(0))


def build_report(
    year: int,
    properties: list[db.Property],
    rents: list[db.RentRow],
    expenses: list[db.ExpenseRow],
    leases: list[db.LeaseRow],
    today: date,
    deposits_kept: list[tuple[date, Decimal, str]] = (),
) -> Report:
    """deposits_kept: (the day, the amount, the tenants) for each security deposit, or part
    of one, kept in `year`."""
    labels = {p.id: p.label for p in properties}
    rent_totals: dict[int | None, Decimal] = defaultdict(Decimal)
    expense_totals: dict[tuple[int | None, str], Decimal] = defaultdict(Decimal)
    flags: list[Flag] = []

    for r in rents:
        rent_totals[r.property_id] += r.amount
        if r.needs_review or r.lease_id is None:
            detail = "not matched to a lease" if r.lease_id is None else "flagged during email sync"
            flags.append(Flag("needs review", r.paid_on, r.amount, r.payer or "-", f"Rent {detail}"))

    for e in expenses:
        expense_totals[(e.property_id, e.category)] += e.amount
        who = e.vendor or "-"
        if e.needs_review:
            flags.append(Flag("needs review", e.spent_on, e.amount, who,
                              f"Expense ({e.category}) flagged during email sync"))
        if e.category in CAPITALIZABLE_CATEGORIES and e.amount > DE_MINIMIS_LIMIT:
            flags.append(Flag("possible capital expense", e.spent_on, e.amount, who,
                              f"Over the ${DE_MINIMIS_LIMIT:,} de minimis safe harbor; may need "
                              "to be capitalized and depreciated instead of expensed"))

    unallocated = [e for e in expenses if e.property_id is None]
    if unallocated:
        total = sum((e.amount for e in unallocated), Decimal(0))
        flags.append(Flag("allocate", None, total, "-",
                          f"{len(unallocated)} LLC-wide expense(s) not assigned to a property; "
                          "split them across properties with your CPA"))

    for gap in rent_gaps(year, leases, rents, today):
        where = labels.get(gap.property_id, f"property {gap.property_id}")
        flags.append(Flag("missing rent" if gap.paid == 0 else "short rent", gap.month, gap.paid,
                          where,
                          f"{gap.month:%b %Y}: recorded ${gap.paid:,.2f} of ${gap.due:,.2f} "
                          "(late payment or not yet synced?)"))

    for kept_on, amount, tenants in deposits_kept:
        flags.append(Flag("deposit kept", kept_on, amount, tenants,
                          "Security deposit kept from the tenant. It is rental income in the "
                          "year it is kept, and it is not in the rent total: ask your CPA"))

    if not any(cat == "depreciation" for _, cat in expense_totals):
        flags.extend(depreciation_flags(year, properties))

    used = set(rent_totals) | {k for k, _ in expense_totals}
    columns = [(p.id, p.label) for p in properties if p.id in used]
    if UNASSIGNED in used:
        columns.append((UNASSIGNED, "LLC-wide / unassigned"))

    return Report(year, columns, dict(rent_totals), dict(expense_totals), flags,
                  list(rents), list(expenses))


def depreciation(cost_basis: Decimal, placed_in_service: date, year: int) -> Decimal:
    """Estimated depreciation for one tax year: the building's cost basis spread evenly over
    330 months, counted from the middle of the month it was placed in service (the mid-month
    convention)."""
    start = Decimal(placed_in_service.month) - Decimal("0.5")   # months after January 1
    year_start = Decimal(12 * (year - placed_in_service.year))
    months = min(year_start + 12, start + RECOVERY_MONTHS) - max(year_start, start)
    return (cost_basis * max(months, Decimal(0)) / RECOVERY_MONTHS).quantize(Decimal("0.01"))


def depreciation_flags(year: int, properties: list[db.Property]) -> list[Flag]:
    """For a year with no depreciation recorded: an estimate for each property whose purchase
    date and cost basis are known, and a note that names the others. The estimate is a line
    on the Review sheet only; it is not added to the Schedule E totals."""
    flags = []
    unknown = []
    for p in properties:
        if p.purchase_date is None or p.cost_basis is None:
            unknown.append(p.label)
            continue
        amount = depreciation(p.cost_basis, p.purchase_date, year)
        if amount:
            flags.append(Flag(
                "depreciation estimate", None, amount, p.label,
                f"Not in the Schedule E totals. Straight line over 27.5 years, mid-month "
                f"convention, on a ${p.cost_basis:,.2f} cost basis in service from "
                f"{p.purchase_date:%B %Y}. Your CPA files Form 4562: check that the basis "
                "leaves out the land and that the home was ready to rent from that date"))
    if unknown or not properties:
        flags.append(Flag("note", None, None, ", ".join(unknown) or "-",
                          "No depreciation recorded; your CPA computes it from each property's "
                          "cost basis (Form 4562). For an estimate here, tell the chat the "
                          "purchase date and the cost basis without land"))
    return flags


@dataclass(frozen=True)
class RentGap:
    property_id: int
    month: date          # first day of the month
    paid: Decimal
    due: Decimal


def rent_gaps(
    year: int,
    leases: list[db.LeaseRow],
    rents: list[db.RentRow],
    today: date,
) -> list[RentGap]:
    """Months of `year` that ended before `today` where an active lease has no or short rent
    recorded. A payment counts toward the calendar month it was paid in."""
    paid: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for r in rents:
        if r.lease_id is not None:
            paid[(r.lease_id, r.paid_on.month)] += r.amount

    gaps = []
    for lease in leases:
        for month in range(1, 13):
            first = date(year, month, 1)
            last = date(year, month, calendar.monthrange(year, month)[1])
            if last >= today or not lease.start_date <= first <= lease.end_date:
                continue
            got = paid[(lease.id, month)]
            if got >= lease.monthly_rent:
                continue
            gaps.append(RentGap(lease.property_id, first, got, lease.monthly_rent))
    return gaps


def write_xlsx(report: Report, path: Path) -> None:
    wb = Workbook()
    bold = Font(bold=True)

    review = wb.active
    review.title = "Review"
    review.append([f"{report.year} rental ledger — items to check before filing"])
    review.append(["Bookkeeping summary, not tax advice. Review with your CPA."])
    review.append([])
    review.append(["Type", "Date", "Amount", "Who / where", "Detail"])
    for f in report.flags:
        review.append([f.kind, f.on, f.amount, f.who, f.detail])
    if not report.flags:
        review.append(["none"])
    _style(review, header_row=4, money_cols=[3], bold=bold)
    review["A1"].font = Font(bold=True, size=13)

    sched = wb.create_sheet("Schedule E")
    keys = [k for k, _ in report.columns]
    sched.append(["Line", "Description", *[label for _, label in report.columns], "Total"])
    sched.append(["3", "Rents received", *[report.rent.get(k, Decimal(0)) for k in keys],
                  report.grand(report.rent)])
    # The owner's own expense types have no line of their own: they are part of line 19.
    own_types = sorted({cat for _, cat in report.expenses} - set(SCHEDULE_E_CATEGORIES))
    for line, label, cat in SCHEDULE_E_LINES:
        cats = [cat, *own_types] if cat == "other" else [cat]
        values = [sum((report.expenses.get((k, c), Decimal(0)) for c in cats), Decimal(0))
                  for k in keys]
        sched.append([line, label, *values, sum(values, Decimal(0))])
    for cat in own_types:
        values = [report.expenses.get((k, cat), Decimal(0)) for k in keys]
        sched.append(["", f"   in line 19: {cat.replace('_', ' ')}", *values,
                      sum(values, Decimal(0))])
    totals = [report.total_expenses(k) for k in keys]
    sched.append(["20", "Total expenses", *totals, sum(totals, Decimal(0))])
    net = [report.rent.get(k, Decimal(0)) - report.total_expenses(k) for k in keys]
    sched.append(["21", "Income or (loss)", *net, sum(net, Decimal(0))])
    for row in (sched[2], sched[sched.max_row - 1], sched[sched.max_row]):
        for cell in row:
            cell.font = bold
    _style(sched, header_row=1, money_cols=range(3, len(keys) + 4), bold=bold)

    labels = dict(report.columns)
    rent = wb.create_sheet("Rent")
    rent.append(["Date", "Amount", "Property", "Payer", "Lease id", "Needs review"])
    for r in report.rent_rows:
        rent.append([r.paid_on, r.amount, labels.get(r.property_id, "unassigned"), r.payer,
                     r.lease_id, "yes" if r.needs_review else ""])
    _style(rent, header_row=1, money_cols=[2], bold=bold)

    exp = wb.create_sheet("Expenses")
    exp.append(["Date", "Amount", "Category", "Property", "Vendor", "Description", "Receipt",
                "Needs review"])
    for e in report.expense_rows:
        exp.append([e.spent_on, e.amount, e.category, labels.get(e.property_id, "LLC-wide"),
                    e.vendor, e.description, e.receipt_path, "yes" if e.needs_review else ""])
    _style(exp, header_row=1, money_cols=[2], bold=bold)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _style(ws, *, header_row: int, money_cols, bold: Font) -> None:
    for cell in ws[header_row]:
        cell.font = bold
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    for col in money_cols:
        for row in ws.iter_rows(min_row=header_row + 1, min_col=col, max_col=col):
            row[0].number_format = MONEY_FORMAT
    for row in ws.iter_rows(min_row=header_row + 1):
        for cell in row:
            if isinstance(cell.value, date):
                cell.number_format = "yyyy-mm-dd"
    for idx, column in enumerate(ws.columns, start=1):
        width = max((len(str(c.value)) for c in column if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(idx)].width = min(max(width + 2, 10), 60)


def write_csv(report: Report, path: Path) -> None:
    labels = dict(report.columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["type", "date", "amount", "category", "property", "counterparty",
                    "description", "receipt_path", "needs_review"])
        for r in report.rent_rows:
            w.writerow(["rent", r.paid_on, r.amount, "", labels.get(r.property_id, "unassigned"),
                        r.payer or "", "", "", r.needs_review])
        for e in report.expense_rows:
            w.writerow(["expense", e.spent_on, e.amount, e.category,
                        labels.get(e.property_id, "LLC-wide"), e.vendor or "",
                        e.description or "", e.receipt_path or "", e.needs_review])


def deposits_kept(conn, year: int) -> list[tuple[date, Decimal, str]]:
    """Deposit money kept in `year`: counted on the day the letter was sent, or on the
    move-out date while it is not sent."""
    today = date.today()
    return [(m.sent_on or m.move_out_date, m.kept, ", ".join(m.tenants) or "-")
            for m in move_out.load(conn, today)
            if m.kept and (m.sent_on or m.move_out_date).year == year]


def run(settings: Settings, year: int) -> tuple[Report, Path, Path]:
    with db.connect(settings.db_url) as conn:
        report = build_report(
            year,
            db.load_properties(conn),
            db.rent_for_year(conn, year),
            db.expenses_for_year(conn, year),
            db.leases_active_in_year(conn, year),
            today=date.today(),
            deposits_kept=deposits_kept(conn, year),
        )
    out = settings.artifacts_dir / str(year) / "tax"
    xlsx = out / f"schedule-e-{year}.xlsx"
    csv_path = out / f"transactions-{year}.csv"
    write_xlsx(report, xlsx)
    write_csv(report, csv_path)
    return report, xlsx, csv_path
