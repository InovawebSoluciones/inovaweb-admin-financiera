"""Flujo prepago del CAF: cargo en el Hub -> webhook -> acreditar wallet.

Modelo PREPAGO (CLAUDE.md, docs/01-admin-financiera-integracion-cores.md):
  contratar plan / recargar = cargo en el Hub (gateway conekta, sandbox) que,
  al confirmarse via webhook payment.paid, acredita saldo en la WALLET del
  Medidor (fuente de verdad del saldo) y deja el asiento en Finanzas-Core.

Responsabilidades de este modulo:
  - `initiate_charge`  : crea el intento de cobro en el Hub (plan o recarga) y
    deja traza en audit_log (action=`recharge.initiated`). Devuelve el intent
    del Hub (checkout_url, etc.) para redirigir al cliente.
  - `process_paid_event`: procesa el evento `payment.paid` ya VERIFICADO por el
    router. Es idempotente (UNIQUE logico por hub_transaction_id en payments) y
    NUNCA pierde el pago: si el credito al Medidor falla, audita el fallo en una
    transaccion propia y deja el evento como recuperable (reintento del Hub o
    del worker lo vuelve a procesar sin doble acreditacion gracias al
    request_id determinista del Medidor).

Convenciones firmes:
  - Centavos enteros (BIGINT). Sin floats.
  - Idempotencia determinista:
      * Hub   -> hub_transaction_id (UNIQUE logico en payments.hub_payment_id)
      * Medidor -> request_id = f"caf-recharge-{recharge_id}"
      * Finanzas -> source_ref = f"caf-recharge-{recharge_id}"
  - Append-only en payments: la fila SE INSERTA ya como pago confirmado; no hay
    UPDATE de estado (lo prohiben los triggers de 002_security_constraints.sql).
    El "intento pendiente" vive en audit_log (recharge.initiated), no en una
    fila mutable.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_event
from app.core.clients.finanzas_client import FinanzasClient
from app.core.clients.hub_client import HubClient
from app.core.clients.medidor_client import MedidorClient
from app.core.clients.messages_client import MessagesClient
from app.core.config import get_settings
from app.core.database import SessionLocal

log = logging.getLogger("prepago")

# purposes que en el modelo prepago acreditan saldo en la wallet del Medidor
_WALLET_PURPOSES = frozenset({"plan_purchase", "wallet_recharge"})

# H2 (hardening #19/22): reintentos con backoff exponencial del paso de credito
# (descuento/acreditacion de saldo en el Medidor), que puede fallar por red.
# tenacity NO es dependencia del proyecto (ver pyproject.toml) -> loop propio
# con asyncio.sleep, sin agregar dependencias nuevas.
_CREDIT_MAX_ATTEMPTS = 3
_CREDIT_BACKOFF_SEC = (1.0, 2.0, 4.0)  # delay TRAS el intento i (1s, 2s, 4s)

_T = TypeVar("_T")


async def _retry_async(
    op: Callable[[], Awaitable[_T]],
    *,
    max_attempts: int = _CREDIT_MAX_ATTEMPTS,
    backoff_sec: tuple[float, ...] = _CREDIT_BACKOFF_SEC,
    op_name: str = "op",
) -> _T:
    """Ejecuta `op` con reintentos y backoff exponencial (H2).

    Reintenta hasta `max_attempts` veces ante cualquier excepcion. Entre el
    intento i y el i+1 espera `backoff_sec[i]` segundos (1s/2s/4s por defecto).
    Si se agotan los intentos, re-lanza la ultima excepcion para que el llamador
    la audite/marque como recuperable (`pending_retry`).

    Args:
        op: corutina sin argumentos que realiza la operacion de red.
        max_attempts: numero maximo de intentos (incluido el primero).
        backoff_sec: delays entre intentos; el ultimo se reusa si faltan.
        op_name: nombre para el log estructurado.

    Returns:
        El resultado de `op()` en el primer intento exitoso.

    Raises:
        Exception: la ultima excepcion si se agotan todos los intentos.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await op()
        except Exception as e:  # noqa: BLE001 - se re-lanza tras agotar intentos
            last_exc = e
            if attempt >= max_attempts:
                break
            delay = backoff_sec[min(attempt - 1, len(backoff_sec) - 1)]
            log.warning(
                "retry_backoff",
                extra={"op": op_name, "attempt": attempt,
                       "max_attempts": max_attempts, "delay_sec": delay,
                       "error": str(e)},
            )
            await asyncio.sleep(delay)
    assert last_exc is not None  # invariante: el loop solo sale por exito o aqui
    raise last_exc


class PrepagoError(Exception):
    """Fallo recuperable en el procesamiento del pago (NO perder el pago)."""


@dataclass(slots=True)
class PaidResult:
    status: str  # 'credited' | 'duplicate_ignored' | 'invoice_paid' | 'ignored'
    recharge_id: str | None = None
    payment_inserted: bool = False
    credited: bool = False


# ---------------------------------------------------------------------
# 1) iniciar compra de plan / recarga (CAF -> Hub)
# ---------------------------------------------------------------------


async def initiate_charge(
    db: AsyncSession,
    *,
    client: dict[str, Any],
    amount_cents: int,
    purpose: str,
    description: str,
    plan_code: str | None = None,
    actor_user_id: int | None,
    actor_ip: str | None,
    request_id: str | None,
    hub: HubClient | None = None,
) -> dict[str, Any]:
    """Crea el intento de cobro en el Hub (gateway conekta).

    `client` es la fila de `clients` (dict). El external_user_id de la wallet es
    `client['hub_account_id']`. `purpose` in {plan_purchase, wallet_recharge}.
    Persiste el intento en audit_log (recharge.initiated) -- no en payments,
    porque payments es append-only y solo registra pagos CONFIRMADOS.
    """
    if amount_cents <= 0:
        raise PrepagoError("amount_cents debe ser > 0")
    if purpose not in _WALLET_PURPOSES:
        raise PrepagoError(f"purpose no soportado para cargo prepago: {purpose}")
    external_user_id = client.get("hub_account_id")
    if not external_user_id:
        raise PrepagoError("cliente sin cuenta provisionada en el hub")

    metadata: dict[str, Any] = {
        "purpose": purpose,
        "caf_client_id": client["id"],
        "return_url": f"https://{get_settings().PORTAL_DOMAIN}/portal/dashboard",
    }
    if plan_code:
        metadata["plan_code"] = plan_code

    own_hub = hub is None
    hub = hub or HubClient()
    try:
        intent = await hub.charge(
            external_user_id=external_user_id,
            amount_cents=amount_cents,
            description=description,
            metadata=metadata,
            gateway=get_settings().HUB_GATEWAY,
        )
    finally:
        if own_hub:
            await hub.close()

    # recharge_id determinista: el id del intento devuelto por el Hub. Es el
    # mismo que volvera en el webhook (payment_id / transaction_id), por lo que
    # request_id/source_ref quedan ligados al intento desde el inicio.
    recharge_id = (
        intent.get("hub_transaction_id")
        or intent.get("transaction_id")
        or intent.get("payment_id")
        or intent.get("id")
    )
    metadata_audit = {**metadata, "amount_cents": amount_cents,
                      "recharge_id": recharge_id, "intent": intent}
    await write_event(
        db, actor_user_id=actor_user_id, actor_ip=actor_ip,
        entity_type="payments", entity_id=None,
        action="recharge.initiated", new_values=metadata_audit,
        request_id=request_id,
    )
    return intent


# ---------------------------------------------------------------------
# 2) procesar webhook payment.paid (ya verificado por el router)
# ---------------------------------------------------------------------


def _parse_amount_cents(payload: dict[str, Any]) -> int | None:
    """FIX-7: parseo robusto del monto (centavos enteros).

    Tolera `amount_cents` o `amount`. Si esta presente pero no es numerico
    entero -> PrepagoError (no 500 sin controlar). Si falta del todo -> None
    (los branches downstream lo rechazan con su propio mensaje).
    """
    for key in ("amount_cents", "amount"):
        if payload.get(key) is None:
            continue
        raw = payload[key]
        try:
            # bool es subclase de int; floats no enteros se rechazan.
            if isinstance(raw, bool):
                raise ValueError("bool no es monto valido")
            if isinstance(raw, float) and not raw.is_integer():
                raise ValueError("monto con fraccion de centavo")
            return int(raw)
        except (TypeError, ValueError) as e:
            raise PrepagoError(f"amount invalido en webhook ({key}={raw!r}): {e}") from e
    return None


def extract_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Normaliza el payload del webhook del Hub a un dict canonico.

    Tolera variantes de nombre del id de transaccion (transaction_id /
    payment_id / id) para que la idempotencia no dependa del wording exacto.
    """
    meta = payload.get("metadata") or {}
    hub_txn_id = (
        payload.get("transaction_id")
        or payload.get("payment_id")
        or payload.get("id")
    )
    return {
        "hub_transaction_id": hub_txn_id,
        "account_id": payload.get("account_id") or payload.get("external_user_id"),
        "amount_cents": _parse_amount_cents(payload),
        "purpose": meta.get("purpose"),
        "plan_code": meta.get("plan_code"),
        "invoice_id": meta.get("invoice_id"),
        "occurred_at": payload.get("occurred_at") or payload.get("paid_at"),
        "metadata": meta,
    }


async def process_paid_event(
    db: AsyncSession,
    payload: dict[str, Any],
    *,
    actor_ip: str | None,
    request_id: str | None,
) -> PaidResult:
    """Procesa un evento payment.paid VERIFICADO. Idempotente.

    El router YA valido la firma HMAC y la ventana de timestamp antes de
    invocar esta funcion. Aqui no se hace ningun I/O sensible sin esa garantia.
    """
    ev = extract_event(payload)
    hub_txn_id = ev["hub_transaction_id"]
    if not hub_txn_id:
        raise PrepagoError("evento sin id de transaccion del hub")

    # NOTA (FIX-1): la idempotencia NO se decide aqui con un SELECT (race entre
    # webhooks concurrentes). Se reclama el pago con INSERT ... ON CONFLICT
    # DO NOTHING contra el indice unico parcial uq_payments_hub (004); el primer
    # webhook gana la fila y el resto se trata como replay (duplicate_ignored).
    purpose = ev["purpose"]

    # ---- branch fuera del piloto: pago de factura ----
    if purpose == "invoice_payment":
        return await _process_invoice_payment(
            db, ev, actor_ip=actor_ip, request_id=request_id
        )

    if purpose not in _WALLET_PURPOSES:
        # purpose desconocido: registrar y salir sin tocar saldo
        await write_event(
            db, actor_user_id=None, actor_ip=actor_ip,
            entity_type="payments", entity_id=None,
            action="hub.paid.ignored", new_values=payload,
            request_id=request_id,
        )
        log.warning("hub_payment_unknown_purpose", extra={"purpose": purpose})
        return PaidResult(status="ignored", recharge_id=str(hub_txn_id))

    # ---- branch prepago: acreditar saldo en el Medidor ----
    return await _process_wallet_credit(
        db, ev, payload, actor_ip=actor_ip, request_id=request_id
    )


async def _process_wallet_credit(
    db: AsyncSession,
    ev: dict[str, Any],
    payload: dict[str, Any],
    *,
    actor_ip: str | None,
    request_id: str | None,
) -> PaidResult:
    hub_txn_id = ev["hub_transaction_id"]
    amount_cents = ev["amount_cents"]
    if amount_cents is None or amount_cents <= 0:
        raise PrepagoError("evento sin amount_cents valido")

    # ---- H5: tope de monto (defensa en profundidad) ----
    # El portal ya valida el techo al iniciar la recarga, pero el webhook es un
    # punto de entrada independiente: un monto manipulado por encima del tope se
    # rechaza aqui ANTES de acreditar (no confiar solo en el cap del router).
    max_recarga = get_settings().MAX_RECARGA_CENTS
    if amount_cents > max_recarga:
        await _persist_failure_audit(
            actor_ip=actor_ip, request_id=request_id,
            action="hub.paid.rejected",
            new_values={"reason": "monto supera el tope de recarga",
                        "hub_transaction_id": hub_txn_id,
                        "amount_cents": amount_cents,
                        "max_recarga_cents": max_recarga},
        )
        log.warning("hub_payment_over_cap",
                    extra={"hub_transaction_id": hub_txn_id,
                           "amount_cents": amount_cents, "cap": max_recarga})
        raise PrepagoError(
            f"monto {amount_cents} supera el tope de recarga {max_recarga}"
        )

    # ---- FIX-2: correlacionar con el intento local (recharge.initiated) ----
    await _correlate_or_reject(
        db, ev, actor_ip=actor_ip, request_id=request_id
    )

    # localizar cliente por hub_account_id
    row = (await db.execute(
        text("SELECT id, medidor_account_id, finanzas_account_id, billing_email, "
             "       legal_name FROM clients WHERE hub_account_id = :h"),
        {"h": ev["account_id"]},
    )).mappings().first()
    if not row:
        raise PrepagoError(
            f"cliente no encontrado para cuenta hub {ev['account_id']}"
        )
    client_id = row["id"]
    wallet_id = row["medidor_account_id"]
    if not wallet_id:
        raise PrepagoError(f"cliente {client_id} sin wallet en el medidor")

    recharge_id = str(hub_txn_id)
    req_id = f"caf-recharge-{recharge_id}"
    occurred_at = ev["occurred_at"] or _now_iso()
    reason = "plan_purchase" if ev["purpose"] == "plan_purchase" else "wallet_recharge"

    # ---- FIX-1: reclamar el pago a nivel BD (idempotencia atomica) ----
    claimed = await db.execute(text("""
        INSERT INTO payments (client_id, invoice_id, amount_cents, currency,
                              method, hub_payment_id, received_at, notes)
        VALUES (:c, NULL, :a, 'MXN', 'hub_card', :h, now(), :n)
        ON CONFLICT (hub_payment_id) DO NOTHING
    """), {"c": client_id, "a": amount_cents, "h": hub_txn_id,
           "n": f"prepago {ev['purpose']} recharge_id={recharge_id}"})
    if claimed.rowcount == 0:
        log.info("hub_payment_replay_ignored",
                 extra={"hub_transaction_id": hub_txn_id})
        return PaidResult(status="duplicate_ignored", recharge_id=recharge_id)

    # 1) acreditar saldo en el Medidor (idempotente por request_id determinista).
    #    H2: el credito puede fallar por red -> reintentos con backoff exponencial
    #    (3 intentos, 1s/2s/4s). El request_id determinista hace que un reintento
    #    NO produzca doble acreditacion (idempotencia del Medidor). Si se agotan
    #    los reintentos, se marca la operacion como pending_retry (audit
    #    hub.paid.pending_retry) y se deja recuperable (502 -> el Hub/worker la
    #    re-procesa; el INSERT del pago ya reclamo la fila, no se duplica).
    medidor = MedidorClient()
    try:
        await _retry_async(
            lambda: medidor.credit(
                wallet_id,
                amount_cents=amount_cents,
                request_id=req_id,
                reason=reason,
                metadata={
                    "caf_client_id": client_id,
                    "hub_transaction_id": hub_txn_id,
                    "plan_code": ev.get("plan_code"),
                },
            ),
            op_name="medidor_credit",
        )
    except Exception as e:
        await _persist_failure_audit(
            actor_ip=actor_ip, request_id=request_id,
            action="hub.paid.pending_retry",
            new_values={"error": str(e), "stage": "medidor_credit",
                        "status": "pending_retry",
                        "attempts": _CREDIT_MAX_ATTEMPTS,
                        "hub_transaction_id": hub_txn_id, "request_id": req_id,
                        "caf_client_id": client_id, "amount_cents": amount_cents},
        )
        log.error(
            "medidor_credit_pending_retry",
            extra={"hub_transaction_id": hub_txn_id, "caf_client_id": client_id,
                   "request_id": req_id, "amount_cents": amount_cents},
        )
        raise PrepagoError(f"falla acreditando saldo en el medidor: {e}") from e
    finally:
        await medidor.close()

    # 2) asiento en Finanzas-Core (idempotente por source_ref determinista)
    finanzas = FinanzasClient()
    try:
        await finanzas.post_entry(
            source_slug="hub",
            source_ref=req_id,
            direction="credit",
            amount_cents=amount_cents,
            occurred_at=occurred_at,
            description=f"Recarga prepago cliente {client_id}",
            meta={"caf_client_id": client_id, "hub_transaction_id": hub_txn_id},
        )
    except Exception as e:
        await _persist_failure_audit(
            actor_ip=actor_ip, request_id=request_id,
            new_values={"error": str(e), "stage": "finanzas_post_entry",
                        "hub_transaction_id": hub_txn_id,
                        "caf_client_id": client_id, "amount_cents": amount_cents},
        )
        raise PrepagoError(f"falla registrando asiento en finanzas: {e}") from e
    finally:
        await finanzas.close()

    # 3) el pago YA quedo reclamado (INSERT idempotente arriba). No se re-inserta.

    # 4) notificar al cliente (best-effort; el pago ya esta confirmado)
    await _notify_paid(
        client_id=client_id,
        billing_email=row["billing_email"],
        legal_name=row["legal_name"],
        amount_cents=amount_cents,
        recharge_id=recharge_id,
    )

    # 5) audit
    await write_event(
        db, actor_user_id=None, actor_ip=actor_ip,
        entity_type="payments", entity_id=None,
        action="hub.paid.credited",
        new_values={"hub_transaction_id": hub_txn_id, "caf_client_id": client_id,
                    "amount_cents": amount_cents, "request_id": req_id,
                    "purpose": ev["purpose"]},
        request_id=request_id,
    )
    return PaidResult(status="credited", recharge_id=recharge_id,
                      payment_inserted=True, credited=True)


async def _process_invoice_payment(
    db: AsyncSession,
    ev: dict[str, Any],
    *,
    actor_ip: str | None,
    request_id: str | None,
) -> PaidResult:
    """Branch fuera del piloto: el cobro liquida una factura.

    Se deja implementado pero sin prioridad (CFDI es Fase 4 / TASK-20). Inserta
    el pago y marca la factura paid si queda totalmente cubierta.
    """
    hub_txn_id = ev["hub_transaction_id"]
    amount_cents = ev["amount_cents"]
    invoice_id = ev["invoice_id"]
    if amount_cents is None or amount_cents <= 0:
        raise PrepagoError("evento sin amount_cents valido")

    row = (await db.execute(
        text("SELECT client_id FROM invoices WHERE id = :id"),
        {"id": invoice_id},
    )).first()
    if not row:
        raise PrepagoError(f"factura {invoice_id} no encontrada")
    client_id = row[0]

    # FIX-1: reclamar el pago de forma idempotente (uq_payments_hub). Si ya
    # existe el hub_transaction_id -> replay, no re-aplicar contra la factura.
    claimed = await db.execute(text("""
        INSERT INTO payments (client_id, invoice_id, amount_cents, currency,
                              method, hub_payment_id, received_at)
        VALUES (:c, :i, :a, 'MXN', 'hub_card', :h, now())
        ON CONFLICT (hub_payment_id) DO NOTHING
    """), {"c": client_id, "i": invoice_id, "a": amount_cents, "h": hub_txn_id})
    if claimed.rowcount == 0:
        log.info("hub_payment_replay_ignored",
                 extra={"hub_transaction_id": hub_txn_id})
        return PaidResult(status="duplicate_ignored", recharge_id=str(hub_txn_id))

    await db.execute(text("""
        UPDATE invoices SET status='paid', paid_at=now()
        WHERE id=:id AND status IN ('stamped','pending_stamp')
          AND total_cents <= (
            SELECT COALESCE(sum(amount_cents),0) FROM payments WHERE invoice_id=:id
          )
    """), {"id": invoice_id})

    await write_event(
        db, actor_user_id=None, actor_ip=actor_ip,
        entity_type="invoices", entity_id=invoice_id,
        action="hub.paid.invoice",
        new_values={"hub_transaction_id": hub_txn_id, "amount_cents": amount_cents},
        request_id=request_id,
    )
    return PaidResult(status="invoice_paid", recharge_id=str(hub_txn_id),
                      payment_inserted=True)


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


async def _correlate_or_reject(
    db: AsyncSession,
    ev: dict[str, Any],
    *,
    actor_ip: str | None,
    request_id: str | None,
) -> None:
    """FIX-2: valida el evento contra el intento local `recharge.initiated`.

    initiate_charge dejo en audit_log (entity_type=payments, action=
    recharge.initiated) un new_values con recharge_id, purpose y amount_cents.
    Aqui se busca el intento por recharge_id (== hub_transaction_id) y se exige
    que purpose y amount_cents coincidan con lo del evento. Si no hay intento o
    no coincide -> audita hub.paid.rejected y lanza PrepagoError (no acredita).
    """
    hub_txn_id = ev["hub_transaction_id"]
    recharge_id = str(hub_txn_id)
    intent = (await db.execute(text("""
        SELECT new_values FROM audit_log
        WHERE action = 'recharge.initiated'
          AND new_values->>'recharge_id' = :rid
        ORDER BY id DESC LIMIT 1
    """), {"rid": recharge_id})).mappings().first()

    reject_reason: str | None = None
    if not intent:
        reject_reason = "sin intento recharge.initiated para el recharge_id"
    else:
        nv = intent["new_values"] or {}
        want_purpose = nv.get("purpose")
        want_amount = nv.get("amount_cents")
        if want_purpose != ev["purpose"]:
            reject_reason = (
                f"purpose no coincide: iniciado={want_purpose!r} "
                f"evento={ev['purpose']!r}"
            )
        elif want_amount is not None and int(want_amount) != int(ev["amount_cents"]):
            reject_reason = (
                f"amount no coincide: iniciado={want_amount} "
                f"evento={ev['amount_cents']}"
            )

    if reject_reason is None:
        return

    await _persist_failure_audit(
        actor_ip=actor_ip, request_id=request_id,
        action="hub.paid.rejected",
        new_values={"reason": reject_reason, "hub_transaction_id": hub_txn_id,
                    "purpose": ev["purpose"], "amount_cents": ev["amount_cents"],
                    "account_id": ev["account_id"]},
    )
    log.warning("hub_payment_rejected",
                extra={"hub_transaction_id": hub_txn_id, "reason": reject_reason})
    raise PrepagoError(f"evento rechazado: {reject_reason}")


async def _notify_paid(
    *,
    client_id: int,
    billing_email: str | None,
    legal_name: str | None,
    amount_cents: int,
    recharge_id: str,
) -> None:
    """Envia el correo de confirmacion (best-effort: no revertir el pago)."""
    if not billing_email:
        return
    s = get_settings()
    messages = MessagesClient()
    try:
        await messages.send_email(
            client_id=str(client_id),
            service_id=s.CAF_MESSAGES_SERVICE_ID,
            template_id=s.CAF_PAGO_CONFIRMADO_TEMPLATE,
            to={"email": billing_email, "name": legal_name or ""},
            variables={
                "monto_mxn": f"{amount_cents / 100:.2f}",
                "amount_cents": amount_cents,
                "recharge_id": recharge_id,
            },
            meta={"caf_client_id": client_id, "recharge_id": recharge_id},
        )
    except Exception as e:  # nunca tumbar el webhook por una notificacion
        log.error("notify_paid_failed",
                  extra={"caf_client_id": client_id, "error": str(e)})
    finally:
        await messages.close()


async def _persist_failure_audit(
    *,
    actor_ip: str | None,
    request_id: str | None,
    new_values: dict[str, Any],
    action: str = "hub.paid.failed",
) -> None:
    """Audita un fallo/rechazo de procesamiento en sesion/transaccion PROPIA.

    Mismo patron que onboarding._persist_failure_audit: la sesion del request
    hara rollback al propagar el error, asi que el evento se persiste en una
    sesion limpia con commit explicito para que el fallo quede auditado.
    `action` permite distinguir hub.paid.failed (I/O recuperable) de
    hub.paid.rejected (correlacion FIX-2 fallida).
    """
    try:
        async with SessionLocal() as audit_db:
            await write_event(
                audit_db, actor_user_id=None, actor_ip=actor_ip,
                entity_type="payments", entity_id=None,
                action=action, new_values=new_values,
                request_id=request_id,
            )
            await audit_db.commit()
    except Exception as e:  # nunca enmascarar el error original
        log.error("hub_paid_failed_audit_persist_error", extra={"error": str(e)})


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
