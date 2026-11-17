"""Import a new lease or LLC document into the database.

The model that reads the document (the web chat, or Claude Code) fills in IMPORT_SCHEMA by
following the import-document skill. This module checks those facts (`validate`) and, after the
owner confirms, saves them (`apply`): the LLC, the property, tenants and lease term, and the
document's text for later questions. Nothing is ever deleted.
"""

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from pypdf import PdfReader

from rental_agent.config import artifacts_dir

DOC_TYPES = ["lease", "lease_extension", "llc_formation", "operating_agreement", "ein_letter",
             "other"]
LEASE_TYPES = {"lease", "lease_extension"}
LLC_TYPES = {"llc_formation", "operating_agreement", "ein_letter"}
MAX_RENT = Decimal(100000)
MAX_SUMMARY = 20000
# Below this many words per page, a PDF's text layer is treated as missing (a scan).
MIN_WORDS_PER_PAGE = 30

SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EIN = re.compile(r"^\d{2}-\d{7}$")
ZIP = re.compile(r"^\d{5}(-\d{4})?$")

_text = {"type": ["string", "null"]}
_names = {"type": "array", "items": {"type": "string"}}
IMPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": DOC_TYPES},
        "title": {"type": "string", "description": "Short title, e.g. '2027 lease extension, "
                  "8705 Empire Blvd'"},
        "summary": {"type": "string", "description": "Markdown summary of the key facts and "
                    "clauses, with page or paragraph numbers. Never include SSNs."},
        "owner_llc": {**_text, "description": "For a lease: the LLC that owns the property, "
                      "if known (must already be imported). Otherwise null."},
        "llc": {"type": ["object", "null"], "properties": {
            "name": {"type": "string"}, "state": _text, "file_number": _text, "ein": _text,
            "formed_on": {**_text, "description": "YYYY-MM-DD"},
            "management": {"type": ["string", "null"], "enum": ["managers", "members", None]},
            "members": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "percent": {"type": ["number", "null"]}},
                "required": ["name", "percent"]}},
            "managers": _names, "registered_agent": _text, "registered_office": _text,
            "mailing_address": _text},
            "required": ["name"]},
        "lease": {"type": ["object", "null"], "properties": {
            "address": {"type": "string", "description": "Street address only"},
            "unit": _text, "city": {"type": "string"}, "county": _text,
            "state": {"type": "string", "description": "2 letters"},
            "zip": {"type": "string"}, "landlord_names": _names, "tenant_names": _names,
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            "monthly_rent": {"type": "number"}, "deposit": {"type": ["number", "null"]},
            "original_lease_date": {**_text, "description": "For an extension: date of the "
                                    "lease it extends, YYYY-MM-DD"},
            "other_terms": _text},
            "required": ["address", "city", "state", "zip", "landlord_names", "tenant_names",
                         "start_date", "end_date", "monthly_rent"]},
    },
    "required": ["doc_type", "title", "summary", "owner_llc", "llc", "lease"],
}


# --- Text helpers -------------------------------------------------------------------------

def redact(text: str) -> str:
    """Replace SSNs. EINs (12-3456789) and filing numbers are kept."""
    return SSN.sub("[REDACTED]", text)


def pdf_text(path: Path) -> tuple[str, int]:
    """Text layer of a PDF with [page N] markers, and the page count."""
    reader = PdfReader(path)
    pages = [" ".join((page.extract_text() or "").split()) for page in reader.pages]
    return "\n\n".join(f"[page {i}]\n{text}" for i, text in enumerate(pages, 1)), len(pages)


def document_text(path: Path, summary: str) -> tuple[str, int | None]:
    """What to store for a document: the summary, plus the full text when the file has a
    usable text layer (scans don't; then the summary is all there is)."""
    pages = None
    body = ""
    if path.suffix.lower() == ".pdf":
        text, pages = pdf_text(path)
        if len(text.split()) >= MIN_WORDS_PER_PAGE * max(pages, 1):
            body = text
    elif path.suffix.lower() in (".md", ".txt"):
        body = path.read_text(errors="replace")
    content = summary if not body else f"{summary}\n\n---\n\nFull text:\n\n{body}"
    return redact(content), pages


# --- Validation ---------------------------------------------------------------------------

def _str(value: Any, field: str, *, required: bool = False, limit: int = 300) -> str | None:
    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if SSN.search(text):
        raise ValueError(f"{field} contains an SSN; leave it out")
    return text[:limit]


def _date(value: Any, field: str, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field} is required (YYYY-MM-DD)")
        return None
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{field} must be YYYY-MM-DD") from None
    if not 1990 <= d.year <= 2100:
        raise ValueError(f"{field} {d} is out of range")
    return d.isoformat()


def _money(value: Any, field: str, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"{field} must be a number") from None
    if not Decimal(0) <= amount <= MAX_RENT:
        raise ValueError(f"{field} must be between $0 and $100,000")
    return str(amount)


def _names_list(values: Any, field: str, *, required: bool = False) -> list[str]:
    names = [n for v in (values or []) if (n := _str(v, field))]
    if required and not names:
        raise ValueError(f"{field} needs at least one name")
    return list(dict.fromkeys(names))


def _llc(raw: dict[str, Any]) -> dict[str, Any]:
    ein = _str(raw.get("ein"), "llc.ein")
    if ein and not EIN.match(ein):
        raise ValueError("llc.ein must look like 12-3456789")
    state = (_str(raw.get("state"), "llc.state") or "TX").upper()
    if len(state) != 2:
        raise ValueError("llc.state must be a 2-letter code")
    management = raw.get("management")
    if management not in (None, "managers", "members"):
        raise ValueError("llc.management must be managers, members or null")
    members = []
    for m in raw.get("members") or []:
        name = _str(m.get("name"), "llc.members.name", required=True)
        pct = m.get("percent")
        if pct is not None:
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                raise ValueError("llc.members.percent must be a number") from None
            if not 0 < pct <= 100:
                raise ValueError("llc.members.percent must be between 0 and 100")
        members.append({"name": name, "percent": pct})
    percents = [m["percent"] for m in members if m["percent"] is not None]
    if percents and len(percents) == len(members) and abs(sum(percents) - 100) > 0.01:
        raise ValueError(f"member percentages add up to {sum(percents):g}, not 100")
    return {"name": _str(raw.get("name"), "llc.name", required=True), "state": state,
            "file_number": _str(raw.get("file_number"), "llc.file_number"), "ein": ein,
            "formed_on": _date(raw.get("formed_on"), "llc.formed_on"),
            "management": management, "members": members,
            "managers": _names_list(raw.get("managers"), "llc.managers"),
            "registered_agent": _str(raw.get("registered_agent"), "llc.registered_agent"),
            "registered_office": _str(raw.get("registered_office"), "llc.registered_office"),
            "mailing_address": _str(raw.get("mailing_address"), "llc.mailing_address")}


def _lease(raw: dict[str, Any], doc_type: str) -> dict[str, Any]:
    state = (_str(raw.get("state"), "lease.state", required=True) or "").upper()
    if len(state) != 2:
        raise ValueError("lease.state must be a 2-letter code")
    zip_code = _str(raw.get("zip"), "lease.zip", required=True)
    if not ZIP.match(zip_code or ""):
        raise ValueError("lease.zip must be 5 digits")
    start = _date(raw.get("start_date"), "lease.start_date", required=True)
    end = _date(raw.get("end_date"), "lease.end_date", required=True)
    if end <= start:
        raise ValueError("lease.end_date must be after start_date")
    rent = _money(raw.get("monthly_rent"), "lease.monthly_rent", required=True)
    if Decimal(rent) == 0:
        raise ValueError("lease.monthly_rent must be more than $0")
    original = _date(raw.get("original_lease_date"), "lease.original_lease_date")
    if doc_type == "lease_extension" and not original:
        raise ValueError("an extension needs lease.original_lease_date")
    return {"address": _str(raw.get("address"), "lease.address", required=True),
            "unit": _str(raw.get("unit"), "lease.unit"),
            "city": _str(raw.get("city"), "lease.city", required=True),
            "county": _str(raw.get("county"), "lease.county"), "state": state, "zip": zip_code,
            "landlord_names": _names_list(raw.get("landlord_names"), "lease.landlord_names",
                                          required=True),
            "tenant_names": _names_list(raw.get("tenant_names"), "lease.tenant_names",
                                        required=True),
            "start_date": start, "end_date": end, "monthly_rent": rent,
            "deposit": _money(raw.get("deposit"), "lease.deposit"),
            "original_lease_date": original,
            "other_terms": _str(raw.get("other_terms"), "lease.other_terms", limit=2000)}


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    """Check the extracted facts. Returns a clean, JSON-safe copy or raises ValueError."""
    doc_type = raw.get("doc_type")
    if doc_type not in DOC_TYPES:
        raise ValueError(f"doc_type must be one of {', '.join(DOC_TYPES)}")
    summary = redact(str(raw.get("summary") or "").strip())
    if not summary:
        raise ValueError("summary is required")
    if len(summary) > MAX_SUMMARY:
        raise ValueError(f"summary is longer than {MAX_SUMMARY} characters")
    llc = raw.get("llc")
    lease = raw.get("lease")
    if doc_type in LLC_TYPES and not llc:
        raise ValueError(f"a {doc_type} needs the llc facts")
    if doc_type in LEASE_TYPES and not lease:
        raise ValueError(f"a {doc_type} needs the lease facts")
    return {"doc_type": doc_type,
            "title": _str(raw.get("title"), "title", required=True, limit=200),
            "summary": summary,
            "owner_llc": _str(raw.get("owner_llc"), "owner_llc"),
            "llc": _llc(llc) if llc else None,
            "lease": _lease(lease, doc_type) if lease else None}


# --- Saving -------------------------------------------------------------------------------

def _one(conn: psycopg.Connection, sql: str, params: tuple) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _save_llc(conn: psycopg.Connection, llc: dict[str, Any]) -> tuple[int, str]:
    llc_id = _one(conn, "select id from llcs where lower(name) = lower(%s)", (llc["name"],))
    fields = ["state", "file_number", "ein", "formed_on", "management", "registered_agent",
              "registered_office", "mailing_address"]
    values = tuple(llc[f] for f in fields)
    members = json.dumps(llc["members"])
    if llc_id is None:
        llc_id = _one(conn, f"""insert into llcs (name, {', '.join(fields)}, members, managers)
                                values (%s, {', '.join(['%s'] * len(fields))}, %s::jsonb, %s)
                                returning id""", (llc["name"], *values, members, llc["managers"]))
        return llc_id, f"Added LLC {llc['name']}."
    # Fill in what this document adds; keep what's already known when it's silent.
    sets = ", ".join(f"{f} = coalesce(%s, {f})" for f in fields)
    conn.execute(f"""update llcs set {sets},
                       members = case when %s::jsonb = '[]'::jsonb then members else %s::jsonb end,
                       managers = case when cardinality(%s::text[]) = 0 then managers
                                       else %s::text[] end,
                       updated_at = now()
                     where id = %s""",
                 (*values, members, members, llc["managers"], llc["managers"], llc_id))
    return llc_id, f"Updated LLC {llc['name']}."


def _save_lease(conn: psycopg.Connection, lease: dict[str, Any], llc_id: int | None,
                pdf_path: str | None) -> list[str]:
    done = []
    prop_id = _one(conn, """select id from properties
                            where lower(address) = lower(%s) and coalesce(unit, '') = coalesce(%s, '')""",
                   (lease["address"], lease["unit"]))
    if prop_id is None:
        prop_id = _one(conn, """insert into properties (address, unit, city, county, state, zip,
                                                        landlord_names, llc_id)
                                values (%s, %s, %s, %s, %s, %s, %s, %s) returning id""",
                       (lease["address"], lease["unit"], lease["city"], lease["county"],
                        lease["state"], lease["zip"], lease["landlord_names"], llc_id))
        done.append(f"Added property {lease['address']} (id {prop_id}). Add beds, baths and "
                    "sq ft in Supabase for rent research.")
    else:
        conn.execute("""update properties set city = coalesce(city, %s),
                          county = coalesce(county, %s), zip = coalesce(zip, %s),
                          landlord_names = %s, llc_id = coalesce(%s, llc_id)
                        where id = %s""",
                     (lease["city"], lease["county"], lease["zip"], lease["landlord_names"],
                      llc_id, prop_id))
        done.append(f"Matched property {lease['address']} (id {prop_id}).")

    tenant_ids = []
    for name in lease["tenant_names"]:
        # Match only tenants already on this property's leases, so a common name at another
        # property is never reused.
        tid = _one(conn, """select t.id from tenants t
                            join lease_tenants lt on lt.tenant_id = t.id
                            join leases l on l.id = lt.lease_id
                            where l.property_id = %s and lower(t.full_name) = lower(%s)
                            limit 1""", (prop_id, name))
        if tid is None:
            tid = _one(conn, "insert into tenants (full_name) values (%s) returning id", (name,))
            done.append(f"Added tenant {name}.")
        tenant_ids.append(tid)

    terms = (lease["monthly_rent"], lease["deposit"], lease["original_lease_date"],
             lease["other_terms"])
    dates = (lease["start_date"], lease["end_date"])
    found = conn.execute("""select id, awaiting_signature from leases
                            where property_id = %s and start_date = %s and end_date = %s""",
                         (prop_id, *dates)).fetchone()
    if found is None and lease["original_lease_date"]:
        # A signed extension whose dates were changed by hand still replaces the unsigned one
        # that generate-lease recorded.
        found = conn.execute("""select id, awaiting_signature from leases
                                where property_id = %s and awaiting_signature
                                order by end_date desc limit 1""", (prop_id,)).fetchone()
    if found is None:
        lease_id = _one(conn, """insert into leases (property_id, start_date, end_date, monthly_rent,
                                                     deposit, original_lease_date, other_terms,
                                                     pdf_path)
                                 values (%s, %s, %s, %s, %s, %s, %s, %s) returning id""",
                        (prop_id, *dates, *terms, pdf_path))
        done.append(f"Added lease term {dates[0]} to {dates[1]} at "
                    f"${Decimal(lease['monthly_rent']):,.2f}/month.")
    else:
        lease_id, was_unsigned = found
        conn.execute("""update leases set start_date = %s, end_date = %s, monthly_rent = %s,
                          deposit = coalesce(%s, deposit),
                          original_lease_date = coalesce(%s, original_lease_date),
                          other_terms = coalesce(%s, other_terms),
                          pdf_path = coalesce(%s, pdf_path), awaiting_signature = false
                        where id = %s""", (*dates, *terms, pdf_path, lease_id))
        done.append(f"Marked the extension {dates[0]} to {dates[1]} as signed at "
                    f"${Decimal(lease['monthly_rent']):,.2f}/month." if was_unsigned else
                    f"Updated the existing lease term {dates[0]} to {dates[1]}.")
    for tid in tenant_ids:
        conn.execute("""insert into lease_tenants (lease_id, tenant_id) values (%s, %s)
                        on conflict do nothing""", (lease_id, tid))
    return done


def _stored_pdf(source: Path) -> str | None:
    """The PDF's path relative to the artifacts folder, so generate-lease can attach the
    original lease to each extension. None for files kept elsewhere."""
    base = artifacts_dir().resolve()
    source = source.resolve()
    if source.suffix.lower() != ".pdf" or not source.is_relative_to(base):
        return None
    return str(source.relative_to(base))


def apply(conn: psycopg.Connection, data: dict[str, Any], source: Path) -> list[str]:
    """Save validated facts and the document text. The caller commits (or rolls back for a
    preview). Returns one line per change."""
    data = validate(data)  # re-check: the stored card may be old or edited
    done: list[str] = []
    llc_id = None
    if data["llc"]:
        llc_id, line = _save_llc(conn, data["llc"])
        done.append(line)
    if data["lease"]:
        if data["owner_llc"] and llc_id is None:
            llc_id = _one(conn, "select id from llcs where lower(name) = lower(%s)",
                          (data["owner_llc"],))
            if llc_id is None:
                raise ValueError(f"LLC {data['owner_llc']!r} isn't in the database yet; import "
                                 "its formation document first, or leave the owner blank")
        done.extend(_save_lease(conn, data["lease"], llc_id, _stored_pdf(source)))

    content, pages = document_text(source, f"# {data['title']}\n\n{data['summary']}")
    category = ("lease" if data["doc_type"] in LEASE_TYPES
                else "llc" if data["doc_type"] in LLC_TYPES else "notes")
    path = f"imports/{source.name}"
    conn.execute("""insert into reference_documents (path, category, title, pages, content)
                    values (%s, %s, %s, %s, %s)
                    on conflict (path) do update set category = excluded.category,
                      title = excluded.title, pages = excluded.pages,
                      content = excluded.content, updated_at = now()""",
                 (path, category, data["title"], pages, content))
    done.append(f"Saved the document text as {path} for questions in the chat.")
    return done
