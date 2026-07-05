"""Application settings, loaded from environment / .env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "sqlite:///./data/prices.db"

    # HTTP fetching. A real-browser UA maximizes success on the lightweight
    # path (many sites reject obvious bots); override via USER_AGENT if desired.
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
    request_timeout_seconds: float = 20.0

    # OpenRouter / LLM fallback
    openrouter_api_key: str = ""
    # A cheap PAID model by default: free-tier models have inconsistent
    # availability and weak parsing. At fallback-only volume this costs pennies.
    openrouter_model: str = "google/gemini-2.5-flash-lite"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_extraction_enabled: bool = True
    llm_max_input_chars: int = 8000
    llm_monthly_call_cap: int = 500

    # Scheduler
    # How often the background sweep looks for items whose check is due.
    scheduler_interval_seconds: int = 300
    # Default per-item check cadence when the user doesn't specify one.
    default_check_interval_minutes: int = 1440

    # Optional HTTP basic auth for the dashboard/API (for public exposure).
    # Auth is enforced only when BOTH are set; empty => open (local dev).
    basic_auth_user: str = ""
    basic_auth_pass: str = ""

    @property
    def basic_auth_enabled(self) -> bool:
        return bool(self.basic_auth_user and self.basic_auth_pass)

    @property
    def llm_available(self) -> bool:
        return self.llm_extraction_enabled and bool(self.openrouter_api_key)


settings = Settings()
