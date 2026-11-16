import asyncio
from datetime import date

import pytest

from rental_agent import chat, memory
from rental_agent.config import Settings
from rental_agent.web.app import render_markdown

TODAY = date(2026, 11, 16)
OK = {"date": "2026-11-12", "amount": 54.2, "vendor": "Home Depot", "category": "repairs",
      "property_id": 1, "description": "faucet", "receipt": "chat-uploads/1/r.jpg"}


def test_validate_expense_accepts_good_input():
    data = chat.validate_expense(OK, {1}, {"chat-uploads/1/r.jpg"}, TODAY)
    assert data["amount"] == "54.20" and data["receipt"] == "chat-uploads/1/r.jpg"


@pytest.mark.parametrize("change", [
    {"amount": 0}, {"amount": -3}, {"amount": 250000}, {"amount": "lots"},
    {"date": "2026-12-01"}, {"date": "2023-01-01"}, {"date": "yesterday"},
    {"category": "groceries"}, {"property_id": 9}, {"receipt": "../../etc/passwd"},
])
def test_validate_expense_rejects_bad_input(change):
    with pytest.raises(ValueError):
        chat.validate_expense(OK | change, {1}, {"chat-uploads/1/r.jpg"}, TODAY)


def test_check_upload():
    assert chat.check_upload("r.JPG", b"\xff\xd8\xff\xe0data") == ".jpg"
    assert chat.check_upload("r.pdf", b"%PDF-1.7") == ".pdf"
    for name, data in (("x.exe", b"MZ"), ("x.pdf", b"not a pdf"), ("x.png", b""),
                       ("x.heic", b"....")):
        with pytest.raises(ValueError):
            chat.check_upload(name, data)
    with pytest.raises(ValueError, match="20 MB"):
        chat.check_upload("big.txt", b"x" * (chat.MAX_UPLOAD_BYTES + 1))


def test_content_blocks(tmp_path):
    img, pdf, txt = tmp_path / "a.png", tmp_path / "b.pdf", tmp_path / "c.csv"
    img.write_bytes(b"\x89PNGxx")
    pdf.write_bytes(b"%PDF-1")
    txt.write_text("a,b")
    blocks = chat.content_blocks("Add this", [("u/a.png", img), ("u/b.pdf", pdf), ("u/c.csv", txt)])
    assert [b["type"] for b in blocks] == ["image", "document", "text", "text"]
    assert "u/a.png" in blocks[-1]["text"] and blocks[-1]["text"].startswith("Add this")


def _tools(tmp_path, attachments=frozenset()):
    proposals = []
    settings = Settings("m", "db", tmp_path, tmp_path, tmp_path, "s")
    tools = {t.name: t for t in chat.make_tools(settings, proposals, set(attachments))}
    return tools, proposals


def test_propose_job_and_memory_only_record_cards(tmp_path):
    tools, proposals = _tools(tmp_path)
    r = asyncio.run(tools["propose_job"].handler({"job": "tax-report", "options": {"year": 2026}}))
    assert not r.get("is_error") and "Nothing has happened" in r["content"][0]["text"]
    bad = asyncio.run(tools["propose_job"].handler({"job": "generate-lease", "options": {}}))
    assert bad["is_error"]                       # property_id is required
    asyncio.run(tools["propose_memory"].handler({"rule": "  Always   use Opus  "}))
    assert [(p["kind"], p["status"]) for p in proposals] == [("job", "pending"), ("memory", "pending")]
    assert proposals[1]["data"]["rule"] == "Always use Opus"


def test_memory_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RENTAL_MEMORY_FILE", str(tmp_path / "m" / "memory.md"))
    assert "# Memory" in memory.read()                    # starter created
    memory.append_rule("Receipts from Costco are personal unless I say otherwise.")
    assert "- Receipts from Costco are personal" in memory.read()
    assert "Owner's standing instructions" in memory.prompt_section()
    memory.write("")
    assert memory.prompt_section() == ""


def test_markdown_is_escaped_and_links_are_safe():
    html = render_markdown("**hi** <script>alert(1)</script> [x](javascript:alert(1)) "
                           "[ok](https://example.com) [in](/rent)")
    assert "<strong>hi</strong>" in html
    assert "<script>" not in html and "javascript:" not in html
    assert 'href="https://example.com" rel="noopener noreferrer"' in html
    assert 'href="/rent"' in html


def test_unknown_model_rejected(tmp_path):
    settings = Settings("m", "db", tmp_path, tmp_path, tmp_path, "s")
    with pytest.raises(ValueError):
        asyncio.run(chat.run_turn(settings, model="gpt-x", text="hi", files=[], session_id=None,
                                  history="", on_text=print, on_status=print))


def test_friendly_errors():
    from rental_agent.web.chat_views import _friendly
    for raw in ("ResultError: Claude Code returned an error result: Not logged in · Please run /login",
                "Failed to authenticate", "invalid x-api-key"):
        assert "Settings page" in _friendly(raw)
    assert _friendly("boom").startswith("Something went wrong: boom")
