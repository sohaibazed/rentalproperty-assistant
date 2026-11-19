import base64
from datetime import UTC, date, datetime

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


def _pdf(text: str) -> bytes:
    """A one-page PDF with one line of text."""
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_read_pdfs_adds_pdf_text_and_skips_what_it_cannot_read(monkeypatch):
    from rental_agent import gmail
    from rental_agent.extract import _format_email
    from rental_agent.gmail import Attachment, EmailMessage

    files = {"a": _pdf("Amount due 250.00"), "b": b"not a pdf", "c": b"photo"}
    monkeypatch.setattr(gmail, "get_attachment_bytes", lambda _s, _m, att_id: files[att_id])
    msg = EmailMessage("m1", "hoa@x.com", "eStatement", datetime(2026, 11, 10, tzinfo=UTC),
                       "Statement attached.",
                       [Attachment("a", "eStatement.pdf", "application/pdf"),
                        Attachment("b", "broken.pdf", "application/pdf"),
                        Attachment("c", "photo.png", "image/png")])
    gmail.read_pdfs(None, msg)
    assert msg.pdf_text == {"eStatement.pdf": "Amount due 250.00"}
    assert _format_email(msg).endswith(
        '</email>\n<attachment name="eStatement.pdf">\nAmount due 250.00\n</attachment>')
