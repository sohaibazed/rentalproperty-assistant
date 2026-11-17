import pytest

from rental_agent import prompts

REQUIRED = ["email-extraction", "rent-recommendation", "comps-researcher",
            "comps-researcher-description", "protest-opinion", "sales-researcher",
            "sales-researcher-description"]


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


MD = "# T\n\n## a\n\n````text\nHello {name}.\n````\n\n## b\n\n````text\nPlain.\n````\n"


@pytest.fixture
def prompts_file(tmp_path, monkeypatch):
    f = tmp_path / "agent-prompts.md"
    f.write_text(MD)
    monkeypatch.setenv("RENTAL_PROMPTS_FILE", str(f))
    return f


def test_saved_edits_apply_on_next_get(prompts_file):
    prompts.write(MD.replace("Plain.", "Edited, with {{braces}}.").replace("\n", "\r\n"))
    assert prompts.get("b") == "Edited, with {{braces}}.\n"
    assert "\r" not in prompts_file.read_text()


@pytest.mark.parametrize("edit,error", [
    (lambda md: md.replace("## b", "## renamed"), "missing or no longer in a ````text block: b"),
    (lambda md: md.replace("````text\nPlain.\n````", "Plain."), "missing"),
    (lambda md: md.replace("{name}", "you"), r"'a': placeholders must stay the same \(\{name\}\)"),
    (lambda md: md.replace("Plain.", "Hi {who}"), r"'b': placeholders must stay the same \(none\)"),
    (lambda md: md.replace("Plain.", "a } b"), "'b': placeholders"),
])
def test_bad_edits_are_refused_and_file_unchanged(prompts_file, edit, error):
    with pytest.raises(ValueError, match=error):
        prompts.write(edit(MD))
    assert prompts_file.read_text() == MD
