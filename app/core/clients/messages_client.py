"""Cliente al core Nivel 1 'Centro de Mensajes' (notificaciones humanas).

Contrato real (docs/01-admin-financiera-integracion-cores.md §6 + fuente del
Centro verificado en `inovaweb-centro-mensajes/app/routers/messages_router.py`):
  - POST /v1/messages/email     envia un correo basado en plantilla del catalogo
  - GET  /v1/reports/usage      agregados de consumo por canal/cliente/periodo
                                (scope messages:read). NO es /v1/usage (PASO 3).

NOTAS firmes del contrato (§6.1):
  - El Centro es multi-tenant: resuelve el tenant desde la API key. NO se crea
    una "cuenta" por cliente; NO existen create_account / issue_api_key /
    delete_account (eran invenciones del scaffolding y se eliminaron). SMS queda
    fuera del piloto (no hay send_sms).
  - El CAF siempre envia por `origin_kind: "template"` contra plantillas
    precargadas al setup (§6.2). Las variables tipadas se validan contra el
    schema del Centro.
  - El Centro despacha y reporta el cargo al Finanzas-Core con su propia llave;
    queda reflejado en el balance del cliente.
  - El endpoint responde 202 Accepted con un `message_id` (persistir para
    correlacion, §6.4).
"""

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


_DEFAULT_FROM: dict[str, str] = {
    "email": "facturacion@inovaweb.com.mx",
    "name": "Inovaweb",
}


class MessagesClient:
    def __init__(self, c: CoreClient | None = None):
        self.c = c or make()

    async def send_email(
        self,
        *,
        client_id: str,
        service_id: str,
        template_id: str,
        to: dict[str, Any],
        variables: dict[str, Any],
        from_: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        app_id: str = "admin-financiera",
    ) -> dict[str, Any]:
        """POST /v1/messages/email - envia correo desde una plantilla (§6.3).

        `client_id` es el UUID del cliente CAF; `template_id` el slug del
        catalogo (§6.2, p.ej. 'caf-factura-emitida'); `to` el destinatario
        `{email, name}`; `variables` el contexto tipado de la plantilla.
        `from_` default `{"email": "facturacion@inovaweb.com.mx",
        "name": "Inovaweb"}`. Devuelve dict con `message_id` (el endpoint
        responde 202 Accepted; persistirlo para correlacion).
        """
        return await self.c.post(
            "/v1/messages/email",
            json={
                "app_id": app_id,
                "client_id": client_id,
                "service_id": service_id,
                "origin_kind": "template",
                "template_id": template_id,
                "from": from_ or dict(_DEFAULT_FROM),
                "to": to,
                "variables": variables,
                "meta": meta or {},
            },
        )

    async def send_whatsapp(
        self,
        *,
        client_id: str,
        service_id: str,
        template_id: str,
        to: dict[str, Any],
        variables: dict[str, Any],
        meta: dict[str, Any] | None = None,
        app_id: str = "admin-financiera",
    ) -> dict[str, Any]:
        """POST /v1/messages/whatsapp - envia WhatsApp desde una plantilla.

        Analogo a `send_email` pero por canal WhatsApp (usado para
        activacion/OTP y recordatorios de vencimiento). El destinatario `to`
        se identifica por `{phone, name}`. WhatsApp no lleva remitente `from`
        (a diferencia del correo). Devuelve dict con `message_id` (el endpoint
        responde 202 Accepted).
        """
        # TODO confirmar endpoint real de WhatsApp con el Centro de Mensajes
        # (el contrato §6.3 solo documenta el endpoint de email; se asume
        #  POST /v1/messages/whatsapp con la misma forma de body).
        return await self.c.post(
            "/v1/messages/whatsapp",
            json={
                "app_id": app_id,
                "client_id": client_id,
                "service_id": service_id,
                "origin_kind": "template",
                "template_id": template_id,
                "to": to,
                "variables": variables,
                "meta": meta or {},
            },
        )

    async def get_usage(
        self, external_user_id: str, *, from_ts: str, to_ts: str
    ) -> dict[str, int]:
        """GET /v1/reports/usage — consumo de mensajeria del cliente en el periodo.

        Ruta REAL del Centro de Mensajes (PASO 3, verificada contra el fuente
        `inovaweb-centro-mensajes/app/routers/messages_router.py:758`):
        `GET /v1/reports/usage?group_by=client&from_ts=&to_ts=` (scope
        messages:read). NO existe `/v1/usage`; ese era el error original. El
        Centro resuelve el tenant desde la API key y agrega por `client_id`,
        devolviendo `{rows: [{client_id, count, amount_cents}, ...]}`. Aqui se
        selecciona la fila cuyo `client_id` coincide con `external_user_id`.

        Devuelve cantidad de mensajes y costo total en centavos enteros (BIGINT).
        El Centro es la fuente de verdad; el CAF solo lee este resumen para
        facturarlo al cierre mensual. Si el cliente no tiene mensajes en el
        periodo (no aparece en `rows`), devuelve ceros (degradacion graceful, no
        es error).

        Args:
            external_user_id: identificador del cliente en el Centro de Mensajes
                (en el CAF corresponde a `clients.messages_account_id`; el Centro
                lo registra como `messages.client_id` al despachar).
            from_ts: inicio del periodo (ISO-8601).
            to_ts: fin del periodo (ISO-8601).

        Returns:
            dict con claves `messages` (int) y `cost_cents` (int).
        """
        resp = await self.c.get(
            "/v1/reports/usage",
            params={
                "group_by": "client",
                "from_ts": from_ts,
                "to_ts": to_ts,
            },
        )
        rows = (resp or {}).get("rows") or []
        target = str(external_user_id)
        messages = 0
        cost_cents = 0
        for row in rows:
            if str(row.get("client_id")) != target:
                continue
            # Campos reales del endpoint: `count` y `amount_cents` (se toleran
            # alias por robustez ante cambios de contrato).
            messages = int(
                row.get("count")
                or row.get("messages")
                or row.get("sent")
                or 0
            )
            cost_cents = int(
                row.get("amount_cents")
                or row.get("cost_cents")
                or row.get("total_cost_cents")
                or 0
            )
            break
        return {"messages": messages, "cost_cents": cost_cents}

    async def close(self) -> None:
        await self.c.close()
