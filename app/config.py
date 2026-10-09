from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, List

from pydantic import BeforeValidator, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"


def _clean_str(value: object) -> str:
    s = "" if value is None else str(value).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def normalize_pg_base_url(raw: str) -> str:
    """Normalize PG panel URL while preserving path.

    Trims whitespace, adds https:// if scheme is missing, drops query/fragment,
    strips userinfo (credentials must not live in the URL), keeps
    scheme://host[:port]/path (trailing slash removed).
    """
    from urllib.parse import urlparse, urlunparse

    s = (raw or "").strip().rstrip("/")
    if not s:
        return s
    if "://" not in s:
        s = "https://" + s
    parsed = urlparse(s)
    if not parsed.scheme or not parsed.netloc:
        return s
    # Reject credential embedding — prevents accidental secret leakage via logs/SSRF
    host = parsed.hostname or ""
    if not host:
        return s
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    path = (parsed.path or "").rstrip("/")
    return urlunparse((parsed.scheme, netloc, path, "", "", "")).rstrip("/")


def pg_api_base_candidates(raw: str) -> list[str]:
    """Candidate API roots for PasarGuard.

    Dashboard path (e.g. /MrClock, /dashboard) is UI-only — API usually lives at
    the domain root. Prefer the configured URL first, then the origin without path.
    """
    from urllib.parse import urlparse, urlunparse

    base = normalize_pg_base_url(raw)
    if not base:
        return []
    out = [base]
    parsed = urlparse(base)
    path = (parsed.path or "").rstrip("/")
    if path and path != "/":
        origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")
        if origin and origin not in out:
            out.append(origin)
    return out


def normalize_pg_subscription_path(raw: str | None) -> str:
    """Canonical subscription prefix; paths only, never an origin or query."""
    import re

    value = (raw or "").strip() or "/sub"
    if len(value) > 128 or not re.fullmatch(r"/?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?", value):
        raise ValueError("مسیر سابسکریپشن نامعتبر است؛ فقط مسیر مثل /sub/ یا /apilog/ را وارد کنید")
    return "/" + value.strip("/")


def _parse_admin_ids(value: object) -> List[int]:
    """Parse ADMIN_IDS from env/.env without requiring JSON (empty string → [])."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [int(v) for v in value]
    cleaned = _clean_str(value)
    if not cleaned:
        return []
    out: list[int] = []
    for part in cleaned.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    bot_username: str = Field(default="", alias="BOT_USERNAME")
    # NoDecode: prevent pydantic-settings from json.loads("") on empty ADMIN_IDS
    admin_ids: Annotated[List[int], NoDecode, BeforeValidator(_parse_admin_ids)] = Field(
        default_factory=list,
        alias="ADMIN_IDS",
    )

    pg_base_url: str = Field(default="", alias="PG_BASE_URL")
    pg_subscription_path: str = Field(default="/sub", alias="PG_SUBSCRIPTION_PATH")
    pg_username: str = Field(default="", alias="PG_USERNAME")
    pg_password: str = Field(default="", alias="PG_PASSWORD")
    # Admin API key for the connected PG identity (X-Api-Key). Preferred over password.
    pg_api_key: str = Field(default="", alias="PG_API_KEY")
    pg_access_token: str = Field(default="", alias="PG_ACCESS_TOKEN")

    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=9000, alias="WEB_PORT")
    web_secret: str = Field(default="change-me", alias="WEB_SECRET")
    web_admin_user: str = Field(default="admin", alias="WEB_ADMIN_USER")
    web_admin_password: str = Field(default="", alias="WEB_ADMIN_PASSWORD")

    # Installs always provision local PostgreSQL (or honor PGCLOCK_DATABASE_URL).
    # SQLite remains only as an in-memory option for unit tests — not for product installs.
    database_url: str = Field(
        default=f"sqlite+aiosqlite:///{DATA_DIR / 'bot.db'}",
        alias="DATABASE_URL",
    )

    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    webhook_path: str = Field(default="/telegram/webhook", alias="WEBHOOK_PATH")
    webhook_secret_token: str = Field(default="", alias="WEBHOOK_SECRET_TOKEN")
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
    # Optional public docs site (e.g. https://docs.example.com). Empty = same-origin /help/
    docs_base_url: str = Field(default="", alias="DOCS_BASE_URL")
    # When 1, trust X-Forwarded-For / X-Forwarded-Proto (only behind a real reverse proxy)
    trust_proxy: bool = Field(default=False, alias="TRUST_PROXY")
    # Number of reverse-proxy hops in front of this app that are trusted to append
    # their own hop to X-Forwarded-For. The client IP is read from the Nth entry
    # counted from the RIGHT (the entry appended by your own proxy), never the
    # left-most entry (which is fully attacker-controlled). Default is a single
    # reverse proxy (e.g. nginx) directly in front of the app.
    trust_proxy_hops: int = Field(default=1, alias="TRUST_PROXY_HOPS")

    currency: str = Field(default="تومان", alias="CURRENCY")
    default_locale: str = Field(default="fa", alias="DEFAULT_LOCALE")
    # Preferred update channel for checks/deploy (dropdown). Not the header badge.
    update_channel: str = Field(default="main", alias="UPDATE_CHANNEL")
    # Actually installed/running channel tip — header badge reads this only.
    deployed_channel: str = Field(default="main", alias="DEPLOYED_CHANNEL")

    allow_settlement_mock: bool = Field(default=False, alias="ALLOW_SETTLEMENT_MOCK")

    @field_validator(
        "bot_token",
        "bot_username",
        "pg_base_url",
        "pg_subscription_path",
        "pg_username",
        "pg_password",
        "pg_api_key",
        "pg_access_token",
        "web_host",
        "web_secret",
        "web_admin_user",
        "web_admin_password",
        "webhook_url",
        "webhook_path",
        "webhook_secret_token",
        "public_base_url",
        "docs_base_url",
        "currency",
        "default_locale",
        "database_url",
        "update_channel",
        "deployed_channel",
        mode="before",
    )
    @classmethod
    def strip_wrap_quotes(cls, value: object) -> str:
        return _clean_str(value)

    @field_validator("update_channel", "deployed_channel", mode="after")
    @classmethod
    def normalize_update_channel(cls, value: str) -> str:
        from app.services.update_channel import normalize_channel

        return normalize_channel(value)

    @field_validator("trust_proxy", "allow_settlement_mock", mode="before")
    @classmethod
    def parse_trust_proxy(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        s = _clean_str(value).lower()
        return s in {"1", "true", "yes", "on"}

    @field_validator("trust_proxy_hops", mode="before")
    @classmethod
    def parse_trust_proxy_hops(cls, value: object) -> int:
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            return 1
        return max(1, min(10, n))

    @field_validator("pg_base_url", mode="after")
    @classmethod
    def normalize_pg_url(cls, value: str) -> str:
        return normalize_pg_base_url(value) or value

    @field_validator("pg_subscription_path", mode="after")
    @classmethod
    def normalize_sub_path(cls, value: str) -> str:
        return normalize_pg_subscription_path(value)

    def _miniapp_https_base(self) -> str:
        """Canonical HTTPS origin for Telegram WebApp buttons.

        Prefer the live panel HTTPS URL (cert meta) when TLS is on — same source
        as ``public_panel_base_url``. Fall back to ``PUBLIC_BASE_URL`` only when
        it is already ``https://…``. Never return ``http://`` (Telegram rejects it).
        """
        try:
            from app.services.ssl_certs import https_is_active, public_panel_base_url

            if https_is_active():
                live = (public_panel_base_url() or "").strip().rstrip("/")
                if live.startswith("https://"):
                    return live
        except Exception:
            pass
        configured = (self.public_base_url or "").strip().rstrip("/")
        if configured.startswith("https://"):
            return configured
        return ""

    @property
    def miniapp_enabled(self) -> bool:
        """True only when a real HTTPS Mini App origin is available."""
        return bool(self._miniapp_https_base())

    @property
    def miniapp_url(self) -> str:
        base = self._miniapp_https_base()
        return f"{base}/miniapp/" if base else ""

    def miniapp_deep_url(self, view: str = "") -> str:
        """HTTPS Mini App URL with optional hash deep-link (e.g. ops, services)."""
        base = self.miniapp_url
        if not base:
            return ""
        view = (view or "").strip().lstrip("#")
        return f"{base}#{view}" if view else base


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()  # type: ignore[call-arg]
