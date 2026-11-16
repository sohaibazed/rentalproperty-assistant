"""Encrypted store for the secrets you enter on the Settings page.

Secrets are encrypted with Fernet (AES-128-CBC + HMAC) using RENTAL_SECRET_KEY, which comes
from your macOS Keychain when the container starts (see `make up`). The encrypted file lives
on the data volume; the key never does. Saved values are never sent back to the browser.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from rental_agent.config import data_dir, env_or_file


@dataclass(frozen=True)
class SecretSpec:
    name: str
    label: str
    help: str
    multiline: bool = False


SECRETS: list[SecretSpec] = [
    SecretSpec("RENTAL_DB_URL", "Supabase database URL",
               "Postgres connection string for the rental_agent role "
               "(Supabase > Project Settings > Database)."),
    SecretSpec("ANTHROPIC_API_KEY", "Anthropic API key",
               "From console.anthropic.com. Set this or the Claude Code token."),
    SecretSpec("CLAUDE_CODE_OAUTH_TOKEN", "Claude Code OAuth token",
               "Run `claude setup-token` on your Mac to get one. Used if no API key is set."),
    SecretSpec("GMAIL_CLIENT_CONFIG", "Gmail OAuth client (JSON)",
               "Google Cloud > APIs & Services > Credentials > OAuth client ID (Desktop app) > "
               "Download JSON, then paste it here.", multiline=True),
]
# Written by the "Connect Gmail" flow, not typed in.
GMAIL_TOKEN = "GMAIL_TOKEN"
ALLOWED = {s.name for s in SECRETS} | {GMAIL_TOKEN}


class SecretStore:
    def __init__(self, path: Path, key: str):
        self.path = path
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError):
            raise RuntimeError("RENTAL_SECRET_KEY is not a valid Fernet key; "
                               "run `make init-keys`") from None

    @classmethod
    def from_env(cls) -> "SecretStore":
        key = env_or_file("RENTAL_SECRET_KEY")
        if not key:
            raise RuntimeError("RENTAL_SECRET_KEY is not set; run `make init-keys`, then `make up`")
        return cls(data_dir() / "secrets.enc", key)

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self._fernet.decrypt(self.path.read_bytes()))
        except InvalidToken:
            raise RuntimeError("Can't decrypt the secrets file: RENTAL_SECRET_KEY changed. "
                               "Restore the old key or delete data/secrets.enc and re-enter.") \
                from None

    def _save(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(self._fernet.encrypt(json.dumps(values).encode()))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def set(self, name: str, value: str) -> None:
        if name not in ALLOWED:
            raise ValueError(f"Unknown secret: {name}")
        value = value.strip()
        if name in ("GMAIL_CLIENT_CONFIG", GMAIL_TOKEN):
            json.loads(value)  # reject pasted text that isn't JSON
        values = self.load()
        values[name] = value
        self._save(values)

    def delete(self, name: str) -> None:
        values = self.load()
        if values.pop(name, None) is not None:
            self._save(values)

    def status(self) -> dict[str, str]:
        """What the UI may show: never the value, only whether it's set (and a short hint)."""
        values = self.load()
        out = {}
        for name in ALLOWED:
            v = values.get(name)
            if not v:
                out[name] = ""
            elif name in ("GMAIL_CLIENT_CONFIG", GMAIL_TOKEN):
                out[name] = "set"
            else:
                out[name] = f"set (ends …{v[-4:]})" if len(v) > 12 else "set"
        return out
