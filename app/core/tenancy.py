"""Capa multi-organizacion (tenancy) del CAF Billing Engine SaaS.

Resuelve la organizacion de cada peticion app-facing a partir de la API key
(tabla `api_keys`, hash SHA-256), con fallback a las llaves legacy de entorno
(SCRAPING_ADMIN_KEY / SWIGG_ADMIN_KEY -> org 1 Inovaweb) para no romper a
LiaForge/Swigg. Todo dato de una org se aisla por `organization_id`; el tenant
se resuelve SIEMPRE de la llave, nunca del body (regla rector 7).
"""

from __future__ import annotations

import hashlib

from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# org canonica del operador de la plataforma (Inovaweb) y de las llaves legacy.
PLATFORM_ORG_ID = 1


def hash_key(plaintext: str) -> str:
    """Hash canonico de una API key (SHA-256 hex). Mismo algoritmo en alta y verificacion."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "falta bearer token")
    return auth.removeprefix("Bearer ").strip()


async def _resolve_app_org_row(request: Request, db: AsyncSession) -> tuple[int, str]:
    """Devuelve (organization_id, scope) dueño de la API key de la peticion.

    scope = 'admin' para las llaves legacy de plataforma y para llaves sin scope
    explicito (compat hacia atras). Lanza 401 si la llave no resuelve a ninguna org.
    """
    token = _bearer(request)
    kh = hash_key(token)
    row = (await db.execute(
        text("SELECT id, organization_id, scope FROM api_keys "
             "WHERE key_hash = :h AND revoked_at IS NULL"),
        {"h": kh},
    )).first()
    if row is not None:
        # marca de uso (best-effort; no bloquea si falla)
        await db.execute(
            text("UPDATE api_keys SET last_used_at = now() WHERE id = :id"),
            {"id": row[0]},
        )
        return int(row[1]), (row[2] or "admin")

    # fallback legacy (llaves en .env de Inovaweb -> org plataforma)
    from app.core.config import get_settings
    s = get_settings()
    legacy = [s.SCRAPING_ADMIN_KEY.get_secret_value()]
    if getattr(s, "SWIGG_ADMIN_KEY", None) is not None:
        legacy.append(s.SWIGG_ADMIN_KEY.get_secret_value())
    if token in legacy:
        return PLATFORM_ORG_ID, "admin"

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid app key")


async def resolve_app_org(request: Request, db: AsyncSession) -> int:
    """`organization_id` dueño de la API key (uso general/lectura)."""
    org, _scope = await _resolve_app_org_row(request, db)
    return org


async def resolve_app_org_write(request: Request, db: AsyncSession) -> int:
    """Como `resolve_app_org` pero EXIGE permiso de escritura. [FEA 2026-07-27]

    Las llaves con scope 'readonly' NO pueden mover dinero (charge/recharge/
    onboard): antes el scope se ignoraba por completo y una llave etiquetada
    readonly podia cobrar, debitar saldo y crear clientes con credito.
    """
    org, scope = await _resolve_app_org_row(request, db)
    if scope == "readonly":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "esta API key es de solo lectura; no puede realizar esta operacion",
        )
    return org


async def assert_client_in_org(db: AsyncSession, client_id: int, org_id: int) -> None:
    """Verifica que `client_id` pertenece a `org_id`. 404 si no (no filtra existencia).

    Es el control de aislamiento central: impide que la org A opere (cargue,
    consulte saldo) sobre un cliente de la org B.
    """
    owned = (await db.execute(
        text("SELECT 1 FROM clients WHERE id = :c AND organization_id = :o"),
        {"c": client_id, "o": org_id},
    )).first()
    if owned is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cliente no existe")
