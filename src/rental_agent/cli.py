"""Command-line entry point. Job commands come from rental_agent.runner.JOBS."""

import argparse
import asyncio
import logging
import sys
from datetime import date
from decimal import Decimal

from rental_agent import db, gmail, runner
from rental_agent.config import gmail_client_secrets_path, gmail_token_path, load_settings
from rental_agent.extract import SCHEDULE_E_CATEGORIES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rental_agent")
    sub = parser.add_subparsers(dest="command", required=True)

    for job, (help_text, params) in runner.JOBS.items():
        cmd = sub.add_parser(job, help=help_text)
        for p in params:
            flag = "--" + p.name.replace("_", "-")
            if p.kind == "bool":
                cmd.add_argument(flag, dest=p.name, action="store_true", help=p.help)
            else:
                cmd.add_argument(flag, dest=p.name, required=p.required, help=p.help)

    sub.add_parser("gmail-auth", help="one-time Gmail read-only consent (opens a browser)")

    add = sub.add_parser("add-expense", help="record an expense that didn't arrive by email")
    add.add_argument("--amount", type=Decimal, required=True)
    add.add_argument("--date", type=date.fromisoformat, required=True)
    add.add_argument("--category", choices=SCHEDULE_E_CATEGORIES, required=True)
    add.add_argument("--vendor")
    add.add_argument("--description")
    add.add_argument("--property-id", type=int, help="omit for an LLC-wide expense")
    add.add_argument("--receipt", help="path to a receipt file, relative to artifacts/")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.command == "gmail-auth":
        gmail.authorize_interactive(gmail_client_secrets_path(), gmail_token_path())
        print(f"Saved Gmail token to {gmail_token_path()}")
        return 0

    settings = load_settings()

    if args.command in runner.JOBS:
        try:
            params = runner.parse_params(args.command, vars(args))
        except ValueError as exc:
            parser.error(str(exc))
        outcome = asyncio.run(runner.run_job(args.command, params, settings))
        for line in outcome.summary:
            print(line)
        for path in outcome.outputs:
            print(f"Wrote {path}")
        return 0 if outcome.ok else 1

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

    return 2


if __name__ == "__main__":
    sys.exit(main())
