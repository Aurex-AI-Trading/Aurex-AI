"""
Aurex AI SaaS — Application Configuration
All settings sourced from environment variables with sensible defaults.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── App ──────────────────────────────────────────────────────────────────
    APP_NAME: str = "Aurex AI"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    # ── Security ─────────────────────────────────────────────────────────────
    SECRET_KEY: str = "change-me-in-production-min-32-chars-long-key"
    ENCRYPTION_KEY: str = ""          # Fernet key; auto-generated if blank
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    ALGORITHM: str = "HS256"

    # ── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://aurex:aurex@localhost:5432/aurex_ai"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "https://app.aurexai.com",
    ]

    # ── Rate limiting ─────────────────────────────────────────────────────────
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 5
    RATE_LIMIT_API_PER_MINUTE: int = 120

    # ── Admin ─────────────────────────────────────────────────────────────────
    FIRST_ADMIN_EMAIL: str = "admin@aurexai.com"
    FIRST_ADMIN_PASSWORD: str = "ChangeMe123!"

    # ── MT5 ──────────────────────────────────────────────────────────────────
    MT5_RECONNECT_INTERVAL_SECONDS: int = 30
    MT5_HEALTH_CHECK_INTERVAL_SECONDS: int = 60
    MT5_MAX_RECONNECT_ATTEMPTS: int = 5

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v):
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
