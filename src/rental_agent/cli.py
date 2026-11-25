"""Command-line entry point. Job commands come from rental_agent.runner.JOBS."""

import argparse
import asyncio
import json
import logging
import os
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg

from rental_agent import backup, categories, db, doc_import, gmail, memory, runner
from rental_agent.config import (
    artifacts_dir,
    env_or_file,
    gmail_client_secrets_path,
    gmail_token_path,
    load_settings,
)


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
    add.add_argument("--category", required=True,
                     help="a Schedule E category, or a type added on the Expenses page")
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

    res = sub.add_parser("restore-backup",
                         help="load a backup zip into an empty database and put its files back")
    res.add_argument("file", type=Path, help="a zip from artifacts/backups/")
    res.add_argument("--overwrite", action="store_true",
                     help="replace files on disk that differ from the backup")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.command == "gmail-auth":
        gmail.authorize_interactive(gmail_client_secrets_path(), gmail_token_path())
        print(f"Saved Gmail token to {gmail_token_path()}")
        return 0

    if args.command == "restore-backup":
        return _restore_backup(args.file, args.overwrite)

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
            known = categories.load(conn)
            if args.category not in known:
                parser.error(f"--category must be one of {', '.join(known)}")
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


def _restore_backup(file: Path, overwrite: bool) -> int:
    # The owner's login, given for this one run and never stored: the app's own role can't
    # reset ID counters, and a restore may be the first thing done on a new database.
    db_url = os.environ.get("RESTORE_DB_URL")
    if not db_url:
        print("Set RESTORE_DB_URL to the database owner's connection string.", file=sys.stderr)
        return 1
    if not file.is_file():
        print(f"No such file: {file}", file=sys.stderr)
        return 1
    try:
        r = backup.restore(file, db_url, artifacts_dir(), memory.path().parent, overwrite)
    except (RuntimeError, psycopg.Error, zipfile.BadZipFile, KeyError) as exc:
        print(f"Not restored: {exc}", file=sys.stderr)
        return 1
    print(f"Restored {r.tables} tables ({r.rows:,} rows).")
    print(f"Files: {r.written} written, {r.unchanged} already there.")
    if r.different:
        print(f"{len(r.different)} file(s) on disk differ from the backup and were kept "
              "(run again with --overwrite to replace them):")
        for name in r.different:
            print(f"- {name}")
    return 0


def _import_document(db_url: str, file: Path, data_path: Path, apply: bool) -> int:
    if not file.is_file():
        print(f"No such file: {file}", file=sys.stderr)
        return 1
    with db.connect(db_url) as conn:
        try:
            data = doc_import.validate(json.loads(data_path.read_text()), categories.load(conn))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"Invalid facts in {data_path}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2))
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
