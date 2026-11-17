"""Document import against Postgres as the rental_agent role (needs supabase/schema.sql applied)."""

import json
import os
from decimal import Decimal

import psycopg
import pytest

from rental_agent import cli, doc_import
from tests.test_doc_import import FORM_1098, LEASE, LLC

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")


@pytest.fixture(autouse=True)
def clean():
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate lease_tenants, rent_payments, leases, tenants, properties, llcs, "
                     "reference_documents restart identity cascade")


def _counts(conn):
    return {t: conn.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("llcs", "properties", "tenants", "leases", "lease_tenants",
                      "reference_documents")}


def test_llc_then_lease_and_reimport_is_idempotent(tmp_path):
    src = tmp_path / "doc.md"
    src.write_text("text")
    lease = {**LEASE, "owner_llc": "oak llc"}
    with psycopg.connect(DB) as conn:
        doc_import.apply(conn, LLC, src)
        lines = doc_import.apply(conn, lease, src)
        assert any("Added property" in x for x in lines)
        first = _counts(conn)
        again = doc_import.apply(conn, lease, src)
        assert any("Updated the existing lease term" in x for x in again)
        assert _counts(conn) == first == {"llcs": 1, "properties": 1, "tenants": 2, "leases": 1,
                                          "lease_tenants": 2, "reference_documents": 1}
        llc_id, ein, members = conn.execute("select id, ein, members from llcs").fetchone()
        assert ein == "39-1234567" and len(members) == 2
        assert conn.execute("select llc_id from properties").fetchone()[0] == llc_id
        # An EIN letter adds nothing about members: existing members are kept.
        ein_letter = {**LLC, "doc_type": "ein_letter",
                      "llc": {**LLC["llc"], "members": [], "managers": [], "file_number": None}}
        doc_import.apply(conn, ein_letter, src)
        assert conn.execute("select jsonb_array_length(members), file_number from llcs") \
            .fetchone() == (2, "806258073")
        conn.commit()


def test_signed_extension_replaces_the_unsigned_one(tmp_path):
    src = tmp_path / "signed.md"
    src.write_text("text")
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("insert into properties (address, city, zip) values ('12 Oak St', 'Plano', "
                     "'75024')")
        conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent, "
                     "original_lease_date, other_terms, awaiting_signature) values "
                     "(1, '2026-01-01', '2026-12-31', 2500, '2024-11-06', null, false), "
                     "(1, '2027-01-01', '2027-12-31', 2650, '2024-11-06', 'Tenant pays water.', true)")

    def terms():
        with psycopg.connect(ADMIN) as conn:
            return conn.execute("select start_date::text, end_date::text, monthly_rent, other_terms, "
                                "awaiting_signature from leases order by id").fetchall()

    with psycopg.connect(DB) as conn:
        lines = doc_import.apply(conn, LEASE, src)
        conn.commit()
    assert "Marked the extension 2027-01-01 to 2027-12-31 as signed at $2,600.00/month." in lines
    # The signed copy's rent wins; terms it doesn't mention are kept.
    assert terms()[1] == ("2027-01-01", "2027-12-31", 2600, "Tenant pays water.", False)

    # Dates changed by hand on the signed copy: it still replaces the unsigned extension.
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("update leases set awaiting_signature = true where id = 2")
    shorter = {**LEASE, "lease": {**LEASE["lease"], "end_date": "2027-06-30"}}
    with psycopg.connect(DB) as conn:
        lines = doc_import.apply(conn, shorter, src)
        conn.commit()
    assert any(line.startswith("Marked the extension 2027-01-01 to 2027-06-30") for line in lines)
    assert [t[:2] + t[4:] for t in terms()] == [("2026-01-01", "2026-12-31", False),
                                               ("2027-01-01", "2027-06-30", False)]


def test_year_end_document_records_each_expense_once(tmp_path, monkeypatch):
    monkeypatch.setenv("RENTAL_ARTIFACTS_DIR", str(tmp_path))
    src = tmp_path / "chat-uploads" / "1" / "1098.md"
    src.parent.mkdir(parents=True)
    src.write_text("Form 1098 text")
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("insert into properties (address, city, zip) values ('12 Oak St', 'Plano', "
                     "'75024')")
        # The county's receipt, saved earlier by a statement import under the date it was paid:
        # the same year as the form, eleven months before the form's December 31.
        conn.execute("insert into expenses (property_id, amount, spent_on, vendor, "
                     "schedule_e_category) values (1, 4812.37, '2025-01-28', 'County', 'taxes')")

    def expenses():
        with psycopg.connect(ADMIN) as conn:
            return conn.execute("select spent_on::text, amount, schedule_e_category, vendor, "
                                "receipt_path from expenses order by id").fetchall()

    with psycopg.connect(DB) as conn:
        marked = doc_import.mark_existing(conn, doc_import.validate(FORM_1098))
        assert [(line["include"], line["existing"]) for line in marked["expenses"]] == [
            (True, None), (False, "same amount already recorded on Jan 28, 2025 (taxes)")]
        lines = doc_import.apply(conn, FORM_1098, src)           # as the command line does
        conn.commit()
    assert lines == ["Recorded $8,123.45 of mortgage interest on 2025-12-31.",
                     "Saved the document text as imports/1098.md for questions in the chat."]
    assert expenses()[1] == ("2025-12-31", Decimal("8123.45"), "mortgage_interest", "Big Bank",
                             "chat-uploads/1/1098.md")

    # A second import records nothing. Ticking both lines anyway adds only the one that is new.
    ticked = {line["id"] for line in marked["expenses"]}
    with psycopg.connect(DB) as conn:
        assert doc_import.apply(conn, FORM_1098, src)[0] == "No expense was recorded."
        lines = doc_import.apply(conn, FORM_1098, src, ticked)
        conn.commit()
        assert conn.execute("select category, title from reference_documents").fetchall() == [
            ("notes", "2025 Form 1098, 12 Oak St")]
    assert lines[:2] == [
        "Skipped $8,123.45 of mortgage interest on 2025-12-31: it was imported before.",
        "Recorded $4,812.37 of taxes on 2025-12-31."]
    assert len(expenses()) == 3

    # A line the owner removed on the Expenses page stays out.
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("update expenses set removed_at = now() where id = 2")
    with psycopg.connect(DB) as conn:
        marked = doc_import.mark_existing(conn, doc_import.validate(FORM_1098))
        assert marked["expenses"][0]["existing"] == "removed earlier; restore it on the Expenses page"
        with pytest.raises(ValueError, match="no property with id 9"):
            doc_import.apply(conn, {**FORM_1098, "property_id": 9}, src)


def test_unknown_owner_llc_is_rejected(tmp_path):
    src = tmp_path / "doc.md"
    src.write_text("text")
    with psycopg.connect(DB) as conn, pytest.raises(ValueError, match="isn't in the database"):
        doc_import.apply(conn, {**LEASE, "owner_llc": "Nope LLC"}, src)


def test_cli_preview_changes_nothing_and_apply_saves(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RENTAL_DB_URL", DB)
    monkeypatch.delenv("RENTAL_SECRET_KEY", raising=False)
    monkeypatch.delenv("RENTAL_SECRET_KEY_FILE", raising=False)
    src, facts = tmp_path / "lease.md", tmp_path / "lease.json"
    src.write_text("lease text")
    facts.write_text(json.dumps(LEASE))
    assert cli.main(["import-document", str(src), "--data", str(facts)]) == 0
    assert "Preview (nothing saved" in capsys.readouterr().out
    with psycopg.connect(DB) as conn:
        assert _counts(conn)["leases"] == 0
    assert cli.main(["import-document", str(src), "--data", str(facts), "--apply"]) == 0
    with psycopg.connect(DB) as conn:
        assert _counts(conn)["leases"] == 1
        content = conn.execute("select content from reference_documents").fetchone()[0]
        assert "lease text" in content and "123-45-6789" not in content
