"""Ajuste manual de saldo (credito/debito de correccion): /admin/clients/{cid}/adjust.

El rector financiero corrige el saldo prepago de un cliente. Como el libro
`prepaid_ledger` es APPEND-ONLY (los triggers de 002_security_constraints.sql
prohiben UPDATE/DELETE), una correccion es SIEMPRE una entrada NUEVA: nunca se
edita un asiento previo. El signo lo da `kind` ('credit'|'debit'); amount_cents
es SIEMPRE > 0 (CHECK de la tabla). Saldo = vista `v_client_balance`.

Multi-tenant delegado (JWT, no API key): la org ajusta a SUS clientes; el super
tenant (is_platform) puede operar sobre cualquier org pasando ?org=<id>. El
tenant NUNCA viene del body. Idempotencia obligatoria por (client_id,
idempotency_key): un reenvio del mismo ajuste devuelve el saldo actual sin
duplicar el asiento.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles

router = APIRouter(prefix="/admin", tags=["adjustments"])

# escritura restringida a roles internos con permiso de gestion financiera
_WRITE = require_roles("super_admin", "finanzas")


# ---------------------------------------------------------------------
# helpers de aislamiento por organizacion (actor = JWT, no API key)
# ---------------------------------------------------------------------


def _resolve_org(user: CurrentUser, org_param: int | None) -> int:
    """org_param solo lo respeta el super tenant; el resto siempre su propia org."""
    if org_param is not None and user.is_platform:
        return org_param
    return user.organization_id


async def _assert_client_in_org_admin(
    db: AsyncSession, cid: int, org: int, user: CurrentUser
) -> None:
    """Garantiza que el cliente existe y pertenece a `org`.

    NO usa resolve_app_org (eso es para llaves de API app-facing): aqui el actor
    es un usuario JWT. 404 si el cliente no existe; 403 si es de otra org y el
    usuario no es el operador de la plataforma.
    """
    row = (await db.execute(
        text("SELECT organization_id FROM clients WHERE id=:c"), {"c": cid}
    )).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cliente no existe")
    if not (user.is_platform or int(row[0]) == org):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cliente de otra organizacion")


# ---------------------------------------------------------------------
# esquema de entrada
# ---------------------------------------------------------------------


class AdjustBody(BaseModel):
    kind: str               # 'credit' | 'debit'
    amount_cents: int       # SIEMPRE > 0; el signo lo da kind
    reason: str             # motivo de la correccion (obligatorio, auditado en meta)
    idempotency_key: str | None = None


# ---------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------


@router.post("/clients/{cid}/adjust")
async def adjust_balance(
    cid: int,
    body: AdjustBody,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Ajuste manual de saldo: inserta un asiento NUEVO de correccion (append-only).

    1) resuelve la org destino y verifica que el cliente es de esa org.
    2) valida kind/amount_cents/reason.
    3) toma advisory lock por cliente (serializa ajustes/cargos concurrentes).
    4) idempotencia: si la idempotency_key ya existe para el cliente -> replay.
    5) si es debit: valida que el saldo alcance (402 saldo_insuficiente).
    6) INSERT en prepaid_ledger (source='ajuste_manual', meta={reason,by_user_id}).
    """
    # --- validaciones de entrada (422) ---
    if body.kind not in ("credit", "debit"):
        raise HTTPException(422, "kind debe ser 'credit' o 'debit'")
    if body.amount_cents <= 0:
        raise HTTPException(422, "amount_cents debe ser > 0")
    if not body.reason or not body.reason.strip():
        raise HTTPException(422, "reason es obligatorio")

    target_org = _resolve_org(user, org)
    await _assert_client_in_org_admin(db, cid, target_org, user)

    # serializa contra otros ajustes/cargos del mismo cliente
    await db.execute(text("SELECT pg_advisory_xact_lock(:c)"), {"c": cid})

    # --- idempotencia: ajuste ya registrado -> replay (no duplica asiento) ---
    if body.idempotency_key:
        prev = (await db.execute(
            text("SELECT 1 FROM prepaid_ledger "
                 "WHERE client_id=:c AND idempotency_key=:k"),
            {"c": cid, "k": body.idempotency_key},
        )).first()
        if prev:
            bal = int((await db.execute(
                text("SELECT balance_cents FROM v_client_balance WHERE client_id=:c"),
                {"c": cid},
            )).scalar() or 0)
            return {"ok": True, "client_id": cid, "kind": body.kind,
                    "amount_cents": body.amount_cents, "balance_cents": bal,
                    "idempotent_replay": True}

    # --- debito: el saldo debe alcanzar (402) ---
    bal = int((await db.execute(
        text("SELECT balance_cents FROM v_client_balance WHERE client_id=:c"),
        {"c": cid},
    )).scalar() or 0)
    if body.kind == "debit" and bal < body.amount_cents:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={"error": "saldo_insuficiente", "balance_cents": bal,
                    "required_cents": body.amount_cents},
        )

    # actor del trigger de auditoria (antes del INSERT)
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))

    # asiento NUEVO de correccion (append-only); organization_id = org destino
    meta = json.dumps({"reason": body.reason, "by_user_id": user.id})
    await db.execute(
        text("INSERT INTO prepaid_ledger "
             "(client_id, organization_id, kind, amount_cents, source, "
             " idempotency_key, meta) "
             "VALUES (:c, :org, :kind, :amt, 'ajuste_manual', :k, CAST(:m AS jsonb))"),
        {"c": cid, "org": target_org, "kind": body.kind,
         "amt": body.amount_cents, "k": body.idempotency_key, "m": meta},
    )
    await db.commit()

    # saldo nuevo: credit suma, debit resta
    new_bal = bal + body.amount_cents if body.kind == "credit" else bal - body.amount_cents
    return {"ok": True, "client_id": cid, "kind": body.kind,
            "amount_cents": body.amount_cents, "balance_cents": new_bal,
            "idempotent_replay": False}


@router.get("/clients/{cid}/adjustments")
async def list_adjustments(
    cid: int,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Lista los ultimos 100 ajustes manuales del cliente (mas recientes primero)."""
    target_org = _resolve_org(user, org)
    await _assert_client_in_org_admin(db, cid, target_org, user)

    rows = (await db.execute(
        text("SELECT created_at, kind, amount_cents, meta FROM prepaid_ledger "
             "WHERE client_id=:c AND source='ajuste_manual' "
             "ORDER BY created_at DESC LIMIT 100"),
        {"c": cid},
    )).all()
    return {
        "client_id": cid,
        "adjustments": [
            {
                "fecha": r[0].isoformat(),
                "kind": r[1],
                "amount_cents": int(r[2]),
                "meta": r[3],
            }
            for r in rows
        ],
    }
