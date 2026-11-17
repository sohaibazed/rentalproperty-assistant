"""JobManager against Postgres as the rental_agent role, with fake jobs."""

import asyncio
import logging
import os

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent import runner
from rental_agent.web.app import create_app
from rental_agent.web.chat_views import ChatManager
from rental_agent.web.jobs import JobManager
from rental_agent.web.secrets import SecretStore

DB = os.environ.get("TEST_DATABASE_URL")
ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (DB and ADMIN), reason="no test database configured")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("RENTAL_ARTIFACTS_DIR", str(tmp_path))
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("truncate job_runs restart identity")
    store = SecretStore(tmp_path / "s.enc", Fernet.generate_key().decode())
    store.set("RENTAL_DB_URL", DB)

    async def fake(job, params, settings):
        logging.getLogger("rental_agent.fake").info("working on %s", job)
        if params.get("fail"):
            raise ValueError("boom")
        out = settings.artifacts_dir / "out.txt"
        out.write_text("x")
        return runner.Outcome(True, ["did it"], [out])

    return JobManager(store, run_fn=fake)


async def _wait(manager):
    while manager.tasks:
        await asyncio.gather(*manager.tasks)


def _row(run_id):
    with psycopg.connect(ADMIN) as conn:
        return conn.execute("select status, summary, outputs, log, trigger from job_runs where id = %s",
                            (run_id,)).fetchone()


def test_successful_run_is_recorded(manager):
    async def go():
        run_id = await manager.start("tax-report", {"year": 2025})
        await _wait(manager)
        return run_id
    status, summary, outputs, log, trigger = _row(asyncio.run(go()))
    assert (status, summary, outputs, trigger) == ("success", "did it", ["out.txt"], "manual")
    assert "working on tax-report" in log


def test_failed_run_keeps_the_traceback(manager):
    async def go():
        run_id = await manager.start("tax-report", {"fail": True})
        await _wait(manager)
        return run_id
    status, summary, outputs, log, _ = _row(asyncio.run(go()))
    assert status == "failed" and summary == "ValueError: boom" and "Traceback" in log
    assert outputs == []


def test_one_job_at_a_time(manager):
    async def go():
        gate = asyncio.Event()
        original = manager.run_fn

        async def slow(job, params, settings):
            while not gate.is_set():
                await asyncio.sleep(0.01)
            return await original(job, params, settings)

        manager.run_fn = slow
        await manager.start("tax-report", {})
        with pytest.raises(RuntimeError, match="Another job is running"):
            await manager.start("tax-report", {})
        gate.set()
        await _wait(manager)
        assert not manager.busy
    asyncio.run(go())


def test_jobs_page_lists_recent_runs_then_all(manager, tmp_path, monkeypatch):
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        for n in range(1, 13):
            conn.execute("insert into job_runs (job, status, summary) values (%s, 'success', 'ok')",
                         ("tax-report" if n % 2 else "sync-email",))
    for k, v in {"RENTAL_UI_PASSWORD": "pw", "RENTAL_UI_USERNAME": "owner@example.com",
                 "RENTAL_SCHEDULER": "off", "RENTAL_DATA_DIR": str(tmp_path / "data")}.items():
        monkeypatch.setenv(k, v)
    app = create_app(store=manager.store, manager=manager, chats=ChatManager(manager.store))
    origin = {"origin": "http://testserver"}
    with TestClient(app) as c:
        c.post("/login", data={"email": "owner@example.com", "password": "pw"}, headers=origin)
        page = c.get("/jobs").text                       # the ten newest, above the job forms
        assert page.count('<a href="/runs/') == 10 and 'href="/runs/2"' not in page
        assert "Recent runs" in page and "Show all runs" in page
        assert page.index('href="/runs/12"') < page.index('id="run-a-job"')
        page = c.get("/jobs?all=1").text
        assert page.count('<a href="/runs/') == 12 and "All runs" in page
        assert "Show all runs" not in page
        page = c.get("/jobs?job=sync-email").text        # the filter that the Runs page had
        assert page.count('<a href="/runs/') == 6 and 'href="/runs/11"' not in page
