"""research-rent: yearly rent recommendation from comparable listings.

Two agents, no write access anywhere:
- comps-researcher (subagent): WebSearch/WebFetch only. Reads listing pages and returns
  a table of comparable rentals, each with its source URL.
- main agent: hands the web work to the subagent, then recommends a rent from the comps and
  the property facts that Python supplies. Returns structured JSON.

Python then drops unusable comps, computes the stats itself, flags anything doubtful,
writes the report, and saves the result so generate-lease can use it.

A dry run writes `<address>-dry-run.md` plus a `.json` file holding the recommendation, and
saves nothing. `save_dry_run` later saves that recommendation without researching again.
"""

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, ResultMessage, query

from rental_agent import db, memory, prompts
from rental_agent.config import Settings
from rental_agent.jobs.lease import NOTICE_DAYS, long_date, money

MIN_COMPS = 5
DRY_RUN_SUFFIX = "-dry-run"
DRY_RUN_NOTE = "> **Dry run:** this recommendation is not saved."

RENT_RATIO_LIMITS = (Decimal("0.4"), Decimal("2.5"))  # vs current rent

# Prompts live in prompts/agent-prompts.md; read at each run so web edits apply.

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["comps", "market_notes", "recommended_rent", "range_low", "range_high",
                 "rationale"],
    "properties": {
        "comps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["address", "beds", "baths", "sqft", "rent", "date", "status",
                             "source_url"],
                "properties": {
                    "address": {"type": "string"},
                    "beds": {"type": ["number", "null"]},
                    "baths": {"type": ["number", "null"]},
                    "sqft": {"type": ["integer", "null"]},
                    "rent": {"type": "number"},
                    "date": {"type": ["string", "null"]},
                    "status": {"type": "string", "enum": ["listed", "leased", "unknown"]},
                    "source_url": {"type": "string"},
                },
            },
        },
        "market_notes": {"type": "string"},
        "recommended_rent": {"type": "number"},
        "range_low": {"type": "number"},
        "range_high": {"type": "number"},
        "rationale": {"type": "string"},
    },
}


@dataclass(frozen=True)
class Facts:
    property_id: int
    address: str
    property_type: str
    beds: Decimal
    baths: Decimal
    sqft: int
    current_rent: Decimal
    current_term_end: date
    year: int
    history: list[tuple[int, Decimal]]

    def as_text(self) -> str:
        past = ", ".join(f"{y}: {money(r)}" for y, r in self.history) or "none"
        return (
            f"Address: {self.address}\n"
            f"Type: {self.property_type}\n"
            f"Bedrooms: {self.beds}, bathrooms: {self.baths}, square feet: {self.sqft}\n"
            f"Current monthly rent: {money(self.current_rent)} "
            f"(term ends {long_date(self.current_term_end)})\n"
            f"Recommending rent for: {self.year}\n"
            f"Past recommendations: {past}\n"
        )


@dataclass(frozen=True)
class Comp:
    address: str
    beds: float | None
    baths: float | None
    sqft: int | None
    rent: Decimal
    date: str | None
    status: str
    source_url: str


@dataclass
class Research:
    facts: Facts
    comps: list[Comp]
    dropped: list[tuple[str, str]]          # (address, reason)
    median: Decimal
    p25: Decimal
    p75: Decimal
    per_sqft_estimate: Decimal | None       # median $/sqft × subject sqft
    recommended: Decimal
    range_low: Decimal
    range_high: Decimal
    rationale: str
    market_notes: str
    flags: list[str] = field(default_factory=list)


def agent_options(settings: Settings) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=settings.model,
        system_prompt=prompts.get("rent-recommendation") + memory.prompt_section(),
        agents={
            "comps-researcher": AgentDefinition(
                description=prompts.get("comps-researcher-description").strip(),
                prompt=prompts.get("comps-researcher") + memory.prompt_section(),
                tools=["WebSearch", "WebFetch"],
                model="inherit",
                maxTurns=25,
                effort="medium",
            ),
        },
        # A subagent can only use tools the session has. Neither agent gets file, shell,
        # or database tools; the main prompt keeps web work with the subagent.
        tools=["Agent", "WebSearch", "WebFetch"],
        allowed_tools=["Agent", "WebSearch", "WebFetch"],
        permission_mode="dontAsk",
        setting_sources=[],
        output_format={"type": "json_schema", "schema": OUTPUT_SCHEMA},
        max_turns=10,
        effort="high",
        env=settings.claude_env,
    )


async def ask_agents(facts: Facts, settings: Settings) -> dict[str, Any]:
    result: ResultMessage | None = None
    async for message in query(prompt=facts.as_text(), options=agent_options(settings)):
        if isinstance(message, ResultMessage):
            result = message
    if result:
        db.record_claude_cost(settings.db_url, "rent-research", settings.model,
                              result.total_cost_usd)
    if result is None or result.is_error or result.structured_output is None:
        detail = result.subtype if result else "no result"
        raise RuntimeError(f"Rent research failed: {detail}")
    return result.structured_output


def clean_comps(raw: list[dict[str, Any]], current_rent: Decimal) -> tuple[list[Comp], list[tuple[str, str]]]:
    low, high = (current_rent * r for r in RENT_RATIO_LIMITS)
    kept: list[Comp] = []
    dropped: list[tuple[str, str]] = []
    seen: set[tuple[str, Decimal]] = set()
    for item in raw:
        address = str(item.get("address") or "unknown").strip()
        url = str(item.get("source_url") or "").strip()
        try:
            rent = Decimal(str(item.get("rent"))).quantize(Decimal("0.01"))
        except Exception:  # noqa: BLE001 — any unparseable rent is dropped
            dropped.append((address, "rent not a number"))
            continue
        if not re.match(r"https?://", url):
            dropped.append((address, "no source URL"))
        elif not low <= rent <= high:
            dropped.append((address, f"rent {money(rent)} outside {money(low)}–{money(high)}"))
        elif (address.lower(), rent) in seen:
            dropped.append((address, "duplicate"))
        else:
            seen.add((address.lower(), rent))
            kept.append(Comp(address, item.get("beds"), item.get("baths"), item.get("sqft"),
                             rent, item.get("date"), item.get("status", "unknown"), url))
    return kept, dropped


def _quantile(values: list[Decimal], q: float) -> Decimal:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return (ordered[lo] + (ordered[hi] - ordered[lo]) * Decimal(str(pos - lo))).quantize(Decimal(1))


def analyze(facts: Facts, raw: dict[str, Any]) -> Research:
    comps, dropped = clean_comps(raw.get("comps", []), facts.current_rent)
    if not comps:
        raise RuntimeError("No usable comps came back; nothing to recommend from.")
    rents = [c.rent for c in comps]
    per_sqft = [c.rent / c.sqft for c in comps if c.sqft]
    per_sqft_estimate = (
        (Decimal(str(statistics.median(per_sqft))) * facts.sqft).quantize(Decimal(1))
        if per_sqft else None
    )
    r = Research(
        facts=facts,
        comps=comps,
        dropped=dropped,
        median=_quantile(rents, 0.5),
        p25=_quantile(rents, 0.25),
        p75=_quantile(rents, 0.75),
        per_sqft_estimate=per_sqft_estimate,
        recommended=Decimal(str(raw["recommended_rent"])).quantize(Decimal(1)),
        range_low=Decimal(str(raw["range_low"])).quantize(Decimal(1)),
        range_high=Decimal(str(raw["range_high"])).quantize(Decimal(1)),
        rationale=str(raw.get("rationale", "")).strip(),
        market_notes=str(raw.get("market_notes", "")).strip(),
    )
    if r.recommended <= 0:
        raise RuntimeError("Agent returned a non-positive rent")
    if len(comps) < MIN_COMPS:
        r.flags.append(f"Only {len(comps)} usable comps (want {MIN_COMPS}+); treat with caution.")
    if not min(rents) <= r.recommended <= max(rents):
        r.flags.append(f"Recommended rent is outside the comps' range "
                       f"({money(min(rents))}–{money(max(rents))}).")
    if not r.range_low <= r.recommended <= r.range_high or r.range_low > r.range_high:
        r.flags.append("Recommended rent is not inside the agent's own range.")
    return r


def render_report(r: Research, today: date, dry_run: bool = False) -> str:
    f = r.facts
    change = r.recommended - f.current_rent
    pct = change / f.current_rent * 100
    sign_by = f.current_term_end - timedelta(days=NOTICE_DAYS)
    lines = [
        f"# Rent research {f.year}: {f.address}",
        "",
        f"_Generated {long_date(today)}. Advice only; the rent decision is yours._",
        "",
        *([DRY_RUN_NOTE, ""] if dry_run else []),
        "## Recommendation",
        "",
        (f"- **Recommended rent:** {money(r.recommended)}/month "
        f"(range {money(r.range_low)}–{money(r.range_high)})"),
        f"- **Current rent:** {money(f.current_rent)} → change {money(change)} ({pct:+.1f}%)",
        (f"- **Sign-by date:** {long_date(sign_by)} ({NOTICE_DAYS} days before the current term "
        "ends; after that the lease rolls to month-to-month at the current rent)"),
    ]
    if change > 0:
        months = (f.current_rent / change).quantize(Decimal("0.1"))
        lines.append(f"- **Vacancy check:** the increase takes {months} months to earn back one "
                     "vacant month.")
    lines += ["", r.rationale, ""]
    if r.flags:
        lines += ["## Check before using", ""] + [f"- {x}" for x in r.flags] + [""]
    lines += [
        "## Comparable rentals",
        "",
        f"Computed from {len(r.comps)} comps: median {money(r.median)}, "
        f"middle half {money(r.p25)}–{money(r.p75)}"
        + (f", size-adjusted estimate {money(r.per_sqft_estimate)} "
           f"(median $/sq ft × {f.sqft} sq ft)" if r.per_sqft_estimate else "") + ".",
        "",
        "| Address | Beds | Baths | Sq ft | Rent | Date | Status | Source |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in sorted(r.comps, key=lambda c: c.rent):
        lines.append(f"| {c.address} | {c.beds or '-'} | {c.baths or '-'} | {c.sqft or '-'} | "
                     f"{money(c.rent)} | {c.date or '-'} | {c.status} | [link]({c.source_url}) |")
    if r.dropped:
        lines += ["", "Dropped:", ""] + [f"- {a}: {why}" for a, why in r.dropped]
    if r.market_notes:
        lines += ["", "## Market notes", "", r.market_notes]
    lines += ["", "## Property facts used", "", "```", f.as_text().rstrip(), "```", ""]
    return "\n".join(lines)


def comps_json(comps: list[Comp]) -> str:
    return json.dumps([{**c.__dict__, "rent": float(c.rent)} for c in comps])


def load_facts(conn, property_id: int, year: int | None) -> Facts:
    prop = db.property_detail(conn, property_id)
    size = db.property_size(conn, property_id)
    current = db.latest_lease(conn, property_id)
    if prop is None or size is None:
        raise ValueError(f"No property with id {property_id}")
    if current is None:
        raise ValueError(f"Property {property_id} has no lease on record")
    missing = [n for n, v in (("properties.property_type", size.property_type),
                              ("properties.beds", size.beds), ("properties.baths", size.baths),
                              ("properties.sqft", size.sqft), ("properties.city", prop.city),
                              ("properties.zip", prop.zip)) if not v]
    if missing:
        raise ValueError("Fill these in before researching rent: " + ", ".join(missing))
    address = f"{prop.address}{', ' + prop.unit if prop.unit else ''}, {prop.city}, " \
              f"{prop.state} {prop.zip}"
    return Facts(
        property_id=property_id,
        address=address,
        property_type=size.property_type,
        beds=size.beds,
        baths=size.baths,
        sqft=size.sqft,
        current_rent=current.monthly_rent,
        current_term_end=current.end_date,
        year=year or (current.end_date + timedelta(days=1)).year,
        history=db.rent_research_history(conn, property_id),
    )


async def run(
    settings: Settings, *, property_id: int, year: int | None, dry_run: bool
) -> tuple[Research, Path]:
    with db.connect(settings.db_url) as conn:
        facts = load_facts(conn, property_id, year)

    research = analyze(facts, await ask_agents(facts, settings))

    slug = re.sub(r"[^a-z0-9]+", "-", facts.address.split(",")[0].lower()).strip("-")
    # A dry run gets its own file, so it never replaces the saved report.
    name = slug + (DRY_RUN_SUFFIX if dry_run else "")
    report = settings.artifacts_dir / str(facts.year) / "rent-research" / f"{name}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(research, date.today(), dry_run))
    saved = {"property_id": property_id, "year": facts.year,
             "comps": comps_json(research.comps), "recommended_rent": str(research.recommended),
             "rationale": research.rationale}

    if dry_run:
        report.with_suffix(".json").write_text(json.dumps(saved))
    else:
        _save(settings, saved, report)
    return research, report


def _save(settings: Settings, saved: dict[str, Any], report: Path) -> None:
    with db.connect(settings.db_url) as conn:
        db.upsert_rent_research(
            conn,
            property_id=saved["property_id"],
            year=saved["year"],
            comps=saved["comps"],
            recommended_rent=Decimal(saved["recommended_rent"]),
            rationale=saved["rationale"],
            report_path=str(report.resolve().relative_to(settings.artifacts_dir.resolve())),
        )
        conn.commit()


def save_dry_run(settings: Settings, report_rel: str) -> str:
    """Save a dry run's recommendation as if it had run without dry run: the report loses
    its dry-run tag (file name and note) and the recommendation is stored for generate-lease.
    Returns the new report path, relative to artifacts/. Does not research again."""
    root = settings.artifacts_dir.resolve()
    report = (root / report_rel).resolve()
    data_file = report.with_suffix(".json")
    if (not report.is_relative_to(root) or report.parent.name != "rent-research"
            or not report.stem.endswith(DRY_RUN_SUFFIX)):
        raise ValueError("That isn't a dry-run rent report")
    if not report.is_file() or not data_file.is_file():
        raise ValueError("The dry-run report is gone; run research-rent again")
    saved = json.loads(data_file.read_text())
    final = report.with_name(report.stem.removesuffix(DRY_RUN_SUFFIX) + ".md")
    text = report.read_text().replace(DRY_RUN_NOTE + "\n\n", "", 1)
    _save(settings, saved, final)
    final.write_text(text)
    report.unlink()
    data_file.unlink()
    return str(final.relative_to(root))
