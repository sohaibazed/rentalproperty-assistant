"""tax-protest against Postgres as the rental_agent role, with the district's data and the
agents faked."""

import asyncio
import json
import os
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from rental_agent import collin_cad, runner
from rental_agent.config import Settings
from rental_agent.jobs import tax_protest as tp

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")

RENT_COMPS = [{"address": f"{r} Elm St", "beds": 3, "baths": 2, "sqft": 1800, "rent": r,
               "date": None, "status": "listed", "source_url": "https://example.com/r"}
              for r in (2500, 2600, 2700)]


@pytest.fixture
def seeded():
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate rent_payments, expenses, email_ingest_log, sync_runs, "
                     "rent_research, lease_tenants, leases, tenants, properties "
                     "restart identity cascade")
        pid = conn.execute(
            "insert into properties (address, city, county, zip, property_type, beds, baths, sqft) "
            "values ('12 Oak St', 'Plano', 'Collin', '75024', 'single-family house', 3, 2, 1800) "
            "returning id").fetchone()[0]
        conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent) "
                     "values (%s, '2026-01-01', '2026-12-31', 2500)", (pid,))
        conn.execute("insert into rent_research (property_id, year, comps, recommended_rent) "
                     "values (%s, 2027, %s::jsonb, 2600)", (pid, json.dumps(RENT_COMPS)))
        for amount, category, prop in ((7000, "taxes", pid), (2000, "insurance", pid),
                                       (9000, "mortgage_interest", pid),   # not a running cost
                                       (300, "legal_professional", None)):  # LLC-wide
            conn.execute("insert into expenses (property_id, amount, spent_on, "
                         "schedule_e_category) values (%s, %s, '2026-06-01', %s)",
                         (prop, amount, category))
    return pid


def _record(street_address, city, year, cad_id):
    assert (street_address, city, year, cad_id) == ("12 Oak St", "Plano", 2027, None)
    home = tp.Home(101, "12 OAK ST", 1995, 1800, False, Decimal(450000))
    similar = [tp.Home(200 + i, f"{200 + i} ELM ST", 1996, 1800, False, Decimal(v))
               for i, v in enumerate((396000, 405000, 414000, 423000, 432000))]
    return tp.Record(home, "OAK ST LLC", "N0617", 2027, Decimal(430000), similar,
                     date(2027, 4, 15), date(2027, 5, 17),
                     collin_cad.Dataset("abcd-1234", "Collin CAD Appraisal Data - 2027"), None)


async def _agents(facts, settings):
    assert facts.income.rent == Decimal(2600)            # median of the saved rent comps
    return {"sales": [], "cap_rate": 5.0, "cap_rate_source_url": "https://example.com/cap",
            "market_notes": "", "opinion_of_value": 415000, "rationale": "Because comps."}


def test_writes_the_report_from_saved_rent_research_and_expenses(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "load_record", _record)
    monkeypatch.setattr(tp, "ask_agents", _agents)
    settings = Settings(model="test", db_url=DB, artifacts_dir=tmp_path, reference_dir=tmp_path,
                        email_sources_path=tmp_path)
    params = runner.parse_params("tax-protest", {"property_id": str(seeded), "year": "2027"})
    outcome = asyncio.run(runner.run_job("tax-protest", params, settings))

    report = tmp_path / "2027" / "tax-protest" / "12-oak-st.md"
    assert outcome.ok and outcome.outputs == [report]
    assert outcome.summary[0] == ("District value $450,000 (2027); opinion of value $415,000 "
                                  "from 5 similar homes and 0 sales.")
    assert outcome.summary[1] == "File by May 17, 2027."
    assert any("1 LLC-wide expense(s) in 2026" in line for line in outcome.summary)
    md = report.read_text()
    assert "# Property tax protest 2027: 12 Oak St, Plano, TX 75024" in md
    assert "**Operating expenses recorded for 2026:** $9,000 (insurance $2,000, taxes $7,000)" in md
    assert "**Net income:** $22,200 a year" in md        # $2,600 x 12 - $9,000
    assert "the income supports **$444,000**" in md
    assert md.count("[link](https://example.com/r)") == 3

    with psycopg.connect(ADMIN) as conn:                  # the job saves nothing
        assert conn.execute("select count(*), max(recommended_rent) from rent_research") \
            .fetchone() == (1, Decimal("2600.00"))


def test_only_collin_county(seeded, tmp_path):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("update properties set county = 'Dallas'")
    settings = Settings(model="test", db_url=DB, artifacts_dir=tmp_path, reference_dir=tmp_path,
                        email_sources_path=tmp_path)
    with pytest.raises(ValueError, match="Collin County only; this property's county is Dallas"):
        asyncio.run(tp.run(settings, property_id=seeded, year=2027, cad_id=None,
                           hearing_date=None))
