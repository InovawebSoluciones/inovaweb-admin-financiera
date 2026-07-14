"""Servicio de ENVIO DE EMAIL del CAF por proveedor configurado.

Resuelve el proveedor por defecto activo del ambito (cliente o, en su defecto,
nivel org) desde email_providers, descifra la credencial (app.core.crypto) y
envia el correo por SMTP (smtplib + ssl).

BEST-EFFORT: ninguna funcion lanza por fallo (de BD, descifrado o envio); siempre
devuelve {"ok": bool, "detail": str, ...}. El secreto NUNCA se loguea ni se
devuelve: en los errores solo se expone el TIPO de excepcion, jamas su mensaje.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret

_PROVIDER_DEFAULTS = {
    "microsoft": ("smtp.office365.com", 587),
    "gmail": ("smtp.gmail.com", 587),
}
_SMTP_TIMEOUT = 15


def _resolve_host_port(
    provider: str, smtp_host: str | None, smtp_port: int | None
) -> tuple[str, int]:
    """Resuelve host/puerto: microsoft/gmail tienen default; smtp los exige (ValueError)."""
    if provider in _PROVIDER_DEFAULTS:
        d_host, d_port = _PROVIDER_DEFAULTS[provider]
        return (smtp_host or d_host, smtp_port or d_port)
    if not smtp_host or not smtp_port:
        raise ValueError("provider 'smtp' requiere smtp_host y smtp_port")
    return (smtp_host, smtp_port)


def _hdr(value: str | None) -> str:
    """Sanea un valor de header: quita CR/LF (defensa anti-inyeccion de cabeceras)."""
    return (value or "").replace("\r", " ").replace("\n", " ").strip()


async def resolve_default_provider(
    db: AsyncSession, org_id: int, client_id: int | None
) -> dict | None:
    """Devuelve el proveedor default ACTIVO del ambito, o None.

    Prioridad: default activo del cliente (si client_id) -> default activo de
    nivel org (client_id IS NULL). Devuelve la fila como dict o None.
    """
    cols = (
        "id, organization_id, client_id, provider, from_email, from_name, "
        "smtp_host, smtp_port, smtp_user, secret_encrypted, is_default, is_active"
    )
    if client_id is not None:
        row = (await db.execute(text(
            f"SELECT {cols} FROM email_providers "
            "WHERE organization_id=:org AND client_id=:cid "
            "AND is_default = true AND is_active = true LIMIT 1"
        ), {"org": org_id, "cid": client_id})).mappings().first()
        if row is not None:
            return dict(row)
    row = (await db.execute(text(
        f"SELECT {cols} FROM email_providers "
        "WHERE organization_id=:org AND client_id IS NULL "
        "AND is_default = true AND is_active = true LIMIT 1"
    ), {"org": org_id})).mappings().first()
    return dict(row) if row is not None else None


async def send_email(
    db: AsyncSession,
    org_id: int,
    client_id: int | None,
    to_email: str,
    subject: str,
    body_html: str,
    to_name: str | None = None,
) -> dict:
    """Envia un email por el proveedor default del ambito. BEST-EFFORT.

    Resuelve el proveedor (cliente -> org); si no hay, ok=False. Resuelve
    host/puerto, descifra el secreto y envia con smtplib (SMTP_SSL en 465,
    STARTTLS en 587). Todo el cuerpo va en try/except: cualquier fallo (BD,
    descifrado, SMTP) degrada a {"ok": False, ...} con SOLO el tipo de error.
    El secreto NUNCA se loguea ni se devuelve.
    """
    try:
        prov = await resolve_default_provider(db, org_id, client_id)
        if prov is None:
            return {"ok": False, "detail": "sin proveedor de email configurado"}
        if not prov["secret_encrypted"]:
            return {"ok": False, "detail": "el proveedor no tiene secreto configurado"}

        try:
            host, port = _resolve_host_port(
                prov["provider"], prov["smtp_host"], prov["smtp_port"]
            )
        except ValueError:
            return {"ok": False, "detail": "smtp_host/smtp_port no configurados"}

        login_user = prov["smtp_user"] or prov["from_email"]
        from_email = prov["from_email"]
        from_name = prov["from_name"]

        try:
            secret = decrypt_secret(prov["secret_encrypted"])
        except Exception:  # noqa: BLE001 - llave/token invalidos; nunca filtra el secreto
            return {"ok": False, "detail": "secreto ilegible"}

        msg = EmailMessage()
        msg["From"] = (f"{_hdr(from_name)} <{from_email}>"
                       if from_name else from_email)
        msg["To"] = (f"{_hdr(to_name)} <{to_email}>" if to_name else to_email)
        msg["Subject"] = _hdr(subject)
        msg.set_content(
            "Este mensaje requiere un cliente con soporte HTML para verse correctamente."
        )
        msg.add_alternative(body_html, subtype="html")

        try:
            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT,
                                      context=ssl.create_default_context()) as srv:
                    srv.login(login_user, secret)
                    srv.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT) as srv:
                    srv.ehlo()
                    if port == 587:
                        srv.starttls(context=ssl.create_default_context())
                        srv.ehlo()
                    srv.login(login_user, secret)
                    srv.send_message(msg)
        except Exception as e:  # noqa: BLE001 - best-effort; solo el TIPO, nunca el secreto
            return {"ok": False, "detail": f"fallo de envio: {type(e).__name__}"}

        return {"ok": True, "detail": "enviado",
                "provider": prov["provider"], "from": from_email}
    except Exception as e:  # noqa: BLE001 - blindaje total best-effort (incl. errores de BD)
        return {"ok": False, "detail": f"error: {type(e).__name__}"}
