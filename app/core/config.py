"""Configuracion centralizada via pydantic-settings.

Carga desde variables de entorno (.env en dev, secrets reales en prod).
Toda variable obligatoria sin default falla al arrancar -> fail fast.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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
    # Pasarela por defecto que el CAF pide al Hub en recargas. Pruebas='mock'
    # (sandbox interno del Hub, pago simulado sin tarjeta); prod='conekta' u otra
    # real configurada en el Hub para la company.
    HUB_GATEWAY: str = "mock"

    # --- apps Nivel 3 (orquestacion desde el CAF) ---
    # Scraping Universidades: el CAF liga wallet + cliente en el alta (#16).
    SCRAPING_BASE_URL: str = "https://scraping.inovaweb.com.mx"
    SCRAPING_ADMIN_KEY: SecretStr = Field(...)
    SWIGG_ADMIN_KEY: SecretStr | None = None

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

    # --- webhooks Hub-Pasarelas ---
    # Secreto HMAC dedicado para validar la firma del webhook del Hub.
    # FIX-3 (TASK-15b): en prod es OBLIGATORIO (validator de abajo falla el
    # arranque si falta). En dev/staging, si no se define uno dedicado, se
    # reusa HUB_API_KEY (firma con la misma llave compartida) solo para no
    # bloquear el desarrollo local.
    HUB_WEBHOOK_SECRET: SecretStr | None = None
    # Tolerancia (segundos) de la marca de tiempo firmada para mitigar replay.
    HUB_WEBHOOK_TOLERANCE_SEC: int = 300
    # FIX-6 (TASK-15b): tope superior del monto de recarga autoservicio
    # (centavos MXN, BIGINT). El minimo (5000 = $50) vive en el router.
    MAX_RECARGA_CENTS: int = 50_000_000  # $500,000 MXN
    # Plantilla y service del Centro de Mensajes para confirmar pago/recarga.
    CAF_PAGO_CONFIRMADO_TEMPLATE: str = "caf-pago-confirmado"
    CAF_MESSAGES_SERVICE_ID: str = "caf-notificaciones"
    # TASK-16: plantilla del Centro de Mensajes para el correo de activacion
    # del titular (token de un solo uso). La activacion expira en 24h.
    CAF_ACTIVACION_TEMPLATE: str = "caf-activacion-correo"

    @model_validator(mode="after")
    def _require_webhook_secret_in_prod(self) -> "Settings":
        """FIX-3 + H4 (hardening #19/22): fail-closed del webhook en prod.

        En produccion HUB_WEBHOOK_SECRET es OBLIGATORIO y debe ser un secreto
        DEDICADO: no puede faltar ni ser igual a HUB_API_KEY. Reusar la llave
        de API como secreto del webhook degradaria la separacion de secretos
        (quien tenga la API key podria forjar webhooks). Cualquiera de las dos
        condiciones aborta el arranque (ValueError -> fail fast).
        """
        if self.ENV == "prod":
            if self.HUB_WEBHOOK_SECRET is None:
                raise ValueError(
                    "HUB_WEBHOOK_SECRET es obligatorio en prod "
                    "(no se permite fallback a HUB_API_KEY)"
                )
            if (self.HUB_WEBHOOK_SECRET.get_secret_value()
                    == self.HUB_API_KEY.get_secret_value()):
                raise ValueError(
                    "HUB_WEBHOOK_SECRET no puede ser igual a HUB_API_KEY en prod "
                    "(debe ser un secreto dedicado para la firma del webhook)"
                )
        return self

    @property
    def MAX_RECHARGE_AMOUNT_CENTS(self) -> int:
        """H5 (hardening #19/22): alias del tope de recarga (centavos BIGINT).

        El setting canonico es MAX_RECARGA_CENTS (ya cableado en el router y el
        servicio). Se expone este alias con el nombre del spec para no duplicar
        la fuente de verdad del tope (ambos devuelven el mismo valor).
        """
        return self.MAX_RECARGA_CENTS

    def hub_webhook_secret(self) -> str:
        """Secreto efectivo para verificar la firma del webhook del Hub.

        En prod siempre devuelve HUB_WEBHOOK_SECRET (garantizado por el
        validator). En dev/staging cae a HUB_API_KEY si no hay dedicado.
        """
        if self.HUB_WEBHOOK_SECRET is not None:
            return self.HUB_WEBHOOK_SECRET.get_secret_value()
        return self.HUB_API_KEY.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
# TASK-15: settings de webhook Hub-Pasarelas agregados arriba.
