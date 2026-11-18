"""Backup and restore: the database, artifacts/ and memory/ in one zip file.

Every backup is a full copy, so any single file restores by itself. The zip holds:
- db/<table>.csv   every table in the public schema, all read in one snapshot;
- artifacts/...    reports, leases, receipts and chat uploads (not the backups folder itself);
- memory/...       memory.md, the owner's rules;
- manifest.json    row counts and SHA-256 checksums, checked after writing and on restore.

Backups are written to artifacts/backups/. The zip is not encrypted: it holds the same private
data as the rest of artifacts/, so keep it on disks you trust.
"""

import csv
import hashlib
import json
import logging
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)

BACKUP_DIR = "backups"            # under artifacts/
PREFIX, SUFFIX = "rental-backup-", ".zip"
KEEP_DAILY = 30                   # the newest backups
KEEP_MONTHLY = 12                 # plus the first backup of each of the last 12 months
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Backup:
    path: Path
    tables: int
    rows: int
    files: int
    removed: list[Path]           # older backups deleted by the retention rule


@dataclass
class Restore:
    tables: int = 0
    rows: int = 0
    written: int = 0
    unchanged: int = 0
    different: list[str] = field(default_factory=list)   # on disk with other content; kept


# --- Create -------------------------------------------------------------------------------

def create(db_url: str, artifacts_dir: Path, memory_dir: Path, today: date) -> Backup:
    """Write today's backup, check it, then delete backups the retention rule no longer keeps."""
    backup_dir = artifacts_dir / BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = backup_dir / f"{PREFIX}{today.isoformat()}{SUFFIX}"
    # Written under another name and renamed at the end: a run that stops halfway never
    # leaves a file that looks like a backup.
    partial = final.with_suffix(".partial")
    manifest: dict = {"created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "tables": {}, "files": {}}
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            _export_tables(db_url, zf, manifest)
            _add_folder(zf, "artifacts", artifacts_dir, manifest, skip=backup_dir)
            _add_folder(zf, "memory", memory_dir, manifest)
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        verify(partial)
        partial.replace(final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    rows = sum(t["rows"] for t in manifest["tables"].values())
    log.info("backup %s: %s tables, %s rows, %s files", final.name, len(manifest["tables"]),
             rows, len(manifest["files"]))
    return Backup(final, len(manifest["tables"]), rows, len(manifest["files"]),
                  prune(backup_dir, today))


def _export_tables(db_url: str, zf: zipfile.ZipFile, manifest: dict) -> None:
    with psycopg.connect(db_url) as conn:
        # One read-only snapshot: every table is read as it was at the same moment.
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        manifest["postgres"] = conn.execute("show server_version").fetchone()[0]
        # From the database, not a fixed list: a table added later is never left out.
        tables = [r[0] for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'public' order by tablename")]
        for table in tables:
            name = sql.Identifier(table)
            rows = conn.execute(sql.SQL("select count(*) from {}").format(name)).fetchone()[0]
            query = sql.SQL("copy (select * from {} order by 1) to stdout (format csv, header)")
            with conn.cursor() as cur, cur.copy(query.format(name)) as copy:
                digest = _write(zf, f"db/{table}.csv", copy)
            manifest["tables"][table] = {"rows": rows, "sha256": digest}


def _add_folder(zf: zipfile.ZipFile, prefix: str, folder: Path, manifest: dict,
                skip: Path | None = None) -> None:
    if not folder.is_dir():
        return
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if skip is not None and path.is_relative_to(skip):
            continue
        name = f"{prefix}/{path.relative_to(folder).as_posix()}"
        manifest["files"][name] = _write(zf, name, _chunks(path))


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as src:
        while chunk := src.read(CHUNK):
            yield chunk


def _write(zf: zipfile.ZipFile, name: str, chunks: Iterable[bytes]) -> str:
    """Add one member to the zip and return the SHA-256 of its content."""
    digest = hashlib.sha256()
    with zf.open(name, "w") as out:
        for chunk in chunks:
            out.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(zf: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with zf.open(name) as src:
        while chunk := src.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path) -> dict:
    """Read the whole zip back and compare each member with the manifest; returns the manifest."""
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        expected = {f"db/{t}.csv": info["sha256"] for t, info in manifest["tables"].items()}
        expected.update(manifest["files"])
        for name, digest in expected.items():
            if _sha256(zf, name) != digest:
                raise RuntimeError(f"{path.name}: {name} does not match the manifest")
    return manifest


# --- Retention ----------------------------------------------------------------------------

def keep(dates: Iterable[date], today: date) -> set[date]:
    """The backup dates to keep: the newest KEEP_DAILY, and the first of each recent month."""
    ordered = sorted(dates)
    kept = set(ordered[-KEEP_DAILY:])
    index = today.year * 12 + today.month - KEEP_MONTHLY          # first month still kept
    earliest = date(index // 12, index % 12 + 1, 1)
    first_of_month: dict[tuple[int, int], date] = {}
    for day in ordered:
        if day >= earliest:
            first_of_month.setdefault((day.year, day.month), day)
    return kept | set(first_of_month.values())


def prune(backup_dir: Path, today: date) -> list[Path]:
    found: dict[date, Path] = {}
    for path in backup_dir.glob(f"{PREFIX}*{SUFFIX}"):
        try:
            found[date.fromisoformat(path.name[len(PREFIX):-len(SUFFIX)])] = path
        except ValueError:
            continue                                   # not one of ours; leave it alone
    kept = keep(found, today)
    removed = [path for day, path in sorted(found.items()) if day not in kept]
    for path in removed:
        path.unlink()
        log.info("removed old backup %s", path.name)
    return removed


# --- Restore ------------------------------------------------------------------------------

def restore(zip_path: Path, db_url: str, artifacts_dir: Path, memory_dir: Path,
            overwrite: bool = False) -> Restore:
    """Load a backup into an empty database (supabase/schema.sql applied), then put the files
    back. `db_url` must be the database owner's login: the app's role can't reset ID counters.

    Nothing is loaded unless every table is empty, and the tables load in one transaction, so
    a failed restore leaves the database as it was. Files already on disk are never replaced
    unless `overwrite` is set.
    """
    manifest = verify(zip_path)
    result = Restore()
    with zipfile.ZipFile(zip_path) as zf:
        with psycopg.connect(db_url) as conn:
            _restore_tables(conn, zf, manifest["tables"], result)
        for name, digest in manifest["files"].items():
            _restore_file(zf, name, digest, artifacts_dir, memory_dir, overwrite, result)
    return result


def _restore_tables(conn: psycopg.Connection, zf: zipfile.ZipFile, tables: dict,
                    result: Restore) -> None:
    existing = {r[0] for r in conn.execute(
        "select tablename from pg_tables where schemaname = 'public'")}
    if missing := sorted(set(tables) - existing):
        raise RuntimeError(f"The database has no table {', '.join(missing)}. "
                           "Run supabase/schema.sql first.")
    for table in tables:
        if conn.execute(sql.SQL("select exists (select 1 from {})")
                        .format(sql.Identifier(table))).fetchone()[0]:
            raise RuntimeError(f"Table {table} already has rows. Restore only into an empty "
                               "database; nothing was changed.")
    for table in _load_order(conn, set(tables)):
        name, member = sql.Identifier(table), f"db/{table}.csv"
        with zf.open(member) as src:
            header = src.readline().decode()
        # Columns by name from the backup, so an older backup loads after a column was added.
        columns = sql.SQL(", ").join(sql.Identifier(c) for c in next(csv.reader([header])))
        query = sql.SQL("copy {} ({}) from stdin (format csv, header)").format(name, columns)
        with zf.open(member) as src, conn.cursor() as cur, cur.copy(query) as copy:
            while chunk := src.read(CHUNK):
                copy.write(chunk)
        rows = conn.execute(sql.SQL("select count(*) from {}").format(name)).fetchone()[0]
        if rows != tables[table]["rows"]:
            raise RuntimeError(f"Table {table}: loaded {rows} rows, the backup lists "
                               f"{tables[table]['rows']}. Nothing was changed.")
        result.tables += 1
        result.rows += rows
    _reset_identities(conn, set(tables))


def _load_order(conn: psycopg.Connection, tables: set[str]) -> list[str]:
    """Tables sorted so each one loads after the tables its foreign keys point to."""
    needs: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent in conn.execute(
            """select child.relname, parent.relname
               from pg_constraint c
               join pg_class child on child.oid = c.conrelid
               join pg_class parent on parent.oid = c.confrelid
               where c.contype = 'f' and c.connamespace = 'public'::regnamespace"""):
        if child in tables and parent in tables and child != parent:
            needs[child].add(parent)
    order: list[str] = []
    while needs:
        ready = sorted(t for t, parents in needs.items() if parents <= set(order))
        if not ready:
            raise RuntimeError(f"Foreign keys form a loop: {', '.join(sorted(needs))}")
        order += ready
        for table in ready:
            del needs[table]
    return order


def _reset_identities(conn: psycopg.Connection, tables: set[str]) -> None:
    """Move each ID counter past the restored rows, so the next insert gets a new ID."""
    identities = conn.execute(
        """select table_name, column_name from information_schema.columns
           where table_schema = 'public' and is_identity = 'YES'""").fetchall()
    for table, column in identities:
        if table not in tables:
            continue
        highest = conn.execute(sql.SQL("select max({}) from {}").format(
            sql.Identifier(column), sql.Identifier(table))).fetchone()[0]
        if highest is not None:
            conn.execute("select setval(pg_get_serial_sequence(%s, %s), %s)",
                         (f"public.{sql.Identifier(table).as_string(conn)}", column, highest))


def _restore_file(zf: zipfile.ZipFile, name: str, digest: str, artifacts_dir: Path,
                  memory_dir: Path, overwrite: bool, result: Restore) -> None:
    prefix, *parts = PurePosixPath(name).parts
    base = {"artifacts": artifacts_dir, "memory": memory_dir}.get(prefix)
    if base is None or not parts or ".." in parts:
        raise RuntimeError(f"Unexpected file in the backup: {name}")
    target = base.joinpath(*parts)
    if target.exists():
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if current == digest:
            result.unchanged += 1
            return
        if not overwrite:
            result.different.append(name)
            return
    target.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(name) as src, target.open("wb") as out:
        while chunk := src.read(CHUNK):
            out.write(chunk)
    result.written += 1
