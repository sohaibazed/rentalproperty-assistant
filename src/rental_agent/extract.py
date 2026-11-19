"""Turn one email into a structured rent/expense candidate using the Claude Agent SDK.

The model gets no tools: it only reads the email and returns JSON matching EXTRACTION_SCHEMA.
Everything it returns is checked again in validate.py before it reaches the database.
"""

from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from rental_agent import db, memory, prompts
from rental_agent.db import Property
from rental_agent.gmail import EmailMessage

SCHEDULE_E_CATEGORIES = [
    "advertising", "auto_travel", "cleaning_maintenance", "commissions", "insurance",
    "legal_professional", "management_fees", "mortgage_interest", "other_interest",
    "repairs", "supplies", "taxes", "utilities", "depreciation", "other",
]

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind", "amount", "date", "counterparty", "description",
        "category", "property_id", "from_statement", "confidence", "reason",
    ],
    "properties": {
        "kind": {"type": "string", "enum": ["rent", "expense", "ignore"]},
        "amount": {"type": ["number", "null"], "description": "USD, positive"},
        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD of payment/purchase"},
        "counterparty": {"type": ["string", "null"], "description": "payer (rent) or vendor"},
        "description": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"], "enum": [*SCHEDULE_E_CATEGORIES, None]},
        "property_id": {"type": ["integer", "null"]},
        "from_statement": {"type": "boolean", "description":
                           "true when the amount is what a statement or bill says is due, "
                           "not a confirmation that it was paid"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "description": "one sentence"},
    },
}


def _format_properties(properties: list[Property]) -> str:
    if not properties:
        return "(none recorded yet)"
    return "\n".join(f"- id {p.id}: {p.label}" for p in properties)


def _format_email(msg: EmailMessage) -> str:
    attachment_names = ", ".join(a.filename for a in msg.attachments) or "none"
    return (
        "<email>\n"
        f"From: {msg.sender}\n"
        f"Subject: {msg.subject}\n"
        f"Sent: {msg.sent_at.isoformat()}\n"
        f"Attachments: {attachment_names}\n\n"
        f"{msg.body}\n"
        "</email>"
        + "".join(f'\n<attachment name="{name}">\n{text}\n</attachment>'
                  for name, text in msg.pdf_text.items())
    )


async def extract(
    msg: EmailMessage, properties: list[Property], model: str, env: dict[str, str], db_url: str
) -> dict[str, Any]:
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=prompts.get("email-extraction").format(properties=_format_properties(properties))
        + memory.prompt_section(),
        tools=[],                # no built-in tools at all
        allowed_tools=[],
        permission_mode="dontAsk",
        setting_sources=[],      # don't load user/project CLAUDE.md or settings
        output_format={"type": "json_schema", "schema": EXTRACTION_SCHEMA},
        max_turns=3,
        effort="medium",
        env=env,                 # Claude credentials, never logged
    )
    result: ResultMessage | None = None
    async for message in query(prompt=_format_email(msg), options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result:
        db.record_claude_cost(db_url, "email-extraction", model, result.total_cost_usd)
    if result is None or result.is_error or result.structured_output is None:
        detail = result.subtype if result else "no result"
        raise RuntimeError(f"Extraction failed for message {msg.message_id}: {detail}")
    return result.structured_output
