"""Command-line entry point: one subcommand per job."""

import argparse
import asyncio
import logging
import sys
from datetime import date
from decimal import Decimal

from rental_agent import db, gmail
from rental_agent.config import load_settings
from rental_agent.extract import SCHEDULE_E_CATEGORIES
from rental_agent.jobs import email_sync, lease, tax_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rental_agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("gmail-auth", help="one-time Gmail read-only consent (opens a browser)")

    sync = sub.add_parser("sync-email", help="import rent and expense emails")
    sync.add_argument("--since", type=date.fromisoformat, help="YYYY-MM-DD; default: last run")
    sync.add_argument("--dry-run", action="store_true", help="print results, write nothing")

    add = sub.add_parser("add-expense", help="record an expense that didn't arrive by email")
    add.add_argument("--amount", type=Decimal, required=True)
    add.add_argument("--date", type=date.fromisoformat, required=True)
    add.add_argument("--category", choices=SCHEDULE_E_CATEGORIES, required=True)
    add.add_argument("--vendor")
    add.add_argument("--description")
    add.add_argument("--property-id", type=int, help="omit for an LLC-wide expense")
    add.add_argument("--receipt", help="path to a receipt file, relative to artifacts/")

    tax = sub.add_parser("tax-report", help="year-end Schedule E summary (xlsx + csv)")
    tax.add_argument("--year", type=int, default=date.today().year - 1,
                     help="tax year; default: last year")

    gen = sub.add_parser("generate-lease", help="yearly lease extension (.docx + .pdf)")
    gen.add_argument("--property-id", type=int, required=True)
    gen.add_argument("--rent", type=Decimal, help="monthly rent; default: this year's rent research")
    gen.add_argument("--start", type=date.fromisoformat, help="default: day after current term")
    gen.add_argument("--end", type=date.fromisoformat, help="default: one year after start")
    gen.add_argument("--other-terms", help='e.g. "Tenant pays water."; default: None.')
    gen.add_argument("--dry-run", action="store_true",
                     help="write the documents but don't record the new lease term")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()

    if args.command == "gmail-auth":
        gmail.authorize_interactive(settings.gmail_client_secrets, settings.gmail_token_path)
        print(f"Saved Gmail token to {settings.gmail_token_path}")
        return 0

    if args.command == "sync-email":
        report = asyncio.run(email_sync.run(settings, since=args.since, dry_run=args.dry_run))
        print(
            f"since {report.since}: found {report.found}, rent {report.rent}, "
            f"expenses {report.expenses}, ignored {report.ignored}, "
            f"review {len(report.review)}, errors {len(report.errors)}"
        )
        return 1 if report.errors else 0

    if args.command == "add-expense":
        if args.amount <= 0:
            parser.error("--amount must be positive")
        with db.connect(settings.db_url) as conn:
            db.insert_expense(
                conn,
                property_id=args.property_id,
                amount=args.amount,
                spent_on=args.date,
                vendor=args.vendor,
                description=args.description,
                category=args.category,
                receipt_path=args.receipt,
                message_id=None,
                needs_review=False,
            )
        print("Expense recorded.")
        return 0

    if args.command == "generate-lease":
        result = lease.run(settings, property_id=args.property_id, start=args.start, end=args.end,
                           rent=args.rent, other_terms=args.other_terms, dry_run=args.dry_run)
        print(f"Wrote {result.docx_path}")
        print(f"Wrote {result.pdf_path}")
        for warning in result.warnings:
            print(f"- {warning}")
        if result.lease_id is None:
            print("Dry run: lease term not recorded.")
        else:
            print(f"Recorded lease term id {result.lease_id}.")
        return 0

    if args.command == "tax-report":
        report, xlsx, csv_path = tax_report.run(settings, args.year)
        print(f"Wrote {xlsx}")
        print(f"Wrote {csv_path}")
        print(f"{len(report.flags)} item(s) to check on the Review sheet")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
