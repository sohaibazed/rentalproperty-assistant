from datetime import date
from decimal import Decimal as D

import pytest
from pypdf import PdfReader, PdfWriter

from rental_agent.db import CurrentLease, PropertyDetail
from rental_agent.jobs.lease import (
    build_context,
    default_term,
    lease_warnings,
    render_pdf,
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
    assert c["tenant_names"] == "Ann Tenant, Bo Tenant & Co"
    assert c["term"] == "12 months"
    assert c["state"] == "Texas"
    assert c["rent"] == "2,600.00"          # the form prints the "$"
    assert c["other_terms_lines"] == ["None."]


def test_other_terms_wrap_and_must_fit():
    c = _ctx(other_terms="Tenant pays water.\nAll other terms remain unchanged.")
    assert c["other_terms_lines"] == ["Tenant pays water.", "All other terms remain unchanged."]
    with pytest.raises(ValueError, match="Other terms must fit"):
        _ctx(other_terms="word " * 60)


def test_more_people_than_signature_lines_rejected():
    three = CurrentLease(7, date(2026, 1, 1), date(2026, 12, 31), D(2500), D(2500), None,
                         [1, 2, 3], ["A", "B", "C"])
    with pytest.raises(ValueError, match="2 signature lines for tenants"):
        build_context(PROP, three, start=date(2027, 1, 1), end=date(2027, 12, 31),
                      rent=D(2600), other_terms=None)


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


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_render_fills_the_form(tmp_path):
    out = tmp_path / "x.pdf"
    render_pdf(_ctx(other_terms="Tenant pays water (city bill)."), out, original=None)
    pages = PdfReader(out).pages
    assert len(pages) == 1
    text = _flat(pages[0].extract_text())
    for value in ("Lee Landlord and Pat Landlord", "Ann Tenant, Bo Tenant & Co", "12 Oak St",
                  "Collin", "Texas", "75024", "November 6, 2024", "12 months",
                  "January 1, 2027", "December 31, 2027", "2,600.00",
                  "Tenant pays water (city bill).", "Extension of Lease Agreement"):
        assert value in text, value


def test_original_lease_is_appended(tmp_path):
    original = tmp_path / "original.pdf"
    w = PdfWriter()
    for _ in range(3):
        w.add_blank_page(612, 792)
    w.write(original)
    out = tmp_path / "x.pdf"
    render_pdf(_ctx(), out, original=original)
    assert len(PdfReader(out).pages) == 4


def test_unprintable_text_rejected(tmp_path):
    with pytest.raises(ValueError, match="Can't print"):
        render_pdf(_ctx(other_terms="Pets: 🐶"), tmp_path / "x.pdf", original=None)
