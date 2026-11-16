"""memory.md: the owner's standing rules, added to every chat and every agent run.

A plain Markdown file on the host (memory/memory.md), editable on the web Memory page or in
any editor. The chat can only *propose* additions; the owner confirms them.
"""

import os
from datetime import date
from pathlib import Path

STARTER = """# Memory

Rules and instructions for the rental agent. Every chat and every job reads this file.
Write one rule per bullet. Keep anything you wouldn't want sent to Claude out of here.

## Rules

- Property: 8705 Empire Blvd, Plano, TX 75024 (Collin County).
- Ask before assuming which property an expense belongs to.
"""

MAX_CHARS = 20_000   # keep prompts bounded; the Memory page warns when it's longer


def path() -> Path:
    return Path(os.environ.get("RENTAL_MEMORY_FILE", "./memory/memory.md")).expanduser().resolve()


def read() -> str:
    p = path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(STARTER)
    return p.read_text()


def write(text: str) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text.replace("\r\n", "\n"))
    tmp.replace(p)


def append_rule(rule: str) -> None:
    rule = " ".join(rule.split())
    if not rule:
        raise ValueError("Empty rule")
    text = read().rstrip("\n")
    write(f"{text}\n- {rule} _(added {date.today():%b %-d, %Y})_\n")


def prompt_section() -> str:
    """Memory as a block to append to a system prompt (empty if there are no rules)."""
    text = read().strip()
    if not text:
        return ""
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[memory.md truncated; shorten it on the Memory page]"
    return ("\n\n## Owner's standing instructions (memory.md)\n"
            "Follow these unless they conflict with the rules above or with safety.\n\n" + text + "\n")
