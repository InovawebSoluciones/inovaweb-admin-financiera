"""Onboarding atomico cross-core (patron Saga).

Alta de cliente:
  1) INSERT clients en BD local (status='active')
  2) crear cuenta en medidor, hub, finanzas, mensajes (en paralelo)
  3) emitir API key admin en cada core (en paralelo)
  4) crear suscripcion al plan inicial
  5) crear user titular (cliente_titular) con password temporal

Si cualquier paso falla -> compensacion:
  - DELETE en los cores que ya respondieron OK
  - rollback de la transaccion local
  - registra falla en audit_log
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_event
from app.core.clients.finanzas_client import FinanzasClient
from app.core.clients.hub_client import HubClient
from app.core.clients.medidor_client import MedidorClient
from app.core.clients.messages_client import MessagesClient
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
    api_keys: dict[str, str]  # core_name -> raw key


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

    # 2) crear cuentas en los 4 cores (en paralelo, con compensacion)
    medidor = MedidorClient()
    hub = HubClient()
    finanzas = FinanzasClient()
    messages = MessagesClient()
    created: dict[str, str] = {}  # core -> account_id

    try:
        med_t = medidor.create_account(legal_name=payload.legal_name, rfc=payload.rfc)
        hub_t = hub.create_account(legal_name=payload.legal_name, rfc=payload.rfc,
                                    email=payload.billing_email)
        fin_t = finanzas.create_account(legal_name=payload.legal_name, rfc=payload.rfc)
        msg_t = messages.create_account(legal_name=payload.legal_name, rfc=payload.rfc)
        med_r, hub_r, fin_r, msg_r = await asyncio.gather(med_t, hub_t, fin_t, msg_t)

        created["medidor"]  = med_r["id"]
        created["hub"]      = hub_r["id"]
        created["finanzas"] = fin_r["id"]
        created["messages"] = msg_r["id"]

        # 3) API keys admin en cada core (paralelo)
        keys_raw = await asyncio.gather(
            medidor.issue_api_key(created["medidor"], "admin-caf"),
            hub.issue_api_key(created["hub"], "admin-caf"),
            finanzas.issue_api_key(created["finanzas"], "admin-caf"),
            messages.issue_api_key(created["messages"], "admin-caf"),
        )
        api_keys = {
            "medidor":  keys_raw[0]["key"],
            "hub":      keys_raw[1]["key"],
            "finanzas": keys_raw[2]["key"],
            "messages": keys_raw[3]["key"],
        }
    except Exception as e:
        await _compensate(created, medidor, hub, finanzas, messages, client_id=client_id, error=str(e))
        await db.rollback()
        await write_event(
            db, actor_user_id=actor_user_id, actor_ip=actor_ip,
            entity_type="clients", entity_id=client_id, action="onboard_failed",
            new_values={"error": str(e), "created_cores": list(created)},
            request_id=request_id,
        )
        raise OnboardingError(f"falla en cores: {e}") from e

    # 4) actualizar IDs externos en clients
    await db.execute(text("""
        UPDATE clients SET
          medidor_account_id  = :m,
          hub_account_id      = :h,
          finanzas_account_id = :f,
          messages_account_id = :s,
          updated_at = now()
        WHERE id = :id
    """), {
        "m": created["medidor"], "h": created["hub"],
        "f": created["finanzas"], "s": created["messages"], "id": client_id,
    })

    # 5) suscripcion inicial
    await db.execute(text("""
        INSERT INTO subscriptions (client_id, plan_id, status, started_at)
        VALUES (:cid, :pid, 'active', CURRENT_DATE)
    """), {"cid": client_id, "pid": plan_id})

    # 6) user titular con password temporal
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

    # cerrar clientes http
    await asyncio.gather(medidor.close(), hub.close(), finanzas.close(), messages.close())

    return OnboardResult(
        client_id=client_id,
        user_id=user_id,
        temp_password=temp_pw,
        api_keys=api_keys,
    )


# ---------------------------------------------------------------------
# compensacion
# ---------------------------------------------------------------------


async def _compensate(
    created: dict[str, str],
    medidor: MedidorClient, hub: HubClient,
    finanzas: FinanzasClient, messages: MessagesClient,
    *,
    client_id: int, error: str,
) -> None:
    log.warning(
        "onboarding_compensate", extra={
            "client_id": client_id, "cores_created": list(created), "error": error
        },
    )
    tasks: list[Any] = []
    if "medidor" in created:
        tasks.append(_safe(medidor.delete_account(created["medidor"]), "medidor"))
    if "hub" in created:
        tasks.append(_safe(hub.delete_account(created["hub"]), "hub"))
    if "finanzas" in created:
        tasks.append(_safe(finanzas.delete_account(created["finanzas"]), "finanzas"))
    if "messages" in created:
        tasks.append(_safe(messages.delete_account(created["messages"]), "messages"))
    if tasks:
        await asyncio.gather(*tasks)
    await asyncio.gather(medidor.close(), hub.close(), finanzas.close(), messages.close())


async def _safe(coro: Any, core: str) -> None:
    try:
        await coro
    except Exception as e:
        log.error("compensate_failed", extra={"core": core, "error": str(e)})


def _gen_temp_password() -> str:
    # 16 chars URL-safe (~96 bits)
    return secrets.token_urlsafe(12)
