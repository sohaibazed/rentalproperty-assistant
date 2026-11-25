"""Chat flow end to end (fake model) against Postgres as the rental_agent role."""

import asyncio
import os
import time
from decimal import Decimal

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import chat, runner
from rental_agent.categories import SCHEDULE_E_CATEGORIES as CATS
from rental_agent.config import Settings
from rental_agent.web.app import create_app
from rental_agent.web.chat_views import ChatManager
from rental_agent.web.jobs import JobManager
from rental_agent.web.secrets import SecretStore
from tests.test_doc_import import FORM_1098

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")
ORIGIN = {"origin": "http://testserver"}
JPEG = b"\xff\xd8\xff\xe0 fake receipt"


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k, v in {"RENTAL_UI_PASSWORD": "pw", "RENTAL_UI_USERNAME": "owner@example.com", "RENTAL_SCHEDULER": "off",
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
        if "house" in text or "1098" in text:
            # The real tools check the facts and build the cards.
            cards: list[dict] = []
            tools = {t.name: t for t in chat.make_tools(settings, cards, {rel for rel, _ in files}, CATS)}
            if "house" in text:
                await tools["propose_property_update"].handler({
                    "property_id": 1, "reason": "From the owner.",
                    "changes": {"property_type": "townhouse", "beds": 3, "baths": 2.5,
                                "sqft": 1900}})
            else:
                await tools["propose_document_import"].handler({**FORM_1098,
                                                                "attachment": files[0][0]})
            return chat.TurnResult("Check the card.", cards, "sess-2")
        return chat.TurnResult(f"Seen {len(history)} chars of history; session {session_id}", [], session_id)

    async def fake_job(job, params, settings):
        return runner.Outcome(True, ["ok"], [])

    app = create_app(store=store, manager=JobManager(store, run_fn=fake_job),
                     chats=ChatManager(store, turn_fn=fake_turn))
    with TestClient(app) as c:
        c.post("/login", data={"email": "owner@example.com", "password": "pw"}, headers=ORIGIN)
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


def _card(client, message, files=None):
    """Send a message. Returns the chat page and the confirm URL of the reply's card."""
    r = client.post("/chat/send", data={"message": message, "model": "claude-sonnet-5-5"},
                    files=files, headers=ORIGIN)
    page = _wait_reply(client, r.url.path)
    with psycopg.connect(ADMIN) as conn:
        msg_id, pid = conn.execute("select id, proposals->0->>'id' from chat_messages "
                                   "where role = 'assistant'").fetchone()
    return page, f"{r.url.path}/m/{msg_id}/p/{pid}"


def test_property_card_saves_the_facts(client, tmp_path):
    page, url = _card(client, "The house is a townhouse: 3 beds, 2.5 baths, 1,900 sq ft")
    assert "Property facts to save: 12 Oak St" in page
    assert "<dt>Sq ft</dt><dd>- → 1,900</dd>" in page and "From the owner." in page

    # The owner corrects the size on the card and adds the facts for depreciation.
    form = {"action": "confirm", "property_type": "townhouse", "beds": "3", "baths": "2.5",
            "sqft": "50", "purchase_date": "2021-07-09", "cost_basis": "275000"}
    assert "Not done: sqft must be a whole number" in client.post(url, data=form,
                                                                  headers=ORIGIN).text
    page = client.post(url, data={**form, "sqft": "1950"}, headers=ORIGIN).text
    assert ("Saved: Type townhouse · Beds 3 · Baths 2.5 · Sq ft 1,950 · Purchase date "
            "2021-07-09 · Cost basis without land $275,000.00.") in page
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select property_type, beds, baths, sqft, purchase_date::text, "
                            "cost_basis from properties").fetchone() == (
            "townhouse", 3, Decimal("2.5"), 1950, "2021-07-09", 275000)
    leases = client.get("/leases").text
    assert "<td>2021-07-09</td>" in leases and "$275,000.00" in leases

    cards: list[dict] = []
    settings = Settings("m", DB, tmp_path, tmp_path, tmp_path)
    tool = {t.name: t for t in chat.make_tools(settings, cards, set(), CATS)}["propose_property_update"]
    same = asyncio.run(tool.handler({"property_id": 1, "changes": {"beds": 3.0}, "reason": ""}))
    assert same["is_error"] and "nothing to change" in same["content"][0]["text"]
    unknown = asyncio.run(tool.handler({"property_id": 1, "changes": {"garage": 2}, "reason": ""}))
    assert "no fact garage" in unknown["content"][0]["text"] and not cards
    asyncio.run(tool.handler({"property_id": 1, "changes": {"sqft": None}, "reason": ""}))
    assert cards[0]["data"]["changes"] == [{"field": "Sq ft", "from": "1,950", "to": "-"}]


def test_form_1098_card_records_the_ticked_expense(client):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        # The county's receipt, saved earlier by a statement import.
        conn.execute("insert into expenses (property_id, amount, spent_on, vendor, "
                     "schedule_e_category) values (1, 4812.37, '2025-12-05', 'County', 'taxes')")
    page, url = _card(client, "Import this 1098",
                      [("files", ("1098.jpg", JPEG, "image/jpeg"))])
    assert "Document to import: 2025 Form 1098, 12 Oak St" in page
    assert "<dt>Property</dt><dd>12 Oak St</dd>" in page
    assert "same amount already recorded on Dec 5, 2025 (taxes)" in page
    with psycopg.connect(ADMIN) as conn:
        attachment, lines = conn.execute(
            "select proposals->0->'data'->>'attachment', proposals->0->'data'->'expenses' "
            "from chat_messages where role = 'assistant'").fetchone()
    assert [line["include"] for line in lines] == [True, False]
    assert page.count('aria-label="Record expense') == 2 and page.count(" checked>") == 1

    page = client.post(url, data={"action": "confirm", "row": lines[0]["id"]},
                       headers=ORIGIN).text
    assert ("Saved. Recorded $8,123.45 of mortgage interest on 2025-12-31. Saved the document "
            "text as imports/") in page
    with psycopg.connect(ADMIN) as conn:
        assert conn.execute("select amount, schedule_e_category, receipt_path from expenses "
                            "order by id").fetchall() == [
            (Decimal("4812.37"), "taxes", None),
            (Decimal("8123.45"), "mortgage_interest", attachment)]
        assert conn.execute("select category from reference_documents where path like "
                            "'imports/%-1098.jpg'").fetchall() == [("notes",)]


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


def test_prompts_page_saves_and_refuses_bad_edits(client, tmp_path, monkeypatch):
    f = tmp_path / "agent-prompts.md"
    f.write_text("## a\n\n````text\nHello {name}.\n````\n")
    monkeypatch.setenv("RENTAL_PROMPTS_FILE", str(f))
    assert "Hello {name}." in client.get("/prompts").text
    page = client.post("/prompts", data={"text": "## a\n\n````text\nHi {name}!\n````\n"},
                       headers=ORIGIN).text
    assert "Prompts saved" in page and "Hi {name}!" in f.read_text()
    page = client.post("/prompts", data={"text": "## a\n\n````text\nHi there\n````\n"},
                       headers=ORIGIN).text
    assert "Not saved:" in page and "Hi there" in page      # the edit stays on screen
    assert "Hi {name}!" in f.read_text()
