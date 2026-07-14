"""Reportes y alertas por organización — CAF Billing Engine SaaS (solo lectura).

Multi-tenant delegado: cada organización ve los suyos; el operador de la
plataforma (super_admin org 1) puede mirar cualquier org vía ?org, o el agregado
de TODAS si no pasa ?org. Solo SELECTs; ningún endpoint escribe.
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles

router = APIRouter(prefix="/admin/reports", tags=["reports"])

# lectura: operador interno o roles de consulta
_READ = require_roles("super_admin", "finanzas", "lectura")


def _org_scope(
    user: CurrentUser, org_param: int | None, column: str = "organization_id"
) -> tuple[str, dict]:
    """Cláusula de aislamiento por org con override de ?org para la plataforma.

    - Plataforma sin ?org  -> ve TODAS las organizaciones (sin filtro).
    - Plataforma con ?org   -> se restringe a esa org.
    - No-plataforma         -> siempre su propia org (ignora ?org).

    Devuelve (fragmento SQL para concatenar en el WHERE, params). `column`
    permite calificar con alias (p.ej. 'l.organization_id') en queries con JOIN.
    """
    if user.is_platform:
        if org_param is None:
            return "", {}
        return f" AND {column} = :_org", {"_org": org_param}
    return f" AND {column} = :_org", {"_org": user.organization_id}


# ---------------------------------------------------------------------
# alerta: clientes con saldo bajo
# ---------------------------------------------------------------------


@router.get("/low-balance")
async def low_balance(
    request: Request,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    threshold_cents: int = Query(5000),
    org: int | None = Query(None),
) -> dict:
    """Clientes activos con saldo por debajo del umbral (alerta de recarga)."""
    oc, op = _org_scope(user, org, "c.organization_id")
    rows = (await db.execute(text(f"""
        SELECT c.id, c.legal_name, b.balance_cents
        FROM clients c
        JOIN v_client_balance b ON b.client_id = c.id
        WHERE c.status = 'active' AND b.balance_cents < :th{oc}
        ORDER BY b.balance_cents ASC
        LIMIT 500
    """), {"th": int(threshold_cents), **op})).mappings().all()
    items = [
        {"id": r["id"], "legal_name": r["legal_name"],
         "balance_cents": int(r["balance_cents"] or 0)}
        for r in rows
    ]
    return {"threshold_cents": int(threshold_cents), "items": items}


# ---------------------------------------------------------------------
# reporte: top de consumo
# ---------------------------------------------------------------------


async def _consumo_rows(
    db: AsyncSession, user: CurrentUser, days: int, org: int | None,
    limit: int | None,
) -> list[dict]:
    """Consumo (debits) por cliente en los últimos N días, aislado por org.

    Agrupa por cliente el monto debitado del prepaid_ledger. Si `limit` es None
    devuelve TODOS los clientes con consumo>0 (para el CSV); si trae valor,
    devuelve el top-N (para el JSON).
    """
    # aísla por org sobre el ledger (alias l)
    oc, op = _org_scope(user, org, "l.organization_id")
    lim = "" if limit is None else " LIMIT :_lim"
    params = {"days": int(days), **op}
    if limit is not None:
        params["_lim"] = int(limit)
    rows = (await db.execute(text(f"""
        SELECT c.id AS client_id, c.legal_name,
               COALESCE(sum(l.amount_cents), 0) AS consumo_cents
        FROM prepaid_ledger l
        JOIN clients c ON c.id = l.client_id
        WHERE l.kind = 'debit'
          AND l.created_at >= now() - make_interval(days => :days){oc}
        GROUP BY c.id, c.legal_name
        HAVING COALESCE(sum(l.amount_cents), 0) > 0
        ORDER BY consumo_cents DESC{lim}
    """), params)).mappings().all()
    return [
        {"client_id": r["client_id"], "legal_name": r["legal_name"],
         "consumo_cents": int(r["consumo_cents"] or 0)}
        for r in rows
    ]


@router.get("/top-consumo")
async def top_consumo(
    request: Request,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30),
    org: int | None = Query(None),
) -> dict:
    """Top 50 clientes por consumo (debits) de los últimos N días."""
    items = await _consumo_rows(db, user, days, org, limit=50)
    return {"days": int(days), "items": items}


# ---------------------------------------------------------------------
# export: consumo a CSV
# ---------------------------------------------------------------------


@router.get("/consumo.csv")
async def consumo_csv(
    request: Request,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30),
    org: int | None = Query(None),
) -> Response:
    """Mismo dato que top-consumo pero TODOS los clientes con consumo>0, en CSV."""
    items = await _consumo_rows(db, user, days, org, limit=None)
    # csv.writer escapa por sí solo comillas/comas del legal_name (QUOTE_MINIMAL)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["client_id", "legal_name", "consumo_cents"])
    for it in items:
        w.writerow([it["client_id"], it["legal_name"], it["consumo_cents"]])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=consumo.csv"},
    )
