"""Runtime configuration. Every value can be set with an ``ORDEXA_<NAME>`` environment variable."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORDEXA_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ordexa:ordexa@localhost:5432/ordexa"
    jwt_secret: str = "dev-only-change-me-dev-only-change-me"  # >= 32 bytes; MUST be overridden in production
    jwt_ttl_minutes: int = 12 * 60  # one shop shift; desktop re-authenticates after that
    environment: str = "development"
    # WhatsApp webhook (Phase 3). Empty = webhook rejects everything.
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    worker_poll_seconds: float = 1.0
    # AI order understanding. "auto" = Claude when an Anthropic credential is configured, else the rule parser.
    ai_provider: str = "auto"  # auto | anthropic | rules
    ai_model: str = "claude-opus-5-5"
    ai_inline: bool = False  # process AI jobs inside the API request (single-process dev); production uses the worker
    # WhatsApp Cloud API (outbound)
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_tenant_code: str = ""  # which business this WhatsApp number belongs to
    whatsapp_api_base: str = "https://graph.facebook.com/v21.0"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.environment == "production" and s.jwt_secret.startswith("dev-only"):
        raise RuntimeError("ORDEXA_JWT_SECRET must be set in production")
    return s
