"""Gestión de organizaciones y API keys — CAF Billing Engine SaaS.

Dos planos:
  - PLATAFORMA (operador Inovaweb, super_admin org 1): da de alta organizaciones
    (empresas del sector) y ve el consumo agregado de TODAS.
  - ORGANIZACIÓN (super_admin de su org): acuña/lista/revoca sus propias API keys
    self-service (sus apps las usan en /api/v2/.../charge). Mata el hardcode de
    llaves en .env: cada app nueva = alta self-service, sin cambio de código.

Las API keys se guardan SOLO por hash (SHA-256); el texto plano se devuelve UNA
vez en el alta y nunca se vuelve a exponer.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles
from app.core.password import hash_password
from app.core.tenancy import hash_key
from app.services.saas_billing import register_org_as_platform_client

router = APIRouter(prefix="/api/v2", tags=["orgs"])

_SUPER = require_roles("super_admin")
_KEY_PREFIX = "cafk_"


async def _platform_only(user: CurrentUser = Depends(_SUPER)) -> CurrentUser:
    """Solo el operador de la plataforma (super_admin de la org 1 Inovaweb)."""
    if not user.is_platform:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "solo operador de plataforma")
    return user


def _can_manage_org(user: CurrentUser, org_id: int) -> bool:
    """Plataforma administra cualquier org; un super_admin solo la suya."""
    return user.is_platform or (user.organization_id == org_id and user.has("super_admin"))


# ---------------------------------------------------------------------
# organizaciones (plano plataforma)
# ---------------------------------------------------------------------


class CreateOrgBody(BaseModel):
    slug: str
    name: str
    owner_email: EmailStr
    owner_full_name: str


class CreateOrgResponse(BaseModel):
    organization_id: int
    slug: str
    owner_user_id: int
    owner_temp_password: str


@router.post("/orgs", response_model=CreateOrgResponse, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: CreateOrgBody, request: Request,
    user: CurrentUser = Depends(_platform_only),
    db: AsyncSession = Depends(get_db),
) -> CreateOrgResponse:
    """Da de alta una organización + su usuario owner (super_admin de esa org)."""
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    dup = (await db.execute(
        text("SELECT 1 FROM organizations WHERE slug=:s"), {"s": body.slug})).first()
    if dup:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug ya existe")
    org_id = (await db.execute(
        text("INSERT INTO organizations (slug, name) VALUES (:s, :n) RETURNING id"),
        {"s": body.slug, "n": body.name},
    )).scalar_one()

    temp_pw = secrets.token_urlsafe(12)
    owner_id = (await db.execute(text("""
        INSERT INTO users (email, password_hash, full_name, is_active, is_internal,
                           client_id, organization_id)
        VALUES (:e, :h, :n, TRUE, FALSE, NULL, :org)
        RETURNING id
    """), {"e": body.owner_email, "h": hash_password(temp_pw),
           "n": body.owner_full_name, "org": org_id})).scalar_one()
    await db.execute(text("""
        INSERT INTO user_roles (user_id, role_id)
        SELECT :uid, id FROM roles WHERE code = 'super_admin'
    """), {"uid": owner_id})

    # registra la org como cliente de la plataforma (sujeto de meta-cobro del SaaS):
    # crea su fila clients en la org 1 + suscripcion al plan caf_saas + enlace.
    await register_org_as_platform_client(db, org_id, body.name)

    return CreateOrgResponse(organization_id=org_id, slug=body.slug,
                             owner_user_id=owner_id, owner_temp_password=temp_pw)


@router.get("/orgs")
async def list_orgs(
    user: CurrentUser = Depends(_platform_only),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Consumo agregado por organización del mes en curso (vista de plataforma)."""
    rows = (await db.execute(text("""
        SELECT o.id, o.slug, o.name, o.status,
               (SELECT count(*) FROM clients c WHERE c.organization_id = o.id) AS clients,
               (SELECT count(*) FROM api_keys k
                  WHERE k.organization_id = o.id AND k.revoked_at IS NULL) AS active_keys,
               COALESCE((SELECT sum(l.amount_cents) FROM prepaid_ledger l
                  WHERE l.organization_id = o.id AND l.kind='debit'
                    AND l.created_at >= date_trunc('month', now())), 0) AS consumo_mes_cents
        FROM organizations o ORDER BY o.id
    """))).mappings().all()
    return {"organizations": [dict(r) for r in rows]}


@router.get("/orgs/{org_id}/consumo")
async def org_consumo(
    org_id: int,
    user: CurrentUser = Depends(_SUPER),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Desglose del consumo del mes de una org: por tenant (cliente) y por servicio.

    Es el dato de fondo de la consola (consumos por cliente/servicio dentro de la
    organización). Plataforma ve cualquier org; un super_admin solo la suya.
    """
    if not _can_manage_org(user, org_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "sin permiso sobre esta organización")
    por_tenant = (await db.execute(text("""
        SELECT c.id AS client_id, c.legal_name,
               COALESCE(sum(CASE WHEN l.kind='debit'
                 AND l.created_at >= date_trunc('month', now())
                 THEN l.amount_cents ELSE 0 END), 0) AS consumo_mes_cents
        FROM clients c LEFT JOIN prepaid_ledger l ON l.client_id = c.id
        WHERE c.organization_id = :org
        GROUP BY c.id, c.legal_name ORDER BY consumo_mes_cents DESC
    """), {"org": org_id})).mappings().all()
    por_servicio = (await db.execute(text("""
        SELECT service_code,
               sum(amount_cents) AS consumo_mes_cents, sum(units) AS units
        FROM prepaid_ledger
        WHERE organization_id = :org AND kind='debit' AND service_code IS NOT NULL
          AND created_at >= date_trunc('month', now())
        GROUP BY service_code ORDER BY consumo_mes_cents DESC
    """), {"org": org_id})).mappings().all()
    return {
        "organization_id": org_id,
        "por_tenant": [dict(r) for r in por_tenant],
        "por_servicio": [dict(r) for r in por_servicio],
    }


# ---------------------------------------------------------------------
# API keys (plano organización, self-service)
# ---------------------------------------------------------------------


class MintKeyBody(BaseModel):
    name: str
    scope: str = "admin"   # admin | readonly


class MintKeyResponse(BaseModel):
    id: int
    organization_id: int
    name: str
    scope: str
    api_key: str           # texto plano: se muestra UNA sola vez


@router.post("/orgs/{org_id}/api-keys", response_model=MintKeyResponse,
             status_code=status.HTTP_201_CREATED)
async def mint_api_key(
    org_id: int, body: MintKeyBody, request: Request,
    user: CurrentUser = Depends(_SUPER),
    db: AsyncSession = Depends(get_db),
) -> MintKeyResponse:
    """Acuña una API key para la organización. Devuelve el texto plano una vez."""
    if not _can_manage_org(user, org_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "sin permiso sobre esta organización")
    if body.scope not in ("admin", "readonly"):
        raise HTTPException(422, "scope invalido")
    exists = (await db.execute(
        text("SELECT 1 FROM organizations WHERE id=:o"), {"o": org_id})).first()
    if not exists:
        raise HTTPException(404, "organización no existe")
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    plaintext = _KEY_PREFIX + secrets.token_urlsafe(32)
    kid = (await db.execute(text("""
        INSERT INTO api_keys (name, key_hash, scope, created_by_user_id, organization_id)
        VALUES (:n, :h, :s, :uid, :org) RETURNING id
    """), {"n": body.name, "h": hash_key(plaintext), "s": body.scope,
           "uid": user.id, "org": org_id})).scalar_one()
    return MintKeyResponse(id=kid, organization_id=org_id, name=body.name,
                           scope=body.scope, api_key=plaintext)


@router.get("/orgs/{org_id}/api-keys")
async def list_api_keys(
    org_id: int,
    user: CurrentUser = Depends(_SUPER),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lista las API keys de la org (enmascaradas; nunca devuelve el texto plano)."""
    if not _can_manage_org(user, org_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "sin permiso sobre esta organización")
    rows = (await db.execute(text("""
        SELECT id, name, scope, created_at, last_used_at, revoked_at
        FROM api_keys WHERE organization_id = :org ORDER BY id DESC
    """), {"org": org_id})).mappings().all()
    return {"organization_id": org_id, "api_keys": [dict(r) for r in rows]}


@router.post("/api-keys/{key_id}/revoke")
async def revoke_api_key(
    key_id: int, request: Request,
    user: CurrentUser = Depends(_SUPER),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Revoca una API key (soft: marca revoked_at, nunca se borra)."""
    owner = (await db.execute(
        text("SELECT organization_id, revoked_at FROM api_keys WHERE id=:id"),
        {"id": key_id})).first()
    if owner is None:
        raise HTTPException(404, "api key no existe")
    if not _can_manage_org(user, int(owner[0])):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "sin permiso sobre esta organización")
    if owner[1] is not None:
        return {"id": key_id, "status": "already_revoked"}
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    await db.execute(
        text("UPDATE api_keys SET revoked_at = now() WHERE id=:id"), {"id": key_id})
    return {"id": key_id, "status": "revoked"}
