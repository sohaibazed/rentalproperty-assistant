"""research-rent against Postgres as the rental_agent role, with the agents faked."""

import asyncio
import os
import shutil
import time
from decimal import Decimal

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import runner
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


def test_dry_run_saves_nothing_and_keeps_the_saved_report(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "ask_agents", _fake_agents(2600, (2500, 2600, 2700, 2800, 2900)))
    asyncio.run(rr.run(_settings(tmp_path), property_id=seeded, year=None, dry_run=False))
    saved = tmp_path / "2027" / "rent-research" / "12-oak-st.md"
    before = saved.read_text()
    monkeypatch.setattr(rr, "ask_agents", _fake_agents(2700, (2600, 2700, 2800, 2900, 3000)))
    _, report = asyncio.run(rr.run(_settings(tmp_path), property_id=seeded, year=None,
                                   dry_run=True))
    assert report.name == "12-oak-st-dry-run.md" and rr.DRY_RUN_NOTE in report.read_text()
    assert saved.read_text() == before                       # saved report untouched
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select recommended_rent from rent_research").fetchall() == \
            [(Decimal("2600.00"),)]


def test_save_dry_run_from_the_run_page(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("RENTAL_ARTIFACTS_DIR", str(tmp_path))
    for k, v in {"RENTAL_UI_PASSWORD": "pw", "RENTAL_UI_USERNAME": "owner@example.com",
                 "RENTAL_SCHEDULER": "off", "RENTAL_DATA_DIR": str(tmp_path / "data"),
                 "RENTAL_MEMORY_FILE": str(tmp_path / "memory.md")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RENTAL_SECRET_KEY", raising=False)
    monkeypatch.setattr(rr, "ask_agents", _fake_agents(2700, (2600, 2700, 2800, 2900, 3000)))
    from rental_agent.web.app import create_app
    from rental_agent.web.chat_views import ChatManager
    from rental_agent.web.jobs import JobManager
    from rental_agent.web.secrets import SecretStore

    store = SecretStore(tmp_path / "s.enc", Fernet.generate_key().decode())
    store.set("RENTAL_DB_URL", DB)
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate job_runs restart identity")
    origin = {"origin": "http://testserver"}
    app = create_app(store=store, manager=JobManager(store, run_fn=runner.run_job),
                     chats=ChatManager(store))
    with TestClient(app) as c:
        c.post("/login", data={"email": "owner@example.com", "password": "pw"}, headers=origin)
        c.post("/jobs/research-rent", data={"property_id": str(seeded), "dry_run": "on"},
               headers=origin)
        for _ in range(200):
            with psycopg.connect(ADMIN) as conn:
                status = conn.execute("select status from job_runs where id = 1").fetchone()[0]
            if status != "running":
                break
            time.sleep(0.05)
        assert status == "success"
        assert "Save this recommendation" in c.get("/runs/1").text

        page = c.post("/runs/1/save-research", headers=origin, follow_redirects=True).text
        assert "Recommendation saved" in page and "Save this recommendation" not in page

        folder = tmp_path / "2027" / "rent-research"
        assert sorted(p.name for p in folder.iterdir()) == ["12-oak-st.md"]
        assert rr.DRY_RUN_NOTE not in (folder / "12-oak-st.md").read_text()
        with psycopg.connect(ADMIN) as conn:
            assert conn.execute("select recommended_rent, report_path from rent_research") \
                .fetchall() == [(Decimal("2700.00"), "2027/rent-research/12-oak-st.md")]
            params, outputs, summary = conn.execute(
                "select params, outputs, summary from job_runs where id = 1").fetchone()
        assert params["dry_run"] is False and outputs == ["2027/rent-research/12-oak-st.md"]
        assert "Saved from this dry run" in summary and "not saved" not in summary
        again = c.post("/runs/1/save-research", headers=origin, follow_redirects=True).text
        assert "Only a successful dry run" in again


def test_missing_property_facts_reported(seeded, tmp_path):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("update properties set sqft = null, property_type = null")
    with pytest.raises(ValueError, match="property_type.*sqft"):
        asyncio.run(rr.run(_settings(tmp_path), property_id=seeded, year=None, dry_run=True))
