"""Runs jobs in the background (one at a time), records them in job_runs, and schedules
the email sync plus a daily database ping."""

import asyncio
import json
import logging
import os
import traceback
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from rental_agent import db, runner
from rental_agent.config import Settings, load_settings
from rental_agent.web.secrets import GMAIL_TOKEN, SecretStore

log = logging.getLogger(__name__)

SYNC_DAYS = (1, 11, 21)          # every ~10 days
MAX_LOG_CHARS = 200_000

RunFn = Callable[[str, dict[str, Any], Settings], Awaitable[runner.Outcome]]


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class JobManager:
    def __init__(self, store: SecretStore, run_fn: RunFn = runner.run_job):
        self.store = store
        self.run_fn = run_fn
        # Jobs log progress at INFO (message ids and outcomes only); keep it for the run log.
        logging.getLogger("rental_agent").setLevel(logging.INFO)
        self._lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()

    def settings(self) -> Settings:
        return load_settings(self.store.load())

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def start(self, job: str, params: dict[str, Any], trigger: str = "manual") -> int:
        """Record the run and start it in the background; returns the job_runs id."""
        if self.busy:
            raise RuntimeError("Another job is running; wait for it to finish.")
        settings = self.settings()
        with db.connect(settings.db_url) as conn:
            run_id = db.start_job_run(conn, job, json.dumps(params, default=str), trigger)
        await self._lock.acquire()
        task = asyncio.create_task(self._run(run_id, job, params, settings))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return run_id

    async def _run(self, run_id: int, job: str, params: dict[str, Any], settings: Settings) -> None:
        capture = _Capture()
        root = logging.getLogger()
        root.addHandler(capture)
        try:
            # Own thread + event loop: jobs make blocking calls (Gmail, LibreOffice, Postgres)
            # that would otherwise freeze the web pages while they run.
            outcome = await asyncio.to_thread(asyncio.run, self.run_fn(job, params, settings))
            ok, summary = outcome.ok, "\n".join(outcome.summary)
            outputs = [_relative(p, settings.artifacts_dir) for p in outcome.outputs]
        except Exception as exc:  # noqa: BLE001 — any failure is recorded on the run
            ok, summary, outputs = False, f"{type(exc).__name__}: {exc}", []
            capture.lines.append(traceback.format_exc())
        finally:
            root.removeHandler(capture)
            self._lock.release()
        text = "\n".join(capture.lines)[-MAX_LOG_CHARS:]
        try:
            with db.connect(settings.db_url) as conn:
                db.finish_job_run(conn, run_id, ok=ok, summary=summary, outputs=outputs, log=text)
        except Exception:
            log.exception("could not record the end of run %s", run_id)


def _relative(path: Path, base: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


async def scheduler(manager: JobManager, interval_seconds: int = 3600) -> None:
    """Hourly: ping the database once a day; run sync-email on the 1st, 11th and 21st."""
    if os.environ.get("RENTAL_SCHEDULER", "on") == "off":
        return
    pinged: date | None = None
    while True:
        try:
            await _tick(manager, pinged)
            pinged = date.today()
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(interval_seconds)


async def _tick(manager: JobManager, pinged: date | None) -> None:
    secrets = manager.store.load()
    if "RENTAL_DB_URL" not in secrets:
        return
    settings = manager.settings()
    today = date.today()
    with db.connect(settings.db_url) as conn:
        if pinged != today:
            db.ping(conn)
        last = db.last_job_run(conn, "sync-email", "schedule")
    already_today = last is not None and _as_date(last["started_at"]) == today
    if today.day in SYNC_DAYS and not already_today and secrets.get(GMAIL_TOKEN) \
            and not manager.busy:
        await manager.start("sync-email", {"since": None, "dry_run": False}, trigger="schedule")


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def next_sync_date(today: date) -> date:
    for day in SYNC_DAYS:
        if today.day <= day:
            return today.replace(day=day)
    month = today.month % 12 + 1
    return date(today.year + (today.month == 12), month, SYNC_DAYS[0])
