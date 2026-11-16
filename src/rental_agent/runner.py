"""One entry point for every job, shared by the CLI and the web app.

`parse_params` turns form/CLI strings into typed options and rejects bad input before a job
starts; `run_job` runs it and returns a short summary plus the files it wrote.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from rental_agent.config import Settings
from rental_agent.jobs import email_sync, lease, rent_research, tax_report


@dataclass(frozen=True)
class Param:
    name: str
    kind: str            # "int" | "date" | "decimal" | "text" | "bool"
    label: str
    required: bool = False
    help: str = ""


JOBS: dict[str, tuple[str, list[Param]]] = {
    "sync-email": ("Import rent and expense emails from Gmail", [
        Param("since", "date", "Search since", help="Default: 2 days before the last successful run"),
        Param("dry_run", "bool", "Dry run", help="Show what would be imported; save nothing"),
    ]),
    "research-rent": ("Recommend next year's rent from comparable listings", [
        Param("property_id", "int", "Property ID", required=True),
        Param("year", "int", "Year", help="Default: year the next lease term starts"),
        Param("dry_run", "bool", "Dry run", help="Write the report; don't save the recommendation"),
    ]),
    "generate-lease": ("Create next year's lease extension (.docx + .pdf)", [
        Param("property_id", "int", "Property ID", required=True),
        Param("rent", "decimal", "Monthly rent", help="Default: this year's rent research"),
        Param("start", "date", "Start date", help="Default: day after the current term ends"),
        Param("end", "date", "End date", help="Default: one year after the start"),
        Param("other_terms", "text", "Other terms", help='e.g. "Tenant pays water."'),
        Param("dry_run", "bool", "Dry run", help="Write the documents; don't record the new term"),
    ]),
    "tax-report": ("Year-end Schedule E summary (xlsx + csv)", [
        Param("year", "int", "Tax year", help="Default: last year"),
    ]),
}


@dataclass
class Outcome:
    ok: bool
    summary: list[str]
    outputs: list[Path] = field(default_factory=list)


def parse_params(job: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    if job not in JOBS:
        raise ValueError(f"Unknown job: {job}")
    params: dict[str, Any] = {}
    for p in JOBS[job][1]:
        value = raw.get(p.name)
        if p.kind == "bool":
            params[p.name] = value in (True, "on", "true", "1", "yes")
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            if p.required:
                raise ValueError(f"{p.label} is required")
            params[p.name] = None
            continue
        if not isinstance(value, str):
            params[p.name] = value
            continue
        text = value.strip()
        try:
            params[p.name] = {
                "int": int, "date": date.fromisoformat, "decimal": Decimal, "text": str,
            }[p.kind](text)
        except (ValueError, InvalidOperation):
            raise ValueError(f"{p.label}: {text!r} is not a valid {p.kind}") from None
        if p.kind == "decimal" and params[p.name] <= 0:
            raise ValueError(f"{p.label} must be positive")
    return params


async def run_job(job: str, params: Mapping[str, Any], settings: Settings) -> Outcome:
    if job == "sync-email":
        r = await email_sync.run(settings, since=params.get("since"), dry_run=params["dry_run"])
        summary = [(f"Searched since {r.since}: {r.found} messages, {r.already_processed} already "
                   f"processed."),
                   (f"Rent {r.rent}, expenses {r.expenses}, ignored {r.ignored}, "
                   f"needs review {len(r.review)}, errors {len(r.errors)}.")]
        summary += [f"Error on {mid}: {err}" for mid, err in r.errors]
        if params["dry_run"]:
            summary.append("Dry run: nothing saved.")
        return Outcome(not r.errors, summary, [r.summary_path] if r.summary_path else [])

    if job == "research-rent":
        r, report = await rent_research.run(settings, property_id=params["property_id"],
                                            year=params.get("year"), dry_run=params["dry_run"])
        summary = [(f"Recommended ${r.recommended:,} (range ${r.range_low:,}–${r.range_high:,}) "
                   f"from {len(r.comps)} comps; median ${r.median:,}.")]
        summary += [f"Check: {f}" for f in r.flags]
        summary.append("Dry run: recommendation not saved." if params["dry_run"]
                       else "Saved; generate-lease will use this rent by default.")
        return Outcome(True, summary, [report])

    if job == "generate-lease":
        r = lease.run(settings, property_id=params["property_id"], start=params.get("start"),
                      end=params.get("end"), rent=params.get("rent"),
                      other_terms=params.get("other_terms"), dry_run=params["dry_run"])
        summary = list(r.warnings)
        summary.append("Dry run: lease term not recorded." if r.lease_id is None
                       else f"Recorded lease term id {r.lease_id}.")
        return Outcome(True, summary, [r.docx_path, r.pdf_path])

    if job == "tax-report":
        year = params.get("year") or date.today().year - 1
        report, xlsx, csv_path = tax_report.run(settings, year)
        summary = [(f"Tax year {year}: rent ${report.grand(report.rent):,.2f}; "
                   f"{len(report.flags)} item(s) to check on the Review sheet.")]
        return Outcome(True, summary, [xlsx, csv_path])

    raise ValueError(f"Unknown job: {job}")
