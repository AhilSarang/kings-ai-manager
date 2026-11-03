"""Application settings, loaded from environment variables / .env.

Every other module reads configuration through `get_settings()`; nothing
reads os.environ directly.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Runtime
    app_env: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    timezone: str = "America/New_York"

    # Database
    database_url: str = "postgresql+psycopg://kings:change-me@localhost:5432/kings_ai"

    # Owner
    owner_name: str = "the owner"
    owner_whatsapp_number: str = "+10000000000"
    owner_email: str | None = None

    # WhatsApp
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_api_version: str = "v21.0"
    whatsapp_template_name: str = "ai_manager_update"
    whatsapp_template_lang: str = "en_US"
    messaging_backend: Literal["meta", "console"] = "meta"
    outbound_max_per_10min: int = Field(default=25, ge=5)

    # LLM
    llm_backend: Literal["anthropic_api", "fake"] = "anthropic_api"
    llm_concurrency: int = Field(default=1, ge=1, le=8)
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    @field_validator("anthropic_effort", mode="before")
    @classmethod
    def _blank_effort(cls, v):
        return v or None

    # Scheduler
    daily_summary_time: str = "07:30"

    # Email (Phase 2)
    gmail_account_address: str = "you@example.com"
    gmail_backend: Literal["api", "fake"] = "api"
    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr = SecretStr("")
    google_cloud_project_id: str = "kings-ai-manager"
    gmail_pubsub_topic: str = "projects/kings-ai-manager/topics/gmail-inbox"
    gmail_push_audience: str = "https://ai.example.com/webhooks/gmail"
    gmail_push_service_account: str = "gmail-push-invoker@kings-ai-manager.iam.gserviceaccount.com"
    token_encryption_key: SecretStr = SecretStr("")

    # ---- Amazon SP-API (Phase 3) ----
    amazon_lwa_client_id: str = ""
    amazon_lwa_client_secret: SecretStr = SecretStr("")
    amazon_lwa_refresh_token: SecretStr = SecretStr("")
    amazon_seller_id: str = ""
    amazon_region: Literal["na", "eu", "fe"] = "na"
    amazon_enabled_marketplaces: str = "US"
    amazon_write_mode: Literal["validation_only", "live"] = "validation_only"
    amazon_backend: Literal["api", "fake"] = "api"
    # ---- Amazon Ads API (Phase 4). A separate registration and LWA client from SP-API. ----
    ads_lwa_client_id: str = ""
    ads_lwa_client_secret: SecretStr = SecretStr("")
    ads_lwa_refresh_token: SecretStr = SecretStr("")
    ads_region: Literal["na", "eu", "fe"] = "na"
    ads_profile_id_us: str = ""
    ads_profile_id_ca: str = ""
    ads_account_id: str = ""            # Amazon-Ads-AccountId, required on every v3 report request
    ads_backend: Literal["api", "fake"] = "api"
    ppc_write_mode: Literal["read_only", "live"] = "read_only"
    # ---- Marketing Stream (M11). Hourly metrics arrive by AWS Firehose into an SQS queue we poll.
    # Empty queue URL means the consumer stays asleep, exactly as the Ads code sleeps without creds.
    ads_marketing_stream_queue_url: str = ""
    ads_marketing_stream_region: str = "us-east-1"
    # Anomaly thresholds. Percentages, measured against the trailing same-weekday average, because
    # a Tuesday that looks nothing like Saturday is a weekday effect, not an anomaly.
    ppc_anomaly_spend_spike_pct: float = 60.0
    ppc_anomaly_cpc_jump_pct: float = 50.0
    ppc_budget_exhausted_pct: float = 95.0
    # Exhausting the budget at 11pm is how a healthy campaign ends its day; at noon it is news.
    ppc_budget_early_fraction: float = 0.75

    public_media_backend: Literal["s3", "fake"] = "s3"
    public_media_bucket: str = ""
    public_media_base_url: str = ""
    public_media_endpoint: str = ""
    public_media_access_key_id: str = ""
    public_media_secret_access_key: SecretStr = SecretStr("")
    # Official Amazon sending domains accepted for "genuine Amazon" (DKIM must pass for one of these)
    amazon_dkim_domains: str = "amazon.com,amazon.ca,amazon.com.mx,amazon.co.uk,amazonses.com"
    email_max_attachment_mb: int = Field(default=25, ge=1, le=25)
    email_body_cap_bytes: int = Field(default=200_000, ge=10_000)

    # Files
    file_storage_dir: str = "/data/files"
    max_upload_mb: int = Field(default=25, ge=1, le=100)

    @field_validator("owner_whatsapp_number")
    @classmethod
    def _e164(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not E164_RE.match(v):
            raise ValueError("OWNER_WHATSAPP_NUMBER must be E.164, e.g. +18045550100")
        return v

    @field_validator("daily_summary_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM_RE.match(v.strip()):
            raise ValueError("DAILY_SUMMARY_TIME must be HH:MM (24h)")
        return v.strip()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {v}") from exc
        return v

    @field_validator("owner_email", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        return v or None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
