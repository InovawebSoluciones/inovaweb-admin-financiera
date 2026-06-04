"""Cliente al core Nivel 1 'Medidor IA'.

Contrato real (docs/01-admin-financiera-integracion-cores.md §3):
  - POST   /v1/wallets                          crea wallet del cliente
  - GET    /v1/wallets/{wallet_id}/balance      saldo actual
  - GET    /v1/usage?from_ts&to_ts&project_id=  consumo del periodo
  - POST   /admin/v1/wallets/{wallet_id}/credit acredita saldo (recarga)

El Medidor es la fuente de verdad de saldo y consumo. El CAF nunca duplica
esos datos; los pide cuando los necesita. Las llaves del CAF hacia el Medidor
son de bootstrap (SQL), NO se emiten por cliente.
"""

from __future__ import annotations

from typing import Any

from app.core.clients._base import CoreClient
from app.core.config import get_settings


def make() -> CoreClient:
    s = get_settings()
    return CoreClient(
        "medidor",
        s.MEDIDOR_BASE_URL,
        s.MEDIDOR_API_KEY.get_secret_value(),
        timeout_sec=s.HTTP_TIMEOUT_SEC,
        retries=s.HTTP_RETRIES,
    )


class MedidorClient:
    def __init__(self, c: CoreClient | None = None):
        self.c = c or make()

    async def create_wallet(
        self, *, external_user_id: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST /v1/wallets -> {id, ...}. Devuelve el wallet creado."""
        return await self.c.post(
            "/v1/wallets",
            json={
                "external_user_id": external_user_id,
                "currency": "MXN",
                "metadata": metadata or {},
            },
        )

    async def get_balance(self, wallet_id: str) -> dict[str, Any]:
        """GET /v1/wallets/{wallet_id}/balance."""
        return await self.c.get(f"/v1/wallets/{wallet_id}/balance")

    async def get_usage(
        self, wallet_id: str, *, from_ts: str, to_ts: str
    ) -> dict[str, Any]:
        """GET /v1/usage?from_ts=&to_ts=&project_id=wallet_id (consumo IA)."""
        return await self.c.get(
            "/v1/usage",
            params={"from_ts": from_ts, "to_ts": to_ts, "project_id": wallet_id},
        )

    async def credit(
        self,
        wallet_id: str,
        *,
        amount_cents: int,
        request_id: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /admin/v1/wallets/{wallet_id}/credit. Acredita saldo (recarga).

        `request_id` es UNIQUE en el Medidor (idempotencia): un webhook duplicado
        del Hub no produce doble acreditacion. Patron: caf-recharge-<RCH-id>.
        """
        return await self.c.post(
            f"/admin/v1/wallets/{wallet_id}/credit",
            json={
                "amount_cents": amount_cents,
                "currency": "MXN",
                "request_id": request_id,
                "reason": reason,
                "metadata": metadata or {},
            },
        )

    async def delete_wallet(self, wallet_id: str) -> None:
        """Compensacion best-effort de la Saga de alta.

        El contrato no documenta un DELETE de wallet; se intenta y se ignora el
        resultado (la Saga lo invoca via _safe). Confirmar endpoint real con el
        equipo del Medidor; si no existe, la compensacion marca para limpieza
        manual en vez de borrar.
        """
        await self.c.delete(f"/admin/v1/wallets/{wallet_id}")

    async def close(self) -> None:
        await self.c.close()
