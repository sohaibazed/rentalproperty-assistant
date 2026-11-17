"""generate-lease: yearly Extension of Lease Agreement (.pdf) from a fixed form.

Each year the original lease stays in force and a one-page extension changes only the term,
the rent, and any "other terms". This job stamps data from Postgres onto the blank form in
templates/lease_extension_template.pdf, appends the signed original lease, and records the new
lease term. No model call: the wording is fixed, so every year's document is predictable.
"""

import re
import textwrap
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from rental_agent import db
from rental_agent.config import Settings

TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "lease_extension_template.pdf"

# The current lease (TXR-2001, paragraph 4A box 2) auto-renews month-to-month unless written
# notice is given at least 45 days before the expiration date.
NOTICE_DAYS = 45

# Where each value goes on the form: (x, baseline y) in PDF points from the bottom-left corner.
# Taken from the hand-filled reference copy of this form, in Helvetica 9.3 pt like that copy.
FONT_SIZE = 9.3
FIELDS: dict[str, tuple[float, float]] = {
    "landlord_names": (76.1, 678.0),
    "tenant_names": (75.4, 636.8),
    "address": (71.4, 579.7),
    "city": (68.8, 532.3),
    "county": (183.8, 532.3),
    "state": (319.8, 532.3),
    "zip": (423.1, 532.3),
    "original_lease_date": (113.0, 492.0),   # left blank on the reference copy
    "term": (120.0, 446.7),                  # left blank on the reference copy
    "start_date": (310.0, 446.7),            # left blank on the reference copy
    "end_date": (113.1, 432.4),
    "rent": (394.7, 392.1),
}
OTHER_TERMS_LINES = [(71.1, 326.6), (73.1, 298.6)]
OTHER_TERMS_WIDTH = 95                      # characters that fit on one "other terms" line
LANDLORD_SIGNATURES = [(67.9, 141.7), (254.1, 141.7)]
TENANT_SIGNATURES = [(68.3, 97.3), (249.8, 97.3)]
STATE_NAMES = {"TX": "Texas"}   # the form spells the state out


@dataclass
class LeaseResult:
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
    for role, names, slots in (("landlords", prop.landlord_names, LANDLORD_SIGNATURES),
                               ("tenants", current.tenant_names, TENANT_SIGNATURES)):
        if len(names) > len(slots):
            raise ValueError(f"The form has {len(slots)} signature lines for {role}; "
                             f"this lease has {len(names)}")

    terms = (other_terms or "").strip() or "None."
    terms_lines = [line for para in terms.splitlines()
                   for line in textwrap.wrap(para, OTHER_TERMS_WIDTH)]
    if len(terms_lines) > len(OTHER_TERMS_LINES):
        raise ValueError(f"Other terms must fit on {len(OTHER_TERMS_LINES)} lines of about "
                         f"{OTHER_TERMS_WIDTH} characters; shorten them")

    months = term_months(start, end)
    address = f"{prop.address}, {prop.unit}" if prop.unit else prop.address
    return {
        "landlords": prop.landlord_names,
        "tenants": current.tenant_names,
        "landlord_names": " and ".join(prop.landlord_names),
        "tenant_names": ", ".join(current.tenant_names),
        "original_lease_date": long_date(current.lease_being_extended),
        "address": address,
        "city": prop.city,
        "county": prop.county,
        "state": STATE_NAMES.get(prop.state, prop.state),
        "zip": prop.zip,
        "term": f"{months} month" + ("" if months == 1 else "s"),
        "start_date": long_date(start),
        "end_date": long_date(end),
        "monthly_rent": money(rent),
        "rent": f"{rent:,.2f}",                 # the form prints the "$"
        "other_terms": terms,
        "other_terms_lines": terms_lines,
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


def _pdf_text(text: str) -> bytes:
    """A PDF literal string in WinAnsi (cp1252), the encoding of the standard Helvetica font."""
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise ValueError(f"Can't print {text!r} on the form: {exc.reason}") from exc
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def placements(context: dict[str, object]) -> list[tuple[float, float, str]]:
    """Every (x, y, text) to print on the form."""
    out = [(x, y, str(context[name])) for name, (x, y) in FIELDS.items()]
    out += [(x, y, line) for (x, y), line in zip(OTHER_TERMS_LINES, context["other_terms_lines"])]
    out += [(x, y, name) for (x, y), name in zip(LANDLORD_SIGNATURES, context["landlords"])]
    out += [(x, y, name) for (x, y), name in zip(TENANT_SIGNATURES, context["tenants"])]
    return out


def render_pdf(context: dict[str, object], out: Path, original: Path | None,
               template: Path = TEMPLATE) -> None:
    """Fill the one-page form and, if given, append every page of the original lease."""
    writer = PdfWriter()
    page = writer.add_page(PdfReader(template).pages[0])

    overlay = writer.add_blank_page(float(page.mediabox.width), float(page.mediabox.height))
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    overlay[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/FillHelv"): font}),
    })
    ops = [b"BT", b"0 g", f"/FillHelv {FONT_SIZE} Tf".encode()]
    for x, y, text in placements(context):
        ops.append(f"1 0 0 1 {x} {y} Tm ".encode() + _pdf_text(text) + b" Tj")
    ops.append(b"ET")
    stream = DecodedStreamObject()
    stream.set_data(b"\n".join(ops))
    overlay[NameObject("/Contents")] = writer._add_object(stream)
    page.merge_page(overlay)
    writer.remove_page(overlay)

    if original is not None:
        writer.append(PdfReader(original))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        writer.write(f)


def find_original_pdf(settings: Settings, stored: str | None) -> Path | None:
    """leases.pdf_path is relative to the artifacts folder (generated or uploaded files) or the
    reference folder (documents you keep there)."""
    if not stored:
        return None
    for base in (settings.artifacts_dir, settings.reference_dir):
        path = base / stored
        if path.is_file():
            return path
    return None


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
        warnings = lease_warnings(current, rent, date.today())
        stored = db.original_lease_pdf(conn, property_id, current.lease_being_extended)
        original = find_original_pdf(settings, stored)
        if original is None:
            warnings.append(
                "The original lease isn't attached: "
                + (f"{stored} wasn't found in artifacts/ or reference/." if stored else
                   f"set leases.pdf_path on the {context['original_lease_date']} lease to its "
                   "PDF (relative to artifacts/ or reference/).")
            )

        slug = re.sub(r"[^a-z0-9]+", "-", prop.address.lower()).strip("-")
        out_dir = settings.artifacts_dir / str(start.year) / "leases"
        pdf = out_dir / f"lease-extension_{slug}_{start:%Y-%m-%d}_{end:%Y-%m-%d}.pdf"
        render_pdf(context, pdf, original)

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
                pdf_path=str(pdf.relative_to(settings.artifacts_dir)),
            )
            conn.commit()

    return LeaseResult(pdf, lease_id, context, warnings)
