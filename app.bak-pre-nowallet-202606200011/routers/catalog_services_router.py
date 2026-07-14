"""CRUD de SERVICIOS del catalogo (Bloque 1): /admin/catalog/services.

API JSON, multi-tenant self-service: cada organizacion administra SUS servicios.
El operador de la plataforma (super tenant, user.is_platform) puede operar sobre
cualquier organizacion pasando ?org=<id> explicito; si no lo pasa, usa la suya.

Patron del proyecto: SQL crudo con text() (sin ORM) y bind_actor antes de cada
escritura para que el trigger de auditoria registre al actor.
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

# escritura restringida a roles internos con permiso de gestion
_WRITE = require_roles("super_admin", "finanzas")

# valores permitidos por el CHECK services_source_core_check
_SOURCE_CORES = {"medidor", "hub", "messages", "finanzas", "internal"}


# ---------------------------------------------------------------------
# helpers de aislamiento por organizacion (self-contained)
# ---------------------------------------------------------------------


def _resolve_org(user: CurrentUser, org_param: int | None) -> int:
    """org_param solo lo respeta el super tenant; el resto siempre su propia org."""
    if org_param is not None and user.is_platform:
        return org_param
    return user.organization_id


async def _assert_service_in_org(
    db: AsyncSession, service_id: int, org: int, user: CurrentUser
) -> None:
    """Garantiza que el servicio existe y pertenece a la org del usuario.

    El super tenant puede tocar el de cualquier org; el resto solo el suyo.
    """
    row = (await db.execute(
        text("SELECT organization_id FROM services WHERE id=:i"), {"i": service_id}
    )).first()
    if row is None:
        raise HTTPException(404, "servicio no existe")
    if not (user.is_platform or int(row[0]) == user.organization_id):
        raise HTTPException(403, "servicio de otra organizacion")


# ---------------------------------------------------------------------
# esquemas de entrada
# ---------------------------------------------------------------------


class ServiceCreateIn(BaseModel):
    code: str
    name: str
    source_core: str
    unit: str
    unit_price_cents: int


class ServiceUpdateIn(BaseModel):
    name: str | None = None
    unit: str | None = None
    unit_price_cents: int | None = None


# ---------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------


@router.post("/services")
async def create_service(
    body: ServiceCreateIn,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Alta de servicio en la org resuelta. organization_id NUNCA viene del body.

    Valida source_core y unit_price_cents; 409 si el code ya existe en esa org.
    """
    if body.source_core not in _SOURCE_CORES:
        raise HTTPException(422, "source_core invalido")
    if body.unit_price_cents < 0:
        raise HTTPException(422, "unit_price_cents debe ser >= 0")

    target_org = _resolve_org(user, org)

    # 409 si ya existe ese code en la org destino
    dup = (await db.execute(
        text("SELECT 1 FROM services WHERE code=:c AND organization_id=:o"),
        {"c": body.code, "o": target_org},
    )).first()
    if dup is not None:
        raise HTTPException(409, "el code ya existe en esta organizacion")

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    row = (await db.execute(text("""
        INSERT INTO services
          (code, name, source_core, unit, unit_price_cents, organization_id)
        VALUES (:code, :name, :sc, :unit, :upc, :org)
        RETURNING id, code, organization_id
    """), {
        "code": body.code, "name": body.name, "sc": body.source_core,
        "unit": body.unit, "upc": body.unit_price_cents, "org": target_org,
    })).mappings().one()
    return {"id": row["id"], "code": row["code"],
            "organization_id": row["organization_id"]}


@router.patch("/services/{sid}")
async def update_service(
    sid: int,
    body: ServiceUpdateIn,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Edita name/unit/unit_price_cents (solo los enviados, COALESCE). Auditado."""
    if body.unit_price_cents is not None and body.unit_price_cents < 0:
        raise HTTPException(422, "unit_price_cents debe ser >= 0")

    target_org = _resolve_org(user, org)
    await _assert_service_in_org(db, sid, target_org, user)

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    await db.execute(text("""
        UPDATE services SET
          name             = COALESCE(:name, name),
          unit             = COALESCE(:unit, unit),
          unit_price_cents = COALESCE(:upc, unit_price_cents),
          updated_at       = now()
        WHERE id = :id
    """), {
        "name": body.name, "unit": body.unit,
        "upc": body.unit_price_cents, "id": sid,
    })
    return {"id": sid, "updated": True}


@router.post("/services/{sid}/toggle")
async def toggle_service(
    sid: int,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Invierte is_active del servicio. Auditado."""
    target_org = _resolve_org(user, org)
    await _assert_service_in_org(db, sid, target_org, user)

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    row = (await db.execute(text("""
        UPDATE services SET is_active = NOT is_active, updated_at = now()
        WHERE id = :id
        RETURNING is_active
    """), {"id": sid})).mappings().one()
    return {"id": sid, "is_active": row["is_active"]}
