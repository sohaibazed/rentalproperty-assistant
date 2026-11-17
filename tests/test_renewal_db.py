"""The renewal workflow against Postgres as the rental_agent role: its steps, the research run
the scheduler starts, and the Renewal page."""

import asyncio
import os
from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import renewal
from rental_agent.web import jobs
from rental_agent.web.app import create_app
from rental_agent.web.chat_views import ChatManager
from rental_agent.web.jobs import JobManager
from rental_agent.web.secrets import SecretStore

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")
ORIGIN = {"origin": "http://testserver"}


def admin(*statements):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        for sql in statements:
            conn.execute(sql)


def lease(start: date, end: date, rent: int, *, unsigned: bool = False) -> str:
    return ("insert into leases (property_id, start_date, end_date, monthly_rent, pdf_path, "
            f"awaiting_signature) values (1, '{start}', '{end}', {rent}, 'leases/x.pdf', {unsigned})")


@pytest.fixture
def one_property():
    admin("truncate chat_messages, chats, job_runs, rent_payments, expenses, lease_tenants, "
          "leases, tenants, rent_research, properties restart identity cascade",
          "insert into properties (address, city, zip) values ('12 Oak St', 'Plano', '75024')")


def state(today: date) -> renewal.Renewal:
    with psycopg.connect(DB) as conn:
        [found] = renewal.load(conn, today)
    return found


def test_steps_run_from_research_to_the_signed_copy(one_property):
    with psycopg.connect(DB) as conn:
        assert renewal.load(conn, date(2026, 10, 2)) == []       # no signed lease, no renewal
    admin(lease(date(2026, 1, 1), date(2026, 12, 31), 2500))

    r = state(date(2026, 10, 1))                                 # the day before it opens
    assert (r.step, r.timing, r.auto_research) == ("wait", "early", False)
    assert (r.opens_on, r.sign_by) == (date(2026, 10, 2), date(2026, 11, 16))
    assert (r.next_start, r.next_end, r.year) == (date(2027, 1, 1), date(2027, 12, 31), 2027)

    r = state(date(2026, 10, 2))
    assert (r.step, r.timing, r.auto_research) == ("research", "open", True)
    assert r.time_left == "45 days left" and state(date(2026, 11, 16)).time_left == "today"

    admin("insert into rent_research (property_id, year, comps, recommended_rent, report_path) "
          "values (1, 2027, '[]', 2600, '2027/rent-research/12-oak-st.md')")
    r = state(date(2026, 11, 17))
    assert (r.step, r.timing, r.auto_research) == ("generate", "late", False)
    assert r.research["recommended_rent"] == Decimal(2600)

    # generate-lease records the extension as unsigned: the 2026 term is still the current one.
    admin(lease(date(2027, 1, 1), date(2027, 12, 31), 2650, unsigned=True))
    r = state(date(2027, 1, 5))
    assert (r.step, r.timing) == ("sign", "ended")
    assert (r.current.end_date, r.draft.monthly_rent) == (date(2026, 12, 31), Decimal(2650))

    # The signed copy is imported: the 2027 term is current and its own renewal waits.
    admin("update leases set awaiting_signature = false")
    r = state(date(2027, 1, 5))
    assert (r.step, r.draft, r.current.end_date) == ("wait", None, date(2027, 12, 31))

    # An unsigned extension that a later signed lease overtook is ignored.
    admin("update leases set awaiting_signature = true where end_date = '2026-12-31'")
    assert state(date(2027, 1, 5)).draft is None


@pytest.fixture
def store(tmp_path, monkeypatch):
    for name, value in {"RENTAL_UI_PASSWORD": "pw", "RENTAL_UI_USERNAME": "owner@example.com",
                        "RENTAL_SCHEDULER": "off", "RENTAL_ARTIFACTS_DIR": str(tmp_path),
                        "RENTAL_DATA_DIR": str(tmp_path / "data"),
                        "RENTAL_MEMORY_FILE": str(tmp_path / "memory.md")}.items():
        monkeypatch.setenv(name, value)
    for name in ("RENTAL_SECRET_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    store = SecretStore(tmp_path / "s.enc", Fernet.generate_key().decode())
    store.set("RENTAL_DB_URL", DB)
    return store


@pytest.fixture
def open_renewal(one_property):
    """A lease that ends 60 days from today, so its renewal is open. Returns the next term's year."""
    today = date.today()
    end = today + timedelta(days=60)
    admin(lease(today - timedelta(days=300), end, 2500))
    return (end + timedelta(days=1)).year


def test_scheduler_starts_rent_research_once(open_renewal, store):
    async def research_fails(job, params, settings):
        raise RuntimeError("no comps")

    manager = JobManager(store, run_fn=research_fails)

    async def tick():
        await jobs._tick(manager, date.today())
        while manager.tasks:
            await asyncio.gather(*manager.tasks)

    def runs():
        with psycopg.connect(ADMIN) as conn:
            return conn.execute("select job, trigger, status, params from job_runs "
                                "order by id").fetchall()

    asyncio.run(tick())
    assert runs() == []                                  # no Claude credentials: nothing to run
    store.set("ANTHROPIC_API_KEY", "test-key")
    asyncio.run(tick())
    scheduled = ("research-rent", "schedule", "failed",
                 {"property_id": 1, "year": open_renewal, "dry_run": False})
    assert runs() == [scheduled]
    asyncio.run(tick())                                  # it failed, and is not repeated
    assert runs() == [scheduled]
    assert state(date.today()).auto_research is False


def test_renewal_page_follows_the_steps(open_renewal, store):
    year = open_renewal
    app = create_app(store=store, manager=JobManager(store), chats=ChatManager(store))
    with TestClient(app) as client:
        client.post("/login", data={"email": "owner@example.com", "password": "pw"},
                    headers=ORIGIN)

        def renewal_page() -> str:
            return " ".join(client.get("/renewal").text.split())

        page = renewal_page()
        assert f'1. Research the rent for {year} <span class="pill running">next</span>' in page
        assert "The scheduler starts it once" in page and "Research the rent now" in page
        assert '<a href="/renewal">Open Renewal</a>' in client.get("/dashboard").text

        admin("insert into job_runs (job, trigger, status, params) values ('research-rent', "
              f"'schedule', 'failed', '{{\"property_id\": 1, \"year\": {year}}}')")
        page = renewal_page()
        assert 'The last research failed: <a href="/runs/1">run #1</a>' in page
        assert "It doesn't start by itself any more." in page

        admin("insert into rent_research (property_id, year, comps, recommended_rent, "
              f"report_path) values (1, {year}, '[]', 2600, 'r/12-oak-st.md')")
        page = renewal_page()
        assert "Recommended $2,600.00 a month" in page and "Research again" in page
        assert '2. Generate the extension <span class="pill running">next</span>' in page
        assert 'name="rent" step="0.01" min="0.01" required value="2600.00"' in page
        assert "The last research failed" not in page

        # A job that can't start comes back here, not to the Jobs page.
        page = client.post("/jobs/generate-lease", headers=ORIGIN, follow_redirects=True,
                           data={"property_id": "1", "rent": "0", "back": "renewal"}).text
        assert "Monthly rent must be positive" in page and "<h1>Renewal</h1>" in page

        start = date.today() + timedelta(days=61)
        admin(lease(start, start + timedelta(days=364), 2650, unsigned=True))
        page = renewal_page()
        assert "at $2,650.00 a month. <a href=\"/files/leases/x.pdf\">Open the PDF</a>" in page
        assert "Replace the extension" in page and 'value="2650.00"' in page
        assert ('3. Get it signed and import the signed copy '
                '<span class="pill running">next</span>') in page
        assert '<a class="pill" href="/renewal">awaiting signature</a>' in client.get("/leases").text

        admin("update leases set awaiting_signature = false")
        page = renewal_page()
        assert "Nothing to do yet. The renewal opens on" in page and "Research the rent" not in page
