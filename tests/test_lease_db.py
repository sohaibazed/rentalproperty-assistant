"""generate-lease against Postgres as the rental_agent role."""

import os
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from pypdf import PdfReader, PdfWriter

from rental_agent.config import Settings
from rental_agent.jobs import lease

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="needs test database")


@pytest.fixture
def seeded():
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate rent_payments, expenses, email_ingest_log, sync_runs, rent_research, "
                     "lease_tenants, leases, tenants, properties restart identity cascade")
        pid = conn.execute(
            "insert into properties (address, city, county, zip, landlord_names) values "
            "('12 Oak St', 'Plano', 'Collin', '75024', array['Lee Landlord']) returning id"
        ).fetchone()[0]
        t1 = conn.execute("insert into tenants (full_name) values ('Ann Tenant') returning id").fetchone()[0]
        t2 = conn.execute("insert into tenants (full_name) values ('Bo Tenant') returning id").fetchone()[0]
        orig = conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent, deposit, "
                            "pdf_path) values (%s, '2024-11-06', '2025-12-31', 2400, 2400, "
                            "'leases/original.pdf') returning id",
                            (pid,)).fetchone()[0]
        ext = conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent, deposit, "
                           "original_lease_date) values (%s, '2026-01-01', '2026-12-31', 2500, 2400, "
                           "'2024-11-06') returning id", (pid,)).fetchone()[0]
        conn.execute("insert into lease_tenants values (%s, %s), (%s, %s), (%s, %s)",
                     (orig, t1, ext, t1, ext, t2))
    return pid


def _settings(tmp_path):
    return Settings(model="test", db_url=DB, artifacts_dir=tmp_path, reference_dir=tmp_path,
                    email_sources_path=tmp_path)


def test_generates_and_records_extension(seeded, tmp_path):
    (tmp_path / "leases").mkdir()
    w = PdfWriter()
    for _ in range(2):
        w.add_blank_page(612, 792)
    w.write(tmp_path / "leases" / "original.pdf")   # reference_dir is tmp_path

    result = lease.run(_settings(tmp_path), property_id=seeded, start=None, end=None,
                       rent=Decimal(2600), other_terms=None, dry_run=False)
    assert result.pdf_path.parent == tmp_path / "2027" / "leases"
    assert len(PdfReader(result.pdf_path).pages) == 3   # extension + 2-page original
    assert not any("isn't attached" in w for w in result.warnings)
    assert result.context["tenants"] == ["Ann Tenant", "Bo Tenant"]
    assert result.context["original_lease_date"] == "November 6, 2024"

    with psycopg.connect(ADMIN) as conn:
        row = conn.execute("select start_date, end_date, monthly_rent, deposit, original_lease_date, "
                           "awaiting_signature from leases where id = %s",
                           (result.lease_id,)).fetchone()
        assert row == (date(2027, 1, 1), date(2027, 12, 31), 2600, 2400, date(2024, 11, 6), True)
        tenants = conn.execute("select count(*) from lease_tenants where lease_id = %s",
                               (result.lease_id,)).fetchone()[0]
        assert tenants == 2


def test_writing_it_again_replaces_the_unsigned_extension(seeded, tmp_path):
    def generate(rent, other_terms=None):
        return lease.run(_settings(tmp_path), property_id=seeded, start=None, end=None,
                         rent=Decimal(rent), other_terms=other_terms, dry_run=False)

    first = generate(2600)
    again = generate(2650, "Tenant pays water.")
    # It still extends the signed 2026 term, not the unsigned 2027 one.
    assert again.context["start_date"] == "January 1, 2027"
    assert again.lease_id == first.lease_id
    assert ("This replaces the unsigned extension written earlier (January 1, 2027 to "
            "December 31, 2027 at $2,600.00).") in again.warnings
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select count(*) from leases").fetchone()[0] == 3
        assert conn.execute("select monthly_rent, other_terms, awaiting_signature from leases "
                            "where id = %s", (first.lease_id,)).fetchone() \
            == (2650, "Tenant pays water.", True)
        assert conn.execute("select count(*) from lease_tenants where lease_id = %s",
                            (first.lease_id,)).fetchone()[0] == 2

    # Once the signed copy is imported, the next run writes the following year's extension.
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("update leases set awaiting_signature = false")
    following = generate(2700)
    assert following.context["start_date"] == "January 1, 2028"
    assert following.lease_id != first.lease_id


def test_dry_run_records_nothing_and_rent_comes_from_research(seeded, tmp_path):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("insert into rent_research (property_id, year, comps, recommended_rent) "
                     "values (%s, 2027, '[]', 2650)", (seeded,))
    result = lease.run(_settings(tmp_path), property_id=seeded, start=None, end=None,
                       rent=None, other_terms=None, dry_run=True)
    assert result.lease_id is None
    assert result.context["monthly_rent"] == "$2,650.00"
    assert any("original lease isn't attached" in w for w in result.warnings)
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select count(*) from leases").fetchone()[0] == 2


def test_no_rent_and_no_research_is_an_error(seeded, tmp_path):
    with pytest.raises(ValueError, match="pass --rent"):
        lease.run(_settings(tmp_path), property_id=seeded, start=None, end=None,
                  rent=None, other_terms=None, dry_run=True)
