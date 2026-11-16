"""Runtime configuration.

Values come from environment variables, overridden by the web app's encrypted secrets store
when one is passed in. The CLI uses environment variables and files only.
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Secret names shared by the environment and the web app's secrets store.
CLAUDE_SECRET_NAMES = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def env_or_file(name: str) -> str | None:
    """Read NAME from the environment, or from the file named by NAME_FILE (Docker secrets)."""
    if value := os.environ.get(name):
        return value
    if path := os.environ.get(f"{name}_FILE"):
        return Path(path).read_text().strip() or None
    return None


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


def artifacts_dir() -> Path:
    return _path("RENTAL_ARTIFACTS_DIR", "./artifacts")


def gmail_client_secrets_path() -> Path:
    return _path("GMAIL_CLIENT_SECRETS", "~/.config/rental-agent/gmail_client.json")


def gmail_token_path() -> Path:
    return _path("GMAIL_TOKEN_PATH", "~/.config/rental-agent/gmail.token.json")


@dataclass(frozen=True)
class Settings:
    model: str
    db_url: str
    artifacts_dir: Path
    reference_dir: Path
    email_sources_path: Path
    soffice_bin: str
    claude_env: dict[str, str] = field(default_factory=dict)   # passed to the Claude process
    gmail_token: str | None = None                             # authorized-user JSON


def load_settings(secrets: Mapping[str, str] | None = None) -> Settings:
    secrets = secrets or {}

    def value(name: str) -> str | None:
        return secrets.get(name) or os.environ.get(name) or None

    db_url = value("RENTAL_DB_URL")
    if not db_url:
        raise RuntimeError("RENTAL_DB_URL is not set (environment variable or web Settings page)")

    gmail_token = secrets.get("GMAIL_TOKEN")
    if gmail_token is None and gmail_token_path().exists():
        gmail_token = gmail_token_path().read_text()

    return Settings(
        model=os.environ.get("RENTAL_AGENT_MODEL", "claude-opus-5-5"),
        db_url=db_url,
        artifacts_dir=artifacts_dir(),
        reference_dir=_path("RENTAL_REFERENCE_DIR", "./reference"),
        email_sources_path=_path("RENTAL_EMAIL_SOURCES", "./config/email_sources.toml"),
        soffice_bin=os.environ.get("SOFFICE_BIN", "soffice"),
        claude_env={n: v for n in CLAUDE_SECRET_NAMES if (v := value(n))},
        gmail_token=gmail_token,
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
