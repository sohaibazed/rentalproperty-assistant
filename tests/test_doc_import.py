"""Document import: validation rules and the chat's propose_document_import card."""

import asyncio
import copy

import pytest

from rental_agent import chat, doc_import
from rental_agent.categories import SCHEDULE_E_CATEGORIES as CATS
from rental_agent.config import Settings

LEASE = {
    "doc_type": "lease_extension", "title": "2027 extension", "owner_llc": None, "llc": None,
    "summary": "Rent $2,600. Responsible party 123-45-6789.",
    "lease": {"address": " 12 Oak  St ", "unit": None, "city": "Plano", "county": "Collin",
              "state": "tx", "zip": "75024", "landlord_names": ["Owner One"],
              "tenant_names": ["Tenant A", "Tenant A", "Tenant B"],
              "start_date": "2027-01-01", "end_date": "2027-12-31", "monthly_rent": 2600,
              "deposit": 2500, "original_lease_date": "2024-11-06", "other_terms": None},
}
LLC = {
    "doc_type": "operating_agreement", "title": "Company agreement", "owner_llc": None,
    "summary": "Two members.", "lease": None,
    "llc": {"name": "Oak LLC", "state": "TX", "file_number": "806258073", "ein": "39-1234567",
            "formed_on": "2025-10-14", "management": "managers",
            "members": [{"name": "A", "percent": 50}, {"name": "B", "percent": 50}],
            "managers": ["A"], "registered_agent": None, "registered_office": None,
            "mailing_address": None},
}

FORM_1098 = {
    "doc_type": "form_1098", "title": "2025 Form 1098, 12 Oak St", "owner_llc": None,
    "llc": None, "lease": None, "summary": "Box 1 interest $8,123.45. Box 10 taxes $4,812.37.",
    "property_id": 1,
    "expenses": [
        {"date": "2025-12-31", "amount": 8123.45, "category": "mortgage_interest",
         "vendor": " Big  Bank ", "description": "Mortgage interest 2025, Form 1098 box 1",
         "include": True, "note": None},
        {"date": "2025-12-31", "amount": "4812.37", "category": "taxes", "vendor": "Big Bank",
         "description": "Real estate taxes paid from escrow, box 10", "include": True,
         "note": "Also on the county receipt?"},
    ],
}


def _with(base, path, value):
    data = copy.deepcopy(base)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


def test_valid_lease_is_cleaned_and_redacted():
    data = doc_import.validate(LEASE, CATS)
    lease = data["lease"]
    assert lease["address"] == "12 Oak St" and lease["state"] == "TX"
    assert lease["tenant_names"] == ["Tenant A", "Tenant B"]
    assert lease["monthly_rent"] == "2600.00" and lease["deposit"] == "2500.00"
    assert "123-45-6789" not in data["summary"] and "[REDACTED]" in data["summary"]


@pytest.mark.parametrize("path,value,error", [
    (("lease", "end_date"), "2026-12-31", "after start_date"),
    (("lease", "original_lease_date"), None, "original_lease_date"),
    (("lease", "zip"), "7502", "5 digits"),
    (("lease", "monthly_rent"), 0, "more than"),
    (("lease", "tenant_names"), [], "at least one"),
    (("lease", "other_terms"), "SSN 123-45-6789", "contains an SSN"),
    (("doc_type",), "deed", "doc_type"),
    (("lease",), None, "needs the lease facts"),
])
def test_bad_lease_rejected(path, value, error):
    with pytest.raises(ValueError, match=error):
        doc_import.validate(_with(LEASE, path, value), CATS)


def test_llc_checks():
    assert doc_import.validate(LLC, CATS)["llc"]["members"][0] == {"name": "A", "percent": 50.0}
    with pytest.raises(ValueError, match="add up to 90"):
        doc_import.validate(_with(LLC, ("llc", "members", 1, "percent"), 40), CATS)
    with pytest.raises(ValueError, match="12-3456789"):
        doc_import.validate(_with(LLC, ("llc", "ein"), "391234567"), CATS)
    with pytest.raises(ValueError, match="needs the llc facts"):
        doc_import.validate(_with(LLC, ("llc",), None), CATS)


def test_year_end_document_carries_expense_lines():
    data = doc_import.validate(FORM_1098, CATS)
    interest, taxes = data["expenses"]
    assert data["property_id"] == 1 and data["lease"] is None
    assert interest == {"id": interest["id"], "date": "2025-12-31", "amount": "8123.45",
                        "category": "mortgage_interest", "vendor": "Big Bank",
                        "description": "Mortgage interest 2025, Form 1098 box 1",
                        "include": True, "note": None, "existing": None}
    assert interest["id"].startswith("doc:") and interest["id"] != taxes["id"]
    # The id doesn't depend on the wording, so a second import can't record the line twice.
    reworded = _with(FORM_1098, ("expenses", 0, "description"), "Interest paid")
    assert doc_import.validate(reworded, CATS)["expenses"][0]["id"] == interest["id"]
    assert doc_import.validate(data, CATS) == data                  # a stored card checks out again
    twice = doc_import.validate({**FORM_1098, "expenses": [FORM_1098["expenses"][0]] * 2}, CATS)
    assert twice["expenses"][0]["id"] != twice["expenses"][1]["id"]
    assert doc_import.validate({**FORM_1098, "expenses": []}, CATS)["expenses"] == []


@pytest.mark.parametrize("path,value,error", [
    (("property_id",), None, "needs property_id"),
    (("expenses", 0, "date"), "2099-01-01", "is in the future"),
    (("expenses", 0, "amount"), 0, "amount must be more than"),
    (("expenses", 1, "category"), "escrow", "category must be one of"),
    (("expenses", 0, "description"), "", "description is required"),
    (("doc_type",), "other", "can't carry expense lines"),
])
def test_year_end_document_rules(path, value, error):
    with pytest.raises(ValueError, match=error):
        doc_import.validate(_with(FORM_1098, path, value), CATS)


def test_document_text_uses_summary_for_files_without_text(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("Full notes")
    content, pages = doc_import.document_text(md, "# T\n\nSummary")
    assert content.startswith("# T") and "Full notes" in content and pages is None
    content, _ = doc_import.document_text(tmp_path / "scan.jpg", "Only summary")
    assert content == "Only summary"


def test_propose_document_import_records_a_card(tmp_path):
    proposals = []
    settings = Settings("m", "db", tmp_path, tmp_path, tmp_path)
    tools = {t.name: t for t in chat.make_tools(settings, proposals, {"chat-uploads/1/x.pdf"}, CATS)}
    tool = tools["propose_document_import"]
    bad = asyncio.run(tool.handler({**LEASE, "attachment": "chat-uploads/9/other.pdf"}))
    assert bad["is_error"] and not proposals
    ok = asyncio.run(tool.handler({**LEASE, "attachment": "chat-uploads/1/x.pdf"}))
    assert not ok.get("is_error")
    assert proposals[0]["kind"] == "document"
    assert proposals[0]["data"]["attachment"] == "chat-uploads/1/x.pdf"
    assert proposals[0]["data"]["lease"]["monthly_rent"] == "2600.00"


def test_chat_loads_the_skill_plugin():
    assert (chat.PLUGIN_DIR / ".claude-plugin" / "plugin.json").is_file()
    assert (chat.PLUGIN_DIR / "skills" / "import-document" / "SKILL.md").is_file()
    assert "rental:import-document" in chat.SKILLS
