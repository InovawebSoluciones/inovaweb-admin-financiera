"""Lecturas JSON del CATALOGO por organizacion: /api/v2/catalog.

Endpoints de SOLO LECTURA (JWT) para que cada organizacion y la consola lean SU
catalogo (servicios / planes / productos / promociones). Multi-tenant delegado:

- Tenant normal: ve unicamente el catalogo de SU organizacion.
- Super tenant (user.is_platform): puede ver el de cualquier org pasando ?org=<id>,
  o el de TODAS las organizaciones si no pasa ?org.

SQL crudo con text() (sin ORM), mismo patron que catalog_services_router /
admin_router. NO usa el prefix /admin/catalog (ocupado por la UI HTML y los
POST/PATCH de los catalog_*); aqui el prefix es /api/v2/catalog.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles

router = APIRouter(prefix="/api/v2/catalog", tags=["catalog-read"])

# lectura: roles internos con visibilidad del catalogo
_READ = require_roles("super_admin", "finanzas", "lectura")


# ---------------------------------------------------------------------
# helpers de aislamiento por organizacion (self-contained)
# ---------------------------------------------------------------------


def _org_filter(user: CurrentUser, org_param: int | None) -> tuple[str, dict]:
    """Clausula de aislamiento por org para los LISTADOS.

    - super tenant sin ?org  -> ("", {})                 (todas las orgs)
    - super tenant con  ?org  -> (" AND organization_id = :_org", {"_org": org})
    - tenant normal           -> (" AND organization_id = :_org", {"_org": su org})
    El ?org solo lo respeta el super tenant; el resto siempre su propia org.
    """
    if user.is_platform:
        if org_param is None:
            return "", {}
        return " AND organization_id = :_org", {"_org": org_param}
    return " AND organization_id = :_org", {"_org": user.organization_id}


def _resolve_org(user: CurrentUser, org_param: int | None) -> int:
    """org_param solo lo respeta el super tenant; el resto siempre su propia org.

    Usado en el WHERE del DETALLE cuando se exige coincidencia de organizacion.
    """
    if org_param is not None and user.is_platform:
        return org_param
    return user.organization_id


def _detail_where(user: CurrentUser, org_param: int | None) -> tuple[str, dict]:
    """Clausula del WHERE para el DETALLE por id.

    - super tenant SIN ?org -> ("", {})  (puede ver por id global, cualquier org)
    - resto                 -> (" AND organization_id = :_org", {"_org": resuelta})
    """
    if user.is_platform and org_param is None:
        return "", {}
    return " AND organization_id = :_org", {"_org": _resolve_org(user, org_param)}


# ---------------------------------------------------------------------
# servicios
# ---------------------------------------------------------------------


@router.get("/services")
async def list_services(
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Lista de servicios del catalogo de la(s) org(s) visibles."""
    clause, params = _org_filter(user, org)
    rows = (await db.execute(text(f"""
        SELECT id, code, name, source_core, unit, unit_price_cents, is_active
        FROM services
        WHERE TRUE{clause}
        ORDER BY name
    """), params)).mappings().all()
    return {"items": [
        {**dict(r), "unit_price_cents": int(r["unit_price_cents"])} for r in rows
    ]}


@router.get("/services/{sid}")
async def get_service(
    sid: int,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Detalle de un servicio. 404 si no existe o no es de la org resuelta."""
    where, params = _detail_where(user, org)
    row = (await db.execute(text(f"""
        SELECT id, code, name, source_core, unit, unit_price_cents, is_active,
               organization_id
        FROM services
        WHERE id = :id{where}
    """), {"id": sid, **params})).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "servicio no existe")
    return {**dict(row), "unit_price_cents": int(row["unit_price_cents"])}


# ---------------------------------------------------------------------
# planes
# ---------------------------------------------------------------------


@router.get("/plans")
async def list_plans(
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Lista de planes del catalogo de la(s) org(s) visibles."""
    clause, params = _org_filter(user, org)
    rows = (await db.execute(text(f"""
        SELECT id, code, name, description, monthly_fee_cents, monthly_credit_cents,
               is_free, app_slug, is_active
        FROM plans
        WHERE TRUE{clause}
        ORDER BY name
    """), params)).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["monthly_fee_cents"] = int(r["monthly_fee_cents"])
        d["monthly_credit_cents"] = (
            int(r["monthly_credit_cents"])
            if r["monthly_credit_cents"] is not None else None
        )
        out.append(d)
    return {"items": out}


@router.get("/plans/{pid}")
async def get_plan(
    pid: int,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Detalle de un plan + sus items (service_code + hard_limit_units)."""
    where, params = _detail_where(user, org)
    row = (await db.execute(text(f"""
        SELECT id, code, name, description, monthly_fee_cents, monthly_credit_cents,
               is_free, app_slug, is_active, organization_id
        FROM plans
        WHERE id = :id{where}
    """), {"id": pid, **params})).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan no existe")
    # items del plan: limite por servicio (JOIN para el code legible)
    items = (await db.execute(text("""
        SELECT s.code AS service_code, pi.service_id, pi.hard_limit_units
        FROM plan_items pi JOIN services s ON s.id = pi.service_id
        WHERE pi.plan_id = :pid
        ORDER BY s.code
    """), {"pid": pid})).mappings().all()
    d = dict(row)
    d["monthly_fee_cents"] = int(row["monthly_fee_cents"])
    d["monthly_credit_cents"] = (
        int(row["monthly_credit_cents"])
        if row["monthly_credit_cents"] is not None else None
    )
    d["items"] = [dict(i) for i in items]
    return d


# ---------------------------------------------------------------------
# productos
# ---------------------------------------------------------------------


@router.get("/products")
async def list_products(
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Lista de productos del catalogo de la(s) org(s) visibles."""
    clause, params = _org_filter(user, org)
    rows = (await db.execute(text(f"""
        SELECT id, code, name, description, sat_key, unit_sat_key, is_active
        FROM products
        WHERE TRUE{clause}
        ORDER BY name
    """), params)).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/products/{prid}")
async def get_product(
    prid: int,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Detalle de un producto. 404 si no existe o no es de la org resuelta."""
    where, params = _detail_where(user, org)
    row = (await db.execute(text(f"""
        SELECT id, code, name, description, sat_key, unit_sat_key, is_active,
               organization_id
        FROM products
        WHERE id = :id{where}
    """), {"id": prid, **params})).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "producto no existe")
    return dict(row)


# ---------------------------------------------------------------------
# promociones
# ---------------------------------------------------------------------


@router.get("/promotions")
async def list_promotions(
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Lista de promociones del catalogo de la(s) org(s) visibles."""
    clause, params = _org_filter(user, org)
    rows = (await db.execute(text(f"""
        SELECT id, code, name, kind, discount_pct, discount_cents, valid_from,
               valid_to, max_uses, min_amount_cents, is_active
        FROM promotions
        WHERE TRUE{clause}
        ORDER BY valid_from DESC
    """), params)).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["discount_cents"] = (
            int(r["discount_cents"]) if r["discount_cents"] is not None else None
        )
        d["min_amount_cents"] = (
            int(r["min_amount_cents"]) if r["min_amount_cents"] is not None else None
        )
        out.append(d)
    return {"items": out}


@router.get("/promotions/{prid}")
async def get_promotion(
    prid: int,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Detalle de una promocion. 404 si no existe o no es de la org resuelta."""
    where, params = _detail_where(user, org)
    row = (await db.execute(text(f"""
        SELECT id, code, name, kind, discount_pct, discount_cents, valid_from,
               valid_to, max_uses, min_amount_cents, is_active, organization_id
        FROM promotions
        WHERE id = :id{where}
    """), {"id": prid, **params})).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "promocion no existe")
    d = dict(row)
    d["discount_cents"] = (
        int(row["discount_cents"]) if row["discount_cents"] is not None else None
    )
    d["min_amount_cents"] = (
        int(row["min_amount_cents"]) if row["min_amount_cents"] is not None else None
    )
    return d
