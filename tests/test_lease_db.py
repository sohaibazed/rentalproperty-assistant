"""generate-lease against Postgres as the rental_agent role (soffice required)."""

import os
import shutil
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from rental_agent.config import Settings
from rental_agent.jobs import lease

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not (DB and ADMIN and shutil.which("soffice")), reason="needs test database and LibreOffice"
)


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
        orig = conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent, deposit) "
                            "values (%s, '2024-11-06', '2025-12-31', 2400, 2400) returning id",
                            (pid,)).fetchone()[0]
        ext = conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent, deposit, "
                           "original_lease_date) values (%s, '2026-01-01', '2026-12-31', 2500, 2400, "
                           "'2024-11-06') returning id", (pid,)).fetchone()[0]
        conn.execute("insert into lease_tenants values (%s, %s), (%s, %s), (%s, %s)",
                     (orig, t1, ext, t1, ext, t2))
    return pid


def _settings(tmp_path):
    return Settings(model="test", db_url=DB, artifacts_dir=tmp_path, reference_dir=tmp_path,
                    email_sources_path=tmp_path, soffice_bin="soffice")


def test_generates_and_records_extension(seeded, tmp_path):
    result = lease.run(_settings(tmp_path), property_id=seeded, start=None, end=None,
                       rent=Decimal(2600), other_terms=None, dry_run=False)
    assert result.docx_path.exists() and result.pdf_path.exists()
    assert result.docx_path.parent == tmp_path / "2027" / "leases"
    assert result.context["tenants"] == ["Ann Tenant", "Bo Tenant"]
    assert result.context["original_lease_date"] == "November 6, 2024"

    with psycopg.connect(ADMIN) as conn:
        row = conn.execute("select start_date, end_date, monthly_rent, deposit, original_lease_date "
                           "from leases where id = %s", (result.lease_id,)).fetchone()
        assert row == (date(2027, 1, 1), date(2027, 12, 31), 2600, 2400, date(2024, 11, 6))
        tenants = conn.execute("select count(*) from lease_tenants where lease_id = %s",
                               (result.lease_id,)).fetchone()[0]
        assert tenants == 2


def test_dry_run_records_nothing_and_rent_comes_from_research(seeded, tmp_path):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("insert into rent_research (property_id, year, comps, recommended_rent) "
                     "values (%s, 2027, '[]', 2650)", (seeded,))
    result = lease.run(_settings(tmp_path), property_id=seeded, start=None, end=None,
                       rent=None, other_terms=None, dry_run=True)
    assert result.lease_id is None
    assert result.context["monthly_rent"] == "$2,650.00"
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select count(*) from leases").fetchone()[0] == 2


def test_no_rent_and_no_research_is_an_error(seeded, tmp_path):
    with pytest.raises(ValueError, match="pass --rent"):
        lease.run(_settings(tmp_path), property_id=seeded, start=None, end=None,
                  rent=None, other_terms=None, dry_run=True)
