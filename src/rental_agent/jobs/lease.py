"""generate-lease: yearly Lease Extension Agreement (.docx + .pdf) from a fixed template.

Each year the original lease stays in force and a one-page extension changes only the term,
the rent, and any "other terms". This job fills templates/lease_extension_template.docx with
data from Postgres, converts it to PDF with LibreOffice, and records the new lease term.
No model call: the wording is fixed and reviewed once, so every year's document is predictable.
"""

import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from docxtpl import DocxTemplate

from rental_agent import db
from rental_agent.config import Settings

TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "lease_extension_template.docx"

# The current lease (TXR-2001, paragraph 4A box 2) auto-renews month-to-month unless written
# notice is given at least 45 days before the expiration date.
NOTICE_DAYS = 45


@dataclass
class LeaseResult:
    docx_path: Path
    pdf_path: Path
    lease_id: int | None
    context: dict[str, object]
    warnings: list[str] = field(default_factory=list)


def default_term(current_end: date) -> tuple[date, date]:
    """One year starting the day after the current term ends."""
    start = current_end + timedelta(days=1)
    try:
        next_year = start.replace(year=start.year + 1)
    except ValueError:  # Feb 29 start
        next_year = start.replace(year=start.year + 1, day=28) + timedelta(days=1)
    return start, next_year - timedelta(days=1)


def term_months(start: date, end: date) -> int:
    after = end + timedelta(days=1)
    return (after.year - start.year) * 12 + after.month - start.month


def long_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def money(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def build_context(
    prop: db.PropertyDetail,
    current: db.CurrentLease,
    *,
    start: date,
    end: date,
    rent: Decimal,
    other_terms: str | None,
) -> dict[str, object]:
    missing = [name for name, value in (
        ("properties.city", prop.city), ("properties.county", prop.county),
        ("properties.zip", prop.zip), ("properties.landlord_names", prop.landlord_names),
        ("lease_tenants for the current lease", current.tenant_names),
    ) if not value]
    if missing:
        raise ValueError("Fill these in before generating a lease: " + ", ".join(missing))
    if end <= start:
        raise ValueError(f"end date {end} must be after start date {start}")
    if rent <= 0:
        raise ValueError("rent must be positive")

    address = f"{prop.address}, {prop.unit}" if prop.unit else prop.address
    return {
        "landlords": prop.landlord_names,
        "tenants": current.tenant_names,
        "landlord_names": " and ".join(prop.landlord_names),
        "tenant_names": " and ".join(current.tenant_names),
        "original_lease_date": long_date(current.lease_being_extended),
        "address": address,
        "city": prop.city,
        "county": prop.county,
        "state": prop.state,
        "zip": prop.zip,
        "term_months": term_months(start, end),
        "start_date": long_date(start),
        "end_date": long_date(end),
        "monthly_rent": money(rent),
        "other_terms": (other_terms or "").strip() or "None.",
    }


def lease_warnings(current: db.CurrentLease, rent: Decimal, today: date) -> list[str]:
    warnings = []
    sign_by = current.end_date - timedelta(days=NOTICE_DAYS)
    if today > sign_by:
        warnings.append(
            f"The {NOTICE_DAYS}-day notice date ({long_date(sign_by)}) has passed. If the tenant "
            "doesn't sign, the lease rolls to month-to-month at the current rent."
        )
    else:
        warnings.append(f"Get this signed by {long_date(sign_by)} ({NOTICE_DAYS} days before the "
                        "current term ends).")
    change = (rent - current.monthly_rent) / current.monthly_rent * 100
    warnings.append(f"Rent {money(current.monthly_rent)} -> {money(rent)} ({change:+.1f}%).")
    return warnings


def render_docx(context: dict[str, object], out: Path, template: Path = TEMPLATE) -> None:
    doc = DocxTemplate(str(template))
    doc.render(context, autoescape=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    leftover = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", xml)
    if leftover:
        raise RuntimeError(f"Unfilled template fields in {out.name}: {leftover[:5]}")


def docx_to_pdf(docx: Path, soffice: str) -> Path:
    """Convert with a throwaway LibreOffice profile so it works even if LibreOffice is open."""
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(
            [soffice, "--headless", f"-env:UserInstallation=file://{profile}",
             "--convert-to", "pdf", "--outdir", str(docx.parent), str(docx)],
            check=True, capture_output=True, timeout=180,
        )
    pdf = docx.with_suffix(".pdf")
    if not pdf.exists():
        raise RuntimeError(f"LibreOffice did not produce {pdf.name}")
    return pdf


def run(
    settings: Settings,
    *,
    property_id: int,
    start: date | None,
    end: date | None,
    rent: Decimal | None,
    other_terms: str | None,
    dry_run: bool,
) -> LeaseResult:
    with db.connect(settings.db_url) as conn:
        prop = db.property_detail(conn, property_id)
        if prop is None:
            raise ValueError(f"No property with id {property_id}")
        current = db.latest_lease(conn, property_id)
        if current is None:
            raise ValueError(f"Property {property_id} has no lease on record to extend")

        # Defaults: start the day after the current term ends; run one year from the start.
        if start is None:
            start = current.end_date + timedelta(days=1)
        if end is None:
            end = default_term(start - timedelta(days=1))[1]
        if rent is None:
            rent = db.recommended_rent(conn, property_id, start.year)
            if rent is None:
                raise ValueError(f"No rent given and no rent research for {start.year}; pass --rent")

        context = build_context(prop, current, start=start, end=end, rent=rent,
                                other_terms=other_terms)
        slug = re.sub(r"[^a-z0-9]+", "-", prop.address.lower()).strip("-")
        out_dir = settings.artifacts_dir / str(start.year) / "leases"
        docx = out_dir / f"lease-extension_{slug}_{start:%Y-%m-%d}_{end:%Y-%m-%d}.docx"
        render_docx(context, docx)
        pdf = docx_to_pdf(docx, settings.soffice_bin)

        lease_id = None
        if not dry_run:
            lease_id = db.insert_lease_extension(
                conn,
                property_id=property_id,
                start_date=start,
                end_date=end,
                monthly_rent=rent,
                deposit=current.deposit,
                original_lease_date=current.lease_being_extended,
                other_terms=other_terms,
                tenant_ids=current.tenant_ids,
                docx_path=str(docx.relative_to(settings.artifacts_dir)),
                pdf_path=str(pdf.relative_to(settings.artifacts_dir)),
            )
            conn.commit()

    return LeaseResult(docx, pdf, lease_id, context, lease_warnings(current, rent, date.today()))
