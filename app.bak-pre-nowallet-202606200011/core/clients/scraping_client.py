"""Cliente al app Nivel 3 'Scraping Universidades' (multi-tenant).

El CAF (Nivel 2) liga, en el alta del cliente (#16), la wallet del Medidor y el
id del cliente CAF con la `Company` correspondiente en Scraping. Scraping ya
tiene en su tabla `companies` las columnas `caf_client_id` y `medidor_wallet_id`
(TASK-21) pero sin ruta que las poblara; este cliente consume el endpoint
`POST /companies/{company_id}/link-caf` que rellena ambas de forma idempotente.

Autenticacion: Bearer admin de Scraping (`SCRAPING_ADMIN_KEY`). El `CoreClient`
base usa por defecto el header `X-API-Key`; aqui se sobreescribe a
`Authorization: Bearer <key>` para alinearse con el mecanismo de auth admin de
Scraping (`settings.scraping_bearer_legacy`).
"""

from __future__ import annotations

from typing import Any

from app.core.clients._base import CoreClient
from app.core.config import get_settings


def make() -> CoreClient:
    """Construye el `CoreClient` hacia Scraping con auth Bearer admin.

    El header de auth se fija a `Authorization` y la llave se prefija con
    `Bearer ` para coincidir con el verificador de Scraping (que compara contra
    `settings.scraping_bearer_legacy`).
    """
    s = get_settings()
    return CoreClient(
        "scraping",
        s.SCRAPING_BASE_URL,
        f"Bearer {s.SCRAPING_ADMIN_KEY.get_secret_value()}",
        timeout_sec=s.HTTP_TIMEOUT_SEC,
        retries=s.HTTP_RETRIES,
        api_key_header="Authorization",
    )


class ScrapingClient:
    """Cliente async al app Scraping (operaciones de orquestacion del CAF)."""

    def __init__(self, c: CoreClient | None = None) -> None:
        """Inicializa el cliente; permite inyectar un `CoreClient` (tests)."""
        self.c = c or make()

    async def link_caf(
        self,
        company_id: int,
        caf_client_id: int,
        medidor_wallet_id: str,
    ) -> dict[str, Any]:
        """POST /companies/{company_id}/link-caf — liga wallet + cliente CAF.

        Persiste en la `Company` de Scraping el `caf_client_id` (id del cliente
        en el CAF) y el `medidor_wallet_id` (wallet prepago del Medidor). El
        endpoint es idempotente: re-ejecutarlo con los mismos valores no falla.

        Args:
            company_id: id de la `Company` en Scraping.
            caf_client_id: id del cliente en el CAF (`clients.id`).
            medidor_wallet_id: id de la wallet del Medidor (string/UUID).

        Returns:
            dict con la Company actualizada que devuelve Scraping.
        """
        return await self.c.post(
            f"/companies/{company_id}/link-caf",
            json={
                # caf_client_id va como int (LinkCafIn.caf_client_id: int, columna
                # BIGINT tras el fix D1). medidor_wallet_id va como string y el
                # endpoint lo coerce a UUID (es un UUID real del Medidor).
                "caf_client_id": caf_client_id,
                "medidor_wallet_id": str(medidor_wallet_id),
            },
        )

    async def close(self) -> None:
        """Cierra el cliente HTTP subyacente."""
        await self.c.close()
