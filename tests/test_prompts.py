import pytest

from rental_agent import prompts

REQUIRED = ["email-extraction", "rent-recommendation", "comps-researcher",
            "comps-researcher-description"]


@pytest.mark.parametrize("name", REQUIRED)
def test_every_prompt_the_code_uses_loads(name):
    assert prompts.get(name).strip()


def test_placeholders_survive_editing():
    assert "{properties}" in prompts.get("email-extraction")
    prompts.get("email-extraction").format(properties="- id 1: 12 Oak St")  # no stray braces


def test_parser_keeps_inner_code_fences_and_ignores_notes():
    md = "# T\n\n## a\n\nnote\n\n````text\nline 1\n```json\n[]\n```\n````\n\n## b\n\nno block here\n"
    assert prompts.parse(md) == {"a": "line 1\n```json\n[]\n```\n"}


def test_unknown_prompt_name_is_a_clear_error():
    with pytest.raises(KeyError, match="agent-prompts.md"):
        prompts.get("does-not-exist")
