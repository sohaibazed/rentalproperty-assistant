"""Move-out arithmetic, form checks, the deposit letter and the tax report's line."""

from datetime import date
from decimal import Decimal

import pytest
from pypdf import PdfReader

from rental_agent import move_out, pdf
from rental_agent.jobs import tax_report

TODAY = date(2027, 1, 10)


def moved(*amounts: str, deposit: str = "2500", sent_on=None, state="TX", address="9 Elm St\nPlano TX",
          letter=None, today=TODAY) -> move_out.MoveOut:
    deductions = [move_out.Deduction(n, f"Repair {n}", Decimal(a)) for n, a in enumerate(amounts, 1)]
    out = date(2026, 12, 31)
    return move_out.MoveOut(
        lease_id=7, property_id=1, label="12 Oak St", address="12 Oak St, Plano TX 75024",
        state=state, landlords=["Sam Owner"], tenants=["Ann Lee", "Bo Lee"],
        start_date=date(2026, 1, 1), end_date=out, move_out_date=out,
        forwarding_address=address, deposit=Decimal(deposit), deductions=deductions,
        letter_path=letter, sent_on=sent_on,
        days_left=(out - today).days + move_out.DEADLINE_DAYS)


def test_refund_kept_and_owed():
    m = moved("300", "450.50")
    assert (m.total_deductions, m.kept, m.refund, m.owed) == (
        Decimal("750.50"), Decimal("750.50"), Decimal("1749.50"), Decimal(0))
    m = moved("2000", "900")                              # more than the deposit
    assert (m.kept, m.refund, m.owed) == (Decimal(2500), Decimal(0), Decimal(400))
    m = moved()
    assert (m.kept, m.refund, m.owed) == (Decimal(0), Decimal(2500), Decimal(0))


def test_deadline_is_30_days_after_the_move_out_and_steps_follow():
    m = moved(address=None)
    assert m.deadline == date(2027, 1, 30) and m.time_left == "20 days left"
    assert m.step == "address"
    assert moved().step == "letter"
    assert moved(letter="x.pdf").step == "send"
    assert moved(letter="x.pdf", sent_on=date(2027, 1, 12)).step == "done"
    assert moved(today=date(2027, 1, 30)).time_left == "today"
    assert moved(today=date(2027, 2, 1)).time_left == "2 days late"


def test_details_form_is_checked():
    start = date(2026, 1, 1)
    clean = move_out.validate_details(
        {"move_out_date": "2026-12-31", "deposit": "$2,500", "forwarding_address": " 9 Elm St \n\n Plano TX "},
        start, TODAY)
    assert clean == {"move_out_date": date(2026, 12, 31), "deposit": Decimal("2500.00"),
                     "forwarding_address": "9 Elm St\nPlano TX"}
    assert move_out.validate_details({"move_out_date": "2026-12-31", "deposit": "0"}, start,
                                     TODAY)["forwarding_address"] is None
    for form, message in [
        ({"move_out_date": "soon", "deposit": "1"}, "not a date"),
        ({"move_out_date": "2025-12-31", "deposit": "1"}, "before the lease term starts"),
        ({"move_out_date": "2030-01-01", "deposit": "1"}, "within two years"),
        ({"move_out_date": "2026-12-31", "deposit": "-5"}, "the deposit must be"),
        ({"move_out_date": "2026-12-31", "deposit": ""}, "the deposit must be a number"),
        ({"move_out_date": "2026-12-31", "deposit": "1", "forwarding_address": "x" * 301}, "too long"),
    ]:
        with pytest.raises(ValueError, match=message):
            move_out.validate_details(form, start, TODAY)


def test_deduction_line_is_checked():
    assert move_out.validate_line("  Repair   door ", "120.5") == ("Repair door", Decimal("120.50"))
    for description, amount in [("", "5"), ("ok text", "0"), ("ok text", "abc"), ("ok text", "200000"),
                                ("ok text", "NaN")]:
        with pytest.raises(ValueError):
            move_out.validate_line(description, amount)


def _text(blocks) -> str:
    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, pdf.Facts):
            parts += [f"{k}: {v}" for k, v in block.rows]
        elif isinstance(block, pdf.Table):
            parts += [" | ".join(row) for row in block.rows]
    return "\n".join(parts)


def test_letter_itemizes_deductions_and_states_the_refund():
    text = _text(move_out.letter_blocks(moved("300", "450.50")))
    assert "To: Ann Lee, Bo Lee" in text and "Forwarding address: 9 Elm St, Plano TX" in text
    assert "Repair 1 | $300.00" in text and "Total deductions | $750.50" in text
    assert "Refund to the tenant | $1,749.50" in text
    assert "Nothing is deducted for normal wear and tear." in text
    assert "Section 92.104 of the Texas Property Code" in text
    assert "Landlord: Sam Owner" in text


def test_letter_for_a_full_refund_and_for_deductions_over_the_deposit():
    full = _text(move_out.letter_blocks(moved()))
    assert "The full security deposit of **$2,500.00** is refunded" in full
    assert "92.104" not in full                           # no deductions, no itemized list
    over = _text(move_out.letter_blocks(moved("2000", "900", state="OK")))
    assert "Balance still owed by the tenant | $400.00" in over and "no refund is due" in over
    assert "Texas" not in over                            # the citation is Texas law only


def test_letter_pdf_is_written(tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(move_out.db, "set_move_out_letter",
                        lambda _conn, lease_id, path: saved.update({lease_id: path}))
    path = move_out.write_letter(None, moved("300"), tmp_path, TODAY)
    assert path == "2026/move-out/deposit-letter_lease-7_2026-12-31.pdf" and saved == {7: path}
    page = PdfReader(tmp_path / path).pages[0].extract_text()
    assert "Security deposit: refund and itemized deductions" in page
    assert "Repair 1" in page and "$2,200.00" in page and "January 10, 2027" in page


def test_tax_report_lists_a_kept_deposit_without_adding_it_to_rent():
    report = tax_report.build_report(
        2026, [], [], [], [], TODAY,
        deposits_kept=[(date(2026, 12, 31), Decimal("750.50"), "Ann Lee")])
    [flag] = [f for f in report.flags if f.kind == "deposit kept"]
    assert (flag.on, flag.amount, flag.who) == (date(2026, 12, 31), Decimal("750.50"), "Ann Lee")
    assert "rental income" in flag.detail and report.grand(report.rent) == 0
