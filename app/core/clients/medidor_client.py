"""Cliente al core Nivel 1 'medidor' (consumo por cliente)."""

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

    async def create_account(self, *, legal_name: str, rfc: str) -> dict[str, Any]:
        return await self.c.post("/v1/accounts", json={"legal_name": legal_name, "rfc": rfc})

    async def delete_account(self, account_id: str) -> None:
        await self.c.delete(f"/v1/accounts/{account_id}")

    async def issue_api_key(self, account_id: str, name: str) -> dict[str, Any]:
        return await self.c.post(f"/v1/accounts/{account_id}/keys", json={"name": name})

    async def get_usage(self, account_id: str, period_start: str, period_end: str) -> dict[str, Any]:
        return await self.c.get(
            f"/v1/accounts/{account_id}/usage",
            params={"from": period_start, "to": period_end},
        )

    async def close(self) -> None:
        await self.c.close()
