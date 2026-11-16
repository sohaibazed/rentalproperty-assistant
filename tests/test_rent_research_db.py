"""research-rent against Postgres as the rental_agent role, with the agents faked."""

import asyncio
import os
import shutil
from decimal import Decimal

import psycopg
import pytest

from rental_agent.config import Settings
from rental_agent.jobs import lease
from rental_agent.jobs import rent_research as rr

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")


def _comps(*rents):
    return [{"address": f"{r} Elm St", "beds": 3, "baths": 2, "sqft": 1800, "rent": r,
             "date": None, "status": "listed", "source_url": "https://example.com/c"} for r in rents]


@pytest.fixture
def seeded():
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate rent_payments, expenses, email_ingest_log, sync_runs, rent_research, "
                     "lease_tenants, leases, tenants, properties restart identity cascade")
        pid = conn.execute(
            "insert into properties (address, city, county, zip, landlord_names, property_type, "
            "beds, baths, sqft) values ('12 Oak St', 'Plano', 'Collin', '75024', "
            "array['Lee Landlord'], 'single-family house', 3, 2, 1800) returning id").fetchone()[0]
        tid = conn.execute("insert into tenants (full_name) values ('Ann') returning id").fetchone()[0]
        lid = conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent) "
                           "values (%s, '2026-01-01', '2026-12-31', 2500) returning id",
                           (pid,)).fetchone()[0]
        conn.execute("insert into lease_tenants values (%s, %s)", (lid, tid))
    return pid


def _settings(tmp_path):
    return Settings(model="test", db_url=DB, artifacts_dir=tmp_path, reference_dir=tmp_path,
                    gmail_client_secrets=tmp_path, gmail_token_path=tmp_path,
                    email_sources_path=tmp_path, soffice_bin="soffice")


def _fake_agents(rec, rents):
    async def fake(facts, settings):
        assert facts.year == 2027 and facts.property_type == "single-family house"
        return {"comps": _comps(*rents), "market_notes": "", "recommended_rent": rec,
                "range_low": rec - 100, "range_high": rec + 100, "rationale": "Because comps."}
    return fake


def test_saves_then_updates_and_feeds_generate_lease(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "ask_agents", _fake_agents(2600, (2500, 2550, 2600, 2650, 2700)))
    _, report = asyncio.run(rr.run(_settings(tmp_path), property_id=seeded, year=None,
                                        dry_run=False))
    assert report == tmp_path / "2027" / "rent-research" / "12-oak-st.md" and report.exists()

    monkeypatch.setattr(rr, "ask_agents", _fake_agents(2650, (2600, 2650, 2700, 2750, 2800)))
    asyncio.run(rr.run(_settings(tmp_path), property_id=seeded, year=None, dry_run=False))

    with psycopg.connect(ADMIN) as conn:
        rows = conn.execute("select year, recommended_rent, jsonb_array_length(comps) "
                            "from rent_research").fetchall()
    assert rows == [(2027, Decimal("2650.00"), 5)]   # re-run replaced, not duplicated

    if shutil.which("soffice"):
        out = lease.run(_settings(tmp_path), property_id=seeded, start=None, end=None, rent=None,
                        other_terms=None, dry_run=True)
        assert out.context["monthly_rent"] == "$2,650.00"


def test_dry_run_saves_nothing(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "ask_agents", _fake_agents(2600, (2500, 2600, 2700, 2800, 2900)))
    asyncio.run(rr.run(_settings(tmp_path), property_id=seeded, year=None, dry_run=True))
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select count(*) from rent_research").fetchone()[0] == 0


def test_missing_property_facts_reported(seeded, tmp_path):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("update properties set sqft = null, property_type = null")
    with pytest.raises(ValueError, match="property_type.*sqft"):
        asyncio.run(rr.run(_settings(tmp_path), property_id=seeded, year=None, dry_run=True))
