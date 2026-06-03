"""Cliente al core Nivel 1 'centro-mensajes' (notificaciones)."""

from __future__ import annotations

from typing import Any

from app.core.clients._base import CoreClient
from app.core.config import get_settings


def make() -> CoreClient:
    s = get_settings()
    return CoreClient(
        "messages",
        s.MESSAGES_BASE_URL,
        s.MESSAGES_API_KEY.get_secret_value(),
        timeout_sec=s.HTTP_TIMEOUT_SEC,
        retries=s.HTTP_RETRIES,
    )


class MessagesClient:
    def __init__(self, c: CoreClient | None = None):
        self.c = c or make()

    async def create_account(self, *, legal_name: str, rfc: str) -> dict[str, Any]:
        return await self.c.post("/v1/accounts", json={"legal_name": legal_name, "rfc": rfc})

    async def delete_account(self, account_id: str) -> None:
        await self.c.delete(f"/v1/accounts/{account_id}")

    async def issue_api_key(self, account_id: str, name: str) -> dict[str, Any]:
        return await self.c.post(f"/v1/accounts/{account_id}/keys", json={"name": name})

    async def send_email(
        self, *, to: str, subject: str, html: str, account_id: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"to": to, "subject": subject, "html": html}
        if account_id:
            payload["account_id"] = account_id
        return await self.c.post("/v1/email", json=payload)

    async def send_sms(self, *, to: str, body: str, account_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"to": to, "body": body}
        if account_id:
            payload["account_id"] = account_id
        return await self.c.post("/v1/sms", json=payload)

    async def close(self) -> None:
        await self.c.close()
