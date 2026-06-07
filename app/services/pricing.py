"""Capa de tarificacion (rating) del CAF.

Convierte la CANTIDAD cruda que miden los cores (tokens de IA / mensajes por
canal) en PESOS al precio publico, usando `price_catalog` (migracion 007).

Principios (ver docs/MODELO-COBRO.md):
  - El precio se decide AQUI (capa comercial del CAF), nunca en Finanzas.
  - Precision SUB-CENTAVO: el precio unitario esta en micro-pesos
    (1 peso = 1_000_000 micros; 1 centavo = 10_000 micros). Se agrega el total
    en micros y se redondea UNA sola vez a centavos -> evita la sub-facturacion
    por redondear cada operacion a 0 (hallazgo A04 de Scraping).
  - Se calcula tambien el costo del proveedor -> margen = precio - costo.

Centavos siempre BIGINT (int de Python). Nada de floats en el resultado.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MICROS_PER_CENT = 10_000  # 1 peso = 100 centavos = 1_000_000 micros


class PricingError(Exception):
    """No hay precio activo para (meter, unit_code), o cantidad invalida."""


@dataclass(frozen=True)
class PriceResult:
    """Resultado de tarificar una cantidad consumida.

    Attributes:
        meter: dominio ('ia' | 'message').
        unit_code: unidad tarificada ('token', 'email', 'whatsapp', ...).
        quantity: cantidad consumida (tokens o numero de mensajes).
        amount_cents: monto a cobrar al cliente, en centavos BIGINT.
        cost_cents: costo del proveedor, en centavos BIGINT (COGS).
        margin_cents: amount_cents - cost_cents.
    """

    meter: str
    unit_code: str
    quantity: int
    amount_cents: int
    cost_cents: int
    margin_cents: int


def _micros_to_cents(micros: int) -> int:
    """Convierte micro-pesos a centavos con redondeo HALF_UP, una sola vez.

    1 centavo = 10_000 micros. El redondeo se aplica al TOTAL agregado, no por
    operacion, para no perder importes sub-centavo.
    """
    return int(
        (Decimal(micros) / Decimal(MICROS_PER_CENT)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


async def price_quantity(
    db: AsyncSession, *, meter: str, unit_code: str, quantity: int
) -> PriceResult:
    """Tarifica `quantity` unidades de (`meter`, `unit_code`) al precio publico.

    Busca el precio ACTIVO en `price_catalog`, multiplica por la cantidad a
    precision de micros y redondea una sola vez a centavos. Devuelve monto al
    publico, costo y margen.

    Args:
        db: sesion async.
        meter: 'ia' | 'message'.
        unit_code: 'token' para ia; canal ('email', 'whatsapp', 'sms',
            'instagram', 'messenger') para message.
        quantity: cantidad consumida (>= 0). tokens o numero de mensajes.

    Returns:
        PriceResult con amount_cents / cost_cents / margin_cents (BIGINT).

    Raises:
        PricingError: cantidad negativa o sin precio activo para la unidad.
    """
    if quantity < 0:
        raise PricingError(f"cantidad negativa: {quantity}")

    row = (await db.execute(text("""
        SELECT public_price_micros, cost_price_micros
        FROM price_catalog
        WHERE meter = :m AND unit_code = :u AND is_active
        LIMIT 1
    """), {"m": meter, "u": unit_code})).mappings().first()
    if not row:
        raise PricingError(
            f"sin precio activo para meter={meter} unit_code={unit_code}"
        )

    public_micros = int(row["public_price_micros"]) * int(quantity)
    cost_micros = int(row["cost_price_micros"]) * int(quantity)
    amount_cents = _micros_to_cents(public_micros)
    cost_cents = _micros_to_cents(cost_micros)
    return PriceResult(
        meter=meter,
        unit_code=unit_code,
        quantity=int(quantity),
        amount_cents=amount_cents,
        cost_cents=cost_cents,
        margin_cents=amount_cents - cost_cents,
    )
