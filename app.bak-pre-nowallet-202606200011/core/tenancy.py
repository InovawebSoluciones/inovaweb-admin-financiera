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


async def resolve_app_org(request: Request, db: AsyncSession) -> int:
    """Devuelve el `organization_id` dueño de la API key de la peticion.

    1) Busca el hash de la llave en `api_keys` (no revocada) -> su organization_id.
    2) Fallback legacy: si coincide con SCRAPING_ADMIN_KEY/SWIGG_ADMIN_KEY del
       entorno -> org 1 (Inovaweb). Permite migrar sin romper apps existentes.
    Lanza 401 si la llave no resuelve a ninguna org.
    """
    token = _bearer(request)
    kh = hash_key(token)
    row = (await db.execute(
        text("SELECT id, organization_id FROM api_keys "
             "WHERE key_hash = :h AND revoked_at IS NULL"),
        {"h": kh},
    )).first()
    if row is not None:
        # marca de uso (best-effort; no bloquea si falla)
        await db.execute(
            text("UPDATE api_keys SET last_used_at = now() WHERE id = :id"),
            {"id": row[0]},
        )
        return int(row[1])

    # fallback legacy (llaves en .env de Inovaweb -> org plataforma)
    from app.core.config import get_settings
    s = get_settings()
    legacy = [s.SCRAPING_ADMIN_KEY.get_secret_value()]
    if getattr(s, "SWIGG_ADMIN_KEY", None) is not None:
        legacy.append(s.SWIGG_ADMIN_KEY.get_secret_value())
    if token in legacy:
        return PLATFORM_ORG_ID

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid app key")


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
