"""tax-report against a real Postgres, reading as the restricted rental_agent role."""

import os
from datetime import date
from pathlib import Path

import psycopg
import pytest

from rental_agent.config import Settings
from rental_agent.jobs import tax_report

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")


def test_report_from_database(tmp_path: Path):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate rent_payments, expenses, email_ingest_log, sync_runs, lease_tenants, leases, "
                     "tenants, properties restart identity cascade")
        pid = conn.execute("insert into properties (address) values ('12 Oak St') returning id").fetchone()[0]
        tid = conn.execute("insert into tenants (full_name) values ('Jane') returning id").fetchone()[0]
        lid = conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent) "
                           "values (%s, '2025-01-01', '2025-12-31', 1000) returning id",
                           (pid,)).fetchone()[0]
        conn.execute("insert into lease_tenants values (%s, %s)", (lid, tid))
        for month in range(1, 13):
            conn.execute("insert into rent_payments (lease_id, amount, paid_on) values (%s, 1000, %s)",
                         (lid, date(2025, month, 1)))
        conn.execute("insert into rent_payments (lease_id, amount, paid_on) values (%s, 1000, '2024-12-01')",
                     (lid,))  # other year, excluded
        conn.execute("insert into expenses (property_id, amount, spent_on, schedule_e_category) "
                     "values (%s, 250.50, '2025-06-01', 'repairs'), (null, 99.5, '2025-02-01', 'supplies')",
                     (pid,))

    settings = Settings(model="test", db_url=DB, artifacts_dir=tmp_path, reference_dir=tmp_path,
                        email_sources_path=tmp_path, soffice_bin="soffice")
    report, xlsx, csv_path = tax_report.run(settings, 2025)

    assert report.rent == {pid: 12000}
    assert report.total_expenses(pid) == 250.50
    assert report.total_expenses(None) == 99.5
    assert not any(f.kind in ("missing rent", "short rent") for f in report.flags)
    assert xlsx == tmp_path / "2025" / "tax" / "schedule-e-2025.xlsx" and xlsx.exists()
    assert csv_path.exists()
