"""Bloque de SEGURIDAD por organizacion — CAF Billing Engine SaaS.

Vista de postura de seguridad multi-tenant:
  - API keys activas/revocadas de la org (sin exponer nunca el hash).
  - Usuarios en riesgo (logins fallidos / cuentas bloqueadas).
  - Rotacion de una API key (revoca la vieja + acuna una nueva con mismo
    name+scope+org; devuelve el texto plano UNA sola vez).

Multi-tenant delegado: la plataforma (super_admin org 1 Inovaweb) administra
cualquier org; un super_admin de una org solo la suya. El tenant se resuelve del
usuario, nunca del body (regla rector 7).
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles
from app.core.tenancy import hash_key

router = APIRouter(prefix="/admin/security", tags=["security"])

_SUPER = require_roles("super_admin")
_KEY_PREFIX = "cafk_"


def _can_manage_org(user: CurrentUser, org_id: int) -> bool:
    """Plataforma administra cualquier org; un super_admin solo la suya."""
    return user.is_platform or (user.organization_id == org_id and user.has("super_admin"))


def _resolve_org(user: CurrentUser, org_param: int | None) -> int:
    """Resuelve la org objetivo: el param si se da, si no la del usuario.

    Valida permiso sobre la org resuelta (403 si no puede administrarla). La
    plataforma puede apuntar a cualquier org; un super_admin solo a la suya.
    """
    org_id = org_param if org_param is not None else user.organization_id
    if not _can_manage_org(user, org_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "sin permiso sobre esta organización")
    return org_id


@router.get("/api-keys")
async def list_security_api_keys(
    org: int | None = Query(default=None),
    user: CurrentUser = Depends(_SUPER),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lista las API keys de la org resuelta (nunca expone el hash)."""
    org_id = _resolve_org(user, org)
    rows = (await db.execute(text("""
        SELECT id, name, scope, created_at, last_used_at, revoked_at
        FROM api_keys WHERE organization_id = :org ORDER BY id DESC
    """), {"org": org_id})).mappings().all()
    return {"organization_id": org_id, "api_keys": [dict(r) for r in rows]}


@router.get("/logins")
async def list_login_risk(
    org: int | None = Query(default=None),
    user: CurrentUser = Depends(_SUPER),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Usuarios de la org en riesgo: con intentos fallidos o cuenta bloqueada."""
    org_id = _resolve_org(user, org)
    rows = (await db.execute(text("""
        SELECT id, email, is_active, failed_attempts, locked_until, last_login_at
        FROM users
        WHERE organization_id = :org
          AND (failed_attempts > 0 OR locked_until IS NOT NULL)
        ORDER BY failed_attempts DESC
    """), {"org": org_id})).mappings().all()
    return {"organization_id": org_id, "users": [dict(r) for r in rows]}


@router.post("/api-keys/{key_id}/rotate")
async def rotate_api_key(
    key_id: int, request: Request,
    user: CurrentUser = Depends(_SUPER),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rota una API key: revoca la vieja y acuña una nueva (mismo name/scope/org).

    Devuelve el texto plano de la nueva llave UNA sola vez.
    """
    # (1) carga la key
    key = (await db.execute(text("""
        SELECT organization_id, scope, name, revoked_at
        FROM api_keys WHERE id = :id
    """), {"id": key_id})).first()
    if key is None:
        raise HTTPException(404, "api key no existe")
    org_id, scope, name, revoked_at = int(key[0]), key[1], key[2], key[3]
    if not _can_manage_org(user, org_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "sin permiso sobre esta organización")
    if revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "api key ya revocada")

    # (2) contexto de auditoría antes de escribir
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))

    # (3) revoca la vieja (soft)
    await db.execute(
        text("UPDATE api_keys SET revoked_at = now() WHERE id = :id"), {"id": key_id})

    # (4) acuña la nueva con mismo name+scope+org
    plaintext = _KEY_PREFIX + secrets.token_urlsafe(32)
    new_id = (await db.execute(text("""
        INSERT INTO api_keys (name, key_hash, scope, created_by_user_id, organization_id)
        VALUES (:n, :h, :s, :uid, :org) RETURNING id
    """), {"n": name, "h": hash_key(plaintext), "s": scope,
           "uid": user.id, "org": org_id})).scalar_one()

    return {"old_key_id": key_id, "new_key_id": new_id, "api_key": plaintext}
