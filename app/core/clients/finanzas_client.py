"""Cliente al core Nivel 1 'Finanzas-Core' (ledger contable consolidado).

Contrato real (docs/01-admin-financiera-integracion-cores.md §5):
  - GET  /v1/ledger/balance?as_of=        saldo del tenant a una fecha
  - GET  /v1/ledger/totals?from_ts&to_ts   totales por fuente y periodo
  - GET  /v1/ledger/entries?source&...     listado paginado de movimientos
  - POST /v1/ledger/entries                registra un asiento (pago/ajuste)

NOTAS firmes del contrato (§5.1):
  - El Finanzas-Core es multi-tenant strict: resuelve `tenant_id` desde la API
    key. El CAF usa una key admin master que ve todos los tenants. NO se crea
    una "cuenta" por cliente; NO existen create_account / issue_api_key /
    delete_account (eran invenciones del scaffolding y se eliminaron).
  - Para la vista filtrada por cliente, el CAF aplica filtros adicionales por
    `external_user_id` (que vive en `meta` JSONB de cada asiento).
  - Dinero siempre en centavos enteros (BIGINT). Nunca floats.

Convencion de `source_ref` (§5.3) — determinismo => idempotencia en el ledger:
  - Pago de factura     : caf-invoice-<invoice_id>-payment
  - Recarga acreditada  : caf-recharge-<rch_id>
  - Ajuste manual       : caf-manual-adj-<adj_id>
  - Cuota suscripcion   : caf-sub-<client>-<yyyymm>
  - Reversion           : <original>-reversal
"""

from __future__ import annotations

from typing import Any

from app.core.clients._base import CoreClient
from app.core.config import get_settings


def make() -> CoreClient:
    s = get_settings()
    return CoreClient(
        "finanzas",
        s.FINANZAS_BASE_URL,
        s.FINANZAS_API_KEY.get_secret_value(),
        timeout_sec=s.HTTP_TIMEOUT_SEC,
        retries=s.HTTP_RETRIES,
    )


class FinanzasClient:
    def __init__(self, c: CoreClient | None = None):
        self.c = c or make()

    async def get_balance(
        self, *, as_of: str, external_user_id: str | None = None
    ) -> dict[str, Any]:
        """GET /v1/ledger/balance?as_of=.

        `as_of` es timestamp ISO-8601 (p.ej. '2026-06-30T23:59:59Z'). El tenant
        se resuelve desde la API key. Si se pasa `external_user_id`, se agrega
        como filtro para obtener la vista del balance de un cliente concreto
        (el Finanzas-Core lo cruza contra `meta.external_user_id`).
        """
        params: dict[str, Any] = {"as_of": as_of}
        if external_user_id:
            params["external_user_id"] = external_user_id
        return await self.c.get("/v1/ledger/balance", params=params)

    async def get_totals(self, *, from_ts: str, to_ts: str) -> dict[str, Any]:
        """GET /v1/ledger/totals?from_ts=&to_ts=.

        Totales agregados por fuente para el periodo [from_ts, to_ts).
        Timestamps ISO-8601.
        """
        return await self.c.get(
            "/v1/ledger/totals",
            params={"from_ts": from_ts, "to_ts": to_ts},
        )

    async def list_entries(
        self,
        *,
        source: str | None = None,
        direction: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /v1/ledger/entries — listado paginado de movimientos.

        `source` (slug de la fuente, p.ej. 'hub') y `direction`
        ('credit'/'debit') son filtros opcionales.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if source is not None:
            params["source"] = source
        if direction is not None:
            params["direction"] = direction
        return await self.c.get("/v1/ledger/entries", params=params)

    async def post_entry(
        self,
        *,
        source_slug: str,
        source_ref: str,
        direction: str,
        amount_cents: int,
        occurred_at: str,
        description: str,
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /v1/ledger/entries — registra un asiento contable.

        `source_ref` debe seguir la convencion del CAF (§5.3, ver docstring del
        modulo); es determinista para garantizar idempotencia en el ledger ante
        reintentos. `amount_cents` en centavos enteros (BIGINT); `direction` es
        'credit' o 'debit'; `occurred_at` ISO-8601.
        """
        return await self.c.post(
            "/v1/ledger/entries",
            json={
                "source_slug": source_slug,
                "source_ref": source_ref,
                "direction": direction,
                "amount_cents": amount_cents,
                "currency": "MXN",
                "occurred_at": occurred_at,
                "description": description,
                "meta": meta,
            },
        )

    async def close(self) -> None:
        await self.c.close()
