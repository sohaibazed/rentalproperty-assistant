"""Chat flow end to end (fake model) against Postgres as the rental_agent role."""

import os
import time

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import chat, runner
from rental_agent.web.app import create_app
from rental_agent.web.chat_views import ChatManager
from rental_agent.web.jobs import JobManager
from rental_agent.web.secrets import SecretStore

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")
ORIGIN = {"origin": "http://testserver"}
JPEG = b"\xff\xd8\xff\xe0 fake receipt"


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k, v in {"RENTAL_UI_PASSWORD": "pw", "RENTAL_SCHEDULER": "off",
                 "RENTAL_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
                 "RENTAL_DATA_DIR": str(tmp_path / "data"),
                 "RENTAL_MEMORY_FILE": str(tmp_path / "memory" / "memory.md")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RENTAL_SECRET_KEY", raising=False)
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate chat_messages, chats, job_runs, expenses, rent_payments, lease_tenants, "
                     "leases, tenants, rent_research, properties restart identity cascade")
        conn.execute("insert into properties (address, city, zip) values ('12 Oak St', 'Plano', '75024')")
    store = SecretStore(tmp_path / "data" / "secrets.enc", Fernet.generate_key().decode())
    store.set("RENTAL_DB_URL", DB)

    async def fake_turn(settings, *, model, text, files, session_id, history, on_text, on_status):
        on_status("Checking your records")
        on_text("I read the receipt")
        if "receipt" in text:
            receipt = files[0][0]
            return chat.TurnResult("I read the receipt. Check and save:", [
                {"id": "e1", "kind": "expense", "status": "pending",
                 "data": {"date": "2026-11-12", "amount": "54.20", "vendor": "Home Depot",
                          "category": "repairs", "property_id": 1, "description": "faucet",
                          "receipt": receipt}},
                {"id": "m1", "kind": "memory", "status": "pending", "data": {"rule": "Costco is personal"}},
                {"id": "j1", "kind": "job", "status": "pending",
                 "data": {"job": "tax-report", "options": {"year": 2026}}}], "sess-1")
        return chat.TurnResult(f"Seen {len(history)} chars of history; session {session_id}", [], session_id)

    async def fake_job(job, params, settings):
        return runner.Outcome(True, ["ok"], [])

    app = create_app(store=store, manager=JobManager(store, run_fn=fake_job),
                     chats=ChatManager(store, turn_fn=fake_turn))
    with TestClient(app) as c:
        c.post("/login", data={"password": "pw"}, headers=ORIGIN)
        yield c


def _wait_reply(client, url):
    for _ in range(100):
        page = client.get(url).text
        if 'id="live"' not in page:
            return page
        time.sleep(0.05)
    raise AssertionError("no reply")


def test_receipt_to_saved_expense(client, tmp_path):
    r = client.post("/chat/send", data={"message": "Add this receipt", "model": "claude-sonnet-5-5"},
                    files=[("files", ("Home Depot.jpg", JPEG, "image/jpeg"))], headers=ORIGIN)
    url = r.url.path
    assert url.startswith("/c/")
    page = _wait_reply(client, url)
    assert "I read the receipt. Check and save:" in page and "Expense to save" in page
    assert "Sonnet 5.5" in page and "Home-Depot.jpg" in page

    chat_id = url.split("/")[-1]
    with psycopg.connect(ADMIN) as conn:
        msg_id, receipt = conn.execute(
            "select id, proposals->0->'data'->>'receipt' from chat_messages where role = 'assistant'").fetchone()
        assert conn.execute("select sdk_session_id from chats").fetchone()[0] == "sess-1"
    assert (tmp_path / "artifacts" / receipt).read_bytes() == JPEG

    base = f"/c/{chat_id}/m/{msg_id}/p"
    form = {"action": "confirm", "date": "2026-11-12", "amount": "60.00", "vendor": "Home Depot",
            "category": "repairs", "property_id": "1", "description": "faucet + tape"}
    page = client.post(f"{base}/e1", data=form, headers=ORIGIN).text
    assert "Saved: $60.00 repairs." in page
    again = client.post(f"{base}/e1", data=form, headers=ORIGIN).text
    assert "already handled" in again
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select amount, receipt_path, description from expenses").fetchall() == \
            [(60, receipt, "faucet + tape")]

    client.post(f"{base}/m1", data={"action": "confirm", "rule": "Costco receipts are personal"},
                headers=ORIGIN)
    assert "- Costco receipts are personal" in (tmp_path / "memory" / "memory.md").read_text()

    page = client.post(f"{base}/j1", data={"action": "confirm"}, headers=ORIGIN).text
    assert "Started run #1." in page and 'href="/runs/1"' in page

    # A follow-up resumes the same SDK session and carries history for the fallback.
    client.post("/chat/send", data={"message": "thanks", "model": "claude-sonnet-5-5", "chat_id": chat_id},
                headers=ORIGIN)
    page = _wait_reply(client, url)
    assert "session sess-1" in page

    assert "Add this receipt" in client.get("/?q=receipt").text
    assert "No matches." in client.get("/?q=zzzz").text
    assert "event: done" in client.get(f"/c/{chat_id}/events").text


def test_disguised_upload_rejected(client):
    r = client.post("/chat/send", data={"message": "hi", "model": "claude-opus-5-5"},
                    files=[("files", ("evil.pdf", b"MZ not a pdf", "application/pdf"))], headers=ORIGIN)
    assert "look like a real PDF" in r.text
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select count(*) from chats").fetchone()[0] == 0


def test_memory_page_saves(client, tmp_path):
    page = client.post("/memory", data={"text": "# Memory\n- Rule one\n"}, headers=ORIGIN).text
    assert "Memory saved" in page and "Rule one" in page
    assert "1 rule" in client.get("/").text
