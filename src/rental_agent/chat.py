"""One chat turn with the assistant (Claude Agent SDK).

Tools:
- read-only data lookups (rent, expenses, leases, research, job runs, properties);
- read-only access to the text of the owner's reference documents (leases, LLC papers, notes),
  stored in the reference_documents table;
- WebSearch / WebFetch for questions like local rental rules;
- the import-document skill (agent-plugin/skills/) and propose_document_import, for a new
  lease or LLC document; the import-statement skill and propose_statement_import, for rent and
  expenses in a bank or card statement;
- propose_expense / propose_job / propose_memory / propose_document_import /
  propose_correction / propose_property_update, which only record a *proposal*. The web app
  shows each as a card; nothing is saved, changed, run or remembered until the owner clicks.
Attachments are sent to the model as content blocks; the agent has no file or shell tools.
"""

import base64
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from rental_agent import (
    corrections,
    db,
    doc_import,
    memory,
    move_out,
    prompts,
    property_facts,
    runner,
    statement_import,
)
from rental_agent.config import MODELS, Settings, data_dir
from rental_agent.extract import SCHEDULE_E_CATEGORIES

# Skills for the chat live in a local plugin; .claude/skills links to the same files so
# Claude Code in this repo uses them too.
PLUGIN_DIR = Path(__file__).resolve().parents[2] / "agent-plugin"
SKILLS = ["rental:import-document", "rental:import-statement"]

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".gif": "image/gif", ".webp": "image/webp"}
TEXT_TYPES = {".txt", ".csv", ".md", ".ofx", ".qfx"}  # OFX/QFX: bank exports
UPLOAD_TYPES = set(IMAGE_TYPES) | TEXT_TYPES | {".pdf"}
MAGIC = {".pdf": [b"%PDF"], ".png": [b"\x89PNG"], ".jpg": [b"\xff\xd8\xff"],
         ".jpeg": [b"\xff\xd8\xff"], ".gif": [b"GIF8"], ".webp": [b"RIFF"]}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_EXPENSE = Decimal(100000)
NO_DOCUMENTS = ("The documents table doesn't exist. The owner needs to run "
                "supabase/schema.sql in the Supabase SQL Editor.")

TOOL_LABELS = {"WebSearch": "Searching the web", "WebFetch": "Reading a web page",
               "mcp__rental__list_reference_documents": "Looking through your documents",
               "mcp__rental__read_reference_document": "Reading a document",
               "Skill": "Following the import steps"}


def check_upload(name: str, data: bytes) -> str:
    """Return the lowercase extension if the file is an allowed, non-empty, plausible upload."""
    ext = Path(name).suffix.lower()
    if ext not in UPLOAD_TYPES:
        raise ValueError(f"{name}: only images (JPG, PNG, GIF, WebP), PDF, TXT, CSV, MD, "
                         "OFX or QFX files")
    if not data:
        raise ValueError(f"{name} is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{name} is larger than 20 MB")
    if ext in MAGIC and not any(data.startswith(m) for m in MAGIC[ext]):
        raise ValueError(f"{name} doesn't look like a real {ext[1:].upper()} file")
    return ext


@dataclass
class TurnResult:
    text: str
    proposals: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None
    error: str | None = None


def _rows(rows: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(rows, default=str)}]}


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def validate_expense(args: dict[str, Any], property_ids: set[int], attachments: set[str],
                     today: date) -> dict[str, Any]:
    try:
        spent_on = date.fromisoformat(str(args["date"]))
        amount = Decimal(str(args["amount"])).quantize(Decimal("0.01"))
    except (KeyError, ValueError, InvalidOperation):
        raise ValueError("date must be YYYY-MM-DD and amount a number") from None
    if spent_on > today or today - spent_on > timedelta(days=730):
        raise ValueError(f"date {spent_on} must be within the last two years")
    if not Decimal(0) < amount <= MAX_EXPENSE:
        raise ValueError("amount must be positive and under $100,000")
    category = args.get("category")
    if category not in SCHEDULE_E_CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(SCHEDULE_E_CATEGORIES)}")
    property_id = args.get("property_id")
    if property_id is not None and property_id not in property_ids:
        raise ValueError(f"unknown property_id {property_id}")
    receipt = args.get("receipt") or None
    if receipt is not None and receipt not in attachments:
        raise ValueError("receipt must be one of this message's attachment paths, or null")
    return {"date": spent_on.isoformat(), "amount": str(amount),
            "vendor": (args.get("vendor") or "").strip()[:200] or None,
            "category": category, "property_id": property_id,
            "description": (args.get("description") or "").strip()[:500] or None,
            "receipt": receipt}


def make_tools(settings: Settings, proposals: list[dict[str, Any]], attachments: set[str]):
    def connect():
        return db.connect(settings.db_url)

    def propose(kind: str, data: dict[str, Any]) -> dict[str, Any]:
        proposals.append({"id": uuid.uuid4().hex[:10], "kind": kind, "status": "pending",
                          "data": data})
        return {"content": [{"type": "text", "text":
                "Shown to the owner as a card. Nothing has happened yet; it takes effect only "
                "if they click to confirm."}]}

    year_schema = {"type": "object", "properties": {"year": {"type": "integer"}},
                   "required": ["year"]}
    none_schema = {"type": "object", "properties": {}}

    @tool("year_summary", "Rent collected, expenses by Schedule E category, and items needing "
          "review, for one year.", year_schema)
    async def year_summary(args):
        with connect() as conn:
            totals = db.year_totals(conn, args["year"])
            by_cat: dict[str, Decimal] = {}
            for e in db.expenses_list(conn, args["year"]):
                by_cat[e["category"]] = by_cat.get(e["category"], Decimal(0)) + e["amount"]
        return _rows({**totals, "expenses_by_category": by_cat})

    @tool("list_rent_payments", "Rent payments recorded in a year, with the id "
          "propose_correction needs.", year_schema)
    async def list_rent_payments(args):
        with connect() as conn:
            return _rows(db.rent_payments_for_year(conn, args["year"]))

    @tool("list_expenses", "Expenses recorded in a year, with the id propose_correction "
          "needs.", year_schema)
    async def list_expenses(args):
        with connect() as conn:
            return _rows(db.expenses_list(conn, args["year"]))

    @tool("list_leases", "All lease terms with rent, dates and tenants.", none_schema)
    async def list_leases(args):
        with connect() as conn:
            return _rows(db.leases_list(conn))

    @tool("list_rent_research", "Saved rent recommendations by year.", none_schema)
    async def list_rent_research(args):
        with connect() as conn:
            return _rows(db.rent_research_list(conn))

    @tool("list_properties", "Properties with ids, address, type, beds, baths, sq ft, purchase "
          "date and cost basis.", none_schema)
    async def list_properties(args):
        with connect() as conn:
            return _rows(db.properties_list(conn))

    @tool("list_job_runs", "Recent agent job runs with status and summary.", none_schema)
    async def list_job_runs(args):
        with connect() as conn:
            return _rows(db.job_runs(conn, limit=20))

    @tool("list_reference_documents", "The owner's stored documents (signed leases and lease "
          "extensions, LLC formation, operating agreement, EIN letters, property notes) with "
          "path, title and length.", none_schema)
    async def list_reference_documents(args):
        try:
            with connect() as conn:
                return _rows(db.reference_documents_list(conn))
        except psycopg.errors.UndefinedTable:
            return _err(NO_DOCUMENTS)

    @tool("read_reference_document", "Full text of one stored document, by path from "
          "list_reference_documents. PDF text is split by [page N] markers.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
    async def read_reference_document(args):
        try:
            with connect() as conn:
                doc = db.reference_document(conn, str(args.get("path", "")))
        except psycopg.errors.UndefinedTable:
            return _err(NO_DOCUMENTS)
        if doc is None:
            return _err("No document with that path; call list_reference_documents.")
        return {"content": [{"type": "text", "text":
                f"<document path=\"{doc['path']}\" title=\"{doc['title']}\">\n"
                f"{doc['content']}\n</document>"}]}

    @tool("propose_expense", "Show the owner an expense card to confirm (e.g. from a receipt). "
          "Does not save anything.", {
              "type": "object",
              "properties": {
                  "date": {"type": "string", "description": "YYYY-MM-DD"},
                  "amount": {"type": "number"},
                  "vendor": {"type": "string"},
                  "category": {"type": "string", "enum": SCHEDULE_E_CATEGORIES},
                  "property_id": {"type": ["integer", "null"],
                                  "description": "null for an LLC-wide expense"},
                  "description": {"type": "string"},
                  "receipt": {"type": ["string", "null"],
                              "description": "attachment path from this message, or null"},
              },
              "required": ["date", "amount", "vendor", "category", "property_id",
                           "description", "receipt"]})
    async def propose_expense(args):
        with connect() as conn:
            property_ids = {p["id"] for p in db.properties_list(conn)}
        try:
            data = validate_expense(args, property_ids, attachments, date.today())
        except ValueError as exc:
            return _err(f"Not proposed: {exc}")
        return propose("expense", data)

    @tool("propose_job", "Show the owner a card to run one agent job. Jobs: "
          + "; ".join(f"{name}: {desc} (options: {', '.join(p.name for p in ps)})"
                      for name, (desc, ps) in runner.JOBS.items())
          + ". Does not run anything.", {
              "type": "object",
              "properties": {"job": {"type": "string", "enum": list(runner.JOBS)},
                             "options": {"type": "object"}},
              "required": ["job", "options"]})
    async def propose_job(args):
        job, options = args.get("job"), args.get("options") or {}
        try:
            runner.parse_params(job, {k: ("on" if v is True else "" if v in (None, False) else str(v))
                                      for k, v in options.items()})
        except ValueError as exc:
            return _err(f"Not proposed: {exc}")
        return propose("job", {"job": job, "options": {k: v for k, v in options.items()}})

    @tool("propose_document_import", "Show the owner a card to save a new lease or LLC "
          "document's facts (LLC, property, tenants, lease term) or a year-end document's "
          "expense lines (Form 1098, property tax statement, insurance declaration), and the "
          "document's text. Follow the import-document skill first. Does not save anything.",
          {**doc_import.IMPORT_SCHEMA,
           "properties": {**doc_import.IMPORT_SCHEMA["properties"],
                          "attachment": {"type": "string",
                                         "description": "path of the attached document"}},
           "required": [*doc_import.IMPORT_SCHEMA["required"], "attachment"]})
    async def propose_document_import(args):
        attachment = args.get("attachment")
        if attachment not in attachments:
            return _err("Not proposed: attachment must be the path of a file attached to "
                        "this message. Ask the owner to attach the document again.")
        try:
            data = doc_import.validate(args)
            if data["doc_type"] in doc_import.YEAR_END_TYPES:
                with connect() as conn:
                    data = doc_import.mark_existing(conn, data)
        except ValueError as exc:
            return _err(f"Not proposed: {exc}")
        result = propose("document", {**data, "attachment": attachment})
        already = [line for line in data["expenses"] if line["existing"]]
        if already:
            result["content"].append({"type": "text", "text":
                f"{len(already)} expense line(s) look already recorded and are unticked: "
                + "; ".join(f"{e['date']} ${e['amount']} ({e['existing']})" for e in already)})
        return result

    @tool("propose_statement_import", "Show the owner a table of rent payments and rental "
          "expenses from an attached statement, to tick and save. Follow the import-statement "
          "skill first. Does not save anything.", {
              "type": "object",
              "properties": {
                  "attachment": {"type": "string",
                                 "description": "path of the attached statement"},
                  "statement_name": {"type": "string",
                                     "description": "e.g. 'Checking, Jan to Nov 2026'"},
                  "transactions": statement_import.TRANSACTIONS_SCHEMA,
              },
              "required": ["attachment", "statement_name", "transactions"]})
    async def propose_statement_import(args):
        attachment = args.get("attachment")
        if attachment not in attachments:
            return _err("Not proposed: attachment must be the path of a file attached to "
                        "this message. Ask the owner to attach the statement again.")
        with connect() as conn:
            property_ids = {p["id"] for p in db.properties_list(conn)}
            try:
                rows = statement_import.validate(args.get("transactions") or [], property_ids,
                                                 date.today())
            except ValueError as exc:
                return _err(f"Not proposed: {exc}")
            rows = statement_import.mark_existing(conn, rows)
        name = " ".join(str(args.get("statement_name") or "Statement").split())[:120]
        result = propose("statement", {"name": name, "attachment": attachment, "rows": rows})
        already = [r for r in rows if r["existing"]]
        if already:
            result["content"].append({"type": "text", "text":
                f"{len(already)} row(s) look already recorded and are unticked: "
                + "; ".join(f"{r['date']} ${r['amount']} ({r['existing']})" for r in already)})
        return result

    @tool("propose_correction", "Show the owner a card to fix or remove one recorded rent "
          "payment or expense, by its id from list_rent_payments or list_expenses. For an "
          "update, `changes` holds only the fields to change (rent: "
          + ", ".join(corrections.FIELDS["rent"]) + "; expense: "
          + ", ".join(corrections.FIELDS["expense"]) + "); leave it empty to mark a flagged "
          "row as checked. A removed row is left out of totals and can be restored. Does not "
          "change anything.", {
              "type": "object",
              "properties": {
                  "kind": {"type": "string", "enum": list(corrections.KINDS)},
                  "id": {"type": "integer"},
                  "action": {"type": "string", "enum": ["update", "remove"]},
                  "changes": {"type": "object"},
                  "reason": {"type": "string",
                             "description": "one short sentence, shown on the card"},
              },
              "required": ["kind", "id", "action", "changes", "reason"]})
    async def propose_correction(args):
        with connect() as conn:
            try:
                card = corrections.propose(conn, args, date.today())
            except ValueError as exc:
                return _err(f"Not proposed: {exc}")
        return propose("correction", card)

    nullable_number = {"type": ["number", "null"]}

    @tool("propose_property_update", "Show the owner a card to set a property's facts. Rent "
          "research and the tax protest need property_type, beds, baths and sqft; the tax "
          "report estimates depreciation from purchase_date and cost_basis (the building "
          "only, without the land). `changes` holds only the facts to change; null clears "
          "one. Does not change anything.", {
              "type": "object",
              "properties": {
                  "property_id": {"type": "integer"},
                  "changes": {"type": "object", "properties": {
                      "property_type": {"type": ["string", "null"],
                                        "description": "e.g. 'single-family house', 'townhome'"},
                      "beds": nullable_number, "baths": nullable_number,
                      "sqft": {"type": ["integer", "null"]},
                      "purchase_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                      "cost_basis": nullable_number}},
                  "reason": {"type": "string", "description": "one short sentence, shown on "
                             "the card: where the facts come from"},
              },
              "required": ["property_id", "changes", "reason"]})
    async def propose_property_update(args):
        with connect() as conn:
            try:
                card = property_facts.propose(conn, args, date.today())
            except ValueError as exc:
                return _err(f"Not proposed: {exc}")
        return propose("property", card)

    @tool("list_move_outs", "Recorded move-outs with the lease id, the move-out date, the "
          "security deposit held, the deductions so far, the refund and the day the deposit "
          "letter is due.", none_schema)
    async def list_move_outs(args):
        with connect() as conn:
            return _rows([{
                "lease_id": m.lease_id, "property": m.label, "tenants": m.tenants,
                "move_out_date": m.move_out_date, "deposit": m.deposit,
                "forwarding_address": m.forwarding_address,
                "deductions": [{"description": d.description, "amount": d.amount}
                               for d in m.deductions],
                "refund": m.refund, "letter_due": m.deadline, "letter_sent_on": m.sent_on,
            } for m in move_out.load(conn, date.today())])

    @tool("propose_deposit_deductions", "Show the owner a card of deductions from a security "
          "deposit, for the itemized letter after a move-out. lease_id comes from "
          "list_move_outs. Each line is one damage or charge the tenant is liable for under "
          "the lease, with its cost; never normal wear and tear. Does not change anything.", {
              "type": "object",
              "properties": {
                  "lease_id": {"type": "integer"},
                  "lines": {"type": "array", "items": {
                      "type": "object",
                      "properties": {
                          "description": {"type": "string", "description":
                                          "what the letter will say, e.g. 'Repair hole in "
                                          "bedroom door'"},
                          "amount": {"type": "number", "description": "USD, positive"},
                          "basis": {"type": "string", "description":
                                    "where the amount comes from, e.g. 'invoice from ABC "
                                    "Handyman, attached'; shown on the card only"},
                      },
                      "required": ["description", "amount", "basis"]}},
                  "reason": {"type": "string", "description": "one short sentence, shown on "
                             "the card"},
              },
              "required": ["lease_id", "lines", "reason"]})
    async def propose_deposit_deductions(args):
        with connect() as conn:
            try:
                card = move_out.propose(conn, args, date.today())
            except ValueError as exc:
                return _err(f"Not proposed: {exc}")
        return propose("deductions", card)

    @tool("propose_memory", "Show the owner a card to add one standing rule to memory.md.",
          {"type": "object", "properties": {"rule": {"type": "string"}}, "required": ["rule"]})
    async def propose_memory(args):
        rule = " ".join(str(args.get("rule", "")).split())
        if not rule or len(rule) > 500:
            return _err("Not proposed: the rule must be 1-500 characters")
        return propose("memory", {"rule": rule})

    return [year_summary, list_rent_payments, list_expenses, list_leases, list_rent_research,
            list_properties, list_job_runs, list_reference_documents, read_reference_document,
            propose_expense, propose_job, propose_document_import, propose_statement_import,
            propose_correction, propose_property_update, list_move_outs,
            propose_deposit_deductions, propose_memory]


def build_tools(settings: Settings, proposals: list[dict[str, Any]], attachments: set[str]):
    tools = make_tools(settings, proposals, attachments)
    return create_sdk_mcp_server("rental", tools=tools), [f"mcp__rental__{t.name}" for t in tools]


def system_prompt(settings: Settings) -> str:
    with db.connect(settings.db_url) as conn:
        props = db.properties_list(conn)
    listing = "\n".join(f"- id {p['id']}: {p['address']}, {p['city']} {p['zip']} "
                        f"({p['property_type'] or 'type unknown'})" for p in props) or "(none yet)"
    return prompts.get("chat-assistant").format(today=f"{date.today():%A, %B %-d, %Y}",
                                                properties=listing) + memory.prompt_section()


def content_blocks(text: str, files: list[tuple[str, Path]]) -> list[dict[str, Any]]:
    """files: (relative path shown to the model, absolute path)."""
    blocks: list[dict[str, Any]] = []
    for rel, path in files:
        ext = path.suffix.lower()
        if ext in IMAGE_TYPES:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[ext],
                           "data": base64.b64encode(path.read_bytes()).decode()}})
        elif ext == ".pdf":
            blocks.append({"type": "document", "source": {"type": "base64",
                           "media_type": "application/pdf",
                           "data": base64.b64encode(path.read_bytes()).decode()}, "title": rel})
        else:
            blocks.append({"type": "text", "text": f"<attachment name=\"{rel}\">\n"
                           f"{path.read_text(errors='replace')}\n</attachment>"})
    names = "\n".join(f"- {rel}" for rel, _ in files)
    note = f"\n\n[Attached files (use these paths as `receipt`):\n{names}]" if files else ""
    blocks.append({"type": "text", "text": (text or "(no message)") + note})
    return blocks


async def _one_message(blocks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "user", "message": {"role": "user", "content": blocks},
           "parent_tool_use_id": None}


async def run_turn(
    settings: Settings,
    *,
    model: str,
    text: str,
    files: list[tuple[str, Path]],
    session_id: str | None,
    history: str,
    on_text: Callable[[str], None],
    on_status: Callable[[str], None],
) -> TurnResult:
    """Run one turn. Resumes the SDK session when possible; otherwise starts fresh with the
    saved history as context."""
    if model not in MODELS:
        raise ValueError(f"Unknown model {model}")
    proposals: list[dict[str, Any]] = []
    server, tool_names = build_tools(settings, proposals, {rel for rel, _ in files})
    sdk_home = data_dir() / "claude"
    sdk_home.mkdir(parents=True, exist_ok=True)
    workdir = data_dir() / "chat-workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    base = {
        "model": model,
        "system_prompt": system_prompt(settings),
        "mcp_servers": {"rental": server},
        "tools": ["WebSearch", "WebFetch", "Skill"],   # `skills` limits Skill to SKILLS
        "allowed_tools": ["WebSearch", "WebFetch", *tool_names],
        "permission_mode": "dontAsk",
        "setting_sources": [],
        "plugins": [{"type": "local", "path": str(PLUGIN_DIR)}],
        "skills": SKILLS,
        "include_partial_messages": True,
        "max_turns": 25,
        "cwd": str(workdir),
        "env": {**settings.claude_env, "CLAUDE_CONFIG_DIR": str(sdk_home)},
    }

    async def attempt(resume: str | None, prompt_text: str) -> TurnResult:
        parts: list[str] = []
        live = ""
        result: ResultMessage | None = None
        async for msg in query(prompt=_one_message(content_blocks(prompt_text, files)),
                               options=ClaudeAgentOptions(resume=resume, **base)):
            if isinstance(msg, StreamEvent) and msg.parent_tool_use_id is None:
                ev = msg.event
                if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    live += ev["delta"]["text"]
                    on_text("\n\n".join([*parts, live]) if parts else live)
            elif isinstance(msg, AssistantMessage) and msg.parent_tool_use_id is None:
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        parts.append(block.text.strip())
                    elif isinstance(block, ToolUseBlock):
                        on_status(TOOL_LABELS.get(block.name, "Checking your records"))
                live = ""
            elif isinstance(msg, ResultMessage):
                result = msg
        if result:
            db.record_claude_cost(settings.db_url, "chat", model, result.total_cost_usd)
        if result is None or result.is_error:
            detail = (result.errors or [result.subtype]) if result else ["no result"]
            return TurnResult("\n\n".join(parts), proposals, getattr(result, "session_id", None),
                              error="; ".join(str(d) for d in detail))
        return TurnResult("\n\n".join(parts), proposals, result.session_id)

    if session_id:
        try:
            first = await attempt(session_id, text)
            if not first.error or first.text:
                return first
        except Exception:  # noqa: BLE001 — session files gone (e.g. new volume): start fresh
            proposals.clear()
    prefix = f"Earlier in this chat:\n{history}\n\n---\n\n" if history else ""
    try:
        return await attempt(None, prefix + text)
    except Exception as exc:  # noqa: BLE001 — shown to the owner in the chat
        return TurnResult("", proposals, None, error=f"{type(exc).__name__}: {exc}")
