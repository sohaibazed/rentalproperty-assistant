"""Load agent prompts from prompts/agent-prompts.md, one per `## name` heading.

Each section's prompt is the first ````text ... ```` block under its heading
(four backticks, so prompts can contain ``` themselves).

The file is read on every `get`, so edits on the web Prompts page apply to the next chat or
job run without a restart.
"""

import os
import re
from pathlib import Path
from string import Formatter

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "prompts" / "agent-prompts.md"

_SECTION = re.compile(r"^## (?P<name>\S+)\s*$", re.MULTILINE)
_BLOCK = re.compile(r"^````text\n(?P<body>.*?)\n````\s*$", re.MULTILINE | re.DOTALL)


def path() -> Path:
    return Path(os.environ.get("RENTAL_PROMPTS_FILE", DEFAULT_FILE)).expanduser().resolve()


def parse(markdown: str) -> dict[str, str]:
    heads = list(_SECTION.finditer(markdown))
    prompts: dict[str, str] = {}
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(markdown)
        block = _BLOCK.search(markdown, head.end(), end)
        if block:
            prompts[head["name"]] = block["body"] + "\n"
    return prompts


def read() -> str:
    return path().read_text()


def get(name: str) -> str:
    try:
        return parse(read())[name]
    except KeyError:
        raise KeyError(f"No prompt named {name!r} in {path()}") from None


def _placeholders(text: str) -> set[str] | None:
    """The {name} fields in a prompt, or None if its braces don't parse."""
    try:
        return {field for _, field, _, _ in Formatter().parse(text) if field is not None}
    except ValueError:
        return None


def write(text: str) -> None:
    """Save a new version of the file after checking it against the current one: every
    prompt must still load and keep exactly the same {placeholders}. Raises ValueError."""
    text = text.replace("\r\n", "\n")
    old, new = parse(read()), parse(text)
    missing = sorted(old.keys() - new.keys())
    if missing:
        raise ValueError("These prompts are missing or no longer in a ````text block: "
                         + ", ".join(missing))
    for name, body in old.items():
        before, after = _placeholders(body), _placeholders(new[name])
        if after != before:
            want = ", ".join(f"{{{f}}}" for f in sorted(before or ())) or "none"
            raise ValueError(f"Prompt {name!r}: placeholders must stay the same ({want}). "
                             "Use {{ and }} for a literal brace.")
    p = path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(p)
