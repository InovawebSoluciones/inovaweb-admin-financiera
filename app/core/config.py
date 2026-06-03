"""Configuracion centralizada via pydantic-settings.

Carga desde variables de entorno (.env en dev, secrets reales en prod).
Toda variable obligatoria sin default falla al arrancar -> fail fast.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- entorno ---
    ENV: Literal["dev", "staging", "prod"] = Field(...)
    LOG_LEVEL: str = "INFO"
    PORT: int = 8001

    # --- base de datos ---
    DATABASE_URL: SecretStr = Field(...)
    POSTGRES_USER: str = "caf"
    POSTGRES_PASSWORD: SecretStr = Field(...)
    POSTGRES_DB: str = "admin_financiera"

    # --- cripto ---
    AES_KEY: SecretStr = Field(..., description="Base64 32 bytes AES-256-GCM")
    JWT_SECRET: SecretStr = Field(..., description="HMAC SHA-256 minimo 32 bytes")
    JWT_ACCESS_TTL_MIN: int = 15
    JWT_REFRESH_TTL_DAYS: int = 30

    # --- dominios ---
    ADMIN_DOMAIN: str = "admin.inovaweb.com.mx"
    PORTAL_DOMAIN: str = "app.inovaweb.com.mx"

    # --- cores Nivel 1 ---
    MEDIDOR_BASE_URL: str = "https://medidor.inovaweb.com.mx"
    MEDIDOR_API_KEY: SecretStr = Field(...)
    HUB_BASE_URL: str = "https://hub.inovaweb.com.mx"
    HUB_API_KEY: SecretStr = Field(...)
    MESSAGES_BASE_URL: str = "https://mensajes.inovaweb.com.mx"
    MESSAGES_API_KEY: SecretStr = Field(...)
    FINANZAS_BASE_URL: str = "https://finanzas.inovaweb.com.mx"
    FINANZAS_API_KEY: SecretStr = Field(...)

    # --- PAC (CFDI 4.0) ---
    PAC_PROVIDER: Literal["facturama", "factible", "edicom"] = "facturama"
    PAC_BASE_URL: str = "https://api.facturama.mx"
    PAC_API_KEY: SecretStr = Field(...)
    PAC_API_SECRET: SecretStr = Field(...)
    RFC_EMISOR: str = Field(...)
    CER_PATH: str = "/secrets/csd.cer"
    KEY_PATH: str = "/secrets/csd.key"
    KEY_PASSWORD: SecretStr = Field(...)

    # --- HTTP ---
    HTTP_TIMEOUT_SEC: float = 10.0
    HTTP_RETRIES: int = 3


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
