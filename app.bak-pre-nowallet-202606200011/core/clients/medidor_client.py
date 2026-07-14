"""Cliente al core Nivel 1 'Medidor IA'.

Contrato real (verificado contra medidor_ia 2026-06-06; ver docs/ARQUITECTURA-GLOBAL.md §3):
  - POST   /v1/wallets                            crea wallet del cliente (scope ADMIN)
  - GET    /v1/wallets/{wallet_id}/balance        saldo actual
  - GET    /v1/usage?from_ts&to_ts&project_id=    consumo del periodo
  - POST   /v1/wallets/{wallet_id}/credit         acredita saldo (scope ADMIN, recarga)
  - POST   /admin/v1/wallets/{wallet_id}/suspend  suspende wallet (compensacion Saga)

NOTA: el credit vive en /v1/wallets/... (NO /admin/v1), aunque exija scope ADMIN.
El Medidor NO expone DELETE de wallet; la compensacion usa suspend.

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
        """POST /v1/wallets -> {id, ...}. Devuelve el wallet creado.

        El Medidor (`CreateWalletIn`, `extra="forbid"`) SOLO acepta
        `external_user_id` y `currency`; NO `metadata`. El parametro `metadata`
        se conserva por compatibilidad de firma pero NO se envia (enviarlo da
        422 extra_forbidden).
        """
        resp = await self.c.post(
            "/v1/wallets",
            json={
                "external_user_id": external_user_id,
                "currency": "MXN",
            },
        )
        # El Medidor responde con `BalanceOut` (clave `wallet_id`); normalizamos a
        # `id` para los callers (onboarding lee wallet["id"]).
        if isinstance(resp, dict) and "id" not in resp and resp.get("wallet_id"):
            resp = {**resp, "id": resp["wallet_id"]}
        return resp

    async def get_balance(self, wallet_id: str) -> dict[str, Any]:
        """GET /v1/wallets/{wallet_id}/balance."""
        return await self.c.get(f"/v1/wallets/{wallet_id}/balance")

    async def get_usage(
        self, wallet_id: str, *, from_ts: str, to_ts: str
    ) -> dict[str, Any]:
        """GET /v1/usage?from_ts=&to_ts=&project_id=wallet_id (consumo IA).

        Devuelve la respuesta cruda del Medidor (incluye `items` por servicio).
        Ruta `/v1/usage` (NO `/admin/v1`): es la forma correcta del contrato
        (ver §3.4 del doc de integracion); el bug C1 de rutas `/admin/v1`
        afecta a otros metodos del cliente, NO a este.
        """
        return await self.c.get(
            "/v1/usage",
            params={"from_ts": from_ts, "to_ts": to_ts, "project_id": wallet_id},
        )

    async def get_usage_summary(
        self, wallet_id: str, *, from_ts: str, to_ts: str
    ) -> dict[str, int]:
        """Resumen agregado de consumo IA del periodo para una wallet.

        Llama a `get_usage` y agrega los `items` del Medidor en dos totales:
          - `operations`: numero total de operaciones IA del periodo.
          - `cost_cents`: costo total consumido, en centavos enteros (BIGINT).

        El Medidor es la fuente de verdad del consumo. Esta funcion solo suma
        sobre la respuesta cruda; tolera respuestas sin `items` (devuelve ceros)
        y nombres de campo alternativos (`operations`/`units`/`count` para la
        cantidad; `cost_cents`/`amount_cents` para el costo).

        Args:
            wallet_id: id de la wallet del cliente en el Medidor.
            from_ts: inicio del periodo (ISO-8601).
            to_ts: fin del periodo (ISO-8601).

        Returns:
            dict con claves `operations` (int) y `cost_cents` (int).
        """
        usage = await self.get_usage(wallet_id, from_ts=from_ts, to_ts=to_ts)
        operations = 0
        cost_cents = 0
        for item in (usage or {}).get("items", []) or []:
            operations += int(
                item.get("operations")
                or item.get("units")
                or item.get("count")
                or 0
            )
            cost_cents += int(
                item.get("cost_cents")
                or item.get("amount_cents")
                or 0
            )
        # algunos despliegues exponen totales a nivel raiz; preferirlos si vienen
        if not operations and usage:
            operations = int(usage.get("operations") or usage.get("total_operations") or 0)
        if not cost_cents and usage:
            cost_cents = int(usage.get("cost_cents") or usage.get("total_cost_cents") or 0)
        return {"operations": operations, "cost_cents": cost_cents}

    async def credit(
        self,
        wallet_id: str,
        *,
        amount_cents: int,
        request_id: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /v1/wallets/{wallet_id}/credit. Acredita saldo (recarga).

        `request_id` es UNIQUE en el Medidor (idempotencia): un webhook duplicado
        del Hub no produce doble acreditacion. Patron: caf-recharge-<RCH-id>.

        `CreditIn` del Medidor es `extra="forbid"`: solo amount_cents, currency,
        request_id, reason. NO metadata (el parametro se conserva por firma pero
        no se envia). `currency` debe matchear la moneda del wallet (MXN).
        """
        return await self.c.post(
            f"/v1/wallets/{wallet_id}/credit",
            json={
                "amount_cents": amount_cents,
                "currency": "MXN",
                "request_id": request_id,
                "reason": reason,
            },
        )

    async def suspend_wallet(
        self, wallet_id: str, *, reason: str = "onboarding compensation"
    ) -> None:
        """Compensacion best-effort de la Saga de alta.

        El Medidor NO expone DELETE de wallet, asi que la compensacion SUSPENDE la
        wallet recien creada. La Saga la invoca via _safe (ignora errores). Una
        wallet suspendida y sin saldo es inocua y queda marcada para
        limpieza/reactivacion manual.

        RUTA VERIFICADA contra el fuente del Medidor (PASO 2, C1 parcial,
        2026-06-06): el router admin se monta con `prefix="/admin/v1"`
        (`medidor_ia/src/medidor_ia/admin/router.py:39`) y define
        `/wallets/{wallet_id}/suspend` (linea 125) con body `SuspendWalletIn`
        (`{reason: str}`). Por tanto `POST /admin/v1/wallets/{id}/suspend` es
        CORRECTO. (El doc de contrato `docs/01-...-integracion-cores.md:183` lo
        lista bajo `/v1/...` por error; la fuente de verdad es el codigo del
        Medidor. A diferencia de `credit`, que SI vive en `/v1/wallets/{id}/credit`.)
        """
        await self.c.post(
            f"/admin/v1/wallets/{wallet_id}/suspend",
            json={"reason": reason},
        )

    async def close(self) -> None:
        await self.c.close()
