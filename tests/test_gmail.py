import base64
from datetime import date

import pytest

from rental_agent.config import EmailRule, EmailSources, load_email_sources
from rental_agent.gmail import build_query, parse_message


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_build_query():
    q = build_query(EmailSources(["bank@x.com"], ["hd@y.com"], ["receipt"]), date(2026, 11, 1))
    assert q == '(from:bank@x.com OR from:hd@y.com OR subject:"receipt") after:2026/11/01'


def test_build_query_with_a_narrow_rule():
    rule = EmailRule(["me@x.com"], ["eStatement", "Willow Crest"])
    q = build_query(EmailSources(["bank@x.com"], [], [], [rule]), date(2026, 11, 1))
    assert q == ('(from:bank@x.com OR ((from:me@x.com) subject:"eStatement" '
                 'subject:"Willow Crest")) after:2026/11/01')


def test_extra_sections_load_as_rules_and_must_be_complete(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text('[rent]\nsenders = ["bank@x.com"]\n'
                    '[hoa]\nsenders = ["me@x.com"]\nsubject_all = ["eStatement"]\n')
    assert load_email_sources(path).rules == [EmailRule(["me@x.com"], ["eStatement"])]
    path.write_text('[hoa]\nsenders = ["me@x.com"]\n')      # would match all mail from me
    with pytest.raises(ValueError, match="needs both"):
        load_email_sources(path)


def test_parse_prefers_plain_text_and_finds_attachments():
    raw = {
        "id": "m1",
        "internalDate": "0",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Bank <bank@x.com>"},
                {"name": "Subject", "value": "Deposit received"},
                {"name": "Date", "value": "Tue, 10 Nov 2026 09:00:00 -0500"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("You received $1,800.00")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>ignored</p>")}},
                {"mimeType": "application/pdf", "filename": "r.pdf", "body": {"attachmentId": "a1"}},
            ],
        },
    }
    msg = parse_message(raw)
    assert msg.body == "You received $1,800.00"
    assert msg.sent_at.date() == date(2026, 11, 10)
    assert [a.attachment_id for a in msg.attachments] == ["a1"]


def test_parse_html_only():
    raw = {
        "id": "m2",
        "internalDate": "1794000000000",
        "payload": {
            "mimeType": "text/html",
            "headers": [],
            "body": {"data": _b64("<style>x{}</style><p>Total:&nbsp;$42.10</p>")},
        },
    }
    assert "Total:" in parse_message(raw).body
    assert "$42.10" in parse_message(raw).body
