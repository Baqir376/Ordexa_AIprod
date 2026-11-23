"""Runtime configuration. Every value can be set with an ``ORDEXA_<NAME>`` environment variable."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import field_validator
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
    # Run the ai/channels/outbox workers as threads inside the API process. For single-process hosting
    # (free tiers); with separate worker processes (docker-compose) leave it off.
    embedded_workers: bool = False
    # AI order understanding. "auto" = Claude if an Anthropic key is set, else Groq (free tier) if GROQ_API_KEY is
    # set, else the built-in rule parser.
    ai_provider: str = "auto"  # auto | anthropic | groq | rules
    ai_model: str = "claude-opus-5-5"
    groq_model: str = "openai/gpt-oss-120b"  # text orders
    groq_vision_model: str = "qwen/qwen3.8-27b"  # photo orders (Groq preview model; override if retired)
    groq_stt_model: str = "whisper-large-v3"  # voice notes
    ai_inline: bool = False  # process AI jobs inside the API request (single-process dev); production uses the worker
    # WhatsApp Cloud API (outbound)
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_tenant_code: str = ""  # which business this WhatsApp number belongs to
    whatsapp_api_base: str = "https://graph.facebook.com/v21.0"
    # Login protection: failed attempts per business+username within the window before a temporary lock.
    login_max_failures: int = 10
    login_window_minutes: int = 15

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        """Hosted Postgres providers (Neon, Render, Supabase...) hand out postgres:// URLs; use the psycopg 3 driver."""
        v = (v or "").strip()
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v


@lru_cache
def get_settings() -> Settings:
    # Common hosting convention: a plain DATABASE_URL is used when ORDEXA_DATABASE_URL is not set.
    if not os.environ.get("ORDEXA_DATABASE_URL") and os.environ.get("DATABASE_URL"):
        s = Settings(database_url=os.environ["DATABASE_URL"])
    else:
        s = Settings()
    if s.environment == "production":
        if s.jwt_secret.startswith("dev-only") or len(s.jwt_secret) < 32:
            raise RuntimeError("ORDEXA_JWT_SECRET must be set to a random value of at least 32 characters in production")
    return s
