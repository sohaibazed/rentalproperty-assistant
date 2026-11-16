"""Statement import against Postgres as the rental_agent role, through the chat card."""

import asyncio
import json
import os
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import chat, db, statement_import
from rental_agent.config import Settings
from rental_agent.web.app import create_app
from rental_agent.web.chat_views import ChatManager
from rental_agent.web.jobs import JobManager
from rental_agent.web.secrets import SecretStore
from tests.test_statement_import import tx

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")
ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def seeded():
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate chat_messages, chats, rent_payments, expenses, lease_tenants, "
                     "leases, tenants, properties restart identity cascade")
        conn.execute("insert into properties (address, city, zip) values ('12 Oak St', 'Plano', "
                     "'75024')")
        conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent) "
                     "values (1, '2026-01-01', '2026-12-31', 2500)")
        # Recorded earlier by the email sync, two days after the statement's posting date.
        conn.execute("insert into rent_payments (lease_id, amount, paid_on, source_message_id) "
                     "values (1, 2500, '2026-10-03', 'gmail-1')")


ROWS = [
    tx(date="2026-09-01"),
    tx(date="2026-10-01"),                                    # matches the Gmail record
    tx(date="2026-08-03", amount=2384, note="$116.00 short"),
    tx(date="2026-03-20", kind="expense", category="repairs", amount=100, property_id=1,
       description="Zelle to Asbel Amin Lopez AC repair", payer_or_vendor="Asbel Amin Lopez"),
    tx(date="2026-09-10", kind="expense", category="other", amount=295, property_id=None,
       description="Zelle to Mike", payer_or_vendor="Mike", include=False, note="unclear payee"),
]


def test_tool_marks_existing_then_card_saves_ticked_rows(seeded, tmp_path, monkeypatch):
    settings = Settings("m", DB, tmp_path, tmp_path, tmp_path, "s")
    proposals = []
    att = "chat-uploads/1/stmt.csv"
    tools = {t.name: t for t in chat.make_tools(settings, proposals, {att})}
    r = asyncio.run(tools["propose_statement_import"].handler(
        {"attachment": att, "statement_name": "Checking 2026", "transactions": ROWS}))
    assert not r.get("is_error") and "look already recorded" in r["content"][-1]["text"]
    rows = proposals[0]["data"]["rows"]
    assert [x["include"] for x in rows] == [True, False, True, True, False]
    assert rows[1]["existing"] == "same amount already recorded on Oct 3"

    for k, v in {"RENTAL_UI_PASSWORD": "pw", "RENTAL_UI_USERNAME": "owner@example.com",
                 "RENTAL_SCHEDULER": "off", "RENTAL_ARTIFACTS_DIR": str(tmp_path),
                 "RENTAL_DATA_DIR": str(tmp_path / "data"),
                 "RENTAL_MEMORY_FILE": str(tmp_path / "memory.md")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RENTAL_SECRET_KEY", raising=False)
    store = SecretStore(tmp_path / "s.enc", Fernet.generate_key().decode())
    store.set("RENTAL_DB_URL", DB)
    with psycopg.connect(DB) as conn:
        chat_id = db.create_chat(conn, "stmt", "claude-sonnet-5-5")
        msg_id = db.add_chat_message(conn, chat_id, "assistant", "Review these",
                                     proposals_json=json.dumps(proposals))
        conn.commit()
    pid = proposals[0]["id"]
    app = create_app(store=store, manager=JobManager(store), chats=ChatManager(store))
    with TestClient(app) as c:
        c.post("/login", data={"email": "owner@example.com", "password": "pw"}, headers=ORIGIN)
        page = c.get(f"/c/{chat_id}").text
        assert "Save ticked rows" in page and "same amount already recorded on Oct 3" in page
        ticked = [x["id"] for x in rows if x["include"]]
        page = c.post(f"/c/{chat_id}/m/{msg_id}/p/{pid}",
                      data={"action": "confirm", "row": ticked}, headers=ORIGIN,
                      follow_redirects=True).text
        assert "Saved 2 rent payment(s) and 1 expense(s)." in page

    with psycopg.connect(ADMIN) as conn:
        rent = conn.execute("select paid_on, amount, lease_id from rent_payments "
                            "order by paid_on").fetchall()
        assert rent == [(date(2026, 8, 3), Decimal("2384.00"), 1),
                        (date(2026, 9, 1), Decimal("2500.00"), 1),
                        (date(2026, 10, 3), Decimal("2500.00"), 1)]
        assert conn.execute("select schedule_e_category::text, vendor from expenses") \
            .fetchall() == [("repairs", "Asbel Amin Lopez")]

    # Importing the same statement again: everything saved before is recognised.
    with psycopg.connect(DB) as conn:
        again = statement_import.mark_existing(
            conn, statement_import.validate(ROWS, {1}, date(2026, 11, 16)))
        assert [x["existing"] for x in again][:1] == ["already imported"]
        assert not any(x["include"] for x in again[:4])
        # Ticking them anyway: rows imported before are skipped by id. Row 2 only matched the
        # Gmail record by amount and date, so a deliberate tick saves it.
        counts = statement_import.save(conn, again, {x["id"] for x in again[:4]})
        assert counts == {"rent": 1, "expense": 0, "skipped": 3}
