"""Webhooks entrantes: PAC y hub-pasarelas.

Cada webhook verifica firma HMAC antes de procesar y registra el evento.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_event
from app.core.config import get_settings
from app.core.database import get_db

log = logging.getLogger("webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_hmac(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


# ---------------------------------------------------------------------
# PAC
# ---------------------------------------------------------------------


@router.post("/pac")
async def pac_webhook(
    request: Request,
    x_signature: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    body = await request.body()
    secret = get_settings().PAC_API_SECRET.get_secret_value()
    if not _verify_hmac(secret, body, x_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "firma invalida")
    payload = await request.json()
    event = payload.get("event")
    invoice_id = payload.get("invoice_id")
    uuid_cfdi = payload.get("uuid")

    if event == "stamped":
        await db.execute(text("""
            UPDATE invoices SET status='stamped', uuid_cfdi=:u, stamped_at=now()
            WHERE id=:id AND status='pending_stamp'
        """), {"id": invoice_id, "u": uuid_cfdi})
    elif event == "stamp_failed":
        await db.execute(text("""
            UPDATE invoices SET status='failed' WHERE id=:id AND status='pending_stamp'
        """), {"id": invoice_id})
    else:
        log.warning("pac_webhook_unknown_event", extra={"event": event})

    await write_event(
        db, actor_user_id=None,
        actor_ip=request.client.host if request.client else None,
        entity_type="invoices", entity_id=invoice_id,
        action=f"pac.{event}", new_values=payload,
        request_id=getattr(request.state, "request_id", None),
    )
    return {"status": "ok"}


# ---------------------------------------------------------------------
# hub-pasarelas: pago recibido
# ---------------------------------------------------------------------


@router.post("/hub-payment-paid")
async def hub_payment_paid(
    request: Request,
    x_signature: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    body = await request.body()
    secret = get_settings().HUB_API_KEY.get_secret_value()
    if not _verify_hmac(secret, body, x_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "firma invalida")
    payload = await request.json()

    hub_account_id = payload["account_id"]
    amount_cents = int(payload["amount_cents"])
    hub_payment_id = payload["payment_id"]
    invoice_id = payload.get("metadata", {}).get("invoice_id")

    # ubicar cliente local por hub_account_id
    row = (await db.execute(
        text("SELECT id FROM clients WHERE hub_account_id = :h"),
        {"h": hub_account_id},
    )).first()
    if not row:
        raise HTTPException(404, "cliente no encontrado para esta cuenta hub")
    client_id = row[0]

    # idempotencia: si ya registramos este hub_payment_id, salir OK
    existing = (await db.execute(
        text("SELECT 1 FROM payments WHERE hub_payment_id = :h"),
        {"h": hub_payment_id},
    )).first()
    if existing:
        return {"status": "duplicate_ignored"}

    await db.execute(text("""
        INSERT INTO payments (client_id, invoice_id, amount_cents, currency,
                              method, hub_payment_id, received_at)
        VALUES (:c, :i, :a, 'MXN', 'hub_card', :h, now())
    """), {"c": client_id, "i": invoice_id, "a": amount_cents, "h": hub_payment_id})

    # si liquida una factura, marcarla paid
    if invoice_id:
        await db.execute(text("""
            UPDATE invoices SET status='paid', paid_at=now()
            WHERE id=:id AND status IN ('stamped','pending_stamp')
              AND total_cents <= (
                SELECT COALESCE(sum(amount_cents),0) FROM payments WHERE invoice_id=:id
              )
        """), {"id": invoice_id})

    await write_event(
        db, actor_user_id=None,
        actor_ip=request.client.host if request.client else None,
        entity_type="payments", entity_id=None,
        action="hub.paid", new_values=payload,
        request_id=getattr(request.state, "request_id", None),
    )
    return {"status": "ok"}
