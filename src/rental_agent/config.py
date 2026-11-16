"""Runtime configuration, read from environment variables and config/email_sources.toml."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name} (see .env.example)")
    return value


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    model: str
    db_url: str
    artifacts_dir: Path
    reference_dir: Path
    gmail_client_secrets: Path
    gmail_token_path: Path
    email_sources_path: Path
    soffice_bin: str


def load_settings() -> Settings:
    return Settings(
        model=os.environ.get("RENTAL_AGENT_MODEL", "claude-opus-5-5"),
        db_url=_required("RENTAL_DB_URL"),
        artifacts_dir=_path("RENTAL_ARTIFACTS_DIR", "./artifacts"),
        reference_dir=_path("RENTAL_REFERENCE_DIR", "./reference"),
        gmail_client_secrets=_path(
            "GMAIL_CLIENT_SECRETS", "~/.config/rental-agent/gmail_client.json"
        ),
        gmail_token_path=_path("GMAIL_TOKEN_PATH", "~/.config/rental-agent/gmail.token.json"),
        email_sources_path=_path("RENTAL_EMAIL_SOURCES", "./config/email_sources.toml"),
        soffice_bin=os.environ.get("SOFFICE_BIN", "soffice"),
    )


@dataclass(frozen=True)
class EmailSources:
    rent_senders: list[str]
    expense_senders: list[str]
    expense_subject_keywords: list[str]


def load_email_sources(path: Path) -> EmailSources:
    data = tomllib.loads(path.read_text())
    return EmailSources(
        rent_senders=data.get("rent", {}).get("senders", []),
        expense_senders=data.get("expenses", {}).get("senders", []),
        expense_subject_keywords=data.get("expenses", {}).get("subject_keywords", []),
    )
