"""Load agent prompts from prompts/agent-prompts.md, one per `## name` heading.

Each section's prompt is the first ````text ... ```` block under its heading
(four backticks, so prompts can contain ``` themselves).
"""

import re
from functools import cache
from pathlib import Path

PROMPTS_FILE = Path(__file__).resolve().parents[2] / "prompts" / "agent-prompts.md"

_SECTION = re.compile(r"^## (?P<name>\S+)\s*$", re.MULTILINE)
_BLOCK = re.compile(r"^````text\n(?P<body>.*?)\n````\s*$", re.MULTILINE | re.DOTALL)


def parse(markdown: str) -> dict[str, str]:
    heads = list(_SECTION.finditer(markdown))
    prompts: dict[str, str] = {}
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(markdown)
        block = _BLOCK.search(markdown, head.end(), end)
        if block:
            prompts[head["name"]] = block["body"] + "\n"
    return prompts


@cache
def _all(path: Path = PROMPTS_FILE) -> dict[str, str]:
    return parse(path.read_text())


def get(name: str) -> str:
    try:
        return _all()[name]
    except KeyError:
        raise KeyError(f"No prompt named {name!r} in {PROMPTS_FILE}") from None
