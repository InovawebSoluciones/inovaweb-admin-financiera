"""Cliente al core Nivel 1 'Hub de Pasarelas' (cobros).

Contrato real (docs/01-admin-financiera-integracion-cores.md §4):
  - POST /hub/v1/charge   inicia un cobro (recarga o pago de factura)

NOTAS firmes del contrato:
  - El Hub NO tiene endpoint admin de alta de empresa. La config de pasarela
    del cliente se inserta por SQL durante el alta (pendiente: el Hub debe
    exponer POST /admin/hub/v1/companies). Por eso este cliente NO crea cuentas.
  - El CAF nunca toca tarjetas. El Hub procesa el pago y devuelve webhook
    payment.paid a /webhooks/hub-payment-paid.
  - Idempotencia: UNIQUE por hub_transaction_id en la tabla payments del CAF.
"""

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

    async def charge(
        self,
        *,
        external_user_id: str,
        amount_cents: int,
        description: str,
        metadata: dict[str, Any],
        customer_email: str,
        customer_name: str,
        gateway: str = "conekta",
        operation: str = "charge_card",
    ) -> dict[str, Any]:
        """POST /hub/v1/charge.

        El Hub (`HubChargeRequest`) exige: gateway, operation, amount_cents,
        currency, description, customer_email, customer_name. NO tiene campo
        `metadata` (el `purpose` del webhook lo resuelve el Hub por defecto a
        'wallet_recharge'); `metadata` se conserva en la firma por compatibilidad
        pero NO se envia. `operation`: charge_card | charge_oxxo | charge_spei |
        charge_msi. Devuelve datos del intento (incluye `hub_transaction_id`).
        """
        return await self.c.post(
            "/hub/v1/charge",
            json={
                "gateway": gateway,
                "operation": operation,
                "amount_cents": amount_cents,
                "currency": "MXN",
                "description": description,
                "customer_email": customer_email,
                "customer_name": customer_name,
                "external_user_id": external_user_id,
            },
        )

    async def close(self) -> None:
        await self.c.close()


def make_admin() -> CoreClient:
    s = get_settings()
    key = s.HUB_ADMIN_KEY.get_secret_value() if s.HUB_ADMIN_KEY else ""
    return CoreClient(
        "hub-admin", s.HUB_BASE_URL, key,
        timeout_sec=s.HTTP_TIMEOUT_SEC, retries=s.HTTP_RETRIES,
    )


class HubAdminClient:
    """Admin del Hub (scope `admin:gateways`): configura las credenciales de
    pasarela de un tenant. El Hub las CIFRA y guarda; el CAF nunca las persiste
    ni las repite en sus respuestas."""

    def __init__(self, c: CoreClient | None = None):
        self.c = c or make_admin()

    async def list_gateways(self, company_id: str) -> dict[str, Any]:
        return await self.c.get(
            "/admin/hub/v1/gateway-config", params={"company_id": company_id}
        )

    async def save_gateway(
        self, *, company_id: str, gateway_slug: str,
        credentials: dict[str, str], is_default: bool = False,
        is_active: bool = True,
    ) -> dict[str, Any]:
        return await self.c.post(
            "/admin/hub/v1/gateway-config",
            json={
                "company_id": company_id, "gateway_slug": gateway_slug,
                "credentials": credentials, "is_default": is_default,
                "is_active": is_active,
            },
        )

    async def close(self) -> None:
        await self.c.close()
