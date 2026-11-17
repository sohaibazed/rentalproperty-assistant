"""Needs-attention checks against Postgres as the rental_agent role, and where they show."""

import os
from datetime import date

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import attention
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


@pytest.fixture
def empty_db():
    admin("truncate chat_messages, chats, job_runs, rent_payments, expenses, lease_tenants, "
          "leases, tenants, rent_research, properties restart identity cascade",
          "insert into properties (address, city, zip) values ('12 Oak St', 'Plano', '75024')")


@pytest.fixture
def leased(empty_db):
    """A 2026 lease at $2,500: August short, September and October paid, November not yet."""
    admin("insert into leases (property_id, start_date, end_date, monthly_rent) "
          "values (1, '2026-01-01', '2026-12-31', 2500)",
          "insert into rent_payments (lease_id, amount, paid_on) values "
          "(1, 2384, '2026-08-03'), (1, 2500, '2026-09-01'), (1, 2500, '2026-10-02')")


def texts(found):
    return [(item.level, item.text) for item in found]


def rent(today):
    with psycopg.connect(DB) as conn:
        return texts(attention.rent_items(conn, today))


def renewal(today):
    with psycopg.connect(DB) as conn:
        return texts(attention.renewal_items(conn, today))


def test_rent_covers_the_last_three_months_and_this_month_from_the_12th(leased):
    short = ("now", "12 Oak St: $2,384.00 of $2,500.00 recorded for August 2026.")
    missing = ("now", "12 Oak St: no rent recorded for November 2026 ($2,500.00 due).")
    assert rent(date(2026, 11, 11)) == [short]         # November isn't judged before the 12th
    assert rent(date(2026, 11, 12)) == [missing]       # and August has aged out

    # A payment the owner removed on the Review page no longer counts.
    admin("update rent_payments set removed_at = now() where paid_on = '2026-10-02'")
    assert rent(date(2026, 11, 12)) == [
        ("now", "12 Oak St: no rent recorded for October 2026 ($2,500.00 due)."), missing]

    # Across the new year. January 2027 has no lease, so it isn't rent that is missing.
    assert [t for _, t in rent(date(2027, 1, 20))] == [
        "12 Oak St: no rent recorded for November 2026 ($2,500.00 due).",
        "12 Oak St: no rent recorded for December 2026 ($2,500.00 due)."]


def test_renewal_reminder_follows_the_notice_date(leased):
    assert renewal(date(2026, 10, 1)) == []            # 46 days before the notice date
    due_soon = ("12 Oak St: the lease ends on December 31, 2026. Get the extension signed by "
                "November 16, 2026 (45 days left). Start with rent research.")
    assert renewal(date(2026, 10, 2)) == [("soon", due_soon)]
    assert "(1 day left)" in renewal(date(2026, 11, 15))[0][1]
    assert "November 16, 2026 (today)" in renewal(date(2026, 11, 16))[0][1]
    level, text = renewal(date(2026, 11, 17))[0]
    assert level == "now" and "45-day notice date (November 16, 2026) has passed" in text
    level, text = renewal(date(2027, 1, 5))[0]
    assert level == "now" and "ended on December 31, 2026 and no signed extension is recorded" in text


def test_renewal_reminder_names_the_next_step_until_the_extension_is_signed(leased):
    def next_step(today):
        return renewal(today)[0][1].split(". ", 1)[1] if renewal(today) else None

    assert next_step(date(2027, 1, 5)) == "Start with rent research."
    admin("insert into rent_research (property_id, year, comps, recommended_rent) "
          "values (1, 2027, '[]', 2600)")
    assert renewal(date(2026, 11, 17))[0][1].endswith(
        "The rent research is saved. Generate the extension next.")

    # generate-lease records the extension as unsigned: the deadline still counts.
    admin("insert into leases (property_id, start_date, end_date, monthly_rent, "
          "original_lease_date, awaiting_signature) "
          "values (1, '2027-01-01', '2027-12-31', 2600, '2026-01-01', true)")
    level, text = renewal(date(2026, 11, 17))[0]
    assert level == "now" and "(November 16, 2026) has passed" in text
    assert text.endswith("The extension is written. Get it signed, then import the signed copy.")
    with psycopg.connect(DB) as conn:
        assert [i.link for i in attention.renewal_items(conn, date(2026, 11, 17))] == ["/renewal"]

    # Importing the signed copy makes it the current term, and the reminder stops.
    admin("update leases set awaiting_signature = false")
    assert renewal(date(2026, 11, 17)) == [] and renewal(date(2027, 1, 5)) == []


def test_urgent_items_come_first(empty_db):
    with psycopg.connect(DB) as conn:
        assert attention.items(conn, date(2026, 11, 16)) == []
    admin("insert into expenses (amount, spent_on, schedule_e_category, needs_review) "
          "values (10, '2026-05-01', 'supplies', true), (20, '2026-05-02', 'supplies', true)",
          "insert into job_runs (job, status, started_at) values "
          "('sync-email', 'success', '2026-11-01 15:00+00'), "
          "('sync-email', 'failed', '2026-11-11 15:00+00')")
    with psycopg.connect(DB) as conn:
        found = attention.items(conn, date(2026, 11, 16))
    assert [(i.level, i.link) for i in found] == [("now", "/runs/2"), ("soon", "/review")]
    assert found[0].text.startswith("The last email sync failed on November 11, 2026.")
    assert found[1].text == "2 rows need a look on the Review page."


@pytest.fixture
def client(empty_db, tmp_path, monkeypatch):
    for k, v in {"RENTAL_UI_PASSWORD": "pw", "RENTAL_UI_USERNAME": "owner@example.com",
                 "RENTAL_SCHEDULER": "off", "RENTAL_ARTIFACTS_DIR": str(tmp_path),
                 "RENTAL_DATA_DIR": str(tmp_path / "data"),
                 "RENTAL_MEMORY_FILE": str(tmp_path / "memory.md")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RENTAL_SECRET_KEY", raising=False)
    store = SecretStore(tmp_path / "s.enc", Fernet.generate_key().decode())
    store.set("RENTAL_DB_URL", DB)
    app = create_app(store=store, manager=JobManager(store), chats=ChatManager(store))
    with TestClient(app) as c:
        c.post("/login", data={"email": "owner@example.com", "password": "pw"}, headers=ORIGIN)
        yield c


def test_dashboard_panel_and_chat_home_count(client):
    # No lease is recorded here, so nothing depends on the date the test runs.
    assert "Nothing needs your attention." in client.get("/dashboard").text
    assert "your attention" not in client.get("/").text

    admin("insert into expenses (amount, spent_on, schedule_e_category, needs_review) "
          "values (10, '2026-05-01', 'supplies', true)",
          "insert into job_runs (job, status) values ('sync-email', 'failed')")
    page = client.get("/dashboard").text
    assert '<li class="now">The last email sync failed on' in page
    assert '<a href="/runs/1">Open the run</a>' in page
    assert '<li class="soon">1 row needs a look on the Review page. <a href="/review">' in page
    assert " ".join(client.get("/").text.split()).count("2 items need your attention") == 1
