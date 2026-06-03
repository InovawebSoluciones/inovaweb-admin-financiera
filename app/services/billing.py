"""Cierre mensual: consumo + plan + promociones -> invoice draft."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clients.medidor_client import MedidorClient
from app.services.promotions import apply_promotions

log = logging.getLogger("billing")


@dataclass(slots=True)
class ClosingSummary:
    period_start: str
    period_end: str
    clients_processed: int
    invoices_created: int
    total_billed_cents: int
    errors: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start, "period_end": self.period_end,
            "clients_processed": self.clients_processed,
            "invoices_created": self.invoices_created,
            "total_billed_cents": self.total_billed_cents,
            "errors": self.errors,
        }


async def run_monthly_closing(db: AsyncSession, ref_date: date | None = None) -> dict[str, Any]:
    """Genera invoice draft para cada suscripcion activa del mes cerrado.

    ref_date = primer dia del mes que se cierra. Default: primer dia del mes actual
    (es decir, cierra el mes anterior).
    """
    today = ref_date or date.today().replace(day=1)
    period_end = today - timedelta(days=1)
    period_start = period_end.replace(day=1)

    subs = (await db.execute(text("""
        SELECT s.id AS subscription_id, s.client_id, s.plan_id,
               p.monthly_fee_cents, p.is_free,
               c.medidor_account_id, c.legal_name
        FROM subscriptions s
        JOIN plans p ON p.id = s.plan_id
        JOIN clients c ON c.id = s.client_id
        WHERE s.status='active' AND c.status='active'
    """))).mappings().all()

    summary = ClosingSummary(
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        clients_processed=0, invoices_created=0,
        total_billed_cents=0, errors=[],
    )

    medidor = MedidorClient()
    try:
        for s in subs:
            summary.clients_processed += 1
            try:
                inv_id, total = await _close_one_subscription(
                    db, medidor, dict(s), period_start, period_end,
                )
                if inv_id is not None:
                    summary.invoices_created += 1
                    summary.total_billed_cents += total
            except Exception as e:
                log.exception("closing_failed", extra={
                    "client_id": s["client_id"], "subscription_id": s["subscription_id"],
                })
                summary.errors.append({
                    "client_id": s["client_id"],
                    "subscription_id": s["subscription_id"],
                    "error": str(e),
                })
    finally:
        await medidor.close()

    return summary.as_dict()


async def _close_one_subscription(
    db: AsyncSession,
    medidor: MedidorClient,
    sub: dict[str, Any],
    period_start: date,
    period_end: date,
) -> tuple[int | None, int]:
    # idempotencia: no duplicar factura del mismo periodo
    existing = (await db.execute(text("""
        SELECT id FROM invoices
        WHERE client_id = :c AND subscription_id = :s
          AND period_start = :ps AND period_end = :pe
          AND status <> 'cancelled'
    """), {
        "c": sub["client_id"], "s": sub["subscription_id"],
        "ps": period_start, "pe": period_end,
    })).first()
    if existing:
        return None, 0

    items: list[dict[str, Any]] = []
    subtotal = 0

    # 1) cargo fijo del plan
    if sub["monthly_fee_cents"] > 0:
        items.append({
            "description": f"Cuota mensual plan ({period_start.isoformat()} a {period_end.isoformat()})",
            "quantity": 1, "unit_price_cents": sub["monthly_fee_cents"],
            "amount_cents": sub["monthly_fee_cents"],
            "sat_key": "81112101", "unit_sat_key": "E48",
            "service_id": None,
        })
        subtotal += sub["monthly_fee_cents"]

    # 2) consumo medido (si tiene cuenta medidor)
    if sub["medidor_account_id"]:
        try:
            usage = await medidor.get_usage(
                sub["medidor_account_id"],
                period_start.isoformat(), period_end.isoformat(),
            )
        except Exception as e:
            log.warning("usage_fetch_failed", extra={
                "client_id": sub["client_id"], "error": str(e),
            })
            usage = {"items": []}

        # plan_items: limites incluidos por service_id
        plan_limits = {
            r["service_id"]: dict(r) for r in (await db.execute(text("""
                SELECT pi.service_id, pi.included_units, pi.overage_price_cents,
                       s.code, s.name, s.unit_price_cents
                FROM plan_items pi JOIN services s ON s.id = pi.service_id
                WHERE pi.plan_id = :p
            """), {"p": sub["plan_id"]})).mappings().all()
        }

        for u in usage.get("items", []):
            service_code = u.get("service_code")
            units = int(u.get("units", 0))
            # localiza service_id por code (cache simple)
            svc_row = (await db.execute(
                text("SELECT id, unit_price_cents FROM services WHERE code = :c"),
                {"c": service_code},
            )).mappings().first()
            if not svc_row:
                continue
            service_id = svc_row["id"]
            unit_price = svc_row["unit_price_cents"]

            limit_info = plan_limits.get(service_id)
            included = limit_info["included_units"] if limit_info else 0
            overage_price = (limit_info or {}).get("overage_price_cents") or unit_price
            billable = max(units - included, 0)
            if billable == 0:
                continue
            amount = billable * overage_price
            items.append({
                "description": f"Consumo {service_code}: {billable} sobre incluidas",
                "quantity": billable, "unit_price_cents": overage_price,
                "amount_cents": amount,
                "sat_key": "81112200", "unit_sat_key": "E48",
                "service_id": service_id,
            })
            subtotal += amount

    if not items:
        return None, 0  # plan free sin consumo

    # 3) promociones
    discount = await apply_promotions(db, client_id=sub["client_id"],
                                       subtotal_cents=subtotal,
                                       period_start=period_start, period_end=period_end)

    # 4) impuestos (IVA 16% sobre subtotal - descuento)
    taxable = max(subtotal - discount, 0)
    tax = round(taxable * 0.16)
    total = taxable + tax

    # 5) insert invoice (draft)
    row = await db.execute(text("""
        INSERT INTO invoices
          (client_id, subscription_id, period_start, period_end,
           subtotal_cents, discount_cents, tax_cents, total_cents, status)
        VALUES (:c, :s, :ps, :pe, :sub, :disc, :tax, :tot, 'draft')
        RETURNING id
    """), {
        "c": sub["client_id"], "s": sub["subscription_id"],
        "ps": period_start, "pe": period_end,
        "sub": subtotal, "disc": discount, "tax": tax, "tot": total,
    })
    invoice_id = row.scalar_one()

    for it in items:
        await db.execute(text("""
            INSERT INTO invoice_items
              (invoice_id, service_id, description, quantity, unit_price_cents,
               amount_cents, sat_key, unit_sat_key)
            VALUES (:i, :sv, :d, :q, :up, :a, :sk, :uk)
        """), {
            "i": invoice_id, "sv": it["service_id"], "d": it["description"],
            "q": it["quantity"], "up": it["unit_price_cents"],
            "a": it["amount_cents"], "sk": it["sat_key"], "uk": it["unit_sat_key"],
        })

    # marcar pending_stamp para que invoice_retry worker lo timbre
    await db.execute(text(
        "UPDATE invoices SET status='pending_stamp' WHERE id=:i"
    ), {"i": invoice_id})

    return invoice_id, total
