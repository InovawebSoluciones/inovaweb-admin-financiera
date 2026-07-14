"""Webhooks entrantes: PAC y hub-pasarelas.

Cada webhook verifica firma HMAC antes de procesar y registra el evento.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_event
from app.core.config import get_settings
from app.core.database import get_db
from app.services.prepago import PrepagoError, process_paid_event

log = logging.getLogger("webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_hmac(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _verify_signed_timestamp(
    secret: str,
    body: bytes,
    signature: str,
    timestamp: str,
    tolerance_sec: int,
    require_timestamp: bool = False,
) -> bool:
    """Verifica firma sobre `<timestamp>.<body>` y la ventana de tiempo."""
    if not timestamp:
        if require_timestamp:
            return False
        return _verify_hmac(secret, body, signature)
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > tolerance_sec:
        return False
    signed = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
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
    x_timestamp: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Webhook payment.paid del Hub (modelo PREPAGO).

    Orden estricto: VALIDAR FIRMA ANTES de cualquier I/O. Solo despues se parsea
    el body y se procesa (acreditar wallet del Medidor / liquidar factura). El
    procesamiento es idempotente por hub_transaction_id (ver services.prepago).
    """
    body = await request.body()
    s = get_settings()
    secret = s.hub_webhook_secret()
    if not _verify_signed_timestamp(
        secret, body, x_signature, x_timestamp, s.HUB_WEBHOOK_TOLERANCE_SEC,
        require_timestamp=(s.ENV == "prod"),
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "firma invalida")

    payload = await request.json()
    actor_ip = request.client.host if request.client else None
    request_id = getattr(request.state, "request_id", None)

    try:
        result = await process_paid_event(
            db, payload, actor_ip=actor_ip, request_id=request_id
        )
    except PrepagoError as e:
        log.error("hub_payment_processing_error", extra={"error": str(e)})
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "error procesando pago (reintentar)"
        ) from e
    return {"status": result.status, "recharge_id": result.recharge_id}
# TASK-15: webhook hub-payment-paid -> services.prepago.process_paid_event.
