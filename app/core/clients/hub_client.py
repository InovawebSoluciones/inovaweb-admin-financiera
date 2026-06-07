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
        gateway: str = "conekta",
        operation: str = "charge_card",
    ) -> dict[str, Any]:
        """POST /hub/v1/charge.

        `metadata.purpose` distingue el flujo en el webhook:
          - 'plan_purchase' / 'wallet_recharge' -> acreditar saldo en Medidor
          - 'invoice_payment'                   -> marcar factura pagada
        `operation` es requerido por el Hub (charge_card | charge_oxxo |
        charge_spei | charge_msi). `amount` va en centavos (BIGINT). Devuelve
        datos del intento de cobro.
        """
        return await self.c.post(
            "/hub/v1/charge",
            json={
                "gateway": gateway,
                "operation": operation,
                "external_user_id": external_user_id,
                "amount": amount_cents,
                "currency": "MXN",
                "description": description,
                "metadata": metadata,
            },
        )

    async def close(self) -> None:
        await self.c.close()
