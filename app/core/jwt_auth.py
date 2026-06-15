"""JWT auth con access + refresh tokens, cookies httpOnly y deps por rol."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db

_settings = get_settings()
_ALGO = "HS256"

ACCESS_COOKIE = "caf_access"
REFRESH_COOKIE = "caf_refresh"

# ---------------------------------------------------------------------
# emision
# ---------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(payload: dict[str, Any], ttl: timedelta) -> str:
    data = payload | {"iat": _now(), "exp": _now() + ttl}
    return jwt.encode(data, _settings.JWT_SECRET.get_secret_value(), algorithm=_ALGO)


def issue_access(
    user_id: int, roles: list[str], client_id: int | None, org_id: int = 1
) -> str:
    return _encode(
        {"sub": str(user_id), "roles": roles, "cid": client_id, "oid": org_id,
         "typ": "access"},
        timedelta(minutes=_settings.JWT_ACCESS_TTL_MIN),
    )


def issue_refresh(user_id: int) -> str:
    return _encode(
        {"sub": str(user_id), "typ": "refresh"},
        timedelta(days=_settings.JWT_REFRESH_TTL_DAYS),
    )


def set_auth_cookies(response: Response, access: str, refresh: str) -> None:
    common = dict(
        httponly=True,
        secure=(_settings.ENV != "dev"),
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        ACCESS_COOKIE, access,
        max_age=_settings.JWT_ACCESS_TTL_MIN * 60,
        **common,
    )
    response.set_cookie(
        REFRESH_COOKIE, refresh,
        max_age=_settings.JWT_REFRESH_TTL_DAYS * 24 * 3600,
        **common,
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")


# ---------------------------------------------------------------------
# decodificacion
# ---------------------------------------------------------------------


def decode(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, _settings.JWT_SECRET.get_secret_value(), algorithms=[_ALGO])
    except JWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token invalido") from e


# ---------------------------------------------------------------------
# dependencias FastAPI
# ---------------------------------------------------------------------


class CurrentUser:
    __slots__ = ("id", "roles", "client_id", "organization_id")

    def __init__(self, id: int, roles: list[str], client_id: int | None,
                 organization_id: int = 1):
        self.id = id
        self.roles = roles
        self.client_id = client_id
        self.organization_id = organization_id

    def has(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)

    @property
    def is_platform(self) -> bool:
        """True para el operador de la plataforma (super_admin de la org 1 Inovaweb),
        que ve/administra TODAS las organizaciones. El resto solo ve la suya."""
        from app.core.tenancy import PLATFORM_ORG_ID
        return self.organization_id == PLATFORM_ORG_ID and "super_admin" in self.roles


async def current_user(request: Request) -> CurrentUser:
    # bearer header tiene prioridad (API JSON); fallback cookie (UI HTML)
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
    token = token or request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no autenticado")

    payload = decode(token)
    if payload.get("typ") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token tipo invalido")

    return CurrentUser(
        id=int(payload["sub"]),
        roles=list(payload.get("roles", [])),
        client_id=payload.get("cid"),
        organization_id=int(payload.get("oid") or 1),
    )


def require_roles(*roles: str):
    async def _dep(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not user.has(*roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "rol insuficiente")
        return user
    return _dep


# ---------------------------------------------------------------------
# login helper
# ---------------------------------------------------------------------


async def login_user(
    db: AsyncSession, email: str, password: str
) -> tuple[CurrentUser, str, str]:
    """Verifica credenciales y devuelve user + tokens. Lanza 401 si invalido."""
    from app.core.password import verify_password

    # import diferido para evitar ciclos
    from sqlalchemy import text

    row = await db.execute(text("""
        SELECT u.id, u.password_hash, u.is_active, u.locked_until, u.client_id,
               u.organization_id,
               COALESCE(array_agg(r.code) FILTER (WHERE r.code IS NOT NULL), '{}') AS roles
        FROM users u
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        LEFT JOIN roles r       ON r.id = ur.role_id
        WHERE u.email = :email
        GROUP BY u.id
    """), {"email": email})
    user = row.mappings().first()
    if user is None or not user["is_active"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "credenciales invalidas")
    if user["locked_until"] and user["locked_until"] > _now():
        raise HTTPException(status.HTTP_423_LOCKED, "cuenta bloqueada temporalmente")
    if not verify_password(user["password_hash"], password):
        # incrementa failed_attempts
        await db.execute(text("""
            UPDATE users SET failed_attempts = failed_attempts + 1,
              locked_until = CASE WHEN failed_attempts + 1 >= 5
                THEN now() + interval '15 minutes' ELSE NULL END
            WHERE id = :id
        """), {"id": user["id"]})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "credenciales invalidas")

    await db.execute(text("""
        UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = now()
        WHERE id = :id
    """), {"id": user["id"]})

    roles = list(user["roles"])
    org_id = int(user["organization_id"] or 1)
    cu = CurrentUser(id=user["id"], roles=roles, client_id=user["client_id"],
                     organization_id=org_id)
    return cu, issue_access(cu.id, roles, cu.client_id, org_id), issue_refresh(cu.id)
