"""Gestion de USUARIOS y ROLES por organizacion (RBAC): /admin/users.

API JSON, multi-tenant delegado: cada organizacion administra SU equipo. El
super_admin de la org gestiona usuarios; el operador de la plataforma (super
tenant, user.is_platform) puede operar sobre cualquier organizacion via ?org=<id>.

Patron del proyecto (igual que catalog_services_router): SQL crudo con text()
(sin ORM), _resolve_org/_assert_*_in_org self-contained y bind_actor antes de
cada escritura para que el trigger de auditoria registre al actor.

Roles administrables por una org (los del portal cliente_* NO se gestionan aqui):
  - super_admin : dueño/owner del equipo de la org
  - finanzas    : admin operativo
  - lectura     : viewer
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles
from app.core.password import hash_password

router = APIRouter(prefix="/admin/users", tags=["users"])

# gestionar usuarios = solo el super_admin de la organizacion
_ADMIN = require_roles("super_admin")

# roles que una org puede asignar a su equipo (excluye los del portal cliente_*)
_ASSIGNABLE_ROLES = {"super_admin", "finanzas", "lectura"}


# ---------------------------------------------------------------------
# helpers de aislamiento por organizacion (self-contained)
# ---------------------------------------------------------------------


def _resolve_org(user: CurrentUser, org_param: int | None) -> int:
    """org_param solo lo respeta el super tenant; el resto siempre su propia org."""
    if org_param is not None and user.is_platform:
        return org_param
    return user.organization_id


def _assert_assignable(role: str) -> None:
    """Rechaza roles fuera del set administrable (p.ej. cliente_* del portal)."""
    if role not in _ASSIGNABLE_ROLES:
        raise HTTPException(422, "rol no administrable por la organizacion")


async def _assert_user_in_org(db: AsyncSession, uid: int, user: CurrentUser) -> None:
    """Garantiza que el usuario existe y pertenece a la org del actor.

    404 si no existe; 403 si es de otra org (salvo que el actor sea el operador
    de la plataforma, que puede tocar cualquier organizacion).
    """
    row = (await db.execute(
        text("SELECT organization_id, is_internal FROM users WHERE id=:i"),
        {"i": uid},
    )).first()
    if row is None:
        raise HTTPException(404, "usuario no existe")
    if not (user.is_platform or int(row[0]) == user.organization_id):
        raise HTTPException(403, "usuario de otra organizacion")


def _gen_temp_password() -> str:
    # 16 chars URL-safe (~96 bits), igual que onboarding
    return secrets.token_urlsafe(12)


# ---------------------------------------------------------------------
# esquemas de entrada
# ---------------------------------------------------------------------


class UserCreateIn(BaseModel):
    email: EmailStr
    full_name: str
    role: str


class RoleIn(BaseModel):
    role: str


# ---------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------


@router.post("")
async def create_user(
    body: UserCreateIn,
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Alta de usuario interno en la org resuelta. organization_id NUNCA del body.

    Crea el user (is_internal=FALSE, client_id=NULL) con password temporal, le
    asigna el rol y genera un activation_token (se persiste solo el SHA-256).
    409 si el email ya existe. Devuelve el password temporal en claro una vez.
    """
    _assert_assignable(body.role)
    target_org = _resolve_org(user, org)

    # 409 si el email ya existe (email es UNIQUE global)
    dup = (await db.execute(
        text("SELECT 1 FROM users WHERE email=:e"), {"e": str(body.email)},
    )).first()
    if dup is not None:
        raise HTTPException(409, "el email ya existe")

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))

    temp_pw = _gen_temp_password()
    pw_hash = hash_password(temp_pw)
    row = (await db.execute(text("""
        INSERT INTO users
          (email, password_hash, full_name, is_active, is_internal,
           client_id, organization_id)
        VALUES (:e, :h, :n, TRUE, FALSE, NULL, :org)
        RETURNING id, email, organization_id
    """), {"e": str(body.email), "h": pw_hash, "n": body.full_name,
           "org": target_org})).mappings().one()
    new_id = row["id"]

    # asigna el rol mapeado
    await db.execute(text("""
        INSERT INTO user_roles (user_id, role_id)
        SELECT :uid, id FROM roles WHERE code = :code
    """), {"uid": new_id, "code": body.role})

    # token de activacion de un solo uso (solo se guarda el hash SHA-256)
    activation_token = secrets.token_hex(32)
    token_hash = hashlib.sha256(activation_token.encode()).hexdigest()
    await db.execute(text("""
        INSERT INTO activation_tokens (user_id, token_hash)
        VALUES (:uid, :h)
    """), {"uid": new_id, "h": token_hash})

    return {"user_id": new_id, "email": row["email"],
            "organization_id": row["organization_id"], "temp_password": temp_pw}


@router.get("")
async def list_users(
    user: CurrentUser = Depends(_ADMIN),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Lista usuarios de la org resuelta con su arreglo de roles.

    Para el super tenant: sin ?org lista la org 1; con ?org la indicada.
    Para el resto: siempre su propia org.
    """
    target_org = _resolve_org(user, org)
    rows = (await db.execute(text("""
        SELECT u.id, u.email, u.full_name, u.is_active,
               COALESCE(array_agg(r.code) FILTER (WHERE r.code IS NOT NULL), '{}') AS roles
        FROM users u
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        LEFT JOIN roles r       ON r.id = ur.role_id
        WHERE u.organization_id = :org
        GROUP BY u.id
        ORDER BY u.id
    """), {"org": target_org})).mappings().all()
    return [{"id": r["id"], "email": r["email"], "full_name": r["full_name"],
             "is_active": r["is_active"], "roles": list(r["roles"])} for r in rows]


@router.post("/{uid}/roles")
async def add_role(
    uid: int,
    body: RoleIn,
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    db: AsyncSession = Depends(get_db),
):
    """Asigna un rol al usuario (idempotente: no duplica). Auditado."""
    _assert_assignable(body.role)
    await _assert_user_in_org(db, uid, user)

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    # ON CONFLICT sobre el PK (user_id, role_id) evita duplicados
    await db.execute(text("""
        INSERT INTO user_roles (user_id, role_id)
        SELECT :uid, id FROM roles WHERE code = :code
        ON CONFLICT (user_id, role_id) DO NOTHING
    """), {"uid": uid, "code": body.role})
    return {"user_id": uid, "role": body.role, "assigned": True}


@router.delete("/{uid}/roles/{role}")
async def remove_role(
    uid: int,
    role: str,
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    db: AsyncSession = Depends(get_db),
):
    """Quita un rol del usuario. Auditado."""
    _assert_assignable(role)
    await _assert_user_in_org(db, uid, user)

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    await db.execute(text("""
        DELETE FROM user_roles
        WHERE user_id = :uid
          AND role_id = (SELECT id FROM roles WHERE code = :code)
    """), {"uid": uid, "code": role})
    return {"user_id": uid, "role": role, "removed": True}


@router.post("/{uid}/toggle")
async def toggle_user(
    uid: int,
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    db: AsyncSession = Depends(get_db),
):
    """Invierte is_active del usuario. Prohibido desactivarse a si mismo. Auditado."""
    if uid == user.id:
        raise HTTPException(400, "no puedes desactivarte a ti mismo")
    await _assert_user_in_org(db, uid, user)

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    row = (await db.execute(text("""
        UPDATE users SET is_active = NOT is_active, updated_at = now()
        WHERE id = :id
        RETURNING is_active
    """), {"id": uid})).mappings().one()
    return {"user_id": uid, "is_active": row["is_active"]}


@router.post("/{uid}/reset-password")
async def reset_password(
    uid: int,
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    db: AsyncSession = Depends(get_db),
):
    """Genera un nuevo password temporal y limpia bloqueos. Auditado.

    Devuelve el password temporal en claro una sola vez.
    """
    await _assert_user_in_org(db, uid, user)

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    temp_pw = _gen_temp_password()
    pw_hash = hash_password(temp_pw)
    await db.execute(text("""
        UPDATE users SET
          password_hash   = :h,
          failed_attempts = 0,
          locked_until    = NULL,
          updated_at      = now()
        WHERE id = :id
    """), {"h": pw_hash, "id": uid})
    return {"user_id": uid, "temp_password": temp_pw}
