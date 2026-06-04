"""Onboarding atomico (patron Saga) — modelo PREPAGO.

Contrato real (docs/01-admin-financiera-integracion-cores.md):
  - Finanzas-Core y Centro de Mensajes son COMPARTIDOS y multi-tenant (resuelven
    el tenant desde la API key). NO se crea una "cuenta" por cliente en el alta,
    y NO se emiten API keys por cliente (son de bootstrap SQL).
  - El unico recurso que se provisiona por cliente en el alta es la WALLET del
    Medidor, que es la fuente de verdad del saldo prepago.

Alta de cliente (prepago):
  1) INSERT clients en BD local (status='active')
  2) crear wallet en el Medidor con external_user_id = f"client-{client_id}";
     guardar el wallet id devuelto en clients.medidor_account_id
  3) crear suscripcion al plan inicial
  4) crear user titular (cliente_titular) con password temporal

`external_user_id` canonico = f"client-{client_id}". Se guarda tambien en
hub_account_id / finanzas_account_id / messages_account_id como referencia del
external_user_id del cliente en cada core compartido (es el mismo string en
todos: esos cores no asignan un id propio por cliente).

Compensacion de la Saga: si algo falla tras crear la wallet ->
  - best-effort medidor.delete_wallet(wallet_id) (via _safe)
  - rollback de la transaccion local
  - registra falla en audit_log ('onboard_failed')
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_event
from app.core.database import SessionLocal
from app.core.clients.medidor_client import MedidorClient
from app.core.password import hash_password

log = logging.getLogger("onboarding")


@dataclass(slots=True)
class OnboardClientPayload:
    legal_name: str
    trade_name: str | None
    rfc: str
    cfdi_use: str
    tax_regime: str
    zip_code: str
    billing_email: str
    contact_phone: str | None
    plan_code: str
    titular_full_name: str
    titular_email: str


@dataclass(slots=True)
class OnboardResult:
    client_id: int
    user_id: int
    temp_password: str
    wallet_id: str  # id de la wallet prepago creada en el Medidor


class OnboardingError(RuntimeError):
    pass


async def onboard_client(
    db: AsyncSession,
    payload: OnboardClientPayload,
    *,
    actor_user_id: int,
    actor_ip: str | None,
    request_id: str | None,
) -> OnboardResult:
    # plan existe?
    row = await db.execute(
        text("SELECT id FROM plans WHERE code = :c AND is_active"),
        {"c": payload.plan_code},
    )
    plan = row.first()
    if not plan:
        raise OnboardingError(f"plan '{payload.plan_code}' no existe o esta inactivo")
    plan_id = plan[0]

    # 1) INSERT clients
    row = await db.execute(text("""
        INSERT INTO clients
          (legal_name, trade_name, rfc, cfdi_use, tax_regime, zip_code,
           billing_email, contact_phone, status)
        VALUES (:ln, :tn, :rfc, :cu, :tr, :zp, :be, :cp, 'active')
        RETURNING id
    """), {
        "ln": payload.legal_name, "tn": payload.trade_name, "rfc": payload.rfc,
        "cu": payload.cfdi_use, "tr": payload.tax_regime, "zp": payload.zip_code,
        "be": payload.billing_email, "cp": payload.contact_phone,
    })
    client_id = row.scalar_one()

    # external_user_id canonico del cliente en todos los cores compartidos
    external_user_id = f"client-{client_id}"

    # 2) crear wallet prepago en el Medidor (unico recurso provisionado por cliente)
    medidor = MedidorClient()
    wallet_id: str | None = None
    try:
        wallet = await medidor.create_wallet(
            external_user_id=external_user_id,
            metadata={"caf_client_id": client_id, "razon_social": payload.legal_name},
        )
        wallet_id = wallet["id"]
    except Exception as e:
        # nada externo que compensar todavia (la wallet no se creo); rollback local
        await medidor.close()
        await db.rollback()
        await _persist_failure_audit(
            actor_user_id=actor_user_id, actor_ip=actor_ip,
            client_id=client_id, request_id=request_id,
            new_values={"error": str(e), "stage": "create_wallet"},
        )
        raise OnboardingError(f"falla creando wallet en medidor: {e}") from e

    # a partir de aqui, cualquier fallo compensa la wallet ya creada
    try:
        # 3) actualizar referencias externas en clients
        #    (medidor_account_id = wallet id; el resto = external_user_id de
        #     referencia en cada core compartido, mismo string en todos)
        await db.execute(text("""
            UPDATE clients SET
              medidor_account_id  = :wallet,
              hub_account_id      = :ext,
              finanzas_account_id = :ext,
              messages_account_id = :ext,
              updated_at = now()
            WHERE id = :id
        """), {"wallet": wallet_id, "ext": external_user_id, "id": client_id})

        # 4) suscripcion inicial al plan
        await db.execute(text("""
            INSERT INTO subscriptions (client_id, plan_id, status, started_at)
            VALUES (:cid, :pid, 'active', CURRENT_DATE)
        """), {"cid": client_id, "pid": plan_id})

        # 5) user titular con password temporal
        temp_pw = _gen_temp_password()
        pw_hash = hash_password(temp_pw)
        row = await db.execute(text("""
            INSERT INTO users (email, password_hash, full_name, is_active, is_internal, client_id)
            VALUES (:e, :h, :n, TRUE, FALSE, :cid)
            RETURNING id
        """), {"e": payload.titular_email, "h": pw_hash,
                "n": payload.titular_full_name, "cid": client_id})
        user_id = row.scalar_one()

        await db.execute(text("""
            INSERT INTO user_roles (user_id, role_id)
            SELECT :uid, id FROM roles WHERE code = 'cliente_titular'
        """), {"uid": user_id})
    except Exception as e:
        await _compensate(medidor, wallet_id=wallet_id, client_id=client_id, error=str(e))
        await db.rollback()
        await _persist_failure_audit(
            actor_user_id=actor_user_id, actor_ip=actor_ip,
            client_id=client_id, request_id=request_id,
            new_values={"error": str(e), "stage": "local_provisioning",
                        "wallet_id": wallet_id},
        )
        raise OnboardingError(f"falla en provisioning local: {e}") from e

    await medidor.close()

    return OnboardResult(
        client_id=client_id,
        user_id=user_id,
        temp_password=temp_pw,
        wallet_id=wallet_id,
    )


# ---------------------------------------------------------------------
# audit de fallo (transaccion independiente que SI se confirma)
# ---------------------------------------------------------------------


async def _persist_failure_audit(
    *,
    actor_user_id: int,
    actor_ip: str | None,
    client_id: int,
    request_id: str | None,
    new_values: dict[str, Any],
) -> None:
    """Registra 'onboard_failed' en una sesion/transaccion propia y la confirma.

    La sesion `db` del request ya hizo rollback (y `get_db` volvera a hacer
    rollback al propagar el error), por lo que un INSERT en ella se perderia.
    Aqui abrimos una sesion limpia, insertamos el evento y hacemos commit
    explicito para que la falla quede auditada de forma inmutable.
    """
    try:
        async with SessionLocal() as audit_db:
            await write_event(
                audit_db, actor_user_id=actor_user_id, actor_ip=actor_ip,
                entity_type="clients", entity_id=client_id,
                action="onboard_failed", new_values=new_values,
                request_id=request_id,
            )
            await audit_db.commit()
    except Exception as e:  # nunca enmascarar el error original del onboarding
        log.error("onboard_failed_audit_persist_error", extra={"error": str(e)})


# ---------------------------------------------------------------------
# compensacion
# ---------------------------------------------------------------------


async def _compensate(
    medidor: MedidorClient, *, wallet_id: str | None, client_id: int, error: str
) -> None:
    log.warning(
        "onboarding_compensate",
        extra={"client_id": client_id, "wallet_id": wallet_id, "error": error},
    )
    if wallet_id:
        await _safe(medidor.delete_wallet(wallet_id), "medidor")
    await medidor.close()


async def _safe(coro: Any, core: str) -> None:
    try:
        await coro
    except Exception as e:
        log.error("compensate_failed", extra={"core": core, "error": str(e)})


def _gen_temp_password() -> str:
    # 16 chars URL-safe (~96 bits)
    return secrets.token_urlsafe(12)
