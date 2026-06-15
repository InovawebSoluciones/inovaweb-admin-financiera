"""Configuracion de PROVEEDORES DE EMAIL del CAF: /admin/email-providers.

Multi-tenant DELEGADO en dos niveles dentro de cada organizacion:
- config a NIVEL ORG  (client_id NULL): el remitente por defecto de la org.
- config a NIVEL CLIENTE (client_id con valor): un remitente propio de ese
  cliente, que pisa al de la org cuando se envia en su nombre.

Igual que el resto del CAF: la organization_id SIEMPRE sale del token (?org solo
lo respeta el operador de la plataforma, user.is_platform; y cuando lo pasa, las
operaciones por-id quedan acotadas a esa org). El tenant NUNCA viene del body.
SQL crudo con text() (sin ORM) y bind_actor antes de cada escritura.

SECRETO: la credencial (password SMTP / app-password / token) se guarda SOLO
cifrada en secret_encrypted con app.core.crypto.encrypt_secret (AES-256-GCM).
NUNCA se devuelve en claro ni en logs; las lecturas solo exponen secret_set.
"""

from __future__ import annotations

import smtplib
import ssl

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles

router = APIRouter(prefix="/admin/email-providers", tags=["email-providers"])

# escritura/lectura restringida a roles internos con permiso de gestion
_WRITE = require_roles("super_admin", "finanzas")

# proveedores permitidos por el CHECK de la tabla email_providers
_PROVIDERS = {"microsoft", "gmail", "smtp"}

# defaults de host/puerto por proveedor administrado. 'smtp' es generico.
_PROVIDER_DEFAULTS = {
    "microsoft": ("smtp.office365.com", 587),
    "gmail": ("smtp.gmail.com", 587),
}


# ---------------------------------------------------------------------
# helpers de aislamiento por organizacion (actor = JWT, no API key)
# ---------------------------------------------------------------------


def _resolve_org(user: CurrentUser, org_param: int | None) -> int:
    """org_param solo lo respeta el super tenant; el resto siempre su propia org."""
    if org_param is not None and user.is_platform:
        return org_param
    return user.organization_id


async def _assert_client_in_org_admin(
    db: AsyncSession, cid: int, org: int, user: CurrentUser
) -> None:
    """Garantiza que el cliente existe y pertenece a `org` (404/403)."""
    row = (await db.execute(
        text("SELECT organization_id FROM clients WHERE id=:c"), {"c": cid}
    )).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cliente no existe")
    if not (user.is_platform or int(row[0]) == org):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cliente de otra organizacion")


async def _load_provider_scoped(
    db: AsyncSession, pid: int, user: CurrentUser, org_param: int | None
) -> dict:
    """Carga un proveedor por id acotado a la org resuelta.

    404 si no existe. 403 si no pertenece a la org resuelta. El super tenant SIN
    ?org tiene acceso global; con ?org=<id> queda acotado a esa org (el ?org
    actua como salvaguarda de ambito, no solo como filtro de listado).
    Devuelve la fila (organization_id, client_id) para reusar sin releer.
    """
    target = _resolve_org(user, org_param)
    row = (await db.execute(
        text("SELECT id, organization_id, client_id FROM email_providers WHERE id=:i"),
        {"i": pid},
    )).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proveedor no existe")
    global_access = user.is_platform and org_param is None
    if not global_access and int(row["organization_id"]) != target:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "proveedor de otra organizacion")
    return dict(row)


def _resolve_host_port(
    provider: str, smtp_host: str | None, smtp_port: int | None
) -> tuple[str, int]:
    """Resuelve host/puerto: microsoft/gmail tienen default; smtp los exige (422)."""
    if provider in _PROVIDER_DEFAULTS:
        d_host, d_port = _PROVIDER_DEFAULTS[provider]
        return (smtp_host or d_host, smtp_port or d_port)
    if not smtp_host or not smtp_port:
        raise HTTPException(422, "provider 'smtp' requiere smtp_host y smtp_port")
    return (smtp_host, smtp_port)


# ---------------------------------------------------------------------
# esquemas de entrada
# ---------------------------------------------------------------------


class ProviderCreateIn(BaseModel):
    provider: str                    # 'microsoft' | 'gmail' | 'smtp'
    from_email: str
    secret: str                      # credencial en claro; se cifra antes de guardar
    from_name: str | None = None
    display_name: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    client_id: int | None = None     # NULL = config a nivel org


class ProviderUpdateIn(BaseModel):
    display_name: str | None = None
    from_email: str | None = None
    from_name: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    secret: str | None = None        # si viene, se RE-cifra; nunca se devuelve


def _mask(row: dict) -> dict:
    """Vista enmascarada: secret_set bool, jamas secret_encrypted ni texto plano."""
    return {
        "id": int(row["id"]),
        "provider": row["provider"],
        "display_name": row["display_name"],
        "from_email": row["from_email"],
        "smtp_host": row["smtp_host"],
        "smtp_port": row["smtp_port"],
        "is_default": row["is_default"],
        "is_active": row["is_active"],
        "secret_set": bool(row["secret_encrypted"]),
    }


# ---------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------


@router.post("")
async def create_provider(
    body: ProviderCreateIn,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Alta de proveedor en la org resuelta. organization_id NUNCA viene del body.

    Valida provider; resuelve host/puerto; si llega client_id verifica que el
    cliente es de esa org; cifra el secreto. Si es el PRIMERO de su ambito
    (misma org + mismo client_id) lo marca is_default automaticamente.
    """
    if body.provider not in _PROVIDERS:
        raise HTTPException(422, "provider invalido (microsoft|gmail|smtp)")
    if not body.from_email or not body.from_email.strip():
        raise HTTPException(422, "from_email es obligatorio")
    if not body.secret:
        raise HTTPException(422, "secret es obligatorio")

    target_org = _resolve_org(user, org)
    host, port = _resolve_host_port(body.provider, body.smtp_host, body.smtp_port)

    if body.client_id is not None:
        await _assert_client_in_org_admin(db, body.client_id, target_org, user)

    # primer proveedor del ambito -> default automatico (NULL = nivel org)
    exists = (await db.execute(text(
        "SELECT 1 FROM email_providers WHERE organization_id=:org "
        "AND client_id IS NOT DISTINCT FROM :cid LIMIT 1"
    ), {"org": target_org, "cid": body.client_id})).first()
    make_default = exists is None

    secret_enc = encrypt_secret(body.secret)

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    row = (await db.execute(text("""
        INSERT INTO email_providers
          (organization_id, client_id, provider, display_name, from_email,
           from_name, smtp_host, smtp_port, smtp_user, secret_encrypted, is_default)
        VALUES
          (:org, :cid, :provider, :display_name, :from_email,
           :from_name, :host, :port, :smtp_user, :secret_enc, :is_default)
        RETURNING id, provider, organization_id, client_id, is_default
    """), {
        "org": target_org, "cid": body.client_id, "provider": body.provider,
        "display_name": body.display_name, "from_email": body.from_email,
        "from_name": body.from_name, "host": host, "port": port,
        "smtp_user": body.smtp_user, "secret_enc": secret_enc,
        "is_default": make_default,
    })).mappings().one()
    await db.commit()
    return {"id": int(row["id"]), "provider": row["provider"],
            "organization_id": int(row["organization_id"]),
            "client_id": row["client_id"], "is_default": row["is_default"]}


@router.get("")
async def list_providers(
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
    client_id: int | None = Query(None),
):
    """Lista enmascarada de proveedores de la org (filtrable por client_id)."""
    target_org = _resolve_org(user, org)
    params: dict = {"org": target_org}
    sql = ("SELECT id, provider, display_name, from_email, smtp_host, smtp_port, "
           "is_default, is_active, secret_encrypted "
           "FROM email_providers WHERE organization_id=:org")
    if client_id is not None:
        await _assert_client_in_org_admin(db, client_id, target_org, user)
        sql += " AND client_id=:cid"
        params["cid"] = client_id
    sql += " ORDER BY client_id NULLS FIRST, is_default DESC, id"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {"providers": [_mask(r) for r in rows]}


@router.get("/{pid}")
async def get_provider(
    pid: int,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Detalle enmascarado de un proveedor (secret_set, nunca el secreto)."""
    await _load_provider_scoped(db, pid, user, org)
    row = (await db.execute(text(
        "SELECT id, provider, display_name, from_email, smtp_host, smtp_port, "
        "is_default, is_active, secret_encrypted FROM email_providers WHERE id=:i"
    ), {"i": pid})).mappings().one()
    return _mask(row)


@router.patch("/{pid}")
async def update_provider(
    pid: int,
    body: ProviderUpdateIn,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Edita campos enviados (COALESCE de no-secretos) + updated_at. Auditado.

    Si viene secret se RE-cifra; si no, conserva el cifrado actual. No cambia
    provider/organization_id/client_id (el ambito es inmutable).
    """
    await _load_provider_scoped(db, pid, user, org)
    secret_enc = encrypt_secret(body.secret) if body.secret else None

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    await db.execute(text("""
        UPDATE email_providers SET
          display_name     = COALESCE(:display_name, display_name),
          from_email       = COALESCE(:from_email, from_email),
          from_name        = COALESCE(:from_name, from_name),
          smtp_host        = COALESCE(:smtp_host, smtp_host),
          smtp_port        = COALESCE(:smtp_port, smtp_port),
          smtp_user        = COALESCE(:smtp_user, smtp_user),
          secret_encrypted = COALESCE(:secret_enc, secret_encrypted),
          updated_at       = now()
        WHERE id = :id
    """), {
        "display_name": body.display_name, "from_email": body.from_email,
        "from_name": body.from_name, "smtp_host": body.smtp_host,
        "smtp_port": body.smtp_port, "smtp_user": body.smtp_user,
        "secret_enc": secret_enc, "id": pid,
    })
    await db.commit()
    return {"id": pid, "updated": True}


@router.post("/{pid}/set-default")
async def set_default_provider(
    pid: int,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Marca este proveedor como default y desmarca los del MISMO ambito.

    Ambito = misma organization_id Y mismo client_id (IS NOT DISTINCT FROM trata
    NULL=nivel-org como valor, sin colisionar con los de clientes).
    """
    row = await _load_provider_scoped(db, pid, user, org)
    own_org, own_cid = int(row["organization_id"]), row["client_id"]

    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    await db.execute(text("""
        UPDATE email_providers SET is_default = false, updated_at = now()
        WHERE organization_id = :org AND client_id IS NOT DISTINCT FROM :cid
          AND is_default = true
    """), {"org": own_org, "cid": own_cid})
    await db.execute(text("""
        UPDATE email_providers SET is_default = true, updated_at = now()
        WHERE id = :id
    """), {"id": pid})
    await db.commit()
    return {"id": pid, "is_default": True}


@router.post("/{pid}/toggle")
async def toggle_provider(
    pid: int,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Invierte is_active del proveedor. Auditado."""
    await _load_provider_scoped(db, pid, user, org)
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    row = (await db.execute(text("""
        UPDATE email_providers SET is_active = NOT is_active, updated_at = now()
        WHERE id = :id RETURNING is_active
    """), {"id": pid})).mappings().one()
    await db.commit()
    return {"id": pid, "is_active": row["is_active"]}


@router.post("/{pid}/test")
async def test_provider(
    pid: int,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    org: int | None = Query(None),
):
    """Prueba la conexion SMTP descifrando el secreto (best-effort).

    Resuelve host/puerto, descifra la credencial y hace connect + login con
    smtplib (STARTTLS si 587, SMTP_SSL si 465; timeout 10s). Captura toda
    excepcion y responde {ok, detail}. El detalle es el TIPO de error (no el
    mensaje crudo) para no devolver datos del servidor; el secreto NUNCA se
    incluye en la respuesta ni en logs.
    """
    await _load_provider_scoped(db, pid, user, org)
    row = (await db.execute(text(
        "SELECT provider, from_email, smtp_host, smtp_port, smtp_user, "
        "secret_encrypted FROM email_providers WHERE id=:i"
    ), {"i": pid})).mappings().one()

    if not row["secret_encrypted"]:
        return {"ok": False, "detail": "el proveedor no tiene secreto configurado"}
    try:
        host, port = _resolve_host_port(
            row["provider"], row["smtp_host"], row["smtp_port"]
        )
    except HTTPException:
        return {"ok": False, "detail": "smtp_host/smtp_port no configurados"}

    login_user = row["smtp_user"] or row["from_email"]
    try:
        secret = decrypt_secret(row["secret_encrypted"])
    except ValueError:
        return {"ok": False, "detail": "secreto invalido o ilegible (revisar AES_KEY)"}

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=10,
                                  context=ssl.create_default_context()) as srv:
                srv.login(login_user, secret)
        else:
            with smtplib.SMTP(host, port, timeout=10) as srv:
                srv.ehlo()
                if port == 587:
                    srv.starttls(context=ssl.create_default_context())
                    srv.ehlo()
                srv.login(login_user, secret)
        return {"ok": True, "detail": f"conexion y login OK ({host}:{port})"}
    except Exception as e:  # noqa: BLE001 - best-effort; solo el TIPO, nunca el secreto
        return {"ok": False, "detail": f"fallo de conexion/login: {type(e).__name__}"}
