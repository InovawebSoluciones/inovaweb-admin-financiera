"""Aplicacion de promociones a un subtotal de factura.

Tipos soportados:
  coupon     - codigo manual (no se autoaplica)
  seasonal   - vigente en el periodo
  volume     - subtotal >= min_amount_cents
  referral   - manejado al alta (no aqui)
  free_plan  - manejado en planes (no aqui)
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def apply_promotions(
    db: AsyncSession,
    *,
    client_id: int,
    subtotal_cents: int,
    period_start: date,
    period_end: date,
) -> int:
    """Calcula descuento total a aplicar; registra redemptions."""
    if subtotal_cents <= 0:
        return 0

    promos = (await db.execute(text("""
        SELECT id, code, kind, discount_pct, discount_cents,
               max_uses, uses_count, min_amount_cents
        FROM promotions
        WHERE is_active
          AND kind IN ('seasonal','volume')
          AND valid_from <= :pe AND valid_to >= :ps
          AND (max_uses IS NULL OR uses_count < max_uses)
          AND COALESCE(min_amount_cents, 0) <= :sub
    """), {
        "ps": period_start, "pe": period_end, "sub": subtotal_cents,
    })).mappings().all()

    total_discount = 0
    remaining = subtotal_cents
    for p in promos:
        d = _calc_discount(p["discount_pct"], p["discount_cents"], remaining)
        if d <= 0:
            continue
        total_discount += d
        remaining = max(remaining - d, 0)

        # consume uso
        await db.execute(text("""
            UPDATE promotions SET uses_count = uses_count + 1 WHERE id = :id
        """), {"id": p["id"]})
        await db.execute(text("""
            INSERT INTO promotion_redemptions
              (promotion_id, client_id, amount_applied_cents)
            VALUES (:p, :c, :a)
        """), {"p": p["id"], "c": client_id, "a": d})

        if remaining == 0:
            break

    return min(total_discount, subtotal_cents)


def _calc_discount(pct, cents, base: int) -> int:
    if pct is not None:
        return int(round(base * float(pct) / 100))
    if cents is not None:
        return int(cents)
    return 0


# ---------------------------------------------------------------------
# cupon manual (validacion + canje)
# ---------------------------------------------------------------------


async def redeem_coupon(
    db: AsyncSession,
    *,
    code: str,
    client_id: int,
    base_amount_cents: int,
) -> int:
    p = (await db.execute(text("""
        SELECT id, discount_pct, discount_cents, max_uses, uses_count,
               valid_from, valid_to, min_amount_cents
        FROM promotions
        WHERE code = :c AND kind = 'coupon' AND is_active
          AND now() BETWEEN valid_from AND valid_to
        FOR UPDATE
    """), {"c": code})).mappings().first()
    if not p:
        raise ValueError("cupon invalido o vencido")
    if p["max_uses"] is not None and p["uses_count"] >= p["max_uses"]:
        raise ValueError("cupon agotado")
    if (p["min_amount_cents"] or 0) > base_amount_cents:
        raise ValueError("monto minimo no alcanzado")

    d = _calc_discount(p["discount_pct"], p["discount_cents"], base_amount_cents)
    await db.execute(text("UPDATE promotions SET uses_count = uses_count + 1 WHERE id = :id"),
                      {"id": p["id"]})
    await db.execute(text("""
        INSERT INTO promotion_redemptions (promotion_id, client_id, amount_applied_cents)
        VALUES (:p, :c, :a)
    """), {"p": p["id"], "c": client_id, "a": d})
    return d
