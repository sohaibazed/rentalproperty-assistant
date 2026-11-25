"""Review pages and the chat's correction card, against Postgres as the rental_agent role."""

import asyncio
import json
import os
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import categories, chat, db, statement_import
from rental_agent.categories import SCHEDULE_E_CATEGORIES as CATS
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

# A personal purchase a statement import recorded as a rental expense (expense id 2).
COSTCO = tx(date="2026-06-01", kind="expense", category="supplies", amount=89.99,
            property_id=None, description="COSTCO WHSE", payer_or_vendor="Costco")


@pytest.fixture
def client(tmp_path, monkeypatch):
    costco_id = statement_import.validate([COSTCO], {1}, CATS, date(2026, 11, 16))[0]["id"]
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate chat_messages, chats, rent_payments, expenses, lease_tenants, "
                     "leases, tenants, properties restart identity cascade")
        conn.execute("insert into properties (address, city, zip) values ('12 Oak St', 'Plano', "
                     "'75024')")
        conn.execute("insert into leases (property_id, start_date, end_date, monthly_rent) "
                     "values (1, '2026-01-01', '2026-12-31', 2500)")
        conn.execute("insert into rent_payments (lease_id, amount, paid_on, payer, "
                     "source_message_id, needs_review) values "
                     "(null, 2500, '2026-03-02', 'J DOE', 'gmail-r1', true), "    # unmatched
                     "(1, 2500, '2026-04-01', 'Jane Doe', 'gmail-r2', false)")
        conn.execute("insert into expenses (property_id, amount, spent_on, vendor, "
                     "schedule_e_category, source_message_id, needs_review) values "
                     "(1, 54.20, '2026-05-10', 'Home Depot', 'repairs', 'gmail-e1', true), "
                     "(null, 89.99, '2026-06-01', 'Costco', 'supplies', %s, true)", (costco_id,))
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


def post(c, path, **data):
    return c.post(path, data=data, headers=ORIGIN, follow_redirects=True).text


def one(sql):
    with psycopg.connect(ADMIN) as conn:
        return conn.execute(sql).fetchone()


def test_review_list_confirm_and_fix(client):
    assert '<a class="num" href="/review">3</a>' in client.get("/dashboard").text
    page = client.get("/review").text
    assert "Not matched to a lease" in page and "Home Depot" in page and "Costco" in page

    # "Looks right" clears the flag without changing the row.
    page = post(client, "/expenses/1", action="checked", back="review")
    assert "Saved: $54.20 on 2026-05-10." in page and "Home Depot" not in page
    assert one("select needs_review, amount from expenses where id = 1") == (False, Decimal("54.20"))

    # Rent is matched to the lease that covers its date; there is none for 2025.
    fields = {"amount": "2550", "payer": "Jane Doe", "property_id": "1", "back": "review"}
    page = post(client, "/rent/1", action="save", date="2025-12-15", **fields)
    assert "Not saved: no lease for that property covers 2025-12-15" in page
    assert one("select lease_id, needs_review from rent_payments where id = 1") == (None, True)

    page = post(client, "/rent/1", action="save", date="2026-03-02", **fields)
    assert "Saved: $2,550.00 on 2026-03-02." in page and "No rent payments to review." in page
    assert one("select lease_id, amount, payer, needs_review from rent_payments where id = 1") \
        == (1, Decimal("2550.00"), "Jane Doe", False)

    # Saved without a property, it stays unmatched and stays on the list.
    page = post(client, "/rent/1", action="save", date="2026-03-02", **{**fields, "property_id": ""})
    assert "Still unmatched" in page and "Not matched to a lease" in page


def test_remove_leaves_totals_and_reimports_then_restore(client):
    page = post(client, "/expenses/2", action="remove")
    assert "Removed." in page and '<span class="pill">removed</span>' in page
    assert '<tr class="total"><td>Total</td><td class="r">$54.20</td></tr>' in page
    with psycopg.connect(DB) as conn:
        assert db.year_totals(conn, 2026)["expenses"] == Decimal("54.20")
        assert db.year_totals(conn, 2026)["needs_review"] == 2
        assert [e.vendor for e in db.expenses_for_year(conn, 2026)] == ["Home Depot"]   # tax report
        assert [e["vendor"] for e in db.expenses_list(conn, 2026)] == ["Home Depot"]    # chat
        # The same statement again: the removed row is recognised, not offered as new.
        again = statement_import.mark_existing(
            conn, statement_import.validate([COSTCO], {1}, CATS, date(2026, 11, 16)))
        assert again[0]["existing"] == "removed earlier; restore it on the Expenses page"
        assert again[0]["include"] is False

    assert "Not saved: that row was already removed" in post(client, "/expenses/2", action="remove")
    page = post(client, "/expenses/2", action="save", date="2026-06-01", amount="1",
                category="supplies")
    assert "Not saved: that row was removed; restore it first" in page and "Restore" in page
    assert one("select amount from expenses where id = 2") == (Decimal("89.99"),)

    assert "Restored." in post(client, "/expenses/2", action="restore")
    with psycopg.connect(DB) as conn:
        assert db.year_totals(conn, 2026)["expenses"] == Decimal("144.19")
        assert db.year_totals(conn, 2026)["needs_review"] == 3

    assert client.get("/expenses/99").status_code == 404


def test_chat_correction_card(client, tmp_path):
    settings = Settings("m", DB, tmp_path, tmp_path, tmp_path, "s")
    proposals = []
    tool = {t.name: t for t in chat.make_tools(settings, proposals, set(), CATS)}["propose_correction"]

    def call(**args):
        return asyncio.run(tool.handler({"changes": {}, "reason": "", **args}))

    def refused(**args):
        r = call(**args)
        return r["content"][0]["text"] if r.get("is_error") else None

    assert "no rent row with id 99" in refused(kind="rent", id=99, action="update")
    assert "rent has no field vendor" in refused(kind="rent", id=1, action="update",
                                                 changes={"vendor": "x"})
    assert "nothing to change" in refused(kind="rent", id=2, action="update")
    assert "no lease for that property covers 2025-12-15" in refused(
        kind="rent", id=1, action="update", changes={"property_id": 1, "date": "2025-12-15"})
    assert proposals == []

    assert not call(kind="rent", id=1, action="update", changes={"property_id": 1},
                    reason="The March rent for Oak St").get("is_error")
    assert not call(kind="expense", id=2, action="remove", reason="Personal").get("is_error")
    assert proposals[0]["data"]["changes"] == [
        {"field": "Property", "from": "unmatched", "to": "12 Oak St"}]

    with psycopg.connect(DB) as conn:
        chat_id = db.create_chat(conn, "fix", "claude-sonnet-5-5")
        msg_id = db.add_chat_message(conn, chat_id, "assistant", "Two fixes",
                                     proposals_json=json.dumps(proposals))
        conn.commit()
    page = client.get(f"/c/{chat_id}").text
    assert "Fix rent payment" in page and "unmatched → 12 Oak St" in page
    assert "Remove expense" in page and "2026-06-01 · $89.99 · supplies · Costco" in page
    assert one("select lease_id from rent_payments where id = 1") == (None,)    # nothing yet

    for p in proposals:
        post(client, f"/c/{chat_id}/m/{msg_id}/p/{p['id']}", action="confirm")
    assert one("select lease_id, needs_review from rent_payments where id = 1") == (1, False)
    assert one("select removed_at is not null from expenses where id = 2") == (True,)


def test_out_of_date_schema_points_to_the_fix(client, monkeypatch):
    def missing_column(*_args, **_kwargs):
        raise psycopg.errors.UndefinedColumn('column "removed_at" does not exist')

    monkeypatch.setattr(db, "year_totals", missing_column)
    page = client.get("/dashboard", follow_redirects=True).text
    assert "Run supabase/schema.sql again" in page


def test_owner_adds_an_expense_type_and_changes_a_row_to_it(client):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("delete from expense_categories where name = 'hoa_dues'")
    page = post(client, "/expense-types", name="HOA dues", year="2026")
    assert "Added the type &#39;hoa dues&#39;" in page
    assert '<option value="hoa_dues" >hoa dues</option>' in page       # in each row's list
    assert "already exists" in post(client, "/expense-types", name="hoa-dues", year="2026")
    assert "Not added" in post(client, "/expense-types", name="!", year="2026")

    page = post(client, "/expenses/1", action="category", category="hoa_dues")
    assert "Category changed to hoa dues." in page
    assert one("select schedule_e_category, needs_review from expenses where id = 1") == \
        ("hoa_dues", True)                                  # still flagged for review
    assert "Not saved: unknown category" in post(client, "/expenses/1", action="category",
                                                 category="groceries")
    assert "Not saved" in post(client, "/rent/1", action="category", category="hoa_dues")
    with db.connect(DB) as conn:
        assert categories.load(conn)[-2:] == ["other", "hoa_dues"]
