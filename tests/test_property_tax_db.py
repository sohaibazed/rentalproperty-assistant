"""The Property tax page and its Dashboard reminder against Postgres as the rental_agent
role."""

import os
from datetime import date

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import attention, property_tax
from rental_agent.jobs import tax_protest as tp
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
def collin_property():
    admin("truncate chat_messages, chats, job_runs, rent_payments, expenses, lease_tenants, "
          "leases, tenants, rent_research, tax_protests, properties restart identity cascade",
          "insert into properties (address, city, county, zip) "
          "values ('12 Oak St', 'Plano', 'Collin', '75024')")


def seasons(today: date) -> list[property_tax.Season]:
    with psycopg.connect(DB) as conn:
        return property_tax.load(conn, today)


def reminders(today: date) -> list[str]:
    with psycopg.connect(DB) as conn:
        return [item.text for item in attention.tax_items(conn, today)]


def status(season: property_tax.Season) -> dict[str, str]:
    """The dates that have a mark, by how they are printed."""
    return {row.when: row.status for row in season.dates if row.status}


def test_the_season_follows_the_notice_the_job_the_filing_and_the_hearing(collin_property):
    # November: the next season is 2027, and only the rule gives its dates.
    [s] = seasons(date(2026, 11, 19))
    assert (s.year, s.step, s.prepared) == (2027, "wait", False)
    assert (s.deadline, s.deadline_is_official) == (date(2027, 5, 17), False)   # May 15: Saturday
    assert (s.headline.day, s.headline.left) == (date(2027, 5, 17), "179 days left")
    assert "expected; the notice prints the exact date" in s.headline.text
    assert status(s) == {"Before February 1, 2027": "next"}      # the next day not to miss
    assert reminders(date(2026, 11, 19)) == reminders(date(2027, 4, 14)) == []

    # A run before the notice uses last year's values: a preview, so step 1 stays open.
    admin("insert into tax_protests (property_id, year, district_value, value_year, opinion, "
          "report_path, prepared_at) values (1, 2027, 440000, 2026, 430000, 'r.md', now())")
    [s] = seasons(date(2027, 4, 20))
    assert (s.step, s.prepared) == ("prepare", False)
    assert reminders(date(2027, 4, 20)) == [
        ("12 Oak St: the district mails the 2027 appraisal notice about April 15, 2027. File the "
         "protest by May 17, 2027 (27 days left); the notice prints the exact date. Start by "
         "preparing the evidence.")]

    # A run on this year's values brings the notice's own dates.
    admin("update tax_protests set value_year = 2027, notice_date = '2027-04-16', "
          "deadline = '2027-05-18'")
    [s] = seasons(date(2027, 4, 20))
    assert (s.step, s.prepared, s.deadline, s.deadline_is_official) == (
        "file", True, date(2027, 5, 18), True)
    assert status(s)["April 16, 2027"] == "passed" and status(s)["May 18, 2027"] == "next"
    assert reminders(date(2027, 4, 20)) == [
        ("12 Oak St: the district mailed the 2027 appraisal notice on April 16, 2027. File the "
         "protest by May 18, 2027 (28 days left).")]
    assert reminders(date(2027, 5, 18))[0].endswith("File the protest by May 18, 2027 (today).")

    # Not filed by the deadline.
    [s] = seasons(date(2027, 5, 19))
    assert (s.step, s.headline.late, s.headline.left) == ("late", True, "")
    assert "A late protest needs good cause" in s.headline.text
    assert reminders(date(2027, 5, 19)) == []

    # Filed: the reminder stops, and the page waits for the hearing date.
    admin("update tax_protests set filed_on = '2027-05-03'")
    [s] = seasons(date(2027, 5, 10))
    assert (s.step, s.headline.day) == ("hearing", None)
    assert s.headline.text.startswith("Filed on May 3, 2027.")
    assert status(s)["May 18, 2027"] == "done" and status(s)["Before February 1, 2028"] == "next"
    assert reminders(date(2027, 5, 10)) == []

    # With the hearing date: first the day to upload the evidence, then the hearing itself.
    admin("update tax_protests set hearing_date = '2027-06-14'")
    [s] = seasons(date(2027, 6, 1))
    assert (s.hearing, s.evidence_due) == (date(2027, 6, 14), date(2027, 6, 9))
    assert (s.headline.day, s.headline.left) == (date(2027, 6, 9), "8 days left")
    assert status(s)["May 31, 2027"] == "passed" and status(s)["June 9, 2027"] == "next"
    [s] = seasons(date(2027, 6, 10))
    assert (s.headline.day, s.headline.text, s.headline.left) == (
        date(2027, 6, 14), "Your hearing.", "4 days left")
    [s] = seasons(date(2027, 6, 15))
    assert s.headline.text.startswith("The hearing was on this day.") and s.headline.left == ""

    # From August the page moves to the next year, and keeps a hearing that is still to come.
    assert [s.year for s in seasons(date(2027, 8, 5))] == [2028]
    admin("update tax_protests set hearing_date = '2027-08-20'")
    assert [s.year for s in seasons(date(2027, 8, 5))] == [2027, 2028]


def test_only_collin_county(collin_property):
    admin("update properties set county = 'Dallas'")
    assert seasons(date(2027, 4, 20)) == [] and reminders(date(2027, 4, 20)) == []


@pytest.fixture
def client(collin_property, tmp_path, monkeypatch):
    for name, value in {"RENTAL_UI_PASSWORD": "pw", "RENTAL_UI_USERNAME": "owner@example.com",
                        "RENTAL_SCHEDULER": "off", "RENTAL_ARTIFACTS_DIR": str(tmp_path),
                        "RENTAL_DATA_DIR": str(tmp_path / "data"),
                        "RENTAL_MEMORY_FILE": str(tmp_path / "memory.md")}.items():
        monkeypatch.setenv(name, value)
    for name in ("RENTAL_SECRET_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    store = SecretStore(tmp_path / "s.enc", Fernet.generate_key().decode())
    store.set("RENTAL_DB_URL", DB)
    app = create_app(store=store, manager=JobManager(store), chats=ChatManager(store))
    with TestClient(app) as client:
        client.post("/login", data={"email": "owner@example.com", "password": "pw"},
                    headers=ORIGIN)
        yield client


def test_page_shows_the_dates_the_files_and_what_the_owner_records(client):
    year = tp.default_year(date.today())

    def page() -> str:
        return " ".join(client.get("/property-tax").text.split())

    def record(data, target=f"/property-tax/1/{year}"):
        return client.post(target, data=data, headers=ORIGIN, follow_redirects=True)

    html = page()
    assert '<a href="/property-tax" aria-current="page">Property tax</a>' in html
    assert f"12 Oak St: {year} tax year" in html and "Last day to file the protest" in html
    assert "Not prepared yet." in html and "Prepare the protest" in html
    assert '<input type="hidden" name="back" value="property-tax">' in html
    assert f"The {year - 1} taxes are due." in html

    # After the job has run: the values and both files.
    admin("insert into tax_protests (property_id, year, district_value, value_year, opinion, "
          f"report_path, packet_path, prepared_at) values (1, {year}, 450000, {year}, 415000, "
          f"'{year}/tax-protest/12-oak-st.md', '{year}/tax-protest/12-oak-st-evidence.pdf', "
          "now())")
    html = page()
    assert f"District's value $450,000 ({year}); opinion of value $415,000." in html
    assert f'<a href="/report/{year}/tax-protest/12-oak-st.md">Read the report</a>' in html
    assert (f'<a href="/files/{year}/tax-protest/12-oak-st-evidence.pdf">'
            "Open the evidence PDF</a>") in html
    assert "Prepare it again" in html and "This is a preview" not in html

    # The filing day and the hearing date are saved one at a time; neither clears the other.
    assert "Saved." in record({"filed_on": f"{year}-05-03"}).text
    assert "Saved." in record({"hearing_date": f"{year}-06-14"}).text
    html = page()
    assert f"Filed on May 3, {year}." in html and '<span class="pill success">filed</span>' in html
    assert f"The hearing is on <strong>June 14, {year}</strong>" in html
    assert f'name="filed_on" value="{year}-05-03"' in html
    record({"hearing_date": ""})                          # an empty date clears it
    assert "The board sends the hearing date at least 15 days ahead. Record it here" in page()
    assert f'name="filed_on" value="{year}-05-03"' in page()

    assert "Not saved: that is not a date." in record({"filed_on": "soon"}).text
    assert record({"filed_on": f"{year}-05-03"}, f"/property-tax/1/{year - 5}").status_code == 404
    assert record({"filed_on": f"{year}-05-03"}, f"/property-tax/9/{year}").status_code == 404

    # A job the page starts comes back to it when it can't start, and its run links back.
    back = client.post("/jobs/tax-protest", headers=ORIGIN, follow_redirects=False,
                       data={"property_id": "", "back": "property-tax"})
    assert back.headers["location"] == "/property-tax"
    admin("insert into job_runs (job, trigger, status, params) values "
          "('tax-protest', 'manual', 'failed', '{\"property_id\": 1}')")
    assert 'The last run failed: <a href="/runs/1">run #1</a>' in page()
    assert '<a href="/property-tax">Back to Property tax</a>' in client.get("/runs/1").text


def test_a_database_without_the_new_table_points_to_the_schema(client):
    admin("alter table tax_protests rename to tax_protests_away")
    try:
        answer = client.get("/property-tax", follow_redirects=True)
    finally:
        admin("alter table tax_protests_away rename to tax_protests")
    assert "Run supabase/schema.sql again" in answer.text
