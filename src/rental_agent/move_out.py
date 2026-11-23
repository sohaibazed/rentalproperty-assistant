"""The end of a tenancy: the move-out, the security deposit and the itemized letter. For the
Leases page, the Dashboard reminder, the chat and the tax report.

Texas Property Code, chapter 92, subchapter C:
- 92.103: refund the deposit on or before the 30th day after the tenant surrenders the premises.
- 92.104: damages and charges the tenant is liable for may be deducted, never normal wear and
  tear; what is kept needs a written description and itemized list of all deductions.
- 92.107: no duty to refund or to give the list until the tenant gives a forwarding address
  in writing.
- 92.1041: a refund or list postmarked on or before the due date counts as on time.

The app records the move-out and the deductions and writes the letter. It sends nothing and
moves no money: the owner mails the letter and the refund, then records the day.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg

from rental_agent import db, pdf
from rental_agent.jobs.lease import long_date, money

DEADLINE_DAYS = 30                 # Property Code 92.103
URGENT_DAYS = 7                    # from here the reminder is marked urgent
MAX_AMOUNT = Decimal(100000)
MAX_LINES = 30                     # deduction lines on one chat card
# A move-out is recorded around the time it happens, not years away from it.
MAX_DAYS_FROM_TODAY = 730


@dataclass(frozen=True)
class Deduction:
    id: int
    description: str
    amount: Decimal


@dataclass(frozen=True)
class MoveOut:
    lease_id: int
    property_id: int
    label: str                         # the property's street address, with the unit
    address: str                       # the full address, for the letter
    state: str
    landlords: list[str]
    tenants: list[str]
    start_date: date                   # the lease term the tenants are leaving
    end_date: date
    move_out_date: date
    forwarding_address: str | None
    deposit: Decimal
    deductions: list[Deduction]
    letter_path: str | None            # None when there is no letter or it is out of date
    sent_on: date | None
    days_left: int                     # until the deadline; negative after it

    @property
    def deadline(self) -> date:
        return self.move_out_date + timedelta(days=DEADLINE_DAYS)

    @property
    def total_deductions(self) -> Decimal:
        return sum((d.amount for d in self.deductions), Decimal(0))

    @property
    def kept(self) -> Decimal:
        """What stays with the landlord: the deductions, up to the whole deposit."""
        return min(self.total_deductions, self.deposit)

    @property
    def refund(self) -> Decimal:
        return self.deposit - self.kept

    @property
    def owed(self) -> Decimal:
        """Deductions beyond the deposit, which the tenant still owes."""
        return self.total_deductions - self.kept

    @property
    def step(self) -> str:
        """What to do next: "address", "letter", "send", or "done"."""
        if self.sent_on:
            return "done"
        if not self.forwarding_address:
            return "address"
        return "send" if self.letter_path else "letter"

    @property
    def time_left(self) -> str:
        if self.days_left < 0:
            return f"{-self.days_left} day{'' if self.days_left == -1 else 's'} late"
        if self.days_left == 0:
            return "today"
        return f"{self.days_left} day{'' if self.days_left == 1 else 's'} left"


@dataclass(frozen=True)
class Candidate:
    """A property's latest signed lease term with no move-out recorded."""
    lease_id: int
    label: str
    end_date: date
    deposit: Decimal | None
    tenants: list[str]


def _move_out(conn: psycopg.Connection, row: dict[str, Any], today: date) -> MoveOut:
    unit = f", {row['unit']}" if row["unit"] else ""
    city = " ".join(part for part in (row["city"], row["state"], row["zip"]) if part)
    deductions = [Deduction(**d) for d in db.deposit_deductions(conn, row["lease_id"])]
    deadline = row["move_out_date"] + timedelta(days=DEADLINE_DAYS)
    return MoveOut(
        lease_id=row["lease_id"], property_id=row["property_id"],
        label=row["address"] + unit, address=f"{row['address']}{unit}, {city}".rstrip(", "),
        state=row["state"], landlords=row["landlord_names"], tenants=row["tenants"],
        start_date=row["start_date"], end_date=row["end_date"],
        move_out_date=row["move_out_date"], forwarding_address=row["forwarding_address"],
        deposit=row["deposit"], deductions=deductions, letter_path=row["letter_path"],
        sent_on=row["sent_on"], days_left=(deadline - today).days)


def load(conn: psycopg.Connection, today: date) -> list[MoveOut]:
    """Every recorded move-out, newest first."""
    return [_move_out(conn, row, today) for row in db.move_outs(conn)]


def get(conn: psycopg.Connection, lease_id: int, today: date) -> MoveOut | None:
    rows = db.move_outs(conn, lease_id)
    return _move_out(conn, rows[0], today) if rows else None


def candidates(conn: psycopg.Connection) -> list[Candidate]:
    """Where a move-out can be recorded: each property's latest signed term without one."""
    recorded = {row["lease_id"] for row in db.move_outs(conn)}
    found = []
    for prop in db.load_properties(conn):
        lease = db.latest_lease(conn, prop.id)
        if lease is not None and lease.id not in recorded:
            found.append(Candidate(lease.id, prop.label, lease.end_date, lease.deposit,
                                   lease.tenant_names))
    return found


def _amount(value: Any, name: str, *, zero_ok: bool = False) -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise ValueError(f"{name} must be a number") from None
    if not amount.is_finite() or amount > MAX_AMOUNT or amount < 0 or (amount == 0 and not zero_ok):
        raise ValueError(f"{name} must be {'zero or more' if zero_ok else 'positive'} and at "
                         f"most {money(MAX_AMOUNT)}")
    return amount.quantize(Decimal("0.01"))


def validate_details(form: dict[str, Any], lease_start: date, today: date) -> dict[str, Any]:
    """Check the move-out form. Returns clean values or raises ValueError."""
    try:
        moved = date.fromisoformat(str(form.get("move_out_date") or ""))
    except ValueError:
        raise ValueError("the move-out date is not a date") from None
    if moved < lease_start:
        raise ValueError(f"the move-out date is before the lease term starts ({lease_start})")
    if abs((moved - today).days) > MAX_DAYS_FROM_TODAY:
        raise ValueError("the move-out date must be within two years of today")
    address = "\n".join(line.strip() for line in str(form.get("forwarding_address") or "")
                        .splitlines() if line.strip())
    if len(address) > 300:
        raise ValueError("the forwarding address is too long (300 characters at most)")
    return {"move_out_date": moved, "forwarding_address": address or None,
            "deposit": _amount(form.get("deposit"), "the deposit", zero_ok=True)}


def validate_line(description: Any, amount: Any) -> tuple[str, Decimal]:
    """One deduction line, from the page or from the chat."""
    text = " ".join(str(description or "").split())
    if not 3 <= len(text) <= 200:
        raise ValueError("a deduction needs a description of 3 to 200 characters")
    return text, _amount(amount, "the amount")


def _open(conn: psycopg.Connection, lease_id: Any, today: date) -> MoveOut:
    """The move-out whose deductions can still change."""
    found = get(conn, lease_id, today) if isinstance(lease_id, int) else None
    if found is None:
        raise ValueError(f"no move-out is recorded for lease {lease_id}; the owner records one "
                         "on the Leases page")
    if found.sent_on:
        raise ValueError(f"the deposit letter for lease {lease_id} was sent on {found.sent_on}; "
                         "its deductions can no longer change")
    return found


def propose(conn: psycopg.Connection, args: dict[str, Any], today: date) -> dict[str, Any]:
    """Check deduction lines the chat suggests and build their card. Raises ValueError."""
    found = _open(conn, args.get("lease_id"), today)
    lines = args.get("lines") or []
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        raise ValueError(f"lines must hold 1 to {MAX_LINES} deductions")
    clean = []
    for n, line in enumerate(lines, start=1):
        line = line if isinstance(line, dict) else {}
        try:
            text, amount = validate_line(line.get("description"), line.get("amount"))
        except ValueError as exc:
            raise ValueError(f"line {n}: {exc}") from None
        clean.append({"id": str(n), "description": text, "amount": str(amount),
                      "basis": " ".join(str(line.get("basis") or "").split())[:200] or None})
    return {"lease_id": found.lease_id, "label": found.label, "tenants": found.tenants,
            "deposit": str(found.deposit), "lines": clean,
            "reason": " ".join(str(args.get("reason") or "").split())[:300] or None}


def apply(conn: psycopg.Connection, data: dict[str, Any], picked: set[str], today: date) -> str:
    """Add the ticked lines of a chat card to the move-out. Raises ValueError."""
    found = _open(conn, data["lease_id"], today)
    lines = [line for line in data["lines"] if line["id"] in picked]
    if not lines:
        raise ValueError("tick at least one line, or dismiss the card")
    for line in lines:
        line["saved"] = True
        db.add_deposit_deduction(conn, found.lease_id,
                                 *validate_line(line["description"], line["amount"]))
    total = sum((Decimal(line["amount"]) for line in lines), Decimal(0))
    return (f"Added {len(lines)} deduction{'' if len(lines) == 1 else 's'} ({money(total)}) for "
            f"{found.label}. Write the letter on the Leases page.")


def letter_blocks(m: MoveOut) -> list[pdf.Block]:
    blocks: list[pdf.Block] = [pdf.Facts([
        ("To", ", ".join(m.tenants) or "Tenant"),
        ("Forwarding address", (m.forwarding_address or "none given").replace("\n", ", ")),
        ("Rental property", m.address),
        ("Lease term", f"{long_date(m.start_date)} to {long_date(m.end_date)}"),
        ("Moved out", long_date(m.move_out_date)),
        ("Security deposit held", money(m.deposit)),
    ])]
    if not m.deductions:
        blocks.append(f"The full security deposit of **{money(m.deposit)}** is refunded with "
                      "this letter. Nothing was deducted from it.")
    else:
        blocks.append("The amounts below are deducted from the security deposit. Each one is a "
                      "damage or charge that the tenant is liable for under the lease. Nothing "
                      "is deducted for normal wear and tear.")
        rows = [[d.description, money(d.amount)] for d in m.deductions]
        rows += [["Total deductions", money(m.total_deductions)],
                 ["Security deposit held", money(m.deposit)]]
        rows.append(["Balance still owed by the tenant", money(m.owed)] if m.owed
                    else ["Refund to the tenant", money(m.refund)])
        blocks.append(pdf.Table(("Deduction", "Amount"), rows, widths=(75, 25),
                                right=frozenset({1}),
                                bold=frozenset(range(len(m.deductions), len(rows)))))
        if m.owed:
            blocks.append(f"The deductions are more than the deposit, so no refund is due. "
                          f"**{money(m.owed)}** is still owed under the lease.")
        elif m.refund:
            blocks.append(f"The balance of the deposit, **{money(m.refund)}**, is refunded with "
                          "this letter.")
        else:
            blocks.append("The deductions equal the deposit, so no refund is due.")
        if m.state == "TX":
            blocks.append("This letter is the written description and itemized list of "
                          "deductions under Section 92.104 of the Texas Property Code.")
    blocks.append(pdf.Facts([("Landlord", ", ".join(m.landlords) or "______________________"),
                             ("Signature", "______________________"),
                             ("Date", "______________________")]))
    return blocks


def write_letter(conn: psycopg.Connection, m: MoveOut, artifacts_dir: Path, today: date) -> str:
    """Write the deposit letter as a PDF and record it. Returns its path under artifacts/."""
    out = (artifacts_dir / str(m.move_out_date.year) / "move-out"
           / f"deposit-letter_lease-{m.lease_id}_{m.move_out_date}.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.write(out,
              title=("Security deposit: refund and itemized deductions" if m.deductions
                     else "Security deposit refund"),
              subtitle=f"{m.address}. Dated {long_date(today)}.",
              footer=f"Security deposit, {m.label}, moved out {long_date(m.move_out_date)}",
              blocks=letter_blocks(m))
    path = str(out.relative_to(artifacts_dir))
    db.set_move_out_letter(conn, m.lease_id, path)
    return path
