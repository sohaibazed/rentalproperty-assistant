"""Backup retention and file handling; no database needed."""

import hashlib
import zipfile
from datetime import date, timedelta

import pytest

from rental_agent import backup


def days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=n) for n in range(count)]


def test_keep_newest_daily_and_first_of_each_recent_month():
    today = date(2026, 11, 18)
    dates = days(date(2025, 1, 1), (today - date(2025, 1, 1)).days + 1)   # one per day
    kept = backup.keep(dates, today)
    daily = set(dates[-backup.KEEP_DAILY:])
    assert daily <= kept
    monthly = sorted(kept - daily)
    # December 2025 to October 2026. November 1 is inside the 30-day window already.
    assert monthly == [date(2025, 12, 1)] + [date(2026, m, 1) for m in range(1, 11)]


def test_keep_uses_the_first_backup_that_exists_in_a_month():
    kept = backup.keep([date(2026, 3, 9), date(2026, 3, 20)] + days(date(2026, 10, 1), 40),
                       date(2026, 11, 9))
    assert date(2026, 3, 9) in kept and date(2026, 3, 20) not in kept


def test_prune_deletes_only_backups_outside_the_rule(tmp_path):
    today = date(2026, 11, 18)
    for day in days(date(2026, 9, 1), 79):
        (tmp_path / f"rental-backup-{day}.zip").write_text("x")
    (tmp_path / "notes.zip").write_text("mine")
    (tmp_path / "rental-backup-old.zip").write_text("mine")
    removed = backup.prune(tmp_path, today)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert "notes.zip" in left and "rental-backup-old.zip" in left
    assert "rental-backup-2026-09-01.zip" in left          # first of a recent month
    assert "rental-backup-2026-09-02.zip" not in left
    assert len(removed) == 79 - backup.KEEP_DAILY - 2      # September 1 and October 1 stay


def _zip_with(tmp_path, files: dict[str, bytes]):
    path = tmp_path / "b.zip"
    manifest = {"tables": {}, "files": {}}
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            manifest["files"][name] = backup._write(zf, name, [data])
    return path, manifest


def test_restore_file_writes_keeps_and_overwrites(tmp_path):
    path, manifest = _zip_with(tmp_path, {"artifacts/2027/lease.pdf": b"signed",
                                          "memory/memory.md": b"my rules"})
    artifacts, memory = tmp_path / "artifacts", tmp_path / "memory"
    memory.mkdir()
    (memory / "memory.md").write_text("starter")

    def run(overwrite):
        result = backup.Restore()
        with zipfile.ZipFile(path) as zf:
            for name, digest in manifest["files"].items():
                backup._restore_file(zf, name, digest, artifacts, memory, overwrite, result)
        return result

    first = run(overwrite=False)
    assert (artifacts / "2027/lease.pdf").read_bytes() == b"signed"
    assert (first.written, first.different) == (1, ["memory/memory.md"])
    assert (memory / "memory.md").read_text() == "starter"

    second = run(overwrite=True)
    assert (second.written, second.unchanged) == (1, 1)
    assert (memory / "memory.md").read_text() == "my rules"


@pytest.mark.parametrize("name", ["artifacts/../../etc/x", "data/secret", "artifacts"])
def test_restore_file_rejects_paths_outside_the_two_folders(tmp_path, name):
    path, _ = _zip_with(tmp_path, {name: b"x"})
    with zipfile.ZipFile(path) as zf, pytest.raises(RuntimeError, match="Unexpected file"):
        backup._restore_file(zf, name, hashlib.sha256(b"x").hexdigest(), tmp_path / "a",
                             tmp_path / "m", True, backup.Restore())


def test_verify_catches_a_changed_member(tmp_path):
    path, manifest = _zip_with(tmp_path, {"artifacts/a.txt": b"one"})
    manifest["files"]["artifacts/a.txt"] = hashlib.sha256(b"two").hexdigest()
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("manifest.json", __import__("json").dumps(manifest))
    with pytest.raises(RuntimeError, match="does not match"):
        backup.verify(path)
