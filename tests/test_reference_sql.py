"""The reference-documents SQL builder: what it includes, skips and redacts."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "build_reference_sql", Path(__file__).resolve().parents[1] / "scripts" / "build_reference_sql.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def test_collect_skips_screening_and_redacts(tmp_path):
    (tmp_path / "tenant-screening" / "income").mkdir(parents=True)
    (tmp_path / "tenant-screening" / "income" / "notes.md").write_text("# Pay\nsecret")
    (tmp_path / "leases").mkdir()
    (tmp_path / "leases" / "scan.pdf").write_bytes(b"%PDF-1.4 not parsed: the .md replaces it")
    (tmp_path / "leases" / "scan.md").write_text("# Signed extension\nRent $2,500.00")
    (tmp_path / "llc").mkdir()
    (tmp_path / "llc" / "ss4.md").write_text("Responsible party SSN 123-45-6789, EIN 39-1234567, "
                                            "file 806258073")
    (tmp_path / "notes.md").write_text("Property notes")
    docs = {d["path"]: d for d in build.collect(tmp_path)}
    assert set(docs) == {"leases/scan.md", "llc/ss4.md", "notes.md"}
    assert docs["leases/scan.md"]["title"] == "Signed extension"
    assert [docs[p]["category"] for p in sorted(docs)] == ["lease", "llc", "notes"]
    text = docs["llc/ss4.md"]["content"]
    assert "123-45-6789" not in text and "[REDACTED]" in text
    assert "39-1234567" in text and "806258073" in text


def test_sql_uses_dollar_quotes_and_upserts():
    sql = build.to_sql([{"path": "notes.md", "category": "notes", "title": "It's",
                         "pages": None, "content": "O'Brien"}])
    assert "$doc$O'Brien$doc$" in sql and "on conflict (path) do update" in sql
