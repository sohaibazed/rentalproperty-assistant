"""The move-out workflow against Postgres as the rental_agent role: the Leases page forms, the
reminders it stops and starts, the chat card and the tax report."""

import os
from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import attention, db, move_out, renewal
from rental_agent.jobs import tax_report
from rental_agent.web.app import create_app
from rental_agent.web.chat_views import ChatManager
from rental_agent.web.jobs import JobManager
from rental_agent.web.secrets import SecretStore

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")
ORIGIN = {"origin": "http://testserver"}
TODAY = date.today()
START, END = TODAY - timedelta(days=330), TODAY + timedelta(days=35)
MOVED = TODAY - timedelta(days=5)


def admin(*statements):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        for sql in statements:
            conn.execute(sql)


@pytest.fixture
def leased():
    """One property whose lease ends in 35 days, so its renewal is open and late."""
    admin("truncate chat_messages, chats, job_runs, rent_payments, expenses, lease_tenants, "
          "leases, tenants, rent_research, properties restart identity cascade",
          "insert into properties (address, city, zip, landlord_names) "
          "values ('12 Oak St', 'Plano', '75024', '{Sam Owner}')",
          "insert into tenants (full_name) values ('Ann Lee'), ('Bo Lee')",
          "insert into leases (property_id, start_date, end_date, monthly_rent, deposit) "
          f"values (1, '{START}', '{END}', 2500, 2400)",
          "insert into lease_tenants values (1, 1), (1, 2)")


@pytest.fixture
def client(leased, tmp_path, monkeypatch):
    for name, value in {"RENTAL_UI_PASSWORD": "pw", "RENTAL_UI_USERNAME": "owner@example.com",
                        "RENTAL_SCHEDULER": "off", "RENTAL_ARTIFACTS_DIR": str(tmp_path),
                        "RENTAL_DATA_DIR": str(tmp_path / "data"),
                        "RENTAL_MEMORY_FILE": str(tmp_path / "memory.md")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("RENTAL_SECRET_KEY", raising=False)
    store = SecretStore(tmp_path / "s.enc", Fernet.generate_key().decode())
    store.set("RENTAL_DB_URL", DB)
    manager = JobManager(store)
    with TestClient(create_app(store=store, manager=manager, chats=ChatManager(store))) as c:
        c.post("/login", data={"email": "owner@example.com", "password": "pw"}, headers=ORIGIN)
        yield c


def post(client, path, **data):
    r = client.post(path, data=data, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/leases#move-out"
    return " ".join(client.get("/leases").text.split())


def record(client, address="9 Elm St\nPlano TX 75001"):
    return post(client, "/leases/1/move-out", move_out_date=str(MOVED), deposit="2400",
                forwarding_address=address)


def test_page_runs_from_the_move_out_to_the_sent_letter(client, tmp_path):
    page = " ".join(client.get("/leases").text.split())
    assert "Record a move-out for Ann Lee, Bo Lee" in page and 'value="2400.00"' in page

    page = record(client, address="")
    assert "Move-out saved." in page and "No forwarding address is recorded yet" in page
    assert f"moved out {MOVED}" in page and "Record a move-out for" not in page

    page = record(client)
    assert "Forwarding address: 9 Elm St, Plano TX 75001." in page and "25 days left" in page

    page = post(client, "/leases/1/move-out/deductions", description="Repair bedroom door", amount="300")
    page = post(client, "/leases/1/move-out/deductions", description="Carpet", amount="0")
    assert "Not saved: the amount must be positive" in page
    post(client, "/leases/1/move-out/deductions", description="Haul away furniture", amount="150.50")
    page = post(client, "/leases/1/move-out/deductions/1/remove")
    assert "Deduction removed." in page and "Repair bedroom door" not in page
    assert "Refund to the tenant" in page and "$2,249.50" in page

    page = post(client, "/leases/1/move-out/sent", sent_on=str(TODAY))
    assert "Not saved: write the letter first." in page

    page = post(client, "/leases/1/move-out/letter")
    letter = f"{MOVED.year}/move-out/deposit-letter_lease-1_{MOVED}.pdf"
    assert f'href="/files/{letter}"' in page and (tmp_path / letter).is_file()
    assert client.get(f"/files/{letter}").status_code == 200

    # A change after the letter was written makes it stale.
    page = post(client, "/leases/1/move-out/deductions", description="Replace broken blind", amount="40")
    assert f'href="/files/{letter}"' not in page and "Write the letter</button>" in page
    post(client, "/leases/1/move-out/letter")

    page = post(client, "/leases/1/move-out/sent", sent_on=str(TODAY))
    assert "The letter and the refund were sent. $2,209.50 refunded, $190.50 kept." in page
    page = post(client, "/leases/1/move-out/deductions", description="One more thing", amount="5")
    assert "Not saved: the letter is recorded as sent" in page
    assert client.post("/leases/99/move-out/letter", headers=ORIGIN).status_code == 404


def test_move_out_stops_the_renewal_and_rent_after_it(client):
    with psycopg.connect(DB) as conn:
        assert [r.property_id for r in renewal.load(conn, TODAY)] == [1]
        assert db.leases_active_in_year(conn, END.year)[-1].end_date == END
    record(client)
    with psycopg.connect(DB) as conn:
        assert renewal.load(conn, TODAY) == [] and renewal.research_to_start(conn, TODAY) is None
        assert [row.end_date for row in db.leases_active_in_year(conn, MOVED.year)] == [MOVED]
        assert db.leases_active_in_year(conn, MOVED.year + 1) == []
        texts = [item.text for item in attention.items(conn, TODAY)]
    assert not any("extension" in t for t in texts)

    page = post(client, "/leases/1/move-out/cancel")
    assert "Move-out cancelled." in page and "Record a move-out for" in page
    with psycopg.connect(DB) as conn:
        assert [r.property_id for r in renewal.load(conn, TODAY)] == [1]
        assert db.leases_active_in_year(conn, END.year)[-1].end_date == END
        assert move_out.load(conn, TODAY) == []


def test_reminder_runs_until_the_letter_is_sent(client):
    def found(today=TODAY):
        with psycopg.connect(DB) as conn:
            return attention.move_out_items(conn, today)

    assert found() == []
    record(client, address="")
    [item] = found()
    assert item.level == "soon" and "No forwarding address is recorded" in item.text
    assert (item.link, item.link_text) == ("/leases#move-out", "Open Leases")

    record(client)
    deadline = MOVED + timedelta(days=30)
    [item] = found()
    assert item.level == "soon" and "(25 days left). Write the letter next." in item.text
    assert f"{deadline:%B} {deadline.day}, {deadline.year}" in item.text
    assert found(deadline - timedelta(days=7))[0].level == "now"
    [late] = found(deadline + timedelta(days=2))
    assert late.level == "now" and "(2 days late)" in late.text

    post(client, "/leases/1/move-out/letter")
    assert "The letter is written; send it and record the day." in found()[0].text
    post(client, "/leases/1/move-out/sent", sent_on=str(TODAY))
    assert found() == []


def test_chat_card_adds_ticked_deductions(client):
    with psycopg.connect(DB) as conn, pytest.raises(ValueError, match="no move-out is recorded"):
        move_out.propose(conn, {"lease_id": 1, "lines": [{"description": "Door", "amount": 5}]}, TODAY)
    record(client)
    args = {"lease_id": 1, "reason": "From the handyman's invoice.", "lines": [
        {"description": "Repair bedroom door", "amount": 300, "basis": "ABC Handyman invoice"},
        {"description": "Deep clean oven", "amount": 85.5, "basis": "owner's figure"}]}
    with psycopg.connect(DB) as conn:
        card = move_out.propose(conn, args, TODAY)
        assert (card["label"], card["deposit"], card["tenants"]) == ("12 Oak St", "2400.00", ["Ann Lee", "Bo Lee"])
        assert [line["amount"] for line in card["lines"]] == ["300.00", "85.50"]
        with pytest.raises(ValueError, match="line 2: the amount must be positive"):
            move_out.propose(conn, {**args, "lines": [args["lines"][0], {"description": "Paint", "amount": -1}]}, TODAY)
        with pytest.raises(ValueError, match="tick at least one line"):
            move_out.apply(conn, card, set(), TODAY)
        assert move_out.apply(conn, card, {"2"}, TODAY) == (
            "Added 1 deduction ($85.50) for 12 Oak St. Write the letter on the Leases page.")
        conn.commit()
        found = move_out.get(conn, 1, TODAY)
    assert [(d.description, d.amount) for d in found.deductions] == [("Deep clean oven", Decimal("85.50"))]


def test_tax_report_counts_the_kept_deposit_in_the_year_it_was_kept(client):
    record(client)
    post(client, "/leases/1/move-out/deductions", description="Repair bedroom door", amount="300")
    with psycopg.connect(DB) as conn:
        assert tax_report.deposits_kept(conn, MOVED.year) == [(MOVED, Decimal("300.00"), "Ann Lee, Bo Lee")]
        assert tax_report.deposits_kept(conn, MOVED.year - 1) == []
