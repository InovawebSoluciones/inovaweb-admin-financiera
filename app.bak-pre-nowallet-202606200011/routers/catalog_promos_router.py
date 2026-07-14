"""CRUD JSON de PRODUCTOS y PROMOCIONES del catalogo (Bloque 1).

Multi-tenant: cada organizacion administra los suyos; el operador de la
plataforma (super tenant / is_platform) puede actuar sobre cualquier org via
?org=<id>. La organization_id SIEMPRE se resuelve del token (o de ?org solo si
es plataforma), NUNCA del body.

SQL crudo con text() (sin ORM), mismo patron que admin_router. bind_actor antes
de cada escritura para que los triggers de auditoria registren el actor.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles

router = APIRouter(prefix="/admin/catalog", tags=["catalog"])

# escritura: solo roles internos con permiso de mutar catalogo
_WRITE = require_roles("super_admin", "finanzas")


# ---------------------------------------------------------------------
# helpers de tenancy (self-contained)
# ---------------------------------------------------------------------


def _resolve_org(user: CurrentUser, org_param: int | None) -> int:
    """Resuelve la organization_id efectiva para una escritura.

    - Usuario normal: SIEMPRE su propia org (ignora ?org si lo manda).
    - Operador de plataforma (is_platform): puede apuntar a otra org via ?org;
      si no lo manda, opera sobre la suya.
    """
    if user.is_platform and org_param is not None:
        if org_param <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "org invalida")
        return org_param
    return user.organization_id


async def _assert_product_in_org(
    db: AsyncSession, row_id: int, user: CurrentUser
) -> int:
    """Verifica que el producto exista y, si el usuario no es plataforma, que
    pertenezca a su org. Devuelve la organization_id real de la fila.

    404 si no existe; 403 si existe pero es de otra org (sin filtrarlo en el
    WHERE para distinguir 'no existe' de 'no autorizado')."""
    row = (await db.execute(
        text("SELECT organization_id FROM products WHERE id = :id"),
        {"id": row_id},
    )).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "producto no existe")
    if not user.is_platform and row["organization_id"] != user.organization_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "fuera de tu organizacion")
    return int(row["organization_id"])


async def _assert_promo_in_org(
    db: AsyncSession, row_id: int, user: CurrentUser
) -> int:
    """Igual que _assert_product_in_org pero para promotions."""
    row = (await db.execute(
        text("SELECT organization_id FROM promotions WHERE id = :id"),
        {"id": row_id},
    )).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "promocion no existe")
    if not user.is_platform and row["organization_id"] != user.organization_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "fuera de tu organizacion")
    return int(row["organization_id"])


async def _bind(db: AsyncSession, request: Request, user: CurrentUser) -> None:
    """Fija el actor de auditoria dentro de la transaccion actual."""
    await bind_actor(
        db,
        actor_user_id=user.id,
        actor_ip=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------
# cuerpos Pydantic (solo columnas reales de cada tabla; dinero en centavos)
# ---------------------------------------------------------------------


class ProductIn(BaseModel):
    """Alta de producto. organization_id NO va aqui (se resuelve del token)."""
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    sat_key: str = Field(..., min_length=1, max_length=64)
    unit_sat_key: str = Field(..., min_length=1, max_length=64)
    is_active: bool = True


class ProductPatch(BaseModel):
    """Edicion parcial: solo los campos enviados se actualizan (COALESCE)."""
    code: str | None = Field(None, min_length=1, max_length=64)
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    sat_key: str | None = Field(None, min_length=1, max_length=64)
    unit_sat_key: str | None = Field(None, min_length=1, max_length=64)
    is_active: bool | None = None


class PromotionIn(BaseModel):
    """Alta de promocion. discount_cents en centavos enteros (BIGINT)."""
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)
    kind: str = Field(..., description="coupon|seasonal|volume|referral|free_plan")
    discount_pct: float | None = Field(None, ge=0, le=100)
    discount_cents: int | None = Field(None, ge=0)
    valid_from: datetime
    valid_to: datetime
    max_uses: int | None = Field(None, ge=0)
    min_amount_cents: int = Field(0, ge=0)
    is_active: bool = True


class PromotionPatch(BaseModel):
    """Edicion parcial de promocion."""
    code: str | None = Field(None, min_length=1, max_length=64)
    name: str | None = Field(None, min_length=1, max_length=255)
    kind: str | None = None
    discount_pct: float | None = Field(None, ge=0, le=100)
    discount_cents: int | None = Field(None, ge=0)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    max_uses: int | None = Field(None, ge=0)
    min_amount_cents: int | None = Field(None, ge=0)
    is_active: bool | None = None


# ---------------------------------------------------------------------
# PRODUCTOS
# ---------------------------------------------------------------------


@router.post("/products", status_code=status.HTTP_201_CREATED)
async def create_product(
    body: ProductIn,
    request: Request,
    org: int | None = None,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Crea un producto en la org resuelta del token (o ?org si es plataforma).

    409 si el code ya existe (UNIQUE global products_code_key)."""
    org_id = _resolve_org(user, org)
    await _bind(db, request, user)
    try:
        row = (await db.execute(text("""
            INSERT INTO products
              (code, name, description, sat_key, unit_sat_key, is_active,
               organization_id)
            VALUES
              (:code, :name, :description, :sat_key, :unit_sat_key, :is_active,
               :org)
            RETURNING id
        """), {
            "code": body.code, "name": body.name, "description": body.description,
            "sat_key": body.sat_key, "unit_sat_key": body.unit_sat_key,
            "is_active": body.is_active, "org": org_id,
        })).mappings().one()
    except IntegrityError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "code de producto duplicado") from e
    return {"id": row["id"], "organization_id": org_id}


@router.patch("/products/{pid}")
async def update_product(
    pid: int,
    body: ProductPatch,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Edita un producto (COALESCE de los campos enviados) + updated_at."""
    await _assert_product_in_org(db, pid, user)
    await _bind(db, request, user)
    try:
        res = await db.execute(text("""
            UPDATE products SET
              code         = COALESCE(:code, code),
              name         = COALESCE(:name, name),
              description  = COALESCE(:description, description),
              sat_key      = COALESCE(:sat_key, sat_key),
              unit_sat_key = COALESCE(:unit_sat_key, unit_sat_key),
              is_active    = COALESCE(:is_active, is_active),
              updated_at   = now()
            WHERE id = :id
            RETURNING id
        """), {
            "code": body.code, "name": body.name, "description": body.description,
            "sat_key": body.sat_key, "unit_sat_key": body.unit_sat_key,
            "is_active": body.is_active, "id": pid,
        })
    except IntegrityError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "code de producto duplicado") from e
    if res.first() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "producto no existe")
    return {"id": pid, "status": "updated"}


@router.post("/products/{pid}/toggle")
async def toggle_product(
    pid: int,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Invierte is_active del producto (products SI tiene is_active)."""
    await _assert_product_in_org(db, pid, user)
    await _bind(db, request, user)
    row = (await db.execute(text("""
        UPDATE products SET is_active = NOT is_active, updated_at = now()
        WHERE id = :id
        RETURNING id, is_active
    """), {"id": pid})).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "producto no existe")
    return {"id": pid, "is_active": row["is_active"]}


# ---------------------------------------------------------------------
# PROMOCIONES
# ---------------------------------------------------------------------


@router.post("/promotions", status_code=status.HTTP_201_CREATED)
async def create_promotion(
    body: PromotionIn,
    request: Request,
    org: int | None = None,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Crea una promocion en la org resuelta. 409 si el code ya existe.

    Reglas de la BD (CHECK): valid_to > valid_from y al menos uno de
    discount_pct/discount_cents; se exigen aqui para devolver 400 limpio."""
    if body.valid_to <= body.valid_from:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "valid_to debe ser > valid_from")
    if body.discount_pct is None and body.discount_cents is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "se requiere discount_pct o discount_cents",
        )
    org_id = _resolve_org(user, org)
    await _bind(db, request, user)
    try:
        row = (await db.execute(text("""
            INSERT INTO promotions
              (code, name, kind, discount_pct, discount_cents, valid_from,
               valid_to, max_uses, min_amount_cents, is_active, organization_id)
            VALUES
              (:code, :name, :kind, :discount_pct, :discount_cents, :valid_from,
               :valid_to, :max_uses, :min_amount_cents, :is_active, :org)
            RETURNING id
        """), {
            "code": body.code, "name": body.name, "kind": body.kind,
            "discount_pct": body.discount_pct, "discount_cents": body.discount_cents,
            "valid_from": body.valid_from, "valid_to": body.valid_to,
            "max_uses": body.max_uses, "min_amount_cents": body.min_amount_cents,
            "is_active": body.is_active, "org": org_id,
        })).mappings().one()
    except IntegrityError as e:
        # cubre code duplicado y violaciones de CHECK (kind, rangos, etc.)
        raise HTTPException(status.HTTP_409_CONFLICT, "promocion invalida o code duplicado") from e
    return {"id": row["id"], "organization_id": org_id}


@router.patch("/promotions/{pid}")
async def update_promotion(
    pid: int,
    body: PromotionPatch,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Edita una promocion (COALESCE de los campos enviados).

    promotions NO tiene updated_at -> no se toca."""
    await _assert_promo_in_org(db, pid, user)
    await _bind(db, request, user)
    try:
        res = await db.execute(text("""
            UPDATE promotions SET
              code             = COALESCE(:code, code),
              name             = COALESCE(:name, name),
              kind             = COALESCE(:kind, kind),
              discount_pct     = COALESCE(:discount_pct, discount_pct),
              discount_cents   = COALESCE(:discount_cents, discount_cents),
              valid_from       = COALESCE(:valid_from, valid_from),
              valid_to         = COALESCE(:valid_to, valid_to),
              max_uses         = COALESCE(:max_uses, max_uses),
              min_amount_cents = COALESCE(:min_amount_cents, min_amount_cents),
              is_active        = COALESCE(:is_active, is_active)
            WHERE id = :id
            RETURNING id
        """), {
            "code": body.code, "name": body.name, "kind": body.kind,
            "discount_pct": body.discount_pct, "discount_cents": body.discount_cents,
            "valid_from": body.valid_from, "valid_to": body.valid_to,
            "max_uses": body.max_uses, "min_amount_cents": body.min_amount_cents,
            "is_active": body.is_active, "id": pid,
        })
    except IntegrityError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "promocion invalida o code duplicado") from e
    if res.first() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "promocion no existe")
    return {"id": pid, "status": "updated"}


@router.post("/promotions/{pid}/toggle")
async def toggle_promotion(
    pid: int,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Invierte is_active de la promocion (promotions SI tiene is_active)."""
    await _assert_promo_in_org(db, pid, user)
    await _bind(db, request, user)
    row = (await db.execute(text("""
        UPDATE promotions SET is_active = NOT is_active
        WHERE id = :id
        RETURNING id, is_active
    """), {"id": pid})).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "promocion no existe")
    return {"id": pid, "is_active": row["is_active"]}
