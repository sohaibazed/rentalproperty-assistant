"""sync-email: pull rent and expense emails from Gmail into Postgres.

Runs every 10 days. Each run searches from the last successful run's start minus an overlap,
so a missed run never leaves a gap; message-id dedupe makes the overlap harmless.
A run with any per-message error is marked 'failed', so the next run re-covers its window.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from rental_agent import db, gmail
from rental_agent.config import Settings, load_email_sources
from rental_agent.extract import extract
from rental_agent.validate import Candidate, validate

log = logging.getLogger(__name__)

OVERLAP = timedelta(days=2)
FIRST_RUN_LOOKBACK = timedelta(days=30)


@dataclass
class ReviewItem:
    message_id: str
    candidate: Candidate


@dataclass
class SyncReport:
    since: date
    found: int = 0
    already_processed: int = 0
    rent: int = 0
    expenses: int = 0
    ignored: int = 0
    review: list[ReviewItem] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (message_id, error)


def resolve_since(explicit: date | None, last_success, today: date) -> date:
    if explicit:
        return explicit
    if last_success:
        return (last_success - OVERLAP).date()
    return today - FIRST_RUN_LOOKBACK


async def run(settings: Settings, *, since: date | None, dry_run: bool) -> SyncReport:
    today = date.today()
    sources = load_email_sources(settings.email_sources_path)
    service = gmail.get_service(settings.gmail_token_path)

    with db.connect(settings.db_url) as conn:
        since = resolve_since(since, db.last_successful_sync_start(conn), today)
        report = SyncReport(since=since)
        properties = db.load_properties(conn)
        property_ids = {p.id for p in properties}

        message_ids = gmail.search_message_ids(service, gmail.build_query(sources, since))
        report.found = len(message_ids)
        done = db.processed_message_ids(conn, message_ids) if message_ids else set()
        report.already_processed = len(done)
        todo = [m for m in message_ids if m not in done]
        log.info("since=%s found=%d new=%d", since, len(message_ids), len(todo))

        run_id = None if dry_run else db.start_sync_run(conn)

        for message_id in todo:
            try:
                msg = gmail.get_message(service, message_id)
                raw = await extract(msg, properties, settings.model)
                cand = validate(
                    raw,
                    email_date=msg.sent_at.date(),
                    known_property_ids=property_ids,
                    today=today,
                )
                if dry_run:
                    _print_candidate(message_id, cand)
                else:
                    _store(conn, service, settings, msg, cand)
                    conn.commit()
                _count(report, message_id, cand)
                log.info("message=%s outcome=%s review=%s", message_id, cand.kind, cand.needs_review)
            except Exception as exc:  # noqa: BLE001 — one bad email must not stop the run
                conn.rollback()
                report.errors.append((message_id, f"{type(exc).__name__}: {exc}"))
                log.warning("message=%s error=%s", message_id, type(exc).__name__)

        if run_id is not None:
            inserted = report.rent + report.expenses
            status = "failed" if report.errors else "success"
            db.finish_sync_run(conn, run_id, status, inserted)
            _write_summary(settings.artifacts_dir, today, report)

    return report


def _store(conn, service, settings: Settings, msg: gmail.EmailMessage, cand: Candidate) -> None:
    if cand.kind == "rent":
        lease_id = db.active_lease_id(conn, cand.property_id, cand.on) if cand.property_id else None
        db.insert_rent_payment(
            conn,
            lease_id=lease_id,
            amount=cand.amount,
            paid_on=cand.on,
            payer=cand.counterparty,
            message_id=msg.message_id,
            needs_review=cand.needs_review or lease_id is None,
        )
        db.log_ingested(conn, msg.message_id, "rent")
    elif cand.kind == "expense":
        receipt = _save_receipt(service, settings.artifacts_dir, msg, cand)
        db.insert_expense(
            conn,
            property_id=cand.property_id,
            amount=cand.amount,
            spent_on=cand.on,
            vendor=cand.counterparty,
            description=cand.description,
            category=cand.category,
            receipt_path=receipt,
            message_id=msg.message_id,
            needs_review=cand.needs_review,
        )
        db.log_ingested(conn, msg.message_id, "expense")
    else:
        db.log_ingested(conn, msg.message_id, "ignored")


def _save_receipt(
    service: Any, artifacts_dir: Path, msg: gmail.EmailMessage, cand: Candidate
) -> str | None:
    """Save the first receipt-like attachment; return its path relative to artifacts_dir."""
    for att in msg.attachments:
        if att.mime_type not in gmail.RECEIPT_MIME_TYPES:
            continue
        folder = artifacts_dir / str(cand.on.year) / "receipts"
        folder.mkdir(parents=True, exist_ok=True)
        vendor = re.sub(r"[^a-z0-9]+", "-", (cand.counterparty or "unknown").lower()).strip("-")
        suffix = Path(att.filename).suffix.lower() or ".bin"
        path = folder / f"{cand.on:%Y-%m-%d}_{vendor[:40]}_{msg.message_id}{suffix}"
        path.write_bytes(gmail.get_attachment_bytes(service, msg.message_id, att.attachment_id))
        return str(path.relative_to(artifacts_dir))
    return None


def _count(report: SyncReport, message_id: str, cand: Candidate) -> None:
    if cand.kind == "rent":
        report.rent += 1
    elif cand.kind == "expense":
        report.expenses += 1
    else:
        report.ignored += 1
    if cand.needs_review:
        report.review.append(ReviewItem(message_id, cand))


def _print_candidate(message_id: str, cand: Candidate) -> None:
    if cand.kind == "ignore":
        print(f"[ignore ] {message_id}")
        return
    flag = " REVIEW: " + "; ".join(cand.review_reasons) if cand.needs_review else ""
    print(
        f"[{cand.kind:7}] {message_id} {cand.on} ${cand.amount} "
        f"{cand.counterparty or '-'} cat={cand.category or '-'} "
        f"property={cand.property_id or '-'}{flag}"
    )


def _write_summary(artifacts_dir: Path, today: date, report: SyncReport) -> Path:
    folder = artifacts_dir / str(today.year)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"sync-{today:%Y-%m-%d}.md"
    lines = [
        f"# Email sync {today:%Y-%m-%d}",
        "",
        f"- Searched since: {report.since}",
        f"- Messages found: {report.found} ({report.already_processed} already processed)",
        f"- Rent payments: {report.rent}",
        f"- Expenses: {report.expenses}",
        f"- Ignored: {report.ignored}",
        f"- Errors: {len(report.errors)}",
        "",
    ]
    if report.review:
        lines += ["## Needs review", "", "| Message | Kind | Date | Amount | Who | Why |",
                  "|---|---|---|---|---|---|"]
        for item in report.review:
            c = item.candidate
            lines.append(
                f"| {item.message_id} | {c.kind} | {c.on} | ${c.amount} | "
                f"{c.counterparty or '-'} | {'; '.join(c.review_reasons)} |"
            )
        lines.append("")
    if report.errors:
        lines += ["## Errors (will be retried next run)", ""]
        lines += [f"- {mid}: {err}" for mid, err in report.errors]
    path.write_text("\n".join(lines) + "\n")
    return path
