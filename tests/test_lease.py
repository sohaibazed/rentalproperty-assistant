import shutil
import zipfile
from datetime import date
from decimal import Decimal as D

import pytest

from rental_agent.db import CurrentLease, PropertyDetail
from rental_agent.jobs.lease import (
    build_context,
    default_term,
    docx_to_pdf,
    lease_warnings,
    render_docx,
    term_months,
)

PROP = PropertyDetail(1, "12 Oak St", None, "Plano", "Collin", "TX", "75024",
                      ["Lee Landlord", "Pat Landlord"])
CURRENT = CurrentLease(7, date(2026, 1, 1), date(2026, 12, 31), D(2500), D(2500),
                       date(2024, 11, 6), [3, 4], ["Ann Tenant", "Bo Tenant & Co"])


def _ctx(**kw):
    args = {"start": date(2027, 1, 1), "end": date(2027, 12, 31), "rent": D(2600),
            "other_terms": None} | kw
    return build_context(PROP, CURRENT, **args)


def test_default_term_is_one_year_after_current():
    assert default_term(date(2026, 12, 31)) == (date(2027, 1, 1), date(2027, 12, 31))
    assert default_term(date(2028, 2, 28)) == (date(2028, 2, 29), date(2029, 2, 28))


def test_term_months():
    assert term_months(date(2027, 1, 1), date(2027, 12, 31)) == 12
    assert term_months(date(2027, 1, 1), date(2027, 6, 30)) == 6


def test_context_values():
    c = _ctx()
    assert c["original_lease_date"] == "November 6, 2024"   # carried from the original lease
    assert c["monthly_rent"] == "$2,600.00"
    assert c["start_date"] == "January 1, 2027"
    assert c["tenant_names"] == "Ann Tenant and Bo Tenant & Co"
    assert c["other_terms"] == "None."


def test_missing_property_details_are_reported():
    bare = PropertyDetail(1, "12 Oak St", None, None, None, "TX", None, [])
    with pytest.raises(ValueError, match="city.*county.*zip.*landlord_names"):
        build_context(bare, CURRENT, start=date(2027, 1, 1), end=date(2027, 12, 31),
                      rent=D(2600), other_terms=None)


def test_bad_term_and_rent_rejected():
    with pytest.raises(ValueError):
        _ctx(end=date(2026, 12, 1))
    with pytest.raises(ValueError):
        _ctx(rent=D(0))


def test_warnings():
    early = lease_warnings(CURRENT, D(2600), date(2026, 10, 1))
    assert "November 16, 2026" in early[0]
    assert "+4.0%" in early[1]
    late = lease_warnings(CURRENT, D(2600), date(2026, 12, 15))
    assert "has passed" in late[0]


def test_render_fills_every_field(tmp_path):
    out = tmp_path / "x.docx"
    render_docx(_ctx(other_terms="Tenant pays water."), out)
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode()
    for text in ("Lee Landlord", "Pat Landlord", "Ann Tenant", "Bo Tenant &amp; Co",
                 "$2,600.00", "November 6, 2024", "Collin", "Tenant pays water."):
        assert text in xml
    assert xml.count("Signature:") == 4   # one row per landlord and tenant
    assert "{{" not in xml and "{%" not in xml


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_pdf_conversion(tmp_path):
    docx = tmp_path / "x.docx"
    render_docx(_ctx(), docx)
    pdf = docx_to_pdf(docx, "soffice")
    assert pdf.read_bytes()[:5] == b"%PDF-"
