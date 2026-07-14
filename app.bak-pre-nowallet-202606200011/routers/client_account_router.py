"""Vista de CUENTA del cliente para el panel de ADMIN: saldo del libro CAF
(`v_client_balance`) + movimientos del ledger (`prepaid_ledger`), por organizacion.

A diferencia de los endpoints app-facing de api_router (que exponen lo mismo via
API key con `resolve_app_org`), aqui el actor es un usuario JWT del panel. El
aislamiento es multi-tenant delegado: la org ve a SUS clientes; el super tenant
(is_platform) puede consultar cualquier org pasando ?org=<id>. Solo lectura.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles

router = APIRouter(prefix="/admin", tags=["client-account"])

# lectura: roles internos con acceso al panel financiero
_READ = require_roles("super_admin", "finanzas", "lectura")


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
# endpoints (solo lectura)
# ---------------------------------------------------------------------


@router.get("/clients/{cid}/balance")
async def client_balance(
    cid: int,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Saldo prepago del cliente desde el libro del CAF (0 si no hay asientos)."""
    target_org = _resolve_org(user, org)
    await _assert_client_in_org_admin(db, cid, target_org, user)

    bal = int((await db.execute(
        text("SELECT balance_cents FROM v_client_balance WHERE client_id=:c"),
        {"c": cid},
    )).scalar() or 0)
    return {"client_id": cid, "balance_cents": bal}


@router.get("/clients/{cid}/ledger")
async def client_ledger(
    cid: int,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
    limit: int = Query(50),
):
    """Movimientos del libro prepago + saldo + consumo del mes en curso."""
    target_org = _resolve_org(user, org)
    await _assert_client_in_org_admin(db, cid, target_org, user)

    limit = max(1, min(int(limit), 200))

    bal = int((await db.execute(
        text("SELECT balance_cents FROM v_client_balance WHERE client_id=:c"),
        {"c": cid},
    )).scalar() or 0)

    rows = (await db.execute(
        text("SELECT created_at, kind, service_code, units, amount_cents, source "
             "FROM prepaid_ledger WHERE client_id=:c "
             "ORDER BY created_at DESC LIMIT :l"),
        {"c": cid, "l": limit},
    )).all()

    consumo_mes = int((await db.execute(
        text("SELECT coalesce(sum(amount_cents),0) FROM prepaid_ledger "
             "WHERE client_id=:c AND kind='debit' "
             "AND created_at >= date_trunc('month', now())"),
        {"c": cid},
    )).scalar() or 0)

    return {
        "client_id": cid,
        "balance_cents": bal,
        "consumo_mes_cents": consumo_mes,
        "movimientos": [
            {
                "created_at": r[0].isoformat(),
                "kind": r[1],
                "service_code": r[2],
                "units": int(r[3]) if r[3] is not None else None,
                "amount_cents": int(r[4]),
                "source": r[5],
            }
            for r in rows
        ],
    }
