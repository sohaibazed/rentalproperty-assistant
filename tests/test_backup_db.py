"""Backup and restore round trip against Postgres: the backup is read as the rental_agent
role, the restore is loaded as the admin (needs supabase/schema.sql applied)."""

import os
import zipfile
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from rental_agent import attention, backup

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")
TODAY = date(2026, 11, 18)
TABLES = ("llcs, properties, tenants, leases, lease_tenants, rent_payments, expenses, "
          "rent_research, tax_protests, email_ingest_log, sync_runs, job_runs, claude_usage, "
          "chats, chat_messages, reference_documents, move_outs, deposit_deductions, "
          "expense_categories")


def admin(*statements):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        for sql in statements:
            conn.execute(sql)


def snapshot() -> dict[str, list]:
    with psycopg.connect(ADMIN) as conn:
        return {t: conn.execute(f"select * from {t} order by 1, 2").fetchall()
                for t in TABLES.split(", ")}


@pytest.fixture
def seeded(tmp_path):
    admin(f"truncate {TABLES} restart identity cascade",
          "insert into properties (address, city, zip) values ('12 Oak St', 'Plano', '75024')",
          "insert into tenants (full_name) values ('Ann Lee'), ('Bo, \"Jr\" Lee')",
          "insert into leases (property_id, start_date, end_date, monthly_rent) "
          "values (1, '2026-01-01', '2026-12-31', 2500)",
          "insert into lease_tenants (lease_id, tenant_id) values (1, 1), (1, 2)",
          "insert into rent_payments (lease_id, amount, paid_on) values (1, 2500, '2026-10-02')",
          "insert into expense_categories (name) values ('hoa_dues')",
          "insert into expenses (amount, spent_on, schedule_e_category) "
          "values (383.85, '2026-10-06', 'hoa_dues')",
          "insert into tax_protests (property_id, year, opinion, filed_on) "
          "values (1, 2027, 415000, '2027-05-03')",
          "insert into reference_documents (path, category, title, content) "
          "values ('imports/lease.pdf', 'lease', 'Lease', E'line one\\nline, \"two\"')",
          # A gap in the IDs: the restore must keep id 2 gone and continue after 3.
          "insert into chats (title, model) values ('one', 'm'), ('two', 'm'), ('three', 'm')",
          "delete from chats where id = 2")
    artifacts, memory = tmp_path / "artifacts", tmp_path / "memory"
    (artifacts / "2027").mkdir(parents=True)
    (artifacts / "2027/lease.pdf").write_bytes(b"%PDF signed")
    memory.mkdir()
    (memory / "memory.md").write_text("- my rule\n")
    return artifacts, memory


def test_round_trip_restores_every_row_and_file(seeded, tmp_path):
    artifacts, memory = seeded
    made = backup.create(DB, artifacts, memory, TODAY)
    before = snapshot()
    assert made.path == artifacts / "backups/rental-backup-2026-11-18.zip"
    assert (made.tables, made.files) == (19, 2)
    with zipfile.ZipFile(made.path) as zf:
        assert not [n for n in zf.namelist() if "backups/" in n]    # never backs up itself

    admin(f"truncate {TABLES} restart identity cascade")
    new_artifacts, new_memory = tmp_path / "new/artifacts", tmp_path / "new/memory"
    result = backup.restore(made.path, ADMIN, new_artifacts, new_memory)

    assert snapshot() == before
    assert (result.tables, result.written) == (19, 2)
    assert (new_artifacts / "2027/lease.pdf").read_bytes() == b"%PDF signed"
    assert (new_memory / "memory.md").read_text() == "- my rule\n"
    # ID counters continue after the restored rows, as the app's own role.
    with psycopg.connect(DB) as conn:
        assert conn.execute("insert into chats (title, model) values ('four', 'm') returning id"
                            ).fetchone()[0] == 4
        assert conn.execute("insert into tenants (full_name) values ('New') returning id"
                            ).fetchone()[0] == 3


def test_same_day_backup_replaces_the_file_and_leaves_no_partial(seeded):
    artifacts, memory = seeded
    backup.create(DB, artifacts, memory, TODAY)
    backup.create(DB, artifacts, memory, TODAY)
    assert [p.name for p in (artifacts / "backups").iterdir()] == ["rental-backup-2026-11-18.zip"]


def test_restore_refuses_a_database_with_rows(seeded, tmp_path):
    artifacts, memory = seeded
    made = backup.create(DB, artifacts, memory, TODAY)
    before = snapshot()
    with pytest.raises(RuntimeError, match="already has rows"):
        backup.restore(made.path, ADMIN, tmp_path / "a", tmp_path / "m")
    assert snapshot() == before
    assert not (tmp_path / "a").exists()


def test_attention_reports_a_failed_or_old_backup(seeded):
    def last_run(status, started):
        admin("truncate claude_usage, job_runs restart identity",
              f"insert into job_runs (job, status, started_at) values ('backup', '{status}', "
              f"'{started.isoformat()}')")
        with psycopg.connect(DB) as conn:
            return [i.text for i in attention.backup_items(conn, TODAY)]

    noon = datetime(2026, 11, 18, 18, tzinfo=UTC)
    assert last_run("success", noon) == []
    assert "failed" in last_run("failed", noon)[0]
    assert "newest backup is from" in last_run("success", noon - timedelta(days=5))[0]
