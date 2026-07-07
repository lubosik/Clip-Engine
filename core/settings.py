"""
core/settings.py — environment variable loading via pydantic-settings.

All vars are optional at import time so the module can be imported in tests and
config-only validation runs without requiring infra credentials.

Call the appropriate require_*() method at the point of use — it raises a clear
RuntimeError when the var is absent, naming exactly which var is missing and
which operation needs it.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Apify — required for discovery, transcripts, comments
    apify_token: str | None = Field(default=None, alias="APIFY_TOKEN")

    # Postgres — required for all DB operations
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    # Postiz — required for scheduling
    postiz_api_url: str | None = Field(default=None, alias="POSTIZ_API_URL")
    postiz_api_key: str | None = Field(default=None, alias="POSTIZ_API_KEY")

    # LLM (Anthropic Messages API) — required for ranking.
    # LLM_BASE_URL is optional: leave unset for api.anthropic.com. OpenRouter
    # keys (sk-or-...) are auto-routed to https://openrouter.ai/api, which
    # serves an Anthropic-compatible /v1/messages endpoint.
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_model: str | None = Field(default=None, alias="LLM_MODEL")
    llm_base_url: str | None = Field(default=None, alias="LLM_BASE_URL")

    # Storage — defaults to /data/clips; required for any file I/O
    storage_dir: str = Field(default="/data/clips", alias="STORAGE_DIR")

    # Web admin password — required when running the web service
    web_admin_password: str | None = Field(default=None, alias="WEB_ADMIN_PASSWORD")

    # Timezone
    tz: str = Field(default="America/New_York", alias="TZ")

    # ------------------------------------------------------------------ #
    # Lazy requirement checkers — call these at the point of use.         #
    # ------------------------------------------------------------------ #

    def require_apify(self) -> str:
        if not self.apify_token:
            raise RuntimeError(
                "APIFY_TOKEN environment variable is required for Apify actor calls "
                "(discovery, transcripts, comments). Set it in your .env or Railway vars."
            )
        return self.apify_token

    def require_database(self) -> str:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL environment variable is required for database operations. "
                "Set it to a valid PostgreSQL connection string."
            )
        return self.database_url

    def require_postiz(self) -> tuple[str, str]:
        missing = []
        if not self.postiz_api_url:
            missing.append("POSTIZ_API_URL")
        if not self.postiz_api_key:
            missing.append("POSTIZ_API_KEY")
        if missing:
            raise RuntimeError(
                f"Missing required env vars for Postiz scheduling: {', '.join(missing)}. "
                "Set them in your .env or Railway vars."
            )
        return self.postiz_api_url, self.postiz_api_key  # type: ignore[return-value]

    def require_llm(self) -> tuple[str, str]:
        missing = []
        if not self.llm_api_key:
            missing.append("LLM_API_KEY")
        if not self.llm_model:
            missing.append("LLM_MODEL")
        if missing:
            raise RuntimeError(
                f"Missing required env vars for LLM ranking: {', '.join(missing)}. "
                "Set them in your .env or Railway vars."
            )
        return self.llm_api_key, self.llm_model  # type: ignore[return-value]

    def require_web_password(self) -> str:
        if not self.web_admin_password:
            raise RuntimeError(
                "WEB_ADMIN_PASSWORD environment variable is required to run the web service. "
                "Set it in your .env or Railway vars."
            )
        return self.web_admin_password


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance. Safe to call repeatedly."""
    return Settings()
