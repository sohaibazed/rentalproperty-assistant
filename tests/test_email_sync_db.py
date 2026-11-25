"""End-to-end sync against a real Postgres, as the restricted rental_agent role.

Needs TEST_DATABASE_URL (a rental_agent login on a database with supabase/schema.sql applied)
and TEST_ADMIN_DATABASE_URL (to seed properties/leases, which the app never inserts).
"""

import asyncio
import os
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from rental_agent import gmail
from rental_agent.config import Settings
from rental_agent.jobs import email_sync

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")

MESSAGES = {
    "rent1": gmail.EmailMessage("rent1", "bank@x.com", "Deposit", datetime(2026, 11, 2, tzinfo=UTC),
                                "Received $1800 from Jane for 12 Oak St"),
    "exp1": gmail.EmailMessage("exp1", "hd@y.com", "Receipt", datetime(2026, 11, 3, tzinfo=UTC),
                               "Total $54.20", [gmail.Attachment("a1", "receipt.pdf", "application/pdf")]),
    "spam": gmail.EmailMessage("spam", "promo@z.com", "Sale", datetime(2026, 11, 4, tzinfo=UTC), "50% off"),
}

EXTRACTIONS = {
    "rent1": {"kind": "rent", "amount": 1800, "date": "2026-11-02", "counterparty": "Jane",
              "description": None, "category": None, "property_id": None, "confidence": 0.95,
              "reason": "deposit"},
    "exp1": {"kind": "expense", "amount": 54.2, "date": "2026-11-03", "counterparty": "Home Depot",
             "description": "parts", "category": "repairs", "property_id": None, "confidence": 0.9,
             "reason": "receipt"},
    "spam": {"kind": "ignore", "amount": None, "date": None, "counterparty": None,
             "description": None, "category": None, "property_id": None, "confidence": 0.99,
             "reason": "marketing"},
}


@pytest.fixture
def seeded():
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate rent_payments, expenses, email_ingest_log, sync_runs, lease_tenants, leases, "
                     "tenants, properties restart identity cascade")
        pid = conn.execute("insert into properties (address) values ('12 Oak St') returning id").fetchone()[0]
        tid = conn.execute("insert into tenants (full_name) values ('Jane') returning id").fetchone()[0]
        lid = conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent) "
                           "values (%s, '2026-01-01', '2026-12-31', 1800) returning id", (pid,)).fetchone()[0]
        conn.execute("insert into lease_tenants values (%s, %s)", (lid, tid))
    for kind in EXTRACTIONS.values():
        if kind["kind"] == "rent":
            kind["property_id"] = pid
    return pid


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(gmail, "get_service", lambda _p: object())
    monkeypatch.setattr(gmail, "search_message_ids", lambda _s, _q: list(MESSAGES))
    monkeypatch.setattr(gmail, "get_message", lambda _s, mid: MESSAGES[mid])
    monkeypatch.setattr(gmail, "get_attachment_bytes", lambda _s, _m, _a: b"%PDF-fake")

    async def fake_extract(msg, _props, _categories, _model, _env, _db_url):
        return dict(EXTRACTIONS[msg.message_id])

    monkeypatch.setattr(email_sync, "extract", fake_extract)
    sources = tmp_path / "sources.toml"
    sources.write_text('[rent]\nsenders=["bank@x.com"]\n')
    return Settings(model="test", db_url=DB, artifacts_dir=tmp_path / "artifacts",
                    reference_dir=tmp_path, email_sources_path=sources,
                    gmail_token="{}")


def test_sync_inserts_then_dedupes(seeded, fakes):
    first = asyncio.run(email_sync.run(fakes, since=date(2026, 11, 1), dry_run=False))
    assert (first.rent, first.expenses, first.ignored, first.errors) == (1, 1, 1, [])

    with psycopg.connect(ADMIN) as conn:
        lease_id, review = conn.execute("select lease_id, needs_review from rent_payments").fetchone()
        assert lease_id is not None and review is False
        receipt = conn.execute("select receipt_path from expenses").fetchone()[0]
        assert (fakes.artifacts_dir / receipt).read_bytes() == b"%PDF-fake"
        assert conn.execute("select status from sync_runs").fetchone()[0] == "success"

    second = asyncio.run(email_sync.run(fakes, since=None, dry_run=False))
    assert second.already_processed == 3
    assert second.rent + second.expenses + second.ignored == 0
    assert second.since == date.today() - timedelta(days=2)  # last success minus overlap
    assert (fakes.artifacts_dir / str(date.today().year)).glob("sync-*.md")


def test_dry_run_writes_nothing(seeded, fakes):
    report = asyncio.run(email_sync.run(fakes, since=date(2026, 11, 1), dry_run=True))
    assert report.rent == 1
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select count(*) from email_ingest_log").fetchone()[0] == 0
        assert conn.execute("select count(*) from sync_runs").fetchone()[0] == 0


def test_role_cannot_delete(seeded):
    with psycopg.connect(DB) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("delete from expenses")
