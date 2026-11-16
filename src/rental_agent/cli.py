"""Command-line entry point. Job commands come from rental_agent.runner.JOBS."""

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from rental_agent import db, doc_import, gmail, runner
from rental_agent.config import (
    env_or_file,
    gmail_client_secrets_path,
    gmail_token_path,
    load_settings,
)
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

    imp = sub.add_parser("import-document",
                         help="save a lease or LLC document's facts (see the import-document skill)")
    imp.add_argument("file", type=Path, help="the document (PDF, MD or TXT)")
    imp.add_argument("--data", type=Path, required=True, help="JSON facts extracted from it")
    imp.add_argument("--apply", action="store_true",
                     help="save; without it, only preview what would change")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.command == "gmail-auth":
        gmail.authorize_interactive(gmail_client_secrets_path(), gmail_token_path())
        print(f"Saved Gmail token to {gmail_token_path()}")
        return 0

    # Inside the container, the database URL and keys live in the web app's encrypted store.
    secrets = None
    if env_or_file("RENTAL_SECRET_KEY"):
        from rental_agent.web.secrets import SecretStore
        secrets = SecretStore.from_env().load()
    settings = load_settings(secrets)

    if args.command == "import-document":
        return _import_document(settings.db_url, args.file, args.data, args.apply)

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


def _import_document(db_url: str, file: Path, data_path: Path, apply: bool) -> int:
    if not file.is_file():
        print(f"No such file: {file}", file=sys.stderr)
        return 1
    try:
        data = doc_import.validate(json.loads(data_path.read_text()))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid facts in {data_path}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2))
    with db.connect(db_url) as conn:
        try:
            lines = doc_import.apply(conn, data, file)
        except ValueError as exc:
            conn.rollback()
            print(f"Not saved: {exc}", file=sys.stderr)
            return 1
        if apply:
            conn.commit()
        else:
            conn.rollback()  # preview: run the same writes, then undo them
    print("\nSaved:" if apply else "\nPreview (nothing saved; run again with --apply):")
    for line in lines:
        print(f"- {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
