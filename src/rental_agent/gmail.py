"""Read-only Gmail access: OAuth, search, message parsing, attachments."""

import base64
import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build

from rental_agent.config import EmailSources

# Read-only: the agent can never send, delete, or modify mail.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

RECEIPT_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/heic"}


@dataclass
class Attachment:
    attachment_id: str
    filename: str
    mime_type: str


@dataclass
class EmailMessage:
    message_id: str
    sender: str
    subject: str
    sent_at: datetime
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def authorize_interactive(client_secrets: Path, token_path: Path) -> None:
    """One-time browser consent from a terminal (CLI). The web app uses start/finish_web_flow."""
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)


def start_web_flow(client_config_json: str, redirect_uri: str) -> tuple[str, str, Flow]:
    """Begin Google consent in the browser. Returns (url, state, flow); keep the flow, keyed
    by state, for finish_web_flow."""
    flow = Flow.from_client_config(json.loads(client_config_json), SCOPES, redirect_uri=redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    return url, state, flow


def finish_web_flow(flow: Flow, authorization_response: str) -> str:
    """Exchange the callback URL for a token; returns authorized-user JSON (has a refresh token)."""
    flow.fetch_token(authorization_response=authorization_response)
    return flow.credentials.to_json()


def get_service(token_json: str | None) -> Any:
    """Build a Gmail client. The access token is refreshed in memory on every run."""
    if not token_json:
        raise RuntimeError("Gmail is not connected. Use Settings > Connect Gmail "
                           "(or `make gmail-auth` for the CLI).")
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("Gmail token is invalid. Connect Gmail again.")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_query(sources: EmailSources, since: date) -> str:
    """Gmail search query covering rent senders, expense senders, and expense subject keywords."""
    terms = [f"from:{s}" for s in sources.rent_senders + sources.expense_senders]
    terms += [f'subject:"{k}"' for k in sources.expense_subject_keywords]
    if not terms:
        raise ValueError("email_sources.toml lists no senders or keywords")
    return f"({' OR '.join(terms)}) after:{since:%Y/%m/%d}"


def search_message_ids(service: Any, query: str) -> list[str]:
    ids: list[str] = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token, maxResults=500)
            .execute()
        )
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids


def get_message(service: Any, message_id: str) -> EmailMessage:
    raw = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    return parse_message(raw)


def get_attachment_bytes(service: Any, message_id: str, attachment_id: str) -> bytes:
    resp = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    return base64.urlsafe_b64decode(resp["data"])


def parse_message(raw: dict[str, Any]) -> EmailMessage:
    payload = raw["payload"]
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    if "date" in headers:
        sent_at = parsedate_to_datetime(headers["date"])
    else:
        sent_at = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=UTC)

    plain: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []
    _walk(payload, plain, html_parts, attachments)
    body = "\n".join(plain) if plain else _html_to_text("\n".join(html_parts))

    return EmailMessage(
        message_id=raw["id"],
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        sent_at=sent_at,
        body=body.strip(),
        attachments=attachments,
    )


def _walk(
    part: dict[str, Any], plain: list[str], html_parts: list[str], attachments: list[Attachment]
) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body", {})
    if part.get("filename") and body.get("attachmentId"):
        attachments.append(Attachment(body["attachmentId"], part["filename"], mime))
    elif mime == "text/plain" and body.get("data"):
        plain.append(_decode(body["data"]))
    elif mime == "text/html" and body.get("data"):
        html_parts.append(_decode(body["data"]))
    for sub in part.get("parts", []):
        _walk(sub, plain, html_parts, attachments)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text))
