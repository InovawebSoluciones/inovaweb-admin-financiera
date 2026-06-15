"""Ciclo de vida de ORGANIZACIONES (plano plataforma) — CAF Billing Engine.

EXCLUSIVO del super tenant (operador de la plataforma, super_admin de la org 1).
NO se delega: editar/suspender/reactivar/cancelar una organización solo lo hace
el operador. Comparte prefijo con orgs_router pero expone rutas distintas
(FastAPI permite varios routers con el mismo prefix).

La org 1 (plataforma) está protegida: no se puede suspender ni cancelar.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles
from app.services.saas_billing import get_saas_account, run_saas_monthly_billing

router = APIRouter(prefix="/api/v2/orgs", tags=["orgs-admin"])

_SUPER = require_roles("super_admin")
_PLATFORM_ORG_ID = 1  # org de la plataforma: intocable (no suspender/cancelar)


async def _platform_only(user: CurrentUser = Depends(_SUPER)) -> CurrentUser:
    """Solo el operador de la plataforma (super_admin de la org 1 Inovaweb)."""
    if not user.is_platform:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "solo operador de plataforma")
    return user


# helper de auditoría: setea el actor antes de cada escritura
async def _audit(db: AsyncSession, user: CurrentUser, request: Request) -> None:
    await bind_actor(
        db,
        actor_user_id=user.id,
        actor_ip=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------


class UpdateOrgBody(BaseModel):
    name: str | None = None
    slug: str | None = None


class ReasonBody(BaseModel):
    reason: str | None = None


# ---------------------------------------------------------------------
# editar
# ---------------------------------------------------------------------


@router.patch("/{org_id}")
async def update_org(
    org_id: int, body: UpdateOrgBody, request: Request,
    user: CurrentUser = Depends(_platform_only),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Edita nombre/slug de una organización. COALESCE deja intactos los no enviados."""
    await _audit(db, user, request)
    # pre-check de colisión de slug (la otra capa es el UNIQUE → IntegrityError)
    if body.slug is not None:
        clash = (await db.execute(
            text("SELECT 1 FROM organizations WHERE slug=:s AND id<>:id"),
            {"s": body.slug, "id": org_id})).first()
        if clash:
            raise HTTPException(status.HTTP_409_CONFLICT, "slug ya existe")
    try:
        row = (await db.execute(text("""
            UPDATE organizations
            SET name = COALESCE(:n, name),
                slug = COALESCE(:s, slug),
                updated_at = now()
            WHERE id = :id
            RETURNING id
        """), {"n": body.name, "s": body.slug, "id": org_id})).first()
    except IntegrityError:
        # carrera contra el UNIQUE de slug
        raise HTTPException(status.HTTP_409_CONFLICT, "slug ya existe")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organización no existe")
    return {"id": org_id, "updated": True}


# ---------------------------------------------------------------------
# suspender / reactivar / cancelar
# ---------------------------------------------------------------------


@router.post("/{org_id}/suspend")
async def suspend_org(
    org_id: int, body: ReasonBody, request: Request,
    user: CurrentUser = Depends(_platform_only),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Suspende una org activa. La org de plataforma es intocable (400)."""
    if org_id == _PLATFORM_ORG_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "no se puede suspender la organización de plataforma")
    await _audit(db, user, request)
    row = (await db.execute(text("""
        UPDATE organizations SET status='suspended', updated_at=now()
        WHERE id=:id AND status<>'cancelled'
        RETURNING id
    """), {"id": org_id})).first()
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "organización inexistente o cancelada")
    return {"id": org_id, "status": "suspended"}


@router.post("/{org_id}/reactivate")
async def reactivate_org(
    org_id: int, request: Request,
    user: CurrentUser = Depends(_platform_only),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reactiva una org suspendida (vuelve a 'active')."""
    await _audit(db, user, request)
    row = (await db.execute(text("""
        UPDATE organizations SET status='active', updated_at=now()
        WHERE id=:id AND status='suspended'
        RETURNING id
    """), {"id": org_id})).first()
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "organización no estaba suspendida")
    return {"id": org_id, "status": "active"}


@router.post("/{org_id}/cancel")
async def cancel_org(
    org_id: int, body: ReasonBody, request: Request,
    user: CurrentUser = Depends(_platform_only),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancela una org (estado terminal). La org de plataforma es intocable (400)."""
    if org_id == _PLATFORM_ORG_ID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "no se puede cancelar la organización de plataforma")
    await _audit(db, user, request)
    row = (await db.execute(text("""
        UPDATE organizations SET status='cancelled', updated_at=now()
        WHERE id=:id AND status<>'cancelled'
        RETURNING id
    """), {"id": org_id})).first()
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "organización inexistente o ya cancelada")
    return {"id": org_id, "status": "cancelled"}


# ---------------------------------------------------------------------
# detalle
# ---------------------------------------------------------------------


@router.get("/{org_id}")
async def org_detail(
    org_id: int,
    user: CurrentUser = Depends(_platform_only),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Detalle de una org + clientes, api_keys activas y consumo del mes en curso.

    Reúsa el SQL de agregado de list_orgs (orgs_router) acotado a una sola org.
    """
    row = (await db.execute(text("""
        SELECT o.id, o.slug, o.name, o.status, o.created_at, o.updated_at,
               (SELECT count(*) FROM clients c WHERE c.organization_id = o.id) AS clients,
               (SELECT count(*) FROM api_keys k
                  WHERE k.organization_id = o.id AND k.revoked_at IS NULL) AS active_keys,
               COALESCE((SELECT sum(l.amount_cents) FROM prepaid_ledger l
                  WHERE l.organization_id = o.id AND l.kind='debit'
                    AND l.created_at >= date_trunc('month', now())), 0) AS consumo_mes_cents
        FROM organizations o WHERE o.id = :id
    """), {"id": org_id})).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organización no existe")
    return dict(row)


# ---------------------------------------------------------------------
# meta-cobro del SaaS (plataforma): estado de cuenta + cierre mensual
# ---------------------------------------------------------------------


@router.get("/{org_id}/saas-account")
async def org_saas_account(
    org_id: int,
    user: CurrentUser = Depends(_platform_only),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Estado de cuenta SaaS de la org frente a la plataforma.

    balance_cents NEGATIVO = la org debe (postpago). usage_mes_cents = consumo +
    cuota del mes en curso. 404 si la org no esta registrada como cliente de
    plataforma (sin platform_client_id).
    """
    acc = await get_saas_account(db, org_id)
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "org sin cuenta de plataforma (platform_client_id)")
    return acc


@router.post("/saas/run-monthly-billing")
async def saas_run_monthly_billing(
    request: Request,
    user: CurrentUser = Depends(_platform_only),
    db: AsyncSession = Depends(get_db),
    period: str | None = Query(None, description="YYYY-MM; default = mes en curso"),
) -> dict:
    """Cierre mensual del SaaS: acumula la cuota ($99) a cada org cliente activa.

    Idempotente por (org, period): re-correr el mismo periodo no duplica. Si no
    se pasa `period`, usa el mes en curso. Solo operador de plataforma.
    """
    await _audit(db, user, request)
    p = period or date.today().strftime("%Y-%m")
    return await run_saas_monthly_billing(db, p)
