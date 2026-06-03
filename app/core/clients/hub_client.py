"""Cliente al core Nivel 1 'hub-pasarelas' (cobros)."""

from __future__ import annotations

from typing import Any

from app.core.clients._base import CoreClient
from app.core.config import get_settings


def make() -> CoreClient:
    s = get_settings()
    return CoreClient(
        "hub",
        s.HUB_BASE_URL,
        s.HUB_API_KEY.get_secret_value(),
        timeout_sec=s.HTTP_TIMEOUT_SEC,
        retries=s.HTTP_RETRIES,
    )


class HubClient:
    def __init__(self, c: CoreClient | None = None):
        self.c = c or make()

    async def create_account(self, *, legal_name: str, rfc: str, email: str) -> dict[str, Any]:
        return await self.c.post(
            "/v1/accounts",
            json={"legal_name": legal_name, "rfc": rfc, "email": email},
        )

    async def delete_account(self, account_id: str) -> None:
        await self.c.delete(f"/v1/accounts/{account_id}")

    async def issue_api_key(self, account_id: str, name: str) -> dict[str, Any]:
        return await self.c.post(f"/v1/accounts/{account_id}/keys", json={"name": name})

    async def create_payment_intent(
        self, account_id: str, *, amount_cents: int, concept: str, return_url: str
    ) -> dict[str, Any]:
        return await self.c.post(
            f"/v1/accounts/{account_id}/payment-intents",
            json={
                "amount_cents": amount_cents,
                "currency": "MXN",
                "concept": concept,
                "return_url": return_url,
            },
        )

    async def close(self) -> None:
        await self.c.close()
