"""Cliente al core Nivel 1 'Centro de Mensajes' (notificaciones humanas).

Contrato real (docs/01-admin-financiera-integracion-cores.md §6):
  - POST /v1/messages/email     envia un correo basado en plantilla del catalogo

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

    async def close(self) -> None:
        await self.c.close()
