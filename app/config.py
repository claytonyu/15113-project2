"""Environment-driven configuration.

Everything that differs between a local run and a deployed run comes from environment
variables (loaded from `.env` locally). Only DATABASE_URL is needed to start locally; Google
settings are optional and the Google endpoints report "not configured" without them.

Mistakes fail at startup with a message naming the variable (never its value).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """The environment is misconfigured. The message names the variable(s) involved."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


@dataclass(frozen=True)
class Config:
    environment: str
    database_url: str | None
    frontend_url: str
    backend_url: str
    google_client_id: str | None
    google_client_secret: str | None
    google_redirect_uri: str
    token_encryption_key: str | None
    schedule_rate_limit: str
    auth_rate_limit: str
    schedule_timeout_s: float
    trusted_proxy_hops: int

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def frontend_origin(self) -> str:
        """scheme://host[:port] of the frontend, used for the CORS allowlist."""
        parts = urlsplit(self.frontend_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.token_encryption_key)

    @property
    def secure_cookies(self) -> bool:
        return self.backend_url.startswith("https://")


def _validate_environment() -> None:
    """Checks that need the raw environment (before defaults are applied)."""
    problems: list[str] = []

    if (_env("ENVIRONMENT", "local") or "local").lower() == "production":
        for name in ("DATABASE_URL", "FRONTEND_ORIGIN"):
            if not _env(name):
                problems.append(f"{name} must be set when ENVIRONMENT=production")
        if not (_env("BACKEND_URL") or _env("GOOGLE_REDIRECT_URI")):
            problems.append("BACKEND_URL (or GOOGLE_REDIRECT_URI) must be set when ENVIRONMENT=production")

    key = _env("TOKEN_ENCRYPTION_KEY")
    if key:
        try:
            Fernet(key.encode())
        except (ValueError, TypeError):
            problems.append("TOKEN_ENCRYPTION_KEY is not a valid Fernet key")

    has_id, has_secret = bool(_env("GOOGLE_CLIENT_ID")), bool(_env("GOOGLE_CLIENT_SECRET"))
    if has_id != has_secret:
        missing = "GOOGLE_CLIENT_SECRET" if has_id else "GOOGLE_CLIENT_ID"
        problems.append(f"{missing} must be set together with the other Google client setting")
    elif has_id and not key:
        problems.append("TOKEN_ENCRYPTION_KEY must be set when Google login is configured")

    for name, kind in (("SCHEDULE_TIMEOUT_S", float), ("TRUSTED_PROXY_HOPS", int)):
        raw = _env(name)
        if raw is not None:
            try:
                if kind(raw) < 0:
                    raise ValueError
            except ValueError:
                problems.append(f"{name} must be a non-negative {'number' if kind is float else 'whole number'}")

    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))


@lru_cache
def get_config() -> Config:
    _validate_environment()
    backend_url = (_env("BACKEND_URL", "http://localhost:8000") or "").rstrip("/")
    return Config(
        environment=(_env("ENVIRONMENT", "local") or "local").lower(),
        database_url=_env("DATABASE_URL"),
        frontend_url=(_env("FRONTEND_ORIGIN", "http://localhost:5500") or "").rstrip("/"),
        backend_url=backend_url,
        google_client_id=_env("GOOGLE_CLIENT_ID"),
        google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
        google_redirect_uri=_env("GOOGLE_REDIRECT_URI", f"{backend_url}/auth/google/callback") or "",
        token_encryption_key=_env("TOKEN_ENCRYPTION_KEY"),
        schedule_rate_limit=_env("SCHEDULE_RATE_LIMIT", "30/minute") or "30/minute",
        auth_rate_limit=_env("AUTH_RATE_LIMIT", "20/minute") or "20/minute",
        schedule_timeout_s=float(_env("SCHEDULE_TIMEOUT_S", "10") or "10"),
        trusted_proxy_hops=int(_env("TRUSTED_PROXY_HOPS", "1") or "1"),
    )
