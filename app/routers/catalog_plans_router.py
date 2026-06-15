"""CRUD de PLANES del catalogo (Bloque 1), multi-tenant.

Cada organizacion administra SUS planes; el operador de la plataforma
(super tenant, user.is_platform) puede administrar los de cualquier org
pasando ?org=<id>. SQL crudo con text() (sin ORM), mismo patron que admin_router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles

router = APIRouter(prefix="/admin/catalog", tags=["catalog"])

# solo roles internos con permiso de escritura
_WRITE = require_roles("super_admin", "finanzas")


# ---------------------------------------------------------------------
# helpers locales (self-contained)
# ---------------------------------------------------------------------


def _resolve_org(user: CurrentUser, org_param: int | None) -> int:
    """Organizacion sobre la que se opera.

    Siempre la del usuario; el super tenant (is_platform) puede operar sobre
    otra org pasando ?org=<id>. NUNCA se toma del body.
    """
    if org_param is not None and user.is_platform:
        return org_param
    return user.organization_id


async def _assert_plan_in_org(db: AsyncSession, plan_id: int, user: CurrentUser) -> int:
    """Valida que el plan exista y pertenezca a la org del usuario.

    404 si no existe; 403 si la organization_id del plan no es la del usuario y
    este no es is_platform. Devuelve la organization_id del plan.
    """
    row = (await db.execute(
        text("SELECT organization_id FROM plans WHERE id = :id"),
        {"id": plan_id},
    )).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan no existe")
    if not user.is_platform and row["organization_id"] != user.organization_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "plan de otra organizacion")
    return int(row["organization_id"])


async def _bind(db: AsyncSession, request: Request, user: CurrentUser) -> None:
    """Fija el actor de auditoria antes de cada escritura."""
    await bind_actor(
        db,
        actor_user_id=user.id,
        actor_ip=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------


class PlanCreateIn(BaseModel):
    code: str
    name: str
    description: str | None = None
    monthly_fee_cents: int = 0
    monthly_credit_cents: int | None = None
    is_free: bool = False
    app_slug: str | None = None


class PlanUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None
    monthly_fee_cents: int | None = None
    monthly_credit_cents: int | None = None
    is_free: bool | None = None
    app_slug: str | None = None


class PlanItemIn(BaseModel):
    service_id: int
    hard_limit_units: int


# ---------------------------------------------------------------------
# planes
# ---------------------------------------------------------------------


@router.post("/plans")
async def create_plan(
    body: PlanCreateIn,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Crea un plan en la org resuelta. 409 si el code ya existe en esa org."""
    org_id = _resolve_org(user, org)
    await _bind(db, request, user)
    # code es UNIQUE global; validamos colision por org para mensaje claro
    dup = (await db.execute(
        text("SELECT 1 FROM plans WHERE code = :code AND organization_id = :org"),
        {"code": body.code, "org": org_id},
    )).first()
    if dup is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "code ya existe en la organizacion")
    try:
        row = (await db.execute(text("""
            INSERT INTO plans
              (code, name, description, monthly_fee_cents, monthly_credit_cents,
               is_free, app_slug, organization_id)
            VALUES
              (:code, :name, :description, :fee, :credit, :is_free, :slug, :org)
            RETURNING id, code, organization_id
        """), {
            "code": body.code, "name": body.name, "description": body.description,
            "fee": body.monthly_fee_cents, "credit": body.monthly_credit_cents,
            "is_free": body.is_free, "slug": body.app_slug, "org": org_id,
        })).mappings().one()
    except Exception as e:  # noqa: BLE001
        # red de seguridad por el UNIQUE global de code
        raise HTTPException(status.HTTP_409_CONFLICT, "code duplicado") from e
    return {"id": row["id"], "code": row["code"],
            "organization_id": row["organization_id"]}


@router.patch("/plans/{pid}")
async def update_plan(
    pid: int,
    body: PlanUpdateIn,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Edita campos del plan (solo los enviados, COALESCE). No toca code ni org."""
    await _assert_plan_in_org(db, pid, user)
    await _bind(db, request, user)
    await db.execute(text("""
        UPDATE plans SET
          name                 = COALESCE(:name, name),
          description          = COALESCE(:description, description),
          monthly_fee_cents    = COALESCE(:fee, monthly_fee_cents),
          monthly_credit_cents = COALESCE(:credit, monthly_credit_cents),
          is_free              = COALESCE(:is_free, is_free),
          app_slug             = COALESCE(:slug, app_slug),
          updated_at           = now()
        WHERE id = :id
    """), {
        "name": body.name, "description": body.description,
        "fee": body.monthly_fee_cents, "credit": body.monthly_credit_cents,
        "is_free": body.is_free, "slug": body.app_slug, "id": pid,
    })
    return {"id": pid, "updated": True}


@router.post("/plans/{pid}/toggle")
async def toggle_plan(
    pid: int,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Invierte is_active del plan."""
    await _assert_plan_in_org(db, pid, user)
    await _bind(db, request, user)
    row = (await db.execute(text("""
        UPDATE plans SET is_active = NOT is_active, updated_at = now()
        WHERE id = :id
        RETURNING id, is_active
    """), {"id": pid})).mappings().one()
    return {"id": row["id"], "is_active": row["is_active"]}


# ---------------------------------------------------------------------
# items del plan (limites por servicio)
# ---------------------------------------------------------------------


@router.post("/plans/{pid}/items")
async def upsert_plan_item(
    pid: int,
    body: PlanItemIn,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Upsert de un limite (hard_limit_units) de un servicio en el plan.

    Valida que plan y servicio existan y pertenezcan a la MISMA org del usuario:
    404 si no existen, 403 si no coinciden. Usa ON CONFLICT(plan_id,service_id)
    (existe la constraint UNIQUE) para insertar o actualizar.
    """
    plan_org = await _assert_plan_in_org(db, pid, user)
    svc = (await db.execute(
        text("SELECT organization_id FROM services WHERE id = :id"),
        {"id": body.service_id},
    )).mappings().first()
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "servicio no existe")
    if svc["organization_id"] != plan_org:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "servicio de otra organizacion")
    await _bind(db, request, user)
    await db.execute(text("""
        INSERT INTO plan_items (plan_id, service_id, hard_limit_units)
        VALUES (:pid, :sid, :lim)
        ON CONFLICT (plan_id, service_id)
        DO UPDATE SET hard_limit_units = EXCLUDED.hard_limit_units
    """), {"pid": pid, "sid": body.service_id, "lim": body.hard_limit_units})
    return {"plan_id": pid, "service_id": body.service_id,
            "hard_limit_units": body.hard_limit_units}


@router.get("/plans/{pid}/items")
async def list_plan_items(
    pid: int,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Lista los items del plan: service_code + hard_limit_units."""
    await _assert_plan_in_org(db, pid, user)
    rows = (await db.execute(text("""
        SELECT s.code AS service_code, pi.service_id, pi.hard_limit_units
        FROM plan_items pi JOIN services s ON s.id = pi.service_id
        WHERE pi.plan_id = :pid
        ORDER BY s.code
    """), {"pid": pid})).mappings().all()
    return {"plan_id": pid, "items": [dict(r) for r in rows]}
